"""Schema migrations.

Phase 1 kept a single ``schema.sql`` full of ``CREATE TABLE IF NOT EXISTS`` and
re-ran it at startup. That works exactly until the first time a column needs to
change: ``IF NOT EXISTS`` silently does nothing to an existing table, so the
schema in your editor and the schema in the database drift apart with no error.

Migrations replace that with an ordered, recorded sequence. Each file in
``migrations/`` runs once, in filename order, inside a transaction, and the
database records which have been applied. The schema is then a consequence of
the migration history rather than something anyone edits by hand.

We use yoyo rather than hand-rolling a runner: applying SQL in order is easy,
but doing it safely under concurrency (two schedulers starting at once) needs
advisory locking, and that is not worth reinventing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yoyo import get_backend, read_migrations

from hansard.db.engine import redact

logger = logging.getLogger(__name__)

# Where the .sql files live. Resolving this is less obvious than it looks: in a
# source checkout the package sits under src/, so the directory is three levels
# up -- but in an installed copy the package sits in site-packages, where that
# path is meaningless. The container hit exactly that and could not migrate.
#
# So: an explicit environment variable wins (the container sets it), then the
# source-checkout layout, then the working directory.
_PACKAGE_RELATIVE = Path(__file__).resolve().parents[3] / "migrations"


def default_directory() -> Path:
    """The migrations directory for this installation."""
    override = os.environ.get("HANSARD_MIGRATIONS_DIR", "").strip()
    if override:
        return Path(override)
    for candidate in (_PACKAGE_RELATIVE, Path.cwd() / "migrations"):
        if candidate.is_dir():
            return candidate
    # Nothing found: return the most likely path so the error names it.
    return _PACKAGE_RELATIVE


# Kept as a module attribute for callers that only need the common case.
MIGRATIONS_DIR = default_directory()


class MigrationError(RuntimeError):
    """A migration could not be applied."""


@dataclass(frozen=True, slots=True)
class MigrationState:
    applied: tuple[str, ...]
    pending: tuple[str, ...]

    @property
    def is_up_to_date(self) -> bool:
        return not self.pending


def _backend_url(database_url: str) -> str:
    """yoyo wants a driver-qualified URL; psycopg 3 is ``postgresql+psycopg``."""
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url[len(prefix) :]
    return database_url


@contextmanager
def _backend(database_url: str) -> Iterator[Any]:
    """A yoyo backend that closes its connection afterwards.

    yoyo backends open a connection on construction and never close it, which
    leaks one per call -- harmless in a short CLI run, but the scheduler calls
    this on every startup and the test suite calls it per session.
    """
    backend = get_backend(_backend_url(database_url))
    try:
        yield backend
    finally:
        with suppress(Exception):
            backend.connection.close()


def _resolve_directory(directory: Path | None) -> Path:
    resolved = directory or default_directory()
    if not resolved.is_dir():
        raise MigrationError(
            f"No migrations directory at {resolved}. "
            "Set HANSARD_MIGRATIONS_DIR if it lives somewhere else."
        )
    return resolved


def status(database_url: str, *, directory: Path | None = None) -> MigrationState:
    """Which migrations have run, and which have not."""
    migrations = read_migrations(str(_resolve_directory(directory)))
    with _backend(database_url) as backend, backend.lock():
        applied_ids = {m.id for m in backend.to_rollback(migrations)}
    return MigrationState(
        applied=tuple(sorted(applied_ids)),
        pending=tuple(m.id for m in migrations if m.id not in applied_ids),
    )


def upgrade(database_url: str, *, directory: Path | None = None) -> tuple[str, ...]:
    """Apply every pending migration. Returns the ids applied, in order.

    Safe to call on every startup: with nothing pending it is a no-op, which is
    what lets the scheduler and the CLI both call it without coordinating.
    """
    migrations = read_migrations(str(_resolve_directory(directory)))
    if not migrations:
        raise MigrationError(f"No migrations found in {_resolve_directory(directory)}")

    with _backend(database_url) as backend, backend.lock():
        pending = backend.to_apply(migrations)
        pending_ids = tuple(m.id for m in pending)
        if not pending_ids:
            logger.debug("schema already up to date at %s", redact(database_url))
            return ()
        logger.info("applying %d migration(s): %s", len(pending_ids), ", ".join(pending_ids))
        backend.apply_migrations(pending)
    return pending_ids


def downgrade(
    database_url: str, *, steps: int = 1, directory: Path | None = None
) -> tuple[str, ...]:
    """Roll back the most recent ``steps`` migrations.

    Present so that a migration written today can be tested today. Reaching for
    it against real data is usually the wrong move -- prefer a new forward
    migration, which leaves a history rather than erasing one.
    """
    migrations = read_migrations(str(_resolve_directory(directory)))
    with _backend(database_url) as backend, backend.lock():
        rollback = backend.to_rollback(migrations)[:steps]
        rolled_back = tuple(m.id for m in rollback)
        if rolled_back:
            logger.warning("rolling back: %s", ", ".join(rolled_back))
            backend.rollback_migrations(rollback)
    return rolled_back
