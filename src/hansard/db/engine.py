"""Postgres connection management.

One place decides how a connection is configured, so the CLI, the scheduler and
the tests all get the same behaviour.

Replaces the SQLite module from Phase 1. The interesting differences:

* Connections come from a pool. The scheduler runs several jobs over the life of
  the process and reconnecting per job would be wasteful; the pool also caps how
  many connections a runaway job can open.
* Transactions are explicit via ``psycopg``'s own context manager rather than
  hand-written BEGIN/COMMIT.
* Rows come back as dictionaries, so call sites read by column name.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Long enough to ride out a container that is still starting, short enough that
# a genuinely wrong host fails while you are still looking at the terminal.
CONNECT_TIMEOUT_SECONDS = 10


class DatabaseUnavailableError(RuntimeError):
    """Postgres could not be reached.

    Raised in place of psycopg's own error so the CLI can print something a
    person can act on -- almost always "the container is not running".
    """


def connect(database_url: str) -> Connection[DictRow]:
    """Open a single connection. Prefer :func:`pool` for anything long-lived."""
    try:
        return psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            autocommit=True,
        )
    except psycopg.OperationalError as exc:
        raise DatabaseUnavailableError(_explain(database_url, exc)) from exc


@contextmanager
def open_connection(database_url: str) -> Iterator[Connection[DictRow]]:
    """A connection that closes itself."""
    connection = connect(database_url)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def pool(database_url: str, *, min_size: int = 1, max_size: int = 4) -> Iterator[ConnectionPool]:
    """A connection pool for the lifetime of a process."""
    try:
        with ConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": True},
            timeout=CONNECT_TIMEOUT_SECONDS,
            open=True,
        ) as connection_pool:
            connection_pool.wait(timeout=CONNECT_TIMEOUT_SECONDS)
            yield connection_pool
    except psycopg.OperationalError as exc:
        raise DatabaseUnavailableError(_explain(database_url, exc)) from exc


@contextmanager
def transaction(connection: Connection[DictRow]) -> Iterator[Connection[DictRow]]:
    """Run a block atomically.

    Connections are opened in autocommit mode, so a transaction is something you
    ask for explicitly and can see in the code, rather than an implicit state
    the driver puts you in.
    """
    with connection.transaction():
        yield connection


def _explain(database_url: str, exc: Exception) -> str:
    """Turn a connection failure into advice."""
    return (
        f"Cannot reach Postgres at {redact(database_url)}: {exc}\n"
        "Is the container running? Try:  docker compose up -d db"
    )


def redact(database_url: str) -> str:
    """Strip the password so a URL can safely appear in logs and errors."""
    try:
        info = psycopg.conninfo.conninfo_to_dict(database_url)
    except psycopg.ProgrammingError:
        return "<unparseable database url>"
    user = info.get("user", "")
    host = info.get("host", "localhost")
    port = info.get("port", "5432")
    dbname = info.get("dbname", "")
    prefix = f"{user}:***@" if user else ""
    return f"postgresql://{prefix}{host}:{port}/{dbname}"
