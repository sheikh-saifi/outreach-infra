"""Simulated carrier. Each number gets a hidden 'true reputation' that degrades with volume,
so the health monitor has a realistic signal to detect without spending money on real lines."""

import random
import uuid

from app.providers.base import CallResult, ProvisionedNumber, SendResult


class MockCarrier:
    name = "mock"

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        self._reputation: dict[str, float] = {}  # e164 -> 0..1 (1 = pristine)
        self._volume: dict[str, int] = {}

    def _rep(self, e164: str) -> float:
        if e164 not in self._reputation:
            # Most lines start healthy; the odd one is a "recycled" number with some baggage.
            self._reputation[e164] = self._rng.choice([0.99, 0.98, 0.97, 0.96, 0.95, 0.93, 0.85])
        return self._reputation[e164]

    def reputation(self, e164: str) -> float:
        return self._rep(e164)

    def set_reputation(self, e164: str, value: float) -> None:
        self._reputation[e164] = value

    def buy_number(self, area_code: str) -> ProvisionedNumber:
        e164 = f"+1{area_code}{self._rng.randint(2000000, 9999999)}"
        return ProvisionedNumber(e164=e164, provider_sid=f"PN{uuid.uuid4().hex[:30]}")

    def release_number(self, provider_sid: str) -> None:
        return None

    def send_sms(self, from_e164: str, to_e164: str, body: str) -> SendResult:
        rep = self._rep(from_e164)
        self._volume[from_e164] = self._volume.get(from_e164, 0) + 1
        # Heavy senders slowly burn their reputation, like real carrier filtering.
        if self._volume[from_e164] % 50 == 0:
            self._reputation[from_e164] = max(0.2, rep - 0.03)

        sid = f"SM{uuid.uuid4().hex[:30]}"
        r = self._rng.random()
        if r < 0.03:
            return SendResult(sid, "failed", self._rng.choice(["30003", "30005", "30006"]))
        if r < 0.03 + (1 - rep) * 0.6:
            return SendResult(sid, "filtered", "30007")
        return SendResult(sid, "delivered")

    def place_call(self, from_e164: str, to_e164: str) -> CallResult:
        rep = self._rep(from_e164)
        sid = f"CA{uuid.uuid4().hex[:30]}"
        r = self._rng.random()
        answer_p = 0.12 + 0.18 * rep  # "Spam Likely" labels crush answer rates
        if r < answer_p:
            return CallResult(sid, "answered", self._rng.randint(15, 300))
        if r < answer_p + 0.25:
            return CallResult(sid, "voicemail", self._rng.randint(20, 40))
        if r < answer_p + 0.30:
            return CallResult(sid, "busy")
        if r < answer_p + 0.33:
            return CallResult(sid, "failed")
        return CallResult(sid, "no_answer")

    def reputation_lookup(self, e164: str) -> str:
        rep = self._rep(e164)
        if rep < 0.6:
            return "spam_likely"
        return "clean"
