"""Compliance gate. Every outbound SMS / call / email passes through `can_contact` first.

Covers the rules that get outbound teams sued or banned:
- suppression list (opt-outs, DNC, hard bounces, complaints)
- TCPA quiet hours in the *recipient's* local time (8am-9pm)
- contact frequency cap per phone number (FL / OK / MD mini-TCPA laws: 3 per 24h)
- opt-out detection, incl. FCC 2025 "revocation by any reasonable means" (not just exact STOP)
- wrong-number replies (reassigned numbers are a leading source of TCPA suits)
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CallLog, Lead, Message, Suppression, utcnow

# Single-word replies that are opt-outs on their own (CTIA standard keywords).
STOP_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "remove", "optout", "revoke"}
# Deliberately NOT "yes": it's the most common answer to "would you consider an offer?"
START_KEYWORDS = {"start", "unstop"}
# Since April 2025 the FCC treats any reasonable expression of opt-out as revocation, so
# "please stop texting me" must count even though it isn't the bare keyword.
OPT_OUT_PHRASES = re.compile(
    r"\b(stop|unsubscribe|opt[\s-]?out|remove me|take me off|revoke"
    r"|do ?n[o']?t (text|call|contact|message)|leave me alone|lose my number)\b"
)
WRONG_NUMBER = re.compile(r"\b(wrong (number|person|#)|not (the|a) (owner|right person)|don'?t own)\b")

# Partial area-code -> timezone map. Production: use a full NANPA dataset or a lookup API.
AREA_CODE_TZ = {
    **dict.fromkeys(["212", "305", "404", "617", "646", "718", "786", "813", "904", "407"], "America/New_York"),
    **dict.fromkeys(["214", "312", "469", "512", "713", "773", "817", "832", "972", "210"], "America/Chicago"),
    **dict.fromkeys(["303", "720", "801"], "America/Denver"),
    **dict.fromkeys(["480", "602"], "America/Phoenix"),  # Arizona has no DST: an hour off Denver in summer
    "907": "America/Anchorage", "808": "Pacific/Honolulu",
    **dict.fromkeys(["206", "213", "310", "415", "503", "619", "702", "818", "909", "916"], "America/Los_Angeles"),
}
# If we can't place the recipient, only contact when it's allowed in *every* US zone.
US_ZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
            "America/Anchorage", "Pacific/Honolulu"]


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


def can_contact(db: Session, lead: Lead, channel: str, now_utc: datetime | None = None,
                enforce_frequency: bool = True) -> tuple[bool, str]:
    """`enforce_frequency=False` is for a human replying inside a conversation the lead continued."""
    target = lead.email if channel == "email" else lead.phone
    if not target:
        return False, f"lead has no {'email' if channel == 'email' else 'phone'}"
    # A text opt-out also covers calls; an email opt-out only covers email.
    channels = ["email"] if channel == "email" else [channel, "sms", "voice"]
    for ch in channels:
        if (s := is_suppressed(db, target, ch)):
            return False, f"suppressed ({s.reason})"
    if channel in ("sms", "voice"):
        if not within_contact_window(lead, now_utc):
            return False, "outside 8am-9pm recipient local time"
        touches = touches_last_24h(db, lead.phone, now_utc) if enforce_frequency else 0
        if touches >= settings.max_touches_per_24h:
            return False, f"frequency cap ({touches} touches in 24h)"
    return True, "ok"


def touches_last_24h(db: Session, phone: str, now_utc: datetime | None = None) -> int:
    """Outbound texts + calls to this phone number in the last 24h, across every lead that shares it
    (one owner with several properties is common, and the caps apply per person, not per property)."""
    now = (now_utc.astimezone(timezone.utc).replace(tzinfo=None) if now_utc and now_utc.tzinfo
           else now_utc or utcnow())
    since = now - timedelta(hours=24)
    texts = db.scalar(select(func.count(Message.id)).where(
        Message.to_addr == phone, Message.direction == "outbound", Message.status != "blocked",
        Message.is_auto_reply.is_(False), Message.created_at > since, Message.created_at <= now)) or 0
    calls = db.scalar(select(func.count(CallLog.id)).join(Lead, CallLog.lead_id == Lead.id).where(
        Lead.phone == phone, CallLog.started_at > since, CallLog.started_at <= now)) or 0
    return texts + calls


def classify_keyword(body: str) -> str | None:
    """Return 'stop' | 'start' | 'wrong_number' | None for an inbound message."""
    text = body.strip().lower()
    word = text.rstrip(".!")
    if word in STOP_KEYWORDS:
        return "stop"
    if word in START_KEYWORDS:
        return "start"
    if WRONG_NUMBER.search(text):
        return "wrong_number"
    if OPT_OUT_PHRASES.search(text):
        return "stop"
    return None
