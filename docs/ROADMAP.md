# Roadmap: R&D → Production

This plan takes the prototype in this repo to production outreach infrastructure across SMS, voice, iMessage, and email. Timelines assume one engineer. Each phase ends with something that works in production, not just a document.

## Guiding principles

1. **Compliance is infrastructure, not a checklist.** TCPA damages run $500–$1,500 *per message*. Carriers and mailbox providers ban whole brands and domains, not just individual numbers. Every send passes one gate.
2. **Measure before scaling.** No channel scales until its health is observable: delivery, filtering, opt-outs, spam labels, bounces, and inbox placement.
3. **Assume assets burn.** Numbers and domains degrade. Rotation, resting, and replacement are automated and boring.
4. **Vendor-agnostic core.** Business logic sits behind a provider interface, so we can move from Twilio to cheaper carriers, or our own SIP trunk, as volume grows.

---

## Phase 0: Foundations (week 1–2) ✅ *prototype done*

- Data model: leads, lines, messages, calls, domains, mailboxes, suppressions.
- Provider abstraction plus a mock carrier for development.
- Compliance gate: suppression, recipient-local quiet hours, STOP/START.
- Dashboard skeleton.

**Exit criteria:** you can send through the mock end to end, every send leaves an audit trail, and the tests pass.

## Phase 1: Email deliverability (week 2–5)

Email is the cheapest channel, and setup takes the longest (warm-up), so it starts first.

| Work | Detail |
|---|---|
| Domain strategy | Never cold-email from the primary domain. Use 3–5 lookalike sending domains per brand, each with its own SPF, DKIM, and DMARC (start at `p=none` with `rua`, move to `quarantine`), plus a redirect to the main site. |
| DNS checker ✅ | Live MX, SPF (10-lookup limit), DKIM selectors, and DMARC checks. Add automated nightly re-checks and alerts. |
| Mailbox provisioning | Google Workspace or Microsoft 365 via their admin APIs: 2–3 inboxes per domain, with real names, photos, and signatures. |
| Warm-up ✅ (scheduler) | Ramp 5→40/day over 28 days. Next: connect to a warm-up network (Instantly, Smartlead, Mailreach) or a self-hosted peer pool. |
| Sending engine | SMTP/OAuth sending with per-mailbox daily caps, randomized send intervals within business hours, one-click unsubscribe headers (RFC 8058, required by Gmail and Yahoo since 2024), and a physical address (CAN-SPAM). |
| Monitoring | Google Postmaster Tools API, Microsoft SNDS, blacklist checks (Spamhaus, Barracuda), seed-list inbox-placement tests, and bounce classification (hard vs soft). |
| Inbox management | IMAP/Graph sync of all mailboxes into the unified inbox. Detect replies, out-of-office messages, and bounces. Pause the sequence on reply. |

**Exit:** 20+ warmed mailboxes, sustained >90% inbox placement, <2% bounce rate, and spam complaints under 0.1% (Gmail's hard limit is 0.3%).

## Phase 2: Phone lines and SMS (week 4–9)

| Work | Detail |
|---|---|
| Carrier registration | **A2P 10DLC** brand plus campaign registration (unregistered traffic is blocked). Use toll-free verification as a second path. Expect 1–3 weeks of approval time; start this in week 1. |
| Line provisioning ✅ | Buy by area code, assign to a campaign, retire, replace. Next: attach numbers to Messaging Services, auto-replenish pools nightly, and choose carriers per region. |
| SMS engine ✅ | Sticky sender, local presence, per-line daily caps, templating. Next: a durable queue (Redis + workers), retries, per-carrier throughput limits (10DLC sets messages-per-second limits per trust score). |
| Delivery webhooks ✅ | Status callbacks update each message. Signature verification is in place. |
| Line health ✅ | Score built from filter rate, opt-outs, spam label, and replies, with automatic rest and quarantine. Next: per-carrier breakdowns (T-Mobile and AT&T filter differently) and alerting. |
| Spam monitoring | Daily reputation lookups via Hiya, TNS, or First Orion. Register numbers with Free Caller Registry. Add a content linter that flags URL shorteners, ALL CAPS, and missing opt-out language before a campaign launches. |
| Lead hygiene | Line-type lookup before sending (skip landlines; this saves money and protects the filter rate), National DNC + state DNC + litigator list scrubbing, and reassigned-number database checks. |

**Exit:** a registered 10DLC campaign, >95% delivery, <1% filtering, <2% opt-out, and automatic rotation running unattended for 2 weeks.

## Phase 3: SIP trunking and dialer (week 8–14)

| Work | Detail |
|---|---|
| SIP trunking | Start on Twilio Elastic SIP or Telnyx, with redundant trunks, codec selection (G.711 / Opus), concurrency limits, and failover. Get **STIR/SHAKEN A-attestation** (calls without it get labeled). Move to our own FreeSWITCH or Kamailio once volume justifies it, roughly when per-minute cost exceeds hosting plus ops. |
| Single-line dialer ✅ (logic) | Preview and power modes. Next: a WebRTC softphone in the browser (Twilio Voice SDK or SIP.js), click-to-call from the inbox, and dispositions. |
| Multi-line dialer ✅ (logic) | Parallel dialing with abandon-rate control (FTC 3% per 30 days). Next: answering-machine detection, voicemail drop, a recorded ID message on abandoned calls within 2s, and dial-ratio tuning from live answer rates. |
| Call health | Answer rate per number (the best early signal of a "Spam Likely" label), average call duration, and short-call rate. Feed these into the line health score. |
| Recording and consent | Recording disclosure that varies by state (two-party-consent states such as CA and FL), plus retention policy. |

**Exit:** agents dialing from the browser, abandon rate <3%, answer rates tracked per line, and STIR/SHAKEN A-level on all outbound calls.

## Phase 4: iMessage (R&D, week 10–14, in parallel)

There is **no official API** for sending iMessages at scale. Apple Messages for Business only works for inbound conversations a customer starts. The options:

| Option | Risk |
|---|---|
| Mac farm (Mac minis + AppleScript or private frameworks) | Apple bans Apple IDs that send at volume. Ongoing hardware and ops cost. Against Apple's terms. |
| Third-party "iMessage API" vendors | The same thing run by someone else. Account bans and vendor shutdowns are common. |
| Apple Messages for Business | Official, but inbound only. Can't be used for cold outreach. |

**Recommendation:** time-boxed research only. Measure whether the blue-bubble reply lift is worth account churn, and keep SMS as the mandatory fallback. Any production rollout needs leadership sign-off on the risk.

## Phase 5: Protection and resilience (week 12–16)

<a id="stealth--protection"></a>
"Stealth / protection" in this plan means **resilience of our own infrastructure**:

- **Blast-radius isolation:** separate domains, mailboxes, line pools, and campaigns, so one burned asset doesn't take down the rest. Keep the main brand domain and main numbers out of cold outreach entirely.
- **Automated rotation:** rest, quarantine, and replace lines and domains based on health (lines are done; extend to domains and mailboxes).
- **Rate shaping:** per-asset caps and business-hours pacing, so traffic looks like what it is: a small team sending personally.
- **Security:** webhook signature verification ✅, secrets in a vault, least-privilege API keys, audit logs, and per-campaign kill switches.
- **Litigation defense:** litigator-list scrubbing, consent and opt-out audit trails, and fast opt-out processing.

**Out of scope on purpose:** evading carrier or mailbox filters (spinning content to defeat filters, snowshoe sending, spoofing caller ID, rotating numbers specifically to dodge blocks). These get the whole 10DLC brand suspended and raise TCPA and FCC exposure. Filters are a signal to listen to, not an obstacle to route around.

## Phase 6: Production hardening (ongoing from week 14)

- Postgres with migrations (Alembic), Redis job queue, and separate API and worker processes.
- Scheduled jobs: health sweep (every 15 min), spam check (daily), DNS re-check (daily), warm-up tick (hourly), pool replenishment (nightly).
- Observability: Prometheus metrics, Grafana dashboards, and alerts (Slack or PagerDuty) when delivery drops, filtering spikes, or a domain or IP lands on a blacklist.
- Load tests at 10× target volume, plus incident runbooks for a burned line, burned domain, carrier outage, or STOP-processing failure.
- Auth and roles on the dashboard (admin vs agent).

## Cost model (rough, US, per month)

| Item | Unit cost | 50 lines / 30 mailboxes |
|---|---|---|
| Local numbers | ~$1.15 each + 10DLC campaign ~$15 | ~$75 |
| SMS | ~$0.008 + carrier fee ~$0.003 | 50k msgs ≈ $550 |
| Voice | ~$0.013/min outbound | 20k min ≈ $260 |
| Google Workspace | ~$7/mailbox | ~$210 |
| Domains | ~$12/yr each | ~$10 |
| Reputation / line-type lookups | ~$0.005 per lookup | ~$250 |

## Risks

| Risk | Mitigation |
|---|---|
| 10DLC campaign rejected or suspended | Register honestly, with opt-out in every message. Keep toll-free as a fallback. |
| TCPA lawsuit | Compliance gate, DNC and litigator scrubbing, consent and opt-out audit trail, legal review before launch. |
| Mass "Spam Likely" labeling | Answer-rate monitoring, number registration, STIR/SHAKEN, lower per-line volume. |
| Domain blacklisted | Isolated sending domains, early warning from DMARC reports and Postmaster Tools. |
| iMessage account bans | R&D only, SMS fallback. |
