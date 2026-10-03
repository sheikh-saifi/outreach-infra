"""SMS sending + inbound handling.

Sender selection, in priority order:
  1. sticky: the number this lead was first contacted from (keeps one conversation thread)
  2. local presence: an active line in the lead's area code
  3. any active line
Within a tier, prefer the healthiest line with the most remaining daily capacity.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead, Message, PhoneNumber, utcnow
from app.providers import get_carrier
from app.services import compliance
from app.services.line_health import sent_today_by_line


class NoLineAvailable(Exception):
    pass


def render(template: str, lead: Lead) -> str:
    first = (lead.name or "").split(" ")[0] or "there"
    return template.replace("{first_name}", first).replace("{address}", lead.property_address or "your property")


def pick_number(db: Session, lead: Lead, campaign: str | None = None, now: datetime | None = None) -> PhoneNumber:
    sent = sent_today_by_line(db, now)

    def usable(n: PhoneNumber | None) -> bool:
        return bool(n) and n.status == "active" and sent.get(n.id, 0) < n.daily_cap

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
        return (n.area_code == area, n.health_score, n.daily_cap - sent.get(n.id, 0))

    for n in sorted(pool, key=rank, reverse=True):
        if usable(n):
            return n
    raise NoLineAvailable("no active line with remaining daily capacity")


def send(db: Session, lead: Lead, template: str, campaign: str | None = None,
         now: datetime | None = None) -> Message:
    body = render(template, lead)
    ok, reason = compliance.can_contact(db, lead, "sms", now)
    if not ok:
        msg = Message(channel="sms", direction="outbound", from_addr="-", to_addr=lead.phone or "-",
                      body=body, status="blocked", block_reason=reason, lead_id=lead.id,
                      created_at=now or utcnow())
        db.add(msg)
        db.commit()
        return msg

    number = pick_number(db, lead, campaign, now)
    result = get_carrier().send_sms(number.e164, lead.phone, body)
    msg = Message(channel="sms", direction="outbound", from_addr=number.e164, to_addr=lead.phone, body=body,
                  status=result.status, error_code=result.error_code, provider_sid=result.provider_sid,
                  number_id=number.id, lead_id=lead.id, created_at=now or utcnow())
    if lead.sticky_number_id is None:
        lead.sticky_number_id = number.id
    db.add(msg)
    db.commit()
    return msg


def handle_inbound(db: Session, from_raw: str, to_raw: str, body: str, now: datetime | None = None) -> dict:
    """Webhook target for inbound SMS. Handles STOP/START and threads the reply to its lead."""
    sender = compliance.normalize_phone(from_raw)
    to = compliance.normalize_phone(to_raw)
    number = db.scalar(select(PhoneNumber).where(PhoneNumber.e164 == to))
    lead = db.scalar(select(Lead).where(Lead.phone == sender))
    db.add(Message(channel="sms", direction="inbound", from_addr=sender, to_addr=to, body=body,
                   status="received", number_id=number.id if number else None,
                   lead_id=lead.id if lead else None, created_at=now or utcnow()))

    keyword = compliance.classify_keyword(body)
    reply = None
    if keyword == "stop":
        compliance.suppress(db, sender, "sms", "opt_out")
        compliance.suppress(db, sender, "voice", "opt_out")
        reply = "You have been unsubscribed and will not receive further messages."
    elif keyword == "start":
        compliance.unsuppress(db, sender, "sms")
        compliance.unsuppress(db, sender, "voice")
        reply = "You have been re-subscribed. Reply STOP to opt out."
    db.commit()

    # Opt-out confirmations are required and exempt from quiet hours.
    if reply and number:
        get_carrier().send_sms(number.e164, sender, reply)
    return {"keyword": keyword, "lead_id": lead.id if lead else None, "auto_reply": reply}
