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
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

from hansard.api.models import DebateDetail, DebateItem

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


def utc_now() -> str:
    """Current time as a UTC ISO-8601 string, to the second."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
    timecode: str | None
    is_reiteration: bool
    content_hash: str


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
    sitting_date: str
    location: str | None
    hrs_tag: str | None
    debate_type_id: int | None
    volume_no: int | None
    content_last_updated: str | None
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
        external_id=item.external_id or None,
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
        timecode=item.timecode.isoformat() if item.timecode else None,
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
    contributions = tuple(normalise_contribution(item, overview.ext_id) for item in detail.items)
    speeches = [row for row in contributions if row.is_speech]

    # The Navigator is the breadcrumb from the day root down to this section,
    # so its length is the section's depth and its second-to-last entry is the
    # parent. The API gives us no explicit parent field to use instead.
    trail = [node for node in detail.navigator if node.external_id]
    parent_title = trail[-2].title.strip() or None if len(trail) >= 2 else None

    debate = DebateRow(
        ext_id=overview.ext_id,
        hansard_id=overview.hansard_id,
        parent_ext_id=detail.parent_ext_id,
        parent_title=parent_title,
        depth=max(len(trail), 1),
        # Hansard titles routinely arrive with a leading space.
        title=overview.title.strip(),
        house=overview.house,
        sitting_date=overview.debate_date.date().isoformat(),
        location=overview.location or None,
        hrs_tag=overview.hrs_tag or None,
        debate_type_id=overview.debate_type_id,
        volume_no=overview.volume_no,
        content_last_updated=(
            overview.content_last_updated.isoformat() if overview.content_last_updated else None
        ),
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
