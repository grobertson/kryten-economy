"""Tests for Sprint 2 — Weekend→Weekday Bridge Bonus."""

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
        logger=logging.getLogger("test.bridge"),
    )


class TestBridgeBonus:
    """Weekend→weekday bridge bonus logic."""

    async def test_weekend_only_no_bonus(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Being seen only on weekend should not trigger bridge."""
        await database.get_or_create_account("alice", "testchannel")
        # 2026-01-03 is Saturday
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-03")
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 0

    async def test_weekday_only_no_bonus(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Being seen only on weekday should not trigger bridge."""
        await database.get_or_create_account("alice", "testchannel")
        # 2026-01-05 is Monday
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-05")
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 0

    async def test_bridge_awarded(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Seen on both weekend and weekday in same week should award bridge."""
        await database.get_or_create_account("alice", "testchannel")
        # 2026-01-05 is Monday (W02), 2026-01-10 is Saturday (W02) — same ISO week
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-05")
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-10")
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 500  # bridge bonus

    async def test_bridge_not_double_claimed(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Bridge should only be claimed once per week."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-05")  # Mon W02
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-10")  # Sat W02 (claims)
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-11")  # Sun W02
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 500  # Not doubled

    async def test_new_week_resets(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Different ISO week should reset bridge tracking."""
        await database.get_or_create_account("alice", "testchannel")
        # Week 2 (2026-W02): Mon Jan 5 + Sat Jan 10 → bridge
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-05")  # Mon W02
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-10")  # Sat W02
        # Week 3 (2026-W03): Mon Jan 12 + Sat Jan 17 → bridge
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-12")  # Mon W03
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-17")  # Sat W03

        # Should have earned bridge for two different weeks
        balance = await database.get_balance("alice", "testchannel")
        assert balance == 1000  # 500 per week × 2

    async def test_bridge_pm_sent(self, tracker: PresenceTracker, database: EconomyDatabase, mock_client: MagicMock):
        """Bridge bonus should trigger a PM notification."""
        await database.get_or_create_account("alice", "testchannel")
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-05")  # Mon W02
        await tracker._evaluate_bridge("alice", "testchannel", "2026-01-10")  # Sat W02
        mock_client.send_pm.assert_called()
        msg = mock_client.send_pm.call_args[0][2]
        assert "bridge" in msg.lower() or "500" in msg
