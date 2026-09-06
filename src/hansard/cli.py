"""Command line interface.

Thin by design: parse arguments, wire the layers together, print the result.
Anything a test would want to assert on lives in the modules underneath.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from hansard.api.client import HansardClient
from hansard.config import Settings
from hansard.db import store
from hansard.db.connection import connect, initialise, open_database
from hansard.pipeline import checks
from hansard.pipeline.ingest import DayProgress, ingest_range

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Ingest UK Parliament Hansard debates into a local SQLite store.",
)
console = Console()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _as_date(value: datetime | None) -> date | None:
    """Typer parses dates as datetimes; the rest of the code wants a date."""
    return value.date() if value is not None else None


def _settings(
    house: str | None,
    start: datetime | None,
    end: datetime | None,
    database: Path | None,
) -> Settings:
    """Environment defaults, overridden by whatever the user passed on the CLI.

    ``replace`` rather than rebuilding the object field by field: a hand-written
    constructor call silently resets any setting it forgets to mention, so
    adding one to Settings would quietly break the CLI.
    """
    overrides: dict[str, object] = {}
    if database is not None:
        overrides["database_path"] = database
    if house is not None:
        overrides["house"] = house
    if (start_date := _as_date(start)) is not None:
        overrides["start_date"] = start_date
    if (end_date := _as_date(end)) is not None:
        overrides["end_date"] = end_date
    return replace(Settings.from_env(), **overrides)  # type: ignore[arg-type]


DatabaseOption = Annotated[
    Path | None, typer.Option("--database", "-d", help="Path to the SQLite file.")
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


@app.command("init-db")
def init_db(database: DatabaseOption = None, verbose: VerboseOption = False) -> None:
    """Create the database and its schema. Safe to run on an existing file."""
    _configure_logging(verbose)
    settings = _settings(None, None, None, database)
    connection = connect(settings.database_path)
    try:
        initialise(connection)
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()

    console.print(f"[green]Ready[/green] {settings.database_path}")
    console.print("  " + ", ".join(row["name"] for row in tables))


@app.command()
def ingest(
    database: DatabaseOption = None,
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
) -> None:
    """Fetch a date range of debates from Hansard into the local store."""
    _configure_logging(verbose)
    settings = _settings(house, start, end, database)

    console.print(
        f"[bold]{settings.house}[/bold] {settings.start_date} to {settings.end_date} "
        f"-> {settings.database_path}"
    )

    with (
        open_database(settings.database_path) as connection,
        HansardClient(settings) as client,
        Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress,
    ):
        task = progress.add_task("sitting days", total=None)

        def on_progress(update: DayProgress) -> None:
            progress.update(
                task,
                total=update.total,
                completed=update.index,
                description=f"{update.sitting_date} ({update.debates} debates)",
            )

        report = ingest_range(
            connection,
            client,
            settings,
            resume=resume,
            limit_days=limit_days,
            on_progress=on_progress,
        )

    table = Table(title="Ingest summary", show_header=False, box=None)
    table.add_row("sitting days", f"{report.sitting_days:,}")
    table.add_row("debates seen", f"{report.debates_seen:,}")
    table.add_row("  inserted", f"{report.debates_inserted:,}")
    table.add_row("  updated", f"{report.debates_updated:,}")
    table.add_row("  unchanged", f"{report.debates_unchanged:,}")
    table.add_row("contributions written", f"{report.contributions_written:,}")
    table.add_row("errors", f"{report.errors:,}")
    console.print(table)

    for failure in report.failures[:5]:
        console.print(f"  [yellow]![/yellow] {failure}")
    if report.errors:
        raise typer.Exit(code=1)


@app.command()
def check(database: DatabaseOption = None, verbose: VerboseOption = False) -> None:
    """Run the integrity and duplicate checks against the store."""
    _configure_logging(verbose)
    settings = _settings(None, None, None, database)

    with open_database(settings.database_path) as connection:
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
def stats(database: DatabaseOption = None) -> None:
    """Summarise what is currently in the store."""
    settings = _settings(None, None, None, database)

    with open_database(settings.database_path) as connection:
        summary = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM sitting_day)                     AS days,
                (SELECT COUNT(*) FROM debate)                          AS debates,
                (SELECT COUNT(*) FROM contribution)                    AS items,
                (SELECT COUNT(*) FROM contribution WHERE is_speech=1)  AS speeches,
                (SELECT COALESCE(SUM(word_count),0) FROM contribution) AS words,
                (SELECT COUNT(*) FROM member)                          AS members,
                (SELECT MIN(sitting_date) FROM debate)                 AS first_day,
                (SELECT MAX(sitting_date) FROM debate)                 AS last_day
            """
        ).fetchone()

        parties = connection.execute(
            """
            SELECT COALESCE(party, '(unstated)') AS party, COUNT(*) AS speeches
              FROM speech
             GROUP BY party ORDER BY speeches DESC LIMIT 8
            """
        ).fetchall()

        speakers = connection.execute(
            """
            SELECT display_name, party, contribution_count
              FROM member ORDER BY contribution_count DESC LIMIT 8
            """
        ).fetchall()

        run = store.latest_run(connection)

    table = Table(title="Store contents", show_header=False, box=None)
    table.add_row("sitting days", f"{summary['days']:,}")
    table.add_row("debates", f"{summary['debates']:,}")
    table.add_row("transcript rows", f"{summary['items']:,}")
    table.add_row("  of which speeches", f"{summary['speeches']:,}")
    table.add_row("words of speech", f"{summary['words']:,}")
    table.add_row("members", f"{summary['members']:,}")
    table.add_row("date range", f"{summary['first_day']} to {summary['last_day']}")
    if settings.database_path.exists():
        table.add_row("file size", f"{settings.database_path.stat().st_size / 1e6:,.1f} MB")
    console.print(table)

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
                row["display_name"], row["party"] or "", f"{row['contribution_count']:,}"
            )
        console.print(speaker_table)

    if run is not None:
        console.print(
            f"\nlast run #{run['id']}: {run['status']} "
            f"({run['started_at']} -> {run['finished_at'] or 'unfinished'})"
        )


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="FTS5 query, e.g. 'climate NEAR/5 target'.")],
    database: DatabaseOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Rows to show.")] = 10,
) -> None:
    """Full-text search the stored speeches."""
    settings = _settings(None, None, None, database)

    with open_database(settings.database_path) as connection:
        try:
            rows = connection.execute(
                """
                SELECT c.sitting_date, c.debate_title, c.speaker_name, c.party,
                       snippet(contribution_fts, 0, '[', ']', ' ... ', 16) AS excerpt
                  FROM contribution_fts
                  JOIN speech AS c ON c.item_id = contribution_fts.rowid
                 WHERE contribution_fts MATCH ?
                 ORDER BY rank
                 LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            console.print(f"[red]Invalid FTS query:[/red] {exc}")
            raise typer.Exit(code=2) from exc

        total = connection.execute(
            """
            SELECT COUNT(*) AS n
              FROM contribution_fts
              JOIN speech AS c ON c.item_id = contribution_fts.rowid
             WHERE contribution_fts MATCH ?
            """,
            (query,),
        ).fetchone()["n"]

    if not rows:
        console.print(f"No matches for [bold]{query}[/bold].")
        return

    console.print(f"[bold]{total:,}[/bold] matching speeches; showing {len(rows)}.\n")
    for row in rows:
        speaker = row["speaker_name"] or "unattributed"
        party = f" ({row['party']})" if row["party"] else ""
        console.print(f"[cyan]{row['sitting_date']}[/cyan]  {row['debate_title']}")
        console.print(f"  [bold]{speaker}[/bold]{party}: {row['excerpt']}\n")


if __name__ == "__main__":  # pragma: no cover
    app()
