"""Storing divisions and the votes cast in them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from psycopg import Connection
from psycopg.rows import DictRow

from hansard.db.debates import WriteOutcome
from hansard.logging_config import get_logger
from hansard.pipeline.normalise import NormalisedDivision

log = get_logger(__name__)


def _debate_exists(connection: Connection[DictRow], ext_id: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM debate WHERE ext_id = %s", (ext_id,)).fetchone()
        is not None
    )


@dataclass(frozen=True, slots=True)
class DivisionWriteResult:
    outcome: WriteOutcome
    votes_written: int


def existing_content_hash(connection: Connection[DictRow], ext_id: str) -> str | None:
    row = connection.execute(
        "SELECT content_hash FROM division WHERE ext_id = %s", (ext_id,)
    ).fetchone()
    return row["content_hash"] if row else None


def save_division(
    connection: Connection[DictRow], normalised: NormalisedDivision
) -> DivisionWriteResult:
    """Write one division and its votes, idempotently.

    The caller owns the transaction. Members referenced by a vote are created
    first if we have never seen them: an MP who voted but never spoke has no row
    from the debate ingest, and the foreign key would reject their vote.
    """
    division = normalised.division
    previous_hash = existing_content_hash(connection, division.ext_id)

    # The link to the debate is a real foreign key, so it has to point at a
    # section we actually hold. Normally it does -- the job only asks for
    # divisions belonging to stored debates -- but a payload can name a section
    # outside the ingested window, and losing the whole division over a broken
    # link would be the wrong trade. Drop the link, keep the votes.
    debate_ext_id = division.debate_ext_id
    if debate_ext_id is not None and not _debate_exists(connection, debate_ext_id):
        log.warning(
            "division.debate_not_stored",
            division_ext_id=division.ext_id,
            debate_ext_id=debate_ext_id,
        )
        debate_ext_id = None

    if previous_hash == division.content_hash:
        connection.execute(
            "UPDATE division SET last_fetched_at = now() WHERE ext_id = %s", (division.ext_id,)
        )
        return DivisionWriteResult(WriteOutcome.UNCHANGED, 0)

    connection.execute(
        """
        INSERT INTO division (
            ext_id, division_id, debate_ext_id, house, division_date, division_time,
            number, debate_section, ayes_count, noes_count, is_committee,
            text_before_vote, text_after_vote, content_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ext_id) DO UPDATE SET
            division_id      = EXCLUDED.division_id,
            debate_ext_id    = EXCLUDED.debate_ext_id,
            house            = EXCLUDED.house,
            division_date    = EXCLUDED.division_date,
            division_time    = EXCLUDED.division_time,
            number           = EXCLUDED.number,
            debate_section   = EXCLUDED.debate_section,
            ayes_count       = EXCLUDED.ayes_count,
            noes_count       = EXCLUDED.noes_count,
            is_committee     = EXCLUDED.is_committee,
            text_before_vote = EXCLUDED.text_before_vote,
            text_after_vote  = EXCLUDED.text_after_vote,
            content_hash     = EXCLUDED.content_hash,
            last_fetched_at  = now()
        """,
        (
            division.ext_id,
            division.division_id,
            debate_ext_id,
            division.house,
            division.division_date,
            division.division_time,
            division.number,
            division.debate_section,
            division.ayes_count,
            division.noes_count,
            division.is_committee,
            division.text_before_vote,
            division.text_after_vote,
            division.content_hash,
        ),
    )

    _ensure_members_exist(connection, normalised)
    _replace_votes(connection, normalised)

    outcome = WriteOutcome.INSERTED if previous_hash is None else WriteOutcome.UPDATED
    return DivisionWriteResult(outcome, len(normalised.votes))


def _ensure_members_exist(connection: Connection[DictRow], normalised: NormalisedDivision) -> None:
    """Create member rows for voters we have never seen speak.

    Voting is the more common act: a backbencher can go a month without
    speaking and still vote thirty times. Without this the foreign key on
    division_vote.member_id would reject their votes outright.

    DO NOTHING on conflict, because an existing row may already carry better
    information from a transcript or from source two.
    """
    if not normalised.votes:
        return
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO member (member_id, display_name_hansard, party_hansard)
            VALUES (%s, %s, %s)
            ON CONFLICT (member_id) DO NOTHING
            """,
            [
                (vote.member_id, vote.list_as or f"member {vote.member_id}", vote.party_at_vote)
                for vote in normalised.votes
            ],
        )


def _replace_votes(connection: Connection[DictRow], normalised: NormalisedDivision) -> None:
    """Make the stored votes exactly match the payload we just fetched."""
    connection.execute(
        "DELETE FROM division_vote WHERE division_ext_id = %s", (normalised.division.ext_id,)
    )
    if not normalised.votes:
        return
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO division_vote (
                division_ext_id, member_id, lobby, is_teller, list_as, party_at_vote
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    vote.division_ext_id,
                    vote.member_id,
                    vote.lobby,
                    vote.is_teller,
                    vote.list_as,
                    vote.party_at_vote,
                )
                for vote in normalised.votes
            ],
        )


def count_discrepancies(connection: Connection[DictRow]) -> list[DictRow]:
    """Divisions where Hansard's stated counts differ from the members it listed.

    Reported, never corrected. Excluding tellers explains most of the gap but
    not all -- of 395 real divisions, 101 reconcile under no rule at all -- so
    this is an upstream property to surface rather than a defect to fix here.
    """
    return connection.execute(
        """
        SELECT d.ext_id, d.division_date, d.debate_section,
               d.ayes_count, d.noes_count,
               COUNT(*) FILTER (WHERE v.lobby = 'aye') AS ayes_stored,
               COUNT(*) FILTER (WHERE v.lobby = 'no')  AS noes_stored
          FROM division AS d
          LEFT JOIN division_vote AS v ON v.division_ext_id = d.ext_id
         GROUP BY d.ext_id, d.division_date, d.debate_section, d.ayes_count, d.noes_count
        HAVING d.ayes_count <> COUNT(*) FILTER (WHERE v.lobby = 'aye')
            OR d.noes_count <> COUNT(*) FILTER (WHERE v.lobby = 'no')
         ORDER BY d.division_date
        """
    ).fetchall()


def stored_division_ids(
    connection: Connection[DictRow], *, house: str, start: date, end: date
) -> set[str]:
    rows = connection.execute(
        "SELECT ext_id FROM division WHERE house = %s AND division_date BETWEEN %s AND %s",
        (house, start, end),
    ).fetchall()
    return {row["ext_id"] for row in rows}
