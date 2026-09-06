"""Pydantic models for the Hansard API responses we consume.

These mirror the wire format exactly (PascalCase field names, the API's own
nullability) and do nothing else. Turning them into our own domain shape is the
job of :mod:`hansard.pipeline.normalise` -- keeping the two apart means an
upstream field rename breaks in one obvious place rather than everywhere.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _WireModel(BaseModel):
    """Base for wire models: ignore unknown fields so new API fields never crash us."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class CalendarEntry(_WireModel):
    """One sitting day from ``/overview/calendar.json``."""

    house: str = Field(alias="House")
    item_date: datetime = Field(alias="ItemDate")

    @property
    def sitting_date(self) -> date:
        return self.item_date.date()


class DebateSummary(_WireModel):
    """One search hit from ``/search/debates.json``.

    This is the index we walk: it tells us which debate sections exist on a day,
    but carries no speech text -- that needs a per-section fetch.
    """

    ext_id: str = Field(alias="DebateSectionExtId")
    title: str = Field(alias="Title")
    house: str = Field(alias="House")
    sitting_date: datetime = Field(alias="SittingDate")
    debate_section: str | None = Field(default=None, alias="DebateSection")
    rank: int | None = Field(default=None, alias="Rank")


class DebateSearchResponse(_WireModel):
    """Envelope returned by ``/search/debates.json``."""

    results: list[DebateSummary] = Field(default_factory=list, alias="Results")
    total_result_count: int = Field(default=0, alias="TotalResultCount")


class DebateOverview(_WireModel):
    """Header block of a debate section from ``/debates/debate/{extId}.json``."""

    hansard_id: int = Field(alias="Id")
    ext_id: str = Field(alias="ExtId")
    title: str = Field(alias="Title")
    house: str = Field(alias="House")
    debate_date: datetime = Field(alias="Date")
    location: str | None = Field(default=None, alias="Location")
    hrs_tag: str | None = Field(default=None, alias="HRSTag")
    volume_no: int | None = Field(default=None, alias="VolumeNo")
    debate_type_id: int | None = Field(default=None, alias="DebateTypeId")
    section_type: int | None = Field(default=None, alias="SectionType")
    # Upstream's own "this changed" marker. We store it so a re-run can tell a
    # genuinely revised debate from one that merely got re-fetched.
    content_last_updated: datetime | None = Field(default=None, alias="ContentLastUpdated")


class NavigatorNode(_WireModel):
    """One ancestor in a debate's breadcrumb trail.

    The last node is the debate itself; the one before it is its parent. This is
    how we recover the section hierarchy, since the API gives no explicit
    ``ParentExtId`` on the overview.
    """

    hansard_id: int = Field(alias="Id")
    title: str = Field(alias="Title")
    parent_id: int | None = Field(default=None, alias="ParentId")
    sort_order: int | None = Field(default=None, alias="SortOrder")
    external_id: str | None = Field(default=None, alias="ExternalId")


class DebateItem(_WireModel):
    """A single row of the transcript: a speech, a timestamp, a division marker."""

    item_id: int = Field(alias="ItemId")
    item_type: str = Field(alias="ItemType")
    order_in_section: int = Field(alias="OrderInSection")
    value: str | None = Field(default=None, alias="Value")
    member_id: int | None = Field(default=None, alias="MemberId")
    attributed_to: str | None = Field(default=None, alias="AttributedTo")
    timecode: datetime | None = Field(default=None, alias="Timecode")
    external_id: str | None = Field(default=None, alias="ExternalId")
    hrs_tag: str | None = Field(default=None, alias="HRSTag")
    hansard_section: str | None = Field(default=None, alias="HansardSection")
    uin: str | None = Field(default=None, alias="UIN")
    is_reiteration: bool = Field(default=False, alias="IsReiteration")

    @field_validator("member_id")
    @classmethod
    def _drop_sentinel_member_id(cls, value: int | None) -> int | None:
        """Treat 0 as absent -- Hansard uses it for unattributed procedural text."""
        return None if value == 0 else value


class DebateDetail(_WireModel):
    """Full payload of ``/debates/debate/{extId}.json``."""

    overview: DebateOverview = Field(alias="Overview")
    navigator: list[NavigatorNode] = Field(default_factory=list, alias="Navigator")
    items: list[DebateItem] = Field(default_factory=list, alias="Items")
    # Child sections are returned inline *and* as their own top-level search
    # hits, so ingest must not recurse here or every child lands twice.
    child_debates: list[DebateDetail] = Field(default_factory=list, alias="ChildDebates")

    @property
    def parent_ext_id(self) -> str | None:
        """External id of this debate's immediate parent, if it has one."""
        trail = [node for node in self.navigator if node.external_id]
        if len(trail) < 2:
            return None
        return trail[-2].external_id
