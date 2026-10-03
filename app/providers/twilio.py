"""Twilio adapter over the REST API (no SDK dependency). Same contract as MockCarrier.

Production notes:
- SMS to US numbers requires an approved A2P 10DLC brand + campaign; attach numbers to a
  Messaging Service rather than sending from bare numbers.
- Delivery status arrives asynchronously via StatusCallback webhooks (see routers/webhooks.py);
  the status returned here is only the initial 'queued'/'sent'.
"""

import httpx

from app.providers.base import CallResult, ProvisionedNumber, SendResult

API = "https://api.twilio.com/2010-04-01"


class TwilioCarrier:
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, status_callback_url: str | None = None):
        self._sid = account_sid
        self._client = httpx.Client(auth=(account_sid, auth_token), timeout=15)
        self._status_callback = status_callback_url

    def _url(self, path: str) -> str:
        return f"{API}/Accounts/{self._sid}/{path}"

    def buy_number(self, area_code: str) -> ProvisionedNumber:
        r = self._client.get(
            self._url("AvailablePhoneNumbers/US/Local.json"),
            params={"AreaCode": area_code, "SmsEnabled": "true", "VoiceEnabled": "true", "PageSize": 1},
        )
        r.raise_for_status()
        candidates = r.json().get("available_phone_numbers", [])
        if not candidates:
            raise RuntimeError(f"No numbers available in area code {area_code}")
        r = self._client.post(self._url("IncomingPhoneNumbers.json"),
                              data={"PhoneNumber": candidates[0]["phone_number"]})
        r.raise_for_status()
        body = r.json()
        return ProvisionedNumber(e164=body["phone_number"], provider_sid=body["sid"])

    def release_number(self, provider_sid: str) -> None:
        self._client.delete(self._url(f"IncomingPhoneNumbers/{provider_sid}.json")).raise_for_status()

    def send_sms(self, from_e164: str, to_e164: str, body: str) -> SendResult:
        data = {"From": from_e164, "To": to_e164, "Body": body}
        if self._status_callback:
            data["StatusCallback"] = self._status_callback
        r = self._client.post(self._url("Messages.json"), data=data)
        if r.status_code >= 400:
            err = r.json()
            return SendResult(provider_sid="", status="failed", error_code=str(err.get("code")))
        msg = r.json()
        return SendResult(provider_sid=msg["sid"], status=msg.get("status", "queued"))

    def place_call(self, from_e164: str, to_e164: str) -> CallResult:
        # TwiML would normally bridge to an agent; outcome arrives via status callback.
        r = self._client.post(self._url("Calls.json"), data={
            "From": from_e164, "To": to_e164, "MachineDetection": "Enable",
            "Twiml": "<Response><Pause length='1'/></Response>",
        })
        r.raise_for_status()
        return CallResult(provider_sid=r.json()["sid"], outcome="queued")

    def reputation_lookup(self, e164: str) -> str:
        # Twilio doesn't expose spam labels directly; production would query a
        # reputation vendor (Hiya, TNS, First Orion via a registration service).
        return "unknown"
