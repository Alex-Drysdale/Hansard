"""Building the vector index from the database.

A full rebuild rather than an incremental update. The index is derived, it takes
half an hour, and an incremental path would need change tracking that could
itself go stale -- exactly the bug class this phase is meant to teach.
Rebuilding is boring and always correct.

**It streams, and that is not an optimisation.** The first version loaded all
36,000 speech turns and then built all 40,000 chunks before embedding any of
them, holding the whole 6.7-million-word corpus in Python strings at once. The
operating system killed it for running the machine out of memory. Now turns
arrive a page at a time from a server-side cursor, and chunks are embedded and
written in batches that are then dropped. Peak memory is a function of the batch
size, not of the size of the corpus.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import Connection
from psycopg.rows import DictRow

from hansard.config import Settings
from hansard.db import speeches as speeches_store
from hansard.logging_config import get_logger
from hansard.rag import embeddings, index
from hansard.rag.chunking import Chunk, SpeechTurn, chunk_turn

log = get_logger(__name__)

#: Chunks embedded and written per batch. Large enough to keep the worker
#: processes busy -- parallel embedding pays a startup cost per call -- and
#: small enough that this, rather than the corpus, sets peak memory.
EMBED_BATCH = 512

#: Speeches below this are dropped. They are real, but "I agree with my hon.
#: Friend" retrieves for almost any question and displaces a passage that would
#: actually answer it.
MIN_TURN_WORDS = 25


@dataclass(frozen=True, slots=True)
class BuildReport:
    turns: int
    chunks: int
    #: Speeches still to index. Non-zero only after a ``--limit`` run.
    remaining: int
    dimensions: int
    seconds: float


def turn_to_speech(row: DictRow) -> SpeechTurn:
    """Convert a database row into the chunker's input."""
    return SpeechTurn(
        turn_id=f"{row['debate_ext_id']}:{row['turn_no']}",
        debate_ext_id=row["debate_ext_id"],
        debate_title=(row["debate_title"] or "").strip(),
        sitting_date=row["sitting_date"],
        house=row["house"],
        location=row["location"],
        member_id=row["member_id"],
        speaker_name=row["speaker_name"],
        party=row["party"],
        constituency=row["constituency"],
        order_in_section=row["order_in_section"],
        word_count=row["word_count"],
        paragraphs=tuple(p for p in row["paragraphs"] if p and p.strip()),
        has_carried_text=bool(row["has_carried_text"]),
    )


def turn_id_of(row: DictRow) -> str:
    """The turn's identity, derivable from the row alone.

    Separate from :func:`turn_to_speech` so a resume can decide to skip a
    speech *before* paying to chunk it.
    """
    return f"{row['debate_ext_id']}:{row['turn_no']}"


def _stream_chunks(
    connection: Connection[DictRow],
    settings: Settings,
    *,
    min_words: int,
    skip_turn_ids: set[str] | None = None,
) -> Iterator[tuple[Chunk, bool]]:
    """Yield every chunk, flagged with whether it opens a new speech.

    The flag exists so the caller can count speeches without holding them: by
    the time a chunk is yielded its turn has already been discarded.

    ``skip_turn_ids`` is applied against the raw row, before the speech is
    assembled or split. Filtering after chunking would make a resume re-do the
    text work for every passage it is about to throw away -- which, at 27,000
    already-indexed speeches, is most of the job.
    """
    skip = skip_turn_ids or set()
    for row in speeches_store.stream_speech_turns(connection):
        if turn_id_of(row) in skip:
            continue
        turn = turn_to_speech(row)
        if turn.word_count < min_words:
            continue
        chunks = chunk_turn(
            turn,
            chunk_words=settings.chunk_words,
            overlap_words=settings.chunk_overlap_words,
        )
        for position, chunk in enumerate(chunks):
            yield chunk, position == 0


def build_index(
    connection: Connection[DictRow],
    settings: Settings,
    *,
    min_words: int = MIN_TURN_WORDS,
    resume: bool = False,
    limit: int | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> BuildReport:
    """Rebuild the vector index from every stored speech.

    ``resume`` continues a build that was interrupted, skipping speeches already
    present.

    ``limit`` stops after that many *new* speeches and exits cleanly. Together
    the two turn one long job into a sequence of short ones, which is what makes
    this survivable: embedding the corpus takes the better part of an hour, and
    on a laptop also running an IDE, a browser and Docker, a process holding
    1.5GB for that long gets killed by the memory manager. This one was, four
    times. A bounded run that exits and frees everything does not.
    """
    # Checked before any work: discovering a truncating model after a half-hour
    # build would mean doing it twice, and the symptom -- slightly worse
    # retrieval -- is not one anybody would notice.
    embeddings.check_model_fits_chunks(settings.embedding_model, settings.chunk_words)

    started = datetime.now(UTC)
    expected = count_indexable_turns(connection, min_words=min_words)
    log.info("index.starting", speeches=expected)

    dimensions = embeddings.dimensions(settings.embedding_model)
    if resume:
        table = index.open_or_create_table(settings.vector_path, dimensions)
        already = index.indexed_turn_ids(settings.vector_path)
        log.info("index.resuming", already_indexed=len(already))
    else:
        # Overwriting is the point of a rebuild, but it is destructive and was
        # once done by accident: a `--resume` flag that the CLI accepted and
        # then failed to pass through discarded 34,816 embedded passages in
        # silence. Say so, loudly, before doing it.
        discarding = index.indexed_turn_ids(settings.vector_path)
        if discarding:
            log.warning(
                "index.discarding_existing",
                speeches=len(discarding),
                hint="pass resume=True to keep them",
            )
        table = index.create_table(settings.vector_path, dimensions, overwrite=True)
        already = set()
    indexed_at = datetime.now(UTC).replace(microsecond=0)

    batch: list[Chunk] = []
    chunks = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        vectors = embeddings.embed_passages(
            [chunk.embed_text for chunk in batch],
            model_name=settings.embedding_model,
            parallel=settings.embed_parallel,
        )
        table.add(
            [
                index.to_row(chunk, vector, indexed_at)
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
        )
        # Dropped, not accumulated: this is what keeps peak memory bounded.
        batch = []

    turns = len(already)
    added = 0
    for chunk, opens_a_speech in _stream_chunks(
        connection, settings, min_words=min_words, skip_turn_ids=already
    ):
        # Checked on a speech boundary, never mid-speech: stopping between two
        # chunks of the same turn would leave it half-indexed, and the resume
        # skips by turn id so the missing half would never be filled in.
        if limit is not None and opens_a_speech and added >= limit:
            break
        batch.append(chunk)
        chunks += 1
        added += int(opens_a_speech)
        turns += int(opens_a_speech)
        if len(batch) >= EMBED_BATCH:
            flush()
            if on_progress is not None:
                on_progress("embedding", turns, expected)
    flush()

    if chunks == 0 and not already:
        raise RuntimeError("No speeches to index. Has `hansard ingest` been run?")

    elapsed = (datetime.now(UTC) - started).total_seconds()
    log.info("index.built", chunks=chunks, speeches=turns, seconds=round(elapsed, 1))

    return BuildReport(
        turns=turns,
        chunks=chunks,
        # What is left to do, not what was skipped for being short. A bounded
        # run stops early by design, and labelling the remainder "too short"
        # was simply untrue -- it read as 7,588 discarded speeches when they
        # were merely not reached yet.
        remaining=max(expected - turns, 0),
        dimensions=dimensions,
        seconds=elapsed,
    )


def count_indexable_turns(
    connection: Connection[DictRow], *, min_words: int = MIN_TURN_WORDS
) -> int:
    """How many speeches the index should end up holding.

    Counted in Postgres rather than by walking the rows, so progress can be
    reported without first loading the thing it is measuring.
    """
    row = connection.execute(
        f"SELECT COUNT(*) AS n FROM ({speeches_store.TURNS_SQL}) AS t "
        "WHERE t.word_count >= %(min_words)s",
        {"start_date": None, "end_date": None, "min_words": min_words},
    ).fetchone()
    return int(row["n"]) if row else 0


def expected_turn_ids(
    connection: Connection[DictRow], *, min_words: int = MIN_TURN_WORDS
) -> set[str]:
    """Turn ids the index should contain, for the staleness check."""
    return {
        f"{row['debate_ext_id']}:{row['turn_no']}"
        for row in speeches_store.stream_speech_turns(connection)
        if row["word_count"] >= min_words
    }
