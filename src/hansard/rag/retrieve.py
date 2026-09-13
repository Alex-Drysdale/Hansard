"""Retrieval: finding the passages an answer should be built from.

This module exists because naive RAG -- embed the question, take the nearest
five chunks, hand them to a model -- fails on exactly the questions people
actually ask of this data. Three failures, and what is done about each:

**"What did Wes Streeting say about cancer?"**
    Vector search returns passages *about cancer*. Most of them are by other
    people. Similarity has no notion of "by this person"; a speech about cancer
    by anyone is close to a question about cancer by anyone. The fix is not a
    better embedding, it is a **hard filter**: resolve the name against the
    member table and restrict the search to that member_id.

**"What's been said about X in the last month?"**
    "Last month" is a range, not a direction in vector space. Nothing in an
    embedding encodes recency, so a naive search happily returns the single
    best-matching passage from four months ago. Dates are a filter too.

**Questions naming a bill, a drug or a constituency.**
    Embeddings blur rare tokens. Ask about "Prax Lindsey" and a vector search
    returns generic passages about refineries. Postgres full-text search finds
    the literal token every time. The two methods fail in different directions,
    so both are run and the results fused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from psycopg import Connection
from psycopg.rows import DictRow

from hansard.config import Settings
from hansard.db import speeches as speeches_store
from hansard.logging_config import get_logger
from hansard.rag import embeddings
from hansard.rag.index import open_table

log = get_logger(__name__)

#: Words that look like a name but are not one. Without this, "What did the
#: Minister say" resolves "Minister" to a member and filters everything away.
_NAME_STOPWORDS = frozenset(
    {
        "the",
        "what",
        "did",
        "has",
        "have",
        "say",
        "said",
        "about",
        "and",
        "for",
        "minister",
        "government",
        "opposition",
        "house",
        "member",
        "members",
        "secretary",
        "state",
        "prime",
        "chancellor",
        "committee",
        "speaker",
        "labour",
        "conservative",
        "reform",
        "green",
        "party",
        "mp",
        "mps",
        "anyone",
        "anybody",
        "everyone",
        "parliament",
        "commons",
        "lords",
    }
)

#: "Wes Streeting", "Dr Kieran Mullan" -- two or three capitalised words.
_NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z'\-]+){1,2})\b")

_RELATIVE_PERIODS = (
    (re.compile(r"\blast\s+week\b", re.I), 7),
    (re.compile(r"\bpast\s+week\b", re.I), 7),
    (re.compile(r"\blast\s+month\b", re.I), 31),
    (re.compile(r"\bpast\s+month\b", re.I), 31),
    (re.compile(r"\blast\s+(?:three|3)\s+months\b", re.I), 92),
    (re.compile(r"\blast\s+quarter\b", re.I), 92),
    (re.compile(r"\blast\s+year\b", re.I), 365),
    (re.compile(r"\brecent(?:ly)?\b", re.I), 31),
)


@dataclass(frozen=True, slots=True)
class QueryFilters:
    """Structured constraints pulled out of a natural-language question."""

    member_id: int | None = None
    member_name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Passage:
    """A retrieved passage, with why it was retrieved."""

    text: str
    speaker_name: str | None
    party: str | None
    debate_title: str
    sitting_date: date
    debate_ext_id: str
    member_id: int | None
    score: float
    sources: tuple[str, ...]
    has_carried_text: bool = False

    def citation(self) -> str:
        who = self.speaker_name or "Unattributed"
        party = f", {self.party}" if self.party else ""
        return f"{who}{party} — {self.debate_title}, {self.sitting_date:%d %b %Y}"


def parse_filters(
    connection: Connection[DictRow],
    question: str,
    *,
    today: date | None = None,
    latest_data_date: date | None = None,
) -> QueryFilters:
    """Pull a member and a date range out of a question.

    Deliberately conservative. A wrong filter is worse than no filter: it
    silently removes the passages that would have answered the question, and the
    model then answers confidently from whatever survived.
    """
    notes: list[str] = []
    member_id: int | None = None
    member_name: str | None = None

    for candidate in _NAME_PATTERN.findall(question):
        if all(word.lower() in _NAME_STOPWORDS for word in candidate.split()):
            continue
        matches = speeches_store.resolve_member_names(connection, candidate)
        if matches:
            member_id = matches[0]["member_id"]
            member_name = matches[0]["name"]
            notes.append(f"filtered to {member_name} (member {member_id})")
            break

    start_date: date | None = None
    end_date: date | None = None
    # Relative periods are measured from the most recent data, not from today.
    # The store covers January to April 2026; anchoring "last month" to the real
    # clock would return nothing at all and look like an empty database.
    anchor = latest_data_date or today or date.today()
    for pattern, days in _RELATIVE_PERIODS:
        if pattern.search(question):
            start_date = anchor - timedelta(days=days)
            end_date = anchor
            notes.append(f"restricted to {start_date} .. {end_date}")
            break

    for year_match in re.finditer(r"\b(20\d{2})\b", question):
        year = int(year_match.group(1))
        if start_date is None:
            start_date, end_date = date(year, 1, 1), date(year, 12, 31)
            notes.append(f"restricted to {year}")

    return QueryFilters(
        member_id=member_id,
        member_name=member_name,
        start_date=start_date,
        end_date=end_date,
        notes=tuple(notes),
    )


#: Words that frame a question rather than describe its subject. Stripped
#: before the keyword search, never from the vector query -- an embedding copes
#: with them, a tsquery does not.
#:
#: Deliberately contains no content words. Anything removed here is a term a
#: speech will not contain, and removing a real one would silently narrow the
#: search instead of widening it.
_QUESTION_WORDS = frozenset(
    {
        "what",
        "whats",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "did",
        "does",
        "do",
        "has",
        "have",
        "had",
        "is",
        "are",
        "was",
        "were",
        "been",
        "say",
        "said",
        "says",
        "saying",
        "tell",
        "told",
        "mention",
        "mentions",
        "mentioned",
        "discuss",
        "discussed",
        "about",
        "regarding",
        "anything",
        "anyone",
        "anybody",
        "everything",
        "please",
        "there",
        "their",
        "them",
        "they",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "for",
        "to",
        "any",
    }
)

#: Removed only when a date filter has already consumed them, so "last year's
#: settlement" keeps "year" unless the range was actually applied.
_PERIOD_WORDS = frozenset(
    {"recently", "recent", "lately", "last", "past", "week", "month", "year", "quarter"}
)


def topic_query(question: str, filters: QueryFilters) -> str:
    """Reduce a question to the words a speech would actually contain.

    The bug this fixes is easy to miss and total. Postgres'
    ``websearch_to_tsquery`` ANDs its terms, so passing the whole question
    "What did Wes Streeting say about cancer?" produces
    ``'wes' & 'street' & 'say' & 'cancer'`` -- and a speech *by* Streeting
    about cancer contains none of the first three. Keyword search returned
    nothing for exactly the questions it should be best at, leaving hybrid
    retrieval running on one leg without saying so.

    The speaker's name is already a metadata filter, so it has no business in
    the text query as well.
    """
    drop = set(_QUESTION_WORDS)
    if filters.member_name:
        drop.update(part.lower() for part in filters.member_name.split())
    if filters.start_date is not None:
        drop.update(_PERIOD_WORDS)

    words = [
        word
        for raw in question.split()
        if (word := raw.strip(".,?!;:\"'()").lower()) and word not in drop
    ]
    # Fall back rather than search for nothing: a question that is *only*
    # framing ("what did Wes Streeting say?") still has a member filter, and an
    # empty tsquery would throw away the keyword half entirely.
    return " ".join(words) if words else question


def _lance_filter(filters: QueryFilters) -> str | None:
    clauses: list[str] = []
    if filters.member_id is not None:
        clauses.append(f"member_id = {filters.member_id}")
    if filters.start_date is not None:
        clauses.append(f"sitting_ordinal >= {filters.start_date.toordinal()}")
    if filters.end_date is not None:
        clauses.append(f"sitting_ordinal <= {filters.end_date.toordinal()}")
    return " AND ".join(clauses) if clauses else None


def vector_search(
    settings: Settings, question: str, *, filters: QueryFilters, limit: int = 12
) -> list[Passage]:
    """Nearest passages by meaning, inside any metadata filter."""
    table = open_table(settings.vector_path)
    if table is None:
        return []

    vector = embeddings.embed_query(question, model_name=settings.embedding_model)
    search = table.search(vector).limit(limit)
    where = _lance_filter(filters)
    if where:
        # prefilter: apply the filter *before* the nearest-neighbour search, so
        # the limit is spent on rows that can actually qualify. Filtering
        # afterwards would return the global top-k and then discard most of it,
        # often leaving nothing.
        search = search.where(where, prefilter=True)

    results = search.to_list()
    passages = []
    for row in results:
        # LanceDB returns L2 distance; smaller is closer. Convert so that larger
        # is better, matching the keyword score and making fusion readable.
        distance = float(row.get("_distance", 0.0))
        passages.append(
            Passage(
                text=row["text"],
                speaker_name=row["speaker_name"] or None,
                party=row["party"] or None,
                debate_title=row["debate_title"],
                sitting_date=row["sitting_date"],
                debate_ext_id=row["debate_ext_id"],
                member_id=row["member_id"] or None,
                score=1.0 / (1.0 + distance),
                sources=("vector",),
                has_carried_text=bool(row["has_carried_text"]),
            )
        )
    return passages


def keyword_passages(
    connection: Connection[DictRow], question: str, *, filters: QueryFilters, limit: int = 12
) -> list[Passage]:
    """Passages containing the question's actual words.

    Searches the topic terms, not the raw question -- see :func:`topic_query`.
    """
    rows = speeches_store.keyword_search(
        connection,
        topic_query(question, filters),
        member_id=filters.member_id,
        start_date=filters.start_date,
        end_date=filters.end_date,
        limit=limit,
    )
    return [
        Passage(
            text=row["body_text"],
            speaker_name=row["speaker_name"],
            party=row["party"],
            debate_title=row["debate_title"],
            sitting_date=row["sitting_date"],
            debate_ext_id=row["debate_ext_id"],
            member_id=row["member_id"],
            score=float(row["rank"]),
            sources=("keyword",),
        )
        for row in rows
    ]


def fuse(vector_hits: list[Passage], keyword_hits: list[Passage], *, limit: int) -> list[Passage]:
    """Combine two ranked lists with reciprocal rank fusion.

    RRF scores by *position* rather than by raw score, which is the point: a
    cosine distance and a ts_rank are not on the same scale and cannot be
    added. It also rewards agreement -- a passage both methods rank highly wins
    over one that only one method loved.
    """
    k = 60  # standard RRF damping; large enough that rank 1 does not dominate
    scored: dict[tuple[str, int], tuple[float, Passage, set[str]]] = {}

    for hits in (vector_hits, keyword_hits):
        for rank, passage in enumerate(hits, start=1):
            key = (passage.debate_ext_id, hash(passage.text[:200]))
            contribution = 1.0 / (k + rank)
            if key in scored:
                score, existing, sources = scored[key]
                scored[key] = (score + contribution, existing, sources | set(passage.sources))
            else:
                scored[key] = (contribution, passage, set(passage.sources))

    ranked = sorted(scored.values(), key=lambda item: item[0], reverse=True)
    return [
        Passage(
            text=passage.text,
            speaker_name=passage.speaker_name,
            party=passage.party,
            debate_title=passage.debate_title,
            sitting_date=passage.sitting_date,
            debate_ext_id=passage.debate_ext_id,
            member_id=passage.member_id,
            score=score,
            sources=tuple(sorted(sources)),
            has_carried_text=passage.has_carried_text,
        )
        for score, passage, sources in ranked[:limit]
    ]


def retrieve(
    connection: Connection[DictRow],
    settings: Settings,
    question: str,
    *,
    limit: int = 8,
    naive: bool = False,
    latest_data_date: date | None = None,
) -> tuple[list[Passage], QueryFilters]:
    """Find passages for a question.

    ``naive=True`` deliberately reproduces the textbook version -- embed, take
    the top k, no filters, no keyword search -- so the two can be compared side
    by side. It is there to be shown failing.
    """
    if naive:
        empty = QueryFilters(notes=("naive mode: no filters, vector search only",))
        return vector_search(settings, question, filters=empty, limit=limit), empty

    filters = parse_filters(connection, question, latest_data_date=latest_data_date)
    # The vector side gets the whole question -- an embedding handles the
    # framing words fine, and they carry intent. Only the tsquery needs them
    # stripped.
    vector_hits = vector_search(settings, question, filters=filters, limit=limit * 2)
    keyword_hits = keyword_passages(connection, question, filters=filters, limit=limit * 2)

    log.info(
        "retrieve.hits",
        vector=len(vector_hits),
        keyword=len(keyword_hits),
        member_id=filters.member_id,
        topic=topic_query(question, filters),
    )
    return fuse(vector_hits, keyword_hits, limit=limit), filters
