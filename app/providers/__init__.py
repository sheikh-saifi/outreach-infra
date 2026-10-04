from functools import lru_cache

from app.config import settings
from app.providers.base import TelephonyProvider
from app.providers.email import EmailProvider, MockEmailProvider
from app.providers.mock import MockCarrier


@lru_cache
def get_carrier() -> TelephonyProvider:
    if settings.telephony_provider == "twilio":
        from app.providers.twilio import TwilioCarrier
        return TwilioCarrier(settings.twilio_account_sid, settings.twilio_auth_token)
    return MockCarrier(seed=42)


@lru_cache
def get_email_provider() -> EmailProvider:
    if settings.email_provider == "smtp":
        from app.providers.email import SMTPEmailProvider
        return SMTPEmailProvider(settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password)
    return MockEmailProvider(seed=7)
