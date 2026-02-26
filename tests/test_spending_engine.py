"""Tests for SpendingEngine — rank discounts, price tiers, validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.spending_engine import SpendOutcome, SpendResult, SpendingEngine

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase,
    username: str = "Alice",
    balance: int = 50000,
    lifetime: int = 0,
) -> None:
    """Create account with given balance and lifetime earnings."""
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")
    if lifetime > 0:
        import asyncio
        loop = asyncio.get_running_loop()

        def _set():
            conn = db._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET lifetime_earned = ? WHERE username = ? AND channel = ?",
                    (lifetime, username, CH),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _set)


# ═══════════════════════════════════════════════════════════════
#  Rank Discount
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rank_discount_tier_zero(spending_engine: SpendingEngine):
    """Tier 0 (Extra) has 0% discount."""
    assert spending_engine.get_rank_discount(0) == 0.0


@pytest.mark.asyncio
async def test_rank_discount_tier_five(spending_engine: SpendingEngine):
    """Tier 5 (Associate Producer) has 10% discount (5 × 0.02)."""
    discount = spending_engine.get_rank_discount(5)
    assert abs(discount - 0.10) < 0.001


@pytest.mark.asyncio
async def test_rank_discount_tier_nine(spending_engine: SpendingEngine):
    """Tier 9 (Studio Mogul) has 18% discount (9 × 0.02)."""
    discount = spending_engine.get_rank_discount(9)
    assert abs(discount - 0.18) < 0.001


# ═══════════════════════════════════════════════════════════════
#  apply_discount
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_apply_discount_basic(spending_engine: SpendingEngine):
    """100 base cost at tier 5 → 90 (10% off)."""
    final, discount = spending_engine.apply_discount(100, 5)
    assert final == 90
    assert abs(discount - 0.10) < 0.001


@pytest.mark.asyncio
async def test_apply_discount_minimum_one(spending_engine: SpendingEngine):
    """Discount can't reduce cost below 1."""
    final, _ = spending_engine.apply_discount(1, 9)
    assert final >= 1


# ═══════════════════════════════════════════════════════════════
#  Price Tiers
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_price_tier_short(spending_engine: SpendingEngine):
    """≤15 min → 250 Z."""
    label, cost = spending_engine.get_price_tier(600)  # 10 minutes
    assert cost == 250
    assert "short" in label.lower() or "music" in label.lower()


@pytest.mark.asyncio
async def test_price_tier_episode(spending_engine: SpendingEngine):
    """16-35 min → 500 Z."""
    label, cost = spending_engine.get_price_tier(1800)  # 30 minutes
    assert cost == 500


@pytest.mark.asyncio
async def test_price_tier_long_episode(spending_engine: SpendingEngine):
    """36-65 min → 750 Z."""
    label, cost = spending_engine.get_price_tier(3600)  # 60 minutes
    assert cost == 750


@pytest.mark.asyncio
async def test_price_tier_movie(spending_engine: SpendingEngine):
    """>65 min → 1000 Z."""
    label, cost = spending_engine.get_price_tier(7200)  # 120 minutes
    assert cost == 1000


# ═══════════════════════════════════════════════════════════════
#  validate_spend
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_validate_spend_no_account(
    spending_engine: SpendingEngine, database: EconomyDatabase,
):
    """User with no account gets insufficient_funds."""
    outcome = await spending_engine.validate_spend("Nobody", CH, 100, "queue")
    assert outcome is not None
    assert outcome.result == SpendResult.INSUFFICIENT_FUNDS


@pytest.mark.asyncio
async def test_validate_spend_banned(
    spending_engine: SpendingEngine, database: EconomyDatabase,
):
    """Banned user gets permission_denied."""
    await _seed_account(database, "Banned", 10000)
    import asyncio
    loop = asyncio.get_running_loop()

    def _ban():
        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET economy_banned = 1 WHERE username = ? AND channel = ?",
                ("Banned", CH),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _ban)

    outcome = await spending_engine.validate_spend("Banned", CH, 100, "queue")
    assert outcome is not None
    assert outcome.result == SpendResult.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_validate_spend_low_balance(
    spending_engine: SpendingEngine, database: EconomyDatabase,
):
    """Insufficient balance blocked."""
    await _seed_account(database, "Poor", 100)
    outcome = await spending_engine.validate_spend("Poor", CH, 50000, "queue")
    assert outcome is not None
    assert outcome.result == SpendResult.INSUFFICIENT_FUNDS


@pytest.mark.asyncio
async def test_validate_spend_ok(
    spending_engine: SpendingEngine, database: EconomyDatabase,
):
    """Valid spend returns None (all checks pass)."""
    await _seed_account(database, "Rich", 50000)
    outcome = await spending_engine.validate_spend("Rich", CH, 100, "queue")
    assert outcome is None


# ═══════════════════════════════════════════════════════════════
#  get_rank_tier_index
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rank_tier_index_zero(
    spending_engine: SpendingEngine, database: EconomyDatabase,
):
    """New user with 0 lifetime → tier 0."""
    await _seed_account(database, "Newbie", 100, 0)
    account = await database.get_account("Newbie", CH)
    assert spending_engine.get_rank_tier_index(account) == 0


@pytest.mark.asyncio
async def test_rank_tier_index_mid(
    spending_engine: SpendingEngine, database: EconomyDatabase,
):
    """User at 100000 lifetime → tier 5 (Associate Producer)."""
    await _seed_account(database, "Mid", 100, 100000)
    account = await database.get_account("Mid", CH)
    assert spending_engine.get_rank_tier_index(account) == 5
