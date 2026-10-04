"""Cold email engine: mailbox rotation, compliant message construction, and inbound processing.

Outbound, for every email:
  1. compliance gate (suppression list) + "has this person already replied?"
  2. recipient verification (syntax / disposable / typo / MX), cached on the lead
  3. pick a mailbox: sticky first (same thread), else the mailbox with the most cold capacity left,
     spreading load across domains. Capacity comes from warm-up age and mailbox/domain health.
  4. build the message: real From name, Message-ID, threading headers on follow-ups,
     RFC 8058 one-click List-Unsubscribe (required by Gmail/Yahoo for bulk senders since 2024),
     and a CAN-SPAM footer with a physical address and unsubscribe link
  5. send through the provider, record it, count it against the mailbox's daily stats

Inbound, every email that arrives at one of our mailboxes is classified as:
  bounce     -> hard (5.x.x): suppress the address forever; soft (4.x.x): just log
  complaint  -> ARF feedback-loop report: suppress, counts against the mailbox
  auto_reply -> out-of-office etc.: logged, does NOT stop the sequence
  reply      -> a human answered: threads to the lead, stops automation, opt-out wording suppresses
"""

import base64
import hashlib
import hmac
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Domain, Lead, Mailbox, MailboxDailyStat, Message, utcnow
from app.providers import get_email_provider
from app.services import compliance, email_verify, enrollment_events, warmup
from app.services.sms import has_replied, render


class NoMailboxAvailable(Exception):
    pass


class MailboxBusy(Exception):
    """Mailboxes have capacity left but sent too recently. Try again at `retry_at`.
    `all_mailboxes` is False when only the thread's own mailbox is busy."""
    def __init__(self, retry_at: datetime, all_mailboxes: bool = True):
        super().__init__(f"mailbox pacing until {retry_at}")
        self.retry_at = retry_at
        self.all_mailboxes = all_mailboxes


# ---------------------------------------------------------------- unsubscribe tokens

def unsubscribe_token(email: str) -> str:
    """Signed, so nobody can unsubscribe someone else by guessing URLs."""
    email = email.lower()
    payload = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    sig = hmac.new(settings.secret_key.encode(), email.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"


def read_unsubscribe_token(token: str) -> str | None:
    try:
        payload, sig = token.split(".", 1)
        email = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(settings.secret_key.encode(), email.encode(), hashlib.sha256).hexdigest()[:24]
    return email if hmac.compare_digest(sig, expected) else None


def unsubscribe_url(email: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/u/{unsubscribe_token(email)}"


# ---------------------------------------------------------------- capacity

def _stat(db: Session, mailbox_id: int, day: date) -> MailboxDailyStat:
    stat = db.scalar(select(MailboxDailyStat).where(MailboxDailyStat.mailbox_id == mailbox_id,
                                                    MailboxDailyStat.day == day))
    if not stat:
        stat = MailboxDailyStat(mailbox_id=mailbox_id, day=day, planned=0, sent=0, cold_sent=0,
                                bounces=0, complaints=0, replies=0)
        db.add(stat)
        db.flush()
    return stat


def cold_quota(db: Session, mailbox: Mailbox, today: date) -> int:
    """Campaign emails this mailbox may send today. Warm-up comes first: nothing before day
    `cold_start_day`, then half the warm-up volume, then the full cap once warm."""
    plan = warmup.evaluate(db, mailbox, today)  # also pauses the mailbox if bounces/complaints spiked
    if mailbox.status == "paused" or mailbox.domain.status == "paused":
        return 0
    if plan["day"] < settings.cold_start_day:
        return 0
    if mailbox.status == "warming":
        return min(mailbox.daily_cold_cap, warmup.planned_volume(plan["day"]) // 2)
    return mailbox.daily_cold_cap


def cold_sent_today(db: Session, today: date) -> dict[int, int]:
    rows = db.execute(select(MailboxDailyStat.mailbox_id, MailboxDailyStat.cold_sent)
                      .where(MailboxDailyStat.day == today)).all()
    return dict(rows)


def last_cold_send(db: Session, now: datetime) -> dict[int, datetime]:
    rows = db.execute(select(Message.mailbox_id, func.max(Message.created_at)).where(
        Message.channel == "email", Message.direction == "outbound", Message.enrollment_id.is_not(None),
        Message.status != "blocked", Message.created_at > now - timedelta(hours=2), Message.created_at <= now,
    ).group_by(Message.mailbox_id)).all()
    return dict(rows)


def pick_mailbox(db: Session, lead: Lead, now: datetime, pace: bool = True,
                 thread_mailbox: Mailbox | None = None, cache: dict | None = None) -> Mailbox:
    """Choose the sending mailbox. Every automated email, follow-ups included, must pass the same
    gates: mailbox/domain not paused, under today's cold quota, and paced (`email_min_gap_minutes`).

    A follow-up must come from the mailbox its thread lives in; if that mailbox is busy or at its
    cap the follow-up waits. Only if the thread's mailbox is paused does it move to another mailbox
    (as a new thread). `cache` lets one dispatcher run reuse quota/usage lookups."""
    today = now.date()
    c = cache if cache is not None else {}
    if "used" not in c:
        c["used"] = cold_sent_today(db, today)
    gap = timedelta(minutes=settings.email_min_gap_minutes)
    if "last" not in c:
        c["last"] = last_cold_send(db, now) if pace and gap else {}
    quotas = c.setdefault("quota", {})
    busy_until: list[datetime] = []

    def remaining(mb: Mailbox) -> int:
        if mb.id not in quotas:
            quotas[mb.id] = cold_quota(db, mb, today)
        return quotas[mb.id] - c["used"].get(mb.id, 0)

    def ready(mb: Mailbox) -> bool:
        t = c["last"].get(mb.id)
        if pace and t and t + gap > now:
            busy_until.append(t + gap)
            return False
        return True

    if thread_mailbox is not None and remaining(thread_mailbox) + c["used"].get(thread_mailbox.id, 0) > 0:
        # Thread mailbox is healthy (quota > 0 today): wait for it rather than break the thread.
        if remaining(thread_mailbox) <= 0:
            raise NoMailboxAvailable("thread mailbox reached today's cap")
        if not ready(thread_mailbox):
            raise MailboxBusy(min(busy_until), all_mailboxes=False)
        return thread_mailbox

    if lead.sticky_mailbox_id:
        sticky = db.get(Mailbox, lead.sticky_mailbox_id)
        if sticky and remaining(sticky) > 0 and ready(sticky):
            return sticky

    if "mailboxes" not in c:
        c["mailboxes"] = db.scalars(select(Mailbox)).all()
    mailboxes = c["mailboxes"]
    per_domain: dict[int, int] = {}
    for mb in mailboxes:
        per_domain[mb.domain_id] = per_domain.get(mb.domain_id, 0) + c["used"].get(mb.id, 0)
    candidates = [(remaining(mb), mb) for mb in mailboxes]
    with_capacity = [(r, mb) for r, mb in candidates if r > 0]
    candidates = [(r, mb) for r, mb in with_capacity if ready(mb)]
    if not candidates:
        if with_capacity and busy_until:
            raise MailboxBusy(min(busy_until))
        raise NoMailboxAvailable("no mailbox has cold-email capacity left today")
    # Least-used domain first, then most remaining capacity: spreads risk across domains.
    candidates.sort(key=lambda x: (per_domain[x[1].domain_id], -x[0]))
    return candidates[0][1]


# ---------------------------------------------------------------- outbound

def build_message(mailbox: Mailbox, lead: Lead, subject: str, body: str,
                  in_reply_to: str | None = None, now: datetime | None = None) -> EmailMessage:
    msg = EmailMessage()
    domain = mailbox.address.split("@")[1]
    msg["From"] = formataddr((mailbox.display_name, mailbox.address))
    msg["To"] = formataddr((lead.name, lead.email))
    msg["Subject"] = subject
    sent_at = now or utcnow()
    msg["Date"] = format_datetime(sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=timezone.utc), usegmt=True)
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@{domain}>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    unsub = unsubscribe_url(lead.email)
    msg["List-Unsubscribe"] = f"<{unsub}>, <mailto:{mailbox.address}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    footer = (f"\n\n--\n{mailbox.display_name}\n{settings.company_name}\n{settings.company_address}\n"
              f"Not interested? Unsubscribe: {unsub}")
    msg.set_content(body + footer)
    return msg


def _blocked(db: Session, lead: Lead, subject: str, body: str, reason: str, now: datetime,
             enrollment_id: int | None) -> Message:
    m = Message(channel="email", direction="outbound", from_addr="-", to_addr=lead.email or "-", subject=subject,
                body=body, status="blocked", block_reason=reason, lead_id=lead.id, enrollment_id=enrollment_id,
                created_at=now)
    db.add(m)
    db.commit()
    return m


def ensure_verified(db: Session, lead: Lead) -> str:
    """Verify once, cache the result on the lead. Invalid addresses go on the suppression list."""
    if lead.email and not lead.email_status:
        status, reason = email_verify.verify(lead.email, check_mx=settings.email_provider != "mock")
        lead.email_status = status
        if status == "invalid":
            compliance.suppress(db, lead.email, "email", "invalid_email")
        db.commit()
    return lead.email_status or "invalid"


def send(db: Session, lead: Lead, subject_t: str, body_t: str, now: datetime | None = None,
         automated: bool = True, reply_to: Message | None = None, enrollment_id: int | None = None,
         cache: dict | None = None) -> Message:
    """Send one email. `reply_to` threads it under an earlier message (follow-ups, inbox replies)."""
    now = now or utcnow()
    subject = render(subject_t or "", lead)
    body = render(body_t, lead)

    decision = compliance.check(db, lead, "email", now)
    if not decision.ok:
        return _blocked(db, lead, subject, body, decision.reason, now, enrollment_id)
    if automated and has_replied(db, lead):
        return _blocked(db, lead, subject, body, "lead replied; continue in inbox", now, enrollment_id)
    if ensure_verified(db, lead) == "invalid":
        return _blocked(db, lead, subject, body, "invalid email address", now, enrollment_id)

    thread_mb = db.get(Mailbox, reply_to.mailbox_id) if reply_to is not None and reply_to.mailbox_id else None
    if thread_mb is not None and not automated:
        mailbox = thread_mb  # a person answering a conversation: same mailbox, no quota or pacing
    else:
        mailbox = pick_mailbox(db, lead, now, pace=automated, thread_mailbox=thread_mb, cache=cache)
    if thread_mb is not None and mailbox.id != thread_mb.id:
        reply_to = None  # thread's mailbox was paused: start a fresh thread from a healthy one
    if reply_to is not None:
        base = re.sub(r"^(re:\s*)+", "", reply_to.subject or subject, flags=re.I)
        subject = f"Re: {base}"
    email = build_message(mailbox, lead, subject, body, reply_to.message_id_hdr if reply_to else None, now)
    result = get_email_provider().send(email)

    msg = Message(channel="email", direction="outbound", from_addr=mailbox.address, to_addr=lead.email.lower(),
                  subject=subject, body=body, status=result.status, error_code=_smtp_code(result.error),
                  message_id_hdr=email["Message-ID"], in_reply_to=email["In-Reply-To"], mailbox_id=mailbox.id,
                  lead_id=lead.id, enrollment_id=enrollment_id, created_at=now)
    db.add(msg)
    stat = _stat(db, mailbox.id, now.date())
    if automated:
        stat.cold_sent += 1
        if cache is not None:
            cache.setdefault("used", {})[mailbox.id] = cache["used"].get(mailbox.id, 0) + 1
            if enrollment_id is not None:
                cache.setdefault("last", {})[mailbox.id] = now
    if result.status == "bounced":
        stat.bounces += 1
        _hard_bounce(db, lead.email)
    if lead.sticky_mailbox_id is None:
        lead.sticky_mailbox_id = mailbox.id
    db.commit()
    return msg


def _smtp_code(error: str | None) -> str | None:
    """'550 5.1.1 user unknown' -> '550 5.1.1'."""
    if not error:
        return None
    m = re.match(r"\s*(\d{3})(?:[ -]+([245]\.\d{1,3}\.\d{1,3}))?", error)
    return " ".join(p for p in m.groups() if p) if m else error[:16]


def _hard_bounce(db: Session, address: str) -> None:
    compliance.suppress(db, address, "email", "bounce")
    for lead in db.scalars(select(Lead).where(func.lower(Lead.email) == address.lower())):
        lead.email_status = "invalid"


# ---------------------------------------------------------------- inbound

AUTO_SUBJECT = re.compile(r"^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|ooo\b|away from|"
                          r"vacation|autosvar|abwesenheit|auto:)", re.I)
BOUNCE_SUBJECT = re.compile(r"(undeliver|delivery status notification|mail delivery (failed|subsystem)|"
                            r"returned mail|failure notice|delivery has failed)", re.I)
STATUS_CODE = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
FINAL_RECIPIENT = re.compile(r"(?:final|original)-recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)", re.I)
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def classify_inbound(from_addr: str, subject: str, body: str, headers: dict[str, str]) -> str:
    h = {k.lower(): (v or "").lower() for k, v in headers.items()}
    sender = from_addr.lower()
    if "feedback-type" in h or "multipart/report; report-type=feedback-report" in h.get("content-type", ""):
        return "complaint"
    if (sender.startswith(("mailer-daemon@", "postmaster@")) or "report-type=delivery-status" in h.get("content-type", "")
            or BOUNCE_SUBJECT.search(subject or "")):
        return "bounce"
    if (h.get("auto-submitted", "no") != "no" or "x-autoreply" in h or "x-autorespond" in h
            or h.get("precedence") in ("auto_reply", "bulk", "junk") or AUTO_SUBJECT.search(subject or "")):
        return "auto_reply"
    return "reply"


def _bounce_details(body: str, headers: dict[str, str]) -> tuple[str | None, str]:
    """(failed recipient, 'hard' | 'soft')."""
    h = {k.lower(): v for k, v in headers.items()}
    recipient = h.get("x-failed-recipients")
    if not recipient and (m := FINAL_RECIPIENT.search(body or "")):
        recipient = m.group(1)
    code = STATUS_CODE.search(body or "")
    severity = "soft" if code and code.group(1) == "4" else "hard"
    return (recipient.strip().lower() if recipient else None), severity


def handle_inbound(db: Session, from_addr: str, to_addr: str, subject: str, body: str,
                   headers: dict[str, str] | None = None, now: datetime | None = None) -> dict:
    """Entry point for every email arriving at one of our mailboxes (IMAP poller / provider webhook)."""
    now = now or utcnow()
    headers = headers or {}
    from_addr, to_addr = from_addr.strip().lower(), to_addr.strip().lower()
    if m := re.search(r"<([^>]+)>", from_addr):
        from_addr = m.group(1)
    mailbox = db.scalar(select(Mailbox).where(Mailbox.address == to_addr))
    kind = classify_inbound(from_addr, subject, body, headers)
    stat = _stat(db, mailbox.id, now.date()) if mailbox else None
    result: dict = {"kind": kind, "lead_id": None, "action": None}

    lead = None
    in_reply_to = {k.lower(): v for k, v in headers.items()}.get("in-reply-to")
    if kind == "bounce":
        recipient, severity = _bounce_details(body, headers)
        result.update(recipient=recipient, severity=severity)
        if recipient:
            lead = db.scalar(select(Lead).where(func.lower(Lead.email) == recipient))
            if severity == "hard":
                _hard_bounce(db, recipient)
                enrollment_events.on_inbound(db, "bounce", email=recipient)
                if stat:
                    stat.bounces += 1
                result["action"] = "suppressed"
    elif kind == "complaint":
        reported = next((e for e in EMAIL_IN_TEXT.findall(body or "") if e.lower() != to_addr), None)
        if reported:
            compliance.suppress(db, reported, "email", "complaint")
            enrollment_events.on_inbound(db, "complaint", email=reported)
            lead = db.scalar(select(Lead).where(func.lower(Lead.email) == reported.lower()))
        if stat:
            stat.complaints += 1
        result["action"] = "suppressed"
    else:
        # Thread by the Message-ID we sent, falling back to the sender's address.
        if in_reply_to:
            orig = db.scalar(select(Message).where(Message.message_id_hdr == in_reply_to.strip()))
            lead = db.get(Lead, orig.lead_id) if orig and orig.lead_id else None
        lead = lead or db.scalar(select(Lead).where(func.lower(Lead.email) == from_addr))
        if kind == "reply":
            if stat:
                stat.replies += 1
            first_lines = "\n".join((body or "").strip().splitlines()[:3])
            opted_out = compliance.classify_keyword(first_lines) in ("stop", "wrong_number") or \
                bool(re.search(r"\bunsubscribe\b", subject or "", re.I))
            if opted_out:
                compliance.suppress(db, from_addr, "email", "opt_out")
                result["action"] = "suppressed"
            enrollment_events.on_inbound(db, "stop" if opted_out else "reply", email=from_addr,
                                         phone=lead.phone if lead else None)

    db.add(Message(channel="email", direction="inbound", from_addr=from_addr, to_addr=to_addr, subject=subject,
                   body=body or "", status="received", kind=kind, in_reply_to=in_reply_to,
                   message_id_hdr={k.lower(): v for k, v in headers.items()}.get("message-id"),
                   mailbox_id=mailbox.id if mailbox else None, lead_id=lead.id if lead else None, created_at=now))
    db.commit()
    result["lead_id"] = lead.id if lead else None
    return result
