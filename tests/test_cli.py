"""Tests for the command line surface.

The CLI is thin, so these test the wiring rather than the logic: that each
command reaches the right layer, reports the right exit code, and fails
usefully when Postgres is not there.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from psycopg import Connection
from psycopg.rows import DictRow
from typer.testing import CliRunner

from hansard.api.models import DebateDetail, DivisionDetail
from hansard.cli import _settings, app
from hansard.config import Settings
from hansard.db import debates as debates_store
from hansard.db import divisions as divisions_store
from hansard.pipeline.normalise import normalise_debate, normalise_division

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """Point every command at the test database, never the development one."""
    for name in (
        "HANSARD_HOUSE",
        "HANSARD_START_DATE",
        "HANSARD_END_DATE",
        "HANSARD_BASE_URL",
        "MEMBERS_BASE_URL",
        "HANSARD_REQUESTS_PER_SECOND",
        "HANSARD_MAX_RETRIES",
        "HANSARD_LOG_FORMAT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HANSARD_DATABASE_URL", database_url)


@pytest.fixture
def populated(
    clean_connection: Connection[DictRow],
    debate_with_speeches: DebateDetail,
    division_detail: DivisionDetail,
) -> Connection[DictRow]:
    """A small but complete store: a sitting day, a debate, and a division."""
    connection = clean_connection
    debates_store.record_sitting_day(
        connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=1
    )
    debates_store.save_debate(connection, normalise_debate(debate_with_speeches))
    debates_store.complete_sitting_day(connection, house="Commons", sitting_date=date(2026, 1, 14))
    debates_store.refresh_member_counts(connection)
    divisions_store.save_division(connection, normalise_division(division_detail))
    return connection


class TestMigrations:
    def test_migrate_is_safe_to_run_again(self) -> None:
        result = runner.invoke(app, ["migrate"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output

    def test_status_lists_applied_migrations(self) -> None:
        result = runner.invoke(app, ["migration-status"])
        assert result.exit_code == 0, result.output
        assert "0001_initial_schema" in result.output
        assert "applied" in result.output

    def test_status_never_prints_the_password(self) -> None:
        result = runner.invoke(app, ["migration-status"])
        assert "hansard:hansard" not in result.output
        assert "***" in result.output


class TestCheck:
    def test_passes_on_a_consistent_store(self, populated: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["check"])
        assert result.exit_code == 0, result.output
        assert "FAIL" not in result.output

    def test_exits_non_zero_when_a_check_fails(self, populated: Connection[DictRow]) -> None:
        # A non-zero exit is what makes this usable as a scheduled gate.
        populated.execute("UPDATE sitting_day SET debate_count = 99")
        result = runner.invoke(app, ["check"])
        assert result.exit_code == 1
        assert "FAIL" in result.output


class TestStats:
    def test_summarises_the_store(self, populated: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "debates" in result.output
        assert "Martin Vickers" in result.output

    def test_reports_source_coverage(self, populated: Connection[DictRow]) -> None:
        # The Phase 2 headline: how much of the store source two has reached.
        result = runner.invoke(app, ["stats"])
        assert "Source coverage" in result.output
        assert "attributed speeches with a party" in result.output
        # The denominator is stated, not implied: unattributed speeches can
        # never have a party and are reported separately rather than counted
        # against coverage.
        assert "attributes to nobody" in result.output

    def test_copes_with_an_empty_store(self, clean_connection: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output


class TestReport:
    def test_surfaces_the_count_discrepancy(self, populated: Connection[DictRow]) -> None:
        # Reported, not failed: the disagreement is upstream's, not ours.
        result = runner.invoke(app, ["report"])
        assert result.exit_code == 0, result.output
        assert "Division counts" in result.output

    def test_says_so_when_there_is_nothing_to_report(
        self, clean_connection: Connection[DictRow]
    ) -> None:
        result = runner.invoke(app, ["report"])
        assert result.exit_code == 0, result.output
        assert "0 of 0" in result.output


class TestSearch:
    def test_finds_a_stored_speech(self, populated: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["search", "refinery"])
        assert result.exit_code == 0, result.output
        assert "Oil Refining Sector" in result.output

    def test_supports_websearch_syntax(self, populated: Connection[DictRow]) -> None:
        # Postgres websearch_to_tsquery, so quoted phrases and - work as people
        # expect from a search engine -- unlike SQLite FTS5's own grammar.
        result = runner.invoke(app, ["search", '"oil refinery" -zebra'])
        assert result.exit_code == 0, result.output

    def test_reports_no_matches_plainly(self, populated: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["search", "zzzznotaword"])
        assert result.exit_code == 0, result.output
        assert "No matches" in result.output


class TestVotes:
    def test_shows_how_a_member_voted(
        self, populated: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        member_id = division_detail.aye_members[0].member_id
        result = runner.invoke(app, ["votes", str(member_id)])
        assert result.exit_code == 0, result.output
        assert "AYE" in result.output

    def test_an_unknown_member_exits_non_zero(self, populated: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["votes", "99999999"])
        assert result.exit_code == 1
        assert "No member" in result.output


class TestSchedule:
    def test_lists_the_schedule_and_runs_once(self, clean_connection: Connection[DictRow]) -> None:
        result = runner.invoke(app, ["schedule", "--only", "checks", "--now"])
        assert result.exit_code == 0, result.output
        assert "checks" in result.output
        assert "next run" in result.output

    def test_an_unknown_job_name_is_rejected(self) -> None:
        result = runner.invoke(app, ["schedule", "--only", "not-a-job"])
        assert result.exit_code == 2
        assert "No jobs matched" in result.output


class TestArgumentHandling:
    def test_an_unknown_house_is_rejected_before_any_fetching(self) -> None:
        result = runner.invoke(app, ["ingest", "--house", "Senate"])
        assert result.exit_code != 0

    def test_dates_must_be_iso(self) -> None:
        result = runner.invoke(app, ["ingest", "--start", "01/02/2026"])
        assert result.exit_code != 0

    def test_help_lists_every_command(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in (
            "migrate",
            "ingest",
            "divisions",
            "sync-members",
            "schedule",
            "check",
            "stats",
            "report",
            "search",
            "votes",
        ):
            assert command in result.output


class TestDatabaseUnavailable:
    def test_a_missing_database_gives_advice_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HANSARD_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 2
        assert "docker compose up" in result.output


class TestSettingsResolution:
    def test_cli_overrides_beat_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HANSARD_HOUSE", "Lords")
        assert _settings(house="Commons").house == "Commons"

    def test_unspecified_options_fall_back_to_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HANSARD_HOUSE", "Lords")
        assert _settings().house == "Lords"

    def test_settings_not_exposed_on_the_cli_are_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regression this guards: rebuilding Settings field by field would
        # silently reset anything the CLI does not name.
        monkeypatch.setenv("HANSARD_REQUESTS_PER_SECOND", "1.5")
        resolved = _settings(house="Commons")
        assert resolved.requests_per_second == 1.5
        assert resolved.backoff_max_seconds == Settings().backoff_max_seconds

    def test_json_logs_flag_switches_the_format(self) -> None:
        assert _settings(json_logs=True).log_format == "json"

    def test_verbose_flag_lowers_the_level(self) -> None:
        assert _settings(verbose=True).log_level == "DEBUG"


class TestIndexFlagsReachTheBuilder:
    """Every CLI flag must actually arrive at the function it names.

    This class exists because two of them did not, and the second slipped past
    the test written for the first. `--resume` was accepted, documented in
    `--help`, and never passed through -- so it took the destructive path and
    discarded 34,816 embedded passages. The test added then asserted only that
    `resume` arrived, so when `--limit` was added and its call-site patch
    silently failed to match, the suite stayed green while the flag did nothing.

    The fix is to assert the *whole* keyword set, not the one flag being worked
    on. A test that checks a single argument cannot notice a missing one.
    """

    @staticmethod
    def _spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        seen: dict[str, object] = {}

        def fake_build(connection, settings, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(turns=1, chunks=1, remaining=0, dimensions=384, seconds=0.1)

        monkeypatch.setattr("hansard.cli.rag_build.build_index", fake_build)
        return seen

    def test_every_flag_arrives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._spy(monkeypatch)
        result = runner.invoke(app, ["index", "--resume", "--limit", "500"])

        assert result.exit_code == 0, result.output
        assert seen.get("resume") is True
        assert seen.get("limit") == 500

    def test_the_full_keyword_set_is_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Asserting the exact set is what catches an argument that was added to
        # the parser and forgotten at the call site.
        seen = self._spy(monkeypatch)
        runner.invoke(app, ["index"])
        assert set(seen) == {"resume", "limit", "on_progress"}

    def test_defaults_are_the_safe_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._spy(monkeypatch)
        runner.invoke(app, ["index"])
        assert seen.get("resume") is False
        assert seen.get("limit") is None
