"""Tests for the integrity checks.

A check that cannot fail is worse than no check, because it reads as assurance.
Each test here deliberately corrupts the store and asserts the check notices.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from hansard.api.models import DebateDetail
from hansard.db import store
from hansard.db.connection import transaction
from hansard.pipeline import checks
from hansard.pipeline.normalise import normalise_debate


@pytest.fixture
def populated(connection: sqlite3.Connection, debate_with_speeches: DebateDetail):
    """A small, internally consistent store: one complete sitting day."""
    normalised = normalise_debate(debate_with_speeches)
    with transaction(connection):
        store.record_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=1
        )
        store.save_debate(connection, normalised)
        store.complete_sitting_day(connection, house="Commons", sitting_date=date(2026, 1, 14))
    return connection


def test_a_consistent_store_passes_everything(populated: sqlite3.Connection) -> None:
    failures = [result for result in checks.run_all(populated) if not result.passed]
    assert not failures, [f"{r.name}: {r.detail}" for r in failures]


def test_every_check_reports_a_name_and_detail(populated: sqlite3.Connection) -> None:
    for result in checks.run_all(populated):
        assert result.name and result.detail


class TestDuplicateDebates:
    def test_notices_one_hansard_id_stored_under_two_ext_ids(
        self, populated: sqlite3.Connection
    ) -> None:
        row = populated.execute("SELECT * FROM debate").fetchone()
        populated.execute(
            "INSERT INTO debate (ext_id, hansard_id, title, house, sitting_date, "
            "content_hash, first_seen_at, last_fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "DUPLICATE",
                row["hansard_id"],
                row["title"],
                row["house"],
                row["sitting_date"],
                "other-hash",
                "now",
                "now",
            ),
        )

        result = checks.check_duplicate_debates(populated)

        assert not result.passed
        assert result.examples


class TestDuplicateContributions:
    def test_notices_the_same_speech_twice_in_one_debate(
        self, populated: sqlite3.Connection
    ) -> None:
        row = populated.execute("SELECT * FROM contribution WHERE is_speech = 1 LIMIT 1").fetchone()
        populated.execute(
            "INSERT INTO contribution (item_id, debate_ext_id, order_in_section, item_type, "
            "is_speech, body_text, content_hash) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                999_999,
                row["debate_ext_id"],
                99,
                row["item_type"],
                row["body_text"],
                row["content_hash"],
            ),
        )

        assert not checks.check_duplicate_contributions(populated).passed


class TestDayCounts:
    def test_notices_a_day_that_stored_fewer_debates_than_advertised(
        self, populated: sqlite3.Connection
    ) -> None:
        # The signature of a silently truncated ingest.
        populated.execute("UPDATE sitting_day SET debate_count = 25")

        result = checks.check_day_counts(populated)

        assert not result.passed
        assert "expected 25" in result.examples[0]

    def test_ignores_a_day_that_has_not_finished(self, populated: sqlite3.Connection) -> None:
        # A day still in progress is expected to be short; only completed days
        # are held to the count.
        populated.execute("UPDATE sitting_day SET debate_count = 25, completed_at = NULL")

        assert checks.check_day_counts(populated).passed


class TestSpeechText:
    def test_notices_markup_that_survived_stripping(self, populated: sqlite3.Connection) -> None:
        populated.execute(
            "UPDATE contribution SET body_text = '<p>still html</p>' WHERE is_speech = 1"
        )
        assert not checks.check_speech_text(populated).passed

    def test_notices_an_empty_speech(self, populated: sqlite3.Connection) -> None:
        populated.execute("UPDATE contribution SET body_text = '' WHERE is_speech = 1")
        assert not checks.check_speech_text(populated).passed


class TestSearchIndex:
    def test_passes_when_the_index_matches(self, populated: sqlite3.Connection) -> None:
        assert checks.check_fts_in_sync(populated).passed

    def test_notices_an_index_that_has_drifted(self, populated: sqlite3.Connection) -> None:
        # Written straight into the index's shadow storage, bypassing the
        # triggers -- the drift a plain row count could never detect, because
        # COUNT(*) on an external-content table reads the content table instead.
        populated.execute(
            "INSERT INTO contribution_fts (rowid, body_text) VALUES (?, ?)",
            (123_456, "a phrase with no matching contribution row"),
        )

        assert not checks.check_fts_in_sync(populated).passed


class TestEmptyStore:
    def test_checks_pass_on_an_empty_database(self, connection: sqlite3.Connection) -> None:
        # Nothing ingested is not the same as something broken.
        failures = [result for result in checks.run_all(connection) if not result.passed]
        assert not failures


class TestOrphanParents:
    def test_a_top_level_section_may_point_at_an_unstored_day_root(
        self, populated: sqlite3.Connection
    ) -> None:
        # Every top-level section does this; it is not a fault.
        row = populated.execute("SELECT depth, parent_ext_id FROM debate").fetchone()
        assert row["depth"] == 2 and row["parent_ext_id"] is not None

        assert checks.check_orphan_parents(populated).passed

    def test_a_nested_section_with_no_stored_parent_is_flagged(
        self, populated: sqlite3.Connection
    ) -> None:
        populated.execute(
            "INSERT INTO debate (ext_id, hansard_id, parent_ext_id, depth, title, house, "
            "sitting_date, content_hash, first_seen_at, last_fetched_at) "
            "VALUES ('NESTED', 42, 'MISSING-PARENT', 3, 't', 'Commons', '2026-01-14', "
            "'h', 'now', 'now')"
        )

        result = checks.check_orphan_parents(populated)

        assert not result.passed
        assert "NESTED" in result.examples[0]
