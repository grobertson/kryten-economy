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


# ══════════════════════════════════════════════════════════════
#  Basic start / join / guard tests
# ══════════════════════════════════════════════════════════════


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
    """Join active heist → sentinel returned with crew size, debited."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)

    bal_before = (await database.get_account("Bob", CH))["balance"]
    result = await gambling_engine.join_heist("Bob", CH, 100)

    assert result.startswith("heist_joined:")
    assert ":2" in result  # crew size = 2
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
async def test_heist_one_per_channel(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Start second heist while one active → error."""
    gambling_engine._config.gambling.heist.enabled = True
    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    result = await gambling_engine.start_heist("Bob", CH, 100)

    assert "already in progress" in result.lower()


# ══════════════════════════════════════════════════════════════
#  Outcome tests (win / loss / push / cancel)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_heist_success_crew_scaled(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Win → crew-scaled multiplier applied.  2 players → 1.5 + 0.25 = 1.75x."""
    cfg = gambling_engine._config.gambling.heist
    cfg.enabled = True
    cfg.min_participants = 2
    cfg.payout_multiplier = 1.5
    cfg.crew_bonus_per_player = 0.25

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    await gambling_engine.join_heist("Bob", CH, 100)

    alice_before = (await database.get_account("Alice", CH))["balance"]
    bob_before = (await database.get_account("Bob", CH))["balance"]

    with patch("random.random", return_value=0.1):  # < 0.4 = success
        with patch("random.choice", side_effect=lambda seq: seq[0]):
            result = await gambling_engine.resolve_heist(CH)

    assert result is not None
    lines, participants = result
    assert len(participants) == 2
    # At least one line should reference payouts
    full = " ".join(lines)
    assert "💰" in full

    alice_after = (await database.get_account("Alice", CH))["balance"]
    bob_after = (await database.get_account("Bob", CH))["balance"]

    # Expected: 100 * (1.5 + (2-1)*0.25) = 100 * 1.75 = 175
    assert alice_after == alice_before + 175
    assert bob_after == bob_before + 175


@pytest.mark.asyncio
async def test_heist_failure_dramatic(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Loss → dramatic lines, wagers forfeited."""
    cfg = gambling_engine._config.gambling.heist
    cfg.enabled = True
    cfg.min_participants = 2
    cfg.push_chance = 0.0  # disable push so 0.5 lands in loss

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    await gambling_engine.join_heist("Bob", CH, 100)

    with patch("random.random", return_value=0.9):  # > success+push = loss
        with patch("random.choice", side_effect=lambda seq: seq[0]):
            result = await gambling_engine.resolve_heist(CH)

    assert result is not None
    lines, _ = result
    full = " ".join(lines)
    assert "🚨" in full or "lost" in full.lower()


@pytest.mark.asyncio
async def test_heist_push(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Push → 95% refund (5% fee)."""
    cfg = gambling_engine._config.gambling.heist
    cfg.enabled = True
    cfg.min_participants = 2
    cfg.success_chance = 0.40
    cfg.push_chance = 0.15
    cfg.push_fee_pct = 0.05

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 1000)
    await gambling_engine.join_heist("Bob", CH, 1000)

    alice_before = (await database.get_account("Alice", CH))["balance"]

    # roll 0.45 → between 0.40 (success boundary) and 0.55 (push boundary)
    with patch("random.random", return_value=0.45):
        with patch("random.choice", side_effect=lambda seq: seq[0]):
            result = await gambling_engine.resolve_heist(CH)

    assert result is not None
    lines, participants = result
    full = " ".join(lines)
    assert "😰" in full or "refund" in full.lower()

    alice_after = (await database.get_account("Alice", CH))["balance"]
    # 1000 * 0.95 = 950 refunded
    assert alice_after == alice_before + 950


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
    lines, participants = result
    full = " ".join(lines)
    assert "cancelled" in full.lower()

    bal_after = (await database.get_account("Alice", CH))["balance"]
    assert bal_after == bal_before  # Refunded


# ══════════════════════════════════════════════════════════════
#  Cooldown tests
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_heist_cooldown_after_resolve(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """After heist resolves, cooldown prevents immediate restart."""
    cfg = gambling_engine._config.gambling.heist
    cfg.enabled = True
    cfg.min_participants = 2
    cfg.cooldown_seconds = 180

    await _seed_account(database, "Alice")
    await _seed_account(database, "Bob")

    await gambling_engine.start_heist("Alice", CH, 100)
    await gambling_engine.join_heist("Bob", CH, 100)

    with patch("random.random", return_value=0.1):
        with patch("random.choice", side_effect=lambda seq: seq[0]):
            await gambling_engine.resolve_heist(CH)

    # Cooldown should be active
    remaining = gambling_engine.get_heist_cooldown_remaining(CH)
    assert remaining > 0

    # Trying to start returns cooldown sentinel
    result = await gambling_engine.start_heist("Alice", CH, 100)
    assert result.startswith("heist_cooldown:")


@pytest.mark.asyncio
async def test_heist_cooldown_expires(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """After cooldown expires, heist can start again."""
    cfg = gambling_engine._config.gambling.heist
    cfg.enabled = True
    cfg.min_participants = 1
    cfg.cooldown_seconds = 180

    await _seed_account(database, "Alice")

    await gambling_engine.start_heist("Alice", CH, 100)
    with patch("random.random", return_value=0.1):
        with patch("random.choice", side_effect=lambda seq: seq[0]):
            await gambling_engine.resolve_heist(CH)

    # Simulate cooldown expiring
    gambling_engine._heist_cooldowns[CH] = (
        datetime.now(timezone.utc) - timedelta(seconds=200)
    )

    assert gambling_engine.get_heist_cooldown_remaining(CH) == 0

    result = await gambling_engine.start_heist("Alice", CH, 100)
    assert result.startswith("heist_started:")


# ══════════════════════════════════════════════════════════════
#  Stats & crew multiplier
# ══════════════════════════════════════════════════════════════


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
        with patch("random.choice", side_effect=lambda seq: seq[0]):
            await gambling_engine.resolve_heist(CH)

    alice_stats = await database.get_gambling_stats("Alice", CH)
    bob_stats = await database.get_gambling_stats("Bob", CH)

    assert alice_stats is not None
    assert alice_stats["total_heists"] == 1
    assert bob_stats is not None
    assert bob_stats["total_heists"] == 1


def test_crew_multiplier(gambling_engine: GamblingEngine):
    """Crew multiplier scales correctly."""
    cfg = gambling_engine._config.gambling.heist
    cfg.payout_multiplier = 1.5
    cfg.crew_bonus_per_player = 0.25

    # 1 player: 1.5 + 0 = 1.5
    assert gambling_engine._heist_crew_multiplier(1) == pytest.approx(1.5)
    # 3 players: 1.5 + 0.5 = 2.0
    assert gambling_engine._heist_crew_multiplier(3) == pytest.approx(2.0)
    # 5 players: 1.5 + 1.0 = 2.5
    assert gambling_engine._heist_crew_multiplier(5) == pytest.approx(2.5)


def test_scenario_text_pools(gambling_engine: GamblingEngine):
    """Ensure all scenario pools are non-empty and contain {user} placeholder."""
    for pool_name in ("HEIST_SCENARIOS", "HEIST_JOIN_LINES"):
        pool = getattr(gambling_engine, pool_name)
        assert len(pool) > 0
        for line in pool:
            assert "{user}" in line, f"{pool_name}: missing {{user}} in: {line}"

    for pool_name in ("HEIST_WIN_LINES", "HEIST_LOSE_LINES", "HEIST_PUSH_LINES"):
        pool = getattr(gambling_engine, pool_name)
        assert len(pool) > 0
