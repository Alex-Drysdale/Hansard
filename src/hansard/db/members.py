"""Storing members as the Members API states them.

Writes the plain columns only. The ``*_hansard`` columns belong to the debate
ingest, so the two sources never overwrite each other and a disagreement stays
visible in the table rather than being silently resolved by whichever job ran
last.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from psycopg import Connection
from psycopg.rows import DictRow

from hansard.pipeline.normalise import MemberRow


@dataclass(frozen=True, slots=True)
class MemberSyncResult:
    seen: int
    inserted: int
    updated: int


def save_members(connection: Connection[DictRow], rows: Sequence[MemberRow]) -> MemberSyncResult:
    """Upsert member records from source two.

    Returns insert/update counts, taken from Postgres rather than guessed:
    ``xmax = 0`` is true exactly for tuples this statement created, which is how
    a single UPSERT can report which rows were new.
    """
    if not rows:
        return MemberSyncResult(0, 0, 0)

    inserted = 0
    with connection.cursor() as cursor:
        for row in rows:
            cursor.execute(
                """
                INSERT INTO member (
                    member_id, display_name, full_title, list_as, party,
                    party_abbreviation, constituency, house, gender, thumbnail_url,
                    membership_start, membership_end, is_current, synced_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (member_id) DO UPDATE SET
                    display_name       = EXCLUDED.display_name,
                    full_title         = EXCLUDED.full_title,
                    list_as            = EXCLUDED.list_as,
                    party              = EXCLUDED.party,
                    party_abbreviation = EXCLUDED.party_abbreviation,
                    constituency       = EXCLUDED.constituency,
                    house              = EXCLUDED.house,
                    gender             = EXCLUDED.gender,
                    thumbnail_url      = EXCLUDED.thumbnail_url,
                    membership_start   = EXCLUDED.membership_start,
                    membership_end     = EXCLUDED.membership_end,
                    is_current         = EXCLUDED.is_current,
                    synced_at          = now(),
                    last_seen_at       = now()
                RETURNING (xmax = 0) AS was_inserted
                """,
                (
                    row.member_id,
                    row.display_name,
                    row.full_title,
                    row.list_as,
                    row.party,
                    row.party_abbreviation,
                    row.constituency,
                    row.house,
                    row.gender,
                    row.thumbnail_url,
                    row.membership_start,
                    row.membership_end,
                    row.is_current,
                ),
            )
            result = cursor.fetchone()
            if result is not None and result["was_inserted"]:
                inserted += 1

    return MemberSyncResult(seen=len(rows), inserted=inserted, updated=len(rows) - inserted)


def unsynced_member_ids(connection: Connection[DictRow], limit: int | None = None) -> list[int]:
    """Members we know of from transcripts but have never fetched from source two.

    This is what makes the members job incremental. A full walk of the search
    endpoint costs ~33 requests; topping up the handful of stragglers that a new
    month of debates introduced costs one request each.
    """
    sql = "SELECT member_id FROM member WHERE synced_at IS NULL ORDER BY member_id"
    if limit is not None:
        rows = connection.execute(sql + " LIMIT %s", (limit,)).fetchall()
    else:
        rows = connection.execute(sql).fetchall()
    return [row["member_id"] for row in rows]


def coverage(connection: Connection[DictRow]) -> DictRow:
    """How much of the store source two has reached.

    The denominator matters here, and an earlier version got it wrong. About a
    quarter of speeches carry no member id at all -- procedural text, motions,
    "Several hon. Members rose" -- and Hansard attributes those to nobody, so no
    source could ever name a party for them. Counting them made a 100% result
    look like 77%.

    So the headline is measured over *attributed* speeches, and the unattributed
    ones are reported separately rather than quietly dragging the number down.
    """
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM member)                                  AS members,
            (SELECT COUNT(*) FROM member WHERE synced_at IS NOT NULL)      AS synced,
            (SELECT COUNT(*) FROM member WHERE party_hansard IS NOT NULL)  AS party_from_hansard,
            (SELECT COUNT(*) FROM member_resolved WHERE party IS NOT NULL) AS party_resolved,
            (SELECT COUNT(*) FROM member
              WHERE party_hansard IS NULL AND party IS NOT NULL)           AS party_only_from_api,
            (SELECT COUNT(*) FROM speech)                                  AS speeches,
            (SELECT COUNT(*) FROM speech WHERE member_id IS NULL)          AS speeches_unattributed,
            (SELECT COUNT(*) FROM speech WHERE member_id IS NOT NULL)      AS speeches_attributed,
            (SELECT COUNT(*) FROM speech
              WHERE member_id IS NOT NULL AND party IS NOT NULL)           AS attributed_with_party
        """
    ).fetchone()
    assert row is not None
    return row
