"""Instantly integration: "buy the sending layer, build the brain".

At millions of emails a month, inbox rotation, warm-up and sending are cheaper to rent from
Instantly than to build (see services/scale_planner.py). This system stays the brain:
lead hygiene, compliance across SMS + email, multi-channel sequencing, reply routing.

  outbound: our compliance gate + verification decide WHO may be emailed, then we push those
            leads into an Instantly campaign  (POST /api/v2/leads)
  inbound:  Instantly webhooks (reply, bounce, unsubscribe, out-of-office...) come back to
            /webhooks/instantly and update suppression lists and stop sequences on every channel

API reference: https://developer.instantly.ai (lead creation, webhook events).
"""

from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Lead, Message, utcnow
from app.services import compliance, email_sender, enrollment_events

API_BASE = "https://api.instantly.ai/api/v2"

# Instantly event_type -> what it means for us
EVENT_KIND = {
    "reply_received": "reply",
    "auto_reply_received": "auto_reply",
    "lead_out_of_office": "auto_reply",
    "email_bounced": "bounce",
    "lead_unsubscribed": "unsubscribe",
    "lead_wrong_person": "wrong_person",
    "lead_not_interested": "not_interested",
    "lead_interested": "interested",
}


class InstantlyClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=API_BASE, headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=20, transport=transport)

    def add_lead(self, campaign_id: str, lead: Lead) -> dict:
        first, _, last = (lead.name or "").partition(" ")
        r = self._client.post("/leads", json={
            "campaign": campaign_id,
            "email": lead.email,
            "first_name": first,
            "last_name": last,
            "custom_variables": {"address": lead.property_address or "", "phone": lead.phone or "",
                                 "lead_id": str(lead.id)},
            "skip_if_in_workspace": True,     # never email the same person from two campaigns
            "verify_leads_on_import": False,  # we already verified
        })
        r.raise_for_status()
        return r.json()


@dataclass
class PushResult:
    pushed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def push_leads(db: Session, client: InstantlyClient, instantly_campaign_id: str, leads: list[Lead]) -> PushResult:
    """Send only leads that pass our gates; Instantly never sees suppressed or invalid addresses."""
    res = PushResult()
    for lead in leads:
        if not lead.email:
            res.skip("no email")
            continue
        d = compliance.check(db, lead, "email")
        if not d.ok:
            res.skip(d.reason.split(" (")[0])
            continue
        if email_sender.ensure_verified(db, lead) == "invalid":
            res.skip("invalid email")
            continue
        client.add_lead(instantly_campaign_id, lead)
        res.pushed += 1
    return res


def handle_webhook(db: Session, payload: dict, now: datetime | None = None) -> dict:
    """Apply an Instantly webhook event to our system."""
    now = now or utcnow()
    event = payload.get("event_type", "")
    kind = EVENT_KIND.get(event)
    email = (payload.get("lead_email") or "").strip().lower()
    if not kind or not email:
        return {"event": event, "handled": False}
    lead = db.scalar(select(Lead).where(func.lower(Lead.email) == email))
    action = None

    if kind == "bounce":
        compliance.suppress(db, email, "email", "bounce")
        if lead:
            lead.email_status = "invalid"
        enrollment_events.on_inbound(db, "bounce", email=email)
        action = "suppressed"
    elif kind in ("unsubscribe", "wrong_person"):
        compliance.suppress(db, email, "email", "unsubscribe" if kind == "unsubscribe" else "wrong_number")
        enrollment_events.on_inbound(db, "stop", email=email)
        action = "suppressed"
    elif kind == "not_interested":
        enrollment_events.on_inbound(db, "reply", email=email, phone=lead.phone if lead else None)
        action = "sequences stopped"

    if kind in ("reply", "auto_reply"):
        body = payload.get("reply_text") or payload.get("reply_text_snippet") or payload.get("body") or ""
        db.add(Message(channel="email", direction="inbound", from_addr=email, to_addr=payload.get("email_account") or "-",
                       subject=payload.get("reply_subject") or payload.get("campaign_name"), body=body,
                       status="received", kind=kind, lead_id=lead.id if lead else None, created_at=now))
        if kind == "reply":
            # A reply in Instantly also stops our SMS sequences for this person, and opt-out wording suppresses.
            opted_out = compliance.classify_keyword("\n".join(body.strip().splitlines()[:3])) in ("stop", "wrong_number")
            if opted_out:
                compliance.suppress(db, email, "email", "opt_out")
            enrollment_events.on_inbound(db, "stop" if opted_out else "reply", email=email,
                                         phone=lead.phone if lead else None)
            action = "suppressed" if opted_out else "routed to inbox, sequences stopped"
    db.commit()
    return {"event": event, "kind": kind, "lead_id": lead.id if lead else None, "action": action, "handled": True}
