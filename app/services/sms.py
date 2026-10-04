"""SMS sending + inbound handling.

Sender selection, in priority order:
  1. sticky: the number this lead was first contacted from (keeps one conversation thread)
  2. local presence: an active line in the lead's area code
  3. any active line
Within a tier, prefer the healthiest line with the most remaining daily capacity.

Automated sends stop for any lead who has replied: from then on a human owns the conversation
in the inbox. Following up a "who is this?" with a canned template is how you earn complaints.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lead, Message, PhoneNumber, Suppression, utcnow
from app.providers import get_carrier
from app.services import compliance, enrollment_events
from app.services.line_health import calls_today_by_line, sent_today_by_line


class NoLineAvailable(Exception):
    pass


class LineBusy(Exception):
    """Lines have capacity left today but all sent too recently. Try again at `retry_at`."""
    def __init__(self, retry_at: datetime):
        super().__init__(f"all lines pacing until {retry_at}")
        self.retry_at = retry_at


def last_sent_by_line(db: Session, now: datetime) -> dict[int, datetime]:
    rows = db.execute(select(Message.number_id, func.max(Message.created_at)).where(
        Message.direction == "outbound", Message.channel == "sms", Message.status != "blocked",
        Message.is_auto_reply.is_(False), Message.created_at > now - timedelta(hours=1),
        Message.created_at <= now, Message.number_id.is_not(None)).group_by(Message.number_id)).all()
    return {k: v for k, v in rows}


def render(template: str, lead: Lead) -> str:
    first = (lead.name or "").split(" ")[0] or "there"
    return template.replace("{first_name}", first).replace("{address}", lead.property_address or "your property")


def pick_number(db: Session, lead: Lead, campaign: str | None = None, now: datetime | None = None,
                channel: str = "sms", pace: bool = False) -> PhoneNumber:
    """`pace=True` (automated texts) also skips lines that sent within `sms_min_gap_seconds`."""
    if channel == "voice":
        sent = calls_today_by_line(db, now)
        cap = lambda n: n.daily_call_cap  # noqa: E731
    else:
        sent = sent_today_by_line(db, now)
        cap = lambda n: n.daily_cap  # noqa: E731

    gap = timedelta(seconds=settings.sms_min_gap_seconds)
    last = last_sent_by_line(db, now or utcnow()) if pace and gap else {}
    busy_until: list[datetime] = []

    def usable(n: PhoneNumber | None) -> bool:
        if not (n and n.status == "active" and sent.get(n.id, 0) < cap(n)):
            return False
        if n.id in last and last[n.id] + gap > (now or utcnow()):
            busy_until.append(last[n.id] + gap)
            return False
        return True

    if lead.sticky_number_id:
        sticky = db.get(PhoneNumber, lead.sticky_number_id)
        if usable(sticky):
            return sticky

    q = select(PhoneNumber).where(PhoneNumber.status == "active")
    if campaign:
        q = q.where(PhoneNumber.campaign == campaign)
    pool = db.scalars(q).all()
    area = compliance.area_code_of(lead.phone) if lead.phone else None

    def rank(n: PhoneNumber):
        return (n.area_code == area, n.health_score, cap(n) - sent.get(n.id, 0))

    for n in sorted(pool, key=rank, reverse=True):
        if usable(n):
            return n
    if busy_until:
        raise LineBusy(min(busy_until))
    raise NoLineAvailable("no active line with remaining daily capacity")


def has_replied(db: Session, lead: Lead) -> bool:
    """Has this person answered on any channel? Checked by phone and email, not lead id: an owner who
    replied about one property is already in a conversation. Bounces and out-of-office replies
    don't count."""
    addrs = [a for a in (lead.phone, (lead.email or "").lower()) if a]
    if not addrs:
        return False
    return db.scalar(select(Message.id).where(
        Message.direction == "inbound", Message.from_addr.in_(addrs),
        or_(Message.kind.is_(None), Message.kind == "reply"),
    ).limit(1)) is not None


def send(db: Session, lead: Lead, template: str, campaign: str | None = None,
         now: datetime | None = None, automated: bool = True) -> Message:
    """`automated=False` is a human replying from the inbox: no reply-stop and no frequency cap,
    but suppression and quiet hours still apply."""
    body = render(template, lead)
    ok, reason = compliance.can_contact(db, lead, "sms", now, enforce_frequency=automated)
    if ok and automated and has_replied(db, lead):
        ok, reason = False, "lead replied; continue in inbox"
    if not ok:
        msg = Message(channel="sms", direction="outbound", from_addr="-", to_addr=lead.phone or "-",
                      body=body, status="blocked", block_reason=reason, lead_id=lead.id,
                      created_at=now or utcnow())
        db.add(msg)
        db.commit()
        return msg

    number = pick_number(db, lead, campaign, now, pace=automated)
    result = get_carrier().send_sms(number.e164, lead.phone, body)
    msg = Message(channel="sms", direction="outbound", from_addr=number.e164, to_addr=lead.phone, body=body,
                  status=result.status, error_code=result.error_code, provider_sid=result.provider_sid,
                  number_id=number.id, lead_id=lead.id, created_at=now or utcnow())
    sticky = db.get(PhoneNumber, lead.sticky_number_id) if lead.sticky_number_id else None
    if sticky is None or sticky.status == "retired":
        lead.sticky_number_id = number.id
    db.add(msg)
    db.commit()
    return msg


def _thread_lead(db: Session, sender: str, number: PhoneNumber | None) -> Lead | None:
    """Which lead is this reply about? One owner often has several properties (several leads, one
    phone), so use the lead we last texted from this line, falling back to any lead with the phone."""
    q = select(Message.lead_id).where(Message.direction == "outbound", Message.to_addr == sender,
                                      Message.lead_id.is_not(None), Message.status != "blocked")
    if number:
        q = q.where(Message.number_id == number.id)
    lead_id = db.scalar(q.order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
    if lead_id:
        return db.get(Lead, lead_id)
    return db.scalar(select(Lead).where(Lead.phone == sender).limit(1))


def handle_inbound(db: Session, from_raw: str, to_raw: str, body: str, now: datetime | None = None) -> dict:
    """Webhook target for inbound SMS: threads the reply and handles opt-out / opt-in / wrong number."""
    now = now or utcnow()
    sender = compliance.normalize_phone(from_raw)
    to = compliance.normalize_phone(to_raw)
    number = db.scalar(select(PhoneNumber).where(PhoneNumber.e164 == to))
    lead = _thread_lead(db, sender, number)
    db.add(Message(channel="sms", direction="inbound", from_addr=sender, to_addr=to, body=body,
                   status="received", number_id=number.id if number else None,
                   lead_id=lead.id if lead else None, created_at=now))

    keyword = compliance.classify_keyword(body)
    # Any inbound text ends this person's automated sequences right away.
    enrollment_events.on_inbound(db, keyword if keyword in ("stop", "wrong_number") else "reply",
                                 phone=sender, email=lead.email if lead else None)
    reply = None
    if keyword in ("stop", "wrong_number"):
        already = compliance.is_suppressed(db, sender, "sms")
        reason = "opt_out" if keyword == "stop" else "wrong_number"
        compliance.suppress(db, sender, "sms", reason)
        compliance.suppress(db, sender, "voice", reason)
        if not already:  # confirm once; don't answer every repeated STOP
            reply = ("You have been unsubscribed and will not receive further messages." if keyword == "stop"
                     else "Sorry for the mix-up. We've removed this number and won't contact you again.")
    elif keyword == "start":
        # Only an opt-out can be undone by the recipient; DNC and wrong-number entries stay.
        was_opted_out = any(s.reason == "opt_out" for s in db.scalars(
            select(Suppression).where(Suppression.value == sender, Suppression.channel.in_(["sms", "voice"]))))
        if was_opted_out:
            compliance.unsuppress(db, sender, "sms")
            compliance.unsuppress(db, sender, "voice")
            reply = "You have been re-subscribed. Reply STOP to opt out."
    db.commit()

    # Opt-out confirmations are required and exempt from quiet hours. Logged for the audit trail.
    if reply and number:
        res = get_carrier().send_sms(number.e164, sender, reply)
        db.add(Message(channel="sms", direction="outbound", from_addr=number.e164, to_addr=sender, body=reply,
                       status=res.status, error_code=res.error_code, provider_sid=res.provider_sid,
                       number_id=number.id, lead_id=lead.id if lead else None, is_auto_reply=True,
                       created_at=now))
        db.commit()
    return {"keyword": keyword, "lead_id": lead.id if lead else None, "auto_reply": reply}
