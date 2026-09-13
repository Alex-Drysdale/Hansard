"""Tests for the scheduler.

Scheduling is easy to get subtly wrong and painful to test by waiting, so these
assert on the trigger's own arithmetic rather than on elapsed time: given a
cron expression and a moment, when does the next run land? No sleeping, no
flakiness, and a wrong cron expression fails immediately.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from hansard.config import DEFAULT_SCHEDULE, Settings
from hansard.scheduler import (
    MISFIRE_GRACE_SECONDS,
    SCHEDULE_TIMEZONE,
    ScheduledJob,
    build_jobs,
    run_once_now,
    translate_day_of_week,
)

UTC = ZoneInfo("UTC")


class TestDefaultSchedule:
    def test_every_scheduled_name_is_a_real_job(self) -> None:
        from hansard.pipeline.jobs import JOBS

        assert set(DEFAULT_SCHEDULE) <= set(JOBS)

    def test_every_cron_expression_parses(self) -> None:
        for name, cron in DEFAULT_SCHEDULE.items():
            job = ScheduledJob(name=name, cron=cron, run=lambda: None)
            assert job.next_run() is not None

    def test_jobs_run_in_dependency_order(self) -> None:
        # Load-bearing, not cosmetic: debates and divisions both introduce
        # member ids, and divisions introduce more of them, so the member sync
        # has to come after both or every new voter waits a day for a party.
        after = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
        order = sorted(
            DEFAULT_SCHEDULE,
            key=lambda name: ScheduledJob(name, DEFAULT_SCHEDULE[name], lambda: None).next_run(
                after
            ),
        )
        assert order == [
            "hansard.debates",
            # Resolves continuation paragraphs from what the ingest just stored.
            "reattribute",
            "hansard.divisions",
            "members.sync",
            # Indexes only once everything it indexes is final.
            "rag.index",
            "checks",
        ]

    def test_checks_run_last(self) -> None:
        after = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
        latest = max(
            DEFAULT_SCHEDULE,
            key=lambda name: ScheduledJob(name, DEFAULT_SCHEDULE[name], lambda: None).next_run(
                after
            ),
        )
        assert latest == "checks"


class TestNextRun:
    def test_a_daily_job_lands_on_the_stated_hour(self) -> None:
        job = ScheduledJob("x", "0 6 * * *", lambda: None)
        following = job.next_run(datetime(2026, 3, 2, 5, 0, tzinfo=UTC))
        assert following is not None
        assert (following.hour, following.minute) == (6, 0)

    def test_a_time_already_past_rolls_to_tomorrow(self) -> None:
        job = ScheduledJob("x", "0 6 * * *", lambda: None)
        following = job.next_run(datetime(2026, 3, 2, 7, 0, tzinfo=UTC))
        assert following is not None
        assert following.day == 3

    def test_a_weekly_job_lands_on_its_weekday(self) -> None:
        # Standard cron: 1 is Monday. APScheduler numbers Monday 0, so passing
        # this through untranslated would quietly schedule it for Tuesday.
        job = ScheduledJob("x", "0 5 * * 1", lambda: None)
        following = job.next_run(datetime(2026, 3, 4, 0, 0, tzinfo=UTC))  # a Wednesday
        assert following is not None
        assert following.weekday() == 0
        assert following.date() == date(2026, 3, 9)

    def test_cron_sunday_is_sunday(self) -> None:
        job = ScheduledJob("x", "0 5 * * 0", lambda: None)
        following = job.next_run(datetime(2026, 3, 4, 0, 0, tzinfo=UTC))
        assert following is not None and following.weekday() == 6

    def test_day_names_work_too(self) -> None:
        job = ScheduledJob("x", "0 5 * * mon", lambda: None)
        following = job.next_run(datetime(2026, 3, 4, 0, 0, tzinfo=UTC))
        assert following is not None and following.weekday() == 0

    def test_triggers_are_built_in_the_declared_timezone(self) -> None:
        # Without an explicit timezone APScheduler uses the machine's, and the
        # trigger's zone beats the scheduler's -- so a UTC scheduler would fire
        # on local time for half the year while reporting UTC.
        job = ScheduledJob("x", "0 6 * * *", lambda: None)
        following = job.next_run(datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
        assert following is not None
        assert str(following.tzinfo) == SCHEDULE_TIMEZONE


class TestDayOfWeekTranslation:
    @pytest.mark.parametrize(
        ("cron_field", "expected"),
        [("0", "6"), ("1", "0"), ("5", "4"), ("6", "5"), ("7", "6")],
    )
    def test_numbers_are_remapped(self, cron_field: str, expected: str) -> None:
        assert translate_day_of_week(cron_field) == expected

    def test_wildcards_pass_through(self) -> None:
        assert translate_day_of_week("*") == "*"

    def test_names_pass_through(self) -> None:
        assert translate_day_of_week("mon,fri") == "mon,fri"

    def test_lists_are_translated_elementwise(self) -> None:
        assert translate_day_of_week("1,3,5") == "0,2,4"

    def test_a_weekday_range_is_translated(self) -> None:
        assert translate_day_of_week("1-5") == "0-4"  # Mon-Fri

    def test_a_wrapping_range_is_rejected_rather_than_guessed(self) -> None:
        # Sun-Tue would remap to 6-1, which reads as an inverted range.
        with pytest.raises(ValueError, match="wraps the week"):
            translate_day_of_week("0-2")


class TestCronParsing:
    def test_a_wrong_field_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Expected 5 cron fields"):
            ScheduledJob("x", "0 6 * *", lambda: None).trigger()


class TestBuildJobs:
    def test_each_job_calls_the_runner_with_its_own_name(self) -> None:
        # The late-binding closure trap: without the default-argument capture in
        # build_jobs, every job would run whichever name happened to be last.
        called: list[str] = []
        built = build_jobs(Settings(), called.append)

        for job in built:
            job.run()

        assert called == list(DEFAULT_SCHEDULE)

    def test_a_custom_schedule_is_honoured(self) -> None:
        built = build_jobs(Settings(), lambda _: None, {"checks": "0 0 * * *"})
        assert [job.name for job in built] == ["checks"]
        assert built[0].cron == "0 0 * * *"

    def test_an_invalid_cron_expression_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_jobs(Settings(), lambda _: None, {"checks": "not a cron"})[0].trigger()


class TestMisfirePolicy:
    def test_the_grace_period_covers_an_overnight_sleep(self) -> None:
        # Long enough that a laptop closed at 06:00 still runs the job when it
        # wakes; short enough that a machine off for a week does not run
        # yesterday's job as though it were today's.
        assert 3 * 3600 <= MISFIRE_GRACE_SECONDS <= 12 * 3600


class TestRunOnceNow:
    def test_runs_every_job_and_returns(self) -> None:
        called: list[str] = []
        jobs = [
            ScheduledJob("a", "0 6 * * *", lambda: called.append("a")),
            ScheduledJob("b", "0 7 * * *", lambda: called.append("b")),
        ]

        run_once_now(jobs, timeout=10)

        assert sorted(called) == ["a", "b"]

    def test_a_failing_job_does_not_prevent_the_others(self) -> None:
        called: list[str] = []

        def explode() -> None:
            raise RuntimeError("boom")

        jobs = [
            ScheduledJob("bad", "0 6 * * *", explode),
            ScheduledJob("good", "0 7 * * *", lambda: called.append("good")),
        ]

        run_once_now(jobs, timeout=10)

        assert called == ["good"]
