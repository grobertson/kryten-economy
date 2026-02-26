"""Tests for CompetitionEngine — Sprint 7."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.competition_engine import CompetitionEngine
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from tests.conftest import make_config_dict

CH = "testchannel"


def _cfg_with_competitions(competitions: list[dict]) -> EconomyConfig:
    return EconomyConfig(**make_config_dict(daily_competitions=competitions))


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 0) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")


async def _set_daily_activity(
    db: EconomyDatabase, username: str, date: str, **fields,
) -> None:
    """Set daily_activity fields for a user."""
    loop = asyncio.get_running_loop()

    def _sync():
        conn = db._get_connection()
        try:
            # Ensure row exists first
            conn.execute(
                """INSERT OR IGNORE INTO daily_activity
                   (username, channel, date) VALUES (?, ?, ?)""",
                (username, CH, date),
            )
            for field, value in fields.items():
                conn.execute(
                    f"UPDATE daily_activity SET {field} = ? "
                    f"WHERE username = ? AND channel = ? AND date = ?",
                    (value, username, CH, date),
                )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _sync)


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_threshold_all_qualify(database: EconomyDatabase, mock_client: MagicMock):
    """3 users with gifs >= 5 → all 3 awarded."""
    cfg = _cfg_with_competitions([{
        "id": "gif_fan",
        "description": "GIF Enthusiast",
        "condition": {"type": "daily_threshold", "field": "gifs_posted", "threshold": 5},
        "reward": 50,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for name in ["Alice", "Bob", "Charlie"]:
        await _seed_account(database, name)
        await _set_daily_activity(database, name, today, gifs_posted=7)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 3
    assert all(a["reward"] == 50 for a in awards)


@pytest.mark.asyncio
async def test_threshold_none_qualify(database: EconomyDatabase, mock_client: MagicMock):
    """No users meet threshold → 0 awards."""
    cfg = _cfg_with_competitions([{
        "id": "gif_fan",
        "description": "GIF Enthusiast",
        "condition": {"type": "daily_threshold", "field": "gifs_posted", "threshold": 10},
        "reward": 50,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, gifs_posted=3)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 0


@pytest.mark.asyncio
async def test_threshold_some_qualify(database: EconomyDatabase, mock_client: MagicMock):
    """1 of 3 meets threshold → 1 awarded."""
    cfg = _cfg_with_competitions([{
        "id": "gif_fan",
        "description": "GIF Enthusiast",
        "condition": {"type": "daily_threshold", "field": "gifs_posted", "threshold": 5},
        "reward": 50,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, gifs_posted=10)
    await _seed_account(database, "Bob")
    await _set_daily_activity(database, "Bob", today, gifs_posted=2)
    await _seed_account(database, "Charlie")
    await _set_daily_activity(database, "Charlie", today, gifs_posted=1)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 1
    assert awards[0]["username"] == "Alice"


@pytest.mark.asyncio
async def test_daily_top_single_winner(database: EconomyDatabase, mock_client: MagicMock):
    """Top earner gets champion bonus."""
    cfg = _cfg_with_competitions([{
        "id": "top_earner",
        "description": "Daily Champion",
        "condition": {"type": "daily_top", "field": "z_earned"},
        "reward": 200,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, z_earned=500)
    await _seed_account(database, "Bob")
    await _set_daily_activity(database, "Bob", today, z_earned=300)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 1
    assert awards[0]["username"] == "Alice"
    assert awards[0]["reward"] == 200


@pytest.mark.asyncio
async def test_daily_top_percent_reward(database: EconomyDatabase, mock_client: MagicMock):
    """Top earner reward = 25% of z_earned."""
    cfg = _cfg_with_competitions([{
        "id": "top_earner_pct",
        "description": "Daily Champion (% reward)",
        "condition": {"type": "daily_top", "field": "z_earned"},
        "reward": 0,
        "reward_percent_of_earnings": 25,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, z_earned=400)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 1
    # 25% of 400 = 100
    assert awards[0]["reward"] == 100


@pytest.mark.asyncio
async def test_daily_top_no_activity(database: EconomyDatabase, mock_client: MagicMock):
    """No daily_activity → no awards."""
    cfg = _cfg_with_competitions([{
        "id": "top_earner",
        "description": "Daily Champion",
        "condition": {"type": "daily_top", "field": "z_earned"},
        "reward": 200,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 0


@pytest.mark.asyncio
async def test_multiple_competitions(database: EconomyDatabase, mock_client: MagicMock):
    """Multiple competitions evaluated in one call."""
    cfg = _cfg_with_competitions([
        {
            "id": "gif_fan",
            "description": "GIF Enthusiast",
            "condition": {"type": "daily_threshold", "field": "gifs_posted", "threshold": 3},
            "reward": 30,
        },
        {
            "id": "top_earner",
            "description": "Top Earner",
            "condition": {"type": "daily_top", "field": "z_earned"},
            "reward": 100,
        },
    ])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, gifs_posted=5, z_earned=300)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    assert len(awards) == 2
    comp_ids = {a["competition_id"] for a in awards}
    assert "gif_fan" in comp_ids
    assert "top_earner" in comp_ids


@pytest.mark.asyncio
async def test_pm_sent_per_award(database: EconomyDatabase, mock_client: MagicMock):
    """Each award → PM to winner."""
    cfg = _cfg_with_competitions([{
        "id": "gif_fan",
        "description": "GIF Enthusiast",
        "condition": {"type": "daily_threshold", "field": "gifs_posted", "threshold": 1},
        "reward": 10,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, gifs_posted=5)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    await engine.evaluate_daily_competitions(CH, today)

    # At least one PM call
    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_public_announcement(database: EconomyDatabase, mock_client: MagicMock):
    """Results announced in public chat."""
    cfg = _cfg_with_competitions([{
        "id": "top_earner",
        "description": "Daily Champion",
        "condition": {"type": "daily_top", "field": "z_earned"},
        "reward": 100,
    }])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, z_earned=500)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    await engine.evaluate_daily_competitions(CH, today)

    mock_client.send_chat.assert_called()
    chat_msg = mock_client.send_chat.call_args[0][1]
    assert "Alice" in chat_msg


@pytest.mark.asyncio
async def test_competition_error_isolated(database: EconomyDatabase, mock_client: MagicMock):
    """One competition error doesn't stop others."""
    cfg = _cfg_with_competitions([
        {
            "id": "bad_comp",
            "description": "Bad competition",
            "condition": {"type": "daily_threshold", "field": "totally_bogus_field", "threshold": 1},
            "reward": 10,
        },
        {
            "id": "good_comp",
            "description": "Good competition",
            "condition": {"type": "daily_top", "field": "z_earned"},
            "reward": 100,
        },
    ])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await _seed_account(database, "Alice")
    await _set_daily_activity(database, "Alice", today, z_earned=500)

    engine = CompetitionEngine(cfg, database, mock_client, logging.getLogger("test"))
    awards = await engine.evaluate_daily_competitions(CH, today)

    # The good competition should still work
    good_awards = [a for a in awards if a["competition_id"] == "good_comp"]
    assert len(good_awards) == 1
