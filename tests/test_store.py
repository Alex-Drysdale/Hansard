"""Tests for the storage layer.

The property under test throughout is idempotency: writing the same debate
twice must leave the database identical to writing it once. Most of the bugs
this catches are invisible to a single-run test.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from hansard.api.models import DebateDetail
from hansard.db import store
from hansard.db.connection import transaction
from hansard.db.store import WriteOutcome
from hansard.pipeline.normalise import normalise_debate


def save(connection: sqlite3.Connection, detail: DebateDetail) -> store.DebateWriteResult:
    with transaction(connection):
        return store.save_debate(connection, normalise_debate(detail))


def count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


class TestSaveDebate:
    def test_first_write_inserts(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        result = save(connection, debate_with_speeches)
        assert result.outcome is WriteOutcome.INSERTED
        assert count(connection, "debate") == 1
        assert count(connection, "contribution") == len(debate_with_speeches.items)

    def test_second_write_of_identical_content_is_a_no_op(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        result = save(connection, debate_with_speeches)

        assert result.outcome is WriteOutcome.UNCHANGED
        assert result.contributions_written == 0
        assert count(connection, "debate") == 1
        assert count(connection, "contribution") == len(debate_with_speeches.items)

    def test_repeated_writes_never_accumulate_rows(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        for _ in range(5):
            save(connection, debate_with_speeches)
        assert count(connection, "debate") == 1
        assert count(connection, "contribution") == len(debate_with_speeches.items)

    def test_unchanged_write_leaves_revision_alone(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        save(connection, debate_with_speeches)
        row = connection.execute("SELECT revision FROM debate").fetchone()
        assert row["revision"] == 1

    def test_changed_content_updates_and_bumps_revision(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        revised = debate_with_speeches.model_copy(
            update={
                "overview": debate_with_speeches.overview.model_copy(
                    update={"title": "Oil Refining Sector (corrected)"}
                )
            }
        )

        result = save(connection, revised)

        assert result.outcome is WriteOutcome.UPDATED
        row = connection.execute("SELECT title, revision FROM debate").fetchone()
        assert row["title"] == "Oil Refining Sector (corrected)"
        assert row["revision"] == 2

    def test_first_seen_at_survives_an_update(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        original = connection.execute("SELECT first_seen_at FROM debate").fetchone()[0]

        revised = debate_with_speeches.model_copy(
            update={"overview": debate_with_speeches.overview.model_copy(update={"title": "New"})}
        )
        save(connection, revised)

        assert connection.execute("SELECT first_seen_at FROM debate").fetchone()[0] == original

    def test_a_withdrawn_contribution_is_removed_not_orphaned(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        # The reason the transcript is replaced wholesale rather than upserted:
        # an upsert alone would leave a retracted speech in the database for ever.
        save(connection, debate_with_speeches)
        assert count(connection, "contribution") == len(debate_with_speeches.items)

        shorter = debate_with_speeches.model_copy(update={"items": debate_with_speeches.items[:-2]})
        save(connection, shorter)

        assert count(connection, "contribution") == len(debate_with_speeches.items) - 2

    def test_parent_and_child_are_separate_rows(
        self,
        connection: sqlite3.Connection,
        debate_with_child: DebateDetail,
        debate_child: DebateDetail,
    ) -> None:
        save(connection, debate_with_child)
        save(connection, debate_child)

        assert count(connection, "debate") == 2
        child = connection.execute(
            "SELECT parent_ext_id FROM debate WHERE ext_id = ?", (debate_child.overview.ext_id,)
        ).fetchone()
        assert child["parent_ext_id"] == debate_with_child.overview.ext_id


class TestFullTextIndex:
    def test_speeches_become_searchable(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        hits = connection.execute(
            "SELECT COUNT(*) AS n FROM contribution_fts WHERE contribution_fts MATCH 'refinery'"
        ).fetchone()["n"]
        assert hits > 0

    def test_index_follows_a_delete(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        # Guards the external-content trap: a plain DELETE would leave the index
        # pointing at rows that no longer exist.
        save(connection, debate_with_speeches)
        with transaction(connection):
            connection.execute("DELETE FROM contribution")
        connection.execute(
            "INSERT INTO contribution_fts (contribution_fts) VALUES ('integrity-check')"
        )

    def test_index_survives_a_rewrite(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        revised = debate_with_speeches.model_copy(
            update={"overview": debate_with_speeches.overview.model_copy(update={"title": "New"})}
        )
        save(connection, revised)
        connection.execute(
            "INSERT INTO contribution_fts (contribution_fts) VALUES ('integrity-check')"
        )


class TestMembers:
    def test_members_are_derived_from_attributions(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        row = connection.execute(
            "SELECT display_name, party, constituency FROM member WHERE member_id = 3957"
        ).fetchone()
        assert row["display_name"] == "Martin Vickers"
        assert row["party"] == "Con"
        assert row["constituency"] == "Brigg and Immingham"

    def test_later_writes_fill_gaps_rather_than_blanking_them(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        # Hansard states party only on a member's first turn in a debate, so a
        # later bare-name mention must not erase what we already learned.
        save(connection, debate_with_speeches)
        bare = debate_with_speeches.model_copy(
            update={
                "overview": debate_with_speeches.overview.model_copy(
                    update={"ext_id": "OTHER-EXT-ID", "hansard_id": 999}
                ),
                "items": [
                    item.model_copy(
                        update={"attributed_to": "Martin Vickers", "item_id": 900 + index}
                    )
                    for index, item in enumerate(debate_with_speeches.items)
                ],
            }
        )
        save(connection, bare)

        assert count(connection, "debate") == 2

        row = connection.execute(
            "SELECT party, constituency FROM member WHERE member_id = 3957"
        ).fetchone()
        assert row["party"] == "Con"
        assert row["constituency"] == "Brigg and Immingham"

    def test_counts_are_recomputed_not_incremented(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        # Recomputing is what stops the count doubling on a re-ingest.
        save(connection, debate_with_speeches)
        with transaction(connection):
            store.refresh_member_counts(connection)
        first = connection.execute(
            "SELECT contribution_count FROM member WHERE member_id = 3957"
        ).fetchone()[0]

        save(connection, debate_with_speeches)
        with transaction(connection):
            store.refresh_member_counts(connection)
        second = connection.execute(
            "SELECT contribution_count FROM member WHERE member_id = 3957"
        ).fetchone()[0]

        assert first == second > 0


class TestSittingDays:
    def test_recording_a_day_twice_keeps_one_row(self, connection: sqlite3.Connection) -> None:
        for _ in range(3):
            with transaction(connection):
                store.record_sitting_day(
                    connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=25
                )
        assert count(connection, "sitting_day") == 1

    def test_only_completed_days_are_reported_as_done(self, connection: sqlite3.Connection) -> None:
        with transaction(connection):
            store.record_sitting_day(
                connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=25
            )
            store.record_sitting_day(
                connection, house="Commons", sitting_date=date(2026, 1, 15), debate_count=42
            )
            store.complete_sitting_day(connection, house="Commons", sitting_date=date(2026, 1, 14))

        assert store.completed_sitting_days(connection, "Commons") == {date(2026, 1, 14)}

    def test_completion_is_scoped_to_a_house(self, connection: sqlite3.Connection) -> None:
        with transaction(connection):
            store.record_sitting_day(
                connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=1
            )
            store.complete_sitting_day(connection, house="Commons", sitting_date=date(2026, 1, 14))
        assert store.completed_sitting_days(connection, "Lords") == set()


class TestIngestRuns:
    def test_a_run_is_recorded_and_closed(self, connection: sqlite3.Connection) -> None:
        with transaction(connection):
            run_id = store.start_run(
                connection, house="Commons", start=date(2026, 1, 1), end=date(2026, 4, 30)
            )
        assert store.latest_run(connection)["status"] == "running"

        with transaction(connection):
            store.finish_run(connection, run_id, status="completed", counters={"debates_seen": 12})

        row = store.latest_run(connection)
        assert row["status"] == "completed"
        assert row["debates_seen"] == 12
        assert row["finished_at"] is not None

    def test_status_is_constrained(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ingest_run (started_at, status, house, start_date, end_date) "
                "VALUES ('now', 'bogus', 'Commons', '2026-01-01', '2026-01-02')"
            )


class TestSchemaConstraints:
    def test_foreign_keys_are_enforced(self, connection: sqlite3.Connection) -> None:
        # PRAGMA foreign_keys is per-connection and off by default; this proves
        # hansard.db.connection.connect actually turned it on.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO contribution "
                "(item_id, debate_ext_id, order_in_section, item_type, content_hash) "
                "VALUES (1, 'no-such-debate', 0, 'Contribution', 'h')"
            )

    def test_house_is_constrained(self, connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sitting_day "
                "(house, sitting_date, first_seen_at, last_fetched_at) "
                "VALUES ('Senate', '2026-01-14', 'now', 'now')"
            )

    def test_deleting_a_debate_takes_its_contributions(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        with transaction(connection):
            connection.execute("DELETE FROM debate")
        assert count(connection, "contribution") == 0


class TestSpeechView:
    def test_view_shows_only_speeches(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        assert count(connection, "speech") == count(connection, "contribution WHERE is_speech = 1")
        assert count(connection, "speech") < count(connection, "contribution")

    def test_view_fills_in_party_from_the_member_record(
        self, connection: sqlite3.Connection, debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        stated = connection.execute(
            "SELECT COUNT(*) AS n FROM contribution WHERE is_speech = 1 AND party IS NOT NULL"
        ).fetchone()["n"]
        resolved = connection.execute(
            "SELECT COUNT(*) AS n FROM speech WHERE party IS NOT NULL"
        ).fetchone()["n"]
        assert resolved >= stated
