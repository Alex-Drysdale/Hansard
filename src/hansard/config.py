"""Runtime configuration.

Every knob has a default that makes ``hansard ingest`` work with no setup at all.
Anything can be overridden by an environment variable (see ``.env.example``) so
that the same code runs unchanged in a test, on a laptop, or in CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Phase 1 deliberately fixes a narrow slice of Hansard: one house, four months.
# A small dataset keeps the whole pipeline re-runnable in minutes, which is what
# makes it safe to keep changing the schema.
DEFAULT_HOUSE = "Commons"
DEFAULT_START_DATE = date(2026, 1, 1)
DEFAULT_END_DATE = date(2026, 4, 30)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

VALID_HOUSES = ("Commons", "Lords")

DEFAULT_BASE_URL = "https://hansard-api.parliament.uk"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "hansard.db"
DEFAULT_REQUESTS_PER_SECOND = 5.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 100
DEFAULT_USER_AGENT = "hansard-pipeline/0.1 (personal research project)"


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

    base_url: str = DEFAULT_BASE_URL
    database_path: Path = DEFAULT_DATABASE_PATH

    house: str = DEFAULT_HOUSE
    start_date: date = DEFAULT_START_DATE
    end_date: date = DEFAULT_END_DATE

    # Politeness. The Hansard API is unauthenticated and publicly funded, so we
    # self-limit rather than discovering their limit the hard way.
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS

    # search/debates is paginated; 100 is the largest page the API reliably returns.
    page_size: int = DEFAULT_PAGE_SIZE

    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if self.house not in VALID_HOUSES:
            raise ConfigError(f"house must be one of {VALID_HOUSES}, got {self.house!r}")
        if self.start_date > self.end_date:
            raise ConfigError(f"start_date {self.start_date} is after end_date {self.end_date}")
        if self.requests_per_second <= 0:
            raise ConfigError("requests_per_second must be positive")
        if self.page_size < 1:
            raise ConfigError("page_size must be at least 1")

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables, falling back to defaults."""
        db_raw = os.environ.get("HANSARD_DB_PATH", "").strip()
        return cls(
            base_url=_env_str("HANSARD_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            database_path=Path(db_raw) if db_raw else DEFAULT_DATABASE_PATH,
            house=_env_str("HANSARD_HOUSE", DEFAULT_HOUSE),
            start_date=_env_date("HANSARD_START_DATE", DEFAULT_START_DATE),
            end_date=_env_date("HANSARD_END_DATE", DEFAULT_END_DATE),
            requests_per_second=_env_float(
                "HANSARD_REQUESTS_PER_SECOND", DEFAULT_REQUESTS_PER_SECOND
            ),
            timeout_seconds=_env_float("HANSARD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            max_retries=_env_int("HANSARD_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            page_size=_env_int("HANSARD_PAGE_SIZE", DEFAULT_PAGE_SIZE),
        )
