"""Tests for carrying a speaker across paragraph boundaries.

Hansard names the speaker on the first paragraph of a speech only, so a long
speech arrives as one named paragraph followed by several anonymous ones. These
tests pin the rules that decide which anonymous rows belong to the speech and
which are the procedural furniture between speeches.

The failure mode this guards against is quiet: a wrong rule does not crash, it
just puts words in the wrong person's mouth, and nothing downstream notices.
"""

from __future__ import annotations

import pytest

from hansard.pipeline.normalise import (
    AttributionSource,
    ContributionRow,
    apply_resolved_speakers,
    resolve_speakers,
)

DEBATE = "test-debate"


def row(
    order: int,
    text: str,
    *,
    member_id: int | None = None,
    attributed_to: str | None = None,
    hrs_tag: str = "hs_Para",
    item_type: str = "Contribution",
    is_speech: bool = True,
) -> ContributionRow:
    return ContributionRow(
        item_id=1000 + order,
        debate_ext_id=DEBATE,
        external_id=None,
        order_in_section=order,
        item_type=item_type,
        hrs_tag=hrs_tag,
        is_speech=is_speech,
        member_id=member_id,
        attributed_to=attributed_to,
        speaker_name=None,
        speaker_key=None,
        speaker_role=None,
        party=None,
        constituency=None,
        body_html=None,
        body_text=text,
        word_count=len(text.split()),
        timecode=None,
        is_reiteration=False,
        content_hash="h",
    )


LONG = " ".join(["word"] * 40)


def sources(rows: list[ContributionRow]) -> list[tuple[int | None, str]]:
    resolved = resolve_speakers(rows)
    return [
        (resolved[r.item_id].member_id, resolved[r.item_id].source.value)
        for r in sorted(rows, key=lambda r: r.order_in_section)
    ]


class TestCarryForward:
    def test_a_named_paragraph_is_stated(self) -> None:
        assert sources([row(1, LONG, member_id=42)]) == [(42, "stated")]

    def test_an_anonymous_prose_paragraph_continues_the_speech(self) -> None:
        rows = [row(1, LONG, member_id=42), row(2, LONG)]
        assert sources(rows) == [(42, "stated"), (42, "carried")]

    def test_a_whole_run_of_paragraphs_is_carried(self) -> None:
        # The real shape: one named opening, then several anonymous paragraphs.
        rows = [row(1, LONG, member_id=42), *[row(n, LONG) for n in range(2, 8)]]
        assert [s for _, s in sources(rows)] == ["stated"] + ["carried"] * 6

    def test_a_new_speaker_takes_over(self) -> None:
        rows = [row(1, LONG, member_id=42), row(2, LONG), row(3, LONG, member_id=99), row(4, LONG)]
        assert sources(rows) == [(42, "stated"), (42, "carried"), (99, "stated"), (99, "carried")]

    def test_nothing_is_carried_before_the_first_named_speaker(self) -> None:
        # Committee front matter comes before anyone has spoken.
        rows = [row(1, LONG), row(2, LONG, member_id=42)]
        assert sources(rows) == [(None, "unattributed"), (42, "stated")]

    def test_timestamps_do_not_break_a_speech(self) -> None:
        # A timecode sits inside a speech; it must not end it.
        rows = [
            row(1, LONG, member_id=42),
            row(2, "15:45:00", item_type="Timestamp", is_speech=False),
            row(3, LONG),
        ]
        assert sources(rows) == [(42, "stated"), (None, "unattributed"), (42, "carried")]

    def test_an_empty_column_marker_does_not_break_a_speech(self) -> None:
        rows = [
            row(1, LONG, member_id=42),
            row(2, "", hrs_tag="hs_ColumnNumber", is_speech=False),
            row(3, LONG),
        ]
        assert sources(rows)[2] == (42, "carried")


class TestWhatIsNotCarried:
    def test_a_speaker_hansard_named_but_we_cannot_map_ends_the_speech(self) -> None:
        # "The Chair" is a real speaker with no member id. Carrying through it
        # attributed 298 Chair interventions to whichever member spoke before.
        rows = [
            row(1, LONG, member_id=42),
            row(
                2,
                "I remind the Minister that this is not part of the amendment. " + LONG,
                attributed_to="The Chair",
            ),
            row(3, LONG),
        ]
        assert sources(rows) == [
            (42, "stated"),
            (None, "unattributed"),
            (None, "unattributed"),
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "Question put and agreed to. " + LONG,
            "Motion made, and Question proposed, " + LONG,
            "Ordered, That the Bill be now read a second time. " + LONG,
            "Resolved, That this House approves " + LONG,
            "Committee rose at four o'clock. " + LONG,
            "Sitting suspended for a Division. " + LONG,
            "Amendment proposed 23, in clause 3, page 4, " + LONG,
        ],
    )
    def test_procedural_formulae_end_the_speech(self, text: str) -> None:
        rows = [row(1, LONG, member_id=42), row(2, text)]
        assert sources(rows)[1] == (None, "unattributed")

    def test_a_chair_calling_order_is_not_the_previous_speaker(self) -> None:
        # Genuine speech, but somebody else's. Leaving it unattributed is right;
        # carrying it forward would be actively wrong.
        rows = [row(1, LONG, member_id=42), row(2, "Order. " + LONG)]
        assert sources(rows)[1] == (None, "unattributed")

    def test_short_paragraphs_are_not_carried(self) -> None:
        # "Brought up, and read the First time." and written-evidence references
        # like "RB 26 Transport UK (TUK)" sit between speeches.
        rows = [row(1, LONG, member_id=42), row(2, "Brought up, and read the First time.")]
        assert sources(rows)[1] == (None, "unattributed")

    def test_a_division_ends_the_speech(self) -> None:
        rows = [
            row(1, LONG, member_id=42),
            row(2, "", item_type="Division", is_speech=False),
            row(3, LONG),
        ]
        assert sources(rows)[2] == (None, "unattributed")

    def test_procedural_text_cannot_bridge_two_speeches(self) -> None:
        # Without clearing the held speaker, the second speaker's anonymous
        # continuation would be credited to the first.
        rows = [
            row(1, LONG, member_id=42),
            row(2, "Question put and agreed to."),
            row(3, LONG),
        ]
        assert sources(rows)[2] == (None, "unattributed")

    def test_amendment_text_is_not_speech(self) -> None:
        rows = [row(1, LONG, member_id=42), row(2, LONG, hrs_tag="hs_AmendmentLevel1")]
        assert sources(rows)[1] == (None, "unattributed")

    def test_a_clause_heading_is_not_speech(self) -> None:
        rows = [row(1, LONG, member_id=42), row(2, "Clause 80", hrs_tag="hs_8Clause")]
        assert sources(rows)[1] == (None, "unattributed")

    def test_the_attendance_list_is_not_speech(self) -> None:
        rows = [
            row(1, LONG, member_id=42),
            row(
                2,
                "† Stringer, Graham (Blackley and Middleton South) (Lab)",
                hrs_tag="hs_CLMember",
            ),
        ]
        assert sources(rows)[1] == (None, "unattributed")


class TestQuotations:
    def test_a_quotation_read_aloud_continues_the_speech(self) -> None:
        # hs_brev is used for both quotations inside a speech and procedural
        # motions; the opening quote mark is what separates them.
        rows = [
            row(1, LONG, member_id=42),
            row(2, "“Policies fine for 2012 are not fine now.”", hrs_tag="hs_brev"),
        ]
        assert sources(rows)[1] == (42, "carried")

    def test_a_procedural_motion_in_the_same_tag_does_not(self) -> None:
        rows = [
            row(1, LONG, member_id=42),
            row(
                2,
                "That the following provisions shall apply to the Bill: " + LONG,
                hrs_tag="hs_brev",
            ),
        ]
        assert sources(rows)[1] == (None, "unattributed")

    def test_a_short_quotation_is_still_carried(self) -> None:
        # Length is not a useful signal for a quoted line.
        rows = [row(1, LONG, member_id=42), row(2, "“It was a team effort.”", hrs_tag="hs_brev")]
        assert sources(rows)[1] == (42, "carried")


class TestFalsePositivesThatCostUsAWholeSpeech:
    def test_a_paragraph_merely_mentioning_an_amendment_is_still_speech(self) -> None:
        # The regression that lost a shadow minister's entire speech: a
        # "lords amendment" prefix rule matched "Lords amendment 7, on court
        # transcripts of sentencing remarks, was passed..." -- prose, not
        # procedure -- and clearing the speaker orphaned everything after it.
        rows = [
            row(1, LONG, member_id=42),
            row(2, "Lords amendment 7, on court transcripts of sentencing remarks, " + LONG),
            row(3, LONG),
        ]
        assert sources(rows) == [(42, "stated"), (42, "carried"), (42, "carried")]

    def test_a_paragraph_beginning_with_clause_prose_is_still_speech(self) -> None:
        rows = [
            row(1, LONG, member_id=42),
            row(
                2,
                "Clauses 12 and 13 together create a duty that the Government have not costed. "
                + LONG,
            ),
        ]
        assert sources(rows)[1] == (42, "carried")


class TestApplyResolvedSpeakers:
    def test_rows_come_back_with_the_resolution_attached(self) -> None:
        rows = [row(1, LONG, member_id=42), row(2, LONG)]
        applied = apply_resolved_speakers(rows)
        assert [r.speaker_member_id for r in applied] == [42, 42]
        assert [r.attribution for r in applied] == ["stated", "carried"]

    def test_the_original_member_id_is_left_alone(self) -> None:
        # member_id stays exactly what Hansard said; the inference lives beside
        # it so an analysis can choose to ignore inferred rows.
        applied = apply_resolved_speakers([row(1, LONG, member_id=42), row(2, LONG)])
        assert applied[1].member_id is None
        assert applied[1].speaker_member_id == 42
        assert applied[1].attribution == AttributionSource.CARRIED.value

    def test_resolution_is_order_independent(self) -> None:
        # Rows arrive in payload order, which is usually but not always sorted.
        rows = [row(3, LONG), row(1, LONG, member_id=42), row(2, LONG)]
        applied = {r.order_in_section: r for r in apply_resolved_speakers(rows)}
        assert applied[2].speaker_member_id == 42
        assert applied[3].speaker_member_id == 42
