"""The local vector store.

LanceDB, embedded: a directory of files, no server, no cloud. Chosen over a
hosted store because Phase 3 is about understanding retrieval, and an
operational dependency would add noise without teaching anything.

The index is a *derived* artefact. Postgres remains the source of truth; this
can be deleted and rebuilt at any time, and that property is what keeps the
staleness problem tractable -- see :func:`index_status`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from hansard.logging_config import get_logger
from hansard.rag.chunking import Chunk

log = get_logger(__name__)

TABLE_NAME = "speech_chunks"

#: Written into every row so a stale index is detectable rather than silently
#: wrong. Bump it whenever chunking or the embedding text changes shape.
INDEX_VERSION = 1


def schema(dimensions: int) -> pa.Schema:
    """Explicit Arrow schema.

    Declared rather than inferred: inference guesses a type from the first
    batch, so a column that is null in the first thousand rows and populated in
    the next -- `constituency`, for instance -- would be typed wrongly and the
    write would fail a long way into a slow job.
    """
    return pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("turn_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimensions)),
            pa.field("text", pa.string()),
            pa.field("debate_ext_id", pa.string()),
            pa.field("debate_title", pa.string()),
            pa.field("sitting_date", pa.date32()),
            # Also stored as an integer: LanceDB's SQL filter syntax handles
            # plain numeric comparison far more predictably than dates.
            pa.field("sitting_ordinal", pa.int32()),
            pa.field("house", pa.string()),
            pa.field("location", pa.string()),
            pa.field("member_id", pa.int32()),
            pa.field("speaker_name", pa.string()),
            pa.field("party", pa.string()),
            pa.field("constituency", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("word_count", pa.int32()),
            pa.field("has_carried_text", pa.bool_()),
            pa.field("index_version", pa.int32()),
            pa.field("indexed_at", pa.timestamp("s", tz="UTC")),
        ]
    )


def to_row(chunk: Chunk, vector: Sequence[float], indexed_at: datetime) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "turn_id": chunk.turn_id,
        "vector": list(vector),
        "text": chunk.text,
        "debate_ext_id": chunk.debate_ext_id,
        "debate_title": chunk.debate_title,
        "sitting_date": chunk.sitting_date,
        "sitting_ordinal": chunk.sitting_date.toordinal(),
        "house": chunk.house,
        "location": chunk.location or "",
        "member_id": chunk.member_id or 0,
        "speaker_name": chunk.speaker_name or "",
        "party": chunk.party or "",
        "constituency": chunk.constituency or "",
        "chunk_index": chunk.chunk_index,
        "word_count": chunk.word_count,
        "has_carried_text": chunk.has_carried_text,
        "index_version": INDEX_VERSION,
        "indexed_at": indexed_at,
    }


def connect(vector_path: Path):  # type: ignore[no-untyped-def]
    import lancedb

    vector_path.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(vector_path))


def open_table(vector_path: Path):  # type: ignore[no-untyped-def]
    """Open the chunk table, or None when nothing has been indexed yet.

    ``list_tables`` rather than the older ``table_names``: the latter is
    deprecated, and this project treats warnings as errors precisely so that
    such a call surfaces now rather than on the next dependency bump.
    """
    db = connect(vector_path)
    listing = db.list_tables()
    names = getattr(listing, "tables", listing)
    if TABLE_NAME not in names:
        return None
    return db.open_table(TABLE_NAME)


def create_table(vector_path: Path, dimensions: int, *, overwrite: bool = True):  # type: ignore[no-untyped-def]
    db = connect(vector_path)
    return db.create_table(
        TABLE_NAME,
        schema=schema(dimensions),
        mode="overwrite" if overwrite else "create",
    )


def open_or_create_table(vector_path: Path, dimensions: int):  # type: ignore[no-untyped-def]
    """Open the existing table, or create it. Used when resuming a build."""
    table = open_table(vector_path)
    return table if table is not None else create_table(vector_path, dimensions, overwrite=False)


def indexed_turn_ids(vector_path: Path) -> set[str]:
    """Turn ids already in the index, for resuming an interrupted build.

    Projected to a single column rather than read whole. ``to_arrow()`` takes no
    column argument and would materialise every vector and every passage --
    hundreds of megabytes, to learn which ids are present -- undoing the memory
    work that made the build survivable. Measured, this projection peaks at
    about 3MB across 35,000 rows.
    """
    table = open_table(vector_path)
    if table is None:
        return set()
    rows = table.count_rows()
    if rows == 0:
        return set()
    projected = table.search().select(["turn_id"]).limit(rows).to_arrow()
    return set(projected.column("turn_id").to_pylist())


@dataclass(frozen=True, slots=True)
class IndexStatus:
    exists: bool
    chunks: int = 0
    speeches: int = 0
    version: int | None = None
    built_at: datetime | None = None
    earliest: date | None = None
    latest: date | None = None
    #: Speech turns in Postgres that the index does not contain. The staleness
    #: number: an index built last week answers last week's questions.
    missing_turns: int = 0

    @property
    def is_stale(self) -> bool:
        return not self.exists or self.version != INDEX_VERSION or self.missing_turns > 0


def index_status(vector_path: Path, *, expected_turn_ids: set[str] | None = None) -> IndexStatus:
    """What the index holds, and whether it has fallen behind the database.

    Staleness is the failure naive RAG hides best: the query works, the answer
    is fluent, and it is simply missing anything ingested since the index was
    built. Reporting it is cheap; noticing it by accident is not.
    """
    table = open_table(vector_path)
    if table is None:
        return IndexStatus(exists=False)

    frame = table.to_arrow()
    if frame.num_rows == 0:
        return IndexStatus(exists=True)

    turn_ids = set(frame.column("turn_id").to_pylist())
    versions = set(frame.column("index_version").to_pylist())
    dates = frame.column("sitting_date").to_pylist()
    built = max(frame.column("indexed_at").to_pylist())

    missing = len(expected_turn_ids - turn_ids) if expected_turn_ids is not None else 0

    return IndexStatus(
        exists=True,
        chunks=frame.num_rows,
        speeches=len(turn_ids),
        version=versions.pop() if len(versions) == 1 else -1,
        built_at=built if built.tzinfo else built.replace(tzinfo=UTC),
        earliest=min(dates),
        latest=max(dates),
        missing_turns=missing,
    )
