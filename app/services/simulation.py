"""Demo data + traffic simulator (mock provider only).

Seeds a realistic week of outreach so every dashboard panel has data, including one
deliberately burned line so you can watch the health monitor catch and quarantine it.
"""

import random
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Domain, Lead, Mailbox, Message, PhoneNumber, utcnow
from app.providers import get_carrier
from app.providers.mock import MockCarrier
from app.services import line_health, provisioning, sms, warmup
from app.services.dialer import run_session

FIRST = ["James", "Maria", "Robert", "Linda", "Michael", "Patricia", "David", "Jennifer", "Carlos", "Aisha",
         "Wei", "Priya", "John", "Elena", "Omar", "Grace", "Daniel", "Fatima", "Kevin", "Sofia"]
LAST = ["Smith", "Garcia", "Johnson", "Lee", "Brown", "Martinez", "Davis", "Khan", "Wilson", "Nguyen"]
STREETS = ["Oak St", "Maple Ave", "Pine Rd", "Cedar Ln", "Elm Dr", "Lakeview Blvd", "Sunset Way", "Hill St"]
AREAS = {"214": "Dallas, TX", "713": "Houston, TX", "305": "Miami, FL", "404": "Atlanta, GA", "602": "Phoenix, AZ"}

OPENER = "Hi {first_name}, this is Sam with Acme Homes. Would you consider an offer on {address}? Reply STOP to opt out."
FOLLOW_UP = "Hi {first_name}, following up on {address}. Still open to a cash offer? Reply STOP to opt out."
REPLIES = ["How much?", "Maybe, what's your offer?", "Who is this?", "Not interested", "Call me tomorrow",
           "It's a rental, could sell", "Wrong number"]


def _business_hours(day: date, rng: random.Random) -> datetime:
    # 16:00-23:00 UTC is inside 8am-9pm in every continental US zone.
    return datetime(day.year, day.month, day.day, rng.randint(16, 22), rng.randint(0, 59))


def seed(db: Session, rng: random.Random | None = None) -> None:
    if db.scalar(select(func.count(PhoneNumber.id))):
        return
    rng = rng or random.Random(7)
    carrier = get_carrier()

    for ac in AREAS:
        provisioning.provision(db, ac, 3, campaign="sellers-q4")
    if isinstance(carrier, MockCarrier):
        burned = db.scalars(select(PhoneNumber).where(PhoneNumber.area_code == "305")).first()
        carrier.set_reputation(burned.e164, 0.35)  # a recycled number with a bad history

    leads = []
    for i in range(360):
        ac = rng.choice(list(AREAS))
        first, last = rng.choice(FIRST), rng.choice(LAST)
        leads.append(Lead(
            name=f"{first} {last}", phone=f"+1{ac}{rng.randint(2000000, 9999999)}",
            email=f"{first.lower()}.{last.lower()}{i}@example.com",
            property_address=f"{rng.randint(100, 9999)} {rng.choice(STREETS)}, {AREAS[ac]}",
        ))
    db.add_all(leads)
    db.commit()

    today = utcnow().date()
    line_health.run_spam_checks(db)
    _simulate_week(db, leads, today, rng)
    run_session(db, [l.id for l in leads[:60]], lines=3,
                now=_business_hours(today - timedelta(days=1), rng))

    for name, ok in [("acmehomes-offers.com", True), ("tryacmehomes.com", True), ("acmehomesdeals.com", False)]:
        db.add(Domain(name=name, has_mx=True, spf_ok=True, dkim_ok=ok, dmarc_ok=ok,
                      dmarc_policy="none" if ok else None, last_checked=utcnow(),
                      notes=None if ok else "No DKIM key found on common selectors.\nNo DMARC record."))
    db.commit()
    domains = db.scalars(select(Domain)).all()
    for i, (local, dom, age, quality) in enumerate([
        ("sam", 0, 34, 0.997), ("sam.r", 0, 21, 0.997), ("offers", 1, 14, 0.997),
        ("sam", 1, 9, 0.997), ("hello", 2, 12, 0.93),  # last one mails a dirty list -> pauses
    ]):
        mb = Mailbox(address=f"{local}@{domains[dom].name}", domain_id=domains[dom].id,
                     warmup_started=today - timedelta(days=age))
        db.add(mb)
        db.commit()
        for d in range(age + 1):
            warmup.simulate_day(db, mb, mb.warmup_started + timedelta(days=d), rng, list_quality=quality)


def _simulate_week(db: Session, leads: list[Lead], today: date, rng: random.Random) -> None:
    schedule = {6: OPENER, 4: FOLLOW_UP, 1: FOLLOW_UP}  # days-ago -> template
    replied: set[int] = set()
    for days_ago, template in sorted(schedule.items(), reverse=True):
        day = today - timedelta(days=days_ago)
        for lead in leads:
            if lead.id in replied:
                continue
            try:
                msg = sms.send(db, lead, template, now=_business_hours(day, rng))
            except sms.NoLineAvailable:
                continue
            _maybe_reply(db, lead, msg, rng, replied)
        # Nightly health sweep, as a scheduler would run it in production.
        line_health.run_health_sweep(db, now=datetime(day.year, day.month, day.day, 23, 59))


def _maybe_reply(db: Session, lead: Lead, msg: Message, rng: random.Random, replied: set[int]) -> None:
    if msg.status != "delivered":
        return
    carrier = get_carrier()
    rep = carrier.reputation(msg.from_addr) if isinstance(carrier, MockCarrier) else 1.0
    r = rng.random()
    stop_p = 0.012 + (1 - rep) * 0.06  # recipients of spammy-looking lines opt out more
    when = msg.created_at + timedelta(minutes=rng.randint(2, 90))
    if r < stop_p:
        sms.handle_inbound(db, lead.phone, msg.from_addr, rng.choice(["STOP", "stop", "Unsubscribe"]), now=when)
        replied.add(lead.id)
    elif r < stop_p + 0.05:
        sms.handle_inbound(db, lead.phone, msg.from_addr, rng.choice(REPLIES), now=when)
        replied.add(lead.id)


def tick(db: Session, batch: int = 40, rng: random.Random | None = None) -> dict:
    """Send one live batch now, generate replies, and re-run health checks."""
    rng = rng or random.Random()
    leads = db.scalars(select(Lead).order_by(func.random()).limit(batch)).all()
    counts: dict[str, int] = {}
    replied: set[int] = set()
    for lead in leads:
        try:
            msg = sms.send(db, lead, FOLLOW_UP)
        except sms.NoLineAvailable:
            counts["no_line"] = counts.get("no_line", 0) + 1
            continue
        counts[msg.status] = counts.get(msg.status, 0) + 1
        _maybe_reply(db, lead, msg, rng, replied)
    sweep = line_health.run_health_sweep(db)
    return {"sent": counts, "replies": len(replied),
            "health_actions": [s for s in sweep if s["action"] != "none"]}
