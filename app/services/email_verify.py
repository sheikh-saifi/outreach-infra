"""Recipient email verification. Bounces are the #1 thing that burns a sending domain, and
skip-traced property-owner lists are full of dead addresses, so check before sending.

valid   -> send
risky   -> send, but it's a role inbox (info@, office@) that rarely reaches a decision maker
invalid -> never send; suppressed so no campaign tries again
"""

import re
from functools import lru_cache

from app.services.email_auth import _has_mx

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
ROLE_ACCOUNTS = {"info", "admin", "support", "sales", "contact", "office", "hello", "help", "billing",
                 "noreply", "no-reply", "postmaster", "webmaster", "abuse", "team", "marketing"}
DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "yopmail.com",
              "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc"}
# Common typos of big providers: worth fixing, not sending to.
TYPOS = {"gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com", "yaho.com": "yahoo.com",
         "hotmial.com": "hotmail.com", "outlok.com": "outlook.com", "aol.co": "aol.com"}


@lru_cache(maxsize=4096)
def _mx_cached(domain: str) -> bool:
    return _has_mx(domain)


def verify(address: str, check_mx: bool = True, has_mx=None) -> tuple[str, str]:
    """Return (status, reason)."""
    address = (address or "").strip()
    if not EMAIL_RE.match(address) or ".." in address:
        return "invalid", "not a valid email address"
    local, domain = address.lower().rsplit("@", 1)
    if domain in TYPOS:
        return "invalid", f"typo domain (did you mean {TYPOS[domain]}?)"
    if domain in DISPOSABLE:
        return "invalid", "disposable email provider"
    if check_mx and not (has_mx or _mx_cached)(domain):
        return "invalid", f"{domain} has no mail server (no MX record)"
    if local in ROLE_ACCOUNTS:
        return "risky", "role account, not a person"
    return "valid", "ok"
