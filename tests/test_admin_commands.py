"""Tests for Sprint 8 admin commands — economy control."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.earning_engine import EarningEngine
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker

CH = "testchannel"


class FakePmEvent:
    """Minimal stand-in for ChatMessageEvent used by handle_pm."""

    def __init__(self, username: str, message: str, channel: str = CH, rank: int = 0):
        self.username = username
        self.message = message
        self.channel = channel
        self.rank = rank
        self.timestamp = datetime.now(timezone.utc)


# ── grant ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_success(pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock):
    """Admin grant credits target, logs transaction, PMs target."""
    await database.get_or_create_account("alice", CH)

    result = await pm_handler._cmd_grant("admin", CH, ["alice", "500", "good", "work"])
    assert "500" in result
    assert "alice" in result

    account = await database.get_account("alice", CH)
    assert account["balance"] == 500  # 0 start + 500 grant

    # Check target was PMed
    mock_client.send_pm.assert_called()
    pm_args = mock_client.send_pm.call_args_list[-1]
    assert "500" in str(pm_args)


@pytest.mark.asyncio
async def test_grant_non_admin(pm_handler: PmHandler, mock_client: MagicMock):
    """CyTube rank < owner_level → rejected."""
    event = FakePmEvent("lowrank", "grant alice 100", rank=1)
    await pm_handler.handle_pm(event)

    # Should send "admin privileges" rejection
    mock_client.send_pm.assert_called()
    msg = mock_client.send_pm.call_args[0][2] if len(mock_client.send_pm.call_args[0]) > 2 else str(mock_client.send_pm.call_args)
    assert "admin" in msg.lower() or "privilege" in msg.lower()


@pytest.mark.asyncio
async def test_grant_missing_args(pm_handler: PmHandler):
    """Grant with insufficient args returns usage."""
    result = await pm_handler._cmd_grant("admin", CH, ["alice"])
    assert "Usage" in result


# ── deduct ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deduct_success(pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock):
    """Admin deduct debits target, logs transaction."""
    await database.get_or_create_account("bob", CH)
    # Bob starts with 0
    await database.credit("bob", CH, 100, tx_type="admin_grant", trigger_id="seed")
    result = await pm_handler._cmd_deduct("admin", CH, ["bob", "30"])
    assert "30" in result
    assert "bob" in result

    account = await database.get_account("bob", CH)
    assert account["balance"] == 70


@pytest.mark.asyncio
async def test_deduct_insufficient(pm_handler: PmHandler, database: EconomyDatabase):
    """Target has insufficient balance → error."""
    await database.get_or_create_account("charlie", CH)
    result = await pm_handler._cmd_deduct("admin", CH, ["charlie", "9999"])
    assert "insufficient" in result.lower()


# ── rain ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rain_distributes(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
    presence_tracker: PresenceTracker,
):
    """Splits among present users, PMs each, announces."""
    # Seed users and mark as present
    for user in ["alice", "bob", "charlie"]:
        await database.get_or_create_account(user, CH)
        await database.credit(user, CH, 100, tx_type="seed", trigger_id="seed")
        await presence_tracker.handle_user_join(user, CH)

    result = await pm_handler._cmd_rain("admin", CH, ["300"])
    assert "300" in result or "100" in result  # 300 total or 100 each
    assert "3 users" in result

    # Each user should have gotten 100 rain on top of 100 seed
    for user in ["alice", "bob", "charlie"]:
        account = await database.get_account(user, CH)
        assert account["balance"] == 200  # 100 seed + 100 rain

    # Public chat announcement
    mock_client.send_chat.assert_called()


@pytest.mark.asyncio
async def test_rain_no_users(pm_handler: PmHandler):
    """No present users → "No users present"."""
    result = await pm_handler._cmd_rain("admin", CH, ["100"])
    assert "No users present" in result


# ── set_balance ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_balance(pm_handler: PmHandler, database: EconomyDatabase):
    """Hard-sets balance, logs diff as transaction."""
    await database.get_or_create_account("dave", CH)
    await database.credit("dave", CH, 100, tx_type="seed", trigger_id="seed")
    result = await pm_handler._cmd_set_balance("admin", CH, ["dave", "5000"])
    assert "5,000" in result

    account = await database.get_account("dave", CH)
    assert account["balance"] == 5000


# ── set_rank ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_rank_valid(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Updates rank_name, PMs target."""
    await database.get_or_create_account("eve", CH)

    # Get a valid rank name from config
    valid_rank = pm_handler._config.ranks.tiers[0].name

    result = await pm_handler._cmd_set_rank("admin", CH, ["eve", valid_rank])
    assert valid_rank in result

    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_set_rank_invalid(pm_handler: PmHandler, database: EconomyDatabase):
    """Unknown rank name → 'Valid:' list."""
    await database.get_or_create_account("frank", CH)
    result = await pm_handler._cmd_set_rank("admin", CH, ["frank", "Nonexistent"])
    assert "Unknown rank" in result or "Valid" in result


# ── announce ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_announce_sends_chat(pm_handler: PmHandler, mock_client: MagicMock):
    """Sends message via client.send_chat()."""
    result = await pm_handler._cmd_announce("admin", CH, ["Hello", "world!"])
    assert "Hello world!" in result

    mock_client.send_chat.assert_called_once()
    args = mock_client.send_chat.call_args[0]
    assert args[0] == CH
    assert "Hello world!" in args[1]


# ── ban / unban ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ban_user(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Inserts ban, PMs target."""
    await database.get_or_create_account("mallory", CH)
    result = await pm_handler._cmd_ban("admin", CH, ["mallory", "spamming"])
    assert "Banned mallory" in result

    assert await database.is_banned("mallory", CH)
    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_ban_already_banned(pm_handler: PmHandler, database: EconomyDatabase):
    """Already banned → 'already banned'."""
    await database.ban_user("mallory", CH, "admin", "test")
    result = await pm_handler._cmd_ban("admin", CH, ["mallory"])
    assert "already banned" in result


@pytest.mark.asyncio
async def test_unban_user(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Removes ban, PMs target."""
    await database.ban_user("mallory", CH, "admin", "test")
    result = await pm_handler._cmd_unban("admin", CH, ["mallory"])
    assert "Unbanned" in result

    assert not await database.is_banned("mallory", CH)
    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_unban_not_banned(pm_handler: PmHandler, database: EconomyDatabase):
    """Not banned → 'not banned'."""
    result = await pm_handler._cmd_unban("admin", CH, ["ghost"])
    assert "not banned" in result


# ── Ban enforcement ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_banned_user_cannot_command(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Banned user's normal commands return 'suspended'."""
    await database.get_or_create_account("mallory", CH)
    await database.ban_user("mallory", CH, "admin", "test")

    event = FakePmEvent("mallory", "balance")
    await pm_handler.handle_pm(event)

    mock_client.send_pm.assert_called()
    msg = mock_client.send_pm.call_args[0][2] if len(mock_client.send_pm.call_args[0]) > 2 else ""
    assert "suspended" in msg.lower()


@pytest.mark.asyncio
async def test_banned_user_admin_commands_work(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Admin can still run admin commands even if banned (they bypass ban check)."""
    await database.get_or_create_account("owner", CH)
    await database.ban_user("owner", CH, "other_admin", "test")

    # Admin commands go through admin_command_map first, which doesn't check bans
    event = FakePmEvent("owner", "econ:stats", rank=4)
    await pm_handler.handle_pm(event)

    # Should not get "suspended" response
    mock_client.send_pm.assert_called()
    msg = mock_client.send_pm.call_args[0][2] if len(mock_client.send_pm.call_args[0]) > 2 else ""
    assert "suspended" not in msg.lower()


# ── Ban in earning path ────────────────────────────────────────

@pytest.mark.asyncio
async def test_banned_user_cannot_earn(
    earning_engine: EarningEngine, database: EconomyDatabase,
):
    """Earning is silently skipped for banned users."""
    await database.get_or_create_account("mallory", CH)
    await database.ban_user("mallory", CH, "admin", "test")

    before = (await database.get_account("mallory", CH))["balance"]

    outcome = await earning_engine.evaluate_chat_message(
        "mallory", CH, "Hello world this is a long message for testing purposes",
        datetime.now(timezone.utc),
    )

    assert outcome.total_earned == 0
    after = (await database.get_account("mallory", CH))["balance"]
    assert after == before
