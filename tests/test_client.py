"""Tests for the HTTP layer.

No real network here: respx intercepts at the transport level, so the tests
exercise the client's own retry, pagination and rate-limit logic while running
in milliseconds and giving the same answer every time.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from hansard.api.client import (
    ApiError,
    HansardClient,
    NotFoundError,
    months_between,
)
from hansard.api.http import RateLimiter
from hansard.config import Settings
from tests.conftest import load_fixture

BASE = "https://hansard.test"


@pytest.fixture
def client(settings: Settings, recorded_sleeps: list[float]):
    with HansardClient(settings, sleep=recorded_sleeps.append) as instance:
        yield instance


class TestMonthsBetween:
    def test_within_one_year(self) -> None:
        assert list(months_between(date(2026, 1, 1), date(2026, 4, 30))) == [
            (2026, 1),
            (2026, 2),
            (2026, 3),
            (2026, 4),
        ]

    def test_crosses_a_year_boundary(self) -> None:
        assert list(months_between(date(2025, 11, 5), date(2026, 2, 3))) == [
            (2025, 11),
            (2025, 12),
            (2026, 1),
            (2026, 2),
        ]

    def test_single_month(self) -> None:
        assert list(months_between(date(2026, 3, 2), date(2026, 3, 30))) == [(2026, 3)]


class TestRateLimiter:
    def test_spaces_successive_calls(self) -> None:
        slept: list[float] = []
        limiter = RateLimiter(5.0, sleep=slept.append, clock=lambda: 0.0)
        limiter.wait()
        limiter.wait()
        limiter.wait()
        assert slept == [pytest.approx(0.2), pytest.approx(0.2)]

    def test_does_not_sleep_when_enough_time_has_passed(self) -> None:
        slept: list[float] = []
        ticks = iter([0.0, 10.0, 20.0])
        limiter = RateLimiter(5.0, sleep=slept.append, clock=lambda: next(ticks))
        limiter.wait()
        limiter.wait()
        assert slept == []

    def test_rejects_a_nonsense_rate(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(0)


class TestSittingDates:
    @respx.mock
    def test_parses_the_calendar(self, client: HansardClient) -> None:
        respx.get(f"{BASE}/overview/calendar.json").mock(
            return_value=httpx.Response(200, json=load_fixture("calendar_2026_01.json"))
        )
        dates = client.sitting_dates("Commons", 2026, 1)
        assert dates[0] == date(2026, 1, 5)
        assert len(dates) == 16

    @respx.mock
    def test_range_walk_filters_to_the_requested_window(self, client: HansardClient) -> None:
        respx.get(f"{BASE}/overview/calendar.json").mock(
            return_value=httpx.Response(200, json=load_fixture("calendar_2026_01.json"))
        )
        found = list(client.iter_sitting_dates("Commons", date(2026, 1, 10), date(2026, 1, 20)))
        assert found == [
            date(2026, 1, 12),
            date(2026, 1, 13),
            date(2026, 1, 14),
            date(2026, 1, 15),
            date(2026, 1, 19),
            date(2026, 1, 20),
        ]

    @respx.mock
    def test_results_come_back_sorted(self, client: HansardClient) -> None:
        respx.get(f"{BASE}/overview/calendar.json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"House": "Commons", "ItemDate": "2026-01-20T00:00:00"},
                    {"House": "Commons", "ItemDate": "2026-01-05T00:00:00"},
                ],
            )
        )
        assert list(client.iter_sitting_dates("Commons", date(2026, 1, 1), date(2026, 1, 31))) == [
            date(2026, 1, 5),
            date(2026, 1, 20),
        ]


class TestDebatesOn:
    @respx.mock
    def test_returns_the_days_sections(self, client: HansardClient) -> None:
        respx.get(f"{BASE}/search/debates.json").mock(
            return_value=httpx.Response(200, json=load_fixture("search_debates_day.json"))
        )
        results = client.debates_on("Commons", date(2026, 1, 14))
        assert len(results) == 3
        assert results[0].ext_id == "9969E436-926F-463C-8323-059D1158DF4A"

    @respx.mock
    def test_sends_the_queryparameters_prefix_the_api_requires(self, client: HansardClient) -> None:
        route = respx.get(f"{BASE}/search/debates.json").mock(
            return_value=httpx.Response(200, json=load_fixture("search_debates_day.json"))
        )
        client.debates_on("Commons", date(2026, 1, 14))

        params = route.calls.last.request.url.params
        assert params["queryParameters.house"] == "Commons"
        assert params["queryParameters.startDate"] == "2026-01-14"
        assert params["queryParameters.endDate"] == "2026-01-14"

    @respx.mock
    def test_pages_until_the_advertised_total_is_reached(self, client: HansardClient) -> None:
        page_one = {
            "TotalResultCount": 3,
            "Results": [
                {
                    "DebateSectionExtId": "A",
                    "Title": "one",
                    "House": "Commons",
                    "SittingDate": "2026-01-14T00:00:00",
                },
                {
                    "DebateSectionExtId": "B",
                    "Title": "two",
                    "House": "Commons",
                    "SittingDate": "2026-01-14T00:00:00",
                },
            ],
        }
        page_two = {
            "TotalResultCount": 3,
            "Results": [
                {
                    "DebateSectionExtId": "C",
                    "Title": "three",
                    "House": "Commons",
                    "SittingDate": "2026-01-14T00:00:00",
                },
            ],
        }
        route = respx.get(f"{BASE}/search/debates.json").mock(
            side_effect=[httpx.Response(200, json=page_one), httpx.Response(200, json=page_two)]
        )

        results = client.debates_on("Commons", date(2026, 1, 14))

        assert [row.ext_id for row in results] == ["A", "B", "C"]
        assert route.call_count == 2
        assert route.calls[1].request.url.params["queryParameters.skip"] == "2"

    @respx.mock
    def test_an_empty_page_stops_the_walk(self, client: HansardClient) -> None:
        # Protects against a server that ignores `skip`: without this guard the
        # loop would keep asking for more for ever.
        overstated = {
            "TotalResultCount": 99,
            "Results": [
                {
                    "DebateSectionExtId": "A",
                    "Title": "one",
                    "House": "Commons",
                    "SittingDate": "2026-01-14T00:00:00",
                }
            ],
        }
        route = respx.get(f"{BASE}/search/debates.json").mock(
            side_effect=[
                httpx.Response(200, json=overstated),
                httpx.Response(200, json={"TotalResultCount": 99, "Results": []}),
            ]
        )
        results = client.debates_on("Commons", date(2026, 1, 14))
        assert len(results) == 1
        assert route.call_count == 2


class TestDebate:
    @respx.mock
    def test_parses_a_transcript(self, client: HansardClient) -> None:
        respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            return_value=httpx.Response(200, json=load_fixture("debate_with_speeches.json"))
        )
        detail = client.debate("ABC")
        assert detail.overview.title == "Oil Refining Sector"
        assert detail.items

    @respx.mock
    def test_unexpected_shape_is_reported_clearly(self, client: HansardClient) -> None:
        respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            return_value=httpx.Response(200, json={"Overview": {"nope": True}})
        )
        with pytest.raises(ApiError, match="Unexpected debate payload"):
            client.debate("ABC")


class TestRetries:
    @respx.mock
    def test_retries_a_server_error_then_succeeds(
        self, client: HansardClient, recorded_sleeps: list[float]
    ) -> None:
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=load_fixture("debate_with_speeches.json")),
            ]
        )
        assert client.debate("ABC").overview.title == "Oil Refining Sector"
        assert route.call_count == 2
        assert recorded_sleeps, "should have backed off before retrying"

    @respx.mock
    def test_retries_a_rate_limit_response(self, client: HansardClient) -> None:
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json=load_fixture("debate_with_speeches.json")),
            ]
        )
        client.debate("ABC")
        assert route.call_count == 2

    @respx.mock
    def test_retries_a_timeout(self, client: HansardClient) -> None:
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            side_effect=[
                httpx.TimeoutException("slow"),
                httpx.Response(200, json=load_fixture("debate_with_speeches.json")),
            ]
        )
        client.debate("ABC")
        assert route.call_count == 2

    @respx.mock
    def test_gives_up_after_the_configured_number_of_attempts(
        self, client: HansardClient, settings: Settings
    ) -> None:
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(return_value=httpx.Response(503))
        with pytest.raises(ApiError, match="failed after"):
            client.debate("ABC")
        assert route.call_count == settings.max_retries + 1

    @respx.mock
    def test_a_404_is_not_retried(self, client: HansardClient) -> None:
        # Retrying a missing resource just wastes the API's time.
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(return_value=httpx.Response(404))
        with pytest.raises(NotFoundError):
            client.debate("ABC")
        assert route.call_count == 1

    @respx.mock
    def test_a_400_is_not_retried(self, client: HansardClient) -> None:
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(return_value=httpx.Response(400))

        # Reported as our own error type, not httpx's: callers should never need
        # to know which HTTP library is underneath, and it lets ingest skip one
        # bad section by catching ApiError rather than aborting the run.
        with pytest.raises(ApiError, match="returned 400"):
            client.debate("ABC")
        assert route.call_count == 1

    @respx.mock
    def test_html_error_page_is_reported_as_such(self, client: HansardClient) -> None:
        # Hansard answers an unknown route with HTML and a 200 status, which
        # would otherwise surface as an opaque JSON decode error.
        respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            return_value=httpx.Response(
                200, text="<html>not found</html>", headers={"content-type": "text/html"}
            )
        )
        with pytest.raises(ApiError, match="non-JSON"):
            client.debate("ABC")


class TestHeaders:
    @respx.mock
    def test_identifies_itself(self, client: HansardClient, settings: Settings) -> None:
        route = respx.get(f"{BASE}/debates/debate/ABC.json").mock(
            return_value=httpx.Response(200, json=load_fixture("debate_with_speeches.json"))
        )
        client.debate("ABC")
        assert route.calls.last.request.headers["user-agent"] == settings.user_agent
