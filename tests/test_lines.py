from datetime import datetime, timedelta

import pytest

from app.models import Message
from app.services import line_health, provisioning, sms
from app.services.line_health import LineMetrics, score

NOON = datetime(2026, 3, 10, 17, 0)


def _traffic(db, line, n=100, filtered=0, failed_landline=0, stops=0, replies=0, at=NOON):
    for i in range(n):
        status, code = "delivered", None
        if i < filtered:
            status, code = "filtered", "30007"
        elif i < filtered + failed_landline:
            status, code = "failed", "30006"
        db.add(Message(channel="sms", direction="outbound", from_addr=line.e164, to_addr=f"+1214555{i:04d}",
                       body="x", status=status, error_code=code, number_id=line.id, created_at=at))
    for i in range(stops + replies):
        db.add(Message(channel="sms", direction="inbound", from_addr=f"+1214555{i:04d}", to_addr=line.e164,
                       body="STOP" if i < stops else "how much?", status="received", number_id=line.id,
                       created_at=at + timedelta(minutes=5)))
    db.commit()


def test_score_formula():
    assert score(LineMetrics(100, 1.0, 0.0, 0.0, 0.0), "clean") == 100
    assert score(LineMetrics(100, 0.9, 0.10, 0.0, 0.0), "clean") == 70
    # a Spam Likely label alone must be enough to rest a line (threshold is < 70)
    assert score(LineMetrics(100, 1.0, 0.0, 0.0, 0.0), "spam_likely") == 65
    # too few messages: rates are ignored, only the spam label counts
    assert score(LineMetrics(5, 0.2, 0.8, 0.0, 0.0), "clean") == 100


def test_landline_failures_do_not_hurt_line(db, make_line):
    line = make_line()
    _traffic(db, line, n=100, failed_landline=30)
    m = line_health.compute_metrics(db, line)
    assert m.sample == 70 and m.delivery_rate == 1.0
    assert line_health.evaluate(db, line, NOON)["action"] == "none"


def test_filtered_line_is_rested_then_reactivated(db, make_line):
    line = make_line()
    _traffic(db, line, n=100, filtered=12)  # 12% filtered -> score 64
    assert line_health.evaluate(db, line, NOON)["action"] == "rested"
    assert line.status == "resting" and "filtered 12%" in line.status_reason

    assert line_health.evaluate(db, line, NOON + timedelta(hours=1))["action"] == "none"
    assert line_health.evaluate(db, line, NOON + timedelta(hours=49))["action"] == "reactivated"
    assert line.status == "active"


def test_burned_line_is_quarantined(db, make_line):
    line = make_line()
    line.spam_label = "spam_likely"
    _traffic(db, line, n=100, filtered=10, stops=4)
    assert line_health.evaluate(db, line, NOON)["action"] == "quarantined"


def test_pick_number_prefers_sticky_then_local_then_healthiest(db, make_line, make_lead):
    remote = make_line("305", score=100)
    local_weak = make_line("214", score=80)
    local_strong = make_line("214", score=95)
    lead = make_lead("+12145551234")
    assert sms.pick_number(db, lead, now=NOON).id == local_strong.id

    lead.sticky_number_id = local_weak.id
    assert sms.pick_number(db, lead, now=NOON).id == local_weak.id

    local_weak.status = local_strong.status = "resting"
    assert sms.pick_number(db, lead, now=NOON).id == remote.id


def test_daily_cap_is_enforced(db, make_line, make_lead):
    line = make_line(cap=3)
    _traffic(db, line, n=3)
    with pytest.raises(sms.NoLineAvailable):
        sms.pick_number(db, make_lead(), now=NOON + timedelta(hours=1))


def test_send_sets_sticky_number(db, make_line, make_lead):
    line = make_line()
    lead = make_lead(name="Maria Lopez", property_address="12 Oak St")
    msg = sms.send(db, lead, "Hi {first_name}, about {address}", now=NOON)
    assert msg.body == "Hi Maria, about 12 Oak St"
    assert lead.sticky_number_id == line.id


def test_replace_retires_and_buys_same_area(db, make_line):
    old = make_line("713")
    new = provisioning.replace(db, old)
    assert old.status == "retired" and new.area_code == "713" and new.status == "active"
