"""Turn Hansard wire models into the rows we actually store.

This is the only module that knows both shapes, so every messy fact about the
upstream data -- HTML in the speech body, party and constituency smuggled into
a display string, "Contribution" rows that are really column markers -- is
handled here once and never leaks into the store or the CLI.

Everything here is a pure function of its inputs, which is why it is the
easiest part of the pipeline to test.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from enum import StrEnum
from html.parser import HTMLParser
from uuid import UUID

from hansard.api.members_models import MemberValue
from hansard.api.models import DebateDetail, DebateItem, DivisionDetail

# HRSTag values that arrive as ItemType "Contribution" but carry no speech:
# page furniture that would otherwise pollute word counts and search results.
NON_SPEECH_TAGS = frozenset(
    {
        "hs_ColumnNumber",
        "hs_Timeline",
        "hs_TimeCode",
    }
)

# Hansard packs up to three facts into one display string, in three shapes:
#
#   "Martin Vickers (Brigg and Immingham) (Con)"          backbencher
#   "The Minister for Policing and Crime (Sarah Jones)"   office-holder
#   "Mr Speaker"                                          bare
#
# Note the second shape: a *single* trailing bracket in the Commons holds the
# person's name and the leading text is their role -- the opposite way round
# from the first shape. Sampled across four sitting days, every single-bracket
# Commons attribution was a role, and every two-bracket one ended in a party.
_ATTRIBUTION_RE = re.compile(
    r"""^\s*
    (?P<lead>.+?)                       # greedy-but-minimal leading text
    (?:\s*\((?P<first>[^()]+)\))?       # optional first bracket
    (?:\s*\((?P<second>[^()]+)\))?      # optional second bracket
    \s*$""",
    re.VERBOSE,
)

# Party abbreviations observed in the data. Used only to disambiguate the
# single-bracket case, where the Lords convention "Lord Callanan (Con)" collides
# with the Commons convention "The Minister for X (Sarah Jones)".
KNOWN_PARTIES = frozenset(
    {
        "alliance",
        "apni",
        "bishops",
        "cb",
        "con",
        "dup",
        "green",
        "ind",
        "lab",
        "lab/co-op",
        "ld",
        "non-afl",
        "pc",
        "reform",
        "sdlp",
        "sf",
        "snp",
        "tuv",
        "ukip",
        "uup",
        "xb",
    }
)

# Fallback for a party abbreviation we have not seen before. Real party labels
# are compact single tokens ("Reform", "Lab/Co-op"); personal names are not.
_MAX_PARTY_TOKEN_LENGTH = 12

# Honorifics we strip when building a speaker key, so "Mr Smith" and "Smith"
# collapse to one identity for members Hansard did not give us an id for.
_HONORIFICS = (
    "rt hon",
    "right hon",
    "sir",
    "dame",
    "lord",
    "lady",
    "baroness",
    "earl",
    "viscount",
    "dr",
    "mr",
    "mrs",
    "ms",
    "miss",
    "prof",
    "professor",
)

_WHITESPACE_RE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    """Collect visible text from a Hansard HTML fragment.

    Hansard bodies are small, well-formed fragments, so the stdlib parser is
    enough -- no third-party HTML dependency needed. Block-level tags become
    spaces so that "</p><p>" does not weld two words together.
    """

    _BLOCK_TAGS = frozenset({"p", "br", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append(" ")

    def text(self) -> str:
        return _WHITESPACE_RE.sub(" ", "".join(self._parts)).strip()


def strip_html(value: str | None) -> str:
    """Return the visible text of an HTML fragment, whitespace-normalised."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def count_words(text: str) -> int:
    return len(text.split()) if text else 0


def content_hash(*parts: object) -> str:
    """Stable hash of the fields that define "has this content changed?".

    Parts are joined with a separator that cannot appear in the values, so
    ("ab", "c") and ("a", "bc") never collide.
    """
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    """Current time, timezone-aware.

    A datetime rather than a string now that Postgres has a real
    TIMESTAMPTZ column to put it in.
    """
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Attribution:
    """The facts hidden inside Hansard's AttributedTo string.

    ``name`` is always the person, never their job title, so that the same
    member groups together whether they spoke from the back benches or from the
    Dispatch Box.
    """

    name: str
    constituency: str | None = None
    party: str | None = None
    role: str | None = None


def looks_like_party(value: str) -> bool:
    """Whether a bracketed value is a party label rather than a person's name."""
    cleaned = value.strip()
    if cleaned.lower() in KNOWN_PARTIES:
        return True
    # An unrecognised compact single token is far more likely to be a new party
    # abbreviation than a person, whose name would carry a space.
    return len(cleaned) <= _MAX_PARTY_TOKEN_LENGTH and not any(ch.isspace() for ch in cleaned)


def parse_attribution(attributed_to: str | None) -> Attribution | None:
    """Pull the speaker, their seat, their party and their role out of one string.

    The three shapes Hansard uses, and what each yields:

        "Martin Vickers (Brigg and Immingham) (Con)"
            -> name, constituency, party
        "The Minister for Policing and Crime (Sarah Jones)"
            -> name from the bracket, role from the leading text
        "Lord Callanan (Con)"
            -> name, party  (bracket recognised as a party label)
        "Mr Speaker"
            -> name only

    Returns None for empty input.
    """
    if not attributed_to or not attributed_to.strip():
        return None

    match = _ATTRIBUTION_RE.match(attributed_to.strip())
    if match is None:  # pragma: no cover - the pattern always matches non-empty input
        return Attribution(name=attributed_to.strip())

    lead = _clean(match.group("lead")) or attributed_to.strip()
    first = _clean(match.group("first"))
    second = _clean(match.group("second"))

    if second is not None:
        # Two brackets: the canonical backbencher form.
        return Attribution(name=lead, constituency=first, party=second)

    if first is not None:
        if looks_like_party(first):
            return Attribution(name=lead, party=first)
        # Office-holder: the bracket names the person, the lead names the job.
        return Attribution(name=first, role=lead)

    return Attribution(name=lead)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WHITESPACE_RE.sub(" ", value).strip()
    return cleaned or None


def speaker_key(member_id: int | None, name: str | None) -> str | None:
    """A stable identity for a speaker, for grouping across contributions.

    Prefers Hansard's numeric member id, which is authoritative. Falls back to a
    normalised name for the many rows that have none -- the Speaker, Deputy
    Speakers, tellers -- so those at least group with themselves rather than
    fragmenting on punctuation or honorific.

    The prefix records which rule was used, so a query can tell a confident
    identity from a best-effort one.
    """
    if member_id is not None:
        return f"member:{member_id}"
    normalised = normalise_name(name)
    return f"name:{normalised}" if normalised else None


def normalise_name(name: str | None) -> str:
    """Lowercase, strip accents, drop honorifics and punctuation."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = re.sub(r"[^a-z0-9\s]", " ", ascii_only.lower())
    tokens = lowered.split()

    while tokens:
        for size in (2, 1):
            if len(tokens) >= size and " ".join(tokens[:size]) in _HONORIFICS:
                tokens = tokens[size:]
                break
        else:
            break

    return " ".join(tokens)


def is_speech(item: DebateItem, body_text: str) -> bool:
    """Whether a transcript row carries something a person actually said."""
    if item.item_type != "Contribution":
        return False
    if item.hrs_tag in NON_SPEECH_TAGS:
        return False
    return bool(body_text)


# ---------------------------------------------------------------------------
# Rows, ready to store
#
# Postgres has real types, so these carry date, datetime and UUID rather than
# the strings SQLite forced on Phase 1. Converting here means a malformed
# identifier fails in the transform, with the payload in hand, instead of
# surfacing as an opaque database error three layers away.
# ---------------------------------------------------------------------------


def canonical_ext_id(value: str | None) -> str | None:
    """Put a Hansard external id into one consistent form.

    Hansard uses at least three identifier formats -- a UUID, a numeric string
    ("26011562000145") and a synthetic one ("DeferredDivisions2026-01-14") --
    so these cannot be UUIDs in the database.

    The part that actually bites: the *same* section arrives with different
    casing depending on which endpoint returned it. A debate's own ExtId comes
    back uppercase while the Navigator trail names its parent in lowercase, so
    comparing them as raw text silently fails to match. Anything that parses as
    a UUID is therefore stored in canonical lowercase form; anything else is
    kept verbatim, since we have no basis for reshaping it.
    """
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    try:
        return str(UUID(cleaned))
    except ValueError:
        return cleaned


@dataclass(frozen=True, slots=True)
class ContributionRow:
    """A transcript row, ready to store."""

    item_id: int
    debate_ext_id: str
    external_id: str | None
    order_in_section: int
    item_type: str
    hrs_tag: str | None
    is_speech: bool
    member_id: int | None
    attributed_to: str | None
    speaker_name: str | None
    speaker_key: str | None
    speaker_role: str | None
    party: str | None
    constituency: str | None
    body_html: str | None
    body_text: str
    word_count: int
    timecode: datetime | None
    is_reiteration: bool
    content_hash: str
    # Filled in by resolve_speakers once the whole debate is known: a single
    # item cannot tell whether it continues the paragraph before it.
    speaker_member_id: int | None = None
    attribution: str = "unattributed"


@dataclass(frozen=True, slots=True)
class DebateRow:
    """A debate section, ready to store."""

    ext_id: str
    hansard_id: int
    parent_ext_id: str | None
    parent_title: str | None
    depth: int
    title: str
    house: str
    sitting_date: date
    location: str | None
    hrs_tag: str | None
    debate_type_id: int | None
    volume_no: int | None
    content_last_updated: datetime | None
    contribution_count: int
    word_count: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class NormalisedDebate:
    """A debate section and its transcript rows, as a single unit of work."""

    debate: DebateRow
    contributions: tuple[ContributionRow, ...]


def normalise_contribution(item: DebateItem, debate_ext_id: str) -> ContributionRow:
    """Flatten one API transcript item into a storable row."""
    body_text = strip_html(item.value)
    attribution = parse_attribution(item.attributed_to)
    speech = is_speech(item, body_text)

    return ContributionRow(
        item_id=item.item_id,
        debate_ext_id=debate_ext_id,
        external_id=canonical_ext_id(item.external_id),
        order_in_section=item.order_in_section,
        item_type=item.item_type,
        hrs_tag=item.hrs_tag or None,
        is_speech=speech,
        member_id=item.member_id,
        attributed_to=item.attributed_to or None,
        speaker_name=attribution.name if attribution else None,
        speaker_key=speaker_key(item.member_id, attribution.name if attribution else None),
        speaker_role=attribution.role if attribution else None,
        party=attribution.party if attribution else None,
        constituency=attribution.constituency if attribution else None,
        body_html=item.value or None,
        body_text=body_text,
        word_count=count_words(body_text) if speech else 0,
        timecode=item.timecode,
        is_reiteration=item.is_reiteration,
        content_hash=content_hash(
            item.item_id, item.item_type, item.attributed_to, item.member_id, body_text
        ),
    )


def normalise_debate(detail: DebateDetail) -> NormalisedDebate:
    """Flatten one API debate payload into the rows it produces.

    Child debates are ignored on purpose: the API returns them inline here *and*
    as their own entries in the day's search results, so recursing would write
    every child twice. Ingest reaches them through the day index instead.
    """
    overview = detail.overview
    ext_id = canonical_ext_id(overview.ext_id)
    if ext_id is None:
        raise ValueError(f"Debate has no usable external id: {overview.ext_id!r}")

    contributions = tuple(normalise_contribution(item, ext_id) for item in detail.items)
    # Attribution is a debate-level question, so it happens once the whole
    # ordered transcript exists rather than per item.
    contributions = apply_resolved_speakers(contributions)
    speeches = [row for row in contributions if row.is_speech]

    # The Navigator is the breadcrumb from the day root down to this section, so
    # its length is the section's depth and its second-to-last entry the parent.
    trail = [node for node in detail.navigator if node.external_id]
    parent_title = trail[-2].title.strip() or None if len(trail) >= 2 else None

    debate = DebateRow(
        ext_id=ext_id,
        hansard_id=overview.hansard_id,
        parent_ext_id=canonical_ext_id(detail.parent_ext_id),
        parent_title=parent_title,
        depth=max(len(trail), 1),
        # Hansard titles routinely arrive with a leading space.
        title=overview.title.strip(),
        house=overview.house,
        sitting_date=overview.debate_date.date(),
        location=overview.location or None,
        hrs_tag=overview.hrs_tag or None,
        debate_type_id=overview.debate_type_id,
        volume_no=overview.volume_no,
        content_last_updated=overview.content_last_updated,
        contribution_count=len(speeches),
        word_count=sum(row.word_count for row in speeches),
        # Hashing the child hashes means any edit to any contribution changes
        # the debate hash, so "did this debate change?" is one column compare.
        content_hash=content_hash(
            overview.ext_id,
            overview.title.strip(),
            overview.location,
            *(row.content_hash for row in contributions),
        ),
    )
    return NormalisedDebate(debate=debate, contributions=contributions)


# ---------------------------------------------------------------------------
# Divisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DivisionVoteRow:
    """One member's vote."""

    division_ext_id: str
    member_id: int
    lobby: str
    is_teller: bool
    list_as: str | None
    party_at_vote: str | None


@dataclass(frozen=True, slots=True)
class DivisionRow:
    """A division, ready to store.

    ``ayes_count``/``noes_count`` are what Hansard states. They are kept apart
    from the vote rows, and never derived from them, because the two disagree.
    Excluding tellers accounts for most of the gap but not all: across 395 real
    divisions, 280 match exactly, 14 more reconcile once tellers are excluded,
    and 101 reconcile under neither rule. Storing both keeps the disagreement
    visible instead of quietly picking a winner.
    """

    ext_id: str
    division_id: int
    debate_ext_id: str | None
    house: str
    division_date: date
    division_time: time | None
    number: str | None
    debate_section: str | None
    ayes_count: int
    noes_count: int
    is_committee: bool
    text_before_vote: str | None
    text_after_vote: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class NormalisedDivision:
    division: DivisionRow
    votes: tuple[DivisionVoteRow, ...]


def _parse_time(value: str | None) -> time | None:
    if not value or not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def normalise_division(detail: DivisionDetail) -> NormalisedDivision:
    """Flatten a division payload into its row and its votes."""
    ext_id = canonical_ext_id(detail.ext_id)
    if ext_id is None:
        raise ValueError(f"Division has no usable external id: {detail.ext_id!r}")

    votes: list[DivisionVoteRow] = []
    seen: set[int] = set()
    for lobby, members in (("aye", detail.aye_members), ("no", detail.noe_members)):
        for member in members:
            # A member cannot be in both lobbies. If the payload says otherwise,
            # keep the first rather than letting a primary-key violation abort
            # the run; the duplicate check reports it afterwards.
            if member.member_id in seen:
                continue
            seen.add(member.member_id)
            votes.append(
                DivisionVoteRow(
                    division_ext_id=ext_id,
                    member_id=member.member_id,
                    lobby=lobby,
                    is_teller=member.is_teller,
                    list_as=member.list_as or None,
                    party_at_vote=member.party or None,
                )
            )

    division = DivisionRow(
        ext_id=ext_id,
        division_id=detail.division_id,
        debate_ext_id=canonical_ext_id(detail.debate_ext_id),
        house=detail.house,
        division_date=detail.division_date.date(),
        division_time=_parse_time(detail.division_time),
        number=detail.number or None,
        debate_section=(detail.debate_section or "").strip() or None,
        ayes_count=detail.ayes_count,
        noes_count=detail.noes_count,
        is_committee=detail.is_committee,
        text_before_vote=strip_html(detail.text_before_vote) or None,
        text_after_vote=strip_html(detail.text_after_vote) or None,
        content_hash=content_hash(
            detail.ext_id,
            detail.ayes_count,
            detail.noes_count,
            *(f"{v.member_id}:{v.lobby}:{v.is_teller}" for v in votes),
        ),
    )
    return NormalisedDivision(division=division, votes=tuple(votes))


# ---------------------------------------------------------------------------
# Members (source two)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberRow:
    """A member as the Members API states them.

    Deliberately separate from the ``*_hansard`` columns the transcript parser
    fills: two sources, two sets of columns, so a disagreement between them is
    visible rather than resolved by whichever job happened to run last.
    """

    member_id: int
    display_name: str | None
    full_title: str | None
    list_as: str | None
    party: str | None
    party_abbreviation: str | None
    constituency: str | None
    house: str | None
    gender: str | None
    thumbnail_url: str | None
    membership_start: date | None
    membership_end: date | None
    is_current: bool | None


def normalise_member(value: MemberValue) -> MemberRow:
    """Flatten a Members API record into a storable row."""
    membership = value.latest_house_membership
    party = value.latest_party
    return MemberRow(
        member_id=value.member_id,
        display_name=value.name_display_as,
        full_title=value.name_full_title,
        list_as=value.name_list_as,
        party=(party.name if party else None),
        party_abbreviation=(party.abbreviation if party else None),
        constituency=(membership.membership_from if membership else None),
        house=(membership.house if membership else None),
        gender=value.gender,
        thumbnail_url=value.thumbnail_url,
        membership_start=(membership.start if membership else None),
        membership_end=(membership.end if membership else None),
        is_current=value.is_current,
    )


# ---------------------------------------------------------------------------
# Speaker attribution across paragraph boundaries
#
# Hansard breaks a long speech into paragraphs and names the speaker on the
# first one only. Every later paragraph arrives with no MemberId and no
# AttributedTo, so counting words per member undercounts exactly the people who
# make long speeches -- ministers and shadow ministers.
#
# It is not recoverable from the API. The contributions endpoints return only
# the opening paragraph of each turn, and Hansard's own search index does not
# contain the continuations at all: searching a phrase from one returns nothing,
# while a phrase from the attributed paragraph is found instantly.
#
# What makes it recoverable locally is that ItemIds run consecutively through a
# speech -- 47259290 (named), 47259291..47259299 (unnamed), 47259300 (next
# speaker) -- so an unnamed paragraph belongs to the last named speaker before
# it. The rules below decide which unnamed rows are prose rather than the
# procedural furniture that sits between speeches.
# ---------------------------------------------------------------------------

#: Tags whose unattributed rows can be a continuation of the previous speech.
CONTINUATION_TAGS = frozenset({"hs_Para"})

#: hs_brev is used both for quotations inside a speech and for procedural
#: motions. Only the quotations continue a speech, and they open with a quote.
QUOTE_CONTINUATION_TAGS = frozenset({"hs_brev"})

QUOTE_OPENERS = ("“", '"', "‘")

#: Below this, a paragraph is far more likely to be clerk text ("Brought up, and
#: read the First time.") or a written-evidence reference ("RB 26 Transport UK")
#: than the middle of somebody's speech.
MIN_CONTINUATION_WORDS = 20

#: Openings that mark procedural text rather than continued speech.
#:
#: Kept deliberately short. An over-eager list is worse than a missing entry,
#: because a false positive does not just drop one row -- it clears the held
#: speaker and orphans every remaining paragraph of that speech. "Lords
#: amendment" was in this list at first and silently cost the whole of a shadow
#: minister's speech, because he opened a paragraph with "Lords amendment 7, on
#: court transcripts...". These are the formulaic openings only; the word-count
#: floor below is what filters the short clerical lines.
#:
#: "Order." earns its place for a different reason. It is genuine speech, but it
#: is the *Chair* interrupting, so carrying the previous speaker forward would
#: put the Chair's words in someone else's mouth.
PROCEDURAL_PREFIXES = (
    "question put",
    "question agreed",
    "question proposed",
    "question again proposed",
    "motion made",
    "ordered,",
    "resolved,",
    "bill read",
    "committee rose",
    "the committee divided",
    "the house divided",
    "sitting suspended",
    "amendment proposed",
    "amendment made",
    "amendment agreed",
    "order.",
    "order,",
)


class AttributionSource(StrEnum):
    """Where a contribution's speaker came from.

    Recorded per row so that any analysis can decide whether to trust an
    inference. "stated" is Hansard's word; "carried" is ours.
    """

    STATED = "stated"
    CARRIED = "carried"
    UNATTRIBUTED = "unattributed"


@dataclass(frozen=True, slots=True)
class ResolvedSpeaker:
    member_id: int | None
    source: AttributionSource


def _is_continuation(row: ContributionRow) -> bool:
    """Whether an unattributed row continues the speech before it."""
    text = row.body_text.strip()
    if not text:
        return False

    lowered = text.lower()
    if lowered.startswith(PROCEDURAL_PREFIXES):
        return False

    if row.hrs_tag in QUOTE_CONTINUATION_TAGS:
        # A block quotation read out mid-speech. Length is not a useful signal
        # here -- a quoted line can be short and still belong to the speaker.
        return text.startswith(QUOTE_OPENERS)

    if row.hrs_tag not in CONTINUATION_TAGS:
        return False

    return row.word_count >= MIN_CONTINUATION_WORDS


def resolve_speakers(rows: Sequence[ContributionRow]) -> dict[int, ResolvedSpeaker]:
    """Attribute continuation paragraphs to the speaker who began the speech.

    Walks a debate in order, holding the last member Hansard named. An
    unattributed row that looks like prose inherits that member; anything else
    clears the held speaker, so procedural text between two speeches cannot
    bridge them and hand the second speaker's words to the first.

    Returns a mapping keyed by item_id, so callers can apply it to rows they
    already hold without rebuilding them.
    """
    resolved: dict[int, ResolvedSpeaker] = {}
    current: int | None = None

    for row in sorted(rows, key=lambda r: r.order_in_section):
        if row.member_id is not None:
            current = row.member_id
            resolved[row.item_id] = ResolvedSpeaker(row.member_id, AttributionSource.STATED)
            continue

        if row.attributed_to and row.attributed_to.strip():
            # Hansard named a speaker we cannot map to a member -- "The Chair",
            # "Hon. Members". The identity is unresolved, but the *turn* is not:
            # somebody else is talking, so the previous speaker has stopped.
            # Missing this attributed 298 Chair interventions to whichever
            # member happened to speak before them.
            current = None
            resolved[row.item_id] = ResolvedSpeaker(None, AttributionSource.UNATTRIBUTED)
            continue

        # A division is a hard break: whatever follows is a new turn.
        if row.item_type == "Division":
            current = None
            resolved[row.item_id] = ResolvedSpeaker(None, AttributionSource.UNATTRIBUTED)
            continue

        # Timestamps and column markers sit inside speeches and must not break
        # them, but they are not speech themselves.
        if not row.is_speech:
            resolved[row.item_id] = ResolvedSpeaker(None, AttributionSource.UNATTRIBUTED)
            continue

        if current is not None and _is_continuation(row):
            resolved[row.item_id] = ResolvedSpeaker(current, AttributionSource.CARRIED)
        else:
            # Procedural text ends the speech it followed.
            current = None
            resolved[row.item_id] = ResolvedSpeaker(None, AttributionSource.UNATTRIBUTED)

    return resolved


def apply_resolved_speakers(
    rows: Sequence[ContributionRow],
) -> tuple[ContributionRow, ...]:
    """Return the same rows with ``speaker_member_id`` and ``attribution`` set."""
    resolved = resolve_speakers(rows)
    return tuple(
        replace(
            row,
            speaker_member_id=resolved[row.item_id].member_id,
            attribution=resolved[row.item_id].source.value,
        )
        for row in rows
    )
