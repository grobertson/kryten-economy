"""Tests for SpectacleManager — mutual exclusion + shared cooldown."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from kryten_economy.spectacle_manager import SpectacleManager


CH = "test-channel"


@pytest.fixture
def manager() -> SpectacleManager:
    config = MagicMock()
    config.gambling.spectacle_cooldown_seconds = 30
    return SpectacleManager(config, logging.getLogger("test"))


class TestSpectacleManager:
    def test_acquire_when_idle(self, manager: SpectacleManager) -> None:
        assert manager.try_acquire(CH, "race") is True
        assert manager.active_game(CH) == "race"

    def test_cannot_acquire_twice(self, manager: SpectacleManager) -> None:
        manager.try_acquire(CH, "race")
        assert manager.try_acquire(CH, "trivia") is False
        assert manager.active_game(CH) == "race"

    def test_release_clears_active(self, manager: SpectacleManager) -> None:
        manager.try_acquire(CH, "race")
        manager.release(CH)
        assert manager.active_game(CH) is None

    def test_cooldown_after_release(self, manager: SpectacleManager) -> None:
        manager.try_acquire(CH, "heist")
        manager.release(CH)
        cd = manager.cooldown_remaining(CH)
        assert cd > 0
        assert cd <= 30

    def test_cannot_acquire_during_cooldown(self, manager: SpectacleManager) -> None:
        manager.try_acquire(CH, "heist")
        manager.release(CH)
        assert manager.try_acquire(CH, "race") is False

    def test_cooldown_zero_when_never_used(self, manager: SpectacleManager) -> None:
        assert manager.cooldown_remaining(CH) == 0

    def test_status_text_idle(self, manager: SpectacleManager) -> None:
        assert "Ready" in manager.status_text(CH)

    def test_status_text_active(self, manager: SpectacleManager) -> None:
        manager.try_acquire(CH, "trivia")
        assert "trivia" in manager.status_text(CH)

    def test_separate_channels(self, manager: SpectacleManager) -> None:
        manager.try_acquire("ch1", "race")
        assert manager.try_acquire("ch2", "trivia") is True
        assert manager.active_game("ch1") == "race"
        assert manager.active_game("ch2") == "trivia"

    def test_release_nonexistent_is_safe(self, manager: SpectacleManager) -> None:
        manager.release("nonexistent")  # Should not raise
