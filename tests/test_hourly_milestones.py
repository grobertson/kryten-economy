"""Tests for Sprint 2 — Hourly Dwell Milestones."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker


@pytest.fixture
def tracker(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    return PresenceTracker(
        config=sample_config, database=database, client=mock_client,
        logger=logging.getLogger("test.milestones"),
    )


class TestHourlyMilestones:
    """Hourly dwell milestone logic."""

    async def test_1h_milestone(self, tracker: PresenceTracker, database: EconomyDatabase):
        """60 cumulative minutes should trigger 1h milestone."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 60)
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 10  # 1h: 10 Z

    async def test_3h_milestone(self, tracker: PresenceTracker, database: EconomyDatabase):
        """180 cumulative minutes should trigger 1h + 3h milestones."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 180)
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 40  # 1h:10 + 3h:30

    async def test_milestone_idempotent(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Checking milestones twice should not double-award."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 60)
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 60)
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 10  # Only once

    async def test_no_milestone_below_threshold(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Less than 60 minutes should not trigger any milestone."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 59)
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 0

    async def test_milestone_pm_sent(self, tracker: PresenceTracker, database: EconomyDatabase, mock_client: MagicMock):
        """Milestone should send a PM notification."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 60)
        mock_client.send_pm.assert_called()
        msg = mock_client.send_pm.call_args[0][2]
        assert "milestone" in msg.lower()

    async def test_different_days_independent(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Milestones on different calendar days should be independent."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-01", 60)
        await tracker._check_hourly_milestones("alice", "testchannel", "2026-01-02", 60)
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 20  # 10 each day

    async def test_db_mark_milestone(self, database: EconomyDatabase):
        """Database should track claimed milestones."""
        await database.get_or_create_account("alice", "testchannel")
        row = await database.get_or_create_hourly_milestones("alice", "testchannel", "2026-01-01")
        assert row["hours_1"] == 0
        await database.mark_hourly_milestone("alice", "testchannel", "2026-01-01", 1)
        row = await database.get_or_create_hourly_milestones("alice", "testchannel", "2026-01-01")
        assert row["hours_1"] == 1

    async def test_invalid_milestone_column(self, database: EconomyDatabase):
        """Invalid milestone hours should be silently ignored."""
        await database.get_or_create_account("alice", "testchannel")
        await database.get_or_create_hourly_milestones("alice", "testchannel", "2026-01-01")
        # hours=99 is invalid — should not error
        await database.mark_hourly_milestone("alice", "testchannel", "2026-01-01", 99)
