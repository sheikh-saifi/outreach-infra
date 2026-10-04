from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import api, public, webhooks
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.scheduler import scheduler
from app.services import simulation

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    if settings.seed_demo_data and settings.telephony_provider == "mock":
        with SessionLocal() as db:
            simulation.seed(db)
    if settings.enable_scheduler and not scheduler.is_alive():
        scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Outreach Infra", version="0.1.0", lifespan=lifespan)
app.include_router(api)
app.include_router(webhooks)
app.include_router(public)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"ok": True}
