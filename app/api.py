import base64
import hashlib
import hmac
import json
from datetime import date, timedelta
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import (CallLog, Campaign, Domain, Enrollment, HealthSnapshot, Lead, Mailbox, MailboxDailyStat,
                        Message, PhoneNumber, Suppression, utcnow)
from app.services import (campaigns, compliance, domain_health, email_auth, email_sender, email_verify,
                          line_health, provisioning, scale_planner, simulation, sms, warmup)
from app.services.dialer import run_session

api = APIRouter(prefix="/api")
webhooks = APIRouter(prefix="/webhooks")
public = APIRouter()  # unauthenticated pages recipients reach from emails (unsubscribe)


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

    email_out = dict(db.execute(select(Message.status, func.count()).where(
        Message.channel == "email", Message.direction == "outbound", Message.created_at >= since)
        .group_by(Message.status)).all())
    email_in = dict(db.execute(select(Message.kind, func.count()).where(
        Message.channel == "email", Message.direction == "inbound", Message.created_at >= since)
        .group_by(Message.kind)).all())
    email_attempted = email_out.get("sent", 0) + email_out.get("bounced", 0)
    bounces = email_out.get("bounced", 0) + email_in.get("bounce", 0)
    email_series = db.execute(select(
        day.label("day"),
        func.sum(case((Message.status == "sent", 1), else_=0)),
        func.sum(case((Message.status == "bounced", 1), else_=0)),
    ).where(Message.channel == "email", Message.direction == "outbound", Message.created_at >= since)
        .group_by(day).order_by(day)).all()
    enrollments = dict(db.execute(select(Enrollment.status, func.count()).group_by(Enrollment.status)).all())
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
        "email_7d": {
            "sent": email_attempted, "by_status": email_out, "inbound": email_in,
            "bounce_rate": round(bounces / email_attempted, 3) if email_attempted else None,
            "reply_rate": round(email_in.get("reply", 0) / email_attempted, 3) if email_attempted else None,
            "replies": email_in.get("reply", 0), "complaints": email_in.get("complaint", 0),
        },
        "email_series": [{"day": str(r[0]), "sent": r[1], "bounced": r[2]} for r in email_series],
        "enrollments": enrollments,
        "suppressed": db.scalar(select(func.count(Suppression.id))),
        "provider": settings.telephony_provider,
        "email_provider": settings.email_provider,
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
    raw = body.value.strip()
    if "@" in raw:
        if not email_verify.EMAIL_RE.match(raw):
            raise HTTPException(422, f"'{raw}' is not a valid email address")
        value = raw.lower()
    else:
        try:
            value = compliance.normalize_phone(raw)
        except ValueError:
            raise HTTPException(422, "Enter a US phone number (e.g. 214-555-1234) or an email address")
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
    out = []
    for d in db.scalars(select(Domain)):
        out.append({"id": d.id, "name": d.name, "mx": d.has_mx, "spf": d.spf_ok, "dkim": d.dkim_ok, "dmarc": d.dmarc_ok,
                    "dmarc_policy": d.dmarc_policy, "last_checked": d.last_checked,
                    "issues": d.notes.split("\n") if d.notes else [],
                    "status": d.status, "status_reason": d.status_reason,
                    "blacklists": json.loads(d.blacklists) if d.blacklists else None,
                    "blacklist_checked": d.blacklist_checked, **domain_health.domain_rates(db, d)})
    return out


@api.post("/email/domains/check")
def check_domain(body: DomainCheckIn, db: Session = Depends(get_db)):
    domain, result = email_auth.check_and_store(db, body.domain, body.dkim_selectors)
    domain_health.evaluate(db, domain)
    db.commit()
    return result


@api.post("/email/domains/{domain_id}/blacklists")
def check_blacklists(domain_id: int, db: Session = Depends(get_db)):
    if not (d := db.get(Domain, domain_id)):
        raise HTTPException(404, "domain not found")
    listed = domain_health.refresh_blacklists(db, d)
    verdict = domain_health.evaluate(db, d)
    db.commit()
    return {"blacklists": listed, **verdict}


class VerifyIn(BaseModel):
    address: str


@api.post("/email/verify")
def verify_email(body: VerifyIn):
    status, reason = email_verify.verify(body.address, check_mx=True)
    return {"address": body.address, "status": status, "reason": reason}


@api.get("/email/mailboxes")
def list_mailboxes(db: Session = Depends(get_db)):
    today = utcnow().date()
    out = []
    for mb in db.scalars(select(Mailbox)):
        plan = warmup.evaluate(db, mb, today)
        series = db.scalars(select(MailboxDailyStat).where(MailboxDailyStat.mailbox_id == mb.id)
                            .order_by(MailboxDailyStat.day)).all()
        out.append({"id": mb.id, **plan, "domain": mb.domain.name, "domain_status": mb.domain.status,
                    "warmup_days": settings.warmup_days, "display_name": mb.display_name,
                    "cold_quota": email_sender.cold_quota(db, mb, today),
                    "cold_sent_today": email_sender.cold_sent_today(db, today).get(mb.id, 0),
                    "series": [{"day": str(s.day), "sent": s.sent, "cold_sent": s.cold_sent or 0, "planned": s.planned,
                                "bounces": s.bounces, "inbox_placement": s.inbox_placement} for s in series]})
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
    human = (Message.direction == "inbound", Message.lead_id.is_not(None),
             (Message.kind.is_(None)) | (Message.kind == "reply"))  # no bounces / out-of-office in the inbox
    last = (select(Message.lead_id, func.max(Message.id).label("last_id"))
            .where(*human).group_by(Message.lead_id).subquery())
    rows = db.execute(select(Message, Lead).join(last, Message.id == last.c.last_id)
                      .join(Lead, Lead.id == Message.lead_id).order_by(Message.created_at.desc())).all()
    unread = dict(db.execute(select(Message.lead_id, func.count()).where(
        *human, Message.is_read.is_(False)).group_by(Message.lead_id)).all())

    def opted_out(lead: Lead, channel: str) -> bool:
        target = lead.email if channel == "email" else lead.phone
        return bool(target and compliance.is_suppressed(db, target, channel))

    return [{"lead_id": lead.id, "name": lead.name, "phone": lead.phone, "email": lead.email,
             "address": lead.property_address, "channel": m.channel, "subject": m.subject,
             "last_message": m.body, "at": m.created_at, "opted_out": opted_out(lead, m.channel),
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
    last_in = next((m for m in reversed(msgs) if m.direction == "inbound" and m.kind in (None, "reply")), None)
    return {"lead": {"id": lead.id, "name": lead.name, "phone": lead.phone, "email": lead.email,
                     "address": lead.property_address},
            "reply_channel": last_in.channel if last_in else "sms",
            "messages": [{"direction": m.direction, "channel": m.channel, "subject": m.subject, "body": m.body,
                          "status": m.status, "kind": m.kind, "from": m.from_addr, "at": m.created_at,
                          "block_reason": m.block_reason} for m in msgs]}


class ReplyIn(BaseModel):
    body: str
    channel: str = Field("sms", pattern="^(sms|email)$")


@api.post("/inbox/{lead_id}/reply")
def reply(lead_id: int, body: ReplyIn, db: Session = Depends(get_db)):
    """A human reply. Email replies thread under the lead's last email and go out from the same mailbox."""
    lead = _lead_or_404(db, lead_id)
    try:
        if body.channel == "email":
            last = db.scalar(select(Message).where(Message.lead_id == lead.id, Message.channel == "email",
                                                   Message.status.in_(["sent", "received"]))
                             .order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
            m = email_sender.send(db, lead, last.subject if last else "Following up", body.body,
                                  automated=False, reply_to=last)
        else:
            m = sms.send(db, lead, body.body, automated=False)
    except (sms.NoLineAvailable, email_sender.NoMailboxAvailable) as e:
        raise HTTPException(503, str(e))
    return {"id": m.id, "status": m.status, "from": m.from_addr, "block_reason": m.block_reason}


# ------------------------------------------------------------------ campaigns

class StepIn(BaseModel):
    channel: str = Field(pattern="^(sms|email)$")
    delay_days: int = Field(0, ge=0, le=60)
    subject: str | None = None
    body: str


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    steps: list[StepIn] = Field(min_length=1, max_length=10)


class EnrollIn(BaseModel):
    lead_ids: list[int] | None = None
    count: int = Field(50, ge=1, le=5000)


def _campaign_or_404(db: Session, campaign_id: int) -> Campaign:
    if not (c := db.get(Campaign, campaign_id)):
        raise HTTPException(404, "campaign not found")
    return c


def _campaign_out(db: Session, c: Campaign) -> dict:
    lint = campaigns.lint_steps(c.steps)
    return {"id": c.id, "name": c.name, "status": c.status, "created_at": c.created_at,
            "steps": [{"position": s.position, "channel": s.channel, "delay_days": s.delay_days, "subject": s.subject,
                       "body": s.body, "lint": lint[i]} for i, s in enumerate(c.steps)],
            "stats": campaigns.stats(db, c)}


@api.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    return [_campaign_out(db, c) for c in db.scalars(select(Campaign).order_by(Campaign.id))]


@api.post("/campaigns/lint")
def lint_campaign(body: CampaignIn):
    """Live preview while writing a campaign: errors block activation, warnings don't."""
    return campaigns.lint_steps([s.model_dump() for s in body.steps])


@api.post("/campaigns")
def create_campaign(body: CampaignIn, db: Session = Depends(get_db)):
    c = campaigns.create(db, body.name, [s.model_dump() for s in body.steps])
    return _campaign_out(db, c)


@api.post("/campaigns/{campaign_id}/activate")
def activate_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = _campaign_or_404(db, campaign_id)
    try:
        campaigns.activate(db, c)
    except campaigns.CampaignInvalid as e:
        raise HTTPException(422, {"message": "fix the content errors first", "lint": e.issues})
    return {"id": c.id, "status": c.status}


@api.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = _campaign_or_404(db, campaign_id)
    c.status = "paused"
    db.commit()
    return {"id": c.id, "status": c.status}


@api.post("/campaigns/{campaign_id}/enroll")
def enroll_leads(campaign_id: int, body: EnrollIn, db: Session = Depends(get_db)):
    """Enroll specific leads, or `count` leads not yet in this campaign that have the needed contact info."""
    c = _campaign_or_404(db, campaign_id)
    ids = body.lead_ids
    if not ids:
        first = c.steps[0].channel if c.steps else "sms"
        has_contact = Lead.email.is_not(None) if first == "email" else Lead.phone.is_not(None)
        enrolled = select(Enrollment.lead_id).where(Enrollment.campaign_id == c.id)
        ids = list(db.scalars(select(Lead.id).where(has_contact, Lead.id.not_in(enrolled))
                              .order_by(Lead.id).limit(body.count)))
    return {"enrolled": campaigns.enroll(db, c, ids)}


@api.get("/campaigns/{campaign_id}/enrollments")
def list_enrollments(campaign_id: int, status: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(Enrollment).where(Enrollment.campaign_id == campaign_id)
    if status:
        q = q.where(Enrollment.status == status)
    rows = db.scalars(q.order_by(Enrollment.next_run_at.is_(None), Enrollment.next_run_at).limit(limit)).all()
    return [{"id": e.id, "lead": e.lead.name, "lead_id": e.lead_id, "status": e.status, "step": min(e.current_step + 1, len(e.campaign.steps)),
             "next_run_at": e.next_run_at, "note": e.last_note, "stop_reason": e.stop_reason} for e in rows]


@api.post("/dispatch/run")
def dispatch_now(db: Session = Depends(get_db)):
    return campaigns.run_due(db)


# ------------------------------------------------------------------ background jobs

@api.get("/scheduler")
def scheduler_status():
    from app.scheduler import scheduler
    return {"running": scheduler.is_alive(), "jobs": scheduler.status()}


@api.post("/scheduler/{job_name}/run")
def run_job(job_name: str):
    from app.scheduler import scheduler
    job = next((j for j in scheduler.jobs if j.name == job_name), None)
    if not job:
        raise HTTPException(404, "unknown job")
    scheduler.run_job(job)
    return {"name": job.name, "result": job.last_result, "error": job.last_error}


# ------------------------------------------------------------------ simulation (mock provider)

@api.post("/simulate/tick")
def simulate_tick(new_leads: int = 20, db: Session = Depends(get_db)):
    if settings.telephony_provider != "mock" or settings.email_provider != "mock":
        raise HTTPException(400, "simulation is only available with the mock providers")
    return simulation.tick(db, new_leads)


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
    try:
        return sms.handle_inbound(db, form["From"], form["To"], form.get("Body", ""))
    except (KeyError, ValueError) as e:  # malformed carrier payload: reject it, don't crash
        raise HTTPException(400, f"invalid inbound message: {e}")


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


# ------------------------------------------------------------------ inbound email (IMAP poller / provider webhook)

class InboundEmailIn(BaseModel):
    from_addr: str = Field(alias="from")
    to: str
    subject: str = ""
    text: str = ""
    headers: dict[str, str] = {}


@webhooks.post("/email/inbound")
def inbound_email(body: InboundEmailIn, request: Request, db: Session = Depends(get_db)):
    """Normalized inbound email. In production an IMAP/Gmail-API poller per mailbox (or a provider's
    inbound-parse webhook) posts here; X-Webhook-Secret keeps strangers from injecting fake replies."""
    if settings.webhook_secret and not hmac.compare_digest(
            request.headers.get("X-Webhook-Secret", ""), settings.webhook_secret):
        raise HTTPException(403, "invalid webhook secret")
    return email_sender.handle_inbound(db, body.from_addr, body.to, body.subject, body.text, body.headers)


# ------------------------------------------------------------------ unsubscribe (public)

def _unsub_page(title: str, text: str, form: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><body style="font:16px/1.5 -apple-system,Segoe UI,sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;color:#222">
<h1 style="font-size:22px">{escape(title)}</h1><p>{escape(text)}</p>{form}</body>""")


@public.get("/u/{token}", include_in_schema=False)
def unsubscribe_page(token: str):
    """GET only shows a confirm button. Corporate link scanners open every URL in an email, so
    unsubscribing on GET would silently unsubscribe people who never clicked."""
    email = email_sender.read_unsubscribe_token(token)
    if not email:
        raise HTTPException(404, "invalid link")
    return _unsub_page("Unsubscribe", f"Stop all emails from {settings.company_name} to {email}?",
                       f'<form method="post"><button style="font:inherit;padding:10px 18px;border-radius:8px;'
                       f'border:0;background:#2f6fed;color:#fff;cursor:pointer">Unsubscribe</button></form>')


@public.post("/u/{token}", include_in_schema=False)
def unsubscribe(token: str, db: Session = Depends(get_db)):
    """Handles both the confirm button and RFC 8058 one-click POSTs sent by Gmail / Yahoo."""
    email = email_sender.read_unsubscribe_token(token)
    if not email:
        raise HTTPException(404, "invalid link")
    compliance.suppress(db, email, "email", "unsubscribe")
    from app.services import enrollment_events
    enrollment_events.on_inbound(db, "stop", email=email)
    db.commit()
    return _unsub_page("You're unsubscribed", f"{email} won't receive any more emails from {settings.company_name}.")


# ------------------------------------------------------------------ scale & unit economics

class PlanIn(BaseModel):
    inputs: dict[str, float] = {}
    prices: dict[str, float] = {}


@api.get("/planner")
def planner_defaults():
    return scale_planner.plan()


@api.post("/planner")
def planner(body: PlanIn):
    try:
        return scale_planner.plan(body.inputs, body.prices)
    except (ValueError, ZeroDivisionError, TypeError) as e:
        raise HTTPException(422, f"invalid planner input: {e}")


# ------------------------------------------------------------------ Instantly integration

class InstantlyPushIn(BaseModel):
    campaign_id: int                   # our campaign whose active leads should be handed to Instantly
    instantly_campaign_id: str
    limit: int = Field(500, ge=1, le=10_000)


@api.post("/integrations/instantly/push")
def instantly_push(body: InstantlyPushIn, db: Session = Depends(get_db)):
    from app.providers.instantly import InstantlyClient, push_leads
    if not settings.instantly_api_key:
        raise HTTPException(400, "set INSTANTLY_API_KEY to use the Instantly integration")
    leads = list(db.scalars(select(Lead).join(Enrollment, Enrollment.lead_id == Lead.id).where(
        Enrollment.campaign_id == body.campaign_id, Enrollment.status == "active").limit(body.limit)))
    res = push_leads(db, InstantlyClient(settings.instantly_api_key), body.instantly_campaign_id, leads)
    return {"pushed": res.pushed, "skipped": res.skipped}


@webhooks.post("/instantly")
async def instantly_webhook(request: Request, db: Session = Depends(get_db)):
    """Instantly webhook target. Auth with X-Webhook-Secret header or ?secret= (set WEBHOOK_SECRET)."""
    from app.providers.instantly import handle_webhook
    if settings.webhook_secret:
        given = request.headers.get("X-Webhook-Secret") or request.query_params.get("secret", "")
        if not hmac.compare_digest(given, settings.webhook_secret):
            raise HTTPException(403, "invalid webhook secret")
    return handle_webhook(db, await request.json())
