"""HTTP client for the Hansard API.

Responsibilities, and nothing else: build URLs, apply politeness (rate limit),
survive transient failure (retry with backoff), and hand back parsed models.
No database, no business rules.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from datetime import date
from types import TracebackType
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from hansard.api.models import (
    CalendarEntry,
    DebateDetail,
    DebateSearchResponse,
    DebateSummary,
)
from hansard.config import Settings

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

# 429 means we were impolite; 5xx means upstream stumbled. Both are worth
# another go. Everything else (400, 404) is our fault and retrying won't help.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HansardApiError(RuntimeError):
    """A request failed in a way we could not recover from."""


class HansardNotFoundError(HansardApiError):
    """The API returned 404 for a resource we expected to exist."""


class RateLimiter:
    """Blocking limiter that spaces requests at least ``1/rate`` seconds apart.

    Deliberately simple: one process, one thread, no burst allowance. Predictable
    beats clever when the goal is to stay welcome on someone else's server.
    """

    def __init__(
        self,
        requests_per_second: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._min_interval = 1.0 / requests_per_second
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            remaining = self._min_interval - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_call = now


class HansardClient:
    """Typed access to the handful of Hansard endpoints this pipeline needs."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._limiter = RateLimiter(settings.requests_per_second, sleep=sleep)
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )

    def __enter__(self) -> HansardClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def sitting_dates(self, house: str, year: int, month: int) -> list[date]:
        """Sitting days for one calendar month, via ``/overview/calendar.json``.

        Asking the calendar which days sat is far cheaper than probing all 30.
        """
        payload = self._get_json(
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

    def debates_on(self, house: str, on: date) -> list[DebateSummary]:
        """Every debate section recorded for one sitting day.

        Pages through ``/search/debates.json`` until we hold the count the API
        told us to expect, so a busy day is never silently truncated.
        """
        collected: list[DebateSummary] = []
        skip = 0
        expected: int | None = None

        while True:
            payload = self._get_json(
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
                raise HansardApiError(f"Unexpected debate search payload for {on}: {exc}") from exc

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
        payload = self._get_json(f"/debates/debate/{ext_id}.json")
        try:
            return DebateDetail.model_validate(payload)
        except ValidationError as exc:
            raise HansardApiError(f"Unexpected debate payload for {ext_id}: {exc}") from exc

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request_with_retries(path, params)
        try:
            return response.json()
        except ValueError as exc:
            # Hansard answers unknown routes with an HTML error page and a 200,
            # so a JSON decode failure usually means "wrong URL", not "bad data".
            raise HansardApiError(
                f"{path} returned non-JSON content ({response.headers.get('content-type')})"
            ) from exc

    def _request_with_retries(self, path: str, params: dict[str, Any] | None) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(self._settings.max_retries + 1):
            self._limiter.wait()
            try:
                response = self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                last_error = exc
                logger.warning("timeout on %s (attempt %d)", path, attempt + 1)
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning("transport error on %s (attempt %d): %s", path, attempt + 1, exc)
            else:
                if response.status_code == 404:
                    raise HansardNotFoundError(f"{path} returned 404")
                if response.status_code not in RETRYABLE_STATUS:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        # Re-raised as our own type so callers never have to
                        # import httpx to handle a failure. It also means ingest
                        # can skip one bad section instead of aborting the run.
                        raise HansardApiError(f"{path} returned {response.status_code}") from exc
                    return response
                last_error = httpx.HTTPStatusError(
                    f"{path} returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
                logger.warning(
                    "retryable status %d on %s (attempt %d)",
                    response.status_code,
                    path,
                    attempt + 1,
                )

            if attempt < self._settings.max_retries:
                self._sleep(self._backoff_delay(attempt))

        raise HansardApiError(
            f"{path} failed after {self._settings.max_retries + 1} attempts"
        ) from last_error

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, so retries don't resynchronise."""
        # 2.0 rather than 2: int ** int is typed as Any, which would leak out
        # of this function and weaken every caller downstream.
        delay = self._settings.backoff_base_seconds * (2.0**attempt)
        capped = min(delay, self._settings.backoff_max_seconds)
        return capped * (0.5 + random.random() / 2)

    @staticmethod
    def _parse_list(model: type[ModelT], payload: Any, *, context: str) -> list[ModelT]:
        if not isinstance(payload, list):
            raise HansardApiError(
                f"Expected a JSON array for {context}, got {type(payload).__name__}"
            )
        try:
            return [model.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise HansardApiError(f"Unexpected payload for {context}: {exc}") from exc


def months_between(start: date, end: date) -> Iterator[tuple[int, int]]:
    """Yield (year, month) pairs covering the inclusive range."""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
