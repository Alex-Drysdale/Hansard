"""Orchestration: walk a date range, fetch each debate, store it.

The shape of the work, and why:

    for each sitting day in range      <- calendar tells us which days sat
        list the day's debate sections <- one paged search per day
        for each section
            fetch its transcript       <- one request per section
            normalise and store it     <- one transaction per section

One transaction per debate, not one per run. A network failure two hours in
leaves everything already fetched safely committed, and ``--resume`` picks up
from the first day that never completed.

This module owns no HTTP details and no SQL. It coordinates the three layers
that do, which keeps the interesting decisions -- retry, hashing, upsert -- in
places that can be tested without a network or a clock.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from sqlite3 import Connection

from hansard.api.client import HansardApiError, HansardClient
from hansard.api.models import DebateSummary
from hansard.config import Settings
from hansard.db import store
from hansard.db.connection import transaction
from hansard.pipeline.normalise import normalise_debate

logger = logging.getLogger(__name__)

ProgressCallback = Callable[["DayProgress"], None]


@dataclass(frozen=True, slots=True)
class DayProgress:
    """Reported after each sitting day so a caller can draw a progress bar."""

    sitting_date: date
    index: int
    total: int
    debates: int
    skipped: bool = False


@dataclass(slots=True)
class IngestReport:
    """What a run did. Returned to the caller and persisted to ``ingest_run``."""

    house: str
    start_date: date
    end_date: date
    sitting_days: int = 0
    debates_seen: int = 0
    debates_inserted: int = 0
    debates_updated: int = 0
    debates_unchanged: int = 0
    contributions_seen: int = 0
    contributions_written: int = 0
    errors: int = 0
    failures: list[str] = field(default_factory=list)

    def as_counters(self) -> dict[str, int]:
        return {
            "sitting_days": self.sitting_days,
            "debates_seen": self.debates_seen,
            "debates_inserted": self.debates_inserted,
            "debates_updated": self.debates_updated,
            "debates_unchanged": self.debates_unchanged,
            "contributions_seen": self.contributions_seen,
            "contributions_written": self.contributions_written,
            "errors": self.errors,
        }


def ingest_range(
    connection: Connection,
    client: HansardClient,
    settings: Settings,
    *,
    resume: bool = False,
    limit_days: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> IngestReport:
    """Ingest every sitting day in the configured range.

    ``resume`` skips days already marked complete, so an interrupted run can be
    restarted without re-fetching what it already has.
    """
    report = IngestReport(
        house=settings.house, start_date=settings.start_date, end_date=settings.end_date
    )
    run_id = store.start_run(
        connection, house=settings.house, start=settings.start_date, end=settings.end_date
    )

    try:
        days = list(
            client.iter_sitting_dates(settings.house, settings.start_date, settings.end_date)
        )
        if resume:
            already_done = store.completed_sitting_days(connection, settings.house)
            days = [day for day in days if day not in already_done]
        if limit_days is not None:
            days = days[:limit_days]

        logger.info("ingesting %d sitting day(s) for %s", len(days), settings.house)

        for index, day in enumerate(days, start=1):
            debates = _ingest_day(connection, client, settings, day, report)
            report.sitting_days += 1
            if on_progress is not None:
                on_progress(DayProgress(day, index, len(days), debates))

    except BaseException as exc:
        store.finish_run(
            connection,
            run_id,
            status="failed",
            counters=report.as_counters(),
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    with transaction(connection):
        store.refresh_member_counts(connection)

    store.finish_run(
        connection,
        run_id,
        status="completed",
        counters=report.as_counters(),
        error_message="; ".join(report.failures[:5]) or None,
    )
    return report


def _ingest_day(
    connection: Connection,
    client: HansardClient,
    settings: Settings,
    day: date,
    report: IngestReport,
) -> int:
    """Fetch and store every debate section for one sitting day."""
    summaries = client.debates_on(settings.house, day)

    with transaction(connection):
        store.record_sitting_day(
            connection, house=settings.house, sitting_date=day, debate_count=len(summaries)
        )

    outcomes: Counter[str] = Counter()
    day_failures = 0

    for summary in _unique_by_ext_id(summaries):
        report.debates_seen += 1
        try:
            detail = client.debate(summary.ext_id)
        except HansardApiError as exc:
            # One unfetchable section must not cost us the other 24 on the day.
            day_failures += 1
            report.errors += 1
            message = f"{day} {summary.ext_id}: {exc}"
            report.failures.append(message)
            logger.warning("skipping debate %s: %s", summary.ext_id, exc)
            continue

        normalised = normalise_debate(detail)
        report.contributions_seen += len(normalised.contributions)

        with transaction(connection):
            result = store.save_debate(connection, normalised)

        outcomes[result.outcome.value] += 1
        report.contributions_written += result.contributions_written

    report.debates_inserted += outcomes["inserted"]
    report.debates_updated += outcomes["updated"]
    report.debates_unchanged += outcomes["unchanged"]

    # A day is only "complete" if nothing on it failed; otherwise --resume
    # would skip past a day we know is missing sections.
    if day_failures == 0:
        with transaction(connection):
            store.complete_sitting_day(connection, house=settings.house, sitting_date=day)

    return len(summaries)


def _unique_by_ext_id(summaries: Iterable[DebateSummary]) -> list[DebateSummary]:
    """Drop repeated ext_ids within one day's search results.

    The first line of dedup defence, and the cheapest: a section returned twice
    on the same page costs a wasted HTTP request if we do not catch it here.
    The upsert behind it makes a miss harmless rather than corrupting.
    """
    seen: set[str] = set()
    unique: list[DebateSummary] = []
    for summary in summaries:
        ext_id = summary.ext_id
        if ext_id in seen:
            logger.debug("dropping repeated ext_id %s in day results", ext_id)
            continue
        seen.add(ext_id)
        unique.append(summary)
    return unique
