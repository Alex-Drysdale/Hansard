"""Reassembling whole speeches out of stored paragraph rows.

The database stores a speech as several ``contribution`` rows, because that is
how Hansard sends it. For retrieval we want the speech back in one piece, with
its speaker attached.

That is only possible because attribution was resolved first: a run of rows
belongs to one turn when they share a ``speaker_member_id``, and a new turn
begins at each row Hansard actually named.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from psycopg import Connection
from psycopg.rows import DictRow

# A turn is a maximal run of rows with the same resolved speaker, bounded by the
# next row Hansard named. The running count of 'stated' rows is what numbers
# them: it increments exactly when a new speaker is announced.
TURNS_SQL = """
WITH marked AS (
    SELECT c.item_id,
           c.debate_ext_id,
           c.order_in_section,
           c.body_text,
           c.word_count,
           c.attribution,
           c.speaker_member_id,
           c.speaker_name        AS row_speaker_name,
           SUM(CASE WHEN c.attribution = 'stated' THEN 1 ELSE 0 END)
               OVER (PARTITION BY c.debate_ext_id ORDER BY c.order_in_section) AS turn_no
      FROM contribution AS c
     WHERE c.is_speech
       AND c.speaker_member_id IS NOT NULL
       AND c.word_count > 0
)
SELECT m.debate_ext_id,
       m.turn_no,
       m.speaker_member_id                                   AS member_id,
       MIN(m.order_in_section)                               AS order_in_section,
       SUM(m.word_count)                                     AS word_count,
       array_agg(m.body_text ORDER BY m.order_in_section)     AS paragraphs,
       bool_or(m.attribution = 'carried')                    AS has_carried_text,
       d.title                                               AS debate_title,
       d.sitting_date,
       d.house::text                                         AS house,
       d.location,
       COALESCE(mem.display_name, MIN(m.row_speaker_name), mem.display_name_hansard)
                                                             AS speaker_name,
       COALESCE(mem.party, mem.party_hansard)                AS party,
       COALESCE(mem.constituency, mem.constituency_hansard)  AS constituency
  FROM marked AS m
  JOIN debate AS d ON d.ext_id = m.debate_ext_id
  LEFT JOIN member AS mem ON mem.member_id = m.speaker_member_id
 WHERE (%(start_date)s::date IS NULL OR d.sitting_date >= %(start_date)s)
   AND (%(end_date)s::date IS NULL OR d.sitting_date <= %(end_date)s)
 GROUP BY m.debate_ext_id, m.turn_no, m.speaker_member_id,
          d.title, d.sitting_date, d.house, d.location,
          mem.display_name, mem.display_name_hansard, mem.party, mem.party_hansard,
          mem.constituency, mem.constituency_hansard
 ORDER BY d.sitting_date, m.debate_ext_id, m.turn_no
"""


def stream_speech_turns(
    connection: Connection[DictRow],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 500,
) -> Iterator[DictRow]:
    """Every speech turn in the range, streamed rather than materialised.

    A server-side cursor, because the alternative was measured and is not
    acceptable: fetching all 36,000 turns at once holds the entire 6.7 million
    word corpus in Python strings before a single one has been embedded, and an
    index build died of it. Postgres now holds the result set and hands over a
    page at a time.

    Server-side cursors need a transaction, which autocommit connections do not
    have, so one is opened explicitly.
    """
    params = {"start_date": start_date, "end_date": end_date}
    with connection.transaction(), connection.cursor(name="speech_turns") as cursor:
        cursor.execute(TURNS_SQL, params)
        while batch := cursor.fetchmany(batch_size):
            yield from batch


def iter_speech_turns(
    connection: Connection[DictRow],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[DictRow]:
    """Every speech turn at once. Only for callers that genuinely need a list."""
    return connection.execute(
        TURNS_SQL, {"start_date": start_date, "end_date": end_date}
    ).fetchall()


def count_speech_turns(
    connection: Connection[DictRow],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) AS n FROM ({TURNS_SQL}) AS turns",
        {"start_date": start_date, "end_date": end_date},
    ).fetchone()
    return int(row["n"]) if row else 0


def resolve_member_names(connection: Connection[DictRow], fragment: str) -> list[DictRow]:
    """Members whose name matches a fragment from a question.

    Used to turn "what did Wes Streeting say" into a hard filter on member_id.
    Matching is done in Postgres against the member table rather than against
    the passages, because a name that appears *in* a speech is usually someone
    being referred to, not the person speaking.
    """
    return connection.execute(
        """
        SELECT member_id, name, party, constituency
          FROM member_resolved
         WHERE name ILIKE %(pattern)s
            OR name ILIKE %(surname)s
         ORDER BY contribution_count DESC
         LIMIT 5
        """,
        {"pattern": f"%{fragment}%", "surname": f"% {fragment}"},
    ).fetchall()


def keyword_search(
    connection: Connection[DictRow],
    query: str,
    *,
    member_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 20,
) -> list[DictRow]:
    """Postgres full-text search over speeches.

    The other half of hybrid retrieval. Vector search finds passages that mean
    something similar; this finds passages that use the actual words. They fail
    in different directions, which is precisely why both are worth having: a
    question naming a drug, a bill or a constituency needs the literal token,
    and an embedding will happily return something merely thematically close.
    """
    return connection.execute(
        """
        SELECT s.item_id, s.debate_ext_id, s.debate_title, s.sitting_date, s.house,
               s.location, s.member_id, s.speaker_name, s.party, s.constituency,
               s.body_text, s.word_count,
               ts_rank(s.body_tsv, websearch_to_tsquery('english', %(query)s)) AS rank
          FROM speech AS s
         WHERE s.body_tsv @@ websearch_to_tsquery('english', %(query)s)
           AND (%(member_id)s::int IS NULL OR s.member_id = %(member_id)s)
           AND (%(start_date)s::date IS NULL OR s.sitting_date >= %(start_date)s)
           AND (%(end_date)s::date IS NULL OR s.sitting_date <= %(end_date)s)
         ORDER BY rank DESC, s.sitting_date DESC
         LIMIT %(limit)s
        """,
        {
            "query": query,
            "member_id": member_id,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        },
    ).fetchall()
