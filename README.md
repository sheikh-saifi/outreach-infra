# Outreach Infra

A working prototype of the outbound infrastructure for reaching property owners over **SMS, voice and email**. It covers line provisioning, phone line health and spam monitoring, a single- and multi-line dialer, email deliverability checks, mailbox warm-up, a unified inbox, and a compliance layer that every send goes through.

It runs end to end on a **simulated carrier**, so you can try every feature without paying for numbers. Set `TELEPHONY_PROVIDER=twilio` to switch to the Twilio adapter. The longer plan, from R&D to production, is in [docs/ROADMAP.md](docs/ROADMAP.md).

![dashboard](docs/dashboard.png)

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000
```

On first start the app seeds a week of realistic outreach data: 15 lines across 5 metros, 360 leads, campaign traffic, replies, opt-outs, dial sessions, and 5 mailboxes at different warm-up stages. One line is a deliberately "burned" recycled number, so you can watch the monitor catch and quarantine it.

With Postgres:

```bash
docker compose up --build    # app on :8000, Postgres on :5432
```

Run the tests with `pytest` (56 tests, about 4s). There are no migrations yet, so after pulling schema changes, delete `outreach.db` and restart.

## What to try in the dashboard

| Tab | What it shows |
|---|---|
| **Overview** | Delivery and filter rates, replies, opt-outs, calls, and mailbox status for the last 7 days |
| **Phone lines** | Health score per line, spam label, daily cap usage, and why a line was rested or quarantined. Buy, rest, reactivate, and replace lines. Click a number to see its health history. |
| **Email** | **Live** SPF / DKIM / DMARC / MX check for any domain you type. Each mailbox's warm-up curve, today's quota, bounce rate, and inbox placement. |
| **Inbox** | Replies from every line, threaded by lead. Reply in place; the reply still goes through compliance. |
| **Dialer** | Run a 1/2/3/5-line session and see the abandon rate. The dialer drops lines automatically when the rate exceeds 3%. |
| **Compliance** | The suppression list (opt-outs, DNC, complaints). Add entries manually. |

**Simulate traffic** sends a live batch through the mock carrier, generates replies and opt-outs, and re-runs the health sweep.

## Architecture

```
                ┌──────────── Dashboard (static HTML + Chart.js) ────────────┐
                                           │ REST
┌──────────────────────────────── FastAPI (app/api.py) ─────────────────────────────────┐
│  lines · monitor · sms · email · dialer · inbox · suppressions · webhooks (Twilio fmt) │
└───────────────┬───────────────────────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────── services/ (business logic, no HTTP) ──────────────────────┐
│ compliance   ← every send passes through here: suppression, quiet hours, STOP/START   │
│ sms          sender selection (sticky → local presence → healthiest), inbound handling│
│ line_health  health score, rest/quarantine policy, spam-label checks, snapshots       │
│ provisioning buy / retire / replace / replenish pools                                 │
│ dialer       single + multi-line, adaptive to the 3% abandon limit                    │
│ email_auth   live DNS: MX, SPF (incl. 10-lookup limit), DKIM selectors, DMARC         │
│ warmup       ramp 5→40/day over 28d, pause on bounces/complaints, graduate            │
└───────────────┬───────────────────────────────────────────────────────────────────────┘
                │ TelephonyProvider protocol
     ┌──────────┴──────────┐
  MockCarrier          TwilioCarrier            (Telnyx / Bandwidth / own SIP trunk: same interface)
```

Data lives in SQLAlchemy models ([app/models.py](app/models.py)), which run on SQLite locally and on Postgres in Docker or production.

## Key design decisions

**One compliance gate for every channel.** `compliance.can_contact()` runs before every SMS, call, and reply, including replies an agent types in the inbox. A blocked send is still logged with `status=blocked` and a reason, which leaves an audit trail. The gate covers:

- **Quiet hours** in the *recipient's* local time (Arizona has no DST). When we can't place the recipient, the send is allowed only inside 8am–9pm in **every** US zone, Hawaii and Alaska included.
- **Frequency cap:** at most 3 texts and calls per *phone number* per 24h, the limit in the Florida, Oklahoma, and Maryland laws. It counts per phone, not per lead, because one owner often has several properties.
- **Opt-outs:** exact keywords (STOP, QUIT…) and natural-language revocations ("please stop texting me", "don't contact me"), which the FCC has required honoring since April 2025. "Yes" is deliberately **not** an opt-in keyword, because it's the most common answer to "would you consider an offer?"
- **Wrong numbers:** suppressed on every channel. Reassigned numbers are a leading source of TCPA suits.
- An SMS opt-out also covers voice. START re-subscribes only opt-outs, never DNC or wrong-number entries. Each confirmation is sent once and logged.

**Automation stops when a person replies.** Once a phone number has replied, automated follow-ups to it are blocked, and the conversation continues in the inbox with a human. Inbox replies skip the frequency cap but still go through suppression and quiet hours. A reply is threaded to the property we last texted about from that line, not just any lead with that phone.

**The health score separates line problems from list problems.** Carrier error 30007 (filtered) and opt-outs count against a line. Landline and unreachable-number errors (30003, 30005, 30006) say nothing about the line, so they're excluded. A line with a bad lead list shouldn't get rested.

| Signal | Effect on score |
|---|---|
| Carrier-filtered rate | −3 per 1% (max −60) |
| Opt-out rate | −5 per 1% (max −30) |
| "Spam Likely" label | −30 |
| Reply rate | +1 per 1% (max +10) |

Below 70 the line rests for 48h. Below 50 it's quarantined until a human reactivates or replaces it. Rates are ignored until a line has sent at least 20 messages.

When a rest ends, the line gets a fresh reputation lookup. If it's still "Spam Likely", the rest is extended. Otherwise it comes back with a **reset metrics window**, so it's judged on new traffic rather than on the traffic that got it rested. Without the reset, a rested line goes straight back into rest on the next sweep and never recovers. Replacing a line moves its conversations to the new number.

**Warm-up acts only on meaningful samples.** An early bug from testing: with 5–10 emails a day, one bounce reads as a 5% bounce rate and paused healthy mailboxes. The pause rule now needs 50+ sends and at least 3 bounces (or 2 complaints) in the trailing 7 days. Resuming a paused mailbox steps the ramp back a week instead of continuing at full volume.

**Sender selection.** The order is: the number the lead was first contacted from (sticky, so the conversation stays in one thread), then a line in the lead's area code (local presence), then the healthiest line with remaining daily capacity. Texts and calls have separate per-line daily caps (150 and 100).

**The dialer measures abandonment over 30 days.** The FTC's 3% limit applies per campaign over 30 days, so the dialer counts the trailing month, not just the current session, before deciding how many lines to dial.

**SPF lookups are counted recursively.** The 10-lookup limit includes everything nested inside each `include:`. github.com, for example, has 8 top-level mechanisms but 10 lookups in total.

**The provider interface is narrow on purpose.** `buy_number`, `release_number`, `send_sms`, `place_call`, and `reputation_lookup`. Moving to Telnyx, Bandwidth, or our own SIP trunk changes only the adapter.

## What's real vs simulated

| Real | Simulated / stubbed |
|---|---|
| DNS checks (SPF/DKIM/DMARC/MX) against live DNS | Carrier delivery outcomes, filtering, and call outcomes (`MockCarrier`) |
| Compliance logic, quiet hours, STOP/START handling | Spam-label lookups (production would use Hiya / TNS / First Orion) |
| Health scoring, rest/quarantine policy, line rotation | Warm-up email sending and inbox-placement seed tests |
| Twilio REST adapter (buy, send, call), webhook signature verification, out-of-order status handling | iMessage (see the roadmap: research only) |
| Dialer pacing and abandon-rate control | Agent audio bridging (would be TwiML or FreeSWITCH) |

The Twilio adapter follows the Twilio REST API but has **not** been run against a live account.

### Known limitations (next steps)

- **No authentication** on the API or dashboard. Anyone who can reach the server can send messages. Add login and roles before any real deployment.
- **Jobs run on request**, not on a schedule. The health sweep, spam checks, and warm-up evaluation run when the dashboard or a simulate call triggers them. Some GET endpoints update state (warm-up status, read receipts); in production these move to a scheduler (APScheduler or Celery beat).
- **Daily caps reset at midnight UTC**, not in each line's local time.
- **Area-code time zones** use a partial map. Production needs the full NANPA dataset. Cell numbers also move with their owners, so the property's location is a useful second signal.
- **No migrations yet** (Alembic is next).

## About "stealth / protection"

I built *protection* to mean keeping our own infrastructure healthy and recoverable: rotating, resting, and replacing degraded lines, separate sending domains so one burned domain doesn't take down the others, sticky senders, per-line caps, and webhook signature checks. I deliberately did **not** build tools to evade carrier spam filters or impersonate other senders. That's a fast way to get the whole 10DLC brand or the company's domains banned, and it creates TCPA liability. More in [docs/ROADMAP.md](docs/ROADMAP.md#stealth--protection).

## Project layout

```
app/
  main.py            app factory, seeding, static dashboard
  api.py             REST endpoints + Twilio-format webhooks
  models.py          SQLAlchemy models
  config.py          all thresholds and policies (env-overridable)
  providers/         base protocol, mock carrier, Twilio adapter
  services/          compliance, sms, line_health, provisioning, dialer, email_auth, warmup, simulation
  static/index.html  dashboard
tests/               56 tests: compliance, line health, routing, DNS parsing, warm-up, dialer, webhooks
docs/ROADMAP.md      R&D → production plan
```

API docs: http://localhost:8000/docs
