"""Tests for GamblingEngine core validation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.gambling_engine import GambleOutcome, GamblingEngine

CH = "testchannel"


async def _make_account(
    db: EconomyDatabase,
    username: str = "Alice",
    balance: int = 1000,
    age_minutes: int = 120,
    banned: bool = False,
) -> None:
    """Create an account with a specific age and balance."""
    first_seen = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    await db.get_or_create_account(username, CH)
    # Set balance directly
    await db.credit(username, CH, balance - 100, tx_type="test", reason="seed")
    # Update first_seen and banned status
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _set_fields():
        conn = db._get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET first_seen = ?, economy_banned = ? "
                "WHERE username = ? AND channel = ?",
                (first_seen.isoformat(), int(banned), username, CH),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _set_fields)


@pytest.mark.asyncio
async def test_gambling_disabled(gambling_engine: GamblingEngine, database: EconomyDatabase, sample_config: EconomyConfig):
    """All games return error when gambling.enabled = false."""
    gambling_engine._config.gambling.enabled = False
    await _make_account(database, "Alice")

    result = await gambling_engine.spin("Alice", CH, 50)
    assert "disabled" in result.message.lower()

    result = await gambling_engine.flip("Alice", CH, 50)
    assert "disabled" in result.message.lower()


@pytest.mark.asyncio
async def test_min_account_age_enforced(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """New account (< 60 min old) → rejected."""
    await _make_account(database, "NewUser", age_minutes=10)

    result = await gambling_engine.spin("NewUser", CH, 50)
    assert "more minutes" in result.message.lower()


@pytest.mark.asyncio
async def test_min_account_age_satisfied(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Account >= 60 min old → allowed (not rejected by age check)."""
    await _make_account(database, "OldUser", age_minutes=120)

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.spin("OldUser", CH, 50)
    # Should not see an age error
    assert "more minutes" not in result.message.lower()


@pytest.mark.asyncio
async def test_economy_banned_rejected(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Banned user → rejected."""
    await _make_account(database, "Banned", banned=True)

    result = await gambling_engine.spin("Banned", CH, 50)
    assert "restricted" in result.message.lower()


@pytest.mark.asyncio
async def test_insufficient_balance(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Balance < wager → rejected."""
    await _make_account(database, "Poor", balance=10)

    result = await gambling_engine.spin("Poor", CH, 500)
    assert "insufficient" in result.message.lower()


@pytest.mark.asyncio
async def test_min_wager_enforced(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Wager below minimum → rejected."""
    await _make_account(database, "Alice")

    result = await gambling_engine.spin("Alice", CH, 1)
    assert "minimum" in result.message.lower()


@pytest.mark.asyncio
async def test_max_wager_enforced(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Wager above maximum → rejected."""
    await _make_account(database, "Alice")

    result = await gambling_engine.spin("Alice", CH, 99999)
    assert "maximum" in result.message.lower()


@pytest.mark.asyncio
async def test_atomic_debit_prevents_overdraft(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Two concurrent spins with balance for one → only one succeeds."""
    await _make_account(database, "Tester", balance=120)

    # Both try to spin 100
    with patch("random.random", return_value=0.99):
        results = await asyncio.gather(
            gambling_engine.spin("Tester", CH, 100),
            gambling_engine.spin("Tester", CH, 100),
        )

    messages = [r.message for r in results]
    # At least one should fail with insufficient funds
    insufficient = sum(1 for m in messages if "insufficient" in m.lower())
    assert insufficient >= 1
