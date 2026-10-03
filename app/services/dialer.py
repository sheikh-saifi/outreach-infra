"""Dialer: single-line (preview / power) and multi-line (parallel) modes.

Multi-line dials N leads at once for one agent. When one answers, the agent is connected;
if another also answers in the same batch there is nobody to take it, which is an
"abandoned" call (production must play a recorded ID message within 2 seconds). The FTC Telemarketing Sales Rule caps abandonment at 3% per campaign
per 30 days, so the dialer measures the rate over the trailing 30 days (not just this
session) and drops a line whenever it crosses the limit.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CallLog, Lead, utcnow
from app.providers import get_carrier
from app.services import compliance
from app.services.sms import NoLineAvailable, pick_number



def _answered_last_30d(db: Session, now: datetime) -> tuple[int, int]:
    rows = dict(db.execute(select(CallLog.outcome, func.count()).where(
        CallLog.outcome.in_(["answered", "abandoned"]),
        CallLog.started_at > now - timedelta(days=30), CallLog.started_at <= now,
    ).group_by(CallLog.outcome)).all())
    return rows.get("answered", 0), rows.get("abandoned", 0)


def run_session(db: Session, lead_ids: list[int], lines: int = 1, now: datetime | None = None) -> dict:
    now = now or utcnow()
    session_id = str(uuid.uuid4())
    carrier = get_carrier()
    leads = db.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all()
    queue = list(leads)
    stats = {"answered": 0, "abandoned": 0, "blocked": 0, "dialed": 0}
    line_history = []
    prior_answered, prior_abandoned = _answered_last_30d(db, now)

    while queue:
        batch, queue = queue[:lines], queue[lines:]
        agent_busy = False
        for lead in batch:
            ok, reason = compliance.can_contact(db, lead, "voice", now)
            if not ok:
                stats["blocked"] += 1
                continue
            try:
                number = pick_number(db, lead, now=now, channel="voice")
            except NoLineAvailable:
                stats["blocked"] += 1
                continue
            res = carrier.place_call(number.e164, lead.phone)
            stats["dialed"] += 1
            outcome = res.outcome
            if outcome == "answered":
                if agent_busy:
                    outcome = "abandoned"
                    stats["abandoned"] += 1
                else:
                    agent_busy = True
                    stats["answered"] += 1
            db.add(CallLog(session_id=session_id, number_id=number.id, lead_id=lead.id,
                           outcome=outcome, started_at=now, duration_s=res.duration_s if outcome != "abandoned" else 0))
            db.flush()  # so per-line call caps and frequency caps see this call

        line_history.append(lines)
        abandoned = prior_abandoned + stats["abandoned"]
        answered_total = prior_answered + stats["answered"] + abandoned
        if lines > 1 and answered_total and abandoned / answered_total > settings.max_abandon_rate:
            lines -= 1

    db.commit()
    answered_total = stats["answered"] + stats["abandoned"]
    abandoned_30d = prior_abandoned + stats["abandoned"]
    answered_30d = prior_answered + answered_total
    return {
        "session_id": session_id, **stats,
        "abandon_rate": round(stats["abandoned"] / answered_total, 3) if answered_total else 0.0,
        "abandon_rate_30d": round(abandoned_30d / answered_30d, 3) if answered_30d else 0.0,
        "lines_start": line_history[0] if line_history else lines, "lines_end": lines,
    }
