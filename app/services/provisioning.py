"""Line provisioning: buy, retire, replace, and keep each area-code pool at its target size."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PhoneNumber
from app.providers import get_carrier


def provision(db: Session, area_code: str, count: int = 1, campaign: str | None = None) -> list[PhoneNumber]:
    carrier = get_carrier()
    created = []
    for _ in range(count):
        p = carrier.buy_number(area_code)
        n = PhoneNumber(e164=p.e164, area_code=area_code, provider=carrier.name, provider_sid=p.provider_sid,
                        campaign=campaign, daily_cap=settings.default_daily_sms_cap)
        db.add(n)
        created.append(n)
    db.commit()
    return created


def retire(db: Session, number: PhoneNumber, reason: str = "manual") -> None:
    if number.provider_sid:
        get_carrier().release_number(number.provider_sid)
    number.status, number.status_reason = "retired", reason
    db.commit()


def replace(db: Session, number: PhoneNumber) -> PhoneNumber:
    """Retire a burned line and buy a fresh one in the same area code / campaign."""
    retire(db, number, reason=f"replaced (score {number.health_score})")
    return provision(db, number.area_code, 1, number.campaign)[0]


def replenish_pools(db: Session, target_per_area: dict[str, int]) -> dict[str, int]:
    """Top up active lines per area code. Returns how many were bought per area code."""
    bought = {}
    for area_code, target in target_per_area.items():
        active = db.scalar(select(func.count(PhoneNumber.id)).where(
            PhoneNumber.area_code == area_code, PhoneNumber.status == "active")) or 0
        if active < target:
            provision(db, area_code, target - active)
            bought[area_code] = target - active
    return bought
