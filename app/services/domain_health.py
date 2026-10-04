"""Sending-domain health. A domain is the unit mailbox providers judge reputation on, so one
bad domain must stop sending without taking the others down.

A domain is paused (all its mailboxes stop cold email) when any of these is true:
  - it is listed on a domain blacklist (Spamhaus DBL, SURBL, URIBL)
  - SPF, DKIM or DMARC is missing (unauthenticated cold email goes straight to spam)
  - 7-day bounce rate across all its mailboxes > 5%, or complaints over 0.1%
It resumes automatically once the cause clears.
"""

import json
from datetime import timedelta

import dns.exception
import dns.resolver
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Domain, Mailbox, MailboxDailyStat, utcnow
from app.services import email_auth

# zone -> function(answer IP) -> True listed / None "query refused" (e.g. via a public resolver)
DNSBLS = {
    # Spamhaus answers 127.255.255.x when it refuses the query (public DNS / over quota).
    "dbl.spamhaus.org": lambda ip: None if ip.startswith("127.255.255.") else ip.startswith("127.0.1."),
    "multi.surbl.org": lambda ip: None if ip == "127.0.0.1" else ip.startswith("127.0.0."),
    "multi.uribl.com": lambda ip: None if ip == "127.0.0.1" else ip.startswith("127.0.0."),
}


def _a_records(name: str) -> list[str] | None:
    """[] = not listed (NXDOMAIN), None = lookup failed."""
    try:
        return [r.to_text() for r in dns.resolver.resolve(name, "A", lifetime=5)]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        return None


def check_blacklists(domain: str, resolve=_a_records) -> dict[str, bool | None]:
    out = {}
    for zone, interpret in DNSBLS.items():
        ips = resolve(f"{domain}.{zone}")
        if ips is None:
            out[zone] = None
        elif not ips:
            out[zone] = False
        else:
            verdicts = [interpret(ip) for ip in ips]
            out[zone] = True if any(v is True for v in verdicts) else (None if all(v is None for v in verdicts) else False)
    return out


def domain_rates(db: Session, domain: Domain, days: int = 7) -> dict:
    today = utcnow().date()
    rows = db.scalars(select(MailboxDailyStat).join(Mailbox, Mailbox.id == MailboxDailyStat.mailbox_id).where(
        Mailbox.domain_id == domain.id, MailboxDailyStat.day > today - timedelta(days=days))).all()
    sent = sum(r.sent + (r.cold_sent or 0) for r in rows)
    bounces = sum(r.bounces for r in rows)
    complaints = sum(r.complaints for r in rows)
    return {"sent": sent, "bounces": bounces, "complaints": complaints,
            "bounce_rate": bounces / sent if sent else 0.0, "complaint_rate": complaints / sent if sent else 0.0}


def evaluate(db: Session, domain: Domain) -> dict:
    reasons = []
    listed = [z for z, v in json.loads(domain.blacklists or "{}").items() if v is True]
    if listed:
        reasons.append(f"blacklisted on {', '.join(listed)}")
    missing = [n for n, ok in (("SPF", domain.spf_ok), ("DKIM", domain.dkim_ok), ("DMARC", domain.dmarc_ok)) if not ok]
    if missing:
        reasons.append(f"{'/'.join(missing)} not set up")
    r = domain_rates(db, domain)
    if r["sent"] >= 100 and r["bounce_rate"] > settings.domain_max_bounce_rate:
        reasons.append(f"bounce rate {r['bounce_rate']:.1%} (7d, all mailboxes)")
    if r["sent"] >= 100 and r["complaints"] >= 2 and r["complaint_rate"] > settings.max_complaint_rate:
        reasons.append(f"complaint rate {r['complaint_rate']:.2%} (7d)")
    domain.status = "paused" if reasons else "active"
    domain.status_reason = "; ".join(reasons) or None
    return {"domain": domain.name, "status": domain.status, "reason": domain.status_reason, **r}


def refresh_blacklists(db: Session, domain: Domain) -> dict:
    result = check_blacklists(domain.name)
    domain.blacklists = json.dumps(result)
    domain.blacklist_checked = utcnow()
    return result


def run_domain_checks(db: Session, live_dns: bool | None = None) -> list[dict]:
    """Daily job. With live DNS, re-checks SPF/DKIM/DMARC and blacklists before evaluating.
    The demo's seeded domains are fictional, so mock mode only re-evaluates sending stats."""
    live = settings.email_provider != "mock" if live_dns is None else live_dns
    out = []
    for d in db.scalars(select(Domain)):
        if live:
            email_auth.check_and_store(db, d.name)
            refresh_blacklists(db, d)
        out.append(evaluate(db, d))
    db.commit()
    return out
