"""Shared test fixtures.

The JSON under ``tests/fixtures`` is real Hansard output, trimmed but not
hand-written. Tests against invented payloads only prove that the code agrees
with our imagination; these prove it agrees with Parliament.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hansard.api.models import DebateDetail
from hansard.config import Settings
from hansard.db.connection import connect, initialise

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Read a captured API response by filename."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a temp database, with sleeps effectively disabled."""
    return Settings(
        base_url="https://hansard.test",
        database_path=tmp_path / "test.db",
        house="Commons",
        # Matches the sitting date of the linked parent/child fixtures, so the
        # day-count check has something real to compare against.
        start_date=date(2026, 3, 3),
        end_date=date(2026, 3, 3),
        requests_per_second=1000.0,
        max_retries=2,
        backoff_base_seconds=0.0,
        page_size=100,
    )


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    """An initialised in-memory database, torn down after each test."""
    conn = connect(":memory:")
    initialise(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def debate_with_speeches() -> DebateDetail:
    return DebateDetail.model_validate(load_fixture("debate_with_speeches.json"))


@pytest.fixture
def debate_with_child() -> DebateDetail:
    return DebateDetail.model_validate(load_fixture("debate_with_child.json"))


@pytest.fixture
def debate_child() -> DebateDetail:
    return DebateDetail.model_validate(load_fixture("debate_child.json"))


@pytest.fixture
def recorded_sleeps() -> list[float]:
    """Collects the durations code asked to sleep for, instead of sleeping."""
    return []
