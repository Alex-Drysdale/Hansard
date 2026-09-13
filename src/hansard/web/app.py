"""A small local web app for asking questions of the database.

Deliberately minimal: one page, one endpoint, no build step and no framework on
the client. The point is to get closer to what is actually in the store, so the
interface shows the retrieved passages next to the answer rather than hiding
them. An answer you cannot check is not much use.

Binds to localhost by default and holds an API key, so it is a development tool,
not something to expose.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from hansard.config import Settings
from hansard.db.engine import DatabaseUnavailableError, open_connection
from hansard.logging_config import get_logger
from hansard.rag import answer as rag_answer
from hansard.rag import index as rag_index
from hansard.rag import retrieve as rag_retrieve

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)
    #: Reproduce textbook RAG -- no filters, vector only -- so the difference is
    #: visible in the UI rather than only described in a README.
    naive: bool = False
    answer: bool = True


def _passage_json(passage: rag_retrieve.Passage) -> dict[str, Any]:
    return {
        "text": passage.text,
        "speaker": passage.speaker_name,
        "party": passage.party,
        "debate": passage.debate_title,
        "date": passage.sitting_date.isoformat(),
        "citation": passage.citation(),
        "score": round(passage.score, 4),
        "sources": list(passage.sources),
        "inferred_attribution": passage.has_carried_text,
    }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Hansard", docs_url=None, redoc_url=None)

    @app.get("/")
    def index_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> JSONResponse:
        """What the store holds, so the page can say what it can answer about."""
        try:
            with open_connection(settings.database_url) as connection:
                row = connection.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM debate)                       AS debates,
                           (SELECT COUNT(*) FROM speech)                       AS speeches,
                           (SELECT COUNT(*) FROM member WHERE contribution_count > 0)
                                                                               AS members,
                           (SELECT MIN(sitting_date) FROM debate)              AS earliest,
                           (SELECT MAX(sitting_date) FROM debate)              AS latest
                    """
                ).fetchone()
        except DatabaseUnavailableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

        assert row is not None
        vector = rag_index.index_status(settings.vector_path)
        return JSONResponse(
            {
                "debates": row["debates"],
                "speeches": row["speeches"],
                "members": row["members"],
                "earliest": row["earliest"].isoformat() if row["earliest"] else None,
                "latest": row["latest"].isoformat() if row["latest"] else None,
                "index": {
                    "exists": vector.exists,
                    "chunks": vector.chunks,
                    "built_at": vector.built_at.isoformat() if vector.built_at else None,
                },
                "model": settings.llm_model,
                "can_answer": bool(settings.llm_api_key),
            }
        )

    @app.post("/api/ask")
    def ask(request: AskRequest) -> JSONResponse:
        try:
            with open_connection(settings.database_url) as connection:
                row = connection.execute("SELECT MAX(sitting_date) AS d FROM debate").fetchone()
                latest: date | None = row["d"] if row else None
                passages, filters = rag_retrieve.retrieve(
                    connection,
                    settings,
                    request.question,
                    limit=request.limit,
                    naive=request.naive,
                    latest_data_date=latest,
                )
        except DatabaseUnavailableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

        payload: dict[str, Any] = {
            "question": request.question,
            "filters": {
                "member": filters.member_name,
                "member_id": filters.member_id,
                "start_date": filters.start_date.isoformat() if filters.start_date else None,
                "end_date": filters.end_date.isoformat() if filters.end_date else None,
                "notes": list(filters.notes),
            },
            "passages": [_passage_json(p) for p in passages],
            "answer": None,
            "model": None,
            "tokens": 0,
        }

        if not request.answer or not passages:
            if not passages:
                payload["answer"] = "Nothing in the stored debates matches that question." + (
                    f" The store covers up to {latest}." if latest else ""
                )
            return JSONResponse(payload)

        try:
            result = rag_answer.answer_question(
                settings, request.question, passages, filters, latest_data_date=latest
            )
        except rag_answer.AnswerError as exc:
            payload["error"] = str(exc)
            return JSONResponse(payload, status_code=502)

        payload["answer"] = result.text
        payload["model"] = result.model
        payload["tokens"] = result.tokens
        return JSONResponse(payload)

    return app
