"""The pipeline's units of work.

Each job is a plain function that takes a context and returns a report. That
shape is what lets the CLI and the scheduler drive exactly the same code: one
runs a job because a person asked, the other because a cron expression fired,
and neither knows the difference.

Every job is wrapped by :func:`run_job`, which owns the parts that must not be
reimplemented per job -- opening and closing the ingest_run row, binding the run
id onto the logger, and making sure a crash is recorded as a failure rather than
leaving a run marked 'running' for ever.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import date

from psycopg import Connection
from psycopg.rows import DictRow

from hansard.api.client import ApiError, HansardClient
from hansard.api.members_client import MembersClient
from hansard.api.models import DebateSummary
from hansard.config import Settings
from hansard.db import debates as debates_store
from hansard.db import divisions as divisions_store
from hansard.db import members as members_store
from hansard.db import runs
from hansard.db.engine import transaction
from hansard.logging_config import bind_run, clear_run, get_logger
from hansard.pipeline import checks
from hansard.pipeline.normalise import (
    ContributionRow,
    normalise_debate,
    normalise_division,
    normalise_member,
    resolve_speakers,
)

log = get_logger(__name__)


@dataclass(slots=True)
class JobReport:
    """What a job did. Persisted to ``ingest_run`` and shown by the CLI."""

    job: str
    sitting_days: int = 0
    debates_seen: int = 0
    debates_inserted: int = 0
    debates_updated: int = 0
    debates_unchanged: int = 0
    contributions_seen: int = 0
    contributions_written: int = 0
    errors: int = 0
    failures: list[str] = field(default_factory=list)
    # Free-form detail that has no ingest_run column; shown, not stored.
    notes: dict[str, int | str] = field(default_factory=dict)

    def as_counters(self) -> dict[str, int]:
        excluded = {"job", "failures", "notes"}
        return {k: v for k, v in asdict(self).items() if k not in excluded}

    def record_failure(self, message: str) -> None:
        self.errors += 1
        # Keep the first handful only: a systemic outage would otherwise put
        # thousands of near-identical strings into the run row.
        if len(self.failures) < 20:
            self.failures.append(message)


@dataclass(frozen=True, slots=True)
class JobContext:
    """Everything a job needs, supplied by the caller.

    Clients are passed in rather than constructed here so that tests can inject
    a stubbed transport, and so the scheduler can share one rate limiter across
    consecutive jobs instead of resetting it each time.
    """

    connection: Connection[DictRow]
    settings: Settings
    hansard: HansardClient
    members: MembersClient
    on_progress: Callable[[str, int, int], None] | None = None

    def progress(self, label: str, done: int, total: int) -> None:
        if self.on_progress is not None:
            self.on_progress(label, done, total)


JobFunction = Callable[[JobContext], JobReport]


def run_job(name: str, function: JobFunction, context: JobContext) -> JobReport:
    """Execute a job with run bookkeeping, whatever happens inside it."""
    handle = runs.start(
        context.connection,
        job=name,
        house=context.settings.house,
        start_date=context.settings.start_date,
        end_date=context.settings.end_date,
    )
    bind_run(job=name, run_id=handle.run_id)
    started = time.monotonic()
    log.info("job.started", house=context.settings.house)

    try:
        report = function(context)
    except BaseException as exc:
        # BaseException, not Exception: a KeyboardInterrupt during a two-hour
        # ingest should still close the run row rather than leave it 'running'.
        runs.finish(
            context.connection,
            handle,
            status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        log.error("job.failed", error=str(exc), error_type=type(exc).__name__)
        clear_run()
        raise

    runs.finish(
        context.connection,
        handle,
        status="completed",
        counters=report.as_counters(),
        error_message="; ".join(report.failures[:5]) or None,
    )
    log.info(
        "job.completed",
        duration_seconds=round(time.monotonic() - started, 1),
        errors=report.errors,
        **report.notes,
    )
    clear_run()
    return report


# ---------------------------------------------------------------------------
# hansard.debates
# ---------------------------------------------------------------------------


def ingest_debates(
    context: JobContext, *, resume: bool = False, limit_days: int | None = None
) -> JobReport:
    """Walk the configured date range, storing every debate section.

    One transaction per debate, not one per run: a network failure two hours in
    leaves everything already fetched committed, and ``resume`` picks up from
    the first day that never completed.
    """
    settings = context.settings
    report = JobReport(job="hansard.debates")

    days = list(
        context.hansard.iter_sitting_dates(settings.house, settings.start_date, settings.end_date)
    )
    if resume:
        done = debates_store.completed_sitting_days(context.connection, settings.house)
        days = [day for day in days if day not in done]
    if limit_days is not None:
        days = days[:limit_days]

    log.info("debates.range_resolved", days=len(days))

    for index, day in enumerate(days, start=1):
        _ingest_one_day(context, day, report)
        report.sitting_days += 1
        context.progress(str(day), index, len(days))

    with transaction(context.connection):
        debates_store.refresh_member_counts(context.connection)

    return report


def _ingest_one_day(context: JobContext, day: date, report: JobReport) -> None:
    settings = context.settings
    summaries = context.hansard.debates_on(settings.house, day)

    with transaction(context.connection):
        debates_store.record_sitting_day(
            context.connection, house=settings.house, sitting_date=day, debate_count=len(summaries)
        )

    day_failures = 0
    for summary in _unique_by_ext_id(summaries):
        report.debates_seen += 1
        try:
            detail = context.hansard.debate(summary.ext_id)
        except ApiError as exc:
            # One unfetchable section must not cost us the other 24 on the day.
            day_failures += 1
            report.record_failure(f"{day} {summary.ext_id}: {exc}")
            log.warning(
                "debate.skipped", ext_id=summary.ext_id, sitting_date=str(day), error=str(exc)
            )
            continue

        normalised = normalise_debate(detail)
        report.contributions_seen += len(normalised.contributions)

        with transaction(context.connection):
            result = debates_store.save_debate(context.connection, normalised)

        if result.outcome is debates_store.WriteOutcome.INSERTED:
            report.debates_inserted += 1
        elif result.outcome is debates_store.WriteOutcome.UPDATED:
            report.debates_updated += 1
        else:
            report.debates_unchanged += 1
        report.contributions_written += result.contributions_written

    # A day counts as complete only if nothing on it failed; otherwise resume
    # would skip past a day we know is missing sections.
    if day_failures == 0:
        with transaction(context.connection):
            debates_store.complete_sitting_day(
                context.connection, house=settings.house, sitting_date=day
            )


def _unique_by_ext_id(summaries: list[DebateSummary]) -> list[DebateSummary]:
    """Drop repeated ext_ids within one day's results.

    The cheapest line of dedup defence: a section returned twice on the same
    page costs a wasted HTTP request if we do not catch it here. The upsert
    behind it makes a miss harmless rather than corrupting.
    """
    seen: set[str] = set()
    unique: list[DebateSummary] = []
    for summary in summaries:
        if summary.ext_id in seen:
            log.debug("debate.duplicate_in_index", ext_id=summary.ext_id)
            continue
        seen.add(summary.ext_id)
        unique.append(summary)
    return unique


# ---------------------------------------------------------------------------
# hansard.divisions
# ---------------------------------------------------------------------------


def ingest_divisions(context: JobContext) -> JobReport:
    """Fetch the votes for every debate whose transcript recorded a division.

    Driven off what we already stored rather than off the calendar: Hansard puts
    a Division item in the transcript, so we know which sections divided and can
    skip the large majority that did not.
    """
    settings = context.settings
    report = JobReport(job="hansard.divisions")

    candidates = debates_store.debates_with_divisions(
        context.connection, house=settings.house, start=settings.start_date, end=settings.end_date
    )
    log.info("divisions.candidates_resolved", debates=len(candidates))

    stored = 0
    for index, debate_ext_id in enumerate(candidates, start=1):
        report.debates_seen += 1
        try:
            summaries = context.hansard.divisions_in(str(debate_ext_id))
        except ApiError as exc:
            report.record_failure(f"divisions for {debate_ext_id}: {exc}")
            log.warning("divisions.list_failed", debate_ext_id=str(debate_ext_id), error=str(exc))
            continue

        for summary in summaries:
            try:
                detail = context.hansard.division(summary.ext_id)
            except ApiError as exc:
                report.record_failure(f"division {summary.ext_id}: {exc}")
                log.warning("division.skipped", ext_id=summary.ext_id, error=str(exc))
                continue

            normalised = normalise_division(detail)
            with transaction(context.connection):
                result = divisions_store.save_division(context.connection, normalised)

            if result.outcome is debates_store.WriteOutcome.INSERTED:
                report.debates_inserted += 1
            elif result.outcome is debates_store.WriteOutcome.UPDATED:
                report.debates_updated += 1
            else:
                report.debates_unchanged += 1
            report.contributions_written += result.votes_written
            stored += 1

        context.progress(str(debate_ext_id), index, len(candidates))

    report.notes["divisions"] = stored
    return report


# ---------------------------------------------------------------------------
# reattribute
# ---------------------------------------------------------------------------


def reattribute(context: JobContext) -> JobReport:
    """Resolve continuation paragraphs for debates already stored.

    Needed because the content hash covers what Hansard sent, not what we infer
    from it -- so a re-ingest correctly reports every debate unchanged and would
    never revisit them. This reads the stored rows and applies the same
    ``resolve_speakers`` used at ingest, so there is one implementation of the
    rules rather than a second copy that could drift.
    """
    report = JobReport(job="reattribute")
    connection = context.connection

    pending = debates_store.all_debate_ids(connection)
    log.info("reattribute.candidates", debates=len(pending))

    carried_total = 0
    for index, debate_ext_id in enumerate(pending, start=1):
        rows = debates_store.load_contributions_for_attribution(connection, debate_ext_id)
        stub = [
            ContributionRow(
                item_id=row["item_id"],
                debate_ext_id=debate_ext_id,
                external_id=None,
                order_in_section=row["order_in_section"],
                item_type=row["item_type"],
                hrs_tag=row["hrs_tag"],
                is_speech=row["is_speech"],
                member_id=row["member_id"],
                attributed_to=row["attributed_to"],
                speaker_name=None,
                speaker_key=None,
                speaker_role=None,
                party=None,
                constituency=None,
                body_html=None,
                body_text=row["body_text"] or "",
                word_count=row["word_count"],
                timecode=None,
                is_reiteration=False,
                content_hash="",
            )
            for row in rows
        ]
        resolution = {
            item_id: (value.member_id, value.source.value)
            for item_id, value in resolve_speakers(stub).items()
        }
        with transaction(connection):
            carried_total += debates_store.apply_attribution(connection, resolution)
        report.debates_seen += 1
        context.progress(debate_ext_id, index, len(pending))

    with transaction(connection):
        debates_store.refresh_member_counts(connection)

    report.notes["paragraphs_carried"] = carried_total
    return report


# ---------------------------------------------------------------------------
# members.sync
# ---------------------------------------------------------------------------


def sync_members(context: JobContext, *, full: bool = False) -> JobReport:
    """Fill in member records from source two.

    Incremental by default: only members we have never synced are fetched, one
    request each. ``full`` re-walks the whole search endpoint, which is what you
    want after a general election or a reshuffle, when people already in the
    table have changed party or seat.
    """
    report = JobReport(job="members.sync")
    connection = context.connection

    if full:
        rows = [
            normalise_member(value)
            for value in context.members.iter_members(context.settings.house)
        ]
        log.info("members.full_walk", fetched=len(rows))
    else:
        pending = members_store.unsynced_member_ids(connection)
        log.info("members.incremental", pending=len(pending))
        rows = []
        for index, member_id in enumerate(pending, start=1):
            try:
                value = context.members.member(member_id)
            except ApiError as exc:
                report.record_failure(f"member {member_id}: {exc}")
                log.warning("member.skipped", member_id=member_id, error=str(exc))
            else:
                if value.member_id != member_id:
                    # Storing the returned record would leave the id we asked
                    # for still unsynced, so the next run would fetch it again,
                    # for ever. Better to fail loudly once.
                    report.record_failure(
                        f"member {member_id}: API returned member {value.member_id}"
                    )
                    log.warning("member.id_mismatch", requested=member_id, returned=value.member_id)
                else:
                    rows.append(normalise_member(value))
            context.progress(str(member_id), index, len(pending))

    if rows:
        with transaction(connection):
            result = members_store.save_members(connection, rows)
        report.notes["members_seen"] = result.seen
        report.notes["members_inserted"] = result.inserted
        report.notes["members_updated"] = result.updated

    stats = members_store.coverage(connection)
    # Measured over attributed speeches only. About a quarter of speeches carry
    # no member id at all -- procedural text, motions -- so no source could ever
    # give them a party, and counting them made a 100% result read as 77%.
    report.notes["party_coverage"] = _percentage(
        stats["attributed_with_party"], stats["speeches_attributed"]
    )
    return report


def _percentage(part: int, whole: int) -> str:
    return f"{(part / whole * 100):.1f}%" if whole else "n/a"


# ---------------------------------------------------------------------------
# rag.index
# ---------------------------------------------------------------------------


def rebuild_index(context: JobContext) -> JobReport:
    """Rebuild the vector index so retrieval sees what was just ingested.

    Scheduled rather than remembered, because a stale index is the quietest
    failure in the whole system: the query still works, the answer is still
    fluent, and it is simply missing everything added since the last build. The
    only symptom is an answer that is wrong in a way nobody can see.
    """
    from hansard.rag.build import build_index

    report = JobReport(job="rag.index")
    result = build_index(context.connection, context.settings, on_progress=context.on_progress)
    report.debates_seen = result.turns
    report.contributions_written = result.chunks
    report.notes["chunks"] = result.chunks
    report.notes["seconds"] = f"{result.seconds:.1f}"
    return report


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def run_checks(context: JobContext) -> JobReport:
    """Run the integrity checks and record failures on the run.

    Scheduled like any other job, so a store that quietly drifts overnight
    shows up as a failed run rather than waiting for someone to look.
    """
    report = JobReport(job="checks")
    results = checks.run_all(context.connection)
    for result in results:
        if not result.passed:
            report.record_failure(f"{result.name}: {result.detail}")
    report.notes["checks_run"] = len(results)
    report.notes["checks_failed"] = sum(1 for r in results if not r.passed)
    return report


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

JOBS: dict[str, JobFunction] = {
    "hansard.debates": ingest_debates,
    "hansard.divisions": ingest_divisions,
    "reattribute": reattribute,
    "members.sync": sync_members,
    "rag.index": rebuild_index,
    "checks": run_checks,
}


def job_names() -> Iterator[str]:
    yield from JOBS
