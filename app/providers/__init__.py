from functools import lru_cache

from app.config import settings
from app.providers.base import TelephonyProvider
from app.providers.mock import MockCarrier


@lru_cache
def get_carrier() -> TelephonyProvider:
    if settings.telephony_provider == "twilio":
        from app.providers.twilio import TwilioCarrier
        return TwilioCarrier(settings.twilio_account_sid, settings.twilio_auth_token)
    return MockCarrier(seed=42)
