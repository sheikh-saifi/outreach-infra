"""Dialer: single-line (preview / power) and multi-line (parallel) modes.

Multi-line dials N leads at once for one agent. When one answers, the agent is connected;
if another also answers in the same batch there is nobody to take it, which is an
"abandoned" call (production must play a recorded ID message within 2 seconds). The FTC Telemarketing Sales Rule caps abandonment at 3% per campaign
per 30 days, so the dialer adapts: it drops a line whenever the running abandon rate
crosses the limit.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallLog, Lead, utcnow
from app.providers import get_carrier
from app.services import compliance
from app.services.sms import NoLineAvailable, pick_number

MAX_ABANDON_RATE = 0.03


def run_session(db: Session, lead_ids: list[int], lines: int = 1, now: datetime | None = None) -> dict:
    now = now or utcnow()
    session_id = str(uuid.uuid4())
    carrier = get_carrier()
    leads = db.scalars(select(Lead).where(Lead.id.in_(lead_ids))).all()
    queue = list(leads)
    stats = {"answered": 0, "abandoned": 0, "blocked": 0, "dialed": 0}
    line_history = []

    while queue:
        batch, queue = queue[:lines], queue[lines:]
        agent_busy = False
        for lead in batch:
            ok, reason = compliance.can_contact(db, lead, "voice", now)
            if not ok:
                stats["blocked"] += 1
                continue
            try:
                number = pick_number(db, lead, now=now)
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

        line_history.append(lines)
        answered_total = stats["answered"] + stats["abandoned"]
        if lines > 1 and answered_total and stats["abandoned"] / answered_total > MAX_ABANDON_RATE:
            lines -= 1

    db.commit()
    answered_total = stats["answered"] + stats["abandoned"]
    return {
        "session_id": session_id, **stats,
        "abandon_rate": round(stats["abandoned"] / answered_total, 3) if answered_total else 0.0,
        "lines_start": line_history[0] if line_history else lines, "lines_end": lines,
    }
