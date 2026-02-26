"""Tests for gambling stats display."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kryten_economy.database import EconomyDatabase
from kryten_economy.gambling_engine import GamblingEngine

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase, username: str = "Alice", balance: int = 5000,
) -> None:
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
async def test_stats_no_gambling(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """No prior gambling → friendly message."""
    await _seed_account(database)
    msg = await gambling_engine.get_stats_message("Alice", CH)
    assert "haven't gambled" in msg.lower()


@pytest.mark.asyncio
async def test_stats_after_spin(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """After a spin → total_spins = 1."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        await gambling_engine.spin("Alice", CH, 50)

    msg = await gambling_engine.get_stats_message("Alice", CH)
    assert "Spins: 1" in msg


@pytest.mark.asyncio
async def test_stats_net_positive(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Wins > losses → positive net displayed."""
    await _seed_account(database, balance=10000)

    # Force a big win (flip)
    with patch("random.random", return_value=0.1):
        await gambling_engine.flip("Alice", CH, 500)

    msg = await gambling_engine.get_stats_message("Alice", CH)
    assert "+" in msg


@pytest.mark.asyncio
async def test_stats_net_negative(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Losses > wins → negative net displayed."""
    await _seed_account(database, balance=10000)

    with patch("random.random", return_value=0.99):
        await gambling_engine.spin("Alice", CH, 500)

    msg = await gambling_engine.get_stats_message("Alice", CH)
    assert "-" in msg


@pytest.mark.asyncio
async def test_biggest_win_tracked(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Largest win recorded."""
    await _seed_account(database, balance=10000)

    with patch("random.random", return_value=0.1):
        result = await gambling_engine.flip("Alice", CH, 200)

    if result.net > 0:
        stats = await database.get_gambling_stats("Alice", CH)
        assert stats["biggest_win"] >= result.net


@pytest.mark.asyncio
async def test_biggest_loss_tracked(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Largest loss recorded."""
    await _seed_account(database, balance=10000)

    with patch("random.random", return_value=0.99):
        await gambling_engine.spin("Alice", CH, 300)

    stats = await database.get_gambling_stats("Alice", CH)
    assert stats["biggest_loss"] >= 300


@pytest.mark.asyncio
async def test_stats_combines_all_games(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Spins + flips → all totals shown."""
    await _seed_account(database, balance=100_000)

    with patch("random.random", return_value=0.99):
        await gambling_engine.spin("Alice", CH, 50)

    gambling_engine._cooldowns.clear()

    with patch("random.random", return_value=0.5):
        await gambling_engine.flip("Alice", CH, 50)

    msg = await gambling_engine.get_stats_message("Alice", CH)
    assert "Spins: 1" in msg
    assert "Flips: 1" in msg
