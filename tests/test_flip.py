"""Tests for coin flip (flip)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kryten_economy.database import EconomyDatabase
from kryten_economy.gambling_engine import GambleOutcome, GamblingEngine

CH = "testchannel"


async def _seed_account(db: EconomyDatabase, username: str = "Alice", balance: int = 5000) -> None:
    """Create account with generous balance and old enough age."""
    await db.get_or_create_account(username, CH)
    await db.credit(username, CH, balance - 100, tx_type="test", reason="seed")

    import asyncio
    loop = asyncio.get_running_loop()
    first_seen = datetime.now(timezone.utc) - timedelta(hours=2)

    def _set():
        conn = db._get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET first_seen = ? WHERE username = ? AND channel = ?",
                (first_seen.isoformat(), username, CH),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _set)


@pytest.mark.asyncio
async def test_flip_win(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Forced random < 0.45 → doubled wager."""
    await _seed_account(database)

    with patch("random.random", return_value=0.2):
        result = await gambling_engine.flip("Alice", CH, 100)
    assert result.outcome == GambleOutcome.WIN
    assert result.payout == 200
    assert result.net == 100


@pytest.mark.asyncio
async def test_flip_loss(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Forced random >= 0.45 → lost wager."""
    await _seed_account(database)

    with patch("random.random", return_value=0.5):
        result = await gambling_engine.flip("Alice", CH, 100)
    assert result.outcome == GambleOutcome.LOSS
    assert result.payout == 0
    assert result.net == -100


@pytest.mark.asyncio
async def test_flip_cooldown_enforced(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Second flip within 15s → rejected."""
    await _seed_account(database)

    with patch("random.random", return_value=0.5):
        await gambling_engine.flip("Alice", CH, 50)
        result = await gambling_engine.flip("Alice", CH, 50)
    assert "cooldown" in result.message.lower()


@pytest.mark.asyncio
async def test_flip_daily_limit(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """101st flip → rejected."""
    await _seed_account(database, balance=100_000)

    for _ in range(100):
        gambling_engine._cooldowns.pop(("alice", "flip"), None)
        with patch("random.random", return_value=0.5):
            await gambling_engine.flip("Alice", CH, 10)

    gambling_engine._cooldowns.pop(("alice", "flip"), None)
    with patch("random.random", return_value=0.5):
        result = await gambling_engine.flip("Alice", CH, 10)
    assert "daily limit" in result.message.lower()


@pytest.mark.asyncio
async def test_flip_balance_updates(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Win: +wager. Loss: -wager."""
    await _seed_account(database, balance=1000)

    bal_before = (await database.get_account("Alice", CH))["balance"]

    with patch("random.random", return_value=0.2):
        result = await gambling_engine.flip("Alice", CH, 100)
    assert result.outcome == GambleOutcome.WIN

    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before + 100  # net +100


@pytest.mark.asyncio
async def test_flip_gambling_stats_updated(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """total_flips incremented."""
    await _seed_account(database)

    with patch("random.random", return_value=0.5):
        await gambling_engine.flip("Alice", CH, 50)

    stats = await database.get_gambling_stats("Alice", CH)
    assert stats is not None
    assert stats["total_flips"] == 1
