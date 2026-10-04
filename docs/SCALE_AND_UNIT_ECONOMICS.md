# Sending ~3M cold emails a month: capacity, setup and unit economics

Every number here comes from the planner in this repo ([app/services/scale_planner.py](../app/services/scale_planner.py), **Scale & cost** tab), so you can change any assumption and see the effect. Prices were checked October 2026; sources are listed at the end.

## 1. What 3M emails a month requires

Cold email should only go out on weekdays, and each inbox can safely send about **25 cold emails a day**. That makes the inbox count the real unit of capacity, not the email count.

| | Value | How |
|---|---|---|
| Emails per sending day | **136,364** | 3,000,000 ÷ 22 weekdays |
| Inboxes actively sending | **5,455** | ÷ 25 cold emails per inbox per day |
| Total inboxes | **6,819** | + 25% reserve warming up or resting, to replace burned ones |
| Domains | **2,273** | ÷ 3 inboxes per domain, so one burned domain takes down only 3 inboxes |
| Domains replaced per month | **~228** | ~10% churn |
| New leads per month | **1,000,000** | 3-step sequences; each lead verified once before the first send |
| Time to full volume | **~6 weeks** | 3 weeks of warm-up, then cold volume ramps from 50% to 100% over 2 weeks |

**The biggest lever is cold sends per inbox per day.** At 15 a day you need 11,364 inboxes; at 40 a day, 4,263. Pushing volume per inbox is exactly how deliverability gets destroyed, so the plan stays at 25. That one number moves cost more than any choice of vendor:

| Sends / inbox / day | Inboxes | Domains | Monthly (recommended setup) | Per 1k emails |
|---|---|---|---|---|
| 15 | 11,364 | 3,788 | $38,029 | $12.68 |
| 20 | 8,524 | 2,842 | $29,077 | $9.69 |
| **25** | **6,819** | **2,273** | **$23,694** | **$7.90** |
| 30 | 5,683 | 1,895 | $20,109 | $6.70 |
| 40 | 4,263 | 1,421 | $15,632 | $5.21 |

## 2. How to keep deliverability high at that volume

1. **Many small senders, not a few big ones.** About 25 cold emails per inbox per day, 2–3 inboxes per domain, and sending domains kept completely separate from the brand domain.
2. **Real authentication.** SPF, DKIM, and DMARC on every domain; Google and Yahoo have required it for bulk senders since 2024. Also the RFC 8058 one-click unsubscribe header and a physical address in every email.
3. **Warm up before sending, and keep a reserve warming.** New inboxes ramp over 3–4 weeks. About 25% of inboxes are always warming or resting, so a burned domain is replaced the same day.
4. **Match the inbox provider to the recipient's.** Gmail recipients get mail from Google Workspace inboxes and Outlook recipients from Microsoft 365. Cheaper shared infrastructure (Mailforge) handles the rest and takes overflow.
5. **Clean lists.** Verify every address before the first email; bounces are the fastest way to burn a domain. Remove role accounts, typo domains, and disposable addresses. Stop the sequence the moment someone replies, bounces, or unsubscribes.
6. **Plain, short first emails.** No links or images, no open tracking in step 1, and real personalization.
7. **Monitor per domain and act automatically.** Track bounce and complaint rates (pause above 3% bounce or 0.1% complaints), blacklists (Spamhaus DBL, SURBL, URIBL), Google Postmaster, and seed-test inbox placement. Pause the domain, not the whole program.

Points 3, 5, 6, and 7 are implemented in this prototype: warm-up gating, verification, content checks, per-mailbox and per-domain monitoring, and automatic pauses ([docs/EMAIL_AND_SMS.md](EMAIL_AND_SMS.md)).

## 3. Would I use Instantly? Yes, for sending. And why.

The monthly cost of each setup at 3M emails/month:

| Setup | Per month | Per 1k emails | Per reply | Per interested lead |
|---|---|---|---|---|
| Instantly + Mailforge | $21,649 | $7.22 | $1.08 | $4.33 |
| Smartlead + Mailforge | $22,205 | $7.40 | $1.11 | $4.44 |
| **Instantly + hybrid (60% Google/MS, 40% Mailforge)** ← recommended | **$23,694** | **$7.90** | **$1.18** | **$4.74** |
| Instantly + Google/MS inboxes via reseller | $25,058 | $8.35 | $1.25 | $5.01 |
| Smartlead + Infraforge (dedicated IPs) | $28,386 | $9.46 | $1.42 | $5.68 |
| Instantly + Google Workspace bought directly | $55,744 | $18.58 | $2.79 | $11.15 |
| Fully in-house sender + Mailforge + paid warm-up | $162,711 | $54.24 | $8.14 | $32.54 |

*Per reply and per interested lead assume a 2% reply rate per lead and 25% of replies interested; change these in the planner.*

**Why not build the sender in-house:**
- **Warm-up is the deal-breaker.** Warming 6,819 inboxes with a standalone tool costs about **$136k a month** (~$20 per inbox). Instantly and Smartlead include unlimited warm-up through a network of millions of real inboxes. A single company can't build that network, because warm-up only works when the mail goes to many independent real inboxes.
- **The platform is a rounding error.** Instantly's fee is about **$1.7k of the ~$24k**. Inboxes and domains are about 90% of the cost, and they're the same whether you build or buy.
- **Building means owning the hard parts:** inbox rotation, per-inbox throttling, bounce and reply parsing across thousands of mailboxes, OAuth/IMAP connections, and keeping up with Google's and Microsoft's changing rules. That's months of engineering, and none of it sets Covent apart.

**Instantly vs Smartlead** is close to a tie on price ($21.6k vs $22.2k at this volume). Instantly's self-serve plans cap at 500k emails/month, so 3M is an **Enterprise quote** (the table uses 6 × Light Speed as a proxy). Smartlead's flat "unlimited" plans are easier to predict at scale. In practice I'd choose on API quality, webhooks, and inbox-rotation features, not on platform price.

**Where the money actually goes, and where to negotiate:**
- **Inboxes (~70%):** buy through resellers or Mailforge at $2–3 instead of $7 retail Google seats. That alone saves about $30k a month.
- **Domains (~25%):** about 2,300 .com domains plus about 230 replacements a month. Keeping domains healthy is the lever: fewer burned domains means fewer replacements.
- **Verification:** ~$450 a month for 1M addresses, and it pays for itself by protecting domains.
- **Platform:** Instantly or Smartlead, ~$1.7–2.3k.

## 4. What I'd build in-house: the brain, not the pipe

Rent the commodity parts (sending, rotation, warm-up) and build the parts specific to Covent's business. This prototype is that layer, already wired to Instantly:

- **Lead data and hygiene:** verification, line-type lookup, dedup, and suppression shared across every channel
- **Compliance across SMS and email:** quiet hours by recipient time zone, frequency caps, opt-out detection, DNC
- **Multi-channel sequencing:** SMS, email, and calls in one sequence; a reply on any channel stops all of them
- **Reply routing and a unified inbox,** plus line and domain health monitoring

**How it connects:**
- `POST /api/integrations/instantly/push` pushes only compliant, verified leads into an Instantly campaign.
- Instantly's webhooks (`reply_received`, `email_bounced`, `lead_unsubscribed`, `auto_reply_received`, …) post to `/webhooks/instantly`. A reply in Instantly then stops the lead's SMS sequence too, and a bounce or unsubscribe updates the shared suppression list.

## Sources (checked October 2026)

- Instantly pricing: https://instantly.ai/pricing
- Instantly API (leads, webhooks): https://developer.instantly.ai
- Smartlead pricing: https://lagrowthmachine.com/smartlead-pricing/
- Mailforge pricing: https://www.mailforge.ai/pricing, https://coldemailkit.com/tools/mailforge
- Infraforge pricing: https://www.aerosend.io/review/infraforge/
- InboxKit / Zapmail / Maildoso pricing: https://www.inboxkit.com/pricing, https://moderninbound.com/blog/zapmail-vs-maildoso
- Google Workspace / Microsoft 365 pricing: https://www.emailvendorselection.com/google-workspace-pricing/
- MailReach warm-up pricing: https://hothawk.ai/compare/mailreach-alternatives
- MillionVerifier pricing: https://pipeline.zoominfo.com/sales/millionverifier-vs-zerobounce
- Cold sends per inbox / inboxes per domain: https://www.warmy.io/blog/how-many-emails-can-i-send-via-gmail-and-outlook/, https://puzzleinbox.com/blog/how-many-inboxes-do-you-need-cold-email/

**Assumptions to validate with real data:** 25 sends per inbox per day; a 25% reserve; 10% domain churn a month; a 2% reply rate; 5,000 emails per dedicated IP per day; in-house engineering at about half an engineer. All of these are editable in the planner.
