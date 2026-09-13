"""Shared test fixtures.

Two decisions worth knowing about:

**The JSON under ``tests/fixtures`` is real API output**, trimmed but not
hand-written. Tests against invented payloads only prove the code agrees with
our imagination; these prove it agrees with Parliament.

**Database tests run against a real Postgres**, not a mock or SQLite. Half of
what Phase 2 added lives *in* the database -- generated columns, enums, foreign
keys, UPSERT semantics -- and none of that is exercised by a fake. The cost is
that those tests need a running container; they skip cleanly when there is not
one, so the pure-logic tests still run anywhere.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row

from hansard.api.members_models import MemberEnvelope, MemberValue
from hansard.api.models import DebateDetail, DivisionDetail
from hansard.config import Settings
from hansard.db import migrate

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# A separate database from the development one, so running the suite can never
# destroy data you spent twenty minutes fetching.
TEST_DATABASE_NAME = "hansard_test"


def load_fixture(name: str) -> Any:
    """Read a captured API response by filename."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Settings and payloads (no database needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Settings pointed at stub hosts, with sleeps effectively disabled."""
    return Settings(
        database_url="postgresql://unused@localhost/unused",
        hansard_base_url="https://hansard.test",
        members_base_url="https://members.test",
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
def debate_with_speeches() -> DebateDetail:
    return DebateDetail.model_validate(load_fixture("debate_with_speeches.json"))


@pytest.fixture
def debate_with_child() -> DebateDetail:
    return DebateDetail.model_validate(load_fixture("debate_with_child.json"))


@pytest.fixture
def debate_child() -> DebateDetail:
    return DebateDetail.model_validate(load_fixture("debate_child.json"))


@pytest.fixture
def division_detail() -> DivisionDetail:
    return DivisionDetail.model_validate(load_fixture("division.json"))


@pytest.fixture
def member_value() -> MemberValue:
    return MemberEnvelope.model_validate(load_fixture("member.json")).value


@pytest.fixture
def recorded_sleeps() -> list[float]:
    """Collects the durations code asked to sleep for, instead of sleeping."""
    return []


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _admin_url() -> str:
    """Connection URL for the server, pointed at the default database."""
    return os.environ.get(
        "HANSARD_TEST_ADMIN_URL", "postgresql://hansard:hansard@localhost:5432/postgres"
    )


def _test_url() -> str:
    return os.environ.get(
        "HANSARD_TEST_DATABASE_URL",
        f"postgresql://hansard:hansard@localhost:5432/{TEST_DATABASE_NAME}",
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    """Create the test database once per session, migrated and empty.

    Skips the whole database suite when Postgres is not reachable, so the
    logic tests still run on a machine with no container.
    """
    try:
        with psycopg.connect(_admin_url(), autocommit=True, connect_timeout=5) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{TEST_DATABASE_NAME}"')
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not available for database tests: {exc}")

    url = _test_url()
    migrate.upgrade(url)
    return url


@pytest.fixture
def connection(database_url: str) -> Iterator[Connection[DictRow]]:
    """A connection whose writes are rolled back at the end of the test.

    Every test runs inside one transaction that is never committed, so tests
    share a migrated database without sharing state and without paying to
    recreate the schema each time.
    """
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


TRUNCATE_ALL = (
    "TRUNCATE division_vote, division, contribution, debate, member, "
    "sitting_day, ingest_run RESTART IDENTITY CASCADE"
)


@pytest.fixture
def clean_connection(database_url: str) -> Iterator[Connection[DictRow]]:
    """An autocommit connection over an emptied database.

    For the tests that need to see committed state -- anything exercising the
    pipeline's own transaction handling, which the wrapping transaction of the
    ``connection`` fixture would mask.

    Truncating on the way *out* as well as in is what keeps the two fixtures
    compatible: this one commits, so without the teardown it would leave rows
    behind for the next rollback-based test to trip over.
    """
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=True)
    conn.execute(TRUNCATE_ALL)
    try:
        yield conn
    finally:
        conn.execute(TRUNCATE_ALL)
        conn.close()
