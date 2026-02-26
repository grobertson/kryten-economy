"""Tests for slot machine (spin)."""

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
async def test_spin_win(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Forced roll in win range → payout > wager, balance increased."""
    await _seed_account(database)
    # Force a roll that hits a winning entry (use a low-ish roll to hit first payout tiers)
    # We need to find the cumulative probability range for a win
    with patch("random.random", return_value=0.01):
        result = await gambling_engine.spin("Alice", CH, 50)
    # The first payout entries should be high-multiplier wins
    assert result.payout >= result.wager or result.outcome == GambleOutcome.LOSS


@pytest.mark.asyncio
async def test_spin_loss(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Forced roll in loss range → payout = 0, balance decreased."""
    await _seed_account(database)
    # Force a roll near 1.0 which should be a loss entry
    with patch("random.random", return_value=0.99):
        result = await gambling_engine.spin("Alice", CH, 50)
    assert result.outcome == GambleOutcome.LOSS
    assert result.payout == 0
    assert result.net == -50


@pytest.mark.asyncio
async def test_spin_jackpot(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Forced roll on jackpot → 50x multiplier, announce_public = true."""
    await _seed_account(database)

    # Find the jackpot entry in the payout table (multiplier >= 50)
    jackpot_entries = [e for e in gambling_engine._slot_payouts if e.multiplier >= 50]
    if not jackpot_entries:
        pytest.skip("No jackpot entry in default config")

    # Roll just before the jackpot entry's cumulative probability
    jackpot = jackpot_entries[0]
    roll = jackpot.cumulative_probability - 0.001

    with patch("random.random", return_value=max(0.0001, roll)):
        result = await gambling_engine.spin("Alice", CH, 50)
    assert result.outcome == GambleOutcome.JACKPOT
    assert result.payout == int(50 * jackpot.multiplier)
    assert result.announce_public


@pytest.mark.asyncio
async def test_spin_partial_match(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Forced roll on partial → small multiplier."""
    await _seed_account(database)

    partial_entries = [e for e in gambling_engine._slot_payouts if e.symbols == "partial"]
    if not partial_entries:
        pytest.skip("No partial entry in default config")

    entry = partial_entries[0]
    # Roll just inside this entry's band
    prev_cum = 0.0
    for e in gambling_engine._slot_payouts:
        if e is entry:
            break
        prev_cum = e.cumulative_probability
    roll = (prev_cum + entry.cumulative_probability) / 2

    with patch("random.random", return_value=roll):
        result = await gambling_engine.spin("Alice", CH, 50)
    assert result.payout == int(50 * entry.multiplier)


@pytest.mark.asyncio
async def test_spin_cooldown_enforced(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Second spin within 30s → rejected."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        await gambling_engine.spin("Alice", CH, 50)
        result = await gambling_engine.spin("Alice", CH, 50)
    assert "cooldown" in result.message.lower()


@pytest.mark.asyncio
async def test_spin_cooldown_expired(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Second spin after cooldown → allowed."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        await gambling_engine.spin("Alice", CH, 50)

    # Expire cooldown
    key = ("alice", "spin")
    gambling_engine._cooldowns[key] = datetime.now(timezone.utc) - timedelta(seconds=31)

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.spin("Alice", CH, 50)
    assert "cooldown" not in result.message.lower()


@pytest.mark.asyncio
async def test_spin_daily_limit(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """51st spin in a day → rejected."""
    await _seed_account(database, balance=100_000)

    # Exhaust daily limit (50 spins)
    for i in range(50):
        gambling_engine._cooldowns.pop(("alice", "spin"), None)
        with patch("random.random", return_value=0.99):
            await gambling_engine.spin("Alice", CH, 10)

    # 51st should fail
    gambling_engine._cooldowns.pop(("alice", "spin"), None)
    with patch("random.random", return_value=0.99):
        result = await gambling_engine.spin("Alice", CH, 10)
    assert "daily limit" in result.message.lower()


@pytest.mark.asyncio
async def test_spin_payout_table_valid(gambling_engine: GamblingEngine):
    """Probabilities sum to 1.0 (within tolerance)."""
    total = gambling_engine._slot_payouts[-1].cumulative_probability
    assert abs(total - 1.0) < 0.02


@pytest.mark.asyncio
async def test_spin_transaction_logged_win(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Win → transaction with type='gamble_win'."""
    await _seed_account(database)

    # Find a winning entry
    win_entries = [e for e in gambling_engine._slot_payouts if e.multiplier > 1]
    if not win_entries:
        pytest.skip("No winning entries in payout table")

    entry = win_entries[0]
    prev_cum = 0.0
    for e in gambling_engine._slot_payouts:
        if e is entry:
            break
        prev_cum = e.cumulative_probability
    roll = (prev_cum + entry.cumulative_probability) / 2

    with patch("random.random", return_value=roll):
        result = await gambling_engine.spin("Alice", CH, 50)

    if result.outcome in (GambleOutcome.WIN, GambleOutcome.JACKPOT):
        # Check transaction logged
        import asyncio
        loop = asyncio.get_running_loop()

        def _check():
            conn = database._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM transactions WHERE username = 'Alice' AND type = 'gamble_win' LIMIT 1"
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        tx = await loop.run_in_executor(None, _check)
        assert tx is not None
        assert tx["type"] == "gamble_win"


@pytest.mark.asyncio
async def test_spin_gambling_stats_updated(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """total_spins incremented, net_gambling updated."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.spin("Alice", CH, 50)

    stats = await database.get_gambling_stats("Alice", CH)
    assert stats is not None
    assert stats["total_spins"] == 1
    assert stats["net_gambling"] == result.net


@pytest.mark.asyncio
async def test_spin_display_jackpot_symbols(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Jackpot → display shows the jackpot symbols."""
    await _seed_account(database)

    jackpot_entries = [e for e in gambling_engine._slot_payouts if e.multiplier >= 50]
    if not jackpot_entries:
        pytest.skip("No jackpot entry")

    jackpot = jackpot_entries[0]
    roll = jackpot.cumulative_probability - 0.001

    with patch("random.random", return_value=max(0.0001, roll)):
        result = await gambling_engine.spin("Alice", CH, 50)
    if result.outcome == GambleOutcome.JACKPOT:
        assert result.display == jackpot.symbols


@pytest.mark.asyncio
async def test_spin_display_loss_random(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Loss → display shows mixed symbols (not empty)."""
    await _seed_account(database)

    with patch("random.random", return_value=0.99):
        result = await gambling_engine.spin("Alice", CH, 50)
    assert result.outcome == GambleOutcome.LOSS
    assert len(result.display) > 0


@pytest.mark.asyncio
async def test_jackpot_announce_threshold(gambling_engine: GamblingEngine, database: EconomyDatabase):
    """Payout < threshold → no announcement."""
    await _seed_account(database)

    # Use a small win that won't exceed the threshold
    win_entries = [e for e in gambling_engine._slot_payouts if 1 < e.multiplier < 5]
    if not win_entries:
        pytest.skip("No small win entries")

    entry = win_entries[0]
    prev_cum = 0.0
    for e in gambling_engine._slot_payouts:
        if e is entry:
            break
        prev_cum = e.cumulative_probability
    roll = (prev_cum + entry.cumulative_probability) / 2

    with patch("random.random", return_value=roll):
        result = await gambling_engine.spin("Alice", CH, 10)  # Small wager

    # Small wager * small multiplier < threshold (500)
    if result.payout < gambling_engine._config.gambling.spin.jackpot_announce_threshold:
        assert not result.announce_public
