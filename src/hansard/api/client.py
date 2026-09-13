"""HTTP client for the Hansard API.

Source one: debates, transcripts and divisions. The transport concerns --
rate limiting, retries, error translation -- live in
:mod:`hansard.api.http`, shared with the Members API client, so this module
holds only the endpoints and their shapes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from hansard.api.http import ApiError, NotFoundError, RetryingHttpClient
from hansard.api.models import (
    CalendarEntry,
    DebateDetail,
    DebateSearchResponse,
    DebateSummary,
    DivisionDetail,
    DivisionSummary,
)
from hansard.config import Settings
from hansard.logging_config import get_logger

log = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

# Re-exported so callers keep importing their errors from the client they use.
__all__ = [
    "ApiError",
    "HansardClient",
    "NotFoundError",
    "months_between",
]


class HansardClient:
    """Typed access to the Hansard endpoints this pipeline needs."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        http: RetryingHttpClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = http or RetryingHttpClient(
            base_url=settings.hansard_base_url,
            settings=settings,
            name="hansard",
            transport=transport,
            sleep=sleep,
        )

    def __enter__(self) -> HansardClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Sitting days
    # ------------------------------------------------------------------

    def sitting_dates(self, house: str, year: int, month: int) -> list[date]:
        """Sitting days for one calendar month.

        Asking the calendar which days sat is far cheaper than probing all 30.
        """
        payload = self._http.get_json(
            "/overview/calendar.json",
            params={"year": year, "month": month, "house": house},
        )
        entries = self._parse_list(CalendarEntry, payload, context=f"calendar {year}-{month:02d}")
        return [entry.sitting_date for entry in entries]

    def iter_sitting_dates(self, house: str, start: date, end: date) -> Iterator[date]:
        """Sitting days across an arbitrary range, in chronological order."""
        seen: set[date] = set()
        for year, month in months_between(start, end):
            for sitting in self.sitting_dates(house, year, month):
                if start <= sitting <= end:
                    seen.add(sitting)
        yield from sorted(seen)

    # ------------------------------------------------------------------
    # Debates
    # ------------------------------------------------------------------

    def debates_on(self, house: str, on: date) -> list[DebateSummary]:
        """Every debate section recorded for one sitting day.

        Pages until we hold the count the API advertised, so a busy day is never
        silently truncated.
        """
        collected: list[DebateSummary] = []
        skip = 0
        expected: int | None = None

        while True:
            payload = self._http.get_json(
                "/search/debates.json",
                params={
                    "queryParameters.house": house,
                    "queryParameters.startDate": on.isoformat(),
                    "queryParameters.endDate": on.isoformat(),
                    "queryParameters.skip": skip,
                    "queryParameters.take": self._settings.page_size,
                },
            )
            try:
                page = DebateSearchResponse.model_validate(payload)
            except ValidationError as exc:
                raise ApiError(f"Unexpected debate search payload for {on}: {exc}") from exc

            if expected is None:
                expected = page.total_result_count
            collected.extend(page.results)

            # An empty page ends the walk. Without that check, a server that
            # ignored `skip` would hand back the same rows until the disk filled.
            if not page.results or len(collected) >= expected:
                break
            skip += len(page.results)

        return collected

    def debate(self, ext_id: str) -> DebateDetail:
        """Full transcript for one debate section."""
        payload = self._http.get_json(f"/debates/debate/{ext_id}.json")
        try:
            return DebateDetail.model_validate(payload)
        except ValidationError as exc:
            raise ApiError(f"Unexpected debate payload for {ext_id}: {exc}") from exc

    # ------------------------------------------------------------------
    # Divisions
    # ------------------------------------------------------------------

    def divisions_in(self, debate_ext_id: str) -> list[DivisionSummary]:
        """Divisions held during a debate section, without the vote lists.

        Cheap enough to call for any debate whose transcript contained a
        Division item, and returns an empty list rather than 404 when there are
        none.
        """
        payload = self._http.get_json(f"/debates/divisions/{debate_ext_id}.json")
        return self._parse_list(DivisionSummary, payload, context=f"divisions for {debate_ext_id}")

    def division(self, division_ext_id: str) -> DivisionDetail:
        """One division including how every member voted."""
        payload = self._http.get_json(f"/debates/division/{division_ext_id}.json")
        try:
            return DivisionDetail.model_validate(payload)
        except ValidationError as exc:
            raise ApiError(f"Unexpected division payload for {division_ext_id}: {exc}") from exc

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_list(model: type[ModelT], payload: object, *, context: str) -> list[ModelT]:
        if not isinstance(payload, list):
            raise ApiError(f"Expected a JSON array for {context}, got {type(payload).__name__}")
        try:
            return [model.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise ApiError(f"Unexpected payload for {context}: {exc}") from exc


def months_between(start: date, end: date) -> Iterator[tuple[int, int]]:
    """Yield (year, month) pairs covering the inclusive range."""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
