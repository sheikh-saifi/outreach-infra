import base64
import hashlib
import hmac
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.providers.base import CallResult
from app.services import dialer

NOON = datetime(2026, 3, 10, 17, 0)


class AlwaysAnswers:
    name = "fake"

    def place_call(self, from_e164, to_e164):
        return CallResult("CA1", "answered", 60)


def test_multiline_abandon_drops_lines(db, make_line, make_lead, monkeypatch):
    monkeypatch.setattr(dialer, "get_carrier", lambda: AlwaysAnswers())
    make_line()
    leads = [make_lead(f"+1214555{i:04d}") for i in range(9)]
    r = dialer.run_session(db, [l.id for l in leads], lines=3, now=NOON)
    # every call answers, so every multi-line batch abandons: 3 -> 2 -> 1
    assert r["lines_start"] == 3 and r["lines_end"] == 1
    assert r["abandoned"] > 0 and r["answered"] + r["abandoned"] == r["dialed"]


def test_single_line_never_abandons(db, make_line, make_lead, monkeypatch):
    monkeypatch.setattr(dialer, "get_carrier", lambda: AlwaysAnswers())
    make_line()
    leads = [make_lead(f"+1214555{i:04d}") for i in range(5)]
    r = dialer.run_session(db, [l.id for l in leads], lines=1, now=NOON)
    assert r["abandoned"] == 0 and r["answered"] == 5


def _sign(url, form):
    payload = url + "".join(k + form[k] for k in sorted(form))
    return base64.b64encode(hmac.new(settings.twilio_auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def test_twilio_webhook_signature(db, make_line, make_lead, carrier, monkeypatch):
    from app.services import sms
    monkeypatch.setattr(sms, "get_carrier", lambda: carrier)  # keep the STOP auto-reply off the network
    monkeypatch.setattr(settings, "telephony_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_auth_token", "secret")
    line, lead = make_line(), make_lead()
    form = {"From": lead.phone, "To": line.e164, "Body": "STOP"}
    url = "http://testserver/webhooks/sms/inbound"
    with TestClient(app) as c:
        assert c.post(url, data=form, headers={"X-Twilio-Signature": "forged"}).status_code == 403
        r = c.post(url, data=form, headers={"X-Twilio-Signature": _sign(url, form)})
        assert r.status_code == 200 and r.json()["keyword"] == "stop"
