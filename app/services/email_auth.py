"""Email deliverability: live DNS checks for MX, SPF, DKIM and DMARC.

Since Feb 2024 Google and Yahoo require SPF + DKIM + DMARC for bulk senders; missing any
of these sends cold email straight to spam. This module checks the real DNS records and
explains what to fix.
"""

import dns.exception
import dns.resolver
from sqlalchemy.orm import Session

from app.models import Domain, utcnow

COMMON_DKIM_SELECTORS = ["google", "selector1", "selector2", "default", "k1", "s1", "s2", "mail", "dkim"]


def _txt(name: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    return [b"".join(r.strings).decode(errors="replace") for r in answers]


def _has_mx(name: str) -> bool:
    try:
        return bool(dns.resolver.resolve(name, "MX", lifetime=5))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return False


def parse_spf(records: list[str]) -> dict:
    spf = [r for r in records if r.lower().startswith("v=spf1")]
    if not spf:
        return {"ok": False, "record": None, "issues": ["No SPF record. Add a TXT record starting with v=spf1."]}
    if len(spf) > 1:
        return {"ok": False, "record": spf, "issues": ["Multiple SPF records; receivers treat this as a permerror. Merge them."]}
    rec = spf[0]
    issues = []
    terms = rec.split()
    lookups = sum(1 for t in terms if t.split(":")[0].lstrip("+~-?") in ("include", "a", "mx", "ptr", "exists", "redirect"))
    if lookups > 10:
        issues.append(f"{lookups} DNS lookups; SPF allows at most 10.")
    if terms[-1] in ("+all", "all"):
        issues.append("Ends in +all, which authorises the whole internet to send as you.")
    elif terms[-1] not in ("-all", "~all") and not terms[-1].startswith("redirect="):
        issues.append("No ~all / -all terminator.")
    return {"ok": not issues, "record": rec, "issues": issues}


def parse_dmarc(records: list[str]) -> dict:
    dm = [r for r in records if r.lower().startswith("v=dmarc1")]
    if not dm:
        return {"ok": False, "record": None, "policy": None,
                "issues": ["No DMARC record. Add TXT at _dmarc.<domain>: v=DMARC1; p=none; rua=mailto:dmarc@<domain>"]}
    rec = dm[0]
    tags = {k.strip().lower(): v.strip() for k, _, v in (p.partition("=") for p in rec.split(";")) if k.strip()}
    policy = tags.get("p")
    issues = []
    if policy not in ("none", "quarantine", "reject"):
        issues.append("Missing or invalid p= policy.")
    if "rua" not in tags:
        issues.append("No rua= address; you won't receive aggregate reports to spot spoofing or misconfig.")
    return {"ok": not issues, "record": rec, "policy": policy, "issues": issues}


def check_dkim(domain: str, selectors: list[str] | None = None) -> dict:
    for sel in selectors or COMMON_DKIM_SELECTORS:
        for rec in _txt(f"{sel}._domainkey.{domain}"):
            if "p=" in rec:
                empty_key = "p=;" in rec.replace(" ", "") or rec.replace(" ", "").endswith("p=")
                return {"ok": not empty_key, "selector": sel,
                        "issues": ["DKIM key is revoked (empty p=)."] if empty_key else []}
    return {"ok": False, "selector": None,
            "issues": ["No DKIM key found on common selectors. Enable DKIM in your mail provider and publish the key."]}


def check_domain(name: str, dkim_selectors: list[str] | None = None) -> dict:
    name = name.strip().lower()
    spf = parse_spf(_txt(name))
    dmarc = parse_dmarc(_txt(f"_dmarc.{name}"))
    dkim = check_dkim(name, dkim_selectors)
    mx = _has_mx(name)
    issues = ([] if mx else ["No MX record; replies (and some receivers' checks) will fail."]) \
        + spf["issues"] + dkim["issues"] + dmarc["issues"]
    return {"domain": name, "mx": mx, "spf": spf, "dkim": dkim, "dmarc": dmarc,
            "ready_for_cold_email": mx and spf["ok"] and dkim["ok"] and dmarc["ok"], "issues": issues}


def check_and_store(db: Session, name: str, dkim_selectors: list[str] | None = None) -> tuple[Domain, dict]:
    result = check_domain(name, dkim_selectors)
    d = db.query(Domain).filter_by(name=result["domain"]).one_or_none() or Domain(name=result["domain"])
    d.has_mx, d.spf_ok, d.dkim_ok, d.dmarc_ok = result["mx"], result["spf"]["ok"], result["dkim"]["ok"], result["dmarc"]["ok"]
    d.dmarc_policy = result["dmarc"]["policy"]
    d.last_checked = utcnow()
    d.notes = "\n".join(result["issues"]) or None
    db.add(d)
    db.commit()
    return d, result
