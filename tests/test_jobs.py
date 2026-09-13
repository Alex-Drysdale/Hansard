"""End-to-end tests for the pipeline jobs.

Everything real except the network: the same clients, the same normalisation, a
real Postgres. These are the tests that would have caught the foreign-key
ordering bug, the double-writing of child debates, and the scheduling order
problem where a member sync running before divisions leaves voters unresolved.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import httpx
import pytest
import respx
from psycopg import Connection
from psycopg.rows import DictRow

from hansard.api.client import ApiError, HansardClient
from hansard.api.members_client import MembersClient
from hansard.config import Settings
from hansard.db import runs
from hansard.pipeline import checks, jobs
from tests.conftest import load_fixture

HANSARD = "https://hansard.test"
MEMBERS = "https://members.test"

PARENT_EXT_ID = "565DB7B1-4CBD-4BD7-86F2-F89DAC86A758"
CHILD_EXT_ID = "A947B3C4-10C7-4848-BC30-2044AD943FB4"

CALENDAR = [
    {"House": "Commons", "ItemDate": "2026-03-03T00:00:00"},
    {"House": "Commons", "ItemDate": "2026-03-04T00:00:00"},
]

# The day index lists a parent section and its child as siblings -- which is
# what Hansard actually does, and the reason ingest must not recurse.
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
    """A stubbed Hansard and Members API.

    Routes are named so a test can re-mock one by name; calling ``router.get()``
    again would register a *second* route while the original kept answering --
    a quiet way to write a test that proves nothing.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{HANSARD}/overview/calendar.json", name="calendar").mock(
            return_value=httpx.Response(200, json=[CALENDAR[0]])
        )
        router.get(f"{HANSARD}/search/debates.json", name="day_index").mock(
            return_value=httpx.Response(200, json=DAY_INDEX)
        )
        router.get(f"{HANSARD}/debates/debate/{PARENT_EXT_ID}.json", name="parent").mock(
            return_value=httpx.Response(200, json=load_fixture("debate_with_child.json"))
        )
        router.get(f"{HANSARD}/debates/debate/{CHILD_EXT_ID}.json", name="child").mock(
            return_value=httpx.Response(200, json=load_fixture("debate_child.json"))
        )
        router.get(url__regex=rf"{HANSARD}/debates/divisions/.*", name="division_list").mock(
            return_value=httpx.Response(200, json=[])
        )
        router.get(url__regex=rf"{HANSARD}/debates/division/.*", name="division_detail").mock(
            return_value=httpx.Response(200, json=load_fixture("division.json"))
        )

        def member_response(request: httpx.Request) -> httpx.Response:
            # Echo the requested id, as the real API does. A stub that always
            # returns the same member would hide the mismatch guard in the job.
            member_id = int(request.url.path.rsplit("/", 1)[-1])
            payload = load_fixture("member.json")
            payload["value"]["id"] = member_id
            return httpx.Response(200, json=payload)

        router.get(url__regex=rf"{MEMBERS}/Members/\d+$", name="member").mock(
            side_effect=member_response
        )
        router.get(f"{MEMBERS}/Members/Search", name="member_search").mock(
            return_value=httpx.Response(200, json={"totalResults": 0, "items": []})
        )
        yield router


@pytest.fixture
def context(clean_connection: Connection[DictRow], settings: Settings, api):
    with (
        HansardClient(settings, sleep=lambda _: None) as hansard,
        MembersClient(settings) as members,
    ):
        yield jobs.JobContext(
            connection=clean_connection, settings=settings, hansard=hansard, members=members
        )


def _mark_debate_as_divided(context: jobs.JobContext, api) -> int:
    """Ingest the fixture day, then make one debate look like it held a division.

    Mirrors reality: the divisions job is driven by Division markers that the
    debate ingest already stored, not by the calendar.
    """
    jobs.ingest_debates(context)
    members_before = count(context.connection, "member")
    context.connection.execute(
        """
        INSERT INTO contribution
            (item_id, debate_ext_id, order_in_section, item_type, content_hash)
        VALUES (999999, %s, 99, 'Division', 'h')
        """,
        (PARENT_EXT_ID.lower(),),
    )
    division = load_fixture("division.json")
    division["DebateSectionExtId"] = PARENT_EXT_ID
    summary = {k: v for k, v in division.items() if not k.endswith("Members")}
    api["division_list"].mock(return_value=httpx.Response(200, json=[summary]))
    api["division_detail"].mock(return_value=httpx.Response(200, json=division))
    return members_before


def count(connection: Connection[DictRow], table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row else 0


class TestIngestDebates:
    def test_stores_the_days_debates(self, context: jobs.JobContext) -> None:
        report = jobs.ingest_debates(context)

        assert report.sitting_days == 1
        assert report.debates_seen == 2
        assert report.debates_inserted == 2
        assert count(context.connection, "debate") == 2

    def test_a_child_section_is_stored_once_not_twice(self, context: jobs.JobContext) -> None:
        # The child arrives inline inside its parent's payload AND as its own
        # entry in the day index. Recursing into ChildDebates would double it.
        jobs.ingest_debates(context)
        row = context.connection.execute(
            "SELECT COUNT(*) AS n FROM debate WHERE ext_id = %s", (CHILD_EXT_ID.lower(),)
        ).fetchone()
        assert row is not None and row["n"] == 1

    def test_parent_child_link_is_recorded(self, context: jobs.JobContext) -> None:
        jobs.ingest_debates(context)
        row = context.connection.execute(
            "SELECT parent_ext_id, parent_title FROM debate WHERE ext_id = %s",
            (CHILD_EXT_ID.lower(),),
        ).fetchone()
        assert row is not None
        assert row["parent_ext_id"] == PARENT_EXT_ID.lower()
        assert row["parent_title"] == "Petition"

    def test_running_twice_changes_nothing(self, context: jobs.JobContext) -> None:
        jobs.ingest_debates(context)
        debates = count(context.connection, "debate")
        contributions = count(context.connection, "contribution")

        second = jobs.ingest_debates(context)

        assert second.debates_inserted == 0
        assert second.debates_updated == 0
        assert second.debates_unchanged == 2
        assert count(context.connection, "debate") == debates
        assert count(context.connection, "contribution") == contributions

    def test_a_repeated_ext_id_in_one_day_is_fetched_once(
        self, context: jobs.JobContext, api
    ) -> None:
        duplicated = {
            "TotalResultCount": 3,
            "Results": [*DAY_INDEX["Results"], DAY_INDEX["Results"][0]],
        }
        api["day_index"].mock(return_value=httpx.Response(200, json=duplicated))

        jobs.ingest_debates(context)

        assert api["parent"].call_count == 1
        assert count(context.connection, "debate") == 2

    def test_resume_skips_a_completed_day(self, context: jobs.JobContext, api) -> None:
        jobs.ingest_debates(context)
        calls_before = api["day_index"].call_count

        second = jobs.ingest_debates(context, resume=True)

        assert second.sitting_days == 0
        assert api["day_index"].call_count == calls_before

    def test_limit_days_caps_the_work(self, context: jobs.JobContext, api) -> None:
        api["calendar"].mock(return_value=httpx.Response(200, json=CALENDAR))
        context = replace(context, settings=replace(context.settings, end_date=date(2026, 3, 4)))

        assert jobs.ingest_debates(context, limit_days=1).sitting_days == 1

    def test_one_bad_section_does_not_lose_the_others(self, context: jobs.JobContext, api) -> None:
        api["child"].mock(return_value=httpx.Response(500))

        report = jobs.ingest_debates(context)

        assert report.errors == 1
        assert report.debates_inserted == 1
        assert count(context.connection, "debate") == 1

    def test_a_day_with_a_failure_is_not_marked_complete(
        self, context: jobs.JobContext, api
    ) -> None:
        # Otherwise resume would skip past a day we know is missing sections.
        api["child"].mock(return_value=httpx.Response(500))
        jobs.ingest_debates(context)

        row = context.connection.execute("SELECT completed_at FROM sitting_day").fetchone()
        assert row is not None and row["completed_at"] is None

    def test_work_done_before_a_fatal_error_is_kept(self, context: jobs.JobContext, api) -> None:
        # One transaction per debate, not one per run: an abort partway through
        # must not roll back hours of successful fetching.
        served = {"count": 0}

        def day_index(request: httpx.Request) -> httpx.Response:
            served["count"] += 1
            if served["count"] == 1:
                return httpx.Response(200, json=DAY_INDEX)
            raise httpx.ConnectError("network gone")

        api["day_index"].mock(side_effect=day_index)
        api["calendar"].mock(return_value=httpx.Response(200, json=CALENDAR))
        context = replace(context, settings=replace(context.settings, end_date=date(2026, 3, 4)))

        with pytest.raises(ApiError):
            jobs.ingest_debates(context)

        assert count(context.connection, "debate") == 2


class TestIngestDivisions:
    def test_only_debates_that_divided_are_queried(self, context: jobs.JobContext, api) -> None:
        # Hansard puts a Division marker in the transcript, so we already know
        # which sections divided and can skip the rest.
        jobs.ingest_debates(context)
        api["division_list"].reset()

        jobs.ingest_divisions(context)

        # Neither fixture debate contains a Division item.
        assert api["division_list"].call_count == 0

    def test_a_division_is_stored_with_its_votes(self, context: jobs.JobContext, api) -> None:
        _mark_debate_as_divided(context, api)

        report = jobs.ingest_divisions(context)

        assert report.notes["divisions"] == 1
        assert count(context.connection, "division") == 1
        assert count(context.connection, "division_vote") > 0

    def test_voters_who_never_spoke_gain_a_member_row(self, context: jobs.JobContext, api) -> None:
        # The scheduling-order lesson in miniature: divisions introduce members
        # that no debate ingest would ever have created.
        before = _mark_debate_as_divided(context, api)

        jobs.ingest_divisions(context)

        assert count(context.connection, "member") > before

    def test_running_divisions_twice_changes_nothing(self, context: jobs.JobContext, api) -> None:
        _mark_debate_as_divided(context, api)
        jobs.ingest_divisions(context)
        votes = count(context.connection, "division_vote")

        second = jobs.ingest_divisions(context)

        assert second.debates_unchanged == 1
        assert count(context.connection, "division_vote") == votes


class TestSyncMembers:
    def test_incremental_sync_fetches_only_unsynced_members(
        self, context: jobs.JobContext, api
    ) -> None:
        jobs.ingest_debates(context)
        pending = count(context.connection, "member")
        assert pending > 0

        report = jobs.sync_members(context)

        assert api["member"].call_count == pending
        assert report.notes["members_seen"] == pending

    def test_a_second_sync_fetches_nothing(self, context: jobs.JobContext, api) -> None:
        jobs.ingest_debates(context)
        jobs.sync_members(context)
        calls = api["member"].call_count

        jobs.sync_members(context)

        assert api["member"].call_count == calls

    def test_full_sync_walks_the_search_endpoint(self, context: jobs.JobContext, api) -> None:
        api["member_search"].mock(
            return_value=httpx.Response(
                200,
                json={
                    "totalResults": 1,
                    "items": [{"value": {"id": 4514, "nameDisplayAs": "Keir Starmer"}}],
                },
            )
        )
        report = jobs.sync_members(context, full=True)
        assert report.notes["members_seen"] == 1
        assert api["member"].call_count == 0

    def test_one_unfetchable_member_does_not_stop_the_rest(
        self, context: jobs.JobContext, api
    ) -> None:
        jobs.ingest_debates(context)
        api["member"].mock(return_value=httpx.Response(500))

        report = jobs.sync_members(context)

        assert report.errors > 0


class TestRunBookkeeping:
    def test_a_completed_job_is_recorded(self, context: jobs.JobContext) -> None:
        jobs.run_job("hansard.debates", jobs.ingest_debates, context)

        row = runs.latest(context.connection, job="hansard.debates")
        assert row is not None
        assert row["status"] == "completed"
        assert row["debates_seen"] == 2
        assert row["finished_at"] is not None

    def test_a_crashing_job_is_recorded_as_failed(self, context: jobs.JobContext, api) -> None:
        # The run row must never be left saying 'running' for ever.
        api["calendar"].mock(return_value=httpx.Response(500))

        with pytest.raises(ApiError):
            jobs.run_job("hansard.debates", jobs.ingest_debates, context)

        row = runs.latest(context.connection, job="hansard.debates")
        assert row is not None
        assert row["status"] == "failed"
        assert row["finished_at"] is not None
        assert "ApiError" in (row["error_message"] or "")

    def test_a_keyboard_interrupt_still_closes_the_run(self, context: jobs.JobContext) -> None:
        def rude(_context: jobs.JobContext) -> jobs.JobReport:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            jobs.run_job("checks", rude, context)

        row = runs.latest(context.connection, job="checks")
        assert row is not None and row["status"] == "failed"

    def test_every_registered_job_is_runnable(self, context: jobs.JobContext) -> None:
        assert set(jobs.JOBS) == {
            "hansard.debates",
            "hansard.divisions",
            "reattribute",
            "members.sync",
            "rag.index",
            "checks",
        }
        # rag.index is excluded: it loads an embedding model and writes a real
        # vector store, which belongs in its own slower test rather than in a
        # loop asserting every job is callable.
        for name, function in jobs.JOBS.items():
            if name == "rag.index":
                continue
            report = jobs.run_job(name, function, context)
            assert report.job == name


class TestChecksJob:
    def test_a_freshly_ingested_store_passes_every_check(self, context: jobs.JobContext) -> None:
        jobs.ingest_debates(context)
        failures = [r for r in checks.run_all(context.connection) if not r.passed]
        assert not failures, [f"{r.name}: {r.detail}" for r in failures]

    def test_check_failures_are_recorded_on_the_run(self, context: jobs.JobContext) -> None:
        jobs.ingest_debates(context)
        # Break something a check looks at.
        context.connection.execute("UPDATE sitting_day SET debate_count = 99")

        report = jobs.run_job("checks", jobs.run_checks, context)

        assert report.errors > 0
        assert report.notes["checks_failed"] > 0
