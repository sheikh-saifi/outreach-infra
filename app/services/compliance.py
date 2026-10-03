"""Compliance gate. Every outbound SMS / call / email passes through `can_contact` first.

Covers the rules that get outbound teams sued or banned:
- suppression list (opt-outs, DNC, hard bounces, complaints)
- TCPA quiet hours in the *recipient's* local time (8am-9pm)
- STOP / START keyword handling (CTIA guidelines)
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lead, Suppression

STOP_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "remove", "optout"}
START_KEYWORDS = {"start", "unstop", "yes"}

# Partial area-code -> timezone map. Production: use a full NANPA dataset or a lookup API.
AREA_CODE_TZ = {
    **dict.fromkeys(["212", "305", "404", "617", "646", "718", "786", "813", "904", "407"], "America/New_York"),
    **dict.fromkeys(["214", "312", "469", "512", "713", "773", "817", "832", "972", "210"], "America/Chicago"),
    **dict.fromkeys(["303", "480", "602", "720", "801"], "America/Denver"),
    **dict.fromkeys(["206", "213", "310", "415", "503", "619", "702", "818", "909", "916"], "America/Los_Angeles"),
}
# If we can't place the recipient, only contact when it's allowed in *every* continental US zone.
US_ZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]


class ComplianceError(Exception):
    pass


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError(f"Not a valid US phone number: {raw!r}")
    return "+" + digits


def area_code_of(e164: str) -> str:
    return e164[2:5]


def lead_timezones(lead: Lead) -> list[str]:
    if lead.timezone:
        return [lead.timezone]
    if lead.phone and (tz := AREA_CODE_TZ.get(area_code_of(lead.phone))):
        return [tz]
    return US_ZONES


def within_contact_window(lead: Lead, now_utc: datetime | None = None) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    for tz in lead_timezones(lead):
        hour = now_utc.astimezone(ZoneInfo(tz)).hour
        if not (settings.quiet_hours_end <= hour < settings.quiet_hours_start):
            return False
    return True


def is_suppressed(db: Session, value: str, channel: str) -> Suppression | None:
    return db.scalar(
        select(Suppression).where(
            Suppression.value == value.lower(),
            or_(Suppression.channel == channel, Suppression.channel == "all"),
        )
    )


def suppress(db: Session, value: str, channel: str, reason: str) -> None:
    value = value.lower()
    if not db.scalar(select(Suppression).where(Suppression.value == value, Suppression.channel == channel)):
        db.add(Suppression(value=value, channel=channel, reason=reason))


def unsuppress(db: Session, value: str, channel: str, reasons: tuple[str, ...] = ("opt_out",)) -> None:
    for row in db.scalars(select(Suppression).where(
        Suppression.value == value.lower(), Suppression.channel == channel, Suppression.reason.in_(reasons)
    )):
        db.delete(row)


def can_contact(db: Session, lead: Lead, channel: str, now_utc: datetime | None = None) -> tuple[bool, str]:
    target = lead.email if channel == "email" else lead.phone
    if not target:
        return False, f"lead has no {'email' if channel == 'email' else 'phone'}"
    # A text opt-out also covers calls; an email opt-out only covers email.
    channels = ["email"] if channel == "email" else [channel, "sms", "voice"]
    for ch in channels:
        if (s := is_suppressed(db, target, ch)):
            return False, f"suppressed ({s.reason})"
    if channel in ("sms", "voice") and not within_contact_window(lead, now_utc):
        return False, "outside 8am-9pm recipient local time"
    return True, "ok"


def classify_keyword(body: str) -> str | None:
    word = body.strip().lower().rstrip(".!")
    if word in STOP_KEYWORDS:
        return "stop"
    if word in START_KEYWORDS:
        return "start"
    return None
