"""Demo data + traffic simulator (mock providers only).

The demo week is produced by the real engine: leads are enrolled in an SMS campaign and an
email campaign, and the dispatcher is run at several points each simulated day, exactly as the
scheduler would. Only the *recipients* are simulated (replies, opt-outs, out-of-office, bounces).
So every number on the dashboard comes from production code paths.

Built-in scenarios to look for:
  - one recycled phone line with a bad reputation  -> caught by the health monitor
  - a sending domain without DKIM/DMARC            -> domain paused, its mailbox sends nothing
  - landlines, dead numbers, dead / typo / role email addresses in the lead list
  - Phoenix leads at 7am (no DST)                  -> texts deferred to 8am local, not dropped
  - weekend                                         -> campaign email waits for Monday
"""

import random
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Domain, Lead, Mailbox, Message, PhoneNumber, utcnow
from app.providers import get_carrier
from app.providers.mock import MockCarrier
from app.services import campaigns, domain_health, email_sender, line_health, provisioning, sms, warmup
from app.services.dialer import run_session

FIRST = ["James", "Maria", "Robert", "Linda", "Michael", "Patricia", "David", "Jennifer", "Carlos", "Aisha",
         "Wei", "Priya", "John", "Elena", "Omar", "Grace", "Daniel", "Fatima", "Kevin", "Sofia"]
LAST = ["Smith", "Garcia", "Johnson", "Lee", "Brown", "Martinez", "Davis", "Khan", "Wilson", "Nguyen"]
STREETS = ["Oak St", "Maple Ave", "Pine Rd", "Cedar Ln", "Elm Dr", "Lakeview Blvd", "Sunset Way", "Hill St"]
AREAS = {"214": "Dallas, TX", "713": "Houston, TX", "305": "Miami, FL", "404": "Atlanta, GA", "602": "Phoenix, AZ"}
EMAIL_HOSTS = ["gmail.com", "yahoo.com", "outlook.com", "aol.com", "icloud.com"]

SMS_STEPS = [
    {"channel": "sms", "delay_days": 0,
     "body": "Hi {first_name}, this is Sam with Acme Homes. Would you consider an offer on {address}? Reply STOP to opt out."},
    {"channel": "sms", "delay_days": 2,
     "body": "Hi {first_name}, following up on {address}. Still open to a cash offer? Reply STOP to opt out."},
    {"channel": "sms", "delay_days": 3,
     "body": "Last note from me, {first_name}. If selling {address} is ever on the table, just reply here."},
]
EMAIL_STEPS = [
    {"channel": "email", "delay_days": 0, "subject": "Question about {address}",
     "body": "Hi {first_name},\n\nI buy houses in your area and wanted to ask if you'd consider an offer on "
             "{address}. No repairs or agent fees, and you pick the closing date.\n\nWould a quick call this week work?\n\nSam"},
    {"channel": "email", "delay_days": 3, "subject": "Question about {address}",
     "body": "Hi {first_name},\n\nJust bumping this up in case it got buried. Happy to share a ballpark number "
             "for {address} if that helps.\n\nSam"},
    {"channel": "email", "delay_days": 4, "subject": "Question about {address}",
     "body": "Hi {first_name},\n\nI'll stop reaching out after this one. If you ever think about selling, "
             "just reply and I'll get back to you the same day.\n\nSam"},
]
SMS_REPLIES = ["How much?", "Maybe, what's your offer?", "Who is this?", "Not interested", "Call me tomorrow",
               "It's a rental, could sell", "Wrong number", "Please stop texting me"]
EMAIL_REPLIES = ["What kind of offer are we talking about?", "Not interested, thanks.", "Can you call me? 214-555-0199",
                 "Please remove me from your list.", "We might sell next year. Keep in touch.", "Who gave you my email?"]
RUN_EVERY_MIN = 15  # the real scheduler runs every 30s; 15 simulated minutes is plenty for a demo


def _make_leads(rng: random.Random, n: int, offset: int = 0) -> list[Lead]:
    leads = []
    for i in range(offset, offset + n):
        ac = rng.choice(list(AREAS))
        first, last = rng.choice(FIRST), rng.choice(LAST)
        r = rng.random()
        host = rng.choice(EMAIL_HOSTS)
        local = f"{first.lower()}.{last.lower()}{i}"
        if r < 0.12:
            email = None                                # skip tracing found no email
        elif r < 0.135:
            email = f"old.{local}@{host}"               # abandoned mailbox: hard-bounces at SMTP
        elif r < 0.145:
            email = f"dead.{local}@{host}"              # bounces later via a DSN email
        elif r < 0.165:
            email = f"{local}@gmial.com"                # typo, caught by verification
        elif r < 0.185:
            email = f"info@{last.lower()}{i}homes.com"  # role account
        else:
            email = f"{local}@{host}"
        leads.append(Lead(name=f"{first} {last}", phone=f"+1{ac}{rng.randint(2000000, 9999999)}", email=email,
                          property_address=f"{rng.randint(100, 9999)} {rng.choice(STREETS)}, {AREAS[ac]}"))
    return leads


def seed(db: Session, rng: random.Random | None = None) -> None:
    if db.scalar(select(func.count(PhoneNumber.id))):
        return
    rng = rng or random.Random(7)
    carrier = get_carrier()
    now = utcnow()
    today = now.date()
    start = today - timedelta(days=6)

    for ac in AREAS:
        provisioning.provision(db, ac, 3, campaign="sellers-q4")
    if isinstance(carrier, MockCarrier):
        # Make one Miami line a recycled number with a bad history (see MockCarrier.RECYCLED_SUFFIX).
        burned = db.scalars(select(PhoneNumber).where(PhoneNumber.area_code == "305")).first()
        burned.e164 = burned.e164[:-2] + MockCarrier.RECYCLED_SUFFIX
        db.commit()
    line_health.run_spam_checks(db)

    leads = _make_leads(rng, 240)
    db.add_all(leads)
    db.commit()

    # Sending domains + mailboxes, with warm-up history up to the start of the demo week.
    for name, ok in [("acmehomes-offers.com", True), ("tryacmehomes.com", True), ("acmehomesdeals.com", False)]:
        db.add(Domain(name=name, has_mx=True, spf_ok=True, dkim_ok=ok, dmarc_ok=ok,
                      dmarc_policy="none" if ok else None, last_checked=now,
                      notes=None if ok else "No DKIM key found on common selectors.\nNo DMARC record."))
    db.commit()
    domains = db.scalars(select(Domain).order_by(Domain.id)).all()
    boxes = []
    for local, display, dom, age in [("sam", "Sam Carter", 0, 40), ("sam.c", "Sam Carter", 0, 34),
                                     ("sam", "Sam Carter", 1, 30), ("offers", "Sam Carter", 1, 18),
                                     ("hello", "Sam Carter", 2, 20)]:
        mb = Mailbox(address=f"{local}@{domains[dom].name}", display_name=display, domain_id=domains[dom].id,
                     warmup_started=today - timedelta(days=age))
        db.add(mb)
        db.commit()
        boxes.append(mb)
        d = mb.warmup_started
        while d < start:
            warmup.simulate_day(db, mb, d, rng)
            d += timedelta(days=1)
    domain_health.run_domain_checks(db, live_dns=False)

    sms_c = campaigns.create(db, "Sellers Q4 · SMS", SMS_STEPS)
    email_c = campaigns.create(db, "Sellers Q4 · Email", EMAIL_STEPS)
    campaigns.activate(db, sms_c)
    campaigns.activate(db, email_c)
    week_start = datetime(start.year, start.month, start.day, 13, 0)
    campaigns.enroll(db, sms_c, [l.id for l in leads], now=week_start)
    campaigns.enroll(db, email_c, [l.id for l in leads if l.email], now=week_start)

    # Run the week through the real dispatcher.
    for d in range(7):
        day = start + timedelta(days=d)
        for mb in boxes:
            warmup.simulate_day(db, mb, day, rng)
        # Every US zone's 8am-9pm falls inside 12:00-08:00 UTC; run through the whole span.
        t = datetime(day.year, day.month, day.day, 12, 0)
        while t < datetime(day.year, day.month, day.day, 23, 59) and t <= now:
            _run_and_respond(db, t, rng)
            t += timedelta(minutes=RUN_EVERY_MIN)
        line_health.run_health_sweep(db, now=min(now, datetime(day.year, day.month, day.day, 23, 59)))
        domain_health.run_domain_checks(db, live_dns=False)

    yesterday = today - timedelta(days=1)
    run_session(db, [l.id for l in leads[:60]], lines=3, now=datetime(yesterday.year, yesterday.month, yesterday.day, 19, 0))


def _run_and_respond(db: Session, t: datetime, rng: random.Random) -> dict:
    last_id = db.scalar(select(func.max(Message.id))) or 0
    result = campaigns.run_due(db, now=t)
    for msg in db.scalars(select(Message).where(Message.id > last_id, Message.direction == "outbound",
                                                Message.is_auto_reply.is_(False))).all():
        lead = db.get(Lead, msg.lead_id) if msg.lead_id else None
        if not lead:
            continue
        if msg.channel == "sms":
            _sms_response(db, lead, msg, rng)
        else:
            _email_response(db, lead, msg, rng)
    return result


def _sms_response(db: Session, lead: Lead, msg: Message, rng: random.Random) -> None:
    if msg.status != "delivered":
        return
    carrier = get_carrier()
    rep = carrier.reputation(msg.from_addr) if isinstance(carrier, MockCarrier) else 1.0
    r = rng.random()
    stop_p = 0.012 + (1 - rep) * 0.06  # recipients of spammy-looking lines opt out more
    when = msg.created_at + timedelta(minutes=rng.randint(2, 90))
    if r < stop_p:
        sms.handle_inbound(db, lead.phone, msg.from_addr, rng.choice(["STOP", "stop", "Unsubscribe"]), now=when)
    elif r < stop_p + 0.05:
        sms.handle_inbound(db, lead.phone, msg.from_addr, rng.choice(SMS_REPLIES), now=when)


def _email_response(db: Session, lead: Lead, msg: Message, rng: random.Random) -> None:
    if msg.status != "sent":
        return
    when = msg.created_at + timedelta(minutes=rng.randint(5, 240))
    if lead.email.startswith("dead."):
        email_sender.handle_inbound(
            db, "MAILER-DAEMON@mx.google.com", msg.from_addr, "Delivery Status Notification (Failure)",
            f"Delivery to the following recipient failed permanently:\n\n    {lead.email}\n\n"
            f"Final-Recipient: rfc822; {lead.email}\nAction: failed\nStatus: 5.1.1\n"
            "Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.",
            {"Content-Type": "multipart/report; report-type=delivery-status"}, now=when)
        return
    r = rng.random()
    re_subject = msg.subject if (msg.subject or "").lower().startswith("re:") else f"Re: {msg.subject}"
    headers = {"In-Reply-To": msg.message_id_hdr, "Message-ID": f"<{rng.getrandbits(64):x}@{lead.email.split('@')[1]}>"}
    if r < 0.035:
        email_sender.handle_inbound(db, lead.email, msg.from_addr, re_subject, rng.choice(EMAIL_REPLIES),
                                    headers, now=when)
    elif r < 0.06:
        email_sender.handle_inbound(db, lead.email, msg.from_addr, f"Automatic reply: {msg.subject}",
                                    "I'm out of the office until Monday with limited access to email.",
                                    {**headers, "Auto-Submitted": "auto-replied"}, now=when)
    elif r < 0.0608:
        email_sender.handle_inbound(db, "fbl@feedback.yahoo.com", msg.from_addr, "Complaint about message",
                                    f"Feedback-Type: abuse\nOriginal-Rcpt-To: {lead.email}",
                                    {"Content-Type": "multipart/report; report-type=feedback-report",
                                     "Feedback-Type": "abuse"}, now=when)


def tick(db: Session, new_leads: int = 20, rng: random.Random | None = None) -> dict:
    """'Simulate traffic': a fresh batch of leads arrives, is enrolled in the active campaigns, and
    the dispatcher runs now. Outside sending hours you'll see them deferred rather than sent."""
    rng = rng or random.Random()
    offset = db.scalar(select(func.count(Lead.id))) or 0
    leads = _make_leads(rng, new_leads, offset)
    db.add_all(leads)
    db.commit()
    from app.models import Campaign
    for c in db.scalars(select(Campaign).where(Campaign.status == "active")):
        ids = [l.id for l in leads if (l.email if c.steps[0].channel == "email" else l.phone)]
        campaigns.enroll(db, c, ids)
    result = _run_and_respond(db, utcnow(), rng)
    sweep = line_health.run_health_sweep(db)
    from app.models import Enrollment
    waiting = db.scalars(select(Enrollment.next_run_at).where(
        Enrollment.lead_id.in_([l.id for l in leads]), Enrollment.status == "active",
        Enrollment.next_run_at > utcnow()).order_by(Enrollment.next_run_at)).all()
    return {"new_leads": new_leads, "dispatch": result, "scheduled_later": len(waiting),
            "next_send_at": waiting[0] if waiting else None,
            "health_actions": [s for s in sweep if s["action"] != "none"]}
