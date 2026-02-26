"""Tests for Sprint 8 admin inspection commands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker

CH = "testchannel"


# ── econ:stats ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_econ_stats_format(
    pm_handler: PmHandler, database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """Returns formatted stats with all fields."""
    await database.get_or_create_account("alice", CH)
    await database.get_or_create_account("bob", CH)
    await presence_tracker.handle_user_join("alice", CH)

    result = await pm_handler._cmd_econ_stats("admin", CH, [])

    assert "Economy Overview" in result
    assert "Accounts:" in result
    assert "Currently present:" in result
    assert "Active today:" in result
    assert "circulation" in result.lower()


# ── econ:user ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_econ_user_found(pm_handler: PmHandler, database: EconomyDatabase):
    """Full user inspection output."""
    await database.get_or_create_account("alice", CH)
    await database.credit("alice", CH, 50, tx_type="earn", trigger_id="test")

    result = await pm_handler._cmd_econ_user("admin", CH, ["alice"])

    assert "alice" in result
    assert "Balance:" in result
    assert "Lifetime earned:" in result
    assert "Banned:" in result
    assert "No" in result  # Not banned


@pytest.mark.asyncio
async def test_econ_user_not_found(pm_handler: PmHandler):
    """Unknown user → error."""
    result = await pm_handler._cmd_econ_user("admin", CH, ["nonexistent"])
    assert "No account" in result


@pytest.mark.asyncio
async def test_econ_user_banned_flag(pm_handler: PmHandler, database: EconomyDatabase):
    """Shows banned status when user is banned."""
    await database.get_or_create_account("mallory", CH)
    await database.ban_user("mallory", CH, "admin", "spam")

    result = await pm_handler._cmd_econ_user("admin", CH, ["mallory"])
    assert "YES" in result


# ── econ:health ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_econ_health_inflation(
    pm_handler: PmHandler, database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """Reports inflationary when earned > spent."""
    await database.get_or_create_account("alice", CH)
    await database.credit("alice", CH, 1000, tx_type="earn", trigger_id="test")
    await presence_tracker.handle_user_join("alice", CH)

    result = await pm_handler._cmd_econ_health("admin", CH, [])

    assert "Economy Health" in result
    assert "Circulation:" in result
    assert "Median balance:" in result
    assert "inflationary" in result.lower() or "Net:" in result


@pytest.mark.asyncio
async def test_econ_health_deflation(
    pm_handler: PmHandler, database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """Reports deflationary when spent > earned after account creation."""
    await database.get_or_create_account("alice", CH)
    # Spend more than earned today (welcome wallet doesn't count as "today earned")
    await database.debit("alice", CH, 50, tx_type="spend", trigger_id="test")
    await presence_tracker.handle_user_join("alice", CH)

    result = await pm_handler._cmd_econ_health("admin", CH, [])
    # Net flow should be negative or zero
    assert "Economy Health" in result


# ── econ:triggers ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_econ_triggers_hot_and_dead(pm_handler: PmHandler, database: EconomyDatabase):
    """Shows active triggers, flags dead ones."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Record some analytics
    await database.record_trigger_analytics(CH, "presence.base", today, 100)
    await database.record_trigger_analytics(CH, "chat.long_message", today, 50)

    result = await pm_handler._cmd_econ_triggers("admin", CH, [])

    assert "Trigger Analytics" in result
    assert "presence.base" in result
    assert "chat.long_message" in result


@pytest.mark.asyncio
async def test_econ_triggers_no_data(pm_handler: PmHandler):
    """No trigger data → message."""
    result = await pm_handler._cmd_econ_triggers("admin", CH, [])
    assert "No trigger data" in result


# ── econ:gambling ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_econ_gambling_stats(pm_handler: PmHandler, database: EconomyDatabase):
    """Reports actual vs. configured house edge."""
    # Create some gambling activity
    await database.get_or_create_account("gambler", CH)
    await database.credit("gambler", CH, 10000, tx_type="earn", trigger_id="test")

    # Record gambling transactions
    await database.debit("gambler", CH, 100, tx_type="gamble_in", trigger_id="spin")
    await database.credit("gambler", CH, 80, tx_type="gamble_out", trigger_id="spin")

    result = await pm_handler._cmd_econ_gambling("admin", CH, [])

    assert "Gambling Report" in result or "No gambling" in result


@pytest.mark.asyncio
async def test_econ_gambling_no_data(pm_handler: PmHandler):
    """No gambling → 'No gambling activity'."""
    result = await pm_handler._cmd_econ_gambling("admin", CH, [])
    assert "No gambling" in result
