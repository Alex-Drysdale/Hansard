"""Turning stored speeches into passages worth embedding.

Chunking is where most of the quality in a RAG system is won or lost, so it
gets its own module and its own tests.

Two decisions shape everything downstream:

**The unit is a speech turn, not a database row.** Hansard stores a speech as
several paragraph rows and names the speaker on the first only. Embedding those
rows individually would produce passages that begin mid-argument, with no
speaker attached -- the model would retrieve "Similarly, there are cases in
which young people..." with no idea who said it or what "similarly" refers to.
Because Phase 3's attribution pass resolved those rows, we can reassemble the
whole turn first and chunk that.

**Chunks break on paragraph boundaries, never mid-sentence.** A passage that
starts halfway through a clause embeds badly and reads worse when it is quoted
back as evidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

#: Sentence-ish split, used only when a single paragraph exceeds a whole chunk.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class SpeechTurn:
    """One continuous speech by one member, reassembled from its paragraphs."""

    turn_id: str
    debate_ext_id: str
    debate_title: str
    sitting_date: date
    house: str
    location: str | None
    member_id: int | None
    speaker_name: str | None
    party: str | None
    constituency: str | None
    order_in_section: int
    word_count: int
    paragraphs: tuple[str, ...]
    #: Whether any paragraph in this turn was attributed by inference rather
    #: than stated by Hansard. Carried through to the chunk so an answer can
    #: say so if it quotes one.
    has_carried_text: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


@dataclass(frozen=True, slots=True)
class Chunk:
    """An embeddable passage, with everything needed to cite it."""

    chunk_id: str
    turn_id: str
    debate_ext_id: str
    debate_title: str
    sitting_date: date
    house: str
    location: str | None
    member_id: int | None
    speaker_name: str | None
    party: str | None
    constituency: str | None
    chunk_index: int
    word_count: int
    text: str
    has_carried_text: bool = False

    @property
    def embed_text(self) -> str:
        """What actually gets embedded: the passage plus a short header.

        A property rather than a stored field. Storing it kept a second copy of
        every passage in memory -- doubling the cost of a full index build for
        a string that is cheap to rebuild and used exactly once.
        """
        speaker = self.speaker_name or "Unattributed"
        party = f" ({self.party})" if self.party else ""
        header = (
            f"{speaker}{party} speaking in {self.debate_title} on {self.sitting_date:%d %B %Y}:"
        )
        return f"{header}\n{self.text}"


def _words(text: str) -> list[str]:
    return text.split()


def _split_long_paragraph(paragraph: str, limit: int) -> list[str]:
    """Break a paragraph longer than a whole chunk on sentence boundaries.

    Rare -- most Hansard paragraphs are well under the limit -- but a single
    3,000-word paragraph would otherwise become one oversized chunk that the
    embedding model silently truncates, losing the end of it entirely.
    """
    sentences = _SENTENCE_END.split(paragraph)
    pieces: list[str] = []
    current: list[str] = []
    count = 0

    for sentence in sentences:
        length = len(_words(sentence))
        if current and count + length > limit:
            pieces.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += length

    if current:
        pieces.append(" ".join(current))
    return pieces or [paragraph]


def chunk_turn(turn: SpeechTurn, *, chunk_words: int, overlap_words: int) -> list[Chunk]:
    """Split one speech into passages.

    Short speeches stay whole: splitting a 200-word answer into two 100-word
    halves makes both halves worse and gains nothing.
    """
    if overlap_words >= chunk_words:
        raise ValueError("overlap_words must be smaller than chunk_words")

    units: list[str] = []
    for paragraph in turn.paragraphs:
        cleaned = paragraph.strip()
        if not cleaned:
            continue
        if len(_words(cleaned)) > chunk_words:
            units.extend(_split_long_paragraph(cleaned, chunk_words))
        else:
            units.append(cleaned)

    if not units:
        return []

    chunks: list[list[str]] = []
    current: list[str] = []
    count = 0

    for unit in units:
        length = len(_words(unit))
        if current and count + length > chunk_words:
            chunks.append(current)
            # Overlap by whole paragraphs from the end of the previous chunk, so
            # a chunk never begins mid-sentence. Carrying context matters most
            # for pronouns: "he said that" is useless without the sentence
            # before it.
            carried: list[str] = []
            carried_words = 0
            for previous in reversed(current):
                previous_length = len(_words(previous))
                if carried_words + previous_length > overlap_words:
                    break
                carried.insert(0, previous)
                carried_words += previous_length
            current = carried
            count = carried_words
        current.append(unit)
        count += length

    if current:
        chunks.append(current)

    return [_build_chunk(turn, index, "\n\n".join(parts)) for index, parts in enumerate(chunks)]


def _build_chunk(turn: SpeechTurn, index: int, text: str) -> Chunk:
    return Chunk(
        chunk_id=f"{turn.turn_id}#{index}",
        turn_id=turn.turn_id,
        debate_ext_id=turn.debate_ext_id,
        debate_title=turn.debate_title,
        sitting_date=turn.sitting_date,
        house=turn.house,
        location=turn.location,
        member_id=turn.member_id,
        speaker_name=turn.speaker_name,
        party=turn.party,
        constituency=turn.constituency,
        chunk_index=index,
        word_count=len(_words(text)),
        text=text,
        has_carried_text=turn.has_carried_text,
    )


def build_embed_text(turn: SpeechTurn, text: str) -> str:
    """The string actually sent to the embedding model.

    Prefixing the speaker, debate and date is a cheap and large win. Without it,
    a passage about hospital waiting times is equally close to every other
    passage about hospital waiting times, and the question "what did *this
    member* say" has nothing in the vector to grab hold of. It does not replace
    a metadata filter -- see :mod:`hansard.rag.retrieve` -- but it stops the
    speaker being invisible to the model.
    """
    speaker = turn.speaker_name or "Unattributed"
    party = f" ({turn.party})" if turn.party else ""
    return (
        f"{speaker}{party} speaking in {turn.debate_title} on {turn.sitting_date:%d %B %Y}:\n{text}"
    )


def chunk_turns(
    turns: Sequence[SpeechTurn], *, chunk_words: int, overlap_words: int
) -> Iterator[Chunk]:
    for turn in turns:
        yield from chunk_turn(turn, chunk_words=chunk_words, overlap_words=overlap_words)
