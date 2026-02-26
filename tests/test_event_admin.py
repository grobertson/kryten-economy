"""Tests for Sprint 7 admin commands (event start/stop, claim_bounty) — via PM handler."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import pytest_asyncio

from kryten_economy.bounty_manager import BountyManager
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.multiplier_engine import MultiplierEngine
from kryten_economy.pm_handler import PmHandler
from tests.conftest import make_config_dict

CH = "testchannel"


def _make_config(**overrides) -> EconomyConfig:
    return EconomyConfig(**make_config_dict(**overrides))


def _make_event(username: str, message: str, rank: int = 0) -> MagicMock:
    """Create a mock ChatMessageEvent."""
    ev = MagicMock()
    ev.username = username
    ev.channel = CH
    ev.message = message
    ev.rank = rank
    return ev


async def _seed_account(db: EconomyDatabase, username: str, balance: int) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="seed", reason="test seed")


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    *,
    multiplier_engine: MultiplierEngine | None = None,
    bounty_manager: BountyManager | None = None,
) -> PmHandler:
    """Build a PmHandler with Sprint 7 wiring."""
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    return PmHandler(
        config=config,
        database=database,
        client=mock_client,
        presence_tracker=mock_presence,
        logger=logging.getLogger("test"),
        multiplier_engine=multiplier_engine,
        bounty_manager=bounty_manager,
    )


# ═══════════════════════════════════════════════════════════════
#  Event Admin Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_event_start_valid(database: EconomyDatabase, mock_client: MagicMock):
    """Parses args, starts event, announces."""
    cfg = _make_config()
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    mult = MultiplierEngine(cfg, mock_presence, logging.getLogger("test"))

    handler = _make_handler(cfg, database, mock_client, multiplier_engine=mult)

    ev = _make_event("Admin", 'event start 2.0 60 "Double Hour"', rank=4)
    await handler.handle_pm(ev)

    # Should have announced in chat
    mock_client.send_chat.assert_called()
    chat_msg = mock_client.send_chat.call_args[0][1]
    assert "Double Hour" in chat_msg
    assert "2.0" in chat_msg

    # Multiplier should be active
    combined, active = mult.get_combined_multiplier(CH)
    adhoc = [m for m in active if "Double Hour" in m.source]
    assert len(adhoc) == 1
    assert adhoc[0].multiplier == 2.0


@pytest.mark.asyncio
async def test_event_start_bad_multiplier(database: EconomyDatabase, mock_client: MagicMock):
    """Multiplier 0.5 → rejected (must be > 1.0)."""
    cfg = _make_config()
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    mult = MultiplierEngine(cfg, mock_presence, logging.getLogger("test"))

    handler = _make_handler(cfg, database, mock_client, multiplier_engine=mult)

    ev = _make_event("Admin", 'event start 0.5 60 "Bad"', rank=4)
    await handler.handle_pm(ev)

    # PM response should contain rejection
    pm_msg = mock_client.send_pm.call_args[0][2]
    assert "1.0" in pm_msg or "between" in pm_msg.lower()

    # No adhoc event started
    _, active = mult.get_combined_multiplier(CH)
    assert len(active) == 0


@pytest.mark.asyncio
async def test_event_start_bad_duration(database: EconomyDatabase, mock_client: MagicMock):
    """9999 minutes → rejected (max 1440)."""
    cfg = _make_config()
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    mult = MultiplierEngine(cfg, mock_presence, logging.getLogger("test"))

    handler = _make_handler(cfg, database, mock_client, multiplier_engine=mult)

    ev = _make_event("Admin", 'event start 2.0 9999 "Too Long"', rank=4)
    await handler.handle_pm(ev)

    pm_msg = mock_client.send_pm.call_args[0][2]
    assert "1440" in pm_msg or "duration" in pm_msg.lower()


@pytest.mark.asyncio
async def test_event_stop(database: EconomyDatabase, mock_client: MagicMock):
    """Stops active event."""
    cfg = _make_config()
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    mult = MultiplierEngine(cfg, mock_presence, logging.getLogger("test"))

    handler = _make_handler(cfg, database, mock_client, multiplier_engine=mult)

    # Start one first
    mult.start_adhoc_event("Test Event", 2.0, 60)

    ev = _make_event("Admin", "event stop", rank=4)
    await handler.handle_pm(ev)

    # Should announce stop
    mock_client.send_chat.assert_called()

    # PM confirms
    pm_msg = mock_client.send_pm.call_args[0][2]
    assert "stopped" in pm_msg.lower()

    # Adhoc cleared
    _, active = mult.get_combined_multiplier(CH)
    adhoc = [m for m in active if "adhoc:" in m.source]
    assert len(adhoc) == 0


@pytest.mark.asyncio
async def test_event_stop_none_active(database: EconomyDatabase, mock_client: MagicMock):
    """No event → message."""
    cfg = _make_config()
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    mult = MultiplierEngine(cfg, mock_presence, logging.getLogger("test"))

    handler = _make_handler(cfg, database, mock_client, multiplier_engine=mult)

    ev = _make_event("Admin", "event stop", rank=4)
    await handler.handle_pm(ev)

    pm_msg = mock_client.send_pm.call_args[0][2]
    assert "no" in pm_msg.lower() and "active" in pm_msg.lower()


# ═══════════════════════════════════════════════════════════════
#  Claim Bounty Admin Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_claim_bounty_valid(database: EconomyDatabase, mock_client: MagicMock):
    """Admin claims bounty for user."""
    cfg = _make_config(bounties={"enabled": True, "min_amount": 100, "max_amount": 50000,
                                  "max_open_per_user": 3, "default_expiry_hours": 168,
                                  "expiry_refund_percent": 50, "description_max_length": 200})
    bounty_mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))

    await _seed_account(database, "Creator", 5000)
    await _seed_account(database, "Winner", 0)

    r = await bounty_mgr.create_bounty("Creator", CH, 500, "Test bounty")
    bid = r["bounty_id"]

    handler = _make_handler(cfg, database, mock_client, bounty_manager=bounty_mgr)

    mock_client.send_pm.reset_mock()
    mock_client.send_chat.reset_mock()

    ev = _make_event("Admin", f"claim_bounty {bid} @Winner", rank=4)
    await handler.handle_pm(ev)

    # Winner should be credited
    acc = await database.get_account("Winner", CH)
    assert acc["balance"] == 500


@pytest.mark.asyncio
async def test_claim_bounty_non_admin(database: EconomyDatabase, mock_client: MagicMock):
    """Rank < 4 → rejected."""
    cfg = _make_config(bounties={"enabled": True, "min_amount": 100, "max_amount": 50000,
                                  "max_open_per_user": 3, "default_expiry_hours": 168,
                                  "expiry_refund_percent": 50, "description_max_length": 200})
    bounty_mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))

    await _seed_account(database, "Creator", 5000)
    r = await bounty_mgr.create_bounty("Creator", CH, 500, "Test")
    bid = r["bounty_id"]

    handler = _make_handler(cfg, database, mock_client, bounty_manager=bounty_mgr)

    mock_client.send_pm.reset_mock()

    ev = _make_event("LowRank", f"claim_bounty {bid} @Winner", rank=2)
    await handler.handle_pm(ev)

    # Should get rejection
    pm_msg = mock_client.send_pm.call_args[0][2]
    assert "admin" in pm_msg.lower() or "privileges" in pm_msg.lower()
