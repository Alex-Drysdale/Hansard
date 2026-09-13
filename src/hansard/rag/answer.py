"""Turning retrieved passages into a grounded answer.

The model is given the passages and told to answer only from them. That
instruction is not a formality: the failure everyone hits with RAG is a fluent
answer built from the model's own knowledge of British politics, which reads
exactly like a grounded one and is impossible to spot without checking.

Two things make that checkable here. Every claim must cite a numbered passage,
and the passages are returned alongside the answer so a person can read them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from hansard.config import Settings
from hansard.logging_config import get_logger
from hansard.rag.retrieve import Passage, QueryFilters

log = get_logger(__name__)

SYSTEM_PROMPT = """You answer questions about UK parliamentary debates using \
only the numbered extracts provided.

Rules:
- Use only the extracts. If they do not answer the question, say so plainly.
- Cite the extract number for every claim, like [3].
- Quote sparingly and exactly; never paraphrase a quotation into something \
stronger than the words support.
- If the extracts show a member saying something only in passing, say that \
rather than presenting it as a considered position.
- Do not add background knowledge about UK politics, even if you are confident \
it is correct. The user needs to know what is in this data, not what you know.
- Be concise. Three or four sentences is usually enough."""


#: Models do not reliably emit the citation format they are asked for. This one
#: returns OpenAI-style file markers -- 【4†L1-L4】 -- despite the prompt asking
#: for [4]. Normalising is more reliable than prompting harder, and keeps the
#: answer readable whichever model is behind LLM_BASE_URL.
_FANCY_CITATION = re.compile(r"【(\d+)[^】]*】")


def normalise_citations(text: str) -> str:
    """Rewrite whatever citation markers the model produced as ``[n]``."""
    return _FANCY_CITATION.sub(r"[\1]", text)


class AnswerError(RuntimeError):
    """The model could not be reached or returned nothing usable."""


@dataclass(frozen=True, slots=True)
class Answer:
    question: str
    text: str
    passages: tuple[Passage, ...]
    filters: QueryFilters
    model: str
    tokens: int = 0
    notes: tuple[str, ...] = field(default=())


def format_passages(passages: list[Passage]) -> str:
    """Number the extracts so the model can cite them."""
    blocks = []
    for number, passage in enumerate(passages, start=1):
        header = (
            f"[{number}] {passage.speaker_name or 'Unattributed'}"
            f"{f' ({passage.party})' if passage.party else ''}"
            f" — {passage.debate_title}, {passage.sitting_date:%d %B %Y}"
        )
        blocks.append(f"{header}\n{passage.text.strip()}")
    return "\n\n".join(blocks)


def build_prompt(question: str, passages: list[Passage], filters: QueryFilters) -> str:
    scope = []
    if filters.member_name:
        scope.append(f"Only extracts by {filters.member_name} were searched.")
    if filters.start_date and filters.end_date:
        scope.append(f"Only {filters.start_date} to {filters.end_date} was searched.")
    scope_note = (" " + " ".join(scope)) if scope else ""

    return (
        f"Question: {question}\n"
        f"{scope_note}\n\n"
        f"Extracts:\n\n{format_passages(passages)}\n\n"
        "Answer the question using only these extracts, citing them by number."
    )


def answer_question(
    settings: Settings,
    question: str,
    passages: list[Passage],
    filters: QueryFilters,
    *,
    latest_data_date: date | None = None,
) -> Answer:
    """Ask the model to answer from the passages.

    Short-circuits when retrieval found nothing rather than asking the model to
    answer from an empty context, which is the single most reliable way to
    produce a confident invention.
    """
    if not passages:
        return Answer(
            question=question,
            text=(
                "Nothing in the stored debates matches that question. "
                + (
                    f"The store covers Commons debates up to {latest_data_date}."
                    if latest_data_date
                    else ""
                )
            ),
            passages=(),
            filters=filters,
            model=settings.llm_model,
            notes=("no passages retrieved",),
        )

    if not settings.llm_api_key:
        raise AnswerError(
            "No LLM_API_KEY set. Retrieval works without it -- try `hansard search` "
            "or `hansard ask --passages-only` -- but answering needs a key."
        )

    from openai import OpenAI

    client = OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.timeout_seconds,
    )

    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(question, passages, filters)},
            ],
            # Generous, because reasoning models spend most of a small budget
            # thinking and then return an empty string.
            max_tokens=settings.llm_max_tokens,
            temperature=0.2,
        )
    except Exception as exc:
        raise AnswerError(f"{settings.llm_model} could not be reached: {exc}") from exc

    choice = response.choices[0]
    text = normalise_citations((choice.message.content or "").strip())
    if not text:
        raise AnswerError(
            f"{settings.llm_model} returned no content (finish_reason="
            f"{choice.finish_reason}). Reasoning models need a larger max_tokens."
        )

    return Answer(
        question=question,
        text=text,
        passages=tuple(passages),
        filters=filters,
        model=response.model,
        tokens=response.usage.total_tokens if response.usage else 0,
    )
