"""Tests for the transform layer.

These are the cheapest tests in the project -- pure functions, no database, no
network -- and they cover the decisions most likely to be wrong: how a speaker
string is read, and what counts as speech.
"""

from __future__ import annotations

from datetime import date

import pytest

from hansard.api.models import DebateDetail
from hansard.pipeline.normalise import (
    Attribution,
    content_hash,
    count_words,
    looks_like_party,
    normalise_debate,
    normalise_name,
    parse_attribution,
    speaker_key,
    strip_html,
)


class TestStripHtml:
    def test_extracts_visible_text(self) -> None:
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_block_tags_do_not_weld_words_together(self) -> None:
        assert strip_html("<p>first</p><p>second</p>") == "first second"

    def test_decodes_entities(self) -> None:
        assert strip_html("<p>Bath &amp; Wells &#8212; here</p>") == "Bath & Wells — here"

    def test_collapses_whitespace(self) -> None:
        assert strip_html("<p>a  \r\n  b</p>") == "a b"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_input_gives_empty_string(self, value: str | None) -> None:
        assert strip_html(value) == ""

    def test_column_marker_has_no_visible_text(self) -> None:
        # Hansard emits these as ItemType "Contribution"; they are page furniture.
        marker = '<span id="1048" class="column-number" data-column-number="1048"></span>'
        assert strip_html(marker) == ""


class TestParseAttribution:
    def test_backbencher_yields_all_three_parts(self) -> None:
        assert parse_attribution("Martin Vickers (Brigg and Immingham) (Con)") == Attribution(
            name="Martin Vickers", constituency="Brigg and Immingham", party="Con"
        )

    def test_office_holder_names_the_person_not_the_job(self) -> None:
        # The regression this guards: reading the single bracket as a party
        # would make "Sarah Jones" a party and lose the speaker entirely.
        assert parse_attribution("The Minister for Policing and Crime (Sarah Jones)") == (
            Attribution(name="Sarah Jones", role="The Minister for Policing and Crime")
        )

    def test_presiding_officer_is_treated_as_an_office_holder(self) -> None:
        assert parse_attribution("Madam Deputy Speaker (Ms Nusrat Ghani)") == Attribution(
            name="Ms Nusrat Ghani", role="Madam Deputy Speaker"
        )

    def test_lords_single_bracket_is_read_as_a_party(self) -> None:
        assert parse_attribution("Lord Callanan (Con)") == Attribution(
            name="Lord Callanan", party="Con"
        )

    def test_bare_name_has_no_extras(self) -> None:
        assert parse_attribution("Mr Speaker") == Attribution(name="Mr Speaker")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_input_gives_none(self, value: str | None) -> None:
        assert parse_attribution(value) is None

    def test_party_with_slash_survives(self) -> None:
        parsed = parse_attribution("Stella Creasy (Walthamstow) (Lab/Co-op)")
        assert parsed is not None and parsed.party == "Lab/Co-op"

    def test_constituency_containing_spaces_is_kept_whole(self) -> None:
        parsed = parse_attribution("Sir Edward Leigh (Gainsborough and Cleethorpes) (Con)")
        assert parsed is not None and parsed.constituency == "Gainsborough and Cleethorpes"


class TestLooksLikeParty:
    @pytest.mark.parametrize("value", ["Con", "Lab", "LD", "Lab/Co-op", "SNP", "Reform"])
    def test_known_abbreviations(self, value: str) -> None:
        assert looks_like_party(value)

    @pytest.mark.parametrize(
        "value", ["Ed Miliband", "Ms Nusrat Ghani", "Martin McCluskey", "Al Carns"]
    )
    def test_personal_names_are_not_parties(self, value: str) -> None:
        assert not looks_like_party(value)

    def test_unknown_compact_token_is_assumed_to_be_a_party(self) -> None:
        # So a party abbreviation invented after this code was written still
        # parses as a party rather than becoming somebody's name.
        assert looks_like_party("NewParty")


class TestNormaliseName:
    def test_strips_honorifics(self) -> None:
        assert normalise_name("Rt Hon Sir Keir Starmer") == "keir starmer"

    def test_strips_punctuation_and_case(self) -> None:
        assert normalise_name("Mr. Speaker") == "speaker"

    def test_strips_accents(self) -> None:
        assert normalise_name("Seán Fearon") == "sean fearon"

    def test_empty_input(self) -> None:
        assert normalise_name(None) == ""


class TestSpeakerKey:
    def test_member_id_wins_when_present(self) -> None:
        assert speaker_key(3957, "Martin Vickers") == "member:3957"

    def test_falls_back_to_normalised_name(self) -> None:
        assert speaker_key(None, "Mr Speaker") == "name:speaker"

    def test_same_person_two_spellings_shares_a_key(self) -> None:
        assert speaker_key(None, "Mr Speaker") == speaker_key(None, "Speaker")

    def test_nothing_to_key_on(self) -> None:
        assert speaker_key(None, None) is None

    def test_key_records_which_rule_was_used(self) -> None:
        # A caller can tell a confident identity from a best-effort one.
        assert speaker_key(1, "x").startswith("member:")
        assert speaker_key(None, "x").startswith("name:")


class TestContentHash:
    def test_is_stable_across_calls(self) -> None:
        assert content_hash("a", 1, None) == content_hash("a", 1, None)

    def test_changes_when_any_part_changes(self) -> None:
        assert content_hash("a", "b") != content_hash("a", "c")

    def test_field_boundaries_cannot_be_forged(self) -> None:
        # Naive concatenation would make these two collide.
        assert content_hash("ab", "c") != content_hash("a", "bc")

    def test_none_and_empty_string_are_distinguishable_by_position(self) -> None:
        assert content_hash(None, "x") != content_hash("x", None)


def test_count_words() -> None:
    assert count_words("one two three") == 3
    assert count_words("") == 0


class TestNormaliseDebate:
    def test_reads_the_overview(self, debate_with_speeches: DebateDetail) -> None:
        row = normalise_debate(debate_with_speeches).debate
        # Canonicalised to lowercase: the same section arrives uppercase from
        # one endpoint and lowercase from another, so raw text would not join.
        assert row.ext_id == "69ffb3cb-33ef-41dd-94b0-e8685beb39ef"
        assert row.title == "Oil Refining Sector"
        assert row.house == "Commons"
        assert row.sitting_date == date(2026, 1, 14)
        assert row.location == "Commons Chamber"

    def test_titles_are_stripped(self, debate_child: DebateDetail) -> None:
        # Hansard routinely prefixes titles with a space.
        assert not normalise_debate(debate_child).debate.title.startswith(" ")

    def test_parent_comes_from_the_navigator_trail(self, debate_child: DebateDetail) -> None:
        row = normalise_debate(debate_child).debate
        assert row.parent_ext_id == "565db7b1-4cbd-4bd7-86f2-f89dac86a758"
        assert row.parent_title == "Petition"

    def test_top_level_debate_points_at_the_day_root(
        self, debate_with_speeches: DebateDetail
    ) -> None:
        assert normalise_debate(debate_with_speeches).debate.parent_title == "Commons Chamber"

    def test_child_debates_are_not_expanded(self, debate_with_child: DebateDetail) -> None:
        # The rule that keeps ingest from writing every child section twice:
        # children arrive inline here AND as their own search hits.
        assert debate_with_child.child_debates, "fixture should contain a child"
        result = normalise_debate(debate_with_child)
        assert all(row.debate_ext_id == result.debate.ext_id for row in result.contributions)

    def test_column_markers_are_not_speech(self, debate_with_speeches: DebateDetail) -> None:
        markers = [
            row
            for row in normalise_debate(debate_with_speeches).contributions
            if row.hrs_tag == "hs_ColumnNumber"
        ]
        assert markers, "fixture should contain a column marker"
        assert not any(row.is_speech for row in markers)

    def test_timestamps_are_not_speech(self, debate_with_speeches: DebateDetail) -> None:
        stamps = [
            row
            for row in normalise_debate(debate_with_speeches).contributions
            if row.item_type == "Timestamp"
        ]
        assert stamps, "fixture should contain a timestamp"
        assert not any(row.is_speech for row in stamps)

    def test_speeches_carry_text_and_a_word_count(self, debate_with_speeches: DebateDetail) -> None:
        speeches = [
            row for row in normalise_debate(debate_with_speeches).contributions if row.is_speech
        ]
        assert speeches
        assert all(row.body_text and row.word_count > 0 for row in speeches)
        assert all("<" not in row.body_text for row in speeches)

    def test_debate_counts_only_speeches(self, debate_with_speeches: DebateDetail) -> None:
        result = normalise_debate(debate_with_speeches)
        expected = sum(1 for row in result.contributions if row.is_speech)
        assert result.debate.contribution_count == expected
        assert result.debate.contribution_count < len(result.contributions)

    def test_debate_hash_covers_its_contributions(self, debate_with_speeches: DebateDetail) -> None:
        # This is what makes "has this debate changed?" a single column compare.
        original = normalise_debate(debate_with_speeches).debate.content_hash
        edited = debate_with_speeches.model_copy(
            update={
                "items": [
                    debate_with_speeches.items[0].model_copy(update={"value": "<p>changed</p>"}),
                    *debate_with_speeches.items[1:],
                ]
            }
        )
        assert normalise_debate(edited).debate.content_hash != original

    def test_normalisation_is_deterministic(self, debate_with_speeches: DebateDetail) -> None:
        first = normalise_debate(debate_with_speeches)
        second = normalise_debate(debate_with_speeches)
        assert first.debate == second.debate
        assert first.contributions == second.contributions


class TestDepth:
    """Depth comes from the Navigator trail and is what makes the orphan-parent
    check meaningful: it separates "parent is the unstored day root" from
    "parent is genuinely missing"."""

    def test_top_level_section_is_depth_two(self, debate_with_speeches: DebateDetail) -> None:
        assert normalise_debate(debate_with_speeches).debate.depth == 2

    def test_nested_section_is_deeper(self, debate_child: DebateDetail) -> None:
        assert normalise_debate(debate_child).debate.depth == 3

    def test_depth_is_never_below_one(self, debate_with_speeches: DebateDetail) -> None:
        stripped = debate_with_speeches.model_copy(update={"navigator": []})
        assert normalise_debate(stripped).debate.depth == 1
