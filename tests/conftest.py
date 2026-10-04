import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["TELEPHONY_PROVIDER"] = "mock"
os.environ["EMAIL_PROVIDER"] = "mock"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["SEND_JITTER_MINUTES"] = "0"  # deterministic schedules in tests

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Lead, PhoneNumber  # noqa: E402
from app.providers import get_carrier  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def carrier():
    return get_carrier()


@pytest.fixture
def make_line(db):
    def _make(area_code="214", status="active", score=100.0, e164=None, cap=150):
        n = PhoneNumber(e164=e164 or f"+1{area_code}{5550000 + db.query(PhoneNumber).count()}",
                        area_code=area_code, provider="mock", status=status, health_score=score, daily_cap=cap)
        db.add(n)
        db.commit()
        return n
    return _make


@pytest.fixture
def make_lead(db):
    def _make(phone="+12145551234", **kw):
        lead = Lead(name=kw.pop("name", "Jane Doe"), phone=phone, **kw)
        db.add(lead)
        db.commit()
        return lead
    return _make
