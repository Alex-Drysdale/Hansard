"""Tests for chunking.

Chunking decides what a retrieval system can ever find, and its failures are
silent: a badly split passage still embeds, still retrieves, and just answers
slightly wrong. These pin the properties that matter.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from hansard.rag.chunking import (
    Chunk,
    SpeechTurn,
    build_embed_text,
    chunk_turn,
)


def turn(*paragraphs: str, speaker: str = "Wes Streeting", party: str = "Labour") -> SpeechTurn:
    text = " ".join(paragraphs)
    return SpeechTurn(
        turn_id="debate-1:3",
        debate_ext_id="debate-1",
        debate_title="NHS Waiting Lists",
        sitting_date=date(2026, 3, 3),
        house="Commons",
        location="Commons Chamber",
        member_id=4514,
        speaker_name=speaker,
        party=party,
        constituency="Ilford North",
        order_in_section=3,
        word_count=len(text.split()),
        paragraphs=paragraphs,
    )


def words(n: int, token: str = "word") -> str:
    return " ".join([token] * n)


def chunks_of(t: SpeechTurn, *, size: int = 100, overlap: int = 20) -> list[Chunk]:
    return chunk_turn(t, chunk_words=size, overlap_words=overlap)


class TestShortSpeeches:
    def test_a_short_speech_stays_whole(self) -> None:
        # Splitting a 40-word answer in half makes both halves worse.
        result = chunks_of(turn(words(40)))
        assert len(result) == 1
        assert result[0].chunk_index == 0

    def test_an_empty_turn_produces_nothing(self) -> None:
        assert chunks_of(turn("", "   ")) == []

    def test_metadata_travels_with_the_chunk(self) -> None:
        chunk = chunks_of(turn(words(30)))[0]
        assert chunk.speaker_name == "Wes Streeting"
        assert chunk.party == "Labour"
        assert chunk.member_id == 4514
        assert chunk.sitting_date == date(2026, 3, 3)
        assert chunk.debate_title == "NHS Waiting Lists"


class TestSplitting:
    def test_a_long_speech_is_split(self) -> None:
        result = chunks_of(turn(words(80), words(80), words(80)))
        assert len(result) > 1

    def test_chunks_are_numbered_in_order(self) -> None:
        result = chunks_of(turn(*[words(60) for _ in range(6)]))
        assert [c.chunk_index for c in result] == list(range(len(result)))

    def test_chunk_ids_are_unique_and_traceable(self) -> None:
        result = chunks_of(turn(*[words(60) for _ in range(6)]))
        assert len({c.chunk_id for c in result}) == len(result)
        assert all(c.chunk_id.startswith(c.turn_id) for c in result)

    def test_no_chunk_greatly_exceeds_the_limit(self) -> None:
        # The embedding model truncates silently, so an oversized chunk loses
        # its tail without any error.
        result = chunks_of(turn(*[words(45) for _ in range(10)]), size=100, overlap=20)
        assert all(c.word_count <= 150 for c in result), [c.word_count for c in result]

    def test_every_paragraph_survives_somewhere(self) -> None:
        paragraphs = [f"paragraph{i} " + words(50) for i in range(8)]
        joined = " ".join(c.text for c in chunks_of(turn(*paragraphs)))
        for i in range(8):
            assert f"paragraph{i}" in joined


class TestBoundaries:
    def test_chunks_break_between_paragraphs_not_inside_them(self) -> None:
        # A passage starting mid-sentence embeds badly and reads worse when it
        # is quoted back as evidence.
        paragraphs = [f"Sentence {i} begins here and continues. " + words(40) for i in range(6)]
        for chunk in chunks_of(turn(*paragraphs)):
            assert chunk.text.startswith("Sentence")

    def test_consecutive_chunks_overlap(self) -> None:
        # Overlap carries context: "he said that" is useless without the
        # sentence before it.
        result = chunks_of(turn(*[f"P{i} " + words(45) for i in range(8)]), size=100, overlap=50)
        assert len(result) > 1
        first_tail = set(result[0].text.split())
        second_head = set(result[1].text.split())
        assert first_tail & second_head

    def test_a_single_huge_paragraph_is_split_on_sentences(self) -> None:
        # One 3,000-word paragraph would otherwise become one oversized chunk.
        huge = ". ".join(words(30) for _ in range(20)) + "."
        result = chunks_of(turn(huge), size=100, overlap=20)
        assert len(result) > 1
        assert all(c.word_count <= 160 for c in result)

    def test_overlap_must_be_smaller_than_the_chunk(self) -> None:
        # Otherwise chunking never advances and loops for ever.
        with pytest.raises(ValueError, match="smaller"):
            chunk_turn(turn(words(500)), chunk_words=50, overlap_words=50)


class TestEmbedText:
    def test_the_speaker_is_in_the_embedded_text(self) -> None:
        # Without this, a passage about waiting lists is equally close to every
        # other passage about waiting lists, and "what did X say" has nothing
        # in the vector to grab.
        embed = build_embed_text(turn(words(20)), "some text")
        assert "Wes Streeting" in embed
        assert "Labour" in embed

    def test_the_debate_and_date_are_included(self) -> None:
        embed = build_embed_text(turn(words(20)), "some text")
        assert "NHS Waiting Lists" in embed
        assert "March 2026" in embed

    def test_the_body_text_is_preserved_verbatim(self) -> None:
        embed = build_embed_text(turn(words(20)), "the actual words spoken")
        assert embed.endswith("the actual words spoken")

    def test_an_unattributed_turn_still_embeds(self) -> None:
        # slots=True means no __dict__, so replace() is the way to vary a field.
        anonymous = replace(turn(words(20)), speaker_name=None, party=None)
        assert "Unattributed" in build_embed_text(anonymous, "text")

    def test_chunks_carry_their_embed_text(self) -> None:
        chunk = chunks_of(turn(words(30)))[0]
        assert chunk.embed_text
        assert chunk.text in chunk.embed_text
