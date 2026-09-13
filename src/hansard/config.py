"""Runtime configuration.

Every knob has a default that works against the container in
``docker-compose.yml``, so a fresh clone needs no ``.env`` at all. Anything can
be overridden by an environment variable -- see ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Phase 1 fixed a narrow slice of Hansard: one house, four months. A dataset
# small enough to rebuild in a quarter of an hour is one you can afford to keep
# changing your mind about, and those defaults still hold.
DEFAULT_HOUSE = "Commons"
DEFAULT_START_DATE = date(2026, 1, 1)
DEFAULT_END_DATE = date(2026, 4, 30)

VALID_HOUSES = ("Commons", "Lords")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DATABASE_URL = "postgresql://hansard:hansard@localhost:5432/hansard"

DEFAULT_HANSARD_BASE_URL = "https://hansard-api.parliament.uk"
DEFAULT_MEMBERS_BASE_URL = "https://members-api.parliament.uk/api"

DEFAULT_REQUESTS_PER_SECOND = 5.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 100
DEFAULT_USER_AGENT = "hansard-pipeline/0.2 (personal research project)"

# --- Retrieval -------------------------------------------------------------
# The vector store is a local directory, not a service: Phase 3 is about
# understanding retrieval, and a server would add operational noise without
# teaching anything.
DEFAULT_VECTOR_PATH = PROJECT_ROOT / "data" / "vectors"

# 384 dimensions, ONNX, no torch and no GPU. Chosen over bge-small for speed
# -- about 3x, which is the difference between a 35-minute index build and a
# two-hour one -- and over all-MiniLM-L6-v2 for context: fastembed truncates
# MiniLM at 128 tokens, roughly 90 words, which would silently discard most of
# every chunk. See embeddings.check_model_fits_chunks.
DEFAULT_EMBEDDING_MODEL = "snowflake/snowflake-arctic-embed-xs"

# A chunk is aimed at roughly 350 words: comfortably inside the model's 512
# token limit, with headroom for the speaker/debate header prefixed to it.
DEFAULT_CHUNK_WORDS = 350

# Processes used for embedding.
#
# Two, not the core count. Each worker loads its own copy of onnxruntime and the
# model, and measurement on this machine puts that at roughly 1GB *per process*
# -- four workers took 4GB and the operating system killed the build. Two is
# still about twice as fast as serial, and leaves the machine usable.
#
# Raise it if you have memory to spare; the guard is your own free RAM, not
# anything in this code.
DEFAULT_EMBED_PARALLEL = 2
DEFAULT_CHUNK_OVERLAP_WORDS = 60

# --- Answering -------------------------------------------------------------
# An OpenAI-compatible endpoint. Groq by default: fast, and the same client
# works against OpenAI or a local llama.cpp server by changing the base URL.
DEFAULT_LLM_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"
# gpt-oss is a reasoning model: it spends tokens thinking before it answers, so
# a budget sized for the answer alone comes back empty.
DEFAULT_LLM_MAX_TOKENS = 2000

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "console"
VALID_LOG_FORMATS = ("console", "json")

# Cron expressions for the scheduled jobs. Hansard publishes overnight, so
# everything runs early, staggered so the jobs do not contend.
#
# The order is load-bearing, not cosmetic. Both debates and divisions introduce
# member ids -- and divisions introduce *more* of them, because a backbencher
# can go a month without speaking and still vote thirty times. Running the
# member sync before divisions leaves every new voter unresolved until the next
# pass, so it goes last. It is cheap enough to run daily because it only fetches
# members it has never seen; checks then run over the finished result.
DEFAULT_SCHEDULE = {
    "hansard.debates": "0 6 * * *",
    # Straight after the ingest: Hansard names the speaker on the first
    # paragraph of a speech only, and the rest are resolved from the stored
    # transcript rather than refetched.
    "reattribute": "20 6 * * *",
    "hansard.divisions": "30 6 * * *",
    "members.sync": "0 7 * * *",
    # Last, and only once everything it indexes is final. A stale index is the
    # quietest failure here -- the answer stays fluent and simply omits
    # whatever arrived after the last build -- so it is scheduled rather than
    # left to somebody remembering.
    "rag.index": "30 7 * * *",
    "checks": "0 9 * * *",
}


class ConfigError(ValueError):
    """Raised when the environment holds a value we cannot act on."""


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_date(name: str, default: date) -> date:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an ISO date (YYYY-MM-DD), got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the pipeline needs to know, resolved once at startup."""

    database_url: str = DEFAULT_DATABASE_URL

    hansard_base_url: str = DEFAULT_HANSARD_BASE_URL
    members_base_url: str = DEFAULT_MEMBERS_BASE_URL

    house: str = DEFAULT_HOUSE
    start_date: date = DEFAULT_START_DATE
    end_date: date = DEFAULT_END_DATE

    # Politeness. Both APIs are unauthenticated and publicly funded, so we
    # self-limit rather than discovering their limit the hard way.
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS

    page_size: int = DEFAULT_PAGE_SIZE

    user_agent: str = DEFAULT_USER_AGENT

    vector_path: Path = DEFAULT_VECTOR_PATH
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    chunk_words: int = DEFAULT_CHUNK_WORDS
    chunk_overlap_words: int = DEFAULT_CHUNK_OVERLAP_WORDS
    embed_parallel: int = DEFAULT_EMBED_PARALLEL

    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS

    log_level: str = DEFAULT_LOG_LEVEL
    log_format: str = DEFAULT_LOG_FORMAT

    def __post_init__(self) -> None:
        if self.house not in VALID_HOUSES:
            raise ConfigError(f"house must be one of {VALID_HOUSES}, got {self.house!r}")
        if self.start_date > self.end_date:
            raise ConfigError(f"start_date {self.start_date} is after end_date {self.end_date}")
        if self.requests_per_second <= 0:
            raise ConfigError("requests_per_second must be positive")
        if self.page_size < 1:
            raise ConfigError("page_size must be at least 1")
        if self.chunk_overlap_words >= self.chunk_words:
            raise ConfigError(
                "chunk_overlap_words must be smaller than chunk_words, "
                "or chunking would never advance"
            )
        if self.log_format not in VALID_LOG_FORMATS:
            raise ConfigError(
                f"log_format must be one of {VALID_LOG_FORMATS}, got {self.log_format!r}"
            )

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables, falling back to defaults."""
        return cls(
            database_url=_env_str("HANSARD_DATABASE_URL", DEFAULT_DATABASE_URL),
            hansard_base_url=_env_str("HANSARD_BASE_URL", DEFAULT_HANSARD_BASE_URL).rstrip("/"),
            members_base_url=_env_str("MEMBERS_BASE_URL", DEFAULT_MEMBERS_BASE_URL).rstrip("/"),
            house=_env_str("HANSARD_HOUSE", DEFAULT_HOUSE),
            start_date=_env_date("HANSARD_START_DATE", DEFAULT_START_DATE),
            end_date=_env_date("HANSARD_END_DATE", DEFAULT_END_DATE),
            requests_per_second=_env_float(
                "HANSARD_REQUESTS_PER_SECOND", DEFAULT_REQUESTS_PER_SECOND
            ),
            timeout_seconds=_env_float("HANSARD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            max_retries=_env_int("HANSARD_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            page_size=_env_int("HANSARD_PAGE_SIZE", DEFAULT_PAGE_SIZE),
            vector_path=Path(_env_str("HANSARD_VECTOR_PATH", str(DEFAULT_VECTOR_PATH))),
            embedding_model=_env_str("HANSARD_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            chunk_words=_env_int("HANSARD_CHUNK_WORDS", DEFAULT_CHUNK_WORDS),
            chunk_overlap_words=_env_int(
                "HANSARD_CHUNK_OVERLAP_WORDS", DEFAULT_CHUNK_OVERLAP_WORDS
            ),
            embed_parallel=_env_int("HANSARD_EMBED_PARALLEL", DEFAULT_EMBED_PARALLEL),
            # LLM_* rather than HANSARD_LLM_*: the same names the other projects
            # on this machine already use, so one .env serves both.
            llm_base_url=_env_str("LLM_BASE_URL", DEFAULT_LLM_BASE_URL).rstrip("/"),
            llm_api_key=_env_str("LLM_API_KEY", ""),
            llm_model=_env_str("LLM_MODEL", DEFAULT_LLM_MODEL),
            llm_max_tokens=_env_int("LLM_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS),
            log_level=_env_str("HANSARD_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
            log_format=_env_str("HANSARD_LOG_FORMAT", DEFAULT_LOG_FORMAT).lower(),
        )
