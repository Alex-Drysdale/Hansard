"""Integrity checks over the local store.

Idempotent writes stop duplicates being *created*; these checks prove it, and
catch the failure modes that upserts alone do not cover -- a truncated day, an
orphaned section, a search index that drifted out of step with its table.

Each check returns a :class:`CheckResult` rather than raising, so ``hansard
check`` can report everything it found in one pass instead of stopping at the
first problem.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    examples: tuple[str, ...] = field(default=())


def _examples(rows: list[sqlite3.Row], template: Callable[[sqlite3.Row], str]) -> tuple[str, ...]:
    return tuple(template(row) for row in rows[:5])


def check_duplicate_debates(connection: sqlite3.Connection) -> CheckResult:
    """No debate section stored twice.

    The real risk this guards against: Hansard returns a child section inline
    inside its parent's payload *and* as its own hit in the day's search
    results. Ingest deliberately does not recurse into ChildDebates; if that
    ever changes, this check is what notices.
    """
    rows = connection.execute(
        """
        SELECT hansard_id, COUNT(*) AS n, GROUP_CONCAT(ext_id) AS ids
          FROM debate
         GROUP BY hansard_id
        HAVING n > 1
         ORDER BY n DESC
        """
    ).fetchall()
    return CheckResult(
        name="duplicate debates",
        passed=not rows,
        detail=(
            "every debate section appears exactly once"
            if not rows
            else f"{len(rows)} Hansard ids map to more than one stored debate"
        ),
        examples=_examples(rows, lambda r: f"hansard_id={r['hansard_id']} -> {r['ids']}"),
    )


def check_duplicate_contributions(connection: sqlite3.Connection) -> CheckResult:
    """No two rows in a debate hold identical text at the same position.

    item_id is the primary key so exact duplicates cannot exist. What can exist
    is the same speech re-emitted under a new item_id, which this catches.
    """
    rows = connection.execute(
        """
        SELECT debate_ext_id, content_hash, COUNT(*) AS n
          FROM contribution
         WHERE is_speech = 1
         GROUP BY debate_ext_id, content_hash
        HAVING n > 1
         ORDER BY n DESC
        """
    ).fetchall()
    return CheckResult(
        name="duplicate contributions",
        passed=not rows,
        detail=(
            "no repeated speech text within a debate"
            if not rows
            else f"{len(rows)} speech texts appear more than once in the same debate"
        ),
        examples=_examples(rows, lambda r: f"{r['debate_ext_id']} x{r['n']}"),
    )


def check_orphan_parents(connection: sqlite3.Connection) -> CheckResult:
    """Every nested section's parent is a section we actually hold.

    One class of unresolved link is expected: a top-level section (depth 2)
    points at the day-root container ("Commons Chamber"), which has no fetchable
    payload and is therefore never stored. Depth is what separates that from a
    real gap -- at depth 3 or more the parent is an ordinary section, and its
    absence means a fetch was missed.
    """
    rows = connection.execute(
        """
        SELECT d.ext_id, d.title, d.depth, d.parent_ext_id
          FROM debate AS d
         WHERE d.parent_ext_id IS NOT NULL
           AND d.depth > 2
           AND NOT EXISTS (SELECT 1 FROM debate p WHERE p.ext_id = d.parent_ext_id)
         ORDER BY d.sitting_date
        """
    ).fetchall()
    return CheckResult(
        name="orphan parents",
        passed=not rows,
        detail=(
            "every nested section's parent is present"
            if not rows
            else f"{len(rows)} nested sections reference a parent that was never stored"
        ),
        examples=_examples(
            rows, lambda r: f"{r['ext_id']} (depth {r['depth']}) -> {r['parent_ext_id']}"
        ),
    )


def check_day_counts(connection: sqlite3.Connection) -> CheckResult:
    """Stored debates per day match the count the API reported for that day.

    A mismatch means a page was dropped or a fetch failed silently -- exactly
    the kind of partial ingest that looks fine until you count something.
    """
    rows = connection.execute(
        """
        SELECT s.house, s.sitting_date, s.debate_count AS expected,
               COUNT(d.ext_id) AS stored
          FROM sitting_day AS s
          LEFT JOIN debate AS d
                 ON d.house = s.house AND d.sitting_date = s.sitting_date
         WHERE s.completed_at IS NOT NULL
         GROUP BY s.house, s.sitting_date
        HAVING stored <> expected
         ORDER BY s.sitting_date
        """
    ).fetchall()
    return CheckResult(
        name="day counts",
        passed=not rows,
        detail=(
            "every completed sitting day holds the number of debates the API advertised"
            if not rows
            else f"{len(rows)} completed days hold a different number of debates than expected"
        ),
        examples=_examples(
            rows, lambda r: f"{r['sitting_date']}: expected {r['expected']}, stored {r['stored']}"
        ),
    )


def check_fts_in_sync(connection: sqlite3.Connection) -> CheckResult:
    """The search index still matches the table it indexes.

    FTS5's own 'integrity-check' is the right tool here, not a row count:
    ``contribution_fts`` is an external-content table, so ``COUNT(*)`` on it
    silently reads the *content* table and would agree with itself no matter how
    far the index had drifted.

    ``rank = 1`` is load-bearing. Without it the command only checks that the
    index is internally consistent, which a drifted index still is; rank = 1 is
    what makes it compare the index against the content table.
    """
    rows = connection.execute("SELECT COUNT(*) AS n FROM contribution").fetchone()["n"]
    try:
        connection.execute(
            "INSERT INTO contribution_fts (contribution_fts, rank) VALUES ('integrity-check', 1)"
        )
    except sqlite3.DatabaseError as exc:
        # SQLite reports this as "database disk image is malformed", which reads
        # far more alarming than it is: the index needs rebuilding, that is all.
        return CheckResult(
            name="search index",
            passed=False,
            detail=(
                "FTS index does not match the contribution table "
                f"({exc}); rebuild it with INSERT INTO contribution_fts"
                "(contribution_fts) VALUES ('rebuild')"
            ),
        )
    return CheckResult(
        name="search index",
        passed=True,
        detail=f"FTS index consistent with {rows:,} transcript rows",
    )


def check_speech_text(connection: sqlite3.Connection) -> CheckResult:
    """Nothing flagged as a speech is empty, and no HTML survived stripping."""
    rows = connection.execute(
        """
        SELECT item_id, debate_ext_id
          FROM contribution
         WHERE is_speech = 1
           AND (body_text IS NULL OR TRIM(body_text) = '' OR body_text LIKE '%<%>%')
         LIMIT 20
        """
    ).fetchall()
    return CheckResult(
        name="speech text",
        passed=not rows,
        detail=(
            "all speeches carry plain, non-empty text"
            if not rows
            else f"{len(rows)}+ speeches are empty or still contain markup"
        ),
        examples=_examples(rows, lambda r: f"item {r['item_id']} in {r['debate_ext_id']}"),
    )


ALL_CHECKS: tuple[Callable[[sqlite3.Connection], CheckResult], ...] = (
    check_duplicate_debates,
    check_duplicate_contributions,
    check_orphan_parents,
    check_day_counts,
    check_fts_in_sync,
    check_speech_text,
)


def run_all(connection: sqlite3.Connection) -> list[CheckResult]:
    return [check(connection) for check in ALL_CHECKS]
