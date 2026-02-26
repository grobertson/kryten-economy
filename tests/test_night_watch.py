"""Tests for Sprint 2 — Night Watch Multiplier."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker

from conftest import make_config_dict


@pytest.fixture
def night_config() -> EconomyConfig:
    """Config with night watch enabled."""
    d = make_config_dict()
    d["presence"]["night_watch"] = {"enabled": True, "hours": [2, 3, 4], "multiplier": 2.0}
    return EconomyConfig(**d)


@pytest.fixture
def night_tracker(night_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    return PresenceTracker(
        config=night_config, database=database, client=mock_client,
        logger=logging.getLogger("test.nightwatch"),
    )


class TestNightWatch:
    """Night watch multiplier tests."""

    def test_night_watch_config(self, night_config: EconomyConfig):
        """Night watch should be enabled with custom hours."""
        nw = night_config.presence.night_watch
        assert nw.enabled is True
        assert nw.hours == [2, 3, 4]
        assert nw.multiplier == 2.0

    def test_night_watch_disabled_by_default(self, sample_config: EconomyConfig):
        """Default config should have night watch disabled."""
        assert sample_config.presence.night_watch.enabled is False

    async def test_night_watch_hours_list(self, night_config: EconomyConfig):
        """Night watch hours should use list[int] from master plan."""
        nw = night_config.presence.night_watch
        assert isinstance(nw.hours, list)
        assert all(isinstance(h, int) for h in nw.hours)
        # Should check membership, not range
        assert 2 in nw.hours
        assert 5 not in nw.hours

    async def test_multiplier_applied_in_config(self, night_config: EconomyConfig):
        """Multiplier val should match config."""
        assert night_config.presence.night_watch.multiplier == 2.0
