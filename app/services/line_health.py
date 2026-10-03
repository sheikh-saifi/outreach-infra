"""Phone line health + spam monitoring.

Health score (0-100) is computed from the line's recent outbound traffic:
  - carrier filter rate (error 30007): the strongest signal that carriers distrust the line
  - opt-out rate: recipients reporting / replying STOP
  - spam label from reputation lookups ("Spam Likely" on caller ID)
  - reply rate: positive engagement earns a small bonus
List-quality failures (landlines, dead numbers) are excluded: they say nothing about the line.

Policy: score < rest threshold -> rest the line for 48h; score < quarantine threshold -> pull it.
When a rest ends, the line gets a fresh reputation lookup (still "Spam Likely" -> rest again)
and its metrics window restarts, so it's judged on new traffic instead of the traffic that
got it rested.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CallLog, HealthSnapshot, Message, PhoneNumber, SpamCheck, utcnow
from app.providers import get_carrier
from app.providers.base import LIST_QUALITY_ERRORS
from app.services.compliance import classify_keyword

MIN_SAMPLE = 20
SPAM_LABEL_PENALTY = 35.0  # alone it drops a perfect line to 65: below the rest threshold


@dataclass
class LineMetrics:
    sample: int
    delivery_rate: float
    filter_rate: float
    optout_rate: float
    reply_rate: float


def compute_metrics(db: Session, number: PhoneNumber) -> LineMetrics:
    q = (select(Message)
         .where(Message.number_id == number.id, Message.direction == "outbound", Message.channel == "sms",
                Message.is_auto_reply.is_(False), Message.status.in_(["sent", "delivered", "failed", "filtered"]))
         .order_by(Message.created_at.desc())
         .limit(settings.health_window_messages))
    if number.metrics_since:
        q = q.where(Message.created_at >= number.metrics_since)
    outbound = db.scalars(q).all()
    relevant = [m for m in outbound if m.error_code not in LIST_QUALITY_ERRORS]
    n = len(relevant)
    if n == 0:
        return LineMetrics(0, 1.0, 0.0, 0.0, 0.0)

    delivered = sum(1 for m in relevant if m.status in ("delivered", "sent"))
    filtered = sum(1 for m in relevant if m.status == "filtered")

    since = min(m.created_at for m in relevant)
    inbound = db.scalars(
        select(Message).where(Message.number_id == number.id, Message.direction == "inbound",
                              Message.created_at >= since)
    ).all()
    kinds = [classify_keyword(m.body) for m in inbound]
    optouts = kinds.count("stop")
    # Wrong-number replies are a list-quality problem, not engagement or a complaint about the line.
    replies = sum(1 for k in kinds if k is None)

    base = max(delivered, 1)
    return LineMetrics(n, delivered / n, filtered / n, optouts / base, replies / base)


def score(metrics: LineMetrics, spam_label: str) -> float:
    s = 100.0
    if metrics.sample >= MIN_SAMPLE:
        s -= min(60.0, metrics.filter_rate * 300)   # 10% filtered -> -30
        s -= min(30.0, metrics.optout_rate * 500)   # 3% opt-out  -> -15
        s += min(10.0, metrics.reply_rate * 100)    # 5% replies  -> +5
    if spam_label == "spam_likely":
        s -= SPAM_LABEL_PENALTY
    return round(max(0.0, min(100.0, s)), 1)


def evaluate(db: Session, number: PhoneNumber, now: datetime | None = None) -> dict:
    """Recompute health for one line and apply the rest / quarantine policy."""
    now = now or utcnow()
    m = compute_metrics(db, number)
    number.health_score = score(m, number.spam_label)
    db.add(HealthSnapshot(number_id=number.id, score=number.health_score, taken_at=now, **{
        k: v for k, v in asdict(m).items() if k != "sample"
    }))

    action = "none"
    if number.status == "resting" and number.rested_until and number.rested_until <= now:
        number.spam_label = _lookup(db, number)
        if number.spam_label == "spam_likely":
            number.rested_until = now + timedelta(hours=settings.rest_hours)
            number.status_reason = "still labelled Spam Likely; rest extended"
            action = "rest_extended"
        else:
            reactivate(number, now, "rest period over")
            action = "reactivated"
    elif number.status == "active":
        if number.health_score < settings.health_quarantine_threshold:
            number.status = "quarantined"
            number.status_reason = _reason(m, number)
            action = "quarantined"
        elif number.health_score < settings.health_rest_threshold:
            number.status = "resting"
            number.status_reason = _reason(m, number)
            number.rested_until = now + timedelta(hours=settings.rest_hours)
            action = "rested"
    return {"number": number.e164, "score": number.health_score, "action": action, **asdict(m)}


def reactivate(number: PhoneNumber, now: datetime, reason: str) -> None:
    number.status, number.status_reason, number.rested_until = "active", reason, None
    number.metrics_since = now
    number.health_score = score(LineMetrics(0, 1.0, 0.0, 0.0, 0.0), number.spam_label)


def _lookup(db: Session, number: PhoneNumber) -> str:
    carrier = get_carrier()
    label = carrier.reputation_lookup(number.e164)
    db.add(SpamCheck(number_id=number.id, source=carrier.name, label=label))
    return label


def _reason(m: LineMetrics, number: PhoneNumber) -> str:
    parts = [f"score {number.health_score}"]
    if m.filter_rate >= 0.05:
        parts.append(f"filtered {m.filter_rate:.0%}")
    if m.optout_rate >= 0.02:
        parts.append(f"opt-out {m.optout_rate:.1%}")
    if number.spam_label == "spam_likely":
        parts.append("labelled Spam Likely")
    return ", ".join(parts)


def run_health_sweep(db: Session, now: datetime | None = None) -> list[dict]:
    numbers = db.scalars(select(PhoneNumber).where(PhoneNumber.status.in_(["active", "resting"]))).all()
    results = [evaluate(db, n, now) for n in numbers]
    db.commit()
    return results


def run_spam_checks(db: Session) -> list[dict]:
    """Query reputation for every in-service line. Schedule this daily in production."""
    out = []
    for n in db.scalars(select(PhoneNumber).where(PhoneNumber.status != "retired")):
        n.spam_label = _lookup(db, n)
        out.append({"number": n.e164, "label": n.spam_label})
    db.commit()
    return out


def sent_today_by_line(db: Session, now: datetime | None = None) -> dict[int, int]:
    """Outbound messages per line since midnight UTC, in one query (used on the send hot path)."""
    now = now or utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(Message.number_id, func.count(Message.id)).where(
            Message.direction == "outbound", Message.status != "blocked", Message.is_auto_reply.is_(False),
            Message.created_at >= start, Message.created_at <= now, Message.number_id.is_not(None),
        ).group_by(Message.number_id)
    ).all()
    return dict(rows)


def calls_today_by_line(db: Session, now: datetime | None = None) -> dict[int, int]:
    now = now or utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(select(CallLog.number_id, func.count(CallLog.id)).where(
        CallLog.started_at >= start, CallLog.started_at <= now).group_by(CallLog.number_id)).all()
    return dict(rows)


def sent_today(db: Session, number_id: int, now: datetime | None = None) -> int:
    return sent_today_by_line(db, now).get(number_id, 0)
