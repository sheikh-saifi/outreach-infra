"""Email sending providers. Same idea as the telephony providers: the sending engine builds a
standards-compliant message and hands it to whichever backend is configured.

Production options behind this interface: Gmail API / Microsoft Graph with per-mailbox OAuth
(best deliverability for cold email, since mail leaves through Google/Microsoft), or SMTP.
"""

import random
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol


@dataclass
class EmailResult:
    status: str               # sent | bounced | failed
    error: str | None = None  # SMTP reply, e.g. "550 5.1.1 user unknown"


class EmailProvider(Protocol):
    name: str

    def send(self, msg: EmailMessage) -> EmailResult: ...


class MockEmailProvider:
    """Simulates the receiving side: dead addresses hard-bounce at SMTP time, a small share of
    good addresses fail transiently. Most bounces in real life arrive later as DSN emails,
    which the inbound pipeline handles."""
    name = "mock"

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        self.outbox: list[EmailMessage] = []

    def send(self, msg: EmailMessage) -> EmailResult:
        self.outbox.append(msg)
        local = msg["To"].addresses[0].username.lower() if msg["To"] else ""
        if local.startswith(("old.", "bad.")):  # "dead." addresses bounce later, via a DSN
            return EmailResult("bounced", "550 5.1.1 The email account that you tried to reach does not exist")
        if self._rng.random() < 0.005:
            return EmailResult("failed", "421 4.7.0 Try again later")
        return EmailResult("sent")


class SMTPEmailProvider:
    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host, self.port, self.username, self.password = host, port, username, password

    def send(self, msg: EmailMessage) -> EmailResult:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls()
                if self.username:
                    s.login(self.username, self.password)
                refused = s.send_message(msg)
            if refused:
                code, text = next(iter(refused.values()))
                return EmailResult("bounced" if 500 <= code < 600 else "failed", f"{code} {text.decode(errors='replace')}")
            return EmailResult("sent")
        except smtplib.SMTPRecipientsRefused as e:
            code, text = next(iter(e.recipients.values()))
            return EmailResult("bounced" if 500 <= code < 600 else "failed", f"{code} {text.decode(errors='replace')}")
        except (smtplib.SMTPException, OSError) as e:
            return EmailResult("failed", str(e)[:200])
