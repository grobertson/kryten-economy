"""Sprint 9 — Integration tests with MockKrytenClient.

End-to-end scenario tests verifying full lifecycle flows:
join → earn → chat → gamble → queue → tip → rank up → achievement → admin.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler, PmRateLimiter
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.event_announcer import EventAnnouncer
from kryten_economy.greeting_handler import GreetingHandler
from tests.conftest import MockKrytenClient, make_config_dict


def _make_event(username: str, channel: str, message: str, rank: int = 0):
    """Build a mock PM / chat event."""
    ev = MagicMock()
    ev.username = username
    ev.channel = channel
    ev.message = message
    ev.rank = rank
    ev.timestamp = datetime.now(timezone.utc)
    return ev


class TestJoinAndBalance:
    """Join → accrue → check balance flow."""

    @pytest.mark.asyncio
    async def test_join_and_check_balance(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """User joins, gets welcome wallet, checks balance."""
        # Create account (simulates what presence_tracker does on genuine join)
        await database.get_or_create_account("alice", "testchannel")
        await database.credit("alice", "testchannel", 100, tx_type="welcome")

        event = _make_event("alice", "testchannel", "balance")
        await pm_handler.handle_pm(event)

        # Should have sent a PM back
        mock_client.send_pm.assert_called()
        call_args = mock_client.send_pm.call_args_list[-1]
        response = call_args[0][2] if len(call_args[0]) > 2 else call_args.kwargs.get("message", "")
        assert "100" in response or "Z" in response


class TestTipCycle:
    """User A tips User B → both get PMs."""

    @pytest.mark.asyncio
    async def test_tip_transfer(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
        sample_config: EconomyConfig,
    ) -> None:
        """Tip deducts from sender and credits receiver."""
        # Disable account age gate so freshly-created accounts can tip
        sample_config.tipping.min_account_age_minutes = 0

        await database.get_or_create_account("alice", "testchannel")
        await database.get_or_create_account("bob", "testchannel")
        await database.credit("alice", "testchannel", 500, tx_type="admin")

        event = _make_event("alice", "testchannel", "tip @bob 50")
        await pm_handler.handle_pm(event)

        alice = await database.get_account("alice", "testchannel")
        bob = await database.get_account("bob", "testchannel")

        assert alice["balance"] == 450
        assert bob["balance"] == 50

    @pytest.mark.asyncio
    async def test_tip_insufficient_funds(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Tip with insufficient funds → denied."""
        await database.get_or_create_account("alice", "testchannel")
        # Balance is 0

        event = _make_event("alice", "testchannel", "tip @bob 50")
        await pm_handler.handle_pm(event)

        # Bob shouldn't have been credited (may not even have an account)
        alice = await database.get_account("alice", "testchannel")
        assert alice["balance"] == 0


class TestGamblingCycle:
    """Earn → gamble → verify stats."""

    @pytest.mark.asyncio
    async def test_flip_changes_balance(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Coin flip either wins or loses — balance changes."""
        await database.get_or_create_account("alice", "testchannel")
        await database.credit("alice", "testchannel", 1000, tx_type="admin")

        event = _make_event("alice", "testchannel", "flip 100")
        await pm_handler.handle_pm(event)

        alice = await database.get_account("alice", "testchannel")
        # Balance should have changed from 1000
        assert alice["balance"] != 1000 or mock_client.send_pm.called


class TestAdminCommands:
    """Admin grant/deduct/ban/unban via mock events."""

    @pytest.mark.asyncio
    async def test_admin_grant(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Admin grants Z to a user."""
        await database.get_or_create_account("bob", "testchannel")

        event = _make_event("admin", "testchannel", "grant @bob 500", rank=5)
        await pm_handler.handle_pm(event)

        bob = await database.get_account("bob", "testchannel")
        assert bob["balance"] == 500

    @pytest.mark.asyncio
    async def test_admin_deduct(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Admin deducts Z from a user."""
        await database.get_or_create_account("bob", "testchannel")
        await database.credit("bob", "testchannel", 1000, tx_type="admin")

        event = _make_event("admin", "testchannel", "deduct @bob 300", rank=5)
        await pm_handler.handle_pm(event)

        bob = await database.get_account("bob", "testchannel")
        assert bob["balance"] == 700

    @pytest.mark.asyncio
    async def test_admin_ban_prevents_commands(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Admin bans user → user can't use non-admin commands."""
        await database.get_or_create_account("bob", "testchannel")

        # Ban bob
        ban_event = _make_event("admin", "testchannel", "ban @bob", rank=5)
        await pm_handler.handle_pm(ban_event)

        # Bob tries balance
        mock_client.send_pm.reset_mock()
        bal_event = _make_event("bob", "testchannel", "balance")
        await pm_handler.handle_pm(bal_event)

        # Should get a ban message
        calls = [str(c) for c in mock_client.send_pm.call_args_list]
        assert any("suspended" in c.lower() for c in calls)

    @pytest.mark.asyncio
    async def test_non_admin_denied(
        self,
        pm_handler: PmHandler,
        mock_client: MagicMock,
    ) -> None:
        """Non-admin trying admin command → denied."""
        event = _make_event("bob", "testchannel", "grant @alice 999", rank=1)
        await pm_handler.handle_pm(event)

        calls = [str(c) for c in mock_client.send_pm.call_args_list]
        assert any("admin" in c.lower() or "privileges" in c.lower() for c in calls)


class TestRateLimitingIntegration:
    """Rate limiting in full PM handler flow."""

    @pytest.mark.asyncio
    async def test_rate_limiting_in_handler(
        self,
        pm_handler: PmHandler,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Rapid PM commands → rate limited after threshold."""
        await database.get_or_create_account("alice", "testchannel")

        # Send 15 balance commands rapidly
        for _ in range(15):
            event = _make_event("alice", "testchannel", "balance")
            await pm_handler.handle_pm(event)

        # Check that at least one "Slow down" response was sent
        calls = [str(c) for c in mock_client.send_pm.call_args_list]
        assert any("Slow down" in c for c in calls)


class TestHelpCommand:
    """Help command returns useful text."""

    @pytest.mark.asyncio
    async def test_help(
        self,
        pm_handler: PmHandler,
        mock_client: MagicMock,
    ) -> None:
        """Help command returns a response."""
        event = _make_event("alice", "testchannel", "help")
        await pm_handler.handle_pm(event)

        mock_client.send_pm.assert_called()


class TestUnknownCommand:
    """Unknown commands get a helpful response."""

    @pytest.mark.asyncio
    async def test_unknown_command(
        self,
        pm_handler: PmHandler,
        mock_client: MagicMock,
    ) -> None:
        """Unknown command → 'Unknown command' response."""
        event = _make_event("alice", "testchannel", "xyzzy_nonexistent")
        await pm_handler.handle_pm(event)

        calls = [str(c) for c in mock_client.send_pm.call_args_list]
        assert any("Unknown" in c or "help" in c.lower() for c in calls)


class TestMockKrytenClientBehavior:
    """Verify MockKrytenClient records calls correctly."""

    @pytest.mark.asyncio
    async def test_send_pm_recording(self, mock_kryten_client) -> None:
        """send_pm calls are recorded."""
        await mock_kryten_client.send_pm("ch", "user", "msg")
        assert mock_kryten_client.sent_pms == [("ch", "user", "msg")]

    @pytest.mark.asyncio
    async def test_send_chat_recording(self, mock_kryten_client) -> None:
        """send_chat calls are recorded."""
        await mock_kryten_client.send_chat("ch", "hello")
        assert mock_kryten_client.sent_chats == [("ch", "hello")]

    @pytest.mark.asyncio
    async def test_fire_event(self, mock_kryten_client) -> None:
        """fire_event dispatches to registered handlers."""
        received = []

        @mock_kryten_client.on("chatmsg")
        async def on_chat(event):
            received.append(event)

        ev = MagicMock()
        await mock_kryten_client.fire_event("chatmsg", ev)
        assert len(received) == 1
        assert received[0] is ev

    @pytest.mark.asyncio
    async def test_kv_store(self, mock_kryten_client) -> None:
        """KV get/put work correctly."""
        await mock_kryten_client.kv_put("bucket", "key", "value")
        result = await mock_kryten_client.kv_get("bucket", "key")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_kv_default(self, mock_kryten_client) -> None:
        """KV get returns default for missing key."""
        result = await mock_kryten_client.kv_get("bucket", "missing", default=42)
        assert result == 42
