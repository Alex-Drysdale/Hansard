"""Command line interface.

Thin by design: parse arguments, wire the layers together, present the result.
Everything a test would want to assert on lives in the modules underneath, and
every command here drives the same job functions the scheduler does.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from typing import Annotated, Any

import typer
from dotenv import find_dotenv, load_dotenv
from psycopg import Connection
from psycopg.rows import DictRow
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from hansard.api.client import HansardClient
from hansard.api.members_client import MembersClient
from hansard.config import DEFAULT_SCHEDULE, Settings
from hansard.db import divisions as divisions_store
from hansard.db import members as members_store
from hansard.db import migrate, runs
from hansard.db.engine import DatabaseUnavailableError, open_connection, redact
from hansard.logging_config import configure as configure_logging
from hansard.pipeline import checks, jobs
from hansard.rag import answer as rag_answer
from hansard.rag import build as rag_build
from hansard.rag import index as rag_index
from hansard.rag import retrieve as rag_retrieve
from hansard.scheduler import build_jobs, run_forever, run_once_now

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Ingest UK Parliament debates, divisions and members into Postgres.",
)
console = Console()


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

DatabaseOption = Annotated[
    str | None, typer.Option("--database-url", "-d", help="Postgres connection URL.")
]
HouseOption = Annotated[str | None, typer.Option("--house", help="Commons or Lords.")]
StartOption = Annotated[
    datetime | None,
    typer.Option("--start", formats=["%Y-%m-%d"], help="First date, inclusive (YYYY-MM-DD)."),
]
EndOption = Annotated[
    datetime | None,
    typer.Option("--end", formats=["%Y-%m-%d"], help="Last date, inclusive (YYYY-MM-DD)."),
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")]
JsonLogsOption = Annotated[
    bool, typer.Option("--json-logs", help="Emit logs as JSON rather than for a terminal.")
]


# Sentinels ts_headline wraps matched terms in. Chosen so they cannot collide
# with Rich's own markup, and swapped for it once the text has been escaped.
HIGHLIGHT_START = "«"
HIGHLIGHT_END = "»"


def _highlight(excerpt: str | None) -> str:
    """Escape database text, then turn the search sentinels into Rich markup.

    Order matters: escaping first means a speech containing literal square
    brackets renders as written instead of being swallowed as markup.
    """
    if not excerpt:
        return ""
    return (
        escape(excerpt)
        .replace(HIGHLIGHT_START, "[bold yellow]")
        .replace(HIGHLIGHT_END, "[/bold yellow]")
    )


def _as_date(value: datetime | None) -> date | None:
    """Typer parses dates as datetimes; the rest of the code wants a date."""
    return value.date() if value is not None else None


def _settings(
    *,
    database_url: str | None = None,
    house: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    verbose: bool = False,
    json_logs: bool = False,
) -> Settings:
    """Environment defaults, overridden by whatever the user passed.

    ``replace`` rather than rebuilding field by field: a hand-written
    constructor call silently resets any setting it forgets to mention, so
    adding one to Settings would quietly break the CLI.
    """
    overrides: dict[str, Any] = {}
    if database_url is not None:
        overrides["database_url"] = database_url
    if house is not None:
        overrides["house"] = house
    if (start_date := _as_date(start)) is not None:
        overrides["start_date"] = start_date
    if (end_date := _as_date(end)) is not None:
        overrides["end_date"] = end_date
    if verbose:
        overrides["log_level"] = "DEBUG"
    if json_logs:
        overrides["log_format"] = "json"

    settings = replace(Settings.from_env(), **overrides)
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    return settings


@contextmanager
def _database(settings: Settings) -> Iterator[Connection[DictRow]]:
    """A connection, with a readable error when Postgres is not there."""
    try:
        with open_connection(settings.database_url) as connection:
            yield connection
    except DatabaseUnavailableError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


@contextmanager
def _job_context(
    settings: Settings, *, progress_label: str | None = None
) -> Iterator[jobs.JobContext]:
    """Everything a job needs, torn down afterwards."""
    with (
        _database(settings) as connection,
        HansardClient(settings) as hansard,
        MembersClient(settings) as members,
        Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            disable=progress_label is None,
        ) as progress,
    ):
        task = progress.add_task(progress_label or "", total=None)

        def on_progress(label: str, done: int, total: int) -> None:
            progress.update(task, total=total, completed=done, description=label)

        yield jobs.JobContext(
            connection=connection,
            settings=settings,
            hansard=hansard,
            members=members,
            on_progress=on_progress,
        )


def _report_table(report: jobs.JobReport) -> Table:
    table = Table(title=f"{report.job}", show_header=False, box=None)
    rows: tuple[tuple[str, int], ...] = (
        ("sitting days", report.sitting_days),
        ("items seen", report.debates_seen),
        ("  inserted", report.debates_inserted),
        ("  updated", report.debates_updated),
        ("  unchanged", report.debates_unchanged),
        ("rows written", report.contributions_written),
        ("errors", report.errors),
    )
    for label, value in rows:
        if value or label in ("items seen", "errors"):
            table.add_row(label, f"{value:,}")
    for key, note in report.notes.items():
        table.add_row(key.replace("_", " "), str(note))
    return table


def _finish(report: jobs.JobReport) -> None:
    console.print(_report_table(report))
    for failure in report.failures[:5]:
        console.print(f"  [yellow]![/yellow] {failure}")
    if report.errors:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@app.command("migrate")
def run_migrations(
    database_url: DatabaseOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Apply any pending schema migrations. Safe to run repeatedly."""
    settings = _settings(database_url=database_url, verbose=verbose)
    try:
        applied = migrate.upgrade(settings.database_url)
    except Exception as exc:
        console.print(f"[red]Migration failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if applied:
        console.print(f"[green]Applied[/green] {len(applied)} migration(s):")
        for name in applied:
            console.print(f"  {name}")
    else:
        console.print("[green]Schema already up to date.[/green]")


@app.command("migration-status")
def migration_status(database_url: DatabaseOption = None) -> None:
    """Show which migrations have been applied and which are pending."""
    settings = _settings(database_url=database_url)
    state = migrate.status(settings.database_url)

    table = Table(title="Migrations", box=None)
    table.add_column("state")
    table.add_column("migration")
    for name in state.applied:
        table.add_row("[green]applied[/green]", name)
    for name in state.pending:
        table.add_row("[yellow]pending[/yellow]", name)
    console.print(table)
    console.print(f"\n{redact(settings.database_url)}")
    if not state.is_up_to_date:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@app.command()
def ingest(
    database_url: DatabaseOption = None,
    house: HouseOption = None,
    start: StartOption = None,
    end: EndOption = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="Skip sitting days already ingested completely.")
    ] = False,
    limit_days: Annotated[
        int | None,
        typer.Option("--limit-days", help="Stop after N sitting days; useful for a smoke test."),
    ] = None,
    verbose: VerboseOption = False,
    json_logs: JsonLogsOption = False,
) -> None:
    """Fetch debates and transcripts from Hansard."""
    settings = _settings(
        database_url=database_url,
        house=house,
        start=start,
        end=end,
        verbose=verbose,
        json_logs=json_logs,
    )
    console.print(
        f"[bold]{settings.house}[/bold] {settings.start_date} to {settings.end_date} "
        f"-> {redact(settings.database_url)}"
    )
    with _job_context(settings, progress_label="sitting days") as context:
        report = jobs.run_job(
            "hansard.debates",
            lambda ctx: jobs.ingest_debates(ctx, resume=resume, limit_days=limit_days),
            context,
        )
    _finish(report)


@app.command()
def divisions(
    database_url: DatabaseOption = None,
    house: HouseOption = None,
    start: StartOption = None,
    end: EndOption = None,
    verbose: VerboseOption = False,
    json_logs: JsonLogsOption = False,
) -> None:
    """Fetch division results for debates already stored."""
    settings = _settings(
        database_url=database_url,
        house=house,
        start=start,
        end=end,
        verbose=verbose,
        json_logs=json_logs,
    )
    with _job_context(settings, progress_label="debates with divisions") as context:
        report = jobs.run_job("hansard.divisions", jobs.ingest_divisions, context)
    _finish(report)


@app.command()
def reattribute(
    database_url: DatabaseOption = None,
    verbose: VerboseOption = False,
    json_logs: JsonLogsOption = False,
) -> None:
    """Attribute continuation paragraphs to the speaker who began the speech.

    Hansard names the speaker on the first paragraph of a speech only. This
    resolves the rest against the stored transcript -- no refetching, because
    the text is already here and the API does not carry the attribution either.
    """
    settings = _settings(database_url=database_url, verbose=verbose, json_logs=json_logs)
    with _job_context(settings, progress_label="debates") as context:
        report = jobs.run_job("reattribute", jobs.reattribute, context)
    _finish(report)


@app.command("sync-members")
def sync_members(
    database_url: DatabaseOption = None,
    house: HouseOption = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Re-walk every member rather than only unsynced ones."),
    ] = False,
    verbose: VerboseOption = False,
    json_logs: JsonLogsOption = False,
) -> None:
    """Fill in member details from the Parliament Members API (source two)."""
    settings = _settings(
        database_url=database_url, house=house, verbose=verbose, json_logs=json_logs
    )
    with _job_context(settings, progress_label="members") as context:
        report = jobs.run_job(
            "members.sync", lambda ctx: jobs.sync_members(ctx, full=full), context
        )
    _finish(report)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@app.command()
def schedule(
    database_url: DatabaseOption = None,
    house: HouseOption = None,
    now: Annotated[
        bool,
        typer.Option("--now", help="Run every scheduled job once immediately, then exit."),
    ] = False,
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Restrict to named jobs.")
    ] = None,
    verbose: VerboseOption = False,
    json_logs: JsonLogsOption = False,
) -> None:
    """Run the pipeline's jobs on their schedule.

    Blocks until interrupted. Each firing opens its own database connection and
    HTTP clients, so a job that fails cannot poison the ones after it.
    """
    settings = _settings(
        database_url=database_url, house=house, verbose=verbose, json_logs=json_logs
    )
    migrate.upgrade(settings.database_url)

    def runner(name: str) -> None:
        function = jobs.JOBS[name]
        with _job_context(settings) as context:
            jobs.run_job(name, function, context)

    selected = {
        name: cron for name, cron in DEFAULT_SCHEDULE.items() if not only or name in set(only)
    }
    if not selected:
        console.print(f"[red]No jobs matched.[/red] Known: {', '.join(DEFAULT_SCHEDULE)}")
        raise typer.Exit(code=2)

    scheduled = build_jobs(settings, runner, selected)

    table = Table(title="Schedule", box=None)
    table.add_column("job")
    table.add_column("cron")
    table.add_column("next run (UTC)")
    for job in scheduled:
        table.add_row(job.name, job.cron, str(job.next_run()))
    console.print(table)

    if now:
        console.print("\nRunning every job once...\n")
        run_once_now(scheduled)
    else:
        console.print("\nRunning. Ctrl-C to stop.\n")
        run_forever(scheduled)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _latest_data_date(connection: Connection[DictRow]) -> date | None:
    """The most recent sitting we hold.

    Relative periods in a question are anchored to this rather than to today.
    The store covers January to April 2026, so "last month" measured against the
    real clock would return nothing and look like an empty database.
    """
    row = connection.execute("SELECT MAX(sitting_date) AS d FROM debate").fetchone()
    return row["d"] if row else None


@app.command("index")
def build_index_command(
    database_url: DatabaseOption = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Continue an interrupted build instead of starting over."),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Stop after this many new speeches, then exit."),
    ] = None,
    verbose: VerboseOption = False,
) -> None:
    """Rebuild the local vector index from the stored speeches.

    Embedding the whole corpus takes the better part of an hour. If it is
    interrupted, `--resume` picks up where it stopped rather than starting again.
    """
    settings = _settings(database_url=database_url, verbose=verbose)
    console.print(
        f"Embedding with [bold]{settings.embedding_model}[/bold] -> {settings.vector_path}"
    )

    with (
        _database(settings) as connection,
        Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress,
    ):
        task = progress.add_task("chunks", total=None)

        def on_progress(label: str, done: int, total: int) -> None:
            progress.update(task, total=total, completed=done, description=label)

        report = rag_build.build_index(
            connection,
            settings,
            resume=resume,
            limit=limit,
            on_progress=on_progress,
        )

    table = Table(title="Index built", show_header=False, box=None)
    table.add_row("speeches", f"{report.turns:,}")
    table.add_row("chunks", f"{report.chunks:,}")
    if report.remaining:
        table.add_row("remaining", f"{report.remaining:,}  (run again with --resume)")
    table.add_row("dimensions", str(report.dimensions))
    table.add_row("seconds", f"{report.seconds:,.1f}")
    console.print(table)


@app.command("index-status")
def index_status_command(database_url: DatabaseOption = None) -> None:
    """Show what the vector index holds and whether it has fallen behind."""
    settings = _settings(database_url=database_url)
    with _database(settings) as connection:
        expected = rag_build.expected_turn_ids(connection)
    status = rag_index.index_status(settings.vector_path, expected_turn_ids=expected)

    if not status.exists:
        console.print("[yellow]No index yet.[/yellow] Run `hansard index`.")
        raise typer.Exit(code=1)

    table = Table(title="Vector index", show_header=False, box=None)
    table.add_row("chunks", f"{status.chunks:,}")
    table.add_row("speeches", f"{status.speeches:,}")
    table.add_row("covers", f"{status.earliest} to {status.latest}")
    table.add_row("built", str(status.built_at))
    table.add_row("index version", f"{status.version} (code expects {rag_index.INDEX_VERSION})")
    console.print(table)

    if status.is_stale:
        console.print("")
        console.print(
            f"[yellow]Stale:[/yellow] {status.missing_turns:,} speeches in the "
            "database are not indexed. Run `hansard index`."
        )
        raise typer.Exit(code=1)
    console.print("")
    console.print("[green]Up to date.[/green]")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="A question about the debates.")],
    database_url: DatabaseOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Passages to retrieve.")] = 8,
    naive: Annotated[
        bool, typer.Option("--naive", help="Vector search only, no filters -- for comparison.")
    ] = False,
    passages_only: Annotated[
        bool, typer.Option("--passages-only", help="Show retrieved passages, skip the model.")
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """Ask a question, answered from the stored debates."""
    settings = _settings(database_url=database_url, verbose=verbose)

    with _database(settings) as connection:
        latest = _latest_data_date(connection)
        passages, filters = rag_retrieve.retrieve(
            connection,
            settings,
            question,
            limit=limit,
            naive=naive,
            latest_data_date=latest,
        )

    for note in filters.notes:
        console.print(f"[dim]{escape(note)}[/dim]")

    if passages_only or not passages:
        if not passages:
            console.print("")
            console.print("[yellow]No passages matched.[/yellow]")
        for number, passage in enumerate(passages, start=1):
            console.print("")
            console.print(
                f"[bold]{number}.[/bold] [cyan]{escape(passage.citation())}[/cyan] "
                f"[dim]({'+'.join(passage.sources)})[/dim]"
            )
            console.print("   " + escape(passage.text[:320]))
        return

    try:
        result = rag_answer.answer_question(
            settings, question, passages, filters, latest_data_date=latest
        )
    except rag_answer.AnswerError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    console.print("")
    console.print(escape(result.text))
    console.print("")
    console.print("[dim]Sources:[/dim]")
    for number, passage in enumerate(result.passages, start=1):
        console.print(f"[dim]  [{number}] {escape(passage.citation())}[/dim]")
    console.print(f"[dim]{result.model}, {result.tokens:,} tokens[/dim]")


@app.command()
def serve(
    database_url: DatabaseOption = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
    verbose: VerboseOption = False,
) -> None:
    """Run the local web UI for asking questions."""
    import uvicorn

    from hansard.web.app import create_app

    settings = _settings(database_url=database_url, verbose=verbose)
    console.print(f"[green]http://{host}:{port}[/green]  (Ctrl-C to stop)")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@app.command()
def check(database_url: DatabaseOption = None, verbose: VerboseOption = False) -> None:
    """Run the integrity checks. Exits non-zero if any fail."""
    settings = _settings(database_url=database_url, verbose=verbose)
    with _database(settings) as connection:
        results = checks.run_all(connection)

    table = Table(title="Integrity checks", box=None)
    table.add_column("")
    table.add_column("check")
    table.add_column("detail")
    for result in results:
        mark = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        table.add_row(mark, result.name, result.detail)
    console.print(table)

    for result in results:
        for example in result.examples:
            console.print(f"  [yellow]{result.name}:[/yellow] {example}")

    if any(not result.passed for result in results):
        raise typer.Exit(code=1)


@app.command()
def stats(database_url: DatabaseOption = None) -> None:
    """Summarise what is currently in the store."""
    settings = _settings(database_url=database_url)
    with _database(settings) as connection:
        summary = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM sitting_day)                    AS days,
                (SELECT COUNT(*) FROM debate)                         AS debates,
                (SELECT COUNT(*) FROM contribution)                   AS items,
                (SELECT COUNT(*) FROM contribution WHERE is_speech)   AS speeches,
                (SELECT COALESCE(SUM(word_count),0) FROM contribution) AS words,
                (SELECT COUNT(*) FROM member)                         AS members,
                (SELECT COUNT(*) FROM division)                       AS divisions,
                (SELECT COUNT(*) FROM division_vote)                  AS votes,
                (SELECT MIN(sitting_date) FROM debate)                AS first_day,
                (SELECT MAX(sitting_date) FROM debate)                AS last_day,
                pg_size_pretty(pg_database_size(current_database()))  AS size
            """
        ).fetchone()
        parties = connection.execute(
            """
            SELECT COALESCE(party, '(unstated)') AS party, COUNT(*) AS speeches
              FROM speech GROUP BY 1 ORDER BY 2 DESC LIMIT 8
            """
        ).fetchall()
        speakers = connection.execute(
            """
            SELECT name, party, contribution_count
              FROM member_resolved ORDER BY contribution_count DESC LIMIT 8
            """
        ).fetchall()
        cover = members_store.coverage(connection)
        latest = runs.recent(connection, limit=5)

    assert summary is not None
    table = Table(title="Store contents", show_header=False, box=None)
    table.add_row("sitting days", f"{summary['days']:,}")
    table.add_row("debates", f"{summary['debates']:,}")
    table.add_row("transcript rows", f"{summary['items']:,}")
    table.add_row("  of which speeches", f"{summary['speeches']:,}")
    table.add_row("words of speech", f"{summary['words']:,}")
    table.add_row("divisions", f"{summary['divisions']:,}")
    table.add_row("  votes cast", f"{summary['votes']:,}")
    table.add_row("members", f"{summary['members']:,}")
    table.add_row("date range", f"{summary['first_day']} to {summary['last_day']}")
    table.add_row("database size", str(summary["size"]))
    console.print(table)

    coverage_table = Table(title="Source coverage", show_header=False, box=None)
    coverage_table.add_row(
        "members synced from Members API",
        f"{cover['synced']:,} / {cover['members']:,}",
    )
    coverage_table.add_row(
        "members with a party",
        f"{cover['party_resolved']:,} / {cover['members']:,}"
        f"  (Hansard alone: {cover['party_from_hansard']:,})",
    )
    if cover["speeches_attributed"]:
        share = cover["attributed_with_party"] / cover["speeches_attributed"] * 100
        coverage_table.add_row(
            "attributed speeches with a party",
            f"{cover['attributed_with_party']:,} / {cover['speeches_attributed']:,}"
            f"  ({share:.1f}%)",
        )
        coverage_table.add_row(
            "speeches Hansard attributes to nobody",
            f"{cover['speeches_unattributed']:,}  (procedural text; no party is possible)",
        )
    coverage_table.add_row(
        "members only source two could name",
        f"{cover['party_only_from_api']:,}",
    )
    console.print(coverage_table)

    if parties:
        party_table = Table(title="Speeches by party", box=None)
        party_table.add_column("party")
        party_table.add_column("speeches", justify="right")
        for row in parties:
            party_table.add_row(row["party"], f"{row['speeches']:,}")
        console.print(party_table)

    if speakers:
        speaker_table = Table(title="Most frequent speakers", box=None)
        speaker_table.add_column("member")
        speaker_table.add_column("party")
        speaker_table.add_column("speeches", justify="right")
        for row in speakers:
            speaker_table.add_row(
                row["name"] or "", row["party"] or "", f"{row['contribution_count']:,}"
            )
        console.print(speaker_table)

    if latest:
        run_table = Table(title="Recent runs", box=None)
        for column in ("job", "status", "started", "errors"):
            run_table.add_column(column)
        for row in latest:
            colour = {"completed": "green", "failed": "red"}.get(row["status"], "yellow")
            run_table.add_row(
                row["job"],
                f"[{colour}]{row['status']}[/{colour}]",
                row["started_at"].strftime("%Y-%m-%d %H:%M"),
                str(row["errors"]),
            )
        console.print(run_table)


@app.command()
def report(database_url: DatabaseOption = None) -> None:
    """Data-quality observations about the sources themselves.

    Distinct from ``check``: these are inconsistencies in the upstream data that
    we can describe but not fix, so they are reported rather than failed.
    """
    settings = _settings(database_url=database_url)
    with _database(settings) as connection:
        discrepancies = divisions_store.count_discrepancies(connection)
        total = connection.execute("SELECT COUNT(*) AS n FROM division").fetchone()

    count = total["n"] if total else 0
    console.print(
        f"[bold]Division counts[/bold]: {len(discrepancies):,} of {count:,} divisions "
        "state a total that differs from the members Hansard listed."
    )
    if discrepancies:
        table = Table(box=None)
        for column in ("date", "section", "stated", "stored", "delta"):
            table.add_column(column)
        for row in discrepancies[:10]:
            stated = f"{row['ayes_count']}/{row['noes_count']}"
            stored = f"{row['ayes_stored']}/{row['noes_stored']}"
            delta = (
                f"{row['ayes_count'] - row['ayes_stored']:+d}/"
                f"{row['noes_count'] - row['noes_stored']:+d}"
            )
            table.add_row(
                str(row["division_date"]), (row["debate_section"] or "")[:40], stated, stored, delta
            )
        console.print(table)
        console.print(
            "\n[dim]Excluding tellers accounts for most of these but not all; the "
            "remainder follow no consistent rule. Both figures are stored, so "
            "neither is lost.[/dim]"
        )


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search terms, e.g. 'housing -rent'.")],
    database_url: DatabaseOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Rows to show.")] = 10,
) -> None:
    """Full-text search the stored speeches.

    Uses Postgres ``websearch_to_tsquery``, so the syntax is the one people
    already know from search engines: bare words, "quoted phrases", OR, and a
    leading minus to exclude.
    """
    settings = _settings(database_url=database_url)
    with _database(settings) as connection:
        # MaxFragments=1 keeps the excerpt contiguous: without it a phrase query
        # returns each matched word as its own fragment, which reads as broken
        # text rather than a quotation.
        #
        # The delimiters are guillemets, not square brackets, because Rich reads
        # "[cost]" as console markup and silently deletes it -- which made
        # highlighted words vanish from the output entirely. They are swapped for
        # real markup below, after the surrounding text has been escaped.
        headline_options = (
            f"MaxFragments=1, MaxWords=30, MinWords=15, "
            f"StartSel={HIGHLIGHT_START}, StopSel={HIGHLIGHT_END}"
        )
        rows = connection.execute(
            """
            SELECT s.sitting_date, s.debate_title, s.speaker_name, s.party,
                   ts_headline('english', s.body_text, websearch_to_tsquery('english', %(q)s),
                               %(options)s) AS excerpt,
                   ts_rank(s.body_tsv, websearch_to_tsquery('english', %(q)s)) AS rank
              FROM speech AS s
             WHERE s.body_tsv @@ websearch_to_tsquery('english', %(q)s)
             ORDER BY rank DESC, s.sitting_date DESC
             LIMIT %(limit)s
            """,
            {"q": query, "limit": limit, "options": headline_options},
        ).fetchall()
        total = connection.execute(
            """
            SELECT COUNT(*) AS n FROM speech
             WHERE body_tsv @@ websearch_to_tsquery('english', %s)
            """,
            (query,),
        ).fetchone()

    if not rows:
        console.print(f"No matches for [bold]{query}[/bold].")
        return

    console.print(
        f"[bold]{total['n'] if total else 0:,}[/bold] matching speeches; showing {len(rows)}.\n"
    )
    for row in rows:
        # Everything from the database is escaped: a speech or a debate title
        # containing square brackets would otherwise be read as Rich markup and
        # silently disappear from the output.
        speaker = escape(row["speaker_name"] or "unattributed")
        party = f" ({escape(row['party'])})" if row["party"] else ""
        title = escape(row["debate_title"] or "")
        console.print(f"[cyan]{row['sitting_date']}[/cyan]  {title}")
        console.print(f"  [bold]{speaker}[/bold]{party}: {_highlight(row['excerpt'])}\n")


@app.command()
def votes(
    member_id: Annotated[int, typer.Argument(help="Parliament member id, e.g. 4514.")],
    database_url: DatabaseOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """How one member voted, joining source one and source two on member_id."""
    settings = _settings(database_url=database_url)
    with _database(settings) as connection:
        who = connection.execute(
            "SELECT name, party, constituency FROM member_resolved WHERE member_id = %s",
            (member_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT d.division_date, d.debate_section, v.lobby, v.is_teller,
                   d.ayes_count, d.noes_count
              FROM division_vote AS v
              JOIN division AS d ON d.ext_id = v.division_ext_id
             WHERE v.member_id = %s
             ORDER BY d.division_date DESC, d.division_time DESC
             LIMIT %s
            """,
            (member_id, limit),
        ).fetchall()

    if who is None:
        console.print(f"No member {member_id} in the store.")
        raise typer.Exit(code=1)

    party = f" ({who['party']})" if who["party"] else ""
    seat = f" — {who['constituency']}" if who["constituency"] else ""
    console.print(f"[bold]{who['name']}[/bold]{party}{seat}\n")
    if not rows:
        console.print("No recorded votes in the stored range.")
        return

    table = Table(box=None)
    for column in ("date", "division", "vote", "result"):
        table.add_column(column)
    for row in rows:
        vote = row["lobby"].upper() + (" (teller)" if row["is_teller"] else "")
        colour = "green" if row["lobby"] == "aye" else "red"
        table.add_row(
            str(row["division_date"]),
            (row["debate_section"] or "")[:44],
            f"[{colour}]{vote}[/{colour}]",
            f"{row['ayes_count']}-{row['noes_count']}",
        )
    console.print(table)


def main() -> None:  # pragma: no cover
    # Loaded here, at the console entry point, rather than in Settings.from_env:
    # tests build settings directly, and a developer's local .env must not leak
    # into them. A real environment variable still beats the file.
    load_dotenv(find_dotenv(usecwd=True))
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
