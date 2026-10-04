"""React to inbound events immediately, instead of waiting for a lead's next step to come due.

A reply on Tuesday must stop the sequence on Tuesday. If it only stopped when the next step
became due, the dashboard would show the lead as "active" for days, and a STOP would be counted
as a reply.
"""

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Enrollment, Lead

OUTCOME = {
    "reply": ("replied", "lead replied"),
    "stop": ("stopped", "opted out"),
    "wrong_number": ("stopped", "wrong number"),
    "bounce": ("stopped", "hard bounce"),
    "complaint": ("stopped", "spam complaint"),
}


def on_inbound(db: Session, event: str, phone: str | None = None, email: str | None = None) -> int:
    """Close every active enrollment of the person behind this phone/email. Returns how many."""
    if event not in OUTCOME:
        return 0
    conds = []
    if phone:
        conds.append(Lead.phone == phone)
    if email:
        conds.append(func.lower(Lead.email) == email.lower())
    if not conds:
        return 0
    status, reason = OUTCOME[event]
    rows = db.scalars(select(Enrollment).join(Lead, Lead.id == Enrollment.lead_id)
                      .where(Enrollment.status == "active", or_(*conds))).all()
    for e in rows:
        e.status, e.stop_reason, e.next_run_at = status, reason, None
    return len(rows)
