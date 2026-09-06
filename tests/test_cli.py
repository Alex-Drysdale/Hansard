"""Tests for the command line surface.

The CLI is thin, so these test the wiring rather than the logic: that each
command reaches the right layer, reports the right exit code, and does not fall
over on an empty database.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hansard.api.models import DebateDetail
from hansard.cli import _settings, app
from hansard.config import Settings
from hansard.db import store
from hansard.db.connection import connect, initialise, transaction
from hansard.pipeline.normalise import normalise_debate

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests off the developer's real database and real environment."""
    for name in (
        "HANSARD_HOUSE",
        "HANSARD_START_DATE",
        "HANSARD_END_DATE",
        "HANSARD_BASE_URL",
        "HANSARD_REQUESTS_PER_SECOND",
        "HANSARD_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HANSARD_DB_PATH", str(tmp_path / "cli.db"))


@pytest.fixture
def database(tmp_path: Path, debate_with_speeches: DebateDetail) -> Path:
    """A database file holding one complete sitting day."""
    path = tmp_path / "cli.db"
    connection: sqlite3.Connection = connect(path)
    initialise(connection)
    with transaction(connection):
        store.record_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=1
        )
        store.save_debate(connection, normalise_debate(debate_with_speeches))
        store.complete_sitting_day(connection, house="Commons", sitting_date=date(2026, 1, 14))
        store.refresh_member_counts(connection)
    connection.close()
    return path


class TestInitDb:
    def test_creates_the_schema(self, tmp_path: Path) -> None:
        target = tmp_path / "new" / "hansard.db"
        result = runner.invoke(app, ["init-db", "--database", str(target)])

        assert result.exit_code == 0, result.output
        assert target.exists()
        assert "debate" in result.output

    def test_is_safe_to_run_twice(self, tmp_path: Path) -> None:
        target = tmp_path / "hansard.db"
        runner.invoke(app, ["init-db", "--database", str(target)])
        result = runner.invoke(app, ["init-db", "--database", str(target)])

        assert result.exit_code == 0, result.output


class TestCheck:
    def test_passes_on_a_consistent_store(self, database: Path) -> None:
        result = runner.invoke(app, ["check", "--database", str(database)])

        assert result.exit_code == 0, result.output
        assert "FAIL" not in result.output

    def test_exits_non_zero_when_a_check_fails(self, database: Path) -> None:
        # A non-zero exit is what makes this usable as a CI gate.
        connection = connect(database)
        connection.execute("UPDATE sitting_day SET debate_count = 99")
        connection.close()

        result = runner.invoke(app, ["check", "--database", str(database)])

        assert result.exit_code == 1
        assert "FAIL" in result.output


class TestStats:
    def test_summarises_the_store(self, database: Path) -> None:
        result = runner.invoke(app, ["stats", "--database", str(database)])

        assert result.exit_code == 0, result.output
        assert "debates" in result.output
        assert "Martin Vickers" in result.output

    def test_copes_with_an_empty_store(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.db"
        runner.invoke(app, ["init-db", "--database", str(target)])

        result = runner.invoke(app, ["stats", "--database", str(target)])

        assert result.exit_code == 0, result.output


class TestSearch:
    def test_finds_a_stored_speech(self, database: Path) -> None:
        result = runner.invoke(app, ["search", "refinery", "--database", str(database)])

        assert result.exit_code == 0, result.output
        assert "Oil Refining Sector" in result.output

    def test_reports_no_matches_plainly(self, database: Path) -> None:
        result = runner.invoke(app, ["search", "zzzznotaword", "--database", str(database)])

        assert result.exit_code == 0, result.output
        assert "No matches" in result.output

    def test_a_malformed_query_is_explained_not_traced(self, database: Path) -> None:
        result = runner.invoke(app, ["search", 'AND OR "', "--database", str(database)])

        assert result.exit_code == 2
        assert "Invalid FTS query" in result.output


class TestArgumentHandling:
    def test_an_unknown_house_is_rejected_before_any_fetching(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "--house", "Senate", "--database", str(tmp_path / "x.db")]
        )
        assert result.exit_code != 0

    def test_dates_must_be_iso(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "--start", "01/02/2026", "--database", str(tmp_path / "x.db")]
        )
        assert result.exit_code != 0

    def test_help_lists_every_command(self) -> None:
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        for command in ("init-db", "ingest", "check", "stats", "search"):
            assert command in result.output


class TestSettingsResolution:
    def test_cli_overrides_beat_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HANSARD_HOUSE", "Lords")
        resolved = _settings(house="Commons", start=None, end=None, database=None)
        assert resolved.house == "Commons"

    def test_unspecified_options_fall_back_to_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HANSARD_HOUSE", "Lords")
        assert _settings(house=None, start=None, end=None, database=None).house == "Lords"

    def test_settings_not_exposed_on_the_cli_are_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regression this guards: rebuilding Settings field by field would
        # silently reset anything the CLI does not name.
        monkeypatch.setenv("HANSARD_REQUESTS_PER_SECOND", "1.5")
        resolved = _settings(house="Commons", start=None, end=None, database=None)
        assert resolved.requests_per_second == 1.5
        assert resolved.backoff_max_seconds == Settings().backoff_max_seconds
