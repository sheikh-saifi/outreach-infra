"""Provider interface. Every carrier / email backend is an adapter behind this contract,
so swapping Twilio for Telnyx or Bandwidth (or our own SIP trunk) doesn't touch business logic."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SendResult:
    provider_sid: str
    status: str            # sent | delivered | failed | filtered
    error_code: str | None = None


@dataclass
class ProvisionedNumber:
    e164: str
    provider_sid: str


@dataclass
class CallResult:
    provider_sid: str
    outcome: str           # answered | no_answer | busy | voicemail | failed
    duration_s: int = 0


class TelephonyProvider(Protocol):
    name: str

    def buy_number(self, area_code: str) -> ProvisionedNumber: ...

    def release_number(self, provider_sid: str) -> None: ...

    def send_sms(self, from_e164: str, to_e164: str, body: str) -> SendResult: ...

    def place_call(self, from_e164: str, to_e164: str) -> CallResult: ...

    def reputation_lookup(self, e164: str) -> str:
        """Return 'clean' | 'spam_likely' | 'unknown' from carrier analytics (Hiya/TNS/First Orion)."""
        ...

    def line_type(self, e164: str) -> str:
        """Return 'mobile' | 'landline' | 'voip' | 'invalid'. Texts to landlines are wasted money
        and count as failures, so look up once per lead and cache it."""
        ...


# Carrier error codes (Twilio numbering) that tell us something about the *sending line*
# rather than the recipient. Only these should hurt a line's health score.
LINE_REPUTATION_ERRORS = {
    "30007",  # message filtered by carrier (spam)
    "21610",  # recipient has replied STOP to this sender
}
# Errors that reflect list quality (bad data), not line reputation.
LIST_QUALITY_ERRORS = {
    "30003",  # unreachable handset
    "30005",  # unknown destination
    "30006",  # landline / unreachable carrier
}
