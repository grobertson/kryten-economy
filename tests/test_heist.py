"""Tests for the heist system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kryten_economy.database import EconomyDatabase
from kryten_economy.gambling_engine import GamblingEngine

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase, username: str, balance: int = 5000,
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
async def test_heist_disabled(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Config disabled → error."""
    await _seed_account(database, "Alice")

    result = await gambling_engine.start_heist("Alice", CH, 100)
    assert "disabled" in result.lower()


@pytest.mark.asyncio
async def test_start_heist(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Valid → heist created, initiator debited, sentinel returned."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")

    bal_before = (await database.get_account("Alice", CH))["balance"]
    result = await gambling_engine.start_heist("Alice", CH, 100)

    assert result.startswith("heist_started:")
    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before - 100

    heist = gambling_engine.get_active_heist(CH)
    assert heist is not None
    assert "Alice" in heist.participants


@pytest.mark.asyncio
async def test_join_heist(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Join active heist → participant added, debited."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)

    bal_before = (await database.get_account("Bob", CH))["balance"]
    result = await gambling_engine.join_heist("Bob", CH, 100)

    assert "joined" in result.lower()
    bal_after = (await database.get_account("Bob", CH))["balance"]
    assert bal_after == bal_before - 100


@pytest.mark.asyncio
async def test_join_heist_already_in(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Already participating → error."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")

    await gambling_engine.start_heist("Alice", CH, 100)
    result = await gambling_engine.join_heist("Alice", CH, 100)

    assert "already" in result.lower()


@pytest.mark.asyncio
async def test_join_heist_expired_window(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Join after window → error."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)

    # Expire the join window
    heist = gambling_engine.get_active_heist(CH)
    heist.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    result = await gambling_engine.join_heist("Bob", CH, 100)
    assert "closed" in result.lower()


@pytest.mark.asyncio
async def test_heist_success(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Random < 0.4 → all participants get wager * 1.5."""
    gambling_engine._config.gambling.heist.enabled = True
    gambling_engine._config.gambling.heist.min_participants = 2

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    await gambling_engine.join_heist("Bob", CH, 100)

    alice_before = (await database.get_account("Alice", CH))["balance"]
    bob_before = (await database.get_account("Bob", CH))["balance"]

    with patch("random.random", return_value=0.1):  # < 0.4 = success
        result = await gambling_engine.resolve_heist(CH)

    assert result is not None
    public_msg, participants = result
    assert "success" in public_msg.lower()
    assert len(participants) == 2

    alice_after = (await database.get_account("Alice", CH))["balance"]
    bob_after = (await database.get_account("Bob", CH))["balance"]

    # Each gets 100 * 1.5 = 150 credited
    assert alice_after == alice_before + 150
    assert bob_after == bob_before + 150


@pytest.mark.asyncio
async def test_heist_failure(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Random >= 0.4 → all participants lose wager."""
    gambling_engine._config.gambling.heist.enabled = True
    gambling_engine._config.gambling.heist.min_participants = 2

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    await gambling_engine.join_heist("Bob", CH, 100)

    with patch("random.random", return_value=0.9):  # >= 0.4 = failure
        result = await gambling_engine.resolve_heist(CH)

    assert result is not None
    public_msg, _ = result
    assert "failed" in public_msg.lower()


@pytest.mark.asyncio
async def test_heist_insufficient_participants(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """< min_participants → cancelled, everyone refunded."""
    gambling_engine._config.gambling.heist.enabled = True
    # min_participants defaults to 3, only Alice joins

    await _seed_account(database, "Alice")

    bal_before = (await database.get_account("Alice", CH))["balance"]
    await gambling_engine.start_heist("Alice", CH, 100)

    result = await gambling_engine.resolve_heist(CH)
    assert result is not None
    public_msg, participants = result
    assert "cancelled" in public_msg.lower()

    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before  # Refunded


@pytest.mark.asyncio
async def test_heist_one_per_channel(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Start second heist while one active → error."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    result = await gambling_engine.start_heist("Bob", CH, 100)

    assert "already in progress" in result.lower()


@pytest.mark.asyncio
async def test_heist_stats_recorded(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """total_heists incremented for all participants."""
    gambling_engine._config.gambling.heist.enabled = True
    gambling_engine._config.gambling.heist.min_participants = 2

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    await gambling_engine.join_heist("Bob", CH, 100)

    with patch("random.random", return_value=0.1):
        await gambling_engine.resolve_heist(CH)

    alice_stats = await database.get_gambling_stats("Alice", CH)
    bob_stats = await database.get_gambling_stats("Bob", CH)

    assert alice_stats is not None
    assert alice_stats["total_heists"] == 1
    assert bob_stats is not None
    assert bob_stats["total_heists"] == 1
