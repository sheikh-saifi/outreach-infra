from datetime import date, timedelta

from app.models import Domain, Mailbox, MailboxDailyStat
from app.services import warmup
from app.services.email_auth import parse_dmarc, parse_spf


def test_spf_parsing():
    assert parse_spf(["v=spf1 include:_spf.google.com ~all"])["ok"]
    assert "No SPF" in parse_spf(["google-site-verification=abc"])["issues"][0]
    assert not parse_spf(["v=spf1 ~all", "v=spf1 -all"])["ok"]
    assert "+all" in parse_spf(["v=spf1 +all"])["issues"][0]
    many = "v=spf1 " + " ".join(f"include:s{i}.example.com" for i in range(11)) + " ~all"
    assert "at most 10" in parse_spf([many])["issues"][0]


def test_dmarc_parsing():
    ok = parse_dmarc(["v=DMARC1; p=quarantine; rua=mailto:d@example.com"])
    assert ok["ok"] and ok["policy"] == "quarantine"
    no_rua = parse_dmarc(["v=DMARC1; p=none"])
    assert not no_rua["ok"] and "rua" in no_rua["issues"][0]
    assert parse_dmarc([])["policy"] is None


def test_ramp_is_monotonic_and_bounded():
    vols = [warmup.planned_volume(d) for d in range(40)]
    assert vols[0] == 5 and vols[-1] == 40
    assert all(a <= b for a, b in zip(vols, vols[1:]))


def _mailbox(db, started):
    d = Domain(name="example.com")
    db.add(d)
    db.commit()
    mb = Mailbox(address="sam@example.com", domain_id=d.id, warmup_started=started)
    db.add(mb)
    db.commit()
    return mb


def _stat(db, mb, day, sent, bounces=0, complaints=0):
    db.add(MailboxDailyStat(mailbox_id=mb.id, day=day, sent=sent, bounces=bounces, complaints=complaints))
    db.commit()


def test_one_early_bounce_does_not_pause(db):
    today = date(2026, 3, 10)
    mb = _mailbox(db, today - timedelta(days=3))
    for i in range(3):
        _stat(db, mb, today - timedelta(days=i + 1), sent=8, bounces=1 if i == 0 else 0)
    plan = warmup.evaluate(db, mb, today)
    assert plan["status"] == "warming" and plan["quota"] > 0


def test_sustained_bounces_pause_and_resume_steps_back(db):
    today = date(2026, 3, 10)
    mb = _mailbox(db, today - timedelta(days=20))
    for i in range(7):
        _stat(db, mb, today - timedelta(days=i), sent=30, bounces=2)  # ~6.7%
    plan = warmup.evaluate(db, mb, today)
    assert plan["status"] == "paused" and plan["quota"] == 0

    warmup.resume(mb, today)
    assert (today - mb.warmup_started).days == 13 and mb.status == "warming"


def test_graduates_after_ramp(db):
    today = date(2026, 3, 10)
    mb = _mailbox(db, today - timedelta(days=30))
    assert warmup.evaluate(db, mb, today)["status"] == "active"
