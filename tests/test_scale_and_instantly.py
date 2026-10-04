"""Scale planner math and the Instantly integration."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Enrollment, Lead, Message
from app.providers import instantly
from app.services import campaigns, compliance, scale_planner


# ---------------------------------------------------------------- planner

def test_three_million_a_month_capacity():
    c = scale_planner.plan()["capacity"]
    assert c["daily_emails"] == 136_364                 # 3M / 22 weekdays
    assert c["active_inboxes"] == 5_455                 # / 25 per inbox
    assert c["total_inboxes"] == 6_819                  # + 25% reserve
    assert c["domains"] == 2_273                        # / 3 per domain
    assert c["new_leads_per_month"] == 1_000_000        # 3 emails per lead
    assert c["replies_per_month"] == 20_000 and c["interested_per_month"] == 5_000


def test_stack_costs_and_ordering():
    r = scale_planner.plan()
    by = {s["key"]: s for s in r["stacks"]}
    hybrid = by["hybrid"]
    # platform: 6 x Instantly Light Speed; inboxes: 60% reseller at $2.50 + 40% Mailforge at $2
    assert hybrid["components"]["platform"] == pytest.approx(6 * 286.30)
    assert hybrid["components"]["inboxes"] == pytest.approx(4091 * 2.50 + 2728 * 2.00)
    assert hybrid["per_1k"] == pytest.approx(hybrid["total"] / 3000, abs=0.01)
    assert r["stacks"][-1]["key"] == "inhouse" and not by["inhouse"]["warmup_included"]
    assert by["instantly_google_direct"]["total"] > 2 * by["instantly_mailforge"]["total"]
    assert [s["total"] for s in r["stacks"]] == sorted(s["total"] for s in r["stacks"])


def test_price_override_and_sensitivity():
    base = scale_planner.plan()
    cheaper = scale_planner.plan(price_overrides={"mailforge_inbox": 1.0})
    key = lambda r: next(s for s in r["stacks"] if s["key"] == "instantly_mailforge")["total"]  # noqa: E731
    assert key(base) - key(cheaper) == pytest.approx(6819 * 1.0)
    costs = [x["monthly"] for x in base["sensitivity"]]
    assert costs == sorted(costs, reverse=True)  # more sends per inbox -> fewer inboxes -> cheaper


def test_planner_api_validates():
    with TestClient(app) as c:
        assert c.post("/api/planner", json={"inputs": {"monthly_emails": 500000}}).json()["capacity"]["daily_emails"] == 22728
        assert c.post("/api/planner", json={"inputs": {"per_inbox_per_day": 0}}).status_code == 422


# ---------------------------------------------------------------- Instantly

def _lead(db, email, phone="+12145551234"):
    lead = Lead(name="Maria Lee", email=email, phone=phone, property_address="12 Oak St, Dallas, TX")
    db.add(lead)
    db.commit()
    return lead


def test_push_sends_only_clean_leads(db):
    sent = []
    transport = httpx.MockTransport(lambda req: (sent.append((req.url.path, json.loads(req.content),
                                                               req.headers["Authorization"])),
                                                  httpx.Response(200, json={"id": "x"}))[1])
    client = instantly.InstantlyClient("key123", transport=transport)
    ok = _lead(db, "maria@gmail.com")
    opted_out = _lead(db, "gone@gmail.com", phone="+12145551299")
    compliance.suppress(db, opted_out.email, "email", "opt_out")
    typo = _lead(db, "x@gmial.com", phone="+12145551288")
    db.commit()
    res = instantly.push_leads(db, client, "camp-uuid", [ok, opted_out, typo])
    assert res.pushed == 1 and res.skipped == {"suppressed": 1, "invalid email": 1}
    path, body, auth = sent[0]
    assert path == "/api/v2/leads" and auth == "Bearer key123"
    assert body["campaign"] == "camp-uuid" and body["email"] == "maria@gmail.com" and body["first_name"] == "Maria"
    assert body["skip_if_in_workspace"] is True and body["custom_variables"]["address"] == "12 Oak St, Dallas, TX"


def _sms_enrollment(db, lead):
    c = campaigns.create(db, "sms", [{"channel": "sms", "delay_days": 0, "body": "Hi. Reply STOP to opt out."}])
    campaigns.activate(db, c)
    campaigns.enroll(db, c, [lead.id])
    return db.query(Enrollment).filter_by(lead_id=lead.id).one()


def test_webhook_reply_stops_sms_sequence_and_lands_in_inbox(db):
    lead = _lead(db, "maria@gmail.com")
    e = _sms_enrollment(db, lead)
    r = instantly.handle_webhook(db, {"event_type": "reply_received", "lead_email": "Maria@Gmail.com",
                                      "email_account": "sam@acme.com", "reply_text": "What's your offer?"})
    db.refresh(e)
    assert r["handled"] and e.status == "replied"
    msg = db.query(Message).filter_by(direction="inbound").one()
    assert msg.kind == "reply" and msg.lead_id == lead.id and msg.body == "What's your offer?"


def test_webhook_bounce_unsubscribe_and_auto_reply(db):
    a, b, c = _lead(db, "a@gmail.com"), _lead(db, "b@gmail.com", "+12145550002"), _lead(db, "c@gmail.com", "+12145550003")
    ea = _sms_enrollment(db, c)
    instantly.handle_webhook(db, {"event_type": "email_bounced", "lead_email": a.email})
    instantly.handle_webhook(db, {"event_type": "lead_unsubscribed", "lead_email": b.email})
    instantly.handle_webhook(db, {"event_type": "auto_reply_received", "lead_email": c.email, "reply_text": "OOO"})
    assert compliance.is_suppressed(db, a.email, "email").reason == "bounce" and a.email_status == "invalid"
    assert compliance.is_suppressed(db, b.email, "email").reason == "unsubscribe"
    db.refresh(ea)
    assert ea.status == "active"  # out-of-office doesn't end anything
    assert instantly.handle_webhook(db, {"event_type": "email_opened", "lead_email": c.email})["handled"] is False


def test_webhook_endpoint_requires_secret(db, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    _lead(db, "a@gmail.com")
    payload = {"event_type": "email_bounced", "lead_email": "a@gmail.com"}
    with TestClient(app) as c:
        assert c.post("/webhooks/instantly", json=payload).status_code == 403
        assert c.post("/webhooks/instantly?secret=s3cret", json=payload).json()["action"] == "suppressed"
