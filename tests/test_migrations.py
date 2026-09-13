"""Tests for schema migrations.

The point of migrations is that the schema is a consequence of a recorded
history rather than something anyone edits by hand. These check the properties
that makes possible: applying is repeatable, ordering is deterministic, and the
schema the code expects is the one the database has.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow

from hansard.db import migrate
from hansard.pipeline import checks

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class TestMigrationFiles:
    def test_at_least_one_migration_exists(self) -> None:
        assert list(MIGRATIONS.glob("*.sql"))

    def test_filenames_sort_into_application_order(self) -> None:
        # Migrations run in filename order, so the numeric prefix is what
        # guarantees a deterministic sequence across machines.
        names = sorted(path.stem for path in MIGRATIONS.glob("*.sql"))
        for name in names:
            prefix = name.split("_", 1)[0]
            assert prefix.isdigit(), f"{name} must start with a numeric prefix"
        assert names == sorted(names, key=lambda n: int(n.split("_", 1)[0]))

    def test_prefixes_are_unique(self) -> None:
        prefixes = [path.stem.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql")]
        assert len(prefixes) == len(set(prefixes))


class TestUpgrade:
    def test_applying_twice_is_a_no_op(self, database_url: str) -> None:
        # The session fixture already migrated, so there should be nothing left
        # to do. This is what lets the CLI and scheduler both call upgrade()
        # on startup without coordinating.
        assert migrate.upgrade(database_url) == ()

    def test_status_reports_nothing_pending(self, database_url: str) -> None:
        state = migrate.status(database_url)
        assert state.is_up_to_date
        assert state.applied
        assert not state.pending

    def test_every_file_on_disk_has_been_applied(self, database_url: str) -> None:
        state = migrate.status(database_url)
        on_disk = {path.stem for path in MIGRATIONS.glob("*.sql")}
        assert on_disk <= set(state.applied)

    def test_a_missing_directory_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(migrate.MigrationError, match="No migrations directory"):
            migrate.status("postgresql://x@localhost/x", directory=tmp_path / "nope")


class TestSchemaShape:
    """The migration produced the schema the code actually depends on."""

    def test_expected_tables_exist(self, connection: Connection[DictRow]) -> None:
        rows = connection.execute(
            """
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """
        ).fetchall()
        names = {row["table_name"] for row in rows}
        assert {
            "ingest_run",
            "sitting_day",
            "member",
            "debate",
            "contribution",
            "division",
            "division_vote",
        } <= names

    def test_expected_views_exist(self, connection: Connection[DictRow]) -> None:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
        ).fetchall()
        assert {"speech", "member_resolved"} <= {row["table_name"] for row in rows}

    def test_identifiers_are_text_not_uuid(self, connection: Connection[DictRow]) -> None:
        # Hansard uses three id formats; a UUID column would reject two of them.
        rows = connection.execute(
            """
            SELECT table_name, column_name, data_type FROM information_schema.columns
             WHERE table_schema = 'public'
               AND column_name IN ('ext_id', 'parent_ext_id', 'debate_ext_id',
                                   'external_id', 'division_ext_id')
            """
        ).fetchall()
        assert rows
        assert all(row["data_type"] == "text" for row in rows), [
            (r["table_name"], r["column_name"], r["data_type"]) for r in rows
        ]

    def test_dates_are_real_dates(self, connection: Connection[DictRow]) -> None:
        row = connection.execute(
            """
            SELECT data_type FROM information_schema.columns
             WHERE table_name = 'debate' AND column_name = 'sitting_date'
            """
        ).fetchone()
        assert row is not None and row["data_type"] == "date"

    def test_the_search_vector_is_generated(self, connection: Connection[DictRow]) -> None:
        # Generated, so no code path can forget to update it -- the hazard the
        # SQLite external-content FTS table carried.
        row = connection.execute(
            """
            SELECT is_generated FROM information_schema.columns
             WHERE table_name = 'contribution' AND column_name = 'body_tsv'
            """
        ).fetchone()
        assert row is not None and row["is_generated"] == "ALWAYS"

    def test_enums_constrain_their_columns(self, connection: Connection[DictRow]) -> None:
        rows = connection.execute("SELECT typname FROM pg_type WHERE typtype = 'e'").fetchall()
        assert {"house", "run_status", "vote_lobby"} <= {row["typname"] for row in rows}

    def test_foreign_keys_are_declared(self, connection: Connection[DictRow]) -> None:
        rows = connection.execute(
            """
            SELECT tc.table_name, kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
             WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
            """
        ).fetchall()
        pairs = {(row["table_name"], row["column_name"]) for row in rows}
        assert ("contribution", "debate_ext_id") in pairs
        assert ("contribution", "member_id") in pairs
        assert ("division_vote", "division_ext_id") in pairs
        assert ("division_vote", "member_id") in pairs

    def test_parent_ext_id_is_deliberately_not_a_foreign_key(
        self, connection: Connection[DictRow]
    ) -> None:
        # The topmost ancestor of a day is a container with no fetchable
        # payload, so a real FK would reject legitimate rows.
        rows = connection.execute(
            """
            SELECT kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
             WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = 'debate'
            """
        ).fetchall()
        assert "parent_ext_id" not in {row["column_name"] for row in rows}


class TestSchemaVersionCheck:
    def test_passes_on_a_migrated_database(self, connection: Connection[DictRow]) -> None:
        result = checks.check_schema_current(connection)
        assert result.passed, result.detail

    def test_fails_when_the_history_is_missing(self, connection: Connection[DictRow]) -> None:
        # Simulates running the code against a database nobody migrated.
        connection.execute("DROP TABLE _yoyo_migration CASCADE")
        result = checks.check_schema_current(connection)
        assert not result.passed
        assert "migration history" in result.detail

    def test_fails_when_a_migration_is_pending(self, connection: Connection[DictRow]) -> None:
        connection.execute("DELETE FROM _yoyo_migration")
        result = checks.check_schema_current(connection)
        assert not result.passed
        assert "pending" in result.detail


class TestConnectionErrors:
    def test_an_unreachable_database_gives_actionable_advice(self) -> None:
        from hansard.db.engine import DatabaseUnavailableError, connect

        with pytest.raises(DatabaseUnavailableError, match="docker compose up"):
            connect("postgresql://nobody@127.0.0.1:1/none")

    def test_the_password_is_never_echoed(self) -> None:
        from hansard.db.engine import redact

        redacted = redact("postgresql://user:hunter2@localhost:5432/hansard")
        assert "hunter2" not in redacted
        assert "user" in redacted and "hansard" in redacted


def test_psycopg_is_the_only_driver_in_use(connection: Connection[DictRow]) -> None:
    assert isinstance(connection, psycopg.Connection)
