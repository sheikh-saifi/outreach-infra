"""Regression tests for the bugs found in the second review pass."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import CallLog, Lead, Message
from app.providers.base import CallResult
from app.services import compliance, dialer, line_health, provisioning, sms
from app.services.email_auth import count_spf_lookups, parse_spf

NOON = datetime(2026, 3, 10, 17, 0)


# ---------------------------------------------------------------- keywords

@pytest.mark.parametrize("body,expected", [
    ("STOP", "stop"), ("stop.", "stop"), ("Please stop texting me", "stop"), ("don't text me again", "stop"),
    ("Do not contact me", "stop"), ("take me off your list", "stop"), ("opt-out", "stop"),
    ("start", "start"), ("Wrong number", "wrong_number"), ("I don't own that house", "wrong_number"),
    ("Yes", None), ("yes, what's your offer?", None), ("How much?", None), ("Not interested", None),
])
def test_classify_keyword(body, expected):
    assert compliance.classify_keyword(body) == expected


def test_yes_does_not_trigger_resubscribe(db, make_lead, make_line):
    line, lead = make_line(), make_lead()
    res = sms.handle_inbound(db, lead.phone, line.e164, "Yes", now=NOON)
    assert res["keyword"] is None and res["auto_reply"] is None


def test_start_without_prior_optout_sends_nothing(db, make_lead, make_line):
    line, lead = make_line(), make_lead()
    assert sms.handle_inbound(db, lead.phone, line.e164, "START", now=NOON)["auto_reply"] is None


def test_stop_confirmed_once_and_logged(db, make_lead, make_line):
    line, lead = make_line(), make_lead()
    assert sms.handle_inbound(db, lead.phone, line.e164, "STOP", now=NOON)["auto_reply"]
    assert sms.handle_inbound(db, lead.phone, line.e164, "STOP", now=NOON)["auto_reply"] is None
    autos = db.query(Message).filter_by(is_auto_reply=True).all()
    assert len(autos) == 1 and autos[0].to_addr == lead.phone


def test_wrong_number_suppresses_and_start_cannot_undo(db, make_lead, make_line):
    line, lead = make_line(), make_lead()
    sms.handle_inbound(db, lead.phone, line.e164, "wrong number", now=NOON)
    assert compliance.is_suppressed(db, lead.phone, "voice").reason == "wrong_number"
    sms.handle_inbound(db, lead.phone, line.e164, "start", now=NOON)
    assert compliance.is_suppressed(db, lead.phone, "sms")


# ---------------------------------------------------------------- conversation handling

def test_no_automated_followup_after_reply_but_human_can_answer(db, make_lead, make_line):
    line, lead = make_line(), make_lead()
    sms.send(db, lead, "Hi {first_name}", now=NOON)
    sms.handle_inbound(db, lead.phone, line.e164, "How much?", now=NOON + timedelta(minutes=5))

    auto = sms.send(db, lead, "Following up", now=NOON + timedelta(hours=1))
    assert auto.status == "blocked" and "replied" in auto.block_reason
    human = sms.send(db, lead, "Around $250k, want to talk?", now=NOON + timedelta(hours=1), automated=False)
    assert human.status != "blocked"


def test_reply_threads_to_the_property_we_texted_about(db, make_line):
    line = make_line()
    a = Lead(name="Cy Owner", phone="+12145553333", property_address="1 A St")
    b = Lead(name="Cy Owner", phone="+12145553333", property_address="2 B St")
    db.add_all([a, b])
    db.commit()
    sms.send(db, b, "About {address}", now=NOON)
    assert sms.handle_inbound(db, b.phone, line.e164, "Which one?", now=NOON)["lead_id"] == b.id


def test_frequency_cap_counts_texts_and_calls_per_phone(db, make_line):
    make_line()
    a = Lead(name="Owner", phone="+12145553333", property_address="1 A St")
    b = Lead(name="Owner", phone="+12145553333", property_address="2 B St")
    db.add_all([a, b])
    db.commit()
    sms.send(db, a, "one", now=NOON)
    sms.send(db, b, "two", now=NOON + timedelta(minutes=1))
    db.add(CallLog(session_id="s", number_id=1, lead_id=a.id, outcome="no_answer", started_at=NOON))
    db.commit()
    third = sms.send(db, b, "three", now=NOON + timedelta(minutes=2))
    assert third.status == "blocked" and "frequency cap" in third.block_reason
    # a day later the window has moved on
    assert compliance.can_contact(db, b, "sms", (NOON + timedelta(hours=25)).replace(tzinfo=timezone.utc))[0]


def test_phoenix_has_no_dst(db, make_lead):
    lead = make_lead("+16025551234")
    july_1430 = datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)  # 8:30am Denver, 7:30am Phoenix
    assert not compliance.can_contact(db, lead, "sms", july_1430)[0]


# ---------------------------------------------------------------- line lifecycle

def _filtered_traffic(db, line, at, filtered=12, n=100):
    for i in range(n):
        db.add(Message(channel="sms", direction="outbound", from_addr=line.e164, to_addr=f"+1214556{i:04d}",
                       body="x", status="filtered" if i < filtered else "delivered",
                       error_code="30007" if i < filtered else None, number_id=line.id, created_at=at))
    db.commit()


def test_rested_line_recovers_instead_of_bouncing_back_into_rest(db, make_line):
    line = make_line()
    line.spam_label = "clean"
    _filtered_traffic(db, line, NOON - timedelta(hours=1))
    assert line_health.evaluate(db, line, NOON)["action"] == "rested"
    assert line_health.evaluate(db, line, NOON + timedelta(hours=49))["action"] == "reactivated"
    # judged on fresh traffic only, so the next sweep leaves it alone
    assert line_health.evaluate(db, line, NOON + timedelta(hours=50))["action"] == "none"
    assert line.status == "active"


def test_rest_extended_while_still_labelled_spam(db, make_line, carrier):
    line = make_line()
    carrier.set_reputation(line.e164, 0.3)
    line.status, line.rested_until = "resting", NOON
    assert line_health.evaluate(db, line, NOON + timedelta(hours=1))["action"] == "rest_extended"
    assert line.status == "resting" and line.rested_until > NOON + timedelta(hours=1)


def test_retired_line_cannot_be_reactivated(db, make_line):
    line = make_line(status="retired")
    with TestClient(app) as c:
        assert c.post(f"/api/lines/{line.id}/reactivate").status_code == 409


def test_quarantined_line_cannot_be_rested_into_auto_reactivation(db, make_line):
    line = make_line(status="quarantined")
    with TestClient(app) as c:
        assert c.post(f"/api/lines/{line.id}/rest").status_code == 409


def test_replace_moves_conversations_to_new_line(db, make_line, make_lead):
    old = make_line()
    lead = make_lead(sticky_number_id=old.id)
    new = provisioning.replace(db, old)
    db.refresh(lead)
    assert lead.sticky_number_id == new.id


def test_status_callbacks_never_go_backwards(db, make_line, make_lead):
    line, lead = make_line(), make_lead()
    db.add(Message(channel="sms", direction="outbound", from_addr=line.e164, to_addr=lead.phone, body="x",
                   status="sent", provider_sid="SM1", number_id=line.id))
    db.commit()
    with TestClient(app) as c:
        c.post("/webhooks/sms/status", data={"MessageSid": "SM1", "MessageStatus": "delivered"})
        r = c.post("/webhooks/sms/status", data={"MessageSid": "SM1", "MessageStatus": "sent"})
    assert r.json()["status"] == "delivered"


# ---------------------------------------------------------------- dialer

class AlwaysAnswers:
    name = "fake"

    def place_call(self, from_e164, to_e164):
        return CallResult("CA1", "answered", 60)


def test_abandon_rate_uses_30_day_history(db, make_line, make_lead, monkeypatch):
    monkeypatch.setattr(dialer, "get_carrier", lambda: AlwaysAnswers())
    line = make_line()
    # earlier this month: 10 abandoned out of 100 answered = 10%, far over the limit
    for i in range(100):
        db.add(CallLog(session_id="old", number_id=line.id, lead_id=1,
                       outcome="abandoned" if i < 10 else "answered", started_at=NOON - timedelta(days=5)))
    db.commit()
    leads = [make_lead(f"+1214557{i:04d}") for i in range(3)]
    r = dialer.run_session(db, [l.id for l in leads], lines=3, now=NOON)
    assert r["lines_end"] < 3 and r["abandon_rate_30d"] > 0.03


def test_per_line_daily_call_cap(db, make_line, make_lead, monkeypatch):
    monkeypatch.setattr(dialer, "get_carrier", lambda: AlwaysAnswers())
    line = make_line()
    line.daily_call_cap = 2
    db.commit()
    leads = [make_lead(f"+1214558{i:04d}") for i in range(4)]
    r = dialer.run_session(db, [l.id for l in leads], lines=1, now=NOON)
    assert r["dialed"] == 2 and r["blocked"] == 2


# ---------------------------------------------------------------- SPF

def test_spf_counts_nested_includes():
    zone = {
        "a.example": ["v=spf1 include:b.example include:c.example ~all"],
        "b.example": ["v=spf1 " + " ".join(f"ip4:10.0.0.{i}" for i in range(3)) + " include:d.example ~all"],
        "c.example": ["v=spf1 a mx include:d.example ~all"],
        "d.example": ["v=spf1 " + " ".join(f"include:e{i}.example" for i in range(6)) + " -all"],
    }
    resolve = lambda name: zone.get(name, [])  # noqa: E731
    top = zone["a.example"][0]
    assert count_spf_lookups(top) == 2  # what the old, top-level-only check saw
    # a: 2 includes; b: +1 (d); d: +6; c: a, mx, include = +3 (d already counted)
    assert count_spf_lookups(top, resolve) == 12
    assert "at most 10" in parse_spf([top], resolve)["issues"][0]
