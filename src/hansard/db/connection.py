"""SQLite connection management.

One place decides how a connection is configured, so every caller -- CLI,
pipeline, tests -- gets the same pragmas and the same row type.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

SCHEMA_RESOURCE = "schema.sql"


def load_schema_sql() -> str:
    """The schema DDL, read from the packaged ``schema.sql``."""
    return resources.files("hansard.db").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")


def connect(database_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas this project relies on.

    ``:memory:`` is passed through untouched so tests can use it directly.
    """
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row

    # foreign_keys is off by default in SQLite and is per-connection, so it has
    # to be set here rather than in the schema, or contribution's FK to debate
    # would silently not be enforced.
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL lets a long ingest run while a query session reads the same file.
    connection.execute("PRAGMA journal_mode = WAL")
    # NORMAL trades a vanishingly small crash window for a large write speedup,
    # and this data is always re-fetchable.
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def initialise(connection: sqlite3.Connection) -> None:
    """Create tables, indexes and views if they are not already there.

    The schema is written entirely with ``IF NOT EXISTS``, so this is safe to
    run against an existing database on every startup.
    """
    connection.executescript(load_schema_sql())


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one atomic unit.

    The connection is opened in autocommit mode (``isolation_level=None``), so
    transactions are explicit and visible rather than implied by the driver.
    """
    connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


@contextmanager
def open_database(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open an initialised database and close it afterwards."""
    connection = connect(database_path)
    try:
        initialise(connection)
        yield connection
    finally:
        connection.close()
