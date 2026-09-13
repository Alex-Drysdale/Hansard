"""Wire models for the Parliament Members API.

A separate module from the Hansard models even though both come from
Parliament, because they are separate contracts: the Members API is camelCase
and wraps everything in ``value``/``items``, while Hansard is PascalCase and
returns bare arrays. Pretending they are one shape would mean a change to
either breaking both.

The one thing they agree on is the identifier -- Members API ``id`` is Hansard
``MemberId`` -- and that agreement is the whole reason this source is useful.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Members API encodes the house as an integer, unlike Hansard's string.
HOUSE_BY_ID = {1: "Commons", 2: "Lords"}


class _WireModel(BaseModel):
    """Ignore unknown fields, so new API fields never crash us."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class Party(_WireModel):
    id: int | None = None
    name: str | None = None
    abbreviation: str | None = None


class MembershipStatus(_WireModel):
    status_is_active: bool | None = Field(default=None, alias="statusIsActive")
    status_description: str | None = Field(default=None, alias="statusDescription")


class HouseMembership(_WireModel):
    """A seat: which constituency, from when, and whether it has ended."""

    membership_from: str | None = Field(default=None, alias="membershipFrom")
    membership_from_id: int | None = Field(default=None, alias="membershipFromId")
    house_id: int | None = Field(default=None, alias="house")
    start_date: datetime | None = Field(default=None, alias="membershipStartDate")
    end_date: datetime | None = Field(default=None, alias="membershipEndDate")
    end_reason: str | None = Field(default=None, alias="membershipEndReason")
    status: MembershipStatus | None = Field(default=None, alias="membershipStatus")

    @property
    def house(self) -> str | None:
        return HOUSE_BY_ID.get(self.house_id) if self.house_id is not None else None

    @property
    def start(self) -> date | None:
        return self.start_date.date() if self.start_date else None

    @property
    def end(self) -> date | None:
        return self.end_date.date() if self.end_date else None


class MemberValue(_WireModel):
    """The member record itself.

    ``id`` is the canonical identifier across this project: the same integer
    Hansard puts on every contribution and every division vote.
    """

    member_id: int = Field(alias="id")
    name_display_as: str | None = Field(default=None, alias="nameDisplayAs")
    name_list_as: str | None = Field(default=None, alias="nameListAs")
    name_full_title: str | None = Field(default=None, alias="nameFullTitle")
    gender: str | None = Field(default=None, alias="gender")
    thumbnail_url: str | None = Field(default=None, alias="thumbnailUrl")
    latest_party: Party | None = Field(default=None, alias="latestParty")
    latest_house_membership: HouseMembership | None = Field(
        default=None, alias="latestHouseMembership"
    )

    @field_validator("name_display_as", "name_list_as", "name_full_title")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @property
    def is_current(self) -> bool | None:
        """Whether the member currently sits.

        Derived from the seat's status rather than its end date: a membership
        with no end date is normally current, but the API states it outright and
        the stated value is the one to trust.
        """
        membership = self.latest_house_membership
        if membership is None:
            return None
        if membership.status is not None and membership.status.status_is_active is not None:
            return membership.status.status_is_active
        return membership.end_date is None


class MemberEnvelope(_WireModel):
    """``/Members/{id}`` wraps the record in ``value``."""

    value: MemberValue


class MemberSearchItem(_WireModel):
    value: MemberValue


class MemberSearchResponse(_WireModel):
    """``/Members/Search`` -- a page of results plus the total available."""

    items: list[MemberSearchItem] = Field(default_factory=list)
    total_results: int = Field(default=0, alias="totalResults")
