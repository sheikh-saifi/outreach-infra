"""Cold email capacity + unit-economics planner.

Answers "what does it take to send N cold emails a month with good deliverability, and what does
each way of doing it cost?" Every price is an editable assumption with its source, because
vendor pricing changes often. Prices were checked October 2026.

The model:
  daily volume     = monthly emails / sending days (cold email goes out on weekdays)
  active inboxes   = daily volume / safe cold emails per inbox per day (~25 on aged Google/MS inboxes)
  total inboxes    = active inboxes x (1 + reserve)   reserve = inboxes warming, resting or being replaced
  domains          = total inboxes / inboxes per domain (2-3 keeps one burned domain from hurting many)
  new leads/month  = monthly emails / emails per lead (sequence length); each lead verified once
"""

import math
from dataclasses import asdict, dataclass, field

CHECKED = "2026-10"


@dataclass
class PlanInputs:
    monthly_emails: int = 3_000_000
    sending_days: int = 22            # weekdays per month
    per_inbox_per_day: int = 25       # safe cold sends per aged inbox per day
    inboxes_per_domain: int = 3
    reserve_pct: float = 0.25         # buffer for warming / resting / replacement inboxes
    domain_burn_pct: float = 0.10     # share of domains replaced each month
    emails_per_lead: float = 3.0      # steps in the sequence
    reply_rate: float = 0.02          # replies per lead over the whole sequence
    positive_share: float = 0.25      # share of replies that are interested
    warmup_weeks: int = 3
    google_ms_share: float = 0.6      # hybrid stack: share of inboxes on Google/Microsoft vs Mailforge


# key -> (value, unit, source, note)
DEFAULT_PRICES: dict[str, dict] = {
    "domain_per_year": {"value": 14.0, "unit": "$/domain/yr", "label": ".com sending domain",
                        "source": "https://www.mailforge.ai/pricing"},
    "verify_per_email": {"value": 0.000449, "unit": "$/email", "label": "Email verification (MillionVerifier, 1M credits)",
                         "source": "https://pipeline.zoominfo.com/sales/millionverifier-vs-zerobounce"},
    "google_direct": {"value": 7.0, "unit": "$/inbox/mo", "label": "Google Workspace Business Starter (annual)",
                      "source": "https://www.emailvendorselection.com/google-workspace-pricing/"},
    "reseller_inbox": {"value": 2.50, "unit": "$/inbox/mo", "label": "Google/Microsoft inbox via reseller (InboxKit annual; Zapmail ~$3)",
                       "source": "https://www.inboxkit.com/pricing"},
    "mailforge_inbox": {"value": 2.00, "unit": "$/inbox/mo", "label": "Mailforge shared cold infra ($3 list, ~$2 at volume)",
                        "source": "https://coldemailkit.com/tools/mailforge"},
    "infraforge_inbox": {"value": 2.50, "unit": "$/inbox/mo", "label": "Infraforge inbox, 1,000+ tier",
                         "source": "https://www.aerosend.io/review/infraforge/"},
    "dedicated_ip": {"value": 99.0, "unit": "$/IP/mo", "label": "Infraforge dedicated IP",
                     "source": "https://www.aerosend.io/review/infraforge/"},
    "emails_per_ip_day": {"value": 5000, "unit": "emails/IP/day", "label": "Cold emails per dedicated IP per day (assumption)",
                          "source": None},
    "instantly_workspace": {"value": 286.30, "unit": "$/mo", "label": "Instantly Light Speed, annual (500k emails/mo)",
                            "source": "https://instantly.ai/pricing"},
    "instantly_emails_cap": {"value": 500_000, "unit": "emails/mo", "label": "Instantly Light Speed monthly email cap",
                             "source": "https://instantly.ai/pricing"},
    "smartlead_workspace": {"value": 379.0, "unit": "$/mo", "label": "Smartlead Unlimited Prime (510k emails/mo)",
                            "source": "https://lagrowthmachine.com/smartlead-pricing/"},
    "smartlead_emails_cap": {"value": 510_000, "unit": "emails/mo", "label": "Smartlead Unlimited Prime monthly email cap",
                             "source": "https://lagrowthmachine.com/smartlead-pricing/"},
    "standalone_warmup": {"value": 20.0, "unit": "$/inbox/mo", "label": "Standalone warm-up (MailReach, annual)",
                          "source": "https://hothawk.ai/compare/mailreach-alternatives"},
    "inhouse_hosting": {"value": 400.0, "unit": "$/mo", "label": "Servers, queue, DB, monitoring for an in-house sender (assumption)",
                        "source": None},
    "inhouse_engineering": {"value": 6000.0, "unit": "$/mo", "label": "Engineering to run an in-house sender (~0.5 engineer, assumption)",
                            "source": None},
}


@dataclass
class Capacity:
    daily_emails: int
    active_inboxes: int
    total_inboxes: int
    domains: int
    domains_replaced_per_month: int
    new_leads_per_month: int
    replies_per_month: int
    interested_per_month: int
    weeks_to_full_volume: int
    ramp: list[dict] = field(default_factory=list)


def capacity(i: PlanInputs) -> Capacity:
    daily = math.ceil(i.monthly_emails / i.sending_days)
    active = math.ceil(daily / i.per_inbox_per_day)
    total = math.ceil(active * (1 + i.reserve_pct))
    domains = math.ceil(total / i.inboxes_per_domain)
    leads = math.ceil(i.monthly_emails / i.emails_per_lead)
    replies = round(leads * i.reply_rate)
    # Week-by-week share of target volume: nothing during warm-up, then a 2-week cold ramp (50% -> 100%).
    ramp, w = [], 0
    for w in range(i.warmup_weeks + 4):
        share = 0.0 if w < i.warmup_weeks else min(1.0, 0.5 + 0.25 * (w - i.warmup_weeks))
        ramp.append({"week": w + 1, "share": share, "emails_per_day": round(daily * share)})
    return Capacity(daily, active, total, domains, math.ceil(domains * i.domain_burn_pct), leads, replies,
                    round(replies * i.positive_share), i.warmup_weeks + 3, ramp)


def _p(prices: dict, key: str) -> float:
    return float(prices[key]["value"])


def stacks(i: PlanInputs, cap: Capacity, prices: dict) -> list[dict]:
    """Monthly cost of each way to run this volume, broken into components."""
    p = lambda k: _p(prices, k)  # noqa: E731
    domains = cap.domains * p("domain_per_year") / 12 + cap.domains_replaced_per_month * p("domain_per_year")
    verify = cap.new_leads_per_month * p("verify_per_email")
    instantly_ws = math.ceil(i.monthly_emails / p("instantly_emails_cap"))
    smartlead_ws = math.ceil(i.monthly_emails / p("smartlead_emails_cap"))
    instantly = instantly_ws * p("instantly_workspace")
    smartlead = smartlead_ws * p("smartlead_workspace")
    n = cap.total_inboxes
    ips = math.ceil(cap.daily_emails / p("emails_per_ip_day"))
    gms = round(n * i.google_ms_share)

    def stack(key, name, platform, inboxes, notes, extra=None, warmup_included=True):
        comp = {"platform": platform, "inboxes": inboxes, "domains": domains, "verification": verify, **(extra or {})}
        total = sum(comp.values())
        return {"key": key, "name": name, "components": {k: round(v, 2) for k, v in comp.items()},
                "total": round(total, 2), "per_1k": round(total / (i.monthly_emails / 1000), 2),
                "per_reply": round(total / cap.replies_per_month, 2) if cap.replies_per_month else None,
                "per_interested": round(total / cap.interested_per_month, 2) if cap.interested_per_month else None,
                "warmup_included": warmup_included, "notes": notes}

    out = [
        stack("instantly_google_direct", "Instantly + Google Workspace bought directly", instantly, n * p("google_direct"),
              f"Baseline. Full retail Google seats; Google also suspends accounts that look like bulk cold senders. "
              f"{instantly_ws} x Light Speed as a proxy for an Enterprise quote."),
        stack("instantly_reseller", "Instantly + Google/Microsoft inboxes via reseller", instantly, n * p("reseller_inbox"),
              "Real Google/Microsoft inboxes (best placement in Gmail/Outlook) at a third of retail, DNS set up for you."),
        stack("instantly_mailforge", "Instantly + Mailforge", instantly, n * p("mailforge_inbox"),
              "Cheapest inboxes: shared infrastructure built for cold email. Placement into Gmail/Outlook is usually "
              "a bit below real Google/Microsoft inboxes."),
        stack("hybrid", f"Instantly + hybrid ({round(i.google_ms_share * 100)}% Google/MS, rest Mailforge)", instantly,
              gms * p("reseller_inbox") + (n - gms) * p("mailforge_inbox"),
              "What I'd run: Google/Microsoft inboxes for Gmail/Outlook recipients (ESP matching), cheap Mailforge "
              "inboxes for the rest and as overflow. Two providers also means no single point of failure."),
        stack("smartlead_mailforge", "Smartlead + Mailforge", smartlead, n * p("mailforge_inbox"),
              f"Like Instantly + Mailforge with Smartlead's flat 'unlimited' plans ({smartlead_ws} x Unlimited Prime)."),
        stack("smartlead_infraforge", "Smartlead + Infraforge (dedicated IPs)", smartlead, n * p("infraforge_inbox"),
              f"Private IPs: no shared reputation, but you own IP warm-up and reputation. {ips} IPs at "
              f"{int(p('emails_per_ip_day')):,} emails/IP/day (assumption).",
              {"dedicated_ips": ips * p("dedicated_ip")}),
        stack("inhouse", "Fully in-house sender + Mailforge + paid warm-up", 0.0, n * p("mailforge_inbox"),
              "Build the sending/rotation/warm-up layer yourself. The killer cost is warm-up: Instantly and Smartlead "
              "include unlimited warm-up through a network of millions of inboxes; on your own you pay per inbox.",
              {"warmup": n * p("standalone_warmup"), "hosting": p("inhouse_hosting"),
               "engineering": p("inhouse_engineering")}, warmup_included=False),
    ]
    return sorted(out, key=lambda s: s["total"])


def plan(inputs: dict | None = None, price_overrides: dict | None = None) -> dict:
    i = PlanInputs(**{k: v for k, v in (inputs or {}).items() if k in PlanInputs.__dataclass_fields__})
    for name in ("monthly_emails", "sending_days", "per_inbox_per_day", "inboxes_per_domain", "emails_per_lead"):
        if getattr(i, name) <= 0:
            raise ValueError(f"{name} must be greater than 0")
    for name in ("reserve_pct", "domain_burn_pct", "reply_rate", "positive_share", "google_ms_share"):
        if getattr(i, name) < 0:
            raise ValueError(f"{name} can't be negative")
    i.warmup_weeks = int(i.warmup_weeks)
    prices = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    for k, v in (price_overrides or {}).items():
        if k in prices:
            prices[k]["value"] = float(v)
    cap = capacity(i)
    options = stacks(i, cap, prices)
    by_key = {s["key"]: s for s in options}
    # How much the safe-sends-per-inbox assumption moves the bill (on the recommended stack).
    sensitivity = []
    for per_inbox in (15, 20, 25, 30, 40):
        alt = PlanInputs(**{**asdict(i), "per_inbox_per_day": per_inbox})
        c = capacity(alt)
        s = next(x for x in stacks(alt, c, prices) if x["key"] == "hybrid")
        sensitivity.append({"per_inbox_per_day": per_inbox, "total_inboxes": c.total_inboxes, "domains": c.domains,
                            "monthly": s["total"], "per_1k": s["per_1k"]})
    return {"inputs": asdict(i), "capacity": asdict(cap), "stacks": options, "recommended": "hybrid",
            "build_vs_buy": {"inhouse": by_key["inhouse"]["total"], "hybrid": by_key["hybrid"]["total"],
                             "warmup_alone": by_key["inhouse"]["components"]["warmup"]},
            "sensitivity": sensitivity, "prices": prices, "prices_checked": CHECKED}
