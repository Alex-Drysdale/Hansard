"""Recording what the pipeline did.

Every job -- ingest, members sync, divisions, checks -- opens a row here and
closes it. That makes "did last night's run work?" a query rather than an
exercise in reading logs, and gives the scheduler somewhere to leave evidence
when nobody is watching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow

COUNTER_COLUMNS = (
    "sitting_days",
    "debates_seen",
    "debates_inserted",
    "debates_updated",
    "debates_unchanged",
    "contributions_seen",
    "contributions_written",
    "errors",
)


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Identifies an open run, so the caller cannot mix up two of them."""

    run_id: int
    job: str


def start(
    connection: Connection[DictRow],
    *,
    job: str,
    house: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> RunHandle:
    """Open a run row and return its handle."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ingest_run (job, status, house, start_date, end_date)
            VALUES (%s, 'running', %s, %s, %s)
            RETURNING id
            """,
            (job, house, start_date, end_date),
        )
        row = cursor.fetchone()
    assert row is not None
    return RunHandle(run_id=int(row["id"]), job=job)


def finish(
    connection: Connection[DictRow],
    handle: RunHandle,
    *,
    status: str,
    counters: dict[str, int] | None = None,
    error_message: str | None = None,
) -> None:
    """Close a run with its final counters.

    Counters are whitelisted against the real columns rather than interpolated,
    so a typo in a caller cannot become SQL.
    """
    values: dict[str, Any] = {name: 0 for name in COUNTER_COLUMNS}
    for name, value in (counters or {}).items():
        if name not in values:
            raise ValueError(f"Unknown run counter {name!r}")
        values[name] = value

    assignments = ", ".join(f"{name} = %({name})s" for name in COUNTER_COLUMNS)
    connection.execute(
        f"""
        UPDATE ingest_run
           SET finished_at = now(), status = %(status)s, error_message = %(error_message)s,
               {assignments}
         WHERE id = %(run_id)s
        """,
        {**values, "status": status, "error_message": error_message, "run_id": handle.run_id},
    )


def latest(connection: Connection[DictRow], job: str | None = None) -> DictRow | None:
    """The most recent run, optionally for one job."""
    if job is None:
        return connection.execute("SELECT * FROM ingest_run ORDER BY id DESC LIMIT 1").fetchone()
    return connection.execute(
        "SELECT * FROM ingest_run WHERE job = %s ORDER BY id DESC LIMIT 1", (job,)
    ).fetchone()


def recent(connection: Connection[DictRow], limit: int = 10) -> list[DictRow]:
    return connection.execute(
        "SELECT * FROM ingest_run ORDER BY id DESC LIMIT %s", (limit,)
    ).fetchall()


def last_success_at(connection: Connection[DictRow], job: str) -> datetime | None:
    """When a job last completed, which is what incremental sync keys off."""
    row = connection.execute(
        """
        SELECT finished_at FROM ingest_run
         WHERE job = %s AND status = 'completed' AND finished_at IS NOT NULL
         ORDER BY finished_at DESC LIMIT 1
        """,
        (job,),
    ).fetchone()
    return row["finished_at"] if row else None
