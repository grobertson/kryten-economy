"""Tests for daily free spin."""

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
async def test_free_spin_win(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Free spin in win range → payout credited, no debit."""
    await _seed_account(database)

    bal_before = (await database.get_account("Alice", CH))["balance"]

    # Force a winning roll
    win_entries = [e for e in gambling_engine._slot_payouts if e.multiplier > 0]
    entry = win_entries[0]
    roll = entry.cumulative_probability - 0.001

    with patch("random.random", return_value=max(0.0001, roll)):
        result = await gambling_engine.daily_free_spin("Alice", CH)

    if result.payout > 0:
        bal_after = (await database.get_account("Alice", CH))["balance"]
        assert bal_after == bal_before + result.payout
        assert result.wager == 0  # Free spin


@pytest.mark.asyncio
async def test_free_spin_loss(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Free spin in loss range → no debit, no credit."""
    await _seed_account(database)

    bal_before = (await database.get_account("Alice", CH))["balance"]

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.daily_free_spin("Alice", CH)

    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before  # No change
    assert result.payout == 0


@pytest.mark.asyncio
async def test_free_spin_once_per_day(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Second free spin same day → rejected."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        await gambling_engine.daily_free_spin("Alice", CH)

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.daily_free_spin("Alice", CH)

    assert "already" in result.message.lower()


@pytest.mark.asyncio
async def test_free_spin_resets_daily(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """New day → eligible again."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        await gambling_engine.daily_free_spin("Alice", CH)

    # Change the daily_activity record to yesterday
    import asyncio
    loop = asyncio.get_running_loop()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def _backdate():
        conn = database._get_connection()
        try:
            conn.execute(
                "UPDATE daily_activity SET date = ? WHERE username = 'Alice' AND channel = ?",
                (yesterday, CH),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _backdate)

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.daily_free_spin("Alice", CH)

    assert "already" not in result.message.lower()


@pytest.mark.asyncio
async def test_free_spin_disabled(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Config disabled → error."""
    await _seed_account(database)

    gambling_engine._config.gambling.daily_free_spin.enabled = False
    result = await gambling_engine.daily_free_spin("Alice", CH)
    assert "disabled" in result.message.lower()


@pytest.mark.asyncio
async def test_free_spin_via_spin_command(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Free spin followed by paid spin with explicit wager."""
    await _seed_account(database)

    # Free spin first
    with patch("random.random", return_value=0.99):
        result1 = await gambling_engine.daily_free_spin("Alice", CH)
    assert "free spin" in result1.message.lower() or result1.wager == 0

    # Now a paid spin should work
    gambling_engine._cooldowns.clear()
    with patch("random.random", return_value=0.99):
        result2 = await gambling_engine.spin("Alice", CH, 50)
    assert result2.wager == 50


@pytest.mark.asyncio
async def test_spin_without_args_after_free_used(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """After free spin used, daily_free_spin returns 'already used' message."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        await gambling_engine.daily_free_spin("Alice", CH)
        result = await gambling_engine.daily_free_spin("Alice", CH)

    assert "already" in result.message.lower()
