"""Tests for RankEngine — Sprint 6."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.rank_engine import RankEngine
from tests.conftest import make_config_dict

CH = "testchannel"


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 0) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")


def _make_engine(config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> RankEngine:
    return RankEngine(config, database, mock_client, logging.getLogger("test"))


# ═══════════════════════════════════════════════════════════════
#  Rank Tier Lookup
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_initial_rank(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """0 lifetime → 'Extra'."""
    engine = _make_engine(sample_config, database, mock_client)
    idx, tier = engine.get_rank_for_lifetime(0)
    assert idx == 0
    assert tier.name == "Extra"


@pytest.mark.asyncio
async def test_rank_at_threshold(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Exactly 1000 → 'Grip'."""
    engine = _make_engine(sample_config, database, mock_client)
    idx, tier = engine.get_rank_for_lifetime(1000)
    assert tier.name == "Grip"
    assert idx == 1


@pytest.mark.asyncio
async def test_rank_promotion(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Lifetime crosses threshold → promote, PM, announce."""
    engine = _make_engine(sample_config, database, mock_client)
    await _seed_account(database, "Alice")
    # Credit 1500 so lifetime_earned = 1500 → "Grip" (>= 1000)
    await database.credit("Alice", CH, 1500, tx_type="earn", reason="test")

    new_tier = await engine.check_rank_promotion("Alice", CH)
    assert new_tier is not None
    assert new_tier.name == "Grip"

    # Verify DB updated
    acc = await database.get_account("Alice", CH)
    assert acc["rank_name"] == "Grip"

    # PM sent
    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_no_promotion_same_rank(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Already at correct rank → no action."""
    engine = _make_engine(sample_config, database, mock_client)
    await _seed_account(database, "Alice")
    # Credit to get Grip rank
    await database.credit("Alice", CH, 1500, tx_type="earn", reason="test")
    # First promotion
    await engine.check_rank_promotion("Alice", CH)
    mock_client.reset_mock()

    # Second check — same lifetime → no promotion
    result = await engine.check_rank_promotion("Alice", CH)
    assert result is None
    mock_client.send_pm.assert_not_called()


@pytest.mark.asyncio
async def test_max_rank(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """'Studio Mogul' → no next tier."""
    engine = _make_engine(sample_config, database, mock_client)
    # Studio Mogul is the last tier index
    idx, tier = engine.get_rank_for_lifetime(5_000_000)
    assert tier.name == "Studio Mogul"

    next_tier = engine.get_next_tier(idx)
    assert next_tier is None


@pytest.mark.asyncio
async def test_cytube_auto_promotion(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Reaching tier with cytube_level_promotion → calls safe_set_channel_rank()."""
    engine = _make_engine(sample_config, database, mock_client)
    await _seed_account(database, "Alice")
    # Studio Mogul (5M) has cytube_level_promotion=2
    await database.credit("Alice", CH, 5_000_000, tx_type="earn", reason="test")

    await engine.check_rank_promotion("Alice", CH)

    mock_client.safe_set_channel_rank.assert_called_once_with(CH, "Alice", 2)


@pytest.mark.asyncio
async def test_cytube_promotion_failure_logged(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Rank change fails → logged, no crash."""
    mock_client.safe_set_channel_rank = AsyncMock(return_value={"success": False, "error": "test fail"})
    engine = _make_engine(sample_config, database, mock_client)
    await _seed_account(database, "Alice")
    await database.credit("Alice", CH, 5_000_000, tx_type="earn", reason="test")

    # Should not raise
    new_tier = await engine.check_rank_promotion("Alice", CH)
    assert new_tier is not None
    assert new_tier.name == "Studio Mogul"


@pytest.mark.asyncio
async def test_rank_discount_calculation(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Tier 5 discount = 5 × 0.02 = 0.10 (10%)."""
    from kryten_economy.spending_engine import SpendingEngine

    spending = SpendingEngine(
        sample_config, database, MagicMock(), logging.getLogger("test"),
    )
    discount = spending.get_rank_discount(5)
    assert abs(discount - 0.10) < 0.001


@pytest.mark.asyncio
async def test_rank_perks_parsed(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Extra queue slots parsed from perk strings."""
    # Best Boy (index 4) has "+1 queue/day"
    tiers = sample_config.ranks.tiers
    best_boy = tiers[4]
    assert best_boy.name == "Best Boy"
    has_queue_perk = any("queue" in p.lower() for p in best_boy.perks)
    assert has_queue_perk


@pytest.mark.asyncio
async def test_rain_multiplier_parsed(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    """Rain bonus parsed from perk strings."""
    # Gaffer (index 3) has "rain drops +20%"
    tiers = sample_config.ranks.tiers
    gaffer = tiers[3]
    assert gaffer.name == "Gaffer"
    has_rain_perk = any("rain" in p.lower() for p in gaffer.perks)
    assert has_rain_perk
