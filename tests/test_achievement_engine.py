"""Tests for AchievementEngine — Sprint 6."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.achievement_engine import AchievementEngine
from kryten_economy.config import (
    AchievementConditionConfig,
    AchievementConfig,
    EconomyConfig,
)
from kryten_economy.database import EconomyDatabase
from tests.conftest import make_config_dict

CH = "testchannel"


def _cfg_with_achievements(achievements: list[dict]) -> EconomyConfig:
    """Build EconomyConfig with custom achievements."""
    return EconomyConfig(**make_config_dict(achievements=achievements))


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 0, **kwargs) -> None:
    """Create account and set extra fields."""
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_award_first_time(database: EconomyDatabase, mock_client: MagicMock):
    """Achievement awarded, reward credited, PM sent."""
    cfg = _cfg_with_achievements([{
        "id": "first_100",
        "description": "Earn 100 Z lifetime",
        "condition": {"type": "lifetime_earned", "threshold": 100},
        "reward": 50,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))

    await _seed_account(database, "Alice", 0)
    await database.credit("Alice", CH, 150, tx_type="earn", reason="test")

    awarded = await engine.check_achievements("Alice", CH, ["lifetime_earned"])
    assert len(awarded) == 1
    assert awarded[0].id == "first_100"

    acc = await database.get_account("Alice", CH)
    assert acc["balance"] >= 50  # reward credited

    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_already_awarded_skipped(database: EconomyDatabase, mock_client: MagicMock):
    """Duplicate achievement not re-awarded."""
    cfg = _cfg_with_achievements([{
        "id": "dup_test",
        "description": "Test dup",
        "condition": {"type": "lifetime_earned", "threshold": 10},
        "reward": 5,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")
    await database.credit("Alice", CH, 100, tx_type="earn", reason="test")

    first = await engine.check_achievements("Alice", CH, ["lifetime_earned"])
    assert len(first) == 1

    second = await engine.check_achievements("Alice", CH, ["lifetime_earned"])
    assert len(second) == 0


@pytest.mark.asyncio
async def test_condition_lifetime_messages(database: EconomyDatabase, mock_client: MagicMock):
    """Threshold met via lifetime_messages → awarded."""
    cfg = _cfg_with_achievements([{
        "id": "chatterbox",
        "description": "Send 10 messages",
        "condition": {"type": "lifetime_messages", "threshold": 10},
        "reward": 20,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")

    # Seed daily_activity with enough messages to meet lifetime threshold
    import asyncio
    def _set():
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT INTO daily_activity (username, channel, date, messages_sent) "
                "VALUES (?, ?, '2026-01-01', 15)",
                ('Alice', CH),
            )
            conn.commit()
        finally:
            conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _set)

    awarded = await engine.check_achievements("Alice", CH, ["lifetime_messages"])
    assert len(awarded) == 1


@pytest.mark.asyncio
async def test_condition_lifetime_messages_below(database: EconomyDatabase, mock_client: MagicMock):
    """Below threshold → not awarded."""
    cfg = _cfg_with_achievements([{
        "id": "chatterbox",
        "description": "Send 100 messages",
        "condition": {"type": "lifetime_messages", "threshold": 100},
        "reward": 20,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")

    awarded = await engine.check_achievements("Alice", CH, ["lifetime_messages"])
    assert len(awarded) == 0


@pytest.mark.asyncio
async def test_condition_daily_streak(database: EconomyDatabase, mock_client: MagicMock):
    """Streak threshold met → awarded."""
    cfg = _cfg_with_achievements([{
        "id": "streak_3",
        "description": "3-day streak",
        "condition": {"type": "daily_streak", "threshold": 3},
        "reward": 30,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")

    # Seed streaks table with a streak of 5
    import asyncio
    def _set():
        conn = database._get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO streaks (username, channel, current_daily_streak) "
                "VALUES (?, ?, 5)",
                ('Alice', CH),
            )
            conn.commit()
        finally:
            conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _set)

    awarded = await engine.check_achievements("Alice", CH, ["daily_streak"])
    assert len(awarded) == 1


@pytest.mark.asyncio
async def test_condition_unique_tip_recipients(database: EconomyDatabase, mock_client: MagicMock):
    """Tip count meets threshold."""
    cfg = _cfg_with_achievements([{
        "id": "tipper_3",
        "description": "Tip 3 different people",
        "condition": {"type": "unique_tip_recipients", "threshold": 3},
        "reward": 25,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", 10000)

    # Record tips to 3 different users
    for target in ["Bob", "Charlie", "Dave"]:
        await _seed_account(database, target)
        await database.record_tip("Alice", target, CH, 10)

    awarded = await engine.check_achievements("Alice", CH, ["unique_tip_recipients"])
    assert len(awarded) == 1


@pytest.mark.asyncio
async def test_condition_rank_reached(database: EconomyDatabase, mock_client: MagicMock):
    """Tier index meets threshold."""
    cfg = _cfg_with_achievements([{
        "id": "rank_2",
        "description": "Reach Key Grip rank",
        "condition": {"type": "rank_reached", "threshold": 2},
        "reward": 100,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")
    # Key Grip requires 5000 lifetime
    await database.credit("Alice", CH, 6000, tx_type="earn", reason="test")

    awarded = await engine.check_achievements("Alice", CH, ["rank_reached"])
    assert len(awarded) == 1


@pytest.mark.asyncio
async def test_hidden_achievement_not_shown(database: EconomyDatabase, mock_client: MagicMock):
    """Hidden achievements excluded from the check output description handling is internal.

    This test ensures that a hidden achievement CAN still be awarded when condition met.
    """
    cfg = _cfg_with_achievements([{
        "id": "secret_1",
        "description": "Secret achievement",
        "condition": {"type": "lifetime_earned", "threshold": 10},
        "reward": 99,
        "hidden": True,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")
    await database.credit("Alice", CH, 100, tx_type="earn", reason="test")

    awarded = await engine.check_achievements("Alice", CH, ["lifetime_earned"])
    assert len(awarded) == 1
    assert awarded[0].hidden is True


@pytest.mark.asyncio
async def test_public_announcement(database: EconomyDatabase, mock_client: MagicMock):
    """Achievement with announce_public sends chat."""
    cfg = _cfg_with_achievements([{
        "id": "loud_one",
        "description": "Big achievement",
        "condition": {"type": "lifetime_earned", "threshold": 5},
        "reward": 10,
        "hidden": False,
        "announce_public": True,
    }])
    # Need to patch announcements config
    cfg.announcements.achievement_milestone = True

    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")
    await database.credit("Alice", CH, 100, tx_type="earn", reason="test")

    await engine.check_achievements("Alice", CH, ["lifetime_earned"])

    mock_client.send_chat.assert_called()
    chat_msg = mock_client.send_chat.call_args[0][1]
    assert "Alice" in chat_msg
    assert "Big achievement" in chat_msg


@pytest.mark.asyncio
async def test_multiple_achievements_same_event(database: EconomyDatabase, mock_client: MagicMock):
    """Multiple achievements can trigger in one check."""
    cfg = _cfg_with_achievements([
        {
            "id": "earn_10",
            "description": "Earn 10 Z",
            "condition": {"type": "lifetime_earned", "threshold": 10},
            "reward": 5,
            "hidden": False,
        },
        {
            "id": "earn_50",
            "description": "Earn 50 Z",
            "condition": {"type": "lifetime_earned", "threshold": 50},
            "reward": 25,
            "hidden": False,
        },
    ])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")
    await database.credit("Alice", CH, 500, tx_type="earn", reason="test")

    awarded = await engine.check_achievements("Alice", CH, ["lifetime_earned"])
    assert len(awarded) == 2
    awarded_ids = {a.id for a in awarded}
    assert "earn_10" in awarded_ids
    assert "earn_50" in awarded_ids


@pytest.mark.asyncio
async def test_unknown_condition_type(database: EconomyDatabase, mock_client: MagicMock):
    """Unknown condition type logged, not awarded."""
    cfg = _cfg_with_achievements([{
        "id": "mystery",
        "description": "Mystery achievement",
        "condition": {"type": "completely_bogus", "threshold": 1},
        "reward": 10,
        "hidden": False,
    }])
    engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice")

    awarded = await engine.check_achievements("Alice", CH, ["completely_bogus"])
    assert len(awarded) == 0
