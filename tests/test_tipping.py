"""Tests for tip command."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.spending_engine import SpendingEngine

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase,
    username: str = "Alice",
    balance: int = 5000,
    age_minutes: int = 120,
) -> None:
    """Create account with given balance and account age."""
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")

    loop = asyncio.get_running_loop()
    first_seen = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)

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


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock | None = None,
    spending_engine: SpendingEngine | None = None,
) -> PmHandler:
    logger = logging.getLogger("test")
    presence = PresenceTracker(config, database, logger)
    return PmHandler(
        config=config,
        database=database,
        client=mock_client,
        presence_tracker=presence,
        logger=logger,
        spending_engine=spending_engine,
    )


# ═══════════════════════════════════════════════════════════════
#  Success
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tip_success(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Tip deducts from sender, credits receiver, records tip."""
    await _seed_account(database, "Alice", 5000)
    await _seed_account(database, "Bob", 1000)
    handler = _make_handler(sample_config, database, mock_client)

    resp = await handler._cmd_tip("Alice", CH, ["Bob", "100"])
    assert "Tipped" in resp or "tipped" in resp.lower()
    assert "100" in resp

    alice = await database.get_account("Alice", CH)
    bob = await database.get_account("Bob", CH)
    assert alice["balance"] == 4900
    assert bob["balance"] == 1100

    # PM sent to receiver
    calls = [c for c in mock_client.send_pm.call_args_list if c.args[1] == "Bob"]
    assert len(calls) >= 1


# ═══════════════════════════════════════════════════════════════
#  Blocked cases
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tip_self_blocked(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Can't tip yourself."""
    await _seed_account(database, "Alice", 5000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_tip("Alice", CH, ["Alice", "100"])
    assert "yourself" in resp.lower()


@pytest.mark.asyncio
async def test_tip_ignored_user(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Can't tip ignored/bot users."""
    await _seed_account(database, "Alice", 5000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_tip("Alice", CH, ["IgnoredBot", "100"])
    assert "not participating" in resp.lower()


@pytest.mark.asyncio
async def test_tip_insufficient_funds(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Tip more than balance → insufficient."""
    await _seed_account(database, "Alice", 100)
    await _seed_account(database, "Bob", 1000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_tip("Alice", CH, ["Bob", "5000"])
    assert "insufficient" in resp.lower() or "funds" in resp.lower()


@pytest.mark.asyncio
async def test_tip_below_minimum(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Tip less than min_amount → rejected."""
    await _seed_account(database, "Alice", 5000)
    await _seed_account(database, "Bob", 1000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_tip("Alice", CH, ["Bob", "0"])
    assert "minimum" in resp.lower() or "whole number" in resp.lower() or "amount" in resp.lower()


@pytest.mark.asyncio
async def test_tip_daily_cap(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_client: MagicMock,
):
    """Tipping past daily max → blocked."""
    await _seed_account(database, "Whale", 100000)
    await _seed_account(database, "Bob", 1000)
    handler = _make_handler(sample_config, database, mock_client)

    # Tip near the daily limit (5000)
    resp = await handler._cmd_tip("Whale", CH, ["Bob", "4900"])
    assert "tipped" in resp.lower() or "Tipped" in resp

    # Try to tip past the cap
    resp = await handler._cmd_tip("Whale", CH, ["Bob", "200"])
    assert "limit" in resp.lower() or "remaining" in resp.lower()


@pytest.mark.asyncio
async def test_tip_new_account(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Accounts < min_account_age_minutes old can't tip."""
    await _seed_account(database, "Newbie", 5000, age_minutes=5)  # Very new
    await _seed_account(database, "Bob", 1000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_tip("Newbie", CH, ["Bob", "100"])
    assert "too new" in resp.lower()


@pytest.mark.asyncio
async def test_tip_target_no_account(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Target without economy account → blocked."""
    await _seed_account(database, "Alice", 5000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_tip("Alice", CH, ["Nobody", "100"])
    assert "doesn't have" in resp.lower() or "account" in resp.lower()


@pytest.mark.asyncio
async def test_tip_disabled(database: EconomyDatabase):
    """Tipping disabled in config → blocked."""
    from tests.conftest import make_config_dict
    cfg = EconomyConfig(**make_config_dict(tipping={"enabled": False}))
    handler = _make_handler(cfg, database)

    resp = await handler._cmd_tip("Alice", CH, ["Bob", "100"])
    assert "not enabled" in resp.lower()
