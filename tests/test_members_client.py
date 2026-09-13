"""Tests for source two: the Parliament Members API client.

HTTP is intercepted at the transport layer, so these exercise pagination and
model parsing without touching the network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from hansard.api.http import ApiError
from hansard.api.members_client import MembersClient
from hansard.api.members_models import MemberValue
from hansard.config import Settings
from hansard.pipeline.normalise import normalise_member
from tests.conftest import load_fixture

BASE = "https://members.test"


@pytest.fixture
def client(settings: Settings):
    with MembersClient(settings) as instance:
        yield instance


class TestMember:
    @respx.mock
    def test_unwraps_the_value_envelope(self, client: MembersClient) -> None:
        # Members API wraps a single record in `value`; Hansard does not wrap
        # anything. The two contracts are modelled separately for this reason.
        respx.get(f"{BASE}/Members/467").mock(
            return_value=httpx.Response(200, json=load_fixture("member.json"))
        )
        member = client.member(467)
        assert member.member_id == 467
        assert member.name_display_as == "Sir Lindsay Hoyle"

    @respx.mock
    def test_unexpected_shape_is_reported_clearly(self, client: MembersClient) -> None:
        respx.get(f"{BASE}/Members/467").mock(
            return_value=httpx.Response(200, json={"value": {"nope": True}})
        )
        with pytest.raises(ApiError, match="Unexpected member payload"):
            client.member(467)


class TestIterMembers:
    @respx.mock
    def test_walks_every_page(self, client: MembersClient) -> None:
        def page(start: int, count: int, total: int) -> dict:
            return {
                "totalResults": total,
                "items": [
                    {"value": {"id": start + i, "nameDisplayAs": f"Member {start + i}"}}
                    for i in range(count)
                ],
            }

        route = respx.get(f"{BASE}/Members/Search").mock(
            side_effect=[
                httpx.Response(200, json=page(1, 20, 45)),
                httpx.Response(200, json=page(21, 20, 45)),
                httpx.Response(200, json=page(41, 5, 45)),
            ]
        )

        members = list(client.iter_members("Commons"))

        assert len(members) == 45
        assert route.call_count == 3
        assert route.calls[1].request.url.params["skip"] == "20"

    @respx.mock
    def test_sends_the_integer_house_id(self, client: MembersClient) -> None:
        # Members API encodes the house as 1/2; Hansard uses the string.
        route = respx.get(f"{BASE}/Members/Search").mock(
            return_value=httpx.Response(200, json={"totalResults": 0, "items": []})
        )
        list(client.iter_members("Commons"))
        assert route.calls.last.request.url.params["House"] == "1"

    @respx.mock
    def test_caps_the_page_size_at_the_api_limit(self, client: MembersClient) -> None:
        # The API silently caps a page at 20 however large `take` is; asking for
        # more just makes request and response disagree.
        route = respx.get(f"{BASE}/Members/Search").mock(
            return_value=httpx.Response(200, json={"totalResults": 0, "items": []})
        )
        list(client.iter_members("Commons"))
        assert route.calls.last.request.url.params["take"] == "20"

    @respx.mock
    def test_an_empty_page_stops_the_walk(self, client: MembersClient) -> None:
        # Guards against a server that ignores `skip` while overstating the total.
        route = respx.get(f"{BASE}/Members/Search").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"totalResults": 999, "items": [{"value": {"id": 1}}]},
                ),
                httpx.Response(200, json={"totalResults": 999, "items": []}),
            ]
        )
        assert len(list(client.iter_members("Commons"))) == 1
        assert route.call_count == 2

    @respx.mock
    def test_current_only_is_off_by_default(self, client: MembersClient) -> None:
        # Our transcripts span a period during which people left: a member who
        # resigned in February still spoke in January and needs a party.
        route = respx.get(f"{BASE}/Members/Search").mock(
            return_value=httpx.Response(200, json={"totalResults": 0, "items": []})
        )
        list(client.iter_members("Commons"))
        assert "IsCurrentMember" not in route.calls.last.request.url.params

    def test_an_unknown_house_is_rejected_before_any_request(self, client: MembersClient) -> None:
        with pytest.raises(ApiError, match="Unknown house"):
            list(client.iter_members("Senate"))


class TestParsingRealPayloads:
    def test_a_search_page_parses(self) -> None:
        from hansard.api.members_models import MemberSearchResponse

        page = MemberSearchResponse.model_validate(load_fixture("members_search.json"))
        assert page.total_results > 0
        assert page.items[0].value.member_id == 172

    def test_normalising_pulls_out_party_and_seat(self, member_value: MemberValue) -> None:
        row = normalise_member(member_value)
        assert row.member_id == 467
        assert row.display_name == "Sir Lindsay Hoyle"
        assert row.party == "Speaker"
        assert row.constituency == "Chorley"
        assert row.house == "Commons"
        assert row.is_current is True

    def test_house_integer_is_decoded(self, member_value: MemberValue) -> None:
        assert member_value.latest_house_membership is not None
        assert member_value.latest_house_membership.house_id == 1
        assert member_value.latest_house_membership.house == "Commons"

    def test_a_member_with_no_membership_normalises_to_nulls(self) -> None:
        row = normalise_member(MemberValue.model_validate({"id": 1, "nameDisplayAs": "X"}))
        assert row.party is None
        assert row.constituency is None
        assert row.is_current is None

    def test_blank_names_become_none(self) -> None:
        value = MemberValue.model_validate({"id": 1, "nameDisplayAs": "   "})
        assert value.name_display_as is None
