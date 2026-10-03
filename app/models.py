from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- phone lines

class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    e164: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    area_code: Mapped[str] = mapped_column(String(3), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    campaign: Mapped[str | None] = mapped_column(String(64))
    # active | resting | quarantined | retired
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(255))
    rested_until: Mapped[datetime | None] = mapped_column(DateTime)
    daily_cap: Mapped[int] = mapped_column(Integer, default=150)
    health_score: Mapped[float] = mapped_column(Float, default=100.0)
    spam_label: Mapped[str] = mapped_column(String(32), default="unknown")  # clean | spam_likely | unknown
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpamCheck(Base):
    __tablename__ = "spam_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    number_id: Mapped[int] = mapped_column(ForeignKey("phone_numbers.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # e.g. hiya, tns, first_orion
    label: Mapped[str] = mapped_column(String(32))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HealthSnapshot(Base):
    """Time series of line health so trends (not just current state) are visible."""
    __tablename__ = "health_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    number_id: Mapped[int] = mapped_column(ForeignKey("phone_numbers.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    delivery_rate: Mapped[float] = mapped_column(Float)
    filter_rate: Mapped[float] = mapped_column(Float)
    optout_rate: Mapped[float] = mapped_column(Float)
    reply_rate: Mapped[float] = mapped_column(Float)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- leads / compliance

class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    property_address: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Number this lead was first contacted from; reused so the conversation stays on one thread.
    sticky_number_id: Mapped[int | None] = mapped_column(ForeignKey("phone_numbers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Suppression(Base):
    """Opt-outs, DNC registry hits, bounces, complaints. Checked before every send."""
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("value", "channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(255), index=True)  # phone (E.164) or email
    channel: Mapped[str] = mapped_column(String(16))  # sms | voice | email | all
    reason: Mapped[str] = mapped_column(String(64))   # opt_out | dnc | bounce | complaint | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- messages / calls

class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)  # sms | email | imessage
    direction: Mapped[str] = mapped_column(String(8))  # outbound | inbound
    from_addr: Mapped[str] = mapped_column(String(255))
    to_addr: Mapped[str] = mapped_column(String(255), index=True)
    body: Mapped[str] = mapped_column(Text)
    # queued | sent | delivered | failed | filtered | received | blocked
    status: Mapped[str] = mapped_column(String(16), index=True)
    error_code: Mapped[str | None] = mapped_column(String(16))
    block_reason: Mapped[str | None] = mapped_column(String(128))  # why compliance stopped the send
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    number_id: Mapped[int | None] = mapped_column(ForeignKey("phone_numbers.id"), index=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    lead: Mapped[Lead | None] = relationship()


class CallLog(Base):
    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    number_id: Mapped[int] = mapped_column(ForeignKey("phone_numbers.id"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    # answered | no_answer | busy | voicemail | failed | cancelled | blocked
    outcome: Mapped[str] = mapped_column(String(16))
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------- email

class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    has_mx: Mapped[bool] = mapped_column(Boolean, default=False)
    spf_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dkim_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dmarc_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dmarc_policy: Mapped[str | None] = mapped_column(String(16))
    last_checked: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(255), unique=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"))
    # warming | active | paused
    status: Mapped[str] = mapped_column(String(16), default="warming")
    status_reason: Mapped[str | None] = mapped_column(String(255))
    warmup_started: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    domain: Mapped[Domain] = relationship()


class MailboxDailyStat(Base):
    __tablename__ = "mailbox_daily_stats"
    __table_args__ = (UniqueConstraint("mailbox_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    planned: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    bounces: Mapped[int] = mapped_column(Integer, default=0)
    complaints: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    inbox_placement: Mapped[float | None] = mapped_column(Float)  # seed-test result, 0..1
