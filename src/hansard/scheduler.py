"""Running jobs on a schedule.

A long-lived process that fires the same job functions the CLI does, on cron
expressions. Deliberately not a distributed task queue: one machine, a handful
of daily jobs, and Postgres already holding the history.

Three properties matter more than the scheduling itself:

* **Jobs never overlap.** ``max_instances=1`` plus a coalescing policy means a
  slow ingest cannot have a second copy of itself started on top of it.
* **A missed fire is caught up, once.** A laptop that was asleep at 06:00 runs
  the job when it wakes, rather than either skipping the day or running it
  three times.
* **A crashing job does not kill the scheduler.** Each run is recorded as a
  failure and the next one is still scheduled.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import FrameType

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from hansard.config import DEFAULT_SCHEDULE, Settings
from hansard.logging_config import get_logger

log = get_logger(__name__)

# How late a fire may be and still run. Long enough to cover a closed laptop
# overnight, short enough that a machine off for a week does not wake up and
# immediately run yesterday's job as though it were today's.
MISFIRE_GRACE_SECONDS = 6 * 60 * 60


# The timezone every cron expression is interpreted in. Stated explicitly
# because APScheduler builds a trigger in the *local* zone when you do not say,
# and the trigger's own zone beats the scheduler's -- so a scheduler configured
# for UTC would still fire on British Summer Time, an hour adrift for half the
# year, while reporting UTC.
SCHEDULE_TIMEZONE = "UTC"

# Standard cron numbers Sunday 0 (and accepts 7); APScheduler numbers Monday 0.
# Passing "1" straight through therefore means Tuesday, not Monday. Expressions
# in this project use standard cron semantics, so the day-of-week field is
# translated rather than left to mean something different from what it says.
_CRON_DOW_TO_APSCHEDULER = {
    "0": "6",
    "1": "0",
    "2": "1",
    "3": "2",
    "4": "3",
    "5": "4",
    "6": "5",
    "7": "6",
}


def translate_day_of_week(field: str) -> str:
    """Convert a standard-cron day-of-week field to APScheduler's numbering.

    Day *names* mean the same thing in both and pass through untouched.
    Numbers are remapped. A numeric range that wraps the week is rejected
    rather than guessed at, since remapping its endpoints would silently invert
    it.
    """
    if field in ("*", "?"):
        return field

    parts = []
    for token in field.split(","):
        step = ""
        if "/" in token:
            token, _, step = token.partition("/")
            step = f"/{step}"

        if "-" in token:
            start, _, end = token.partition("-")
            if start.isdigit() and end.isdigit():
                new_start = _CRON_DOW_TO_APSCHEDULER[start]
                new_end = _CRON_DOW_TO_APSCHEDULER[end]
                if int(new_start) > int(new_end):
                    raise ValueError(
                        f"Day-of-week range {token!r} wraps the week; "
                        "write it as day names or as separate values"
                    )
                parts.append(f"{new_start}-{new_end}{step}")
            else:
                parts.append(f"{token}{step}")
        elif token.isdigit():
            parts.append(f"{_CRON_DOW_TO_APSCHEDULER[token]}{step}")
        else:
            parts.append(f"{token}{step}")

    return ",".join(parts)


def to_trigger(cron: str, *, timezone: str = SCHEDULE_TIMEZONE) -> CronTrigger:
    """Build a trigger from a standard cron expression."""
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5 cron fields, got {len(fields)} in {cron!r}")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=translate_day_of_week(day_of_week),
        timezone=timezone,
    )


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    name: str
    cron: str
    run: Callable[[], None]

    def trigger(self) -> CronTrigger:
        return to_trigger(self.cron)

    def next_run(self, after: datetime | None = None) -> datetime | None:
        # Annotated rather than returned straight through: APScheduler is
        # untyped, and letting its Any escape would weaken every caller.
        fire_time: datetime | None = self.trigger().get_next_fire_time(
            None, after or datetime.now().astimezone()
        )
        return fire_time


def build_jobs(
    settings: Settings,
    runner: Callable[[str], None],
    schedule: dict[str, str] | None = None,
) -> list[ScheduledJob]:
    """Pair each configured cron expression with the job it should run.

    ``runner`` is injected so the scheduler never needs to know how a job
    acquires a database connection or an HTTP client -- and so a test can supply
    a recorder instead.
    """
    resolved = schedule or DEFAULT_SCHEDULE
    return [
        ScheduledJob(name=name, cron=cron, run=lambda n=name: runner(n))  # type: ignore[misc]
        for name, cron in resolved.items()
    ]


def _on_job_event(event: JobExecutionEvent) -> None:
    if event.exception:
        # Already recorded against ingest_run by run_job; logged here so the
        # scheduler's own output shows that it survived.
        log.error("scheduler.job_failed", job=event.job_id, error=str(event.exception))
    else:
        log.info("scheduler.job_finished", job=event.job_id)


def _configure(
    scheduler: BlockingScheduler | BackgroundScheduler, jobs: list[ScheduledJob]
) -> None:
    for job in jobs:
        scheduler.add_job(
            job.run,
            trigger=job.trigger(),
            id=job.name,
            name=job.name,
            # One at a time: a run that overruns its next fire must not be
            # joined by a second copy competing for the same rows.
            max_instances=1,
            # Collapse several missed fires into one catch-up run.
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        log.info(
            "scheduler.job_registered", job=job.name, cron=job.cron, next_run=str(job.next_run())
        )
    scheduler.add_listener(_on_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)


def run_forever(jobs: list[ScheduledJob]) -> None:
    """Block, running jobs until interrupted.

    Handles SIGTERM as well as SIGINT so that ``docker compose down`` shuts the
    container down cleanly instead of waiting out the kill timeout.
    """
    scheduler = BlockingScheduler(timezone=SCHEDULE_TIMEZONE)
    _configure(scheduler, jobs)

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        log.info("scheduler.stopping", signal=signal.Signals(signum).name)
        # wait=False: stop accepting work immediately; an in-flight job finishes
        # its current transaction because each one commits per debate.
        scheduler.shutdown(wait=False)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    log.info("scheduler.started", jobs=len(jobs))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.stopped")


def run_once_now(jobs: list[ScheduledJob], *, timeout: float = 300.0) -> None:
    """Fire every job immediately and wait for them.

    What ``--now`` uses: a way to prove the wiring works without waiting for
    06:00 to come round.
    """
    scheduler = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)
    finished = threading.Event()
    remaining = {job.name for job in jobs}
    lock = threading.Lock()

    def mark_done(event: JobExecutionEvent) -> None:
        with lock:
            remaining.discard(event.job_id)
            if not remaining:
                finished.set()

    scheduler.add_listener(mark_done, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    for job in jobs:
        scheduler.add_job(job.run, id=job.name, name=job.name, max_instances=1)

    scheduler.start()
    try:
        if not finished.wait(timeout):
            log.warning("scheduler.timeout", waiting_for=sorted(remaining))
    finally:
        scheduler.shutdown(wait=False)
