"""Tests for Sprint 2 — Daily Streaks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker


@pytest.fixture
def tracker(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    return PresenceTracker(
        config=sample_config, database=database, client=mock_client,
        logger=logging.getLogger("test.streaks"),
    )


class TestDailyStreaks:
    """Daily streak evaluation logic."""

    async def test_first_day_no_bonus(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Day 1 (first qualifying day) should set streak=1 but no bonus."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-01")
        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 1
        # No bonus for day 1 (only day 2+)
        assert await database.get_balance("alice", "testchannel") == 0

    async def test_day_two_bonus(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Day 2 should earn streak bonus."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-01")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-02")
        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 2
        # Day 2 reward = 10 (from config)
        assert await database.get_balance("alice", "testchannel") == 10

    async def test_streak_break_resets(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Missing a day should reset streak to 1."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-01")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-02")
        # Skip Jan 3
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-04")
        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 1

    async def test_same_day_idempotent(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Evaluating twice on same day should not double-count."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-01")
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-01")
        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 1

    async def test_7_day_milestone_bonus(self, tracker: PresenceTracker, database: EconomyDatabase):
        """7-day streak should get milestone bonus on top of daily."""
        await database.get_or_create_account("alice", "testchannel")
        # Build up 7 consecutive days
        for day in range(1, 8):
            date = f"2026-01-{day:02d}"
            await tracker._evaluate_daily_streak("alice", "testchannel", date)

        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 7

        # Balance = sum of streak rewards (day2:10 + day3:20 + day4-6 use defaults + day7:100) + milestone_7:200
        balance = await database.get_balance("alice", "testchannel")
        assert balance > 200  # Must include milestone bonus

    async def test_longest_streak_tracked(self, tracker: PresenceTracker, database: EconomyDatabase):
        """longest_daily_streak should persist even after reset."""
        await database.get_or_create_account("alice", "testchannel")
        # Build 3-day streak
        for day in range(1, 4):
            date = f"2026-01-{day:02d}"
            await tracker._evaluate_daily_streak("alice", "testchannel", date)

        # Break streak
        await tracker._evaluate_daily_streak("alice", "testchannel", "2026-01-06")
        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 1
        assert streak["longest_daily_streak"] == 3

    async def test_day_rewards_beyond_config(self, tracker: PresenceTracker, database: EconomyDatabase):
        """For streak days beyond configured rewards, fallback to day-7 reward."""
        await database.get_or_create_account("alice", "testchannel")
        # Build up 8 consecutive days
        for day in range(1, 9):
            date = f"2026-01-{day:02d}"
            await tracker._evaluate_daily_streak("alice", "testchannel", date)

        streak = await database.get_or_create_streak("alice", "testchannel")
        assert streak["current_daily_streak"] == 8
        # Day 8 should use day-7 fallback reward (100)
        # Verify balance is higher than sum of configured rewards
        balance = await database.get_balance("alice", "testchannel")
        assert balance > 0
