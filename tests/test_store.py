"""Tests for the storage layer, against a real Postgres.

Deliberately not a mock or SQLite. Half of what Phase 2 added lives *in* the
database -- generated columns, enums, foreign keys, UPSERT semantics -- and a
fake would let all of it pass while being wrong.

The property under test throughout is idempotency: writing the same thing twice
must leave the database identical to writing it once.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow

from hansard.api.members_models import MemberValue
from hansard.api.models import DebateDetail, DivisionDetail
from hansard.db import debates as debates_store
from hansard.db import divisions as divisions_store
from hansard.db import members as members_store
from hansard.db import runs
from hansard.db.debates import WriteOutcome
from hansard.pipeline.normalise import normalise_debate, normalise_division, normalise_member


def save(connection: Connection[DictRow], detail: DebateDetail):
    return debates_store.save_debate(connection, normalise_debate(detail))


def count(connection: Connection[DictRow], table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row else 0


class TestSaveDebate:
    def test_first_write_inserts(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        result = save(connection, debate_with_speeches)
        assert result.outcome is WriteOutcome.INSERTED
        assert count(connection, "debate") == 1
        assert count(connection, "contribution") == len(debate_with_speeches.items)

    def test_second_write_of_identical_content_is_a_no_op(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        result = save(connection, debate_with_speeches)

        assert result.outcome is WriteOutcome.UNCHANGED
        assert result.contributions_written == 0
        assert count(connection, "debate") == 1
        assert count(connection, "contribution") == len(debate_with_speeches.items)

    def test_repeated_writes_never_accumulate_rows(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        for _ in range(5):
            save(connection, debate_with_speeches)
        assert count(connection, "debate") == 1
        assert count(connection, "contribution") == len(debate_with_speeches.items)

    def test_changed_content_updates_and_bumps_revision(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        revised = debate_with_speeches.model_copy(
            update={
                "overview": debate_with_speeches.overview.model_copy(
                    update={"title": "Oil Refining Sector (corrected)"}
                )
            }
        )

        result = save(connection, revised)

        assert result.outcome is WriteOutcome.UPDATED
        row = connection.execute("SELECT title, revision FROM debate").fetchone()
        assert row is not None
        assert row["title"] == "Oil Refining Sector (corrected)"
        assert row["revision"] == 2

    def test_first_seen_at_survives_an_update(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        before = connection.execute("SELECT first_seen_at FROM debate").fetchone()
        revised = debate_with_speeches.model_copy(
            update={"overview": debate_with_speeches.overview.model_copy(update={"title": "New"})}
        )
        save(connection, revised)
        after = connection.execute("SELECT first_seen_at FROM debate").fetchone()
        assert before is not None and after is not None
        assert before["first_seen_at"] == after["first_seen_at"]

    def test_a_withdrawn_contribution_is_removed_not_orphaned(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        # Why the transcript is replaced wholesale rather than upserted: an
        # upsert alone would leave a retracted speech in the database for ever.
        save(connection, debate_with_speeches)
        shorter = debate_with_speeches.model_copy(update={"items": debate_with_speeches.items[:-2]})
        save(connection, shorter)
        assert count(connection, "contribution") == len(debate_with_speeches.items) - 2

    def test_parent_and_child_are_separate_rows(
        self,
        connection: Connection[DictRow],
        debate_with_child: DebateDetail,
        debate_child: DebateDetail,
    ) -> None:
        save(connection, debate_with_child)
        save(connection, debate_child)

        assert count(connection, "debate") == 2
        row = connection.execute(
            "SELECT parent_ext_id FROM debate WHERE ext_id = %s",
            (normalise_debate(debate_child).debate.ext_id,),
        ).fetchone()
        assert row is not None
        assert row["parent_ext_id"] == normalise_debate(debate_with_child).debate.ext_id


class TestGeneratedSearchColumn:
    def test_speeches_become_searchable_without_any_index_maintenance(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        # No application code touches body_tsv; Postgres generates it.
        save(connection, debate_with_speeches)
        row = connection.execute(
            """
            SELECT COUNT(*) AS n FROM contribution
             WHERE is_speech AND body_tsv @@ websearch_to_tsquery('english', 'refinery')
            """
        ).fetchone()
        assert row is not None and row["n"] > 0

    def test_the_index_cannot_drift_from_the_text(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        # The Phase 1 hazard this design removes: there is no way to update the
        # text without the vector following, because it is not separately stored.
        save(connection, debate_with_speeches)
        item = connection.execute(
            "SELECT item_id FROM contribution WHERE is_speech LIMIT 1"
        ).fetchone()
        assert item is not None
        connection.execute(
            "UPDATE contribution SET body_text = 'zebras orbit quietly' WHERE item_id = %s",
            (item["item_id"],),
        )
        found = connection.execute(
            """
            SELECT COUNT(*) AS n FROM contribution
             WHERE item_id = %s AND body_tsv @@ websearch_to_tsquery('english', 'zebras')
            """,
            (item["item_id"],),
        ).fetchone()
        assert found is not None and found["n"] == 1

    def test_the_generated_column_cannot_be_written_directly(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        with pytest.raises(psycopg.errors.GeneratedAlways):
            connection.execute("UPDATE contribution SET body_tsv = ''::tsvector")


class TestMembersFromTranscript:
    def test_members_are_derived_from_attributions(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        row = connection.execute(
            """
            SELECT display_name_hansard, party_hansard, constituency_hansard
              FROM member WHERE member_id = 3957
            """
        ).fetchone()
        assert row is not None
        assert row["display_name_hansard"] == "Martin Vickers"
        assert row["party_hansard"] == "Con"
        assert row["constituency_hansard"] == "Brigg and Immingham"

    def test_a_later_bare_mention_does_not_erase_the_party(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        # Hansard states party only on a member's first turn in a debate.
        save(connection, debate_with_speeches)
        bare = debate_with_speeches.model_copy(
            update={
                "overview": debate_with_speeches.overview.model_copy(
                    update={"ext_id": "11111111-1111-1111-1111-111111111111", "hansard_id": 999}
                ),
                "items": [
                    item.model_copy(
                        update={"attributed_to": "Martin Vickers", "item_id": 900 + index}
                    )
                    for index, item in enumerate(debate_with_speeches.items)
                ],
            }
        )
        save(connection, bare)

        assert count(connection, "debate") == 2
        row = connection.execute(
            "SELECT party_hansard, constituency_hansard FROM member WHERE member_id = 3957"
        ).fetchone()
        assert row is not None
        assert row["party_hansard"] == "Con"
        assert row["constituency_hansard"] == "Brigg and Immingham"

    def test_counts_are_recomputed_not_incremented(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        debates_store.refresh_member_counts(connection)
        first = connection.execute(
            "SELECT contribution_count FROM member WHERE member_id = 3957"
        ).fetchone()

        save(connection, debate_with_speeches)
        debates_store.refresh_member_counts(connection)
        second = connection.execute(
            "SELECT contribution_count FROM member WHERE member_id = 3957"
        ).fetchone()

        assert first is not None and second is not None
        assert first["contribution_count"] == second["contribution_count"] > 0


class TestMembersFromApi:
    def test_source_two_fills_its_own_columns(
        self, connection: Connection[DictRow], member_value: MemberValue
    ) -> None:
        members_store.save_members(connection, [normalise_member(member_value)])
        row = connection.execute(
            "SELECT display_name, party, constituency, synced_at FROM member WHERE member_id = 467"
        ).fetchone()
        assert row is not None
        assert row["display_name"] == "Sir Lindsay Hoyle"
        assert row["party"] == "Speaker"
        assert row["constituency"] == "Chorley"
        assert row["synced_at"] is not None

    def test_source_two_does_not_overwrite_source_one(
        self, connection: Connection[DictRow], member_value: MemberValue
    ) -> None:
        # The two sources occupy different columns on purpose, so a disagreement
        # stays visible rather than being resolved by whichever ran last.
        connection.execute(
            """
            INSERT INTO member (member_id, display_name_hansard, party_hansard)
            VALUES (467, 'Mr Speaker', NULL)
            """
        )

        members_store.save_members(connection, [normalise_member(member_value)])

        row = connection.execute(
            """
            SELECT display_name_hansard, display_name, party_hansard, party
              FROM member WHERE member_id = 467
            """
        ).fetchone()
        assert row is not None
        # What Hansard called them survives untouched...
        assert row["display_name_hansard"] == "Mr Speaker"
        assert row["party_hansard"] is None
        # ...alongside what the Members API says they are.
        assert row["display_name"] == "Sir Lindsay Hoyle"
        assert row["party"] == "Speaker"

    def test_the_resolved_view_prefers_source_two(
        self, connection: Connection[DictRow], member_value: MemberValue
    ) -> None:
        connection.execute(
            "INSERT INTO member (member_id, display_name_hansard) VALUES (467, 'Mr Speaker')"
        )
        members_store.save_members(connection, [normalise_member(member_value)])

        row = connection.execute(
            "SELECT name, party, from_members_api FROM member_resolved WHERE member_id = 467"
        ).fetchone()
        assert row is not None
        assert row["name"] == "Sir Lindsay Hoyle"
        assert row["party"] == "Speaker"
        assert row["from_members_api"] is True

    def test_resolved_view_prefers_the_api_and_falls_back(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        row = connection.execute(
            "SELECT name, party, from_members_api FROM member_resolved WHERE member_id = 3957"
        ).fetchone()
        assert row is not None
        assert row["party"] == "Con"  # from the transcript
        assert row["from_members_api"] is False

    def test_insert_and_update_are_reported_separately(
        self, connection: Connection[DictRow], member_value: MemberValue
    ) -> None:
        row = normalise_member(member_value)
        first = members_store.save_members(connection, [row])
        second = members_store.save_members(connection, [row])
        assert (first.inserted, first.updated) == (1, 0)
        assert (second.inserted, second.updated) == (0, 1)

    def test_unsynced_ids_are_what_the_incremental_job_walks(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        pending = members_store.unsynced_member_ids(connection)
        assert 3957 in pending

        members_store.save_members(
            connection,
            [
                normalise_member(
                    MemberValue.model_validate({"id": 3957, "nameDisplayAs": "Martin Vickers"})
                )
            ],
        )
        assert 3957 not in members_store.unsynced_member_ids(connection)


class TestDivisions:
    """The divisions job only asks about debates it already stored, so these
    mirror that: the debate exists, then its division arrives."""

    @pytest.fixture(autouse=True)
    def _debate_exists(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        """Create the debate the fixture division belongs to."""
        connection.execute(
            """
            INSERT INTO debate (ext_id, hansard_id, title, house, sitting_date, content_hash)
            VALUES (%s, 1, 'Sentencing Bill', 'Commons', %s, 'h')
            ON CONFLICT DO NOTHING
            """,
            (
                normalise_division(division_detail).division.debate_ext_id,
                division_detail.division_date.date(),
            ),
        )

    def test_first_write_stores_votes(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        normalised = normalise_division(division_detail)
        result = divisions_store.save_division(connection, normalised)

        assert result.outcome is WriteOutcome.INSERTED
        assert count(connection, "division") == 1
        assert count(connection, "division_vote") == len(normalised.votes)

    def test_writing_twice_is_a_no_op(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        normalised = normalise_division(division_detail)
        divisions_store.save_division(connection, normalised)
        votes = count(connection, "division_vote")

        result = divisions_store.save_division(connection, normalised)

        assert result.outcome is WriteOutcome.UNCHANGED
        assert count(connection, "division_vote") == votes

    def test_voters_who_never_spoke_get_a_member_row(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        # A backbencher can go a month without speaking and still vote thirty
        # times; without this the foreign key would reject their votes.
        assert count(connection, "member") == 0
        normalised = normalise_division(division_detail)
        divisions_store.save_division(connection, normalised)
        assert count(connection, "member") == len({v.member_id for v in normalised.votes})

    def test_tellers_are_recorded_as_such(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        divisions_store.save_division(connection, normalise_division(division_detail))
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM division_vote WHERE is_teller"
        ).fetchone()
        assert row is not None and row["n"] == 4

    def test_stated_counts_are_stored_not_derived(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        # The fixture is trimmed, so stated counts deliberately exceed the
        # stored votes. Storing both is the point: they disagree upstream too.
        divisions_store.save_division(connection, normalise_division(division_detail))
        row = connection.execute("SELECT ayes_count, noes_count FROM division").fetchone()
        assert row is not None
        assert row["ayes_count"] == division_detail.ayes_count
        assert row["noes_count"] == division_detail.noes_count

    def test_discrepancies_are_reported(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        divisions_store.save_division(connection, normalise_division(division_detail))
        assert divisions_store.count_discrepancies(connection)

    def test_a_lobby_outside_the_enum_is_rejected(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        divisions_store.save_division(connection, normalise_division(division_detail))
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            connection.execute("UPDATE division_vote SET lobby = 'abstain'")

    def test_a_division_survives_its_debate_being_outside_the_window(
        self, connection: Connection[DictRow], division_detail: DivisionDetail
    ) -> None:
        # Losing a whole division, and every vote in it, because one link cannot
        # resolve would be the wrong trade. The link is dropped, not the data.
        connection.execute("DELETE FROM debate")

        result = divisions_store.save_division(connection, normalise_division(division_detail))

        assert result.outcome is WriteOutcome.INSERTED
        row = connection.execute("SELECT debate_ext_id FROM division").fetchone()
        assert row is not None and row["debate_ext_id"] is None
        assert count(connection, "division_vote") > 0


class TestSchemaConstraints:
    def test_foreign_keys_are_enforced(self, connection: Connection[DictRow]) -> None:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO contribution
                    (item_id, debate_ext_id, order_in_section, item_type, content_hash)
                VALUES (1, 'no-such-debate', 0, 'Contribution', 'h')
                """
            )

    def test_house_is_constrained_by_the_enum(self, connection: Connection[DictRow]) -> None:
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            connection.execute(
                "INSERT INTO sitting_day (house, sitting_date) VALUES ('Senate', '2026-01-14')"
            )

    def test_deleting_a_debate_takes_its_contributions(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        save(connection, debate_with_speeches)
        connection.execute("DELETE FROM debate")
        assert count(connection, "contribution") == 0

    def test_non_uuid_identifiers_are_accepted(
        self, connection: Connection[DictRow], debate_with_speeches: DebateDetail
    ) -> None:
        # 13 of 1,962 real sections use an id like "DeferredDivisions2026-01-14"
        # or "26011562000145"; a UUID column would reject every one of them.
        synthetic = debate_with_speeches.model_copy(
            update={
                "overview": debate_with_speeches.overview.model_copy(
                    update={"ext_id": "DeferredDivisions2026-01-14"}
                )
            }
        )
        save(connection, synthetic)
        row = connection.execute("SELECT ext_id FROM debate").fetchone()
        assert row is not None
        assert row["ext_id"] == "DeferredDivisions2026-01-14"


class TestSittingDays:
    def test_recording_a_day_twice_keeps_one_row(self, connection: Connection[DictRow]) -> None:
        for _ in range(3):
            debates_store.record_sitting_day(
                connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=25
            )
        assert count(connection, "sitting_day") == 1

    def test_only_completed_days_are_reported_as_done(
        self, connection: Connection[DictRow]
    ) -> None:
        debates_store.record_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=25
        )
        debates_store.record_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 15), debate_count=42
        )
        debates_store.complete_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 14)
        )
        assert debates_store.completed_sitting_days(connection, "Commons") == {date(2026, 1, 14)}

    def test_completion_is_scoped_to_a_house(self, connection: Connection[DictRow]) -> None:
        debates_store.record_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=1
        )
        debates_store.complete_sitting_day(
            connection, house="Commons", sitting_date=date(2026, 1, 14)
        )
        assert debates_store.completed_sitting_days(connection, "Lords") == set()


class TestRuns:
    def test_a_run_is_recorded_and_closed(self, connection: Connection[DictRow]) -> None:
        handle = runs.start(
            connection,
            job="hansard.debates",
            house="Commons",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 4, 30),
        )
        opened = runs.latest(connection)
        assert opened is not None and opened["status"] == "running"

        runs.finish(connection, handle, status="completed", counters={"debates_seen": 12})

        row = runs.latest(connection)
        assert row is not None
        assert row["status"] == "completed"
        assert row["debates_seen"] == 12
        assert row["finished_at"] is not None

    def test_runs_are_scoped_by_job(self, connection: Connection[DictRow]) -> None:
        runs.finish(
            connection,
            runs.start(connection, job="members.sync"),
            status="completed",
            counters={},
        )
        runs.start(connection, job="hansard.debates")

        members_run = runs.latest(connection, job="members.sync")
        assert members_run is not None and members_run["status"] == "completed"

    def test_an_unknown_counter_is_rejected(self, connection: Connection[DictRow]) -> None:
        # Counters name real columns, so a typo must fail loudly rather than
        # being interpolated into SQL.
        handle = runs.start(connection, job="checks")
        with pytest.raises(ValueError, match="Unknown run counter"):
            runs.finish(connection, handle, status="completed", counters={"bogus": 1})

    def test_status_is_constrained_by_the_enum(self, connection: Connection[DictRow]) -> None:
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            connection.execute("INSERT INTO ingest_run (job, status) VALUES ('x', 'sideways')")

    def test_last_success_ignores_failures(self, connection: Connection[DictRow]) -> None:
        failed = runs.start(connection, job="hansard.debates")
        runs.finish(connection, failed, status="failed", counters={})
        assert runs.last_success_at(connection, "hansard.debates") is None

        ok = runs.start(connection, job="hansard.debates")
        runs.finish(connection, ok, status="completed", counters={})
        assert runs.last_success_at(connection, "hansard.debates") is not None
