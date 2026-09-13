"""Tests for the web layer.

Thin, like the CLI: these check the wiring and the failure paths, not the
retrieval logic, which is tested directly in test_retrieve.py.

The failure paths matter more than usual here. A page that silently shows an
empty answer when Postgres is down, or when nobody has built an index, is worse
than one that says so -- the user cannot tell "nothing was said about that" from
"nothing is running".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import DictRow

from hansard.config import Settings
from hansard.db import debates as debates_store
from hansard.rag import answer as rag_answer
from hansard.rag import retrieve as rag_retrieve
from hansard.web.app import create_app


@pytest.fixture
def web_settings(settings: Settings, database_url: str, tmp_path) -> Settings:
    """Settings pointed at the test database and an empty vector directory."""
    return replace(settings, database_url=database_url, vector_path=tmp_path / "vectors")


@pytest.fixture
def client(web_settings: Settings, clean_connection: Connection[DictRow]) -> Iterator[TestClient]:
    with TestClient(create_app(web_settings)) as test_client:
        yield test_client


@pytest.fixture
def populated(clean_connection: Connection[DictRow], debate_with_speeches) -> None:
    debates_store.record_sitting_day(
        clean_connection, house="Commons", sitting_date=date(2026, 1, 14), debate_count=1
    )
    from hansard.pipeline.normalise import normalise_debate

    debates_store.save_debate(clean_connection, normalise_debate(debate_with_speeches))
    debates_store.refresh_member_counts(clean_connection)


class TestPage:
    def test_the_page_loads(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Hansard" in response.text

    def test_the_page_needs_no_build_step(self, client: TestClient) -> None:
        # One file, no bundler: the point is to see the data, not to run a
        # frontend toolchain.
        assert "<script>" in client.get("/").text


class TestStatus:
    def test_reports_what_the_store_holds(self, client: TestClient, populated: None) -> None:
        body = client.get("/api/status").json()
        assert body["debates"] == 1
        assert body["speeches"] > 0
        assert body["earliest"] == "2026-01-14"

    def test_says_when_there_is_no_index(self, client: TestClient, populated: None) -> None:
        # So the page can tell the user to run `hansard index` rather than
        # returning nothing and looking broken.
        assert client.get("/api/status").json()["index"]["exists"] is False

    def test_reports_whether_answering_is_possible(self, client: TestClient) -> None:
        assert client.get("/api/status").json()["can_answer"] is False


class TestAsk:
    def test_retrieval_works_without_a_vector_index(
        self, client: TestClient, populated: None
    ) -> None:
        # Keyword search is half the hybrid, and it needs no embeddings at all.
        body = client.post(
            "/api/ask", json={"question": "oil refinery closure", "answer": False}
        ).json()
        assert body["passages"], "keyword search should still find something"
        assert all("keyword" in p["sources"] for p in body["passages"])

    def test_a_question_with_no_matches_says_so(self, client: TestClient, populated: None) -> None:
        body = client.post("/api/ask", json={"question": "zzzznotaword", "answer": False}).json()
        assert body["passages"] == []
        assert "Nothing in the stored debates" in body["answer"]

    def test_filters_are_reported_back(self, client: TestClient, populated: None) -> None:
        # The UI shows how the question was interpreted, because a silently
        # applied filter is how you get a confident answer to a question you
        # did not ask.
        body = client.post(
            "/api/ask",
            json={"question": "What did Martin Vickers say about refineries?", "answer": False},
        ).json()
        assert body["filters"]["member"] == "Martin Vickers"

    def test_passages_carry_their_citation(self, client: TestClient, populated: None) -> None:
        body = client.post("/api/ask", json={"question": "oil refinery", "answer": False}).json()
        assert body["passages"][0]["citation"]
        assert body["passages"][0]["date"]

    def test_a_missing_api_key_is_reported_not_hidden(
        self, client: TestClient, populated: None
    ) -> None:
        response = client.post("/api/ask", json={"question": "oil refinery", "answer": True})
        assert response.status_code == 502
        assert "LLM_API_KEY" in response.json()["error"]
        # The passages still come back, so the page stays useful without a key.
        assert response.json()["passages"]

    @pytest.mark.parametrize("question", ["", "a"])
    def test_a_trivial_question_is_rejected(self, client: TestClient, question: str) -> None:
        assert client.post("/api/ask", json={"question": question}).status_code == 422

    def test_the_limit_is_bounded(self, client: TestClient) -> None:
        # An unbounded limit would let one request pull the whole index into
        # memory and into a prompt.
        assert (
            client.post("/api/ask", json={"question": "housing", "limit": 500}).status_code == 422
        )


class TestDatabaseDown:
    def test_status_reports_a_missing_database(self, tmp_path) -> None:
        broken = Settings(database_url="postgresql://nobody@127.0.0.1:1/none", vector_path=tmp_path)
        with TestClient(create_app(broken)) as client:
            response = client.get("/api/status")
        assert response.status_code == 503
        assert "docker compose up" in response.json()["error"]

    def test_ask_reports_a_missing_database(self, tmp_path) -> None:
        broken = Settings(database_url="postgresql://nobody@127.0.0.1:1/none", vector_path=tmp_path)
        with TestClient(create_app(broken)) as client:
            response = client.post("/api/ask", json={"question": "housing"})
        assert response.status_code == 503


class TestAnswerGuards:
    def test_no_passages_means_no_model_call(self, settings: Settings) -> None:
        # Asking a model to answer from an empty context is the single most
        # reliable way to get a confident invention.
        result = rag_answer.answer_question(
            settings,
            "anything?",
            [],
            rag_retrieve.QueryFilters(),
            latest_data_date=date(2026, 4, 29),
        )
        assert "Nothing in the stored debates" in result.text
        assert result.passages == ()

    def test_a_missing_key_is_an_actionable_error(self, settings: Settings) -> None:
        passage = rag_retrieve.Passage(
            text="something",
            speaker_name="A",
            party="Lab",
            debate_title="D",
            sitting_date=date(2026, 3, 1),
            debate_ext_id="d",
            member_id=1,
            score=1.0,
            sources=("vector",),
        )
        with pytest.raises(rag_answer.AnswerError, match="passages-only"):
            rag_answer.answer_question(settings, "q", [passage], rag_retrieve.QueryFilters())

    def test_passages_are_numbered_for_citation(self) -> None:
        passages = [
            rag_retrieve.Passage(
                text=f"text {i}",
                speaker_name=f"Speaker {i}",
                party="Lab",
                debate_title="D",
                sitting_date=date(2026, 3, 1),
                debate_ext_id="d",
                member_id=i,
                score=1.0,
                sources=("vector",),
            )
            for i in range(3)
        ]
        formatted = rag_answer.format_passages(passages)
        assert "[1]" in formatted and "[3]" in formatted
        assert "Speaker 0" in formatted

    def test_the_prompt_states_the_scope_that_was_searched(self) -> None:
        # Otherwise the model can claim a member "never mentioned" something
        # when the search was restricted to a fortnight.
        passages = [
            rag_retrieve.Passage(
                text="t",
                speaker_name="A",
                party="Lab",
                debate_title="D",
                sitting_date=date(2026, 3, 1),
                debate_ext_id="d",
                member_id=1,
                score=1.0,
                sources=("vector",),
            )
        ]
        prompt = rag_answer.build_prompt(
            "q",
            passages,
            rag_retrieve.QueryFilters(
                member_name="Wes Streeting",
                start_date=date(2026, 3, 1),
                end_date=date(2026, 4, 1),
            ),
        )
        assert "Wes Streeting" in prompt
        assert "2026-03-01" in prompt

    def test_the_system_prompt_forbids_outside_knowledge(self) -> None:
        # The characteristic RAG failure is a fluent answer assembled from the
        # model's own knowledge, which reads exactly like a grounded one.
        assert "only the extracts" in rag_answer.SYSTEM_PROMPT.lower()
        assert "background knowledge" in rag_answer.SYSTEM_PROMPT.lower()
