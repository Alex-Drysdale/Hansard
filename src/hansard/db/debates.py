"""Storing debates, transcripts and the members implied by them.

Every write is idempotent: running the same ingest twice produces the same
database, not two copies of it. That comes from keying each table on an
identifier Parliament itself owns, and using UPSERT rather than INSERT.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from psycopg import Connection
from psycopg.rows import DictRow

from hansard.pipeline.normalise import ContributionRow, NormalisedDebate


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
# Sitting days
# ---------------------------------------------------------------------------


def record_sitting_day(
    connection: Connection[DictRow], *, house: str, sitting_date: date, debate_count: int
) -> None:
    """Note that we enumerated this day. ``first_seen_at`` survives re-runs."""
    connection.execute(
        """
        INSERT INTO sitting_day (house, sitting_date, debate_count)
        VALUES (%s, %s, %s)
        ON CONFLICT (house, sitting_date) DO UPDATE SET
            debate_count    = EXCLUDED.debate_count,
            last_fetched_at = now()
        """,
        (house, sitting_date, debate_count),
    )


def complete_sitting_day(
    connection: Connection[DictRow], *, house: str, sitting_date: date
) -> None:
    """Mark a day fully ingested, which is what ``--resume`` skips on."""
    connection.execute(
        "UPDATE sitting_day SET completed_at = now() WHERE house = %s AND sitting_date = %s",
        (house, sitting_date),
    )


def completed_sitting_days(connection: Connection[DictRow], house: str) -> set[date]:
    rows = connection.execute(
        "SELECT sitting_date FROM sitting_day WHERE house = %s AND completed_at IS NOT NULL",
        (house,),
    ).fetchall()
    return {row["sitting_date"] for row in rows}


# ---------------------------------------------------------------------------
# Debates
# ---------------------------------------------------------------------------


def existing_content_hash(connection: Connection[DictRow], ext_id: str) -> str | None:
    row = connection.execute(
        "SELECT content_hash FROM debate WHERE ext_id = %s", (ext_id,)
    ).fetchone()
    return row["content_hash"] if row else None


def save_debate(connection: Connection[DictRow], normalised: NormalisedDebate) -> DebateWriteResult:
    """Write one debate and its contributions.

    Short-circuits when the content hash is unchanged: the common case for a
    re-run is that nothing has moved, and skipping the write keeps a repeat
    ingest fast and leaves ``last_fetched_at`` as the only churn.

    The caller is responsible for the transaction, so that a debate and the day
    it belongs to can be committed together.
    """
    debate = normalised.debate
    previous_hash = existing_content_hash(connection, debate.ext_id)

    if previous_hash == debate.content_hash:
        connection.execute(
            "UPDATE debate SET last_fetched_at = now() WHERE ext_id = %s", (debate.ext_id,)
        )
        return DebateWriteResult(WriteOutcome.UNCHANGED, 0)

    connection.execute(
        """
        INSERT INTO debate (
            ext_id, hansard_id, parent_ext_id, parent_title, depth, title, house,
            sitting_date, location, hrs_tag, debate_type_id, volume_no,
            content_last_updated, contribution_count, word_count, content_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ext_id) DO UPDATE SET
            hansard_id           = EXCLUDED.hansard_id,
            parent_ext_id        = EXCLUDED.parent_ext_id,
            parent_title         = EXCLUDED.parent_title,
            depth                = EXCLUDED.depth,
            title                = EXCLUDED.title,
            house                = EXCLUDED.house,
            sitting_date         = EXCLUDED.sitting_date,
            location             = EXCLUDED.location,
            hrs_tag              = EXCLUDED.hrs_tag,
            debate_type_id       = EXCLUDED.debate_type_id,
            volume_no            = EXCLUDED.volume_no,
            content_last_updated = EXCLUDED.content_last_updated,
            contribution_count   = EXCLUDED.contribution_count,
            word_count           = EXCLUDED.word_count,
            content_hash         = EXCLUDED.content_hash,
            last_fetched_at      = now(),
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
        ),
    )

    # Members first: contribution.member_id is a foreign key into member, so the
    # referenced rows must exist before the transcript rows that cite them.
    upsert_members_from_transcript(connection, normalised.contributions)
    _replace_contributions(connection, debate.ext_id, normalised.contributions)

    outcome = WriteOutcome.INSERTED if previous_hash is None else WriteOutcome.UPDATED
    return DebateWriteResult(outcome, len(normalised.contributions))


def _replace_contributions(
    connection: Connection[DictRow], debate_ext_id: str, rows: Sequence[ContributionRow]
) -> None:
    """Make the stored transcript exactly match the one we just fetched.

    Delete-then-insert rather than upsert-only, because a revised debate can
    *remove* a contribution. An upsert alone would leave the withdrawn row
    behind for ever, quietly corrupting every count derived from it.

    Nothing here touches a search index: ``body_tsv`` is a generated column, so
    Postgres maintains it as part of the same statement. That is the whole
    reason for preferring a generated column over the hand-synchronised FTS
    table this replaces.
    """
    connection.execute("DELETE FROM contribution WHERE debate_ext_id = %s", (debate_ext_id,))
    if not rows:
        return

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO contribution (
                item_id, debate_ext_id, external_id, order_in_section, item_type, hrs_tag,
                is_speech, member_id, attributed_to, speaker_name, speaker_key, speaker_role,
                party, constituency, body_html, body_text, word_count, timecode,
                is_reiteration, content_hash, speaker_member_id, attribution
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            """,
            [
                (
                    row.item_id,
                    row.debate_ext_id,
                    row.external_id,
                    row.order_in_section,
                    row.item_type,
                    row.hrs_tag,
                    row.is_speech,
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
                    row.is_reiteration,
                    row.content_hash,
                    row.speaker_member_id,
                    row.attribution,
                )
                for row in rows
            ],
        )


def upsert_members_from_transcript(
    connection: Connection[DictRow], rows: Iterable[ContributionRow]
) -> None:
    """Create or update members from what a transcript told us.

    Writes only the ``*_hansard`` columns. The Members API owns the plain ones,
    so a debate ingest can never overwrite what source two established -- the
    two sources sit side by side and the ``member_resolved`` view decides which
    to prefer.

    COALESCE on update because Hansard states a party only on a member's first
    turn in a debate; a later bare-name mention must not erase what we learned.
    """
    seen: dict[int, tuple[str, str | None, str | None]] = {}
    for row in rows:
        if row.member_id is None:
            continue
        # Every member_id we cite needs a row, even one Hansard gave no name
        # for, or the foreign key on contribution.member_id would reject it.
        display_name = row.speaker_name or row.attributed_to or f"member {row.member_id}"
        name, party, constituency = seen.get(row.member_id, ("", None, None))
        seen[row.member_id] = (
            name or display_name,
            party or row.party,
            constituency or row.constituency,
        )

    if not seen:
        return

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO member (member_id, display_name_hansard, party_hansard,
                                constituency_hansard)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (member_id) DO UPDATE SET
                display_name_hansard = EXCLUDED.display_name_hansard,
                party_hansard        = COALESCE(EXCLUDED.party_hansard,
                                                member.party_hansard),
                constituency_hansard = COALESCE(EXCLUDED.constituency_hansard,
                                                member.constituency_hansard),
                last_seen_at         = now()
            """,
            [
                (member_id, name, party, constituency)
                for member_id, (name, party, constituency) in seen.items()
            ],
        )


def refresh_member_counts(connection: Connection[DictRow]) -> None:
    """Recompute member.contribution_count from the contributions table.

    A derived column, refreshed once at the end of a run rather than incremented
    per write, so it can never drift out of step with the rows it summarises.

    Counts against ``speaker_member_id``, so a member gets credit for the
    continuation paragraphs of their own speech rather than only its opening.
    """
    connection.execute(
        """
        UPDATE member SET contribution_count = COALESCE(counts.n, 0)
          FROM (
                SELECT m.member_id,
                       (SELECT COUNT(*) FROM contribution c
                         WHERE c.speaker_member_id = m.member_id AND c.is_speech) AS n
                  FROM member m
               ) AS counts
         WHERE member.member_id = counts.member_id
        """
    )


def all_debate_ids(connection: Connection[DictRow]) -> list[str]:
    """Every stored debate, for a full reattribution pass.

    Deliberately not "only the ones still unattributed". Attribution is an
    inference, and inferences get corrected: when the rules change, every debate
    needs recomputing, including ones a previous pass already marked. Recomputing
    all of them takes about a minute and cannot leave a stale answer behind.

    A re-ingest would not do this job either -- the content hash covers what
    Hansard sent, not what we inferred from it, so unchanged debates are
    correctly skipped. This reads the rows we already hold instead.
    """
    rows = connection.execute("SELECT ext_id FROM debate ORDER BY ext_id").fetchall()
    return [row["ext_id"] for row in rows]


def load_contributions_for_attribution(
    connection: Connection[DictRow], debate_ext_id: str
) -> list[DictRow]:
    """The fields resolve_speakers needs, for one debate, in order."""
    return connection.execute(
        """
        SELECT item_id, order_in_section, item_type, hrs_tag, member_id, attributed_to,
               body_text, word_count, is_speech
          FROM contribution WHERE debate_ext_id = %s ORDER BY order_in_section
        """,
        (debate_ext_id,),
    ).fetchall()


def apply_attribution(
    connection: Connection[DictRow], resolved: dict[int, tuple[int | None, str]]
) -> int:
    """Write resolved speakers back. Returns the number of carried rows."""
    if not resolved:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            UPDATE contribution
               SET speaker_member_id = %s, attribution = %s
             WHERE item_id = %s
            """,
            [(member_id, source, item_id) for item_id, (member_id, source) in resolved.items()],
        )
    return sum(1 for _, source in resolved.values() if source == "carried")


def debates_with_divisions(
    connection: Connection[DictRow], *, house: str, start: date, end: date
) -> list[str]:
    """Debates whose transcript contained a Division item.

    The divisions job walks these rather than every debate: Hansard records the
    marker in the transcript, so we already know which sections are worth asking
    about and can skip the ~95% that never divided.
    """
    rows = connection.execute(
        """
        SELECT DISTINCT d.ext_id
          FROM debate AS d
          JOIN contribution AS c ON c.debate_ext_id = d.ext_id
         WHERE c.item_type = 'Division'
           AND d.house = %s
           AND d.sitting_date BETWEEN %s AND %s
         ORDER BY d.ext_id
        """,
        (house, start, end),
    ).fetchall()
    return [row["ext_id"] for row in rows]
