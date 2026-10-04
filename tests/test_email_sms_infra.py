"""Email + SMS infrastructure: sending engine, inbound processing, campaigns, linting, verification."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Campaign, Domain, Enrollment, Lead, Mailbox, MailboxDailyStat, Message
from app.providers import get_email_provider
from app.services import campaigns, compliance, content_lint, domain_health, email_sender, email_verify, sms

# Tuesday 2026-03-10, 17:00 UTC = 12pm Dallas: inside every SMS and email window.
NOON = datetime(2026, 3, 10, 17, 0)


@pytest.fixture
def mailbox(db):
    def _make(address="sam@acme-offers.com", age_days=40, domain_ok=True, cap=30):
        name = address.split("@")[1]
        d = db.query(Domain).filter_by(name=name).one_or_none()
        if not d:
            d = Domain(name=name, has_mx=True, spf_ok=True, dkim_ok=domain_ok, dmarc_ok=domain_ok, status="active")
            db.add(d)
            db.commit()
        mb = Mailbox(address=address, domain_id=d.id, warmup_started=NOON.date() - timedelta(days=age_days),
                     status="active" if age_days >= 28 else "warming", daily_cold_cap=cap, display_name="Sam Carter")
        db.add(mb)
        db.commit()
        return mb
    return _make


@pytest.fixture
def email_lead(db):
    def _make(email="maria.lee@gmail.com", phone="+12145551234", name="Maria Lee"):
        lead = Lead(name=name, phone=phone, email=email, property_address="12 Oak St, Dallas, TX")
        db.add(lead)
        db.commit()
        return lead
    return _make


# ---------------------------------------------------------------- content lint

def test_sms_segments_and_encoding():
    assert content_lint.sms_segments("a" * 160) == {"encoding": "GSM-7", "length": 160, "segments": 1, "non_gsm_chars": []}
    assert content_lint.sms_segments("a" * 161)["segments"] == 2
    curly = content_lint.sms_segments("It’s a great offer")  # curly apostrophe
    assert curly["encoding"] == "UCS-2" and curly["non_gsm_chars"] == ["’"]
    assert content_lint.sms_segments("a" * 71 + "’")["segments"] == 2
    assert content_lint.sms_segments("[" * 80)["length"] == 160  # extended chars count double


def test_sms_lint_errors_and_warnings():
    codes = lambda r: {i["code"] for i in r["issues"]}  # noqa: E731
    assert "opt_out" in codes(content_lint.lint_sms("Hi {first_name}, want an offer?", first_message=True))
    assert "opt_out" not in codes(content_lint.lint_sms("Following up!", first_message=False))
    assert "shortener" in codes(content_lint.lint_sms("See bit.ly/abc Reply STOP to opt out", True))
    assert "merge_field" in codes(content_lint.lint_sms("Hi {firstname} Reply STOP", True))
    assert "shaft" in codes(content_lint.lint_sms("Free beer for sellers! Reply STOP", True))
    clean = content_lint.lint_sms("Hi {first_name}, would you sell {address}? Reply STOP to opt out.", True)
    assert not content_lint.has_errors(clean)


def test_email_lint():
    assert content_lint.has_errors(content_lint.lint_email("", "body"))
    r = content_lint.lint_email("Quick question", "word " * 200 + "http://a.com http://b.com")
    assert {"length", "links"} <= {i["code"] for i in r["issues"]} and not content_lint.has_errors(r)


# ---------------------------------------------------------------- verification

@pytest.mark.parametrize("address,status", [
    ("maria@gmail.com", "valid"), ("not-an-email", "invalid"), ("a..b@gmail.com", "invalid"),
    ("x@mailinator.com", "invalid"), ("maria@gmial.com", "invalid"), ("info@acmehomes.com", "risky"),
])
def test_email_verification(address, status):
    assert email_verify.verify(address, check_mx=False)[0] == status


def test_email_verification_mx():
    assert email_verify.verify("a@nomx.example", has_mx=lambda d: False)[0] == "invalid"


# ---------------------------------------------------------------- sending

def test_email_has_required_headers_and_footer(db, mailbox, email_lead):
    mailbox()
    lead = email_lead()
    msg = email_sender.send(db, lead, "About {address}", "Hi {first_name}, interested?", now=NOON)
    sent = get_email_provider().outbox[-1]
    assert msg.status == "sent" and sent["Subject"] == "About 12 Oak St, Dallas, TX"
    assert sent["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/u/" in sent["List-Unsubscribe"] and sent["Message-ID"] == msg.message_id_hdr
    body = sent.get_content()
    assert "Hi Maria, interested?" in body and "Dallas, TX 75201" in body and "Unsubscribe:" in body
    assert sent["From"] == "Sam Carter <sam@acme-offers.com>"


def test_followup_threads_under_previous_email(db, mailbox, email_lead):
    mailbox()
    lead = email_lead()
    first = email_sender.send(db, lead, "About {address}", "one", now=NOON)
    second = email_sender.send(db, lead, "ignored", "two", now=NOON, reply_to=first)
    assert second.subject == "Re: About 12 Oak St, Dallas, TX"
    assert second.in_reply_to == first.message_id_hdr and second.mailbox_id == first.mailbox_id


def test_mailbox_rotation_respects_warmup_and_caps(db, mailbox, email_lead):
    young = mailbox("a@one.com", age_days=5)        # before cold_start_day: no cold email
    warming = mailbox("b@two.com", age_days=20)     # half of warm-up volume
    warm = mailbox("c@three.com", age_days=40, cap=2)
    today = NOON.date()
    assert email_sender.cold_quota(db, young, today) == 0
    assert 0 < email_sender.cold_quota(db, warming, today) < 30
    assert email_sender.cold_quota(db, warm, today) == 2
    used = {email_sender.send(db, email_lead(f"p{i}@gmail.com", phone=None), "s", "b", now=NOON).mailbox_id
            for i in range(6)}
    assert young.id not in used and {warming.id, warm.id} <= used


def test_paused_domain_sends_nothing(db, mailbox, email_lead):
    mailbox(domain_ok=False)
    for d in db.query(Domain):
        domain_health.evaluate(db, d)
    db.commit()
    with pytest.raises(email_sender.NoMailboxAvailable):
        email_sender.send(db, email_lead(), "s", "b", now=NOON)


def test_invalid_address_is_suppressed_not_sent(db, mailbox, email_lead):
    mailbox()
    lead = email_lead("maria@gmial.com")
    msg = email_sender.send(db, lead, "s", "b", now=NOON)
    assert msg.status == "blocked" and compliance.is_suppressed(db, lead.email, "email").reason == "invalid_email"


def test_smtp_hard_bounce_suppresses(db, mailbox, email_lead):
    mb = mailbox()
    lead = email_lead("old.maria@gmail.com")
    msg = email_sender.send(db, lead, "s", "b", now=NOON)
    assert msg.status == "bounced" and msg.error_code == "550 5.1.1"
    assert compliance.is_suppressed(db, lead.email, "email").reason == "bounce"
    assert db.query(MailboxDailyStat).filter_by(mailbox_id=mb.id).one().bounces == 1


# ---------------------------------------------------------------- inbound email

def _enrolled(db, lead, channel="email"):
    steps = [{"channel": channel, "delay_days": 0, "subject": "Hi", "body": "Hi {first_name}. Reply STOP to opt out."},
             {"channel": channel, "delay_days": 3, "subject": "Hi", "body": "Follow up. Reply STOP to opt out."}]
    c = campaigns.create(db, "t", steps)
    campaigns.activate(db, c)
    campaigns.enroll(db, c, [lead.id], now=NOON)
    return db.query(Enrollment).filter_by(lead_id=lead.id, campaign_id=c.id).one()


def test_dsn_hard_bounce(db, mailbox, email_lead):
    mb = mailbox()
    lead = email_lead("dead.maria@gmail.com")
    e = _enrolled(db, lead)
    r = email_sender.handle_inbound(
        db, "MAILER-DAEMON@mx.google.com", mb.address, "Delivery Status Notification (Failure)",
        f"Final-Recipient: rfc822; {lead.email}\nStatus: 5.1.1\n", {}, now=NOON)
    assert r["kind"] == "bounce" and r["severity"] == "hard" and r["recipient"] == lead.email
    assert compliance.is_suppressed(db, lead.email, "email")
    db.refresh(e)
    assert e.status == "stopped" and e.stop_reason == "hard bounce"


def test_soft_bounce_does_not_suppress(db, mailbox, email_lead):
    mb = mailbox()
    lead = email_lead()
    r = email_sender.handle_inbound(db, "postmaster@yahoo.com", mb.address, "Delivery delayed",
                                    f"Final-Recipient: rfc822; {lead.email}\nStatus: 4.2.2 mailbox full", {}, now=NOON)
    assert r["severity"] == "soft" and not compliance.is_suppressed(db, lead.email, "email")


def test_out_of_office_does_not_stop_sequence(db, mailbox, email_lead):
    mb = mailbox()
    lead = email_lead()
    e = _enrolled(db, lead)
    r = email_sender.handle_inbound(db, lead.email, mb.address, "Automatic reply: Hi", "Away until Monday",
                                    {"Auto-Submitted": "auto-replied"}, now=NOON)
    db.refresh(e)
    assert r["kind"] == "auto_reply" and e.status == "active" and not sms.has_replied(db, lead)


def test_reply_threads_and_stops_all_sequences_immediately(db, mailbox, email_lead):
    mb = mailbox()
    lead = email_lead()
    e_email = _enrolled(db, lead, "email")
    e_sms = _enrolled(db, lead, "sms")
    sent = email_sender.send(db, lead, "Hi", "b", now=NOON)
    other = email_lead("someone.else@gmail.com", phone=None)  # reply from a different address, threaded by header
    r = email_sender.handle_inbound(db, other.email, mb.address, "Re: Hi", "My husband owns it, what's the offer?",
                                    {"In-Reply-To": sent.message_id_hdr}, now=NOON)
    assert r["kind"] == "reply" and r["lead_id"] == lead.id
    sms.handle_inbound(db, lead.phone, "+12145550000", "how much?", now=NOON)
    db.refresh(e_email)
    db.refresh(e_sms)
    assert e_email.status == "replied" and e_sms.status == "replied"


def test_unsubscribe_reply_and_complaint(db, mailbox, email_lead):
    mb = mailbox()
    a, b = email_lead("a.person@gmail.com"), email_lead("b.person@yahoo.com", phone="+12145559999")
    email_sender.handle_inbound(db, a.email, mb.address, "Re: Hi", "Please remove me from your list.", {}, now=NOON)
    assert compliance.is_suppressed(db, a.email, "email").reason == "opt_out"
    r = email_sender.handle_inbound(db, "fbl@feedback.yahoo.com", mb.address, "Complaint",
                                    f"Feedback-Type: abuse\nOriginal-Rcpt-To: {b.email}", {"Feedback-Type": "abuse"}, now=NOON)
    assert r["kind"] == "complaint" and compliance.is_suppressed(db, b.email, "email").reason == "complaint"


def test_unsubscribe_link_get_is_safe_post_unsubscribes(db, email_lead):
    lead = email_lead()
    token = email_sender.unsubscribe_token(lead.email)
    with TestClient(app) as c:
        assert c.get(f"/u/{token}").status_code == 200
        assert not compliance.is_suppressed(db, lead.email, "email")  # link scanners must not unsubscribe
        assert c.post(f"/u/{token}", data={"List-Unsubscribe": "One-Click"}).status_code == 200
        assert c.get(f"/u/{token[:-2]}xx").status_code == 404           # forged token
    assert compliance.is_suppressed(db, lead.email, "email")


# ---------------------------------------------------------------- campaign dispatcher

def test_activation_blocked_by_lint_errors(db):
    c = campaigns.create(db, "bad", [{"channel": "sms", "delay_days": 0, "body": "Hi {first_name}, sell? bit.ly/x"}])
    with pytest.raises(campaigns.CampaignInvalid):
        campaigns.activate(db, c)
    assert c.status == "draft"


def test_sms_sequence_end_to_end(db, make_line):
    make_line()
    lead = Lead(name="Ann Lee", phone="+12145551234")
    db.add(lead)
    db.commit()
    e = _enrolled(db, lead, "sms")
    assert campaigns.run_due(db, now=NOON)["sent"] == 1
    assert e.current_step == 1 and e.next_run_at.date() == date(2026, 3, 13)
    assert campaigns.run_due(db, now=NOON + timedelta(days=1))["due"] == 0          # not due yet
    assert campaigns.run_due(db, now=NOON + timedelta(days=3))["completed"] == 1
    assert e.status == "completed" and db.query(Message).filter_by(enrollment_id=e.id).count() == 2


def test_quiet_hours_defer_instead_of_drop(db, make_line):
    make_line()
    lead = Lead(name="Ann Lee", phone="+16025551234")  # Phoenix
    db.add(lead)
    db.commit()
    e = _enrolled(db, lead, "sms")
    six_am_phoenix = datetime(2026, 7, 14, 13, 0)        # MST all year: 13:00 UTC = 6am
    e.next_run_at = six_am_phoenix
    db.commit()
    r = campaigns.run_due(db, now=six_am_phoenix)
    assert r["deferred"] == 1 and e.status == "active"
    assert e.next_run_at == datetime(2026, 7, 14, 15, 0)  # 8:00am Phoenix
    assert campaigns.run_due(db, now=e.next_run_at)["sent"] == 1


def test_email_waits_for_monday(db, mailbox, email_lead):
    mailbox()
    lead = email_lead()
    e = _enrolled(db, lead, "email")
    saturday = datetime(2026, 3, 14, 17, 0)
    e.next_run_at = saturday
    db.commit()
    assert campaigns.run_due(db, now=saturday)["deferred"] == 1
    assert e.next_run_at.weekday() == 0  # Monday


def test_landline_skips_sms_step(db, make_line):
    make_line()
    lead = Lead(name="Land Line", phone="+12145551203")  # mock carrier: last two digits < 08 -> landline
    db.add(lead)
    db.commit()
    e = _enrolled(db, lead, "sms")
    r = campaigns.run_due(db, now=NOON)
    assert r["skipped"] == 1 and lead.phone_type == "landline" and e.current_step == 1
    assert db.query(Message).filter_by(lead_id=lead.id).count() == 0


def test_opted_out_lead_is_stopped(db, make_line):
    make_line()
    lead = Lead(name="Ann Lee", phone="+12145551234")
    db.add(lead)
    db.commit()
    e = _enrolled(db, lead, "sms")
    compliance.suppress(db, lead.phone, "sms", "dnc")
    db.commit()
    assert campaigns.run_due(db, now=NOON)["stopped"] == 1 and e.stop_reason == "suppressed (dnc)"


def test_no_capacity_defers_to_later(db, mailbox, email_lead):
    mailbox(cap=1)
    a, b = email_lead("a1@gmail.com"), email_lead("b1@gmail.com", phone="+12145550001")
    ea, eb = _enrolled(db, a), _enrolled(db, b)
    r = campaigns.run_due(db, now=NOON)
    assert r["sent"] == 1 and r["deferred"] == 1
    waiting = ea if ea.current_step == 0 else eb
    assert waiting.next_run_at > NOON and "capacity" in waiting.last_note


def test_api_campaign_flow(db, make_line):
    make_line()
    db.add(Lead(name="Ann Lee", phone="+12145551234"))
    db.commit()
    with TestClient(app) as c:
        bad = {"name": "x", "steps": [{"channel": "sms", "body": "hi {first_name}"}]}
        assert any(i["code"] == "opt_out" for i in c.post("/api/campaigns/lint", json=bad).json()[0]["issues"])
        cid = c.post("/api/campaigns", json=bad).json()["id"]
        assert c.post(f"/api/campaigns/{cid}/activate").status_code == 422
        good = {"name": "y", "steps": [{"channel": "sms", "body": "Hi {first_name}! Reply STOP to opt out."}]}
        cid = c.post("/api/campaigns", json=good).json()["id"]
        assert c.post(f"/api/campaigns/{cid}/activate").json()["status"] == "active"
        assert c.post(f"/api/campaigns/{cid}/enroll", json={"count": 10}).json()["enrolled"] == 1
    assert db.query(Campaign).count() == 2


# ---------------------------------------------------------------- domain health

def test_blacklist_interpretation():
    zone = {"bad.com.dbl.spamhaus.org": ["127.0.1.2"], "x.com.dbl.spamhaus.org": ["127.255.255.254"]}
    resolve = lambda name: zone.get(name, [])  # noqa: E731
    assert domain_health.check_blacklists("bad.com", resolve)["dbl.spamhaus.org"] is True
    assert domain_health.check_blacklists("x.com", resolve)["dbl.spamhaus.org"] is None  # refused != listed
    assert domain_health.check_blacklists("ok.com", resolve) == {z: False for z in domain_health.DNSBLS}


def test_blacklisted_domain_is_paused(db, mailbox):
    mailbox()
    d = db.query(Domain).one()
    d.blacklists = '{"dbl.spamhaus.org": true}'
    assert domain_health.evaluate(db, d)["status"] == "paused" and "dbl.spamhaus.org" in d.status_reason


def test_enrolling_inside_the_window_sends_immediately(db, make_line):
    make_line()
    lead = Lead(name="Ann Lee", phone="+12145551234")
    db.add(lead)
    db.commit()
    e = _enrolled(db, lead, "sms")
    assert e.next_run_at == NOON and campaigns.run_due(db, now=NOON)["sent"] == 1


def test_mock_reputation_survives_restart():
    from app.providers.mock import MockCarrier
    a, b = MockCarrier(seed=1), MockCarrier(seed=2)
    assert a.reputation("+12145550123") == b.reputation("+12145550123")
    assert a.reputation("+13055550113") == 0.35  # recycled-number suffix


def test_mailbox_pacing_defers_second_email(db, mailbox, email_lead):
    mailbox()
    a, b = email_lead("a2@gmail.com"), email_lead("b2@gmail.com", phone="+12145550002")
    ea, eb = _enrolled(db, a), _enrolled(db, b)
    r = campaigns.run_due(db, now=NOON)
    assert r["sent"] == 1 and r["reasons"] == {"mailbox pacing": 1}
    waiting = ea if ea.current_step == 0 else eb
    # retry after the 6-minute gap, spread over the following ~45 minutes
    assert NOON + timedelta(minutes=6) <= waiting.next_run_at <= NOON + timedelta(minutes=52)
    assert campaigns.run_due(db, now=waiting.next_run_at)["sent"] == 1


def test_line_pacing_and_human_replies_skip_it(db, make_line):
    make_line()
    a = Lead(name="A", phone="+12145551234")
    b = Lead(name="B", phone="+12145551299")
    db.add_all([a, b])
    db.commit()
    sms.send(db, a, "Hi. Reply STOP to opt out.", now=NOON)
    with pytest.raises(sms.LineBusy):
        sms.send(db, b, "Hi. Reply STOP to opt out.", now=NOON + timedelta(seconds=5))
    assert sms.send(db, b, "Hi", now=NOON + timedelta(seconds=5), automated=False).status != "blocked"
    assert sms.send(db, b, "Hi", now=NOON + timedelta(seconds=25)).status != "blocked"


def test_jitter_spreads_but_stays_in_window(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "send_jitter_minutes", 120)
    lead = Lead(name="A", phone="+12145551234")
    saturday_9pm = datetime(2026, 3, 14, 2, 0)  # Fri 9pm Dallas
    times = {campaigns.schedule(lead, saturday_9pm, "sms") for _ in range(20)}
    assert len(times) > 5
    assert all(compliance.within_contact_window(lead, t) for t in times)


def test_followup_obeys_pacing_and_caps_of_its_thread_mailbox(db, mailbox, email_lead):
    mb = mailbox(cap=2)
    lead, other = email_lead(), email_lead("x@gmail.com", phone=None)
    e1, e2 = _enrolled(db, lead), _enrolled(db, other)
    first = email_sender.send(db, lead, "Hi", "one", now=NOON, enrollment_id=e1.id)
    with pytest.raises(email_sender.MailboxBusy):  # same mailbox, 1 minute later
        email_sender.send(db, lead, "Hi", "two", now=NOON + timedelta(minutes=1), reply_to=first, enrollment_id=e1.id)
    email_sender.send(db, other, "Hi", "b", now=NOON + timedelta(minutes=7), enrollment_id=e2.id)  # cap of 2 used up
    with pytest.raises(email_sender.NoMailboxAvailable):
        email_sender.send(db, lead, "Hi", "two", now=NOON + timedelta(minutes=20), reply_to=first, enrollment_id=e1.id)
    assert mb.daily_cold_cap == 2


def test_followup_moves_to_new_thread_when_its_mailbox_is_paused(db, mailbox, email_lead):
    old = mailbox("sam@one.com")
    mailbox("sam@two.com")
    lead = email_lead()
    first = email_sender.send(db, lead, "About {address}", "one", now=NOON)
    assert first.mailbox_id == old.id
    old.status, old.status_reason = "paused", "bounce rate"
    db.commit()
    second = email_sender.send(db, lead, "About {address}", "two", now=NOON + timedelta(hours=1), reply_to=first)
    assert second.mailbox_id != old.id and second.in_reply_to is None and not second.subject.startswith("Re:")


def test_human_reply_uses_thread_mailbox_without_pacing(db, mailbox, email_lead):
    mb = mailbox(cap=1)
    lead = email_lead()
    first = email_sender.send(db, lead, "Hi", "one", now=NOON, enrollment_id=_enrolled(db, lead).id)
    reply = email_sender.send(db, lead, "x", "Sure, call me at 3?", now=NOON, automated=False, reply_to=first)
    assert reply.status == "sent" and reply.mailbox_id == mb.id and reply.subject == "Re: Hi"
