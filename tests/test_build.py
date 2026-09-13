"""Tests for the index builder's resume and limit logic.

This module gets its own tests because it is the one place in the project that
has destroyed real work. A ``--resume`` flag the CLI accepted and then failed to
forward silently discarded 34,816 embedded passages; ``--limit`` had the
identical bug, and the regression test written for the first one missed the
second because it only asserted the flag it was written for.

The dangerous behaviour is not "does it embed things" -- that is a library call
-- but three decisions around it: what a resume skips, where a bounded run is
allowed to stop, and whether an overwrite announces itself. All three are
tested against fakes, so they run without Postgres, LanceDB or a model.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hansard.config import Settings
from hansard.rag import build
from hansard.rag.chunking import chunk_turn


class FakeTable:
    """Stands in for a LanceDB table, remembering what was written to it."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)


def make_row(turn_no: int, *, words: int = 60, paragraphs: int = 1) -> dict[str, Any]:
    """One speech turn, shaped as the database hands it over."""
    per_paragraph = max(words // paragraphs, 1)
    text = " ".join(f"word{i}" for i in range(per_paragraph))
    return {
        "debate_ext_id": "debate-1",
        "turn_no": turn_no,
        "debate_title": "Cancer Diagnosis",
        "sitting_date": date(2026, 2, 24),
        "house": "Commons",
        "location": None,
        "member_id": 4504,
        "speaker_name": "Wes Streeting",
        "party": "Labour",
        "constituency": "Ilford North",
        "order_in_section": turn_no,
        "word_count": words,
        "paragraphs": [text] * paragraphs,
        "has_carried_text": False,
    }


def chunks_for(row: dict[str, Any], settings: Settings) -> int:
    """How many chunks this speech produces on its own."""
    return len(
        chunk_turn(
            build.turn_to_speech(row),  # type: ignore[arg-type]
            chunk_words=settings.chunk_words,
            overlap_words=settings.chunk_overlap_words,
        )
    )


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeTable, dict[str, Any]]:
    """Replace every collaborator; hand back the fake table and its controls."""
    table = FakeTable()
    state: dict[str, Any] = {"rows": [], "already": set(), "created_with": None}

    monkeypatch.setattr(build.embeddings, "check_model_fits_chunks", lambda *a, **k: None)
    monkeypatch.setattr(build.embeddings, "dimensions", lambda *a, **k: 384)
    monkeypatch.setattr(
        build.embeddings, "embed_passages", lambda texts, **k: [[0.0] * 384 for _ in texts]
    )
    monkeypatch.setattr(build.index, "indexed_turn_ids", lambda path: set(state["already"]))
    monkeypatch.setattr(build.index, "open_or_create_table", lambda path, dims: table)
    monkeypatch.setattr(
        build.index,
        "to_row",
        lambda chunk, vector, at: {"turn_id": chunk.turn_id, "chunk_id": chunk.chunk_id},
    )

    def create_table(path: Path, dims: int, *, overwrite: bool = True) -> FakeTable:
        state["created_with"] = overwrite
        return table

    monkeypatch.setattr(build.index, "create_table", create_table)
    monkeypatch.setattr(
        build.speeches_store, "stream_speech_turns", lambda conn, **k: iter(state["rows"])
    )
    monkeypatch.setattr(build, "count_indexable_turns", lambda conn, **k: len(state["rows"]))
    # A batch of one, so where a bounded run stops is observable rather than
    # rounded off by a 512-chunk flush.
    monkeypatch.setattr(build, "EMBED_BATCH", 1)
    return table, state


def written_turns(table: FakeTable) -> list[str]:
    return [row["turn_id"] for row in table.rows]


class TestLimitStopsCleanly:
    def test_it_indexes_only_the_requested_number_of_speeches(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        table, state = wired
        state["rows"] = [make_row(n) for n in range(10)]

        report = build.build_index(object(), settings, limit=3)  # type: ignore[arg-type]

        assert len(set(written_turns(table))) == 3
        assert report.turns == 3

    def test_it_never_stops_half_way_through_a_speech(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        """The failure this guards against is silent and unrecoverable.

        A resume skips by *turn* id, so a speech whose first chunk was written
        and whose second was not looks finished to the next run. The rest of it
        would never be indexed, and nothing would say so.
        """
        table, state = wired
        rows = [make_row(n, words=1200, paragraphs=12) for n in range(6)]
        state["rows"] = rows
        assert chunks_for(rows[0], settings) > 1, "test needs speeches that split"

        build.build_index(object(), settings, limit=2)  # type: ignore[arg-type]

        written = written_turns(table)
        assert len(set(written)) == 2
        for row in rows:
            turn_id = build.turn_id_of(row)  # type: ignore[arg-type]
            count = written.count(turn_id)
            # Present in full, or absent entirely. Never part-way.
            assert count in (0, chunks_for(row, settings))

    def test_remaining_reports_what_is_left_not_what_was_skipped(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        """The report once called this "skipped (too short)", which was untrue.

        A bounded run stops early by design. Labelling the speeches it had not
        reached as discarded made a healthy build look like it was throwing most
        of the corpus away.
        """
        _, state = wired
        state["rows"] = [make_row(n) for n in range(10)]

        report = build.build_index(object(), settings, limit=4)  # type: ignore[arg-type]

        assert report.remaining == 6

    def test_no_limit_indexes_everything(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        table, state = wired
        state["rows"] = [make_row(n) for n in range(7)]

        report = build.build_index(object(), settings)  # type: ignore[arg-type]

        assert len(set(written_turns(table))) == 7
        assert report.remaining == 0


class TestResume:
    def test_it_skips_speeches_already_in_the_index(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        table, state = wired
        state["rows"] = [make_row(n) for n in range(5)]
        state["already"] = {"debate-1:0", "debate-1:1", "debate-1:2"}

        report = build.build_index(object(), settings, resume=True)  # type: ignore[arg-type]

        assert set(written_turns(table)) == {"debate-1:3", "debate-1:4"}
        # The total is the index's contents, not this run's output.
        assert report.turns == 5

    def test_it_opens_the_table_rather_than_overwriting_it(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        _, state = wired
        state["rows"] = [make_row(0)]
        state["already"] = {"debate-1:0"}

        build.build_index(object(), settings, resume=True)  # type: ignore[arg-type]

        assert state["created_with"] is None, "resume must not call create_table"

    def test_limit_counts_new_speeches_not_total_ones(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        """Otherwise a resume would stop before doing any work at all.

        With 27,000 speeches already indexed, a limit measured against the total
        would be exhausted by the skipped ones and the run would exit having
        added nothing -- looking, from the outside, exactly like it had finished.
        """
        table, state = wired
        state["rows"] = [make_row(n) for n in range(10)]
        state["already"] = {f"debate-1:{n}" for n in range(6)}

        build.build_index(object(), settings, resume=True, limit=2)  # type: ignore[arg-type]

        assert set(written_turns(table)) == {"debate-1:6", "debate-1:7"}


class TestOverwriteIsAnnounced:
    def test_discarding_an_existing_index_is_logged_as_a_warning(
        self,
        wired: tuple[FakeTable, dict[str, Any]],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Silence here is what made the original data loss invisible."""
        _, state = wired
        state["rows"] = [make_row(0)]
        state["already"] = {f"debate-1:{n}" for n in range(34816)}

        warnings: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(build.log, "warning", lambda event, **kw: warnings.append((event, kw)))

        build.build_index(object(), settings, resume=False)  # type: ignore[arg-type]

        assert [event for event, _ in warnings] == ["index.discarding_existing"]
        assert warnings[0][1]["speeches"] == 34816
        assert state["created_with"] is True

    def test_a_first_build_says_nothing(
        self,
        wired: tuple[FakeTable, dict[str, Any]],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A warning on every clean build would train people to ignore it."""
        _, state = wired
        state["rows"] = [make_row(0)]

        warnings: list[str] = []
        monkeypatch.setattr(build.log, "warning", lambda event, **kw: warnings.append(event))

        build.build_index(object(), settings, resume=False)  # type: ignore[arg-type]

        assert warnings == []


class TestShortSpeechesAreDropped:
    def test_speeches_below_the_word_floor_are_not_indexed(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        table, state = wired
        state["rows"] = [make_row(0, words=200), make_row(1, words=5)]

        build.build_index(object(), settings, min_words=25)  # type: ignore[arg-type]

        assert set(written_turns(table)) == {"debate-1:0"}


class TestEmptyDatabase:
    def test_it_refuses_rather_than_writing_an_empty_index(
        self, wired: tuple[FakeTable, dict[str, Any]], settings: Settings
    ) -> None:
        _, state = wired
        state["rows"] = []

        with pytest.raises(RuntimeError, match="ingest"):
            build.build_index(object(), settings)  # type: ignore[arg-type]
