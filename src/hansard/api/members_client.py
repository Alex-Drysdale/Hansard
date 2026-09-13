"""HTTP client for the Parliament Members API.

Source two. It answers the question Hansard cannot: who a ``MemberId``
actually is. Hansard states a member's party only on their first turn in a
debate and never for a minister speaking by office, so roughly one in seven of
our members had no party at all after Phase 1. This is where that gets fixed --
from Parliament's own system of record, keyed on the same identifier.

Shares :class:`~hansard.api.http.RetryingHttpClient` with the Hansard client:
rate limiting, backoff and error translation are properties of "calling a
public Parliament API", not of either endpoint set.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import ValidationError

from hansard.api.http import ApiError, RetryingHttpClient
from hansard.api.members_models import (
    HOUSE_BY_ID,
    MemberEnvelope,
    MemberSearchResponse,
    MemberValue,
)
from hansard.config import Settings
from hansard.logging_config import get_logger

log = get_logger(__name__)

HOUSE_ID_BY_NAME = {name: house_id for house_id, name in HOUSE_BY_ID.items()}

# The API caps a page at 20 regardless of what `take` asks for, so asking for
# more just produces a confusing mismatch between request and response.
MAX_PAGE_SIZE = 20


class MembersClient:
    """Typed access to the Members API endpoints this pipeline needs."""

    def __init__(self, settings: Settings, *, http: RetryingHttpClient | None = None) -> None:
        self._settings = settings
        self._http = http or RetryingHttpClient(
            base_url=settings.members_base_url,
            settings=settings,
            name="members",
        )

    def __enter__(self) -> MembersClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def member(self, member_id: int) -> MemberValue:
        """One member by their canonical Parliament id."""
        payload = self._http.get_json(f"/Members/{member_id}")
        try:
            return MemberEnvelope.model_validate(payload).value
        except ValidationError as exc:
            raise ApiError(f"Unexpected member payload for {member_id}: {exc}") from exc

    def iter_members(self, house: str, *, current_only: bool = False) -> Iterator[MemberValue]:
        """Every member of a house, walking the paginated search endpoint.

        ``current_only=False`` by default because our transcripts span a period
        during which people left: a member who resigned in February still spoke
        in January, and we need their party to describe that speech.
        """
        house_id = HOUSE_ID_BY_NAME.get(house)
        if house_id is None:
            raise ApiError(f"Unknown house {house!r}")

        page_size = min(self._settings.page_size, MAX_PAGE_SIZE)
        skip = 0
        seen = 0
        total: int | None = None

        while True:
            params: dict[str, object] = {
                "House": house_id,
                "skip": skip,
                "take": page_size,
            }
            if current_only:
                params["IsCurrentMember"] = "true"

            payload = self._http.get_json("/Members/Search", params=params)
            try:
                page = MemberSearchResponse.model_validate(payload)
            except ValidationError as exc:
                raise ApiError(f"Unexpected member search payload: {exc}") from exc

            if total is None:
                total = page.total_results
                log.info("members.search.started", house=house, total=total)

            if not page.items:
                # An empty page ends the walk even if the total said otherwise,
                # so a server that ignores `skip` cannot loop us for ever.
                break

            for item in page.items:
                yield item.value
            seen += len(page.items)

            if seen >= total:
                break
            skip += len(page.items)
