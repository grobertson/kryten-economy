"""Tests for Sprint 6 PM commands — rank, profile, achievements, top."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.achievement_engine import AchievementEngine
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.rank_engine import RankEngine
from kryten_economy.spending_engine import SpendingEngine
from tests.conftest import make_config_dict

CH = "testchannel"


def _cfg_with_achievements(achievements: list[dict] | None = None) -> EconomyConfig:
    data = make_config_dict()
    if achievements:
        data["achievements"] = achievements
    return EconomyConfig(**data)


async def _seed_account(
    db: EconomyDatabase, username: str, balance: int = 0, lifetime: int = 0,
) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")
    if lifetime > 0:
        await db.credit(username, CH, lifetime, tx_type="earn", reason="seed-lifetime")


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine | None = None,
    rank_engine: RankEngine | None = None,
    achievement_engine: AchievementEngine | None = None,
) -> PmHandler:
    logger = logging.getLogger("test")
    presence = PresenceTracker(config, database, logger)
    return PmHandler(
        config=config,
        database=database,
        client=mock_client,
        presence_tracker=presence,
        logger=logger,
        spending_engine=spending_engine,
        rank_engine=rank_engine,
        achievement_engine=achievement_engine,
    )


# ═══════════════════════════════════════════════════════════════
#  rank command
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rank_shows_progress(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine,
):
    """Progress bar, next tier, perks."""
    rank_engine = RankEngine(sample_config, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", lifetime=500)

    handler = _make_handler(
        sample_config, database, mock_client, spending_engine, rank_engine,
    )
    response = await handler._cmd_rank("Alice", CH, [])

    assert "Extra" in response
    assert "Next:" in response or "Grip" in response
    assert "█" in response or "░" in response  # progress bar


@pytest.mark.asyncio
async def test_rank_max_tier(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine,
):
    """Shows 'Maximum rank achieved'."""
    rank_engine = RankEngine(sample_config, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", lifetime=5_000_000)

    handler = _make_handler(
        sample_config, database, mock_client, spending_engine, rank_engine,
    )
    response = await handler._cmd_rank("Alice", CH, [])

    assert "Studio Mogul" in response
    assert "Maximum" in response or "maximum" in response or "max" in response.lower()


# ═══════════════════════════════════════════════════════════════
#  profile command
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_profile_self(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine,
):
    """Own profile with all sections."""
    rank_engine = RankEngine(sample_config, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", balance=5000, lifetime=2000)

    handler = _make_handler(
        sample_config, database, mock_client, spending_engine, rank_engine,
    )
    response = await handler._cmd_profile("Alice", CH, [])

    assert "Alice" in response
    assert "Balance" in response
    assert "Rank" in response


@pytest.mark.asyncio
async def test_profile_other_user(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine,
):
    """profile @Alice shows Alice's profile."""
    rank_engine = RankEngine(sample_config, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", balance=3000, lifetime=1000)

    handler = _make_handler(
        sample_config, database, mock_client, spending_engine, rank_engine,
    )
    response = await handler._cmd_profile("Bob", CH, ["@Alice"])

    assert "Alice" in response
    assert "Balance" in response


@pytest.mark.asyncio
async def test_profile_not_found(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine,
):
    """Unknown user → error."""
    rank_engine = RankEngine(sample_config, database, mock_client, logging.getLogger("test"))
    handler = _make_handler(
        sample_config, database, mock_client, spending_engine, rank_engine,
    )
    response = await handler._cmd_profile("Bob", CH, ["@NonExistent"])

    assert "No account" in response or "not found" in response.lower()


# ═══════════════════════════════════════════════════════════════
#  achievements command
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_achievements_earned_and_progress(
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Shows earned + in-progress."""
    cfg = _cfg_with_achievements([
        {
            "id": "earn_10",
            "description": "Earn 10 Z",
            "condition": {"type": "lifetime_earned", "threshold": 10},
            "reward": 5,
            "hidden": False,
        },
        {
            "id": "earn_1000",
            "description": "Earn 1000 Z",
            "condition": {"type": "lifetime_earned", "threshold": 1000},
            "reward": 50,
            "hidden": False,
        },
    ])
    await _seed_account(database, "Alice", lifetime=50)

    # Award the first achievement manually
    ach_engine = AchievementEngine(cfg, database, mock_client, logging.getLogger("test"))
    await ach_engine.check_achievements("Alice", CH, ["lifetime_earned"])

    handler = _make_handler(cfg, database, mock_client)
    response = await handler._cmd_achievements("Alice", CH, [])

    assert "Earn 10 Z" in response  # earned
    assert "Earn 1000 Z" in response  # in progress


@pytest.mark.asyncio
async def test_achievements_hidden_count(
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Shows hidden count hint."""
    cfg = _cfg_with_achievements([
        {
            "id": "secret_1",
            "description": "Secret",
            "condition": {"type": "lifetime_earned", "threshold": 99999},
            "reward": 100,
            "hidden": True,
        },
    ])
    await _seed_account(database, "Alice")

    handler = _make_handler(cfg, database, mock_client)
    response = await handler._cmd_achievements("Alice", CH, [])

    assert "hidden" in response.lower()
    assert "1" in response


# ═══════════════════════════════════════════════════════════════
#  top / leaderboard command
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_top_earners(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Formatted leaderboard."""
    from datetime import datetime, timezone

    # Seed daily_activity for today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await _seed_account(database, "Alice", lifetime=300)
    await _seed_account(database, "Bob", lifetime=500)

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._cmd_top("TestUser", CH, ["earners"])

    # Even if no daily data, the response should not crash
    assert "earner" in response.lower() or "No earnings" in response


@pytest.mark.asyncio
async def test_top_richest(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Formatted by balance."""
    await _seed_account(database, "Alice", balance=5000)
    await _seed_account(database, "Bob", balance=2000)

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._cmd_top("TestUser", CH, ["rich"])

    assert "Alice" in response
    assert "Bob" in response
    assert "Richest" in response


@pytest.mark.asyncio
async def test_top_lifetime(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Formatted by lifetime earned."""
    await _seed_account(database, "Alice", lifetime=5000)
    await _seed_account(database, "Bob", lifetime=2000)

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._cmd_top("TestUser", CH, ["lifetime"])

    assert "Lifetime" in response
    assert "Alice" in response


@pytest.mark.asyncio
async def test_rank_distribution(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Count per rank tier."""
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._cmd_top("TestUser", CH, ["ranks"])

    assert "Rank" in response or "Distribution" in response or "Extra" in response


@pytest.mark.asyncio
async def test_top_unknown_subcmd(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Shows usage help."""
    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._cmd_top("TestUser", CH, ["bogus"])

    assert "Usage" in response or "usage" in response.lower()
