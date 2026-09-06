"""Tests for configuration loading and validation.

Config bugs are the cheapest to prevent and the most annoying to debug -- a
misread date silently ingests the wrong four months -- so the validation is
tested rather than trusted.
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from hansard.config import (
    DEFAULT_END_DATE,
    DEFAULT_HOUSE,
    DEFAULT_START_DATE,
    ConfigError,
    Settings,
)


class TestDefaults:
    def test_defaults_describe_the_phase_one_slice(self) -> None:
        settings = Settings()
        assert settings.house == DEFAULT_HOUSE == "Commons"
        assert settings.start_date == DEFAULT_START_DATE == date(2026, 1, 1)
        assert settings.end_date == DEFAULT_END_DATE == date(2026, 4, 30)

    def test_works_with_no_environment_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in [key for key in os.environ if key.startswith("HANSARD_")]:
            monkeypatch.delenv(name, raising=False)
        assert Settings.from_env().house == "Commons"


class TestValidation:
    def test_rejects_an_unknown_house(self) -> None:
        with pytest.raises(ConfigError, match="house must be one of"):
            Settings(house="Senate")

    def test_rejects_an_inverted_date_range(self) -> None:
        with pytest.raises(ConfigError, match="is after"):
            Settings(start_date=date(2026, 5, 1), end_date=date(2026, 1, 1))

    def test_allows_a_single_day_range(self) -> None:
        day = date(2026, 1, 14)
        assert Settings(start_date=day, end_date=day).start_date == day

    def test_rejects_a_non_positive_rate(self) -> None:
        with pytest.raises(ConfigError, match="requests_per_second"):
            Settings(requests_per_second=0)

    def test_rejects_a_zero_page_size(self) -> None:
        with pytest.raises(ConfigError, match="page_size"):
            Settings(page_size=0)


class TestFromEnv:
    def test_reads_overrides(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HANSARD_HOUSE", "Lords")
        monkeypatch.setenv("HANSARD_START_DATE", "2026-02-01")
        monkeypatch.setenv("HANSARD_END_DATE", "2026-02-28")
        monkeypatch.setenv("HANSARD_DB_PATH", str(tmp_path / "custom.db"))
        monkeypatch.setenv("HANSARD_REQUESTS_PER_SECOND", "2.5")

        settings = Settings.from_env()

        assert settings.house == "Lords"
        assert settings.start_date == date(2026, 2, 1)
        assert settings.end_date == date(2026, 2, 28)
        assert settings.database_path == tmp_path / "custom.db"
        assert settings.requests_per_second == 2.5

    def test_blank_values_fall_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HANSARD_HOUSE", "   ")
        assert Settings.from_env().house == "Commons"

    def test_a_malformed_date_is_reported_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HANSARD_START_DATE", "01/02/2026")
        with pytest.raises(ConfigError, match="HANSARD_START_DATE must be an ISO date"):
            Settings.from_env()

    def test_a_malformed_number_is_reported_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HANSARD_MAX_RETRIES", "lots")
        with pytest.raises(ConfigError, match="HANSARD_MAX_RETRIES must be an integer"):
            Settings.from_env()

    def test_trailing_slash_on_base_url_is_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Otherwise every request path would contain a double slash.
        monkeypatch.setenv("HANSARD_BASE_URL", "https://example.test/")
        assert Settings.from_env().base_url == "https://example.test"


def test_settings_are_immutable() -> None:
    # Frozen so a mid-run mutation cannot silently change what is being fetched.
    settings = Settings()
    with pytest.raises(FrozenInstanceError):
        settings.house = "Lords"  # type: ignore[misc]
