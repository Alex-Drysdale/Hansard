"""Integrity checks over the store.

Idempotent writes stop duplicates being *created*; these prove it, and catch the
failure modes upserts alone do not cover -- a truncated day, an orphaned
section, a division whose votes never landed.

The distinction that runs through this module: a check asserts something *we*
control. Where the upstream data is merely inconsistent with itself, that gets
reported rather than failed, because failing on it would mean a red build every
night for something nobody can fix.

Each check returns a :class:`CheckResult` rather than raising, so one pass
reports everything it found instead of stopping at the first problem.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from psycopg import Connection
from psycopg.rows import DictRow


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    examples: tuple[str, ...] = field(default=())


def _examples(rows: list[DictRow], template: Callable[[DictRow], str]) -> tuple[str, ...]:
    return tuple(template(row) for row in rows[:5])


def check_duplicate_debates(connection: Connection[DictRow]) -> CheckResult:
    """No debate section stored twice.

    The real risk: Hansard returns a child section inline inside its parent's
    payload *and* as its own hit in the day's search results. Ingest walks the
    flat index and never recurses; if that changes, this notices.
    """
    rows = connection.execute(
        """
        SELECT hansard_id, COUNT(*) AS n, string_agg(ext_id::text, ', ') AS ids
          FROM debate GROUP BY hansard_id HAVING COUNT(*) > 1
         ORDER BY COUNT(*) DESC
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


def check_duplicate_contributions(connection: Connection[DictRow]) -> CheckResult:
    """No two rows in a debate hold identical text.

    item_id is the primary key so exact duplicates cannot exist. What can is the
    same speech re-emitted under a new item_id, which this catches.
    """
    rows = connection.execute(
        """
        SELECT debate_ext_id, content_hash, COUNT(*) AS n
          FROM contribution WHERE is_speech
         GROUP BY debate_ext_id, content_hash HAVING COUNT(*) > 1
         ORDER BY COUNT(*) DESC
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


def check_orphan_parents(connection: Connection[DictRow]) -> CheckResult:
    """Every nested section's parent is a section we actually hold.

    One class of unresolved link is expected: a top-level section (depth 2)
    points at the day-root container, which has no fetchable payload and is
    never stored. Depth separates that from a real gap -- at depth 3 or more the
    parent is an ordinary section, and its absence means a fetch was missed.
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


def check_day_counts(connection: Connection[DictRow]) -> CheckResult:
    """Stored debates per day match the count the API reported for that day.

    A mismatch means a page was dropped or a fetch failed silently -- the kind
    of partial ingest that looks fine until you count something.
    """
    rows = connection.execute(
        """
        SELECT s.house, s.sitting_date, s.debate_count AS expected,
               COUNT(d.ext_id) AS stored
          FROM sitting_day AS s
          LEFT JOIN debate AS d
                 ON d.house = s.house AND d.sitting_date = s.sitting_date
         WHERE s.completed_at IS NOT NULL
         GROUP BY s.house, s.sitting_date, s.debate_count
        HAVING COUNT(d.ext_id) <> s.debate_count
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


def check_speech_text(connection: Connection[DictRow]) -> CheckResult:
    """Nothing flagged as a speech is empty, and no HTML survived stripping."""
    rows = connection.execute(
        """
        SELECT item_id, debate_ext_id
          FROM contribution
         WHERE is_speech
           AND (body_text IS NULL OR btrim(body_text) = '' OR body_text LIKE '%%<%%>%%')
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


def check_search_index(connection: Connection[DictRow]) -> CheckResult:
    """Substantial speeches produce search terms.

    Not "every speech has a non-empty tsvector": a short interjection like
    "I will." is entirely English stopwords, so an empty vector is correct
    behaviour rather than a fault, and asserting otherwise fails on real data.

    What would signal a genuine problem is a *long* speech indexing to nothing,
    which would mean the text never reached the tokeniser. Anything above a
    handful of words should yield at least one term.
    """
    threshold = 10
    rows = connection.execute(
        """
        SELECT item_id, word_count, left(body_text, 60) AS preview
          FROM contribution
         WHERE is_speech AND word_count >= %s AND body_tsv = ''::tsvector
         LIMIT 20
        """,
        (threshold,),
    ).fetchall()
    stopword_only = connection.execute(
        "SELECT COUNT(*) AS n FROM contribution WHERE is_speech AND body_tsv = ''::tsvector"
    ).fetchone()
    indexed = connection.execute(
        "SELECT COUNT(*) AS n FROM contribution WHERE is_speech AND body_tsv <> ''::tsvector"
    ).fetchone()

    trivial = stopword_only["n"] if stopword_only else 0
    return CheckResult(
        name="search index",
        passed=not rows,
        detail=(
            f"{indexed['n'] if indexed else 0:,} speeches indexed"
            + (f"; {trivial} too short to index (all stopwords)" if trivial else "")
            if not rows
            else f"{len(rows)} speeches of {threshold}+ words produced no search terms"
        ),
        examples=_examples(
            rows, lambda r: f"item {r['item_id']} ({r['word_count']}w): {r['preview']}"
        ),
    )


def check_division_votes(connection: Connection[DictRow]) -> CheckResult:
    """Every division that recorded votes actually stored some.

    Deliberately *not* a check that stored votes equal Hansard's stated counts.
    Those disagree upstream: of 395 real divisions, 101 reconcile under neither
    "counts equal listed" nor "counts equal listed minus tellers". Failing
    nightly on that would teach us only to ignore the alert, so it is reported
    by ``hansard report`` instead. This asserts what we control -- that a
    division with a non-zero count did not land with an empty vote list.
    """
    rows = connection.execute(
        """
        SELECT d.ext_id, d.division_date, d.ayes_count, d.noes_count
          FROM division AS d
         WHERE (d.ayes_count > 0 OR d.noes_count > 0)
           AND NOT EXISTS (SELECT 1 FROM division_vote v WHERE v.division_ext_id = d.ext_id)
         ORDER BY d.division_date
        """
    ).fetchall()
    return CheckResult(
        name="division votes",
        passed=not rows,
        detail=(
            "every division with a stated count stored its votes"
            if not rows
            else f"{len(rows)} divisions stored no votes despite a non-zero count"
        ),
        examples=_examples(
            rows,
            lambda r: (
                f"{r['ext_id']} on {r['division_date']} ({r['ayes_count']}/{r['noes_count']})"
            ),
        ),
    )


def check_member_references(connection: Connection[DictRow]) -> CheckResult:
    """Everyone who spoke or voted has a member row.

    The database enforces this with foreign keys, so a failure here means
    somebody disabled one -- which is exactly when you want to be told.
    """
    orphan_speakers = connection.execute(
        """
        SELECT COUNT(*) AS n FROM contribution c
         WHERE c.member_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM member m WHERE m.member_id = c.member_id)
        """
    ).fetchone()
    orphan_voters = connection.execute(
        """
        SELECT COUNT(*) AS n FROM division_vote v
         WHERE NOT EXISTS (SELECT 1 FROM member m WHERE m.member_id = v.member_id)
        """
    ).fetchone()
    speakers = orphan_speakers["n"] if orphan_speakers else 0
    voters = orphan_voters["n"] if orphan_voters else 0
    return CheckResult(
        name="member references",
        passed=speakers == 0 and voters == 0,
        detail=(
            "every speaker and voter resolves to a member row"
            if speakers == 0 and voters == 0
            else f"{speakers} contributions and {voters} votes reference an unknown member"
        ),
    )


def check_schema_current(connection: Connection[DictRow]) -> CheckResult:
    """The database is at the migration version this code expects.

    Cheap insurance against the failure migrations exist to prevent: running new
    code against a schema nobody upgraded.

    Reads the applied-migrations table directly through the connection we
    already hold, rather than rebuilding a connection URL from it -- psycopg
    reports its DSN in keyword form, which is not the URI yoyo expects, so
    reconstructing one was both fiddly and wrong.
    """
    from hansard.db.migrate import default_directory

    try:
        rows = connection.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
    except Exception as exc:
        return CheckResult(
            name="schema version",
            passed=False,
            detail=f"no migration history in this database ({type(exc).__name__})",
        )

    applied = {row["migration_id"] for row in rows}
    on_disk = sorted(path.stem for path in default_directory().glob("*.sql"))
    pending = [name for name in on_disk if name not in applied]

    return CheckResult(
        name="schema version",
        passed=not pending,
        detail=(
            f"schema at {on_disk[-1] if on_disk else 'baseline'} ({len(applied)} applied)"
            if not pending
            else f"{len(pending)} migration(s) pending: {', '.join(pending)}"
        ),
    )


ALL_CHECKS: tuple[Callable[[Connection[DictRow]], CheckResult], ...] = (
    check_duplicate_debates,
    check_duplicate_contributions,
    check_orphan_parents,
    check_day_counts,
    check_speech_text,
    check_search_index,
    check_division_votes,
    check_member_references,
    check_schema_current,
)


def run_all(connection: Connection[DictRow]) -> list[CheckResult]:
    return [check(connection) for check in ALL_CHECKS]
