import base64
import hashlib
import hmac
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import (CallLog, Domain, HealthSnapshot, Lead, Mailbox, MailboxDailyStat, Message, PhoneNumber,
                        Suppression, utcnow)
from app.services import compliance, email_auth, line_health, provisioning, simulation, sms, warmup
from app.services.dialer import run_session

api = APIRouter(prefix="/api")
webhooks = APIRouter(prefix="/webhooks")


def _number_or_404(db: Session, number_id: int) -> PhoneNumber:
    if not (n := db.get(PhoneNumber, number_id)):
        raise HTTPException(404, "number not found")
    return n


def _lead_or_404(db: Session, lead_id: int) -> Lead:
    if not (lead := db.get(Lead, lead_id)):
        raise HTTPException(404, "lead not found")
    return lead


# ------------------------------------------------------------------ overview

@api.get("/overview")
def overview(db: Session = Depends(get_db)):
    now = utcnow()
    since = now - timedelta(days=7)
    lines = dict(db.execute(select(PhoneNumber.status, func.count()).group_by(PhoneNumber.status)).all())

    campaign_sms = (Message.channel == "sms", Message.direction == "outbound",
                    Message.is_auto_reply.is_(False), Message.created_at >= since)
    out = db.execute(select(Message.status, func.count()).where(*campaign_sms)
                     .group_by(Message.status)).all()
    out = dict(out)
    attempted = sum(v for k, v in out.items() if k != "blocked")
    inbound = db.scalars(select(Message.body).where(
        Message.channel == "sms", Message.direction == "inbound", Message.created_at >= since)).all()
    kinds = [compliance.classify_keyword(b) for b in inbound]
    optouts = kinds.count("stop")

    day = func.date(Message.created_at)
    series = db.execute(select(
        day.label("day"),
        func.sum(case((Message.status == "delivered", 1), else_=0)),
        func.sum(case((Message.status == "filtered", 1), else_=0)),
        func.sum(case((Message.status == "failed", 1), else_=0)),
        func.sum(case((Message.status == "blocked", 1), else_=0)),
    ).where(*campaign_sms).group_by(day).order_by(day)).all()

    calls = dict(db.execute(select(CallLog.outcome, func.count()).where(CallLog.started_at >= since)
                            .group_by(CallLog.outcome)).all())
    mailboxes = dict(db.execute(select(Mailbox.status, func.count()).group_by(Mailbox.status)).all())
    return {
        "lines": lines,
        "sms_7d": {
            "attempted": attempted, "by_status": out,
            "delivery_rate": round(out.get("delivered", 0) / attempted, 3) if attempted else None,
            "filter_rate": round(out.get("filtered", 0) / attempted, 3) if attempted else None,
            "replies": kinds.count(None), "optouts": optouts, "wrong_numbers": kinds.count("wrong_number"),
        },
        "sms_series": [{"day": str(r[0]), "delivered": r[1], "filtered": r[2], "failed": r[3], "blocked": r[4]}
                       for r in series],
        "calls_7d": calls,
        "mailboxes": mailboxes,
        "suppressed": db.scalar(select(func.count(Suppression.id))),
        "provider": settings.telephony_provider,
    }


# ------------------------------------------------------------------ lines

class ProvisionIn(BaseModel):
    area_code: str = Field(pattern=r"^\d{3}$")
    count: int = Field(1, ge=1, le=50)
    campaign: str | None = None


@api.get("/lines")
def list_lines(db: Session = Depends(get_db)):
    # Problems first: lowest health at the top, retired lines at the bottom.
    rows = db.scalars(select(PhoneNumber).order_by(PhoneNumber.status == "retired", PhoneNumber.health_score)).all()
    sent = line_health.sent_today_by_line(db)
    return [{
        "id": n.id, "e164": n.e164, "area_code": n.area_code, "provider": n.provider, "campaign": n.campaign,
        "status": n.status, "status_reason": n.status_reason, "rested_until": n.rested_until,
        "health_score": n.health_score, "spam_label": n.spam_label, "daily_cap": n.daily_cap,
        "sent_today": sent.get(n.id, 0),
    } for n in rows]


@api.get("/lines/{number_id}/history")
def line_history(number_id: int, db: Session = Depends(get_db)):
    _number_or_404(db, number_id)
    snaps = db.scalars(select(HealthSnapshot).where(HealthSnapshot.number_id == number_id)
                       .order_by(HealthSnapshot.taken_at)).all()
    return [{"at": s.taken_at, "score": s.score, "delivery_rate": s.delivery_rate, "filter_rate": s.filter_rate,
             "optout_rate": s.optout_rate, "reply_rate": s.reply_rate} for s in snaps]


@api.post("/lines/provision")
def provision_lines(body: ProvisionIn, db: Session = Depends(get_db)):
    return [n.e164 for n in provisioning.provision(db, body.area_code, body.count, body.campaign)]


@api.post("/lines/{number_id}/{action}")
def line_action(number_id: int, action: str, db: Session = Depends(get_db)):
    n = _number_or_404(db, number_id)
    if n.status == "retired":
        raise HTTPException(409, "line is retired and released at the carrier; provision a new one")
    if action == "rest":
        if n.status != "active":
            raise HTTPException(409, f"only active lines can be rested (line is {n.status})")
        n.status, n.status_reason = "resting", "manual"
        n.rested_until = utcnow() + timedelta(hours=settings.rest_hours)
    elif action == "reactivate":
        line_health.reactivate(n, utcnow(), "manual")
    elif action == "retire":
        provisioning.retire(db, n)
    elif action == "replace":
        return {"retired": n.e164, "new": provisioning.replace(db, n).e164}
    else:
        raise HTTPException(400, "action must be rest | reactivate | retire | replace")
    db.commit()
    return {"id": n.id, "status": n.status}


@api.post("/monitor/health-sweep")
def health_sweep(db: Session = Depends(get_db)):
    return line_health.run_health_sweep(db)


@api.post("/monitor/spam-check")
def spam_check(db: Session = Depends(get_db)):
    return line_health.run_spam_checks(db)


# ------------------------------------------------------------------ leads & compliance

class LeadIn(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None
    property_address: str | None = None
    timezone: str | None = None


@api.get("/leads")
def list_leads(limit: int = 100, db: Session = Depends(get_db)):
    return [{"id": l.id, "name": l.name, "phone": l.phone, "email": l.email, "property_address": l.property_address}
            for l in db.scalars(select(Lead).limit(limit))]


@api.post("/leads")
def create_lead(body: LeadIn, db: Session = Depends(get_db)):
    data = body.model_dump()
    if data["phone"]:
        try:
            data["phone"] = compliance.normalize_phone(data["phone"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    lead = Lead(**data)
    db.add(lead)
    db.commit()
    return {"id": lead.id}


class SuppressionIn(BaseModel):
    value: str
    channel: str = Field("all", pattern="^(sms|voice|email|all)$")
    reason: str = "manual"


@api.get("/suppressions")
def list_suppressions(db: Session = Depends(get_db)):
    return [{"value": s.value, "channel": s.channel, "reason": s.reason, "at": s.created_at}
            for s in db.scalars(select(Suppression).order_by(Suppression.created_at.desc()))]


@api.post("/suppressions")
def add_suppression(body: SuppressionIn, db: Session = Depends(get_db)):
    value = body.value if "@" in body.value else compliance.normalize_phone(body.value)
    compliance.suppress(db, value, body.channel, body.reason)
    db.commit()
    return {"suppressed": value}


# ------------------------------------------------------------------ sms

class SendIn(BaseModel):
    lead_id: int
    template: str
    campaign: str | None = None


@api.post("/sms/send")
def send_sms(body: SendIn, db: Session = Depends(get_db)):
    lead = _lead_or_404(db, body.lead_id)
    try:
        m = sms.send(db, lead, body.template, body.campaign)
    except sms.NoLineAvailable as e:
        raise HTTPException(503, str(e))
    return {"id": m.id, "status": m.status, "from": m.from_addr, "error_code": m.error_code,
            "block_reason": m.block_reason}


# ------------------------------------------------------------------ email

class DomainCheckIn(BaseModel):
    domain: str
    dkim_selectors: list[str] | None = None


class MailboxIn(BaseModel):
    address: str


@api.get("/email/domains")
def list_domains(db: Session = Depends(get_db)):
    return [{"id": d.id, "name": d.name, "mx": d.has_mx, "spf": d.spf_ok, "dkim": d.dkim_ok, "dmarc": d.dmarc_ok,
             "dmarc_policy": d.dmarc_policy, "last_checked": d.last_checked,
             "issues": d.notes.split("\n") if d.notes else []} for d in db.scalars(select(Domain))]


@api.post("/email/domains/check")
def check_domain(body: DomainCheckIn, db: Session = Depends(get_db)):
    _, result = email_auth.check_and_store(db, body.domain, body.dkim_selectors)
    return result


@api.get("/email/mailboxes")
def list_mailboxes(db: Session = Depends(get_db)):
    today = utcnow().date()
    out = []
    for mb in db.scalars(select(Mailbox)):
        plan = warmup.evaluate(db, mb, today)
        series = db.scalars(select(MailboxDailyStat).where(MailboxDailyStat.mailbox_id == mb.id)
                            .order_by(MailboxDailyStat.day)).all()
        out.append({"id": mb.id, **plan, "domain": mb.domain.name, "warmup_days": settings.warmup_days,
                    "series": [{"day": str(s.day), "sent": s.sent, "planned": s.planned, "bounces": s.bounces,
                                "inbox_placement": s.inbox_placement} for s in series]})
    db.commit()
    return out


@api.post("/email/mailboxes")
def add_mailbox(body: MailboxIn, db: Session = Depends(get_db)):
    domain_name = body.address.split("@")[-1].lower()
    domain = db.scalar(select(Domain).where(Domain.name == domain_name))
    if not domain:
        raise HTTPException(400, f"check the domain {domain_name} first (POST /api/email/domains/check)")
    mb = Mailbox(address=body.address.lower(), domain_id=domain.id, warmup_started=date.today())
    db.add(mb)
    db.commit()
    return {"id": mb.id}


@api.post("/email/mailboxes/{mailbox_id}/resume")
def resume_mailbox(mailbox_id: int, db: Session = Depends(get_db)):
    if not (mb := db.get(Mailbox, mailbox_id)):
        raise HTTPException(404, "mailbox not found")
    warmup.resume(mb, utcnow().date())
    db.commit()
    return {"id": mb.id, "status": mb.status}


# ------------------------------------------------------------------ dialer

class DialIn(BaseModel):
    lead_ids: list[int] | None = None
    count: int = Field(20, ge=1, le=500)
    lines: int = Field(1, ge=1, le=10)


@api.post("/dialer/sessions")
def start_dial_session(body: DialIn, db: Session = Depends(get_db)):
    ids = body.lead_ids or list(db.scalars(select(Lead.id).order_by(func.random()).limit(body.count)))
    return run_session(db, ids, body.lines)


@api.get("/dialer/calls")
def list_calls(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.execute(select(CallLog, Lead.name, PhoneNumber.e164).join(Lead, CallLog.lead_id == Lead.id)
                      .join(PhoneNumber, CallLog.number_id == PhoneNumber.id)
                      .order_by(CallLog.id.desc()).limit(limit)).all()
    return [{"id": c.id, "session": c.session_id[:8], "lead": name, "from": e164, "outcome": c.outcome,
             "duration_s": c.duration_s, "at": c.started_at} for c, name, e164 in rows]


# ------------------------------------------------------------------ unified inbox

@api.get("/inbox")
def inbox(db: Session = Depends(get_db)):
    """One row per lead who has replied on any channel, newest first."""
    last = (select(Message.lead_id, func.max(Message.id).label("last_id"))
            .where(Message.direction == "inbound", Message.lead_id.is_not(None))
            .group_by(Message.lead_id).subquery())
    rows = db.execute(select(Message, Lead).join(last, Message.id == last.c.last_id)
                      .join(Lead, Lead.id == Message.lead_id).order_by(Message.created_at.desc())).all()
    unread = dict(db.execute(select(Message.lead_id, func.count()).where(
        Message.direction == "inbound", Message.is_read.is_(False)).group_by(Message.lead_id)).all())
    return [{"lead_id": lead.id, "name": lead.name, "phone": lead.phone, "address": lead.property_address,
             "channel": m.channel, "last_message": m.body, "at": m.created_at,
             "opted_out": bool(lead.phone and compliance.is_suppressed(db, lead.phone, "sms")),
             "unread": unread.get(lead.id, 0)}
            for m, lead in rows]


@api.get("/inbox/{lead_id}")
def thread(lead_id: int, db: Session = Depends(get_db)):
    lead = _lead_or_404(db, lead_id)
    msgs = db.scalars(select(Message).where(Message.lead_id == lead_id).order_by(Message.created_at)).all()
    for m in msgs:
        if m.direction == "inbound":
            m.is_read = True
    db.commit()
    return {"lead": {"id": lead.id, "name": lead.name, "phone": lead.phone, "address": lead.property_address},
            "messages": [{"direction": m.direction, "channel": m.channel, "body": m.body, "status": m.status,
                          "from": m.from_addr, "at": m.created_at} for m in msgs]}


class ReplyIn(BaseModel):
    body: str


@api.post("/inbox/{lead_id}/reply")
def reply(lead_id: int, body: ReplyIn, db: Session = Depends(get_db)):
    lead = _lead_or_404(db, lead_id)
    try:
        m = sms.send(db, lead, body.body, automated=False)
    except sms.NoLineAvailable as e:
        raise HTTPException(503, str(e))
    return {"id": m.id, "status": m.status, "from": m.from_addr, "block_reason": m.block_reason}


# ------------------------------------------------------------------ simulation (mock provider)

@api.post("/simulate/tick")
def simulate_tick(batch: int = 40, db: Session = Depends(get_db)):
    if settings.telephony_provider != "mock":
        raise HTTPException(400, "simulation is only available with the mock provider")
    return simulation.tick(db, batch)


# ------------------------------------------------------------------ carrier webhooks (Twilio format)

async def _verified_form(request: Request) -> dict:
    form = dict(await request.form())
    if settings.telephony_provider == "twilio":
        # https://www.twilio.com/docs/usage/security#validating-requests
        payload = str(request.url) + "".join(k + form[k] for k in sorted(form))
        expected = base64.b64encode(hmac.new(settings.twilio_auth_token.encode(), payload.encode(),
                                             hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "invalid signature")
    return form


@webhooks.post("/sms/inbound")
async def inbound_sms(request: Request, db: Session = Depends(get_db)):
    form = await _verified_form(request)
    return sms.handle_inbound(db, form["From"], form["To"], form.get("Body", ""))


# Callbacks can arrive out of order (a late "sent" after "delivered"); never move a message backwards.
STATUS_RANK = {"queued": 0, "sending": 1, "sent": 2, "delivered": 3, "failed": 3, "filtered": 3}


@webhooks.post("/sms/status")
async def sms_status(request: Request, db: Session = Depends(get_db)):
    form = await _verified_form(request)
    msg = db.scalar(select(Message).where(Message.provider_sid == form.get("MessageSid")))
    if not msg:
        return {"ok": False}
    status, code = form.get("MessageStatus"), form.get("ErrorCode")
    new = "filtered" if code == "30007" else {"undelivered": "failed"}.get(status, status)
    if STATUS_RANK.get(new, -1) > STATUS_RANK.get(msg.status, -1):
        msg.status = new
        msg.error_code = code or msg.error_code
        db.commit()
    return {"ok": True, "status": msg.status}
