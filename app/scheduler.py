"""In-process background scheduler. Runs the recurring jobs the infrastructure depends on.

Good enough for one server. At scale, move these to a real queue (Celery/RQ + Redis, or a
cron-driven worker) so a slow job can't delay the dispatcher and jobs survive restarts.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.config import settings
from app.db import SessionLocal
from app.models import Mailbox, utcnow

log = logging.getLogger("scheduler")


@dataclass
class Job:
    name: str
    every_s: int
    fn: Callable
    description: str
    last_run: datetime | None = None
    last_result: str | None = None
    last_error: str | None = None
    runs: int = 0
    _next: float = field(default=0.0, repr=False)


def _dispatch(db):
    from app.services.campaigns import run_due
    r = run_due(db)
    return f"sent {r['sent']}, deferred {r['deferred']}, stopped {r['stopped']}" if r["due"] else "nothing due"


def _health(db):
    from app.services.line_health import run_health_sweep
    acted = [r for r in run_health_sweep(db) if r["action"] != "none"]
    return ", ".join(f"{r['number']} {r['action']}" for r in acted) or "all lines within policy"


def _spam(db):
    from app.services.line_health import run_spam_checks
    r = run_spam_checks(db)
    return f"{sum(x['label'] == 'spam_likely' for x in r)}/{len(r)} flagged"


def _domains(db):
    from app.services.domain_health import run_domain_checks
    r = run_domain_checks(db)
    return f"{sum(x['status'] == 'paused' for x in r)}/{len(r)} paused"


def _warmup(db):
    """Mock mode: generate today's warm-up network traffic. Real mode: a warm-up service does this."""
    import random

    from app.services import warmup
    if settings.email_provider != "mock":
        return "skipped (external warm-up service)"
    today = utcnow().date()
    rng = random.Random()
    boxes = db.query(Mailbox).all()
    for mb in boxes:
        warmup.simulate_day(db, mb, today, rng)
    return f"{len(boxes)} mailboxes"


JOBS = [
    Job("dispatcher", 30, _dispatch, "Send due campaign steps"),
    Job("line_health", 15 * 60, _health, "Score lines, rest / quarantine / reactivate"),
    Job("spam_labels", 24 * 3600, _spam, "Reputation lookup for every line"),
    Job("domain_health", 24 * 3600, _domains, "Auth + blacklist + bounce checks per domain"),
    Job("warmup", 6 * 3600, _warmup, "Warm-up volume for today"),
]


class Scheduler(threading.Thread):
    def __init__(self, jobs: list[Job]):
        super().__init__(daemon=True, name="scheduler")
        self.jobs = jobs
        self._halt = threading.Event()

    def run(self) -> None:
        now = time.monotonic()
        for i, job in enumerate(self.jobs):
            job._next = now + 5 + i  # stagger the first runs
        while not self._halt.wait(1):
            for job in self.jobs:
                if time.monotonic() >= job._next:
                    self.run_job(job)

    def run_job(self, job: Job) -> None:
        job._next = time.monotonic() + job.every_s
        try:
            with SessionLocal() as db:
                job.last_result = job.fn(db)
            job.last_error = None
        except Exception as e:  # a failing job must never kill the scheduler
            log.exception("job %s failed", job.name)
            job.last_error = f"{type(e).__name__}: {e}"[:300]
        job.last_run = utcnow()
        job.runs += 1

    def stop(self) -> None:
        self._halt.set()

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [{"name": j.name, "description": j.description, "every_s": j.every_s, "runs": j.runs,
                 "last_run": j.last_run, "last_result": j.last_result, "last_error": j.last_error,
                 "next_in_s": max(0, int(j._next - now)) if self.is_alive() else None} for j in self.jobs]


scheduler = Scheduler(JOBS)
