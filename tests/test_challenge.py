"""Tests for the challenge system."""

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
async def test_create_challenge_success(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Valid challenge → pending row created, challenger debited."""
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    bal_before = (await database.get_account("Alice", CH))["balance"]
    result = await gambling_engine.create_challenge("Alice", "Bob", CH, 200)

    assert result.startswith("challenge_created:")
    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before - 200


@pytest.mark.asyncio
async def test_challenge_self_rejected(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Challenge self → error."""
    await _seed_account(database, "Alice")

    result = await gambling_engine.create_challenge("Alice", "Alice", CH, 200)
    assert "yourself" in result.lower()


@pytest.mark.asyncio
async def test_challenge_ignored_user_rejected(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Challenge ignored user → error."""
    await _seed_account(database, "Alice")
    # "IgnoredBot" is in config.ignored_users

    result = await gambling_engine.create_challenge("Alice", "IgnoredBot", CH, 200)
    assert "can't be challenged" in result.lower()


@pytest.mark.asyncio
async def test_challenge_target_insufficient_balance(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Target can't afford → error."""
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob", balance=100)

    result = await gambling_engine.create_challenge("Alice", "Bob", CH, 500)
    assert "can't afford" in result.lower()


@pytest.mark.asyncio
async def test_challenge_duplicate_rejected(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Existing pending → error."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob")

    result1 = await gambling_engine.create_challenge("Alice", "Bob", CH, 200)
    assert result1.startswith("challenge_created:")

    result2 = await gambling_engine.create_challenge("Alice", "Bob", CH, 200)
    assert "already" in result2.lower()


@pytest.mark.asyncio
async def test_accept_challenge_success(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Accept → both debited, winner credited (minus rake)."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=10000)

    await gambling_engine.create_challenge("Alice", "Bob", CH, 500)

    with patch("random.random", return_value=0.2):  # challenger wins (< 0.5)
        target_msg, challenger_msg, public_msg = await gambling_engine.accept_challenge(
            "Bob", CH,
        )

    assert target_msg is not None
    assert challenger_msg is not None
    # Verify rake is mentioned or implicit in totals
    assert "⚔️" in target_msg


@pytest.mark.asyncio
async def test_accept_challenge_expired(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Accept after timeout → 'expired' + refund."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=10000)

    await gambling_engine.create_challenge("Alice", "Bob", CH, 200)

    # Expire the challenge directly in DB
    import asyncio
    loop = asyncio.get_running_loop()

    def _expire():
        conn = database._get_connection()
        try:
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            conn.execute(
                "UPDATE pending_challenges SET expires_at = ? WHERE status = 'pending'",
                (past,),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _expire)

    target_msg, _, _ = await gambling_engine.accept_challenge("Bob", CH)
    assert "expired" in target_msg.lower()


@pytest.mark.asyncio
async def test_decline_challenge_refund(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Decline → challenger refunded, both notified."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=10000)

    bal_before = (await database.get_account("Alice", CH))["balance"]
    await gambling_engine.create_challenge("Alice", "Bob", CH, 300)

    target_msg, challenger_msg = await gambling_engine.decline_challenge("Bob", CH)

    assert "declined" in target_msg.lower()
    assert challenger_msg is not None
    assert "refunded" in challenger_msg.lower()

    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before  # Refunded


@pytest.mark.asyncio
async def test_challenge_rake_calculated(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """5% of (wager x 2) removed from pool."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=10000)

    wager = 500
    await gambling_engine.create_challenge("Alice", "Bob", CH, wager)

    alice_before = (await database.get_account("Alice", CH))["balance"]
    bob_before = (await database.get_account("Bob", CH))["balance"]

    with patch("random.random", return_value=0.2):  # challenger (Alice) wins
        await gambling_engine.accept_challenge("Bob", CH)

    alice_after = (await database.get_account("Alice", CH))["balance"]
    bob_after = (await database.get_account("Bob", CH))["balance"]

    # Total pot = 1000, rake = 50, prize = 950
    # Alice started at alice_before (already debited 500 for challenge creation)
    # Alice wins 950
    alice_net_change = alice_after - alice_before
    assert alice_net_change == 950  # credited the prize

    # Bob debited 500 on accept, gets nothing
    bob_net_change = bob_after - bob_before
    assert bob_net_change == -500


@pytest.mark.asyncio
async def test_challenge_result_announced(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """announce_public = true → public message returned."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=10000)

    # Ensure announce_public is True
    gambling_engine._config.gambling.challenge.announce_public = True

    await gambling_engine.create_challenge("Alice", "Bob", CH, 200)

    with patch("random.random", return_value=0.2):
        _, _, public_msg = await gambling_engine.accept_challenge("Bob", CH)

    assert public_msg is not None
    assert "defeated" in public_msg.lower()


@pytest.mark.asyncio
async def test_challenge_expiry_cleanup(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Expired challenges auto-refund and return info."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=10000)

    bal_before = (await database.get_account("Alice", CH))["balance"]
    await gambling_engine.create_challenge("Alice", "Bob", CH, 200)

    # Expire challenge
    import asyncio
    loop = asyncio.get_running_loop()

    def _expire():
        conn = database._get_connection()
        try:
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            conn.execute(
                "UPDATE pending_challenges SET expires_at = ? WHERE status = 'pending'",
                (past,),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _expire)

    expired = await gambling_engine.cleanup_expired_challenges(CH)
    assert len(expired) >= 1

    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before  # Refunded


@pytest.mark.asyncio
async def test_challenge_no_pending(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Accept with no pending → error."""
    await _seed_account(database, "Bob")

    target_msg, _, _ = await gambling_engine.accept_challenge("Bob", CH)
    assert "no pending" in target_msg.lower()


@pytest.mark.asyncio
async def test_accept_target_insufficient_now(
    gambling_engine: GamblingEngine, database: EconomyDatabase,
):
    """Target could afford at creation but not now → error on accept."""
    await _seed_account(database, "Alice", balance=10000)
    await _seed_account(database, "Bob", balance=600)

    await gambling_engine.create_challenge("Alice", "Bob", CH, 500)

    # Drain Bob's balance so he can't afford
    await database.atomic_debit("Bob", CH, 200)

    target_msg, _, _ = await gambling_engine.accept_challenge("Bob", CH)
    assert "can't afford" in target_msg.lower() or "insufficient" in target_msg.lower()
