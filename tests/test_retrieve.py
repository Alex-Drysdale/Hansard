"""Tests for retrieval.

The interesting behaviour here is not the vector search -- that is a library
call -- but everything around it: turning a question into filters, and fusing
two ranked lists. Both are where naive RAG goes wrong, and both are testable
without embedding anything.
"""

from __future__ import annotations

from datetime import date

import pytest
from psycopg import Connection
from psycopg.rows import DictRow

from hansard.rag.answer import normalise_citations
from hansard.rag.retrieve import (
    Passage,
    QueryFilters,
    fuse,
    parse_filters,
    topic_query,
)

LATEST = date(2026, 4, 29)


@pytest.fixture
def members(clean_connection: Connection[DictRow]) -> Connection[DictRow]:
    """A handful of real-shaped members to resolve names against."""
    clean_connection.execute(
        """
        INSERT INTO member (member_id, display_name, party, constituency, synced_at)
        VALUES (4514, 'Wes Streeting', 'Labour', 'Ilford North', now()),
               (4860, 'Dr Kieran Mullan', 'Conservative', 'Bexhill and Battle', now()),
               (172,  'Ms Diane Abbott', 'Labour', 'Hackney North', now())
        """
    )
    return clean_connection


class TestNameFilter:
    def test_a_member_name_becomes_a_hard_filter(self, members: Connection[DictRow]) -> None:
        # The whole point: "what did X say about Y" is not a similarity
        # question. Without this filter, vector search returns passages about Y
        # by everyone, and the model answers as though they were X's.
        filters = parse_filters(members, "What did Wes Streeting say about cancer?")
        assert filters.member_id == 4514
        assert filters.member_name == "Wes Streeting"
        assert any("Wes Streeting" in note for note in filters.notes)

    def test_a_surname_alone_resolves(self, members: Connection[DictRow]) -> None:
        filters = parse_filters(members, "What has Kieran Mullan said about jury trials?")
        assert filters.member_id == 4860

    def test_an_unknown_name_is_left_alone(self, members: Connection[DictRow]) -> None:
        # A wrong filter is worse than no filter: it removes the passages that
        # would have answered the question and leaves the model to invent.
        filters = parse_filters(members, "What did Marcus Fictional say about trains?")
        assert filters.member_id is None

    @pytest.mark.parametrize(
        "question",
        [
            "What did the Minister say about housing?",
            "What has the Prime Minister said?",
            "What did the Secretary of State announce?",
            "What has the Government said about Reform?",
        ],
    )
    def test_titles_are_not_mistaken_for_names(
        self, members: Connection[DictRow], question: str
    ) -> None:
        assert parse_filters(members, question).member_id is None

    def test_no_name_means_no_member_filter(self, members: Connection[DictRow]) -> None:
        filters = parse_filters(members, "What has been said about hospital waiting times?")
        assert filters.member_id is None


class TestDateFilter:
    def test_last_month_becomes_a_range(self, members: Connection[DictRow]) -> None:
        # "Last month" is a range, not a direction in vector space. Nothing in
        # an embedding encodes recency.
        filters = parse_filters(
            members, "What has been said about cancer in the last month?", latest_data_date=LATEST
        )
        assert filters.end_date == LATEST
        assert filters.start_date == date(2026, 3, 29)

    def test_relative_dates_anchor_to_the_data_not_to_today(
        self, members: Connection[DictRow]
    ) -> None:
        # Anchoring to the real clock would return nothing at all, and an empty
        # result reads exactly like an empty database.
        filters = parse_filters(members, "anything about trains recently?", latest_data_date=LATEST)
        assert filters.end_date == LATEST
        assert filters.start_date is not None
        assert filters.start_date < LATEST

    def test_last_week_is_narrower_than_last_month(self, members: Connection[DictRow]) -> None:
        week = parse_filters(members, "housing last week", latest_data_date=LATEST)
        month = parse_filters(members, "housing last month", latest_data_date=LATEST)
        assert week.start_date is not None and month.start_date is not None
        assert week.start_date > month.start_date

    def test_an_explicit_year_restricts_to_it(self, members: Connection[DictRow]) -> None:
        filters = parse_filters(members, "What was said about the Budget in 2026?")
        assert filters.start_date == date(2026, 1, 1)
        assert filters.end_date == date(2026, 12, 31)

    def test_no_date_language_means_no_date_filter(self, members: Connection[DictRow]) -> None:
        filters = parse_filters(members, "What has been said about cancer?")
        assert filters.start_date is None
        assert filters.end_date is None

    def test_a_name_and_a_period_combine(self, members: Connection[DictRow]) -> None:
        filters = parse_filters(
            members,
            "What did Wes Streeting say about cancer last month?",
            latest_data_date=LATEST,
        )
        assert filters.member_id == 4514
        assert filters.start_date is not None


def passage(
    text: str, *, speaker: str = "A", day: int = 1, score: float = 1.0, source: str = "vector"
) -> Passage:
    return Passage(
        text=text,
        speaker_name=speaker,
        party="Labour",
        debate_title="A debate",
        sitting_date=date(2026, 3, day),
        debate_ext_id=f"debate-{day}",
        member_id=1,
        score=score,
        sources=(source,),
    )


class TestFusion:
    def test_a_passage_found_by_both_methods_outranks_one_found_by_either(self) -> None:
        # The point of fusing: agreement between two methods that fail in
        # different directions is stronger evidence than either alone.
        shared = passage("shared text", day=1)
        result = fuse(
            [shared, passage("vector only", day=2)],
            [passage("shared text", day=1, source="keyword"), passage("kw only", day=3)],
            limit=5,
        )
        assert result[0].text == "shared text"
        assert set(result[0].sources) == {"vector", "keyword"}

    def test_scores_from_different_scales_are_not_added(self) -> None:
        # A cosine distance and a ts_rank are not comparable numbers. Fusing on
        # rank rather than score is what makes this safe.
        huge = passage("huge score", day=1, score=9999.0)
        result = fuse([passage("first", day=2, score=0.1), huge], [], limit=5)
        assert result[0].text == "first"

    def test_the_limit_is_respected(self) -> None:
        hits = [passage(f"text {i}", day=i + 1) for i in range(10)]
        assert len(fuse(hits, [], limit=3)) == 3

    def test_either_list_may_be_empty(self) -> None:
        assert len(fuse([passage("only vector")], [], limit=5)) == 1
        assert len(fuse([], [passage("only keyword", source="keyword")], limit=5)) == 1
        assert fuse([], [], limit=5) == []

    def test_sources_are_reported_so_you_can_see_why_it_was_found(self) -> None:
        result = fuse([passage("v")], [passage("k", source="keyword")], limit=5)
        assert {tuple(p.sources) for p in result} == {("vector",), ("keyword",)}


class TestCitations:
    def test_a_citation_names_speaker_party_debate_and_date(self) -> None:
        text = passage("x", speaker="Wes Streeting").citation()
        assert "Wes Streeting" in text
        assert "Labour" in text
        assert "A debate" in text
        assert "2026" in text

    def test_an_unattributed_passage_still_cites(self) -> None:
        anonymous = Passage(
            text="x",
            speaker_name=None,
            party=None,
            debate_title="D",
            sitting_date=date(2026, 3, 1),
            debate_ext_id="d",
            member_id=None,
            score=1.0,
            sources=("vector",),
        )
        assert "Unattributed" in anonymous.citation()


class TestNaiveMode:
    def test_naive_filters_are_empty_and_say_so(self) -> None:
        # Naive mode exists to be shown failing, so it should announce itself.
        empty = QueryFilters(notes=("naive mode: no filters, vector search only",))
        assert empty.member_id is None
        assert empty.start_date is None
        assert "naive" in empty.notes[0]


class TestTopicQuery:
    """The keyword half of hybrid retrieval searches topic terms, not the
    question. Postgres ANDs tsquery terms, so passing the whole question makes
    the keyword search require words a speech will never contain."""

    def test_the_speaker_name_is_removed(self) -> None:
        # 'wes' & 'street' & 'say' & 'cancer' matched nothing, because a speech
        # BY Streeting does not contain the word "Streeting". The name is
        # already a metadata filter; it has no business in the text query too.
        assert (
            topic_query(
                "What did Wes Streeting say about cancer?",
                QueryFilters(member_name="Wes Streeting"),
            )
            == "cancer"
        )

    def test_question_framing_is_removed(self) -> None:
        assert topic_query("What has been said about jury trials?", QueryFilters()) == (
            "jury trials"
        )

    def test_content_words_survive(self) -> None:
        # The stopword list must never contain a word a speech might use.
        assert topic_query("Prax Lindsey oil refinery", QueryFilters()) == (
            "prax lindsey oil refinery"
        )

    def test_cost_of_living_is_not_gutted(self) -> None:
        # "cost" and "living" are content; only the framing goes.
        assert (
            topic_query("What has been said about the cost of living?", QueryFilters())
            == "cost living"
        )

    def test_period_words_go_only_when_a_date_filter_applied(self) -> None:
        with_filter = topic_query("spending last year", QueryFilters(start_date=date(2026, 1, 1)))
        without_filter = topic_query("spending last year", QueryFilters())
        assert with_filter == "spending"
        assert "year" in without_filter

    def test_a_question_of_pure_framing_falls_back(self) -> None:
        # An empty tsquery would throw away the keyword half entirely; better
        # to search the raw question and let the member filter do the work.
        question = "What did Wes Streeting say?"
        assert topic_query(question, QueryFilters(member_name="Wes Streeting")) == question

    def test_punctuation_is_stripped(self) -> None:
        assert topic_query("cancer, waiting-times?", QueryFilters()) == "cancer waiting-times"


class TestCitationsAreNormalised:
    """The model is asked for [3] and does not reliably give it.

    gpt-oss-120b returns OpenAI-style file markers instead. Rewriting them is
    more reliable than prompting harder, and the answer has to be readable
    whichever model sits behind LLM_BASE_URL.
    """

    def test_fancy_markers_become_plain_ones(self):
        text = (
            "he warned"
            + "【"
            + "1"
            + "†"
            + "L1-L5"
            + "】"
            + " and added"
            + "【"
            + "4"
            + "†"
            + "L2"
            + "】"
        )
        assert normalise_citations(text) == "he warned[1] and added[4]"

    def test_plain_citations_are_left_alone(self):
        assert normalise_citations("as he said [3] and [7]") == "as he said [3] and [7]"

    def test_ordinary_numbers_are_not_touched(self):
        # The obvious wrong fix -- rewriting bare numbers -- would mangle the
        # figures the answers are made of.
        assert normalise_citations("213,000 diagnoses and 9.5 million") == (
            "213,000 diagnoses and 9.5 million"
        )
