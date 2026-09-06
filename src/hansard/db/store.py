"""Writing to and reading from the local store.

Every write here is idempotent: running the same ingest twice produces the same
database, not two copies of it. That property comes from keying each table on an
identifier Hansard itself owns, and using UPSERT rather than INSERT.

The store deals in the row dataclasses from :mod:`hansard.pipeline.normalise`.
It never sees an HTTP response and never calls the API.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from hansard.pipeline.normalise import (
    ContributionRow,
    NormalisedDebate,
    utc_now,
)


class WriteOutcome(StrEnum):
    """What actually happened to a debate when we wrote it."""

    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class DebateWriteResult:
    outcome: WriteOutcome
    contributions_written: int


# ---------------------------------------------------------------------------
# Ingest run bookkeeping
# ---------------------------------------------------------------------------


def start_run(connection: sqlite3.Connection, *, house: str, start: date, end: date) -> int:
    """Record that an ingest has begun; returns the run id."""
    cursor = connection.execute(
        """
        INSERT INTO ingest_run (started_at, status, house, start_date, end_date)
        VALUES (?, 'running', ?, ?, ?)
        """,
        (utc_now(), house, start.isoformat(), end.isoformat()),
    )
    run_id = cursor.lastrowid
    assert run_id is not None
    return run_id


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    counters: dict[str, int],
    error_message: str | None = None,
) -> None:
    """Close out an ingest run with its final counters."""
    connection.execute(
        """
        UPDATE ingest_run
           SET finished_at = ?,
               status = ?,
               sitting_days = ?,
               debates_seen = ?,
               debates_inserted = ?,
               debates_updated = ?,
               debates_unchanged = ?,
               contributions_seen = ?,
               contributions_written = ?,
               errors = ?,
               error_message = ?
         WHERE id = ?
        """,
        (
            utc_now(),
            status,
            counters.get("sitting_days", 0),
            counters.get("debates_seen", 0),
            counters.get("debates_inserted", 0),
            counters.get("debates_updated", 0),
            counters.get("debates_unchanged", 0),
            counters.get("contributions_seen", 0),
            counters.get("contributions_written", 0),
            counters.get("errors", 0),
            error_message,
            run_id,
        ),
    )


def latest_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    # Annotated rather than returned directly: sqlite3.Cursor.fetchone is typed
    # as returning Any, and letting that escape would silence real type errors
    # at every call site.
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM ingest_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# Sitting days
# ---------------------------------------------------------------------------


def record_sitting_day(
    connection: sqlite3.Connection, *, house: str, sitting_date: date, debate_count: int
) -> None:
    """Note that we enumerated this day. ``first_seen_at`` survives re-runs."""
    now = utc_now()
    connection.execute(
        """
        INSERT INTO sitting_day (house, sitting_date, debate_count, first_seen_at, last_fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (house, sitting_date) DO UPDATE SET
            debate_count    = excluded.debate_count,
            last_fetched_at = excluded.last_fetched_at
        """,
        (house, sitting_date.isoformat(), debate_count, now, now),
    )


def complete_sitting_day(connection: sqlite3.Connection, *, house: str, sitting_date: date) -> None:
    """Mark a day fully ingested, which is what ``--resume`` skips on."""
    connection.execute(
        "UPDATE sitting_day SET completed_at = ? WHERE house = ? AND sitting_date = ?",
        (utc_now(), house, sitting_date.isoformat()),
    )


def completed_sitting_days(connection: sqlite3.Connection, house: str) -> set[date]:
    rows = connection.execute(
        "SELECT sitting_date FROM sitting_day WHERE house = ? AND completed_at IS NOT NULL",
        (house,),
    ).fetchall()
    return {date.fromisoformat(row["sitting_date"]) for row in rows}


# ---------------------------------------------------------------------------
# Debates and contributions
# ---------------------------------------------------------------------------


def existing_debate_hash(connection: sqlite3.Connection, ext_id: str) -> str | None:
    row = connection.execute(
        "SELECT content_hash FROM debate WHERE ext_id = ?", (ext_id,)
    ).fetchone()
    return row["content_hash"] if row else None


def save_debate(connection: sqlite3.Connection, normalised: NormalisedDebate) -> DebateWriteResult:
    """Write one debate and its contributions, atomically.

    Short-circuits when the content hash is unchanged: the common case for a
    re-run is that nothing has moved, and skipping the write keeps a repeat
    ingest fast and leaves ``last_fetched_at`` as the only churn.
    """
    debate = normalised.debate
    now = utc_now()
    previous_hash = existing_debate_hash(connection, debate.ext_id)

    if previous_hash == debate.content_hash:
        connection.execute(
            "UPDATE debate SET last_fetched_at = ? WHERE ext_id = ?", (now, debate.ext_id)
        )
        return DebateWriteResult(WriteOutcome.UNCHANGED, 0)

    connection.execute(
        """
        INSERT INTO debate (
            ext_id, hansard_id, parent_ext_id, parent_title, depth, title, house,
            sitting_date, location, hrs_tag, debate_type_id, volume_no,
            content_last_updated, contribution_count, word_count, content_hash,
            first_seen_at, last_fetched_at, revision
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT (ext_id) DO UPDATE SET
            hansard_id           = excluded.hansard_id,
            parent_ext_id        = excluded.parent_ext_id,
            parent_title         = excluded.parent_title,
            depth                = excluded.depth,
            title                = excluded.title,
            house                = excluded.house,
            sitting_date         = excluded.sitting_date,
            location             = excluded.location,
            hrs_tag              = excluded.hrs_tag,
            debate_type_id       = excluded.debate_type_id,
            volume_no            = excluded.volume_no,
            content_last_updated = excluded.content_last_updated,
            contribution_count   = excluded.contribution_count,
            word_count           = excluded.word_count,
            content_hash         = excluded.content_hash,
            last_fetched_at      = excluded.last_fetched_at,
            revision             = debate.revision + 1
        """,
        (
            debate.ext_id,
            debate.hansard_id,
            debate.parent_ext_id,
            debate.parent_title,
            debate.depth,
            debate.title,
            debate.house,
            debate.sitting_date,
            debate.location,
            debate.hrs_tag,
            debate.debate_type_id,
            debate.volume_no,
            debate.content_last_updated,
            debate.contribution_count,
            debate.word_count,
            debate.content_hash,
            now,
            now,
        ),
    )

    # Members first: contribution.member_id is a foreign key into member, so the
    # referenced rows have to exist before the transcript rows that cite them.
    _upsert_members(connection, normalised.contributions)
    _replace_contributions(connection, debate.ext_id, normalised.contributions)

    outcome = WriteOutcome.INSERTED if previous_hash is None else WriteOutcome.UPDATED
    return DebateWriteResult(outcome, len(normalised.contributions))


def _replace_contributions(
    connection: sqlite3.Connection, debate_ext_id: str, rows: Sequence[ContributionRow]
) -> None:
    """Make the stored transcript exactly match the one we just fetched.

    Delete-then-insert rather than upsert-only, because a revised debate can
    *remove* a contribution. An upsert alone would leave the withdrawn row
    behind for ever, quietly corrupting every count derived from it.

    The full-text index is not touched here: triggers in the schema mirror these
    writes into it, so this function stays the single place that decides what a
    debate's transcript contains.
    """
    connection.execute("DELETE FROM contribution WHERE debate_ext_id = ?", (debate_ext_id,))

    connection.executemany(
        """
        INSERT INTO contribution (
            item_id, debate_ext_id, external_id, order_in_section, item_type, hrs_tag,
            is_speech, member_id, attributed_to, speaker_name, speaker_key, speaker_role,
            party, constituency, body_html, body_text, word_count, timecode,
            is_reiteration, content_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.item_id,
                row.debate_ext_id,
                row.external_id,
                row.order_in_section,
                row.item_type,
                row.hrs_tag,
                int(row.is_speech),
                row.member_id,
                row.attributed_to,
                row.speaker_name,
                row.speaker_key,
                row.speaker_role,
                row.party,
                row.constituency,
                row.body_html,
                row.body_text,
                row.word_count,
                row.timecode,
                int(row.is_reiteration),
                row.content_hash,
            )
            for row in rows
        ],
    )


def _upsert_members(connection: sqlite3.Connection, rows: Iterable[ContributionRow]) -> None:
    """Maintain the member table from whatever the transcript told us.

    Hansard's debate payload has no member endpoint attached, so the member
    record is assembled from attribution strings. Later rows fill in fields
    earlier ones left blank -- a member who only ever spoke from the Dispatch
    Box has no constituency on that row, but will on another.
    """
    now = utc_now()
    seen: dict[int, tuple[str, str | None, str | None]] = {}
    for row in rows:
        if row.member_id is None:
            continue
        # Every member_id we cite must get a row, even one Hansard gave us no
        # name for, or the foreign key on contribution.member_id would reject it.
        display_name = row.speaker_name or row.attributed_to or f"member {row.member_id}"
        name, party, constituency = seen.get(row.member_id, ("", None, None))
        seen[row.member_id] = (
            name or display_name,
            party or row.party,
            constituency or row.constituency,
        )

    connection.executemany(
        """
        INSERT INTO member (member_id, display_name, party, constituency,
                            first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (member_id) DO UPDATE SET
            display_name = excluded.display_name,
            party        = COALESCE(excluded.party, member.party),
            constituency = COALESCE(excluded.constituency, member.constituency),
            last_seen_at = excluded.last_seen_at
        """,
        [
            (member_id, name, party, constituency, now, now)
            for member_id, (name, party, constituency) in seen.items()
        ],
    )


def refresh_member_counts(connection: sqlite3.Connection) -> None:
    """Recompute member.contribution_count from the contributions table.

    A derived column, refreshed once at the end of a run rather than incremented
    per write -- so it can never drift out of step with the rows it summarises.
    """
    connection.execute(
        """
        UPDATE member
           SET contribution_count = COALESCE((
                   SELECT COUNT(*) FROM contribution c
                    WHERE c.member_id = member.member_id AND c.is_speech = 1
               ), 0)
        """
    )
