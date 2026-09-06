"""End-to-end tests for the pipeline, with HTTP mocked at the transport layer.

Everything real except the network: the same client, the same normalisation, a
real SQLite database. These are the tests that would have caught the foreign-key
ordering bug and the double-writing of child debates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date

import httpx
import pytest
import respx

from hansard.api.client import HansardApiError, HansardClient
from hansard.config import Settings
from hansard.db import store
from hansard.pipeline import checks
from hansard.pipeline.ingest import ingest_range
from tests.conftest import load_fixture

BASE = "https://hansard.test"

PARENT_EXT_ID = "565DB7B1-4CBD-4BD7-86F2-F89DAC86A758"
CHILD_EXT_ID = "A947B3C4-10C7-4848-BC30-2044AD943FB4"

CALENDAR = [
    {"House": "Commons", "ItemDate": "2026-03-03T00:00:00"},
    {"House": "Commons", "ItemDate": "2026-03-04T00:00:00"},
]

# The day index deliberately lists a parent section and its child as siblings --
# which is exactly what Hansard does, and the reason ingest must not recurse.
DAY_INDEX = {
    "TotalResultCount": 2,
    "Results": [
        {
            "DebateSectionExtId": PARENT_EXT_ID,
            "Title": "Petition",
            "House": "Commons",
            "SittingDate": "2026-03-03T00:00:00",
        },
        {
            "DebateSectionExtId": CHILD_EXT_ID,
            "Title": "A5036 Park Lane footbridge",
            "House": "Commons",
            "SittingDate": "2026-03-03T00:00:00",
        },
    ],
}


@pytest.fixture
def api():
    """A stubbed Hansard serving one sitting day of two linked sections.

    Routes are named so a test can re-mock one by name. Calling ``router.get()``
    again would register a *second* route instead, and the original would keep
    answering -- a quiet way to write a test that proves nothing.
    """
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/overview/calendar.json", name="calendar").mock(
            return_value=httpx.Response(200, json=[CALENDAR[0]])
        )
        router.get("/search/debates.json", name="day_index").mock(
            return_value=httpx.Response(200, json=DAY_INDEX)
        )
        router.get(f"/debates/debate/{PARENT_EXT_ID}.json", name="parent").mock(
            return_value=httpx.Response(200, json=load_fixture("debate_with_child.json"))
        )
        router.get(f"/debates/debate/{CHILD_EXT_ID}.json", name="child").mock(
            return_value=httpx.Response(200, json=load_fixture("debate_child.json"))
        )
        yield router


@pytest.fixture
def client(settings: Settings):
    with HansardClient(settings, sleep=lambda _: None) as instance:
        yield instance


def run(connection: sqlite3.Connection, client: HansardClient, settings: Settings, **kwargs):
    return ingest_range(connection, client, settings, **kwargs)


def count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


class TestIngestRange:
    def test_stores_the_days_debates(self, connection, client, settings, api) -> None:
        report = run(connection, client, settings)

        assert report.sitting_days == 1
        assert report.debates_seen == 2
        assert report.debates_inserted == 2
        assert count(connection, "debate") == 2

    def test_a_child_section_is_stored_once_not_twice(
        self, connection, client, settings, api
    ) -> None:
        # The child arrives inline inside its parent's payload AND as its own
        # entry in the day index. Recursing into ChildDebates would double it.
        run(connection, client, settings)

        row = connection.execute(
            "SELECT COUNT(*) AS n FROM debate WHERE ext_id = ?", (CHILD_EXT_ID,)
        ).fetchone()
        assert row["n"] == 1

    def test_parent_child_link_is_recorded(self, connection, client, settings, api) -> None:
        run(connection, client, settings)

        row = connection.execute(
            "SELECT parent_ext_id, parent_title FROM debate WHERE ext_id = ?", (CHILD_EXT_ID,)
        ).fetchone()
        assert row["parent_ext_id"] == PARENT_EXT_ID
        assert row["parent_title"] == "Petition"

    def test_running_twice_changes_nothing(self, connection, client, settings, api) -> None:
        run(connection, client, settings)
        debates, contributions = count(connection, "debate"), count(connection, "contribution")

        second = run(connection, client, settings)

        assert second.debates_inserted == 0
        assert second.debates_updated == 0
        assert second.debates_unchanged == 2
        assert count(connection, "debate") == debates
        assert count(connection, "contribution") == contributions

    def test_a_repeated_ext_id_in_one_day_is_fetched_once(
        self, connection, client, settings, api
    ) -> None:
        # First line of dedup defence: a duplicate in the index costs an HTTP
        # request if it is not caught before the fetch.
        duplicated = {
            "TotalResultCount": 3,
            "Results": [*DAY_INDEX["Results"], DAY_INDEX["Results"][0]],
        }
        api["day_index"].mock(return_value=httpx.Response(200, json=duplicated))

        run(connection, client, settings)

        assert api["parent"].call_count == 1
        assert count(connection, "debate") == 2

    def test_the_day_is_marked_complete(self, connection, client, settings, api) -> None:
        run(connection, client, settings)
        assert store.completed_sitting_days(connection, "Commons") == {date(2026, 3, 3)}

    def test_resume_skips_a_completed_day(self, connection, client, settings, api) -> None:
        run(connection, client, settings)
        calls_before = api["day_index"].call_count

        second = run(connection, client, settings, resume=True)

        assert second.sitting_days == 0
        assert api["day_index"].call_count == calls_before

    def test_limit_days_caps_the_work(self, connection, client, settings, api) -> None:
        api["calendar"].mock(return_value=httpx.Response(200, json=CALENDAR))
        two_days = replace(settings, end_date=date(2026, 3, 4))

        report = run(connection, client, two_days, limit_days=1)

        assert report.sitting_days == 1

    def test_the_run_is_recorded(self, connection, client, settings, api) -> None:
        run(connection, client, settings)

        row = store.latest_run(connection)
        assert row["status"] == "completed"
        assert row["house"] == "Commons"
        assert row["debates_seen"] == 2
        assert row["finished_at"] is not None

    def test_member_counts_are_populated(self, connection, client, settings, api) -> None:
        run(connection, client, settings)

        total = connection.execute(
            "SELECT COALESCE(SUM(contribution_count), 0) AS n FROM member"
        ).fetchone()["n"]
        assert total > 0

    def test_a_freshly_ingested_store_passes_every_check(
        self, connection, client, settings, api
    ) -> None:
        run(connection, client, settings)

        failures = [result for result in checks.run_all(connection) if not result.passed]
        assert not failures, [f"{r.name}: {r.detail}" for r in failures]


class TestFailureHandling:
    def test_one_bad_section_does_not_lose_the_others(
        self, connection, client, settings, api
    ) -> None:
        api["child"].mock(return_value=httpx.Response(500))

        report = run(connection, client, settings)

        assert report.errors == 1
        assert report.debates_inserted == 1
        assert count(connection, "debate") == 1

    def test_a_day_with_a_failure_is_not_marked_complete(
        self, connection, client, settings, api
    ) -> None:
        # Otherwise --resume would skip past a day we know is missing sections.
        api["child"].mock(return_value=httpx.Response(500))

        run(connection, client, settings)

        assert store.completed_sitting_days(connection, "Commons") == set()

    def test_the_failure_is_reported_on_the_run(self, connection, client, settings, api) -> None:
        api["child"].mock(return_value=httpx.Response(500))

        run(connection, client, settings)

        assert store.latest_run(connection)["errors"] == 1

    def test_work_done_before_a_fatal_error_is_kept(
        self, connection, client, settings, api
    ) -> None:
        # One transaction per debate, not one per run: an abort partway through
        # must not roll back hours of successful fetching.
        # A callable rather than a list: the client retries a transport error,
        # so the second day must keep failing for every attempt it makes.
        served = {"count": 0}

        def day_index(request: httpx.Request) -> httpx.Response:
            served["count"] += 1
            if served["count"] == 1:
                return httpx.Response(200, json=DAY_INDEX)
            raise httpx.ConnectError("network gone")

        api["day_index"].mock(side_effect=day_index)
        api["calendar"].mock(return_value=httpx.Response(200, json=CALENDAR))
        two_days = replace(settings, end_date=date(2026, 3, 4))

        with pytest.raises(HansardApiError):
            run(connection, client, two_days)

        assert count(connection, "debate") == 2
        assert store.latest_run(connection)["status"] == "failed"
