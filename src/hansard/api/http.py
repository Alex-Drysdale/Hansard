"""Shared HTTP behaviour for the Parliament APIs.

Phase 1 had one client, so rate limiting and retries lived inside it. With a
second source that would mean two copies of the same careful logic, drifting
apart -- so the behaviour that belongs to "calling a public Parliament API"
lives here, and each client is left holding only its own endpoints.

What this owns: politeness, retrying the failures worth retrying, and making
sure callers never see an ``httpx`` exception.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from hansard.logging_config import get_logger

if TYPE_CHECKING:
    from hansard.config import Settings

log = get_logger(__name__)

# 429 means we were impolite; 5xx means upstream stumbled. Both are worth
# another go. Everything else (400, 404) is our fault and retrying won't help.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """A request failed in a way we could not recover from."""


class NotFoundError(ApiError):
    """The API returned 404 for a resource we expected to exist."""


class RateLimiter:
    """Blocking limiter that spaces requests at least ``1/rate`` seconds apart.

    Deliberately simple: one process, one thread, no burst allowance.
    Predictable beats clever when the goal is to stay welcome on someone else's
    server.
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


class RetryingHttpClient:
    """A JSON HTTP client that is polite, patient and honest about failure."""

    def __init__(
        self,
        *,
        base_url: str,
        settings: Settings,
        name: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._name = name
        self._settings = settings
        self._sleep = sleep
        self._limiter = RateLimiter(settings.requests_per_second, sleep=sleep)
        self._client = httpx.Client(
            base_url=base_url,
            timeout=settings.timeout_seconds,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request_with_retries(path, params)
        try:
            return response.json()
        except ValueError as exc:
            # Both APIs answer an unknown route with an HTML error page and a
            # 200, so a decode failure usually means "wrong URL", not "bad data".
            raise ApiError(
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
                log.warning("http.timeout", api=self._name, path=path, attempt=attempt + 1)
            except httpx.TransportError as exc:
                last_error = exc
                log.warning(
                    "http.transport_error",
                    api=self._name,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )
            else:
                if response.status_code == 404:
                    raise NotFoundError(f"{path} returned 404")
                if response.status_code not in RETRYABLE_STATUS:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        # Re-raised as our own type so callers never have to
                        # import httpx to handle a failure, and so ingest can
                        # skip one bad item rather than abort the whole run.
                        raise ApiError(f"{path} returned {response.status_code}") from exc
                    return response

                last_error = httpx.HTTPStatusError(
                    f"{path} returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
                log.warning(
                    "http.retryable_status",
                    api=self._name,
                    path=path,
                    status=response.status_code,
                    attempt=attempt + 1,
                )

            if attempt < self._settings.max_retries:
                self._sleep(self._backoff_delay(attempt))

        raise ApiError(
            f"{path} failed after {self._settings.max_retries + 1} attempts"
        ) from last_error

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, so retries don't resynchronise."""
        # 2.0 rather than 2: int ** int is typed as Any, which would leak out.
        delay = self._settings.backoff_base_seconds * (2.0**attempt)
        capped = min(delay, self._settings.backoff_max_seconds)
        return capped * (0.5 + random.random() / 2)
