from datetime import datetime, timezone

import pytest

from app.services import compliance, sms

NOON_ET = datetime(2026, 3, 10, 16, 0, tzinfo=timezone.utc)      # 12pm ET, 9am PT
LATE_ET = datetime(2026, 3, 11, 2, 30, tzinfo=timezone.utc)      # 10:30pm ET, 7:30pm PT
EARLY_PT = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)     # 10am ET, 7am PT


def test_normalize_phone():
    assert compliance.normalize_phone("(214) 555-1234") == "+12145551234"
    assert compliance.normalize_phone("1-214-555-1234") == "+12145551234"
    with pytest.raises(ValueError):
        compliance.normalize_phone("12345")


def test_quiet_hours_use_recipient_local_time(db, make_lead):
    dallas = make_lead("+12145551234")
    assert compliance.can_contact(db, dallas, "sms", NOON_ET) == (True, "ok")
    ok, reason = compliance.can_contact(db, dallas, "sms", LATE_ET)  # 9:30pm in Dallas
    assert not ok and "8am-9pm" in reason


def test_unknown_area_code_must_be_ok_in_every_zone(db, make_lead):
    unknown = make_lead("+19995551234")
    afternoon_et = datetime(2026, 3, 10, 19, 0, tzinfo=timezone.utc)  # 3pm ET, 9am Honolulu
    assert compliance.can_contact(db, unknown, "sms", afternoon_et)[0]
    assert not compliance.can_contact(db, unknown, "sms", NOON_ET)[0]   # 6am in Hawaii
    assert not compliance.can_contact(db, unknown, "sms", EARLY_PT)[0]  # 7am on the west coast


def test_explicit_timezone_wins(db, make_lead):
    lead = make_lead("+12145551234", timezone="America/Los_Angeles")
    assert not compliance.can_contact(db, lead, "sms", EARLY_PT)[0]


def test_suppression_blocks_and_sms_optout_covers_voice(db, make_lead):
    lead = make_lead()
    compliance.suppress(db, lead.phone, "sms", "opt_out")
    db.commit()
    assert compliance.can_contact(db, lead, "sms", NOON_ET) == (False, "suppressed (opt_out)")
    assert not compliance.can_contact(db, lead, "voice", NOON_ET)[0]


def test_stop_then_start(db, make_lead, make_line):
    line = make_line()
    lead = make_lead()
    res = sms.handle_inbound(db, lead.phone, line.e164, "Stop.")
    assert res["keyword"] == "stop" and res["auto_reply"]
    assert compliance.is_suppressed(db, lead.phone, "sms")
    assert compliance.is_suppressed(db, lead.phone, "voice")

    sms.handle_inbound(db, lead.phone, line.e164, "START")
    assert not compliance.is_suppressed(db, lead.phone, "sms")


def test_start_does_not_clear_dnc(db, make_lead, make_line):
    line, lead = make_line(), make_lead()
    compliance.suppress(db, lead.phone, "sms", "dnc")
    db.commit()
    sms.handle_inbound(db, lead.phone, line.e164, "start")
    assert compliance.is_suppressed(db, lead.phone, "sms")


def test_blocked_send_is_logged_not_sent(db, make_lead, make_line):
    make_line()
    lead = make_lead()
    msg = sms.send(db, lead, "hi {first_name}", now=LATE_ET)
    assert msg.status == "blocked" and msg.number_id is None and "8am-9pm" in msg.block_reason
