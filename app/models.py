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
    daily_call_cap: Mapped[int] = mapped_column(Integer, default=100)
    # Health is computed only from traffic after this point; reset when a line comes back from rest
    # so it isn't judged forever on the traffic that got it rested.
    metrics_since: Mapped[datetime | None] = mapped_column(DateTime)
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
    # Number / mailbox this lead was first contacted from; reused so the conversation stays on one thread.
    sticky_number_id: Mapped[int | None] = mapped_column(ForeignKey("phone_numbers.id"))
    sticky_mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"))
    phone_type: Mapped[str | None] = mapped_column(String(16))    # mobile | landline | voip | invalid (cached lookup)
    email_status: Mapped[str | None] = mapped_column(String(16))  # valid | risky | invalid (cached verification)
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
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    # sms: sent | delivered | failed | filtered | received | blocked
    # email: sent | bounced | failed | received | blocked
    status: Mapped[str] = mapped_column(String(16), index=True)
    # Inbound email classification: reply | auto_reply | bounce | complaint. Only "reply" is a human answer.
    kind: Mapped[str | None] = mapped_column(String(16))
    message_id_hdr: Mapped[str | None] = mapped_column(String(255), index=True)  # RFC 5322 Message-ID
    in_reply_to: Mapped[str | None] = mapped_column(String(255))
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id"), index=True)
    error_code: Mapped[str | None] = mapped_column(String(16))
    block_reason: Mapped[str | None] = mapped_column(String(128))  # why compliance stopped the send
    is_auto_reply: Mapped[bool] = mapped_column(Boolean, default=False)  # STOP/START confirmations
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
    # active | paused. A paused domain sends no cold email from any of its mailboxes.
    status: Mapped[str] = mapped_column(String(16), default="active")
    status_reason: Mapped[str | None] = mapped_column(String(255))
    blacklists: Mapped[str | None] = mapped_column(Text)  # JSON: {"dbl.spamhaus.org": true/false/null}
    blacklist_checked: Mapped[datetime | None] = mapped_column(DateTime)


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(255), unique=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"))
    # warming | active | paused
    status: Mapped[str] = mapped_column(String(16), default="warming")
    status_reason: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), default="Sam Carter")
    warmup_started: Mapped[date] = mapped_column(Date)
    daily_cold_cap: Mapped[int] = mapped_column(Integer, default=30)  # cold emails/day once fully warm
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    domain: Mapped[Domain] = relationship()


class MailboxDailyStat(Base):
    __tablename__ = "mailbox_daily_stats"
    __table_args__ = (UniqueConstraint("mailbox_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    planned: Mapped[int] = mapped_column(Integer, default=0)  # warm-up volume planned for the day
    sent: Mapped[int] = mapped_column(Integer, default=0)     # warm-up emails sent
    cold_sent: Mapped[int] = mapped_column(Integer, default=0)  # campaign emails sent
    bounces: Mapped[int] = mapped_column(Integer, default=0)
    complaints: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    inbox_placement: Mapped[float | None] = mapped_column(Float)  # seed-test result, 0..1


# ---------------------------------------------------------------- campaigns / sequences

class Campaign(Base):
    """A multi-step outreach sequence (any mix of SMS and email steps)."""
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | active | paused
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    steps: Mapped[list["CampaignStep"]] = relationship(order_by="CampaignStep.position",
                                                        cascade="all, delete-orphan")


class CampaignStep(Base):
    __tablename__ = "campaign_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(16))  # sms | email
    delay_days: Mapped[int] = mapped_column(Integer, default=0)  # wait after the previous step
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)


class Enrollment(Base):
    """One lead's progress through one campaign. The dispatcher advances it step by step."""
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("campaign_id", "lead_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    # active | replied | completed | stopped
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(128))
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_note: Mapped[str | None] = mapped_column(String(128))  # e.g. "deferred: outside 8am-9pm"
    last_email_message_id: Mapped[int | None] = mapped_column(Integer)  # to thread follow-up emails
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    lead: Mapped[Lead] = relationship()
    campaign: Mapped[Campaign] = relationship()
