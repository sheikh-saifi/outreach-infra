"""Campaign (sequence) engine for SMS and email.

A campaign is an ordered list of steps ("day 0: text", "day 3: email", "day 7: follow-up email").
Each lead in it has an Enrollment holding which step is next and *when* (`next_run_at`).
The dispatcher (`run_due`, called by the scheduler every 30s) picks up due enrollments and
sends their next step. The important part is what happens when a step can't go out now:

  outcome of the compliance / capacity check      enrollment becomes
  ---------------------------------------------   ------------------------------------------
  person replied on any channel                    replied   (a human takes over in the inbox)
  opted out / DNC / wrong number / hard bounce     stopped
  outside allowed hours, frequency cap reached     deferred to the next allowed time (not dropped)
  no line / mailbox capacity left today            deferred to tomorrow's window
  no phone or email, landline, invalid address     that step is skipped, the sequence continues
  sent                                             next step scheduled after its delay, inside
                                                   the recipient's sending window
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Campaign, CampaignStep, Enrollment, Lead, Message, utcnow
from app.providers import get_carrier
from app.providers.base import LIST_QUALITY_ERRORS
from app.services import compliance, content_lint, email_sender, sms


_jitter_rng = random.Random()


def schedule(lead: Lead, earliest: datetime, channel: str) -> datetime:
    """First allowed time at/after `earliest`, plus random jitter so a batch of leads doesn't all
    fire at 8:00:00. If the jitter lands outside the window, the next window opening is used."""
    t = compliance.next_window(lead, earliest, channel)
    jitter = settings.send_jitter_minutes
    if jitter:
        t = compliance.next_window(lead, t + timedelta(minutes=_jitter_rng.uniform(0, jitter)), channel)
    return t


class CampaignInvalid(Exception):
    def __init__(self, issues: list[dict]):
        super().__init__("campaign has content errors")
        self.issues = issues


# ---------------------------------------------------------------- building campaigns

def lint_step(step: CampaignStep | dict, first_sms: bool) -> dict:
    get = (lambda k: step.get(k)) if isinstance(step, dict) else (lambda k: getattr(step, k))
    if get("channel") == "sms":
        return content_lint.lint_sms(get("body") or "", first_message=first_sms)
    return content_lint.lint_email(get("subject"), get("body") or "")


def lint_steps(steps: list) -> list[dict]:
    """Lint every step. Only the first SMS step must carry opt-out language."""
    out, seen_sms = [], False
    for i, step in enumerate(steps):
        channel = step["channel"] if isinstance(step, dict) else step.channel
        result = lint_step(step, first_sms=channel == "sms" and not seen_sms)
        seen_sms = seen_sms or channel == "sms"
        out.append({"step": i, **result})
    return out


def create(db: Session, name: str, steps: list[dict]) -> Campaign:
    c = Campaign(name=name, status="draft")
    for i, s in enumerate(steps):
        c.steps.append(CampaignStep(position=i, channel=s["channel"], delay_days=int(s.get("delay_days", 0)),
                                    subject=s.get("subject"), body=s["body"]))
    db.add(c)
    db.commit()
    return c


def activate(db: Session, campaign: Campaign) -> None:
    if not campaign.steps:
        raise CampaignInvalid([{"step": None, "issues": [{"level": "error", "message": "campaign has no steps"}]}])
    lint = lint_steps(campaign.steps)
    if any(content_lint.has_errors(r) for r in lint):
        raise CampaignInvalid(lint)
    campaign.status = "active"
    db.commit()


def enroll(db: Session, campaign: Campaign, lead_ids: list[int], now: datetime | None = None) -> int:
    now = now or utcnow()
    existing = set(db.scalars(select(Enrollment.lead_id).where(Enrollment.campaign_id == campaign.id)))
    first = campaign.steps[0]
    added = 0
    for lead in db.scalars(select(Lead).where(Lead.id.in_(lead_ids))):
        if lead.id in existing:
            continue
        db.add(Enrollment(campaign_id=campaign.id, lead_id=lead.id, status="active", current_step=0,
                          next_run_at=schedule(lead, now + timedelta(days=first.delay_days), first.channel)))
        added += 1
    db.commit()
    return added


def stats(db: Session, campaign: Campaign) -> dict:
    rows = db.execute(select(Enrollment.status, Enrollment.current_step)
                      .where(Enrollment.campaign_id == campaign.id)).all()
    by_status: dict[str, int] = {}
    for status, _ in rows:
        by_status[status] = by_status.get(status, 0) + 1
    sent = db.execute(select(Message.channel, Message.status).join(Enrollment, Message.enrollment_id == Enrollment.id)
                      .where(Enrollment.campaign_id == campaign.id, Message.direction == "outbound")).all()
    delivered = sum(1 for ch, st in sent if st in ("delivered", "sent"))
    return {"enrolled": len(rows), **by_status, "messages_sent": delivered,
            "messages_blocked": sum(1 for _, st in sent if st == "blocked"),
            "reply_rate": round(by_status.get("replied", 0) / len(rows), 3) if rows else 0.0}


# ---------------------------------------------------------------- dispatcher

@dataclass
class DispatchStats:
    sent: int = 0
    deferred: int = 0
    skipped: int = 0
    stopped: int = 0
    replied: int = 0
    completed: int = 0
    notes: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        key = reason.split(" (")[0]
        self.notes[key] = self.notes.get(key, 0) + 1


def _advance(e: Enrollment, campaign: Campaign, now: datetime, st: DispatchStats) -> None:
    """Move to the next step and schedule it inside the recipient's window."""
    e.current_step += 1
    if e.current_step >= len(campaign.steps):
        e.status, e.next_run_at = "completed", None
        st.completed += 1
        return
    nxt = campaign.steps[e.current_step]
    e.next_run_at = schedule(e.lead, now + timedelta(days=nxt.delay_days), nxt.channel)


def _defer(e: Enrollment, until: datetime, reason: str, st: DispatchStats) -> None:
    e.next_run_at, e.last_note = until, f"deferred: {reason}"[:128]
    st.deferred += 1
    st.note(reason)


def _sms_step(db: Session, e: Enrollment, step: CampaignStep, now: datetime,
              cache: dict) -> tuple[str, str, datetime | None]:
    lead = e.lead
    if lead.phone and not lead.phone_type:
        lead.phone_type = get_carrier().line_type(lead.phone)  # one lookup per lead, cached
    if lead.phone_type in ("landline", "invalid"):
        return "skip", f"{lead.phone_type} number", None
    d = compliance.check(db, lead, "sms", now)
    if not d.ok:
        return d.action, d.reason, d.retry_at
    if (until := cache.get("sms_busy_until")) and until > now:  # every line is pacing: don't re-ask
        return "defer", "line pacing", until + timedelta(seconds=_jitter_rng.uniform(0, 600))
    try:
        msg = sms.send(db, lead, step.body, now=now)
    except sms.LineBusy as busy:
        cache["sms_busy_until"] = busy.retry_at
        return "defer", "line pacing", busy.retry_at + timedelta(seconds=_jitter_rng.uniform(0, 600))
    except sms.NoLineAvailable:
        return "defer", "no line capacity", schedule(lead, now + timedelta(hours=12), "sms")
    msg.enrollment_id = e.id
    if msg.status == "blocked":
        return "stop", msg.block_reason, None
    if msg.error_code in LIST_QUALITY_ERRORS:
        lead.phone_type = "invalid"  # dead / landline: later SMS steps are skipped instead of retried
    return "sent", msg.status, None


def _email_step(db: Session, e: Enrollment, step: CampaignStep, now: datetime,
                cache: dict) -> tuple[str, str, datetime | None]:
    lead = e.lead
    d = compliance.check(db, lead, "email", now)
    if not d.ok:
        return d.action, d.reason, d.retry_at
    if not compliance.within_email_window(lead, now):
        return "defer", "outside email sending hours", compliance.next_window(lead, now, "email")
    if email_sender.ensure_verified(db, lead) == "invalid":
        return "skip", "invalid email address", None
    # Follow-ups thread under the previous email of this sequence ("Re: ..."), like a person would.
    prev = db.get(Message, e.last_email_message_id) if e.last_email_message_id else None
    if prev is None and (until := cache.get("email_busy_until")) and until > now:
        return "defer", "mailbox pacing", until + timedelta(seconds=_jitter_rng.uniform(0, 2700))
    try:
        msg = email_sender.send(db, lead, step.subject or "", step.body, now=now, reply_to=prev,
                                enrollment_id=e.id, cache=cache)
    except email_sender.MailboxBusy as busy:
        if busy.all_mailboxes:
            cache["email_busy_until"] = busy.retry_at
        # Spread retries over the next ~45 min so waiting leads don't all wake up at once.
        return "defer", "mailbox pacing", busy.retry_at + timedelta(seconds=_jitter_rng.uniform(0, 2700))
    except email_sender.NoMailboxAvailable:
        return "defer", "no mailbox capacity", schedule(lead, now + timedelta(hours=12), "email")
    if msg.status == "blocked":
        return "stop", msg.block_reason, None
    if msg.status == "bounced":
        return "stop", "hard bounce", None
    if msg.status == "failed":
        # Temporary SMTP failure (4xx): retry this step once in an hour, then move on.
        if (e.last_note or "").startswith("retry"):
            return "skip", "send failed twice", None
        e.last_note = "retry: temporary send failure"
        return "retry", msg.error_code or "temporary failure", now + timedelta(hours=1)
    e.last_email_message_id = msg.id  # only delivered emails become the thread for follow-ups
    return "sent", msg.status, None


def run_due(db: Session, now: datetime | None = None, limit: int = 500) -> dict:
    """Send every enrollment step that is due. Safe to call as often as you like."""
    now = now or utcnow()
    st = DispatchStats()
    cache: dict = {}  # per-run lookups (mailbox quotas, usage, pacing) shared across enrollments
    due = db.scalars(
        select(Enrollment).join(Campaign).where(
            Campaign.status == "active", Enrollment.status == "active", Enrollment.next_run_at <= now)
        .order_by(Enrollment.next_run_at).limit(limit)
    ).all()
    for e in due:
        campaign = e.campaign
        if sms.has_replied(db, e.lead):  # safety net; inbound handlers normally close these immediately
            e.status, e.stop_reason, e.next_run_at = "replied", "lead replied", None
            st.replied += 1
            db.commit()
            continue
        step = campaign.steps[e.current_step]
        handler = _sms_step if step.channel == "sms" else _email_step
        outcome, reason, retry_at = handler(db, e, step, now, cache)
        if outcome == "sent":
            st.sent += 1
            e.last_note = f"step {e.current_step + 1} {step.channel} {reason}"
            _advance(e, campaign, now, st)
        elif outcome == "defer":
            _defer(e, retry_at or now + timedelta(hours=1), reason, st)
        elif outcome == "retry":
            e.next_run_at = retry_at
            st.deferred += 1
            st.note("temporary send failure")
        elif outcome == "skip":
            st.skipped += 1
            st.note(reason)
            e.last_note = f"skipped step {e.current_step + 1}: {reason}"
            _advance(e, campaign, now, st)
        else:  # stop
            e.status, e.stop_reason, e.next_run_at = "stopped", reason, None
            st.stopped += 1
            st.note(reason)
        db.commit()
    return {"due": len(due), "sent": st.sent, "deferred": st.deferred, "skipped": st.skipped,
            "stopped": st.stopped, "replied": st.replied, "completed": st.completed, "reasons": st.notes}
