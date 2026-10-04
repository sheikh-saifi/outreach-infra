from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False, "timeout": 15} if _sqlite else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)

if _sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):
        # WAL lets the background scheduler write while API requests read/write, and
        # synchronous=NORMAL avoids a disk flush on every commit (still crash-safe in WAL mode).
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
