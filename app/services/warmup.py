"""Mailbox warm-up scheduler.

New inboxes ramp from ~5 to ~40 emails/day over 4 weeks. The ramp is a safety rail, not a
fixed timetable: if bounces or complaints over the trailing 7 days exceed thresholds, the
mailbox pauses; it only graduates to 'active' after completing the ramp cleanly.
"""

import random
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Mailbox, MailboxDailyStat

MIN_SAMPLE = 50
MIN_BOUNCES = 3
MIN_COMPLAINTS = 2


def planned_volume(day_index: int) -> int:
    """Daily send target on warm-up day `day_index` (0-based)."""
    if day_index <= 0:
        return settings.warmup_start_volume
    if day_index >= settings.warmup_days:
        return settings.warmup_target_volume
    span = settings.warmup_target_volume - settings.warmup_start_volume
    return round(settings.warmup_start_volume + span * day_index / settings.warmup_days)


def trailing_rates(db: Session, mailbox: Mailbox, today: date, days: int = 7) -> dict:
    rows = db.scalars(select(MailboxDailyStat).where(
        MailboxDailyStat.mailbox_id == mailbox.id,
        MailboxDailyStat.day > today - timedelta(days=days), MailboxDailyStat.day <= today,
    )).all()
    # Warm-up and campaign mail both count: a bad lead list bounces regardless of why we sent.
    sent = sum(r.sent + (r.cold_sent or 0) for r in rows)
    placements = [r.inbox_placement for r in rows if r.inbox_placement is not None]
    return {
        "sent": sent,
        "bounces": sum(r.bounces for r in rows),
        "complaints": sum(r.complaints for r in rows),
        "bounce_rate": sum(r.bounces for r in rows) / sent if sent else 0.0,
        "complaint_rate": sum(r.complaints for r in rows) / sent if sent else 0.0,
        "reply_rate": sum(r.replies for r in rows) / sent if sent else 0.0,
        "inbox_placement": sum(placements) / len(placements) if placements else None,
    }


def evaluate(db: Session, mailbox: Mailbox, today: date) -> dict:
    """Decide today's quota and update status. Paused mailboxes get a quota of 0."""
    day_index = (today - mailbox.warmup_started).days
    rates = trailing_rates(db, mailbox, today)

    # Early warm-up volume is tiny, so one bounce in 20 sends reads as 5%. Only act on rates
    # once there's a meaningful sample AND more than a one-off event.
    enough = rates["sent"] >= MIN_SAMPLE
    if enough and rates["bounces"] >= MIN_BOUNCES and rates["bounce_rate"] > settings.max_bounce_rate:
        mailbox.status, mailbox.status_reason = "paused", f"bounce rate {rates['bounce_rate']:.1%} (7d)"
    elif enough and rates["complaints"] >= MIN_COMPLAINTS and rates["complaint_rate"] > settings.max_complaint_rate:
        mailbox.status, mailbox.status_reason = "paused", f"complaint rate {rates['complaint_rate']:.2%} (7d)"
    elif mailbox.status == "warming" and day_index >= settings.warmup_days:
        mailbox.status, mailbox.status_reason = "active", "warm-up complete"

    quota = 0 if mailbox.status == "paused" else planned_volume(day_index)
    return {"mailbox": mailbox.address, "day": day_index, "status": mailbox.status,
            "reason": mailbox.status_reason, "quota": quota, **rates}


def resume(mailbox: Mailbox, today: date, step_back_days: int = 7) -> None:
    """Manual resume after fixing the cause (e.g. cleaning the list). The ramp steps back a week."""
    day_index = max(0, (today - mailbox.warmup_started).days - step_back_days)
    mailbox.warmup_started = today - timedelta(days=day_index)
    mailbox.status, mailbox.status_reason = "warming", "resumed manually"


def simulate_day(db: Session, mailbox: Mailbox, today: date, rng: random.Random,
                 list_quality: float = 0.998) -> MailboxDailyStat:
    """Mock-mode only: generate a day of warm-up results so the dashboard shows real curves.
    Additive and once per day, so it never overwrites campaign sends/bounces already recorded."""
    plan = evaluate(db, mailbox, today)
    sent = plan["quota"]
    stat = db.scalar(select(MailboxDailyStat).where(
        MailboxDailyStat.mailbox_id == mailbox.id, MailboxDailyStat.day == today)) \
        or MailboxDailyStat(mailbox_id=mailbox.id, day=today, sent=0, cold_sent=0, bounces=0, complaints=0, replies=0)
    if stat.planned:  # already simulated today
        return stat
    stat.planned = plan["quota"]
    stat.sent = (stat.sent or 0) + sent
    stat.bounces = (stat.bounces or 0) + sum(1 for _ in range(sent) if rng.random() > list_quality)
    stat.replies = (stat.replies or 0) + sum(1 for _ in range(sent) if rng.random() < 0.25)  # warm-up network
    stat.inbox_placement = round(min(1.0, 0.7 + 0.01 * plan["day"] + rng.uniform(-0.05, 0.05)), 2) if sent else None
    db.add(stat)
    db.commit()
    return stat
