# How email and SMS work

This document follows one lead through the system: from enrollment, through each send decision, to whatever happens when they answer. File references point at the code that does each step.

## 1. Campaigns and enrollments

A **campaign** ([services/campaigns.py](../app/services/campaigns.py)) is an ordered list of steps. Each step is an SMS or an email, plus a delay:

```
Step 1  SMS    day 0   "Hi {first_name}, would you consider an offer on {address}? Reply STOP to opt out."
Step 2  SMS    +2 days "Following up on {address}…"
Step 3  EMAIL  +3 days subject "Question about {address}"
```

Before a campaign can be activated, every step goes through the **content checks** ([content_lint.py](../app/services/content_lint.py)). Errors block activation; warnings don't.

| Check | Level | Why |
|---|---|---|
| First SMS has opt-out wording ("Reply STOP") | error | Required by CTIA and 10DLC. Carriers filter cold texts without it. |
| Public link shortener (bit.ly, tinyurl…), with or without `https://` | error | Carriers filter shortened links as a spam signal |
| Unknown merge field like `{firstname}` | error | The lead would receive a literal `{firstname}` |
| SHAFT-C words (alcohol, firearms, cannabis…) | error | Restricted content on 10DLC |
| Characters outside GSM-7 (curly quotes ’ “, emoji) | warning | The text switches to UCS-2: 70 characters per segment instead of 160, which can double the cost |
| More than 2 segments | warning | Every segment is billed |
| Spammy phrases, CAPS, "!!", "$$" | warning | Hurt filtering and inbox placement |
| Email: empty or long subject, >150 words, >1 link, HTML | warning or error | Cold-email deliverability |

**Enrolling** a lead creates an `Enrollment` holding `current_step` and `next_run_at`. The first send time is the next moment the lead can be contacted, plus up to 2 hours of random **jitter**, so 500 new leads don't all go out at 8:00:00.

## 2. The dispatcher: deciding each send

The scheduler ([scheduler.py](../app/scheduler.py)) calls `campaigns.run_due()` every 30 seconds. For each enrollment whose `next_run_at` has passed, the dispatcher decides one of four things:

```
                       ┌─ replied on any channel? ──────────────── replied (human takes over)
                       │
due enrollment ────────┼─ compliance.check()  ── stop  ─────────── stopped (opted out / DNC / bounced…)
                       │                       ── defer ─────────── wait until retry_at
                       │                       ── skip  ─────────── next step (no phone / email)
                       │
                       ├─ channel-specific checks
                       │     SMS:   line type (landline → skip), any line with capacity? pacing?
                       │     email: sending window, verification (invalid → skip), mailbox capacity? pacing?
                       │
                       └─ send ─── schedule next step (+delay, inside window, + jitter) or complete
```

Every decision is written to the enrollment: `last_note` (e.g. "deferred: mailbox pacing") or `stop_reason`. The Campaigns tab shows these per lead.

### The compliance gate ([compliance.py](../app/services/compliance.py))

`check(db, lead, channel, now)` returns a `Decision(ok, reason, action, retry_at)`:

| Situation | Action | retry_at |
|---|---|---|
| No phone or email for this channel | skip | |
| On the suppression list for this channel (an SMS opt-out also covers calls) | stop | |
| SMS/voice outside 8am–9pm in the recipient's zone (all US zones if unknown; Arizona has no DST) | defer | the next allowed 15-minute slot |
| 3+ texts and calls to this phone number in 24h, across every lead sharing it | defer | ~12h later, inside the window |

Email also has a **sending window** (8am–6pm, Monday to Friday, recipient time). It isn't a legal rule, but email at 3am or on Sunday looks automated and gets fewer replies.

## 3. SMS: picking a line and sending ([sms.py](../app/services/sms.py))

1. **Line type.** On a lead's first SMS step the carrier is asked once whether the number is mobile, landline, VoIP, or invalid (Twilio Lookup v2), and the answer is cached on the lead. Landlines and invalid numbers skip SMS steps. A send that fails with a dead-number error (30003/30005/30006) also marks the number invalid.
2. **Line selection.** Lines are tried in this order:
   - the lead's *sticky* line (same conversation thread)
   - a line in the lead's area code (local presence)
   - any line

   Within each group, the healthiest line with the most capacity left today wins. A line is usable only if it's `active`, under its daily cap (150), and hasn't sent an automated text in the last 20 seconds.
3. **Pacing.** If lines have capacity but all are pacing, the step is deferred to when the first one frees up, plus a few random minutes, so waiting leads don't all retry at once.
4. **Send and record.** The `Message` row stores the carrier's status (delivered, filtered/30007, failed), and later delivery receipts update it through `/webhooks/sms/status`. Receipts can arrive out of order, so a message's status never moves backwards.

### Inbound SMS (`/webhooks/sms/inbound`, Twilio-signed)

| Text | What happens |
|---|---|
| "STOP", "unsubscribe", "please stop texting me", "don't contact me" | Suppress SMS + voice, send one confirmation, stop all sequences |
| "wrong number", "I don't own that house" | Suppress (reason `wrong_number`), stop all sequences |
| "START" | Re-subscribe, but only if the person had opted out (never undoes DNC or wrong-number entries). "Yes" is **not** a keyword. |
| Anything else | A reply: threaded to the property last texted from that line, all sequences marked `replied`, shown in the inbox |

## 4. Email: picking a mailbox and sending ([email_sender.py](../app/services/email_sender.py))

1. **Verification** ([email_verify.py](../app/services/email_verify.py)), once per lead:
   - **invalid** (never sent, suppressed): bad syntax, typo domains (`gmial.com`), disposable providers, or domains with no MX record
   - **risky** (sent, but flagged): role accounts like `info@`
   - **valid:** everything else
2. **Mailbox quota.** How many campaign emails a mailbox may send today:

   | Mailbox state | Quota |
   |---|---|
   | Mailbox or its domain paused | 0 |
   | Warm-up day < 14 | 0 |
   | Still warming | half the warm-up volume |
   | Fully warm | `daily_cold_cap` (30) |

3. **Mailbox selection.**
   - A **follow-up** must come from the mailbox its thread lives in. If that mailbox is pacing or at its cap, the follow-up waits. Only if the mailbox is *paused* does the follow-up move to another mailbox, as a new thread.
   - A **first email** goes from the lead's sticky mailbox if available. Otherwise it goes from the least-used **domain**, then the mailbox with the most quota left, which spreads risk across domains.
   - Each mailbox waits **6 minutes** between campaign emails (at most ~10 an hour).
4. **Message construction** (`build_message`):
   - `From: "Sam Carter" <sam@domain>`, plus a unique `Message-ID`
   - follow-ups get `Re: <original subject>`, `In-Reply-To`, and `References`, so they appear as one conversation in the recipient's inbox
   - `List-Unsubscribe: <https://…/u/{signed-token}>, <mailto:…>` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058, required by Gmail and Yahoo)
   - a plain-text footer with the company's physical address (CAN-SPAM) and the unsubscribe link
5. **Send** through the provider: mock, or SMTP; Gmail API or Microsoft Graph would plug in the same way. What happens next depends on the server's answer:
   - **5xx hard bounce:** suppressed immediately, and the lead's address is marked invalid.
   - **4xx temporary failure:** retried once an hour later, then the step is skipped.
   - **Success:** counted in the mailbox's daily `cold_sent`.

### Inbound email (`/webhooks/email/inbound`, shared secret)

Each inbound message is classified ([`classify_inbound`](../app/services/email_sender.py)):

| Kind | Detected by | What happens |
|---|---|---|
| **complaint** | `Feedback-Type` header / ARF report | Suppress the reported address, count against the mailbox, stop sequences |
| **bounce** | `MAILER-DAEMON@`, `report-type=delivery-status`, "Undeliverable" subjects | Read the `Final-Recipient` and status code. **5.x.x** (hard): suppress, mark invalid, count against the mailbox, stop sequences. **4.x.x** (soft): log only. |
| **auto_reply** | `Auto-Submitted`, `X-Autoreply`, `Precedence: auto_reply`, "Automatic reply" / "Out of office" subjects | Logged only. The sequence continues and it doesn't count as a reply. |
| **reply** | everything else | Threaded by `In-Reply-To` to the exact message we sent (works even when someone else in the household answers), falling back to the sender's address. Stops all sequences. Opt-out wording ("remove me", "unsubscribe") also suppresses. |

**Unsubscribe link** `/u/{token}`: the token is an HMAC-signed address, so it can't be forged. **GET** only shows a confirm button, because corporate link scanners open every URL. **POST** (the button, or Gmail/Yahoo's one-click request) suppresses the address and stops its sequences.

## 5. Keeping sending assets healthy

### Phone lines ([line_health.py](../app/services/line_health.py)), every 15 minutes
- **Score** (0–100), from the last 200 texts: carrier-filtered rate (−3 per 1%), opt-out rate (−5 per 1%), a "Spam Likely" label (−35), and reply rate (+1 per 1%). List-quality errors are excluded.
- **Below 70:** the line rests for 48h. **Below 50:** quarantined until a person decides.
- **When a rest ends:** the spam label is checked again (still flagged → rest extended), and the metrics window resets.
- **Replacing** a line buys a new number in the same area code and moves its conversations to it.

### Mailboxes ([warmup.py](../app/services/warmup.py))
- The warm-up ramp goes from 5 to 40 emails a day over 28 days.
- A mailbox **pauses** when its 7-day bounce rate is over 3% (with 50+ sends and 3+ bounces), or its complaint rate is over 0.1%. Warm-up *and* campaign mail both count.
- Resuming steps the ramp back a week.

### Domains ([domain_health.py](../app/services/domain_health.py)), daily
- A domain **pauses** (all its mailboxes stop campaign email) when:
  - it's listed on Spamhaus DBL, SURBL, or URIBL. "Query refused" answers (e.g. from public resolvers) are treated as *unknown*, not listed.
  - SPF, DKIM, or DMARC is missing
  - its 7-day bounce rate across all its mailboxes is over 5%, or its complaint rate is over 0.1%
- It resumes automatically once the cause clears.

## 6. Background jobs ([scheduler.py](../app/scheduler.py))

| Job | Every | Does |
|---|---|---|
| dispatcher | 30s | `campaigns.run_due()` |
| line_health | 15m | score, rest, quarantine, reactivate lines |
| spam_labels | 24h | reputation lookup for every line |
| domain_health | 24h | auth + blacklists (live DNS when not in mock mode) + bounce/complaint limits |
| warmup | 6h | today's warm-up volume (mock mode: simulated) |

A job that fails records its error and doesn't stop the others. The Campaigns tab shows each job's last result and can run any job on demand.

## 7. Settings ([config.py](../app/config.py))

Every threshold above can be overridden with an environment variable. See [.env.example](../.env.example). The most important:

| Setting | Default |
|---|---|
| `QUIET_HOURS_END` / `QUIET_HOURS_START` | 8 / 21 |
| `MAX_TOUCHES_PER_24H` | 3 |
| `EMAIL_SEND_START` / `EMAIL_SEND_END` | 8 / 18 (weekdays) |
| `SMS_MIN_GAP_SECONDS` / `EMAIL_MIN_GAP_MINUTES` | 20 / 6 |
| `SEND_JITTER_MINUTES` | 120 |
| `COLD_START_DAY` | 14 |
| `DEFAULT_DAILY_SMS_CAP` | 150 per line |
| `MAX_BOUNCE_RATE` / `DOMAIN_MAX_BOUNCE_RATE` | 3% / 5% |
| `MAX_COMPLAINT_RATE` | 0.1% |
| `COMPANY_NAME` / `COMPANY_ADDRESS` | used in the CAN-SPAM footer |
| `SECRET_KEY` | signs unsubscribe links; **change it in production** |
