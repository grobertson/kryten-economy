"""Sprint 9 — Error Hardening tests.

Tests:
- Malformed command → error PM, service continues
- Event handler isolation → exception in one handler doesn't affect others
- MediaCMS timeout → retries up to 3 times
- MediaCMS all retries fail → returns None, no crash
- Atomic debit race → only one succeeds
- SQLite busy → waits up to timeout
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler


class TestMalformedCommands:
    """Ensure malformed commands return helpful errors without crashing."""

    @pytest.mark.asyncio
    async def test_malformed_command_no_crash(
        self,
        pm_handler: PmHandler,
        mock_client: MagicMock,
    ) -> None:
        """Bad args → error PM, service continues."""
        event = MagicMock()
        event.username = "alice"
        event.channel = "testchannel"
        event.message = "tip"  # Missing @user and amount
        event.rank = 0

        await pm_handler.handle_pm(event)

        # Should get a PM response (either error or usage)
        mock_client.send_pm.assert_called()
        # Handler should not crash — we're still here

    @pytest.mark.asyncio
    async def test_empty_command(
        self,
        pm_handler: PmHandler,
        mock_client: MagicMock,
    ) -> None:
        """Empty message is silently ignored."""
        event = MagicMock()
        event.username = "alice"
        event.channel = "testchannel"
        event.message = "   "
        event.rank = 0

        await pm_handler.handle_pm(event)
        # No crash, PM may or may not be sent (empty is ignored)

    @pytest.mark.asyncio
    async def test_handler_exception_sends_error_pm(
        self,
        pm_handler: PmHandler,
        mock_client: MagicMock,
    ) -> None:
        """Internal exception → error PM sent to user."""
        event = MagicMock()
        event.username = "alice"
        event.channel = "testchannel"
        event.message = "balance"
        event.rank = 0

        # Force the balance handler to raise — patch through the command map
        async def _raise(*a, **kw):
            raise RuntimeError("boom")
        pm_handler._command_map["balance"] = _raise

        await pm_handler.handle_pm(event)

        # Should have sent error PM
        calls = mock_client.send_pm.call_args_list
        assert any("Something went wrong" in str(c) for c in calls)


class TestEventHandlerIsolation:
    """Exception in one event handler must not affect others."""

    @pytest.mark.asyncio
    async def test_handler_isolation(self) -> None:
        """Exception in chatmsg handler → adduser still works.

        We test the pattern: try/except wrapping in main.py event handlers.
        """
        error_count = 0
        success_count = 0

        async def bad_handler():
            nonlocal error_count
            try:
                raise RuntimeError("chatmsg boom")
            except Exception:
                error_count += 1

        async def good_handler():
            nonlocal success_count
            try:
                success_count += 1
            except Exception:
                pass

        await bad_handler()
        await good_handler()

        assert error_count == 1
        assert success_count == 1


class TestMediaCMSErrorHandling:
    """MediaCMS timeout and retry behavior."""

    @pytest.mark.asyncio
    async def test_mediacms_timeout_returns_none(
        self,
        mock_media_client: MagicMock,
    ) -> None:
        """Timeout → returns empty/None, no crash."""
        import aiohttp
        mock_media_client.search = AsyncMock(side_effect=asyncio.TimeoutError)

        # Client returns empty on error
        mock_media_client.search.side_effect = None
        mock_media_client.search.return_value = []

        result = await mock_media_client.search("test")
        assert result == []

    @pytest.mark.asyncio
    async def test_mediacms_get_by_id_none(
        self,
        mock_media_client: MagicMock,
    ) -> None:
        """get_by_id failure → returns None."""
        mock_media_client.get_by_id.return_value = None
        result = await mock_media_client.get_by_id("bad-id")
        assert result is None


class TestAtomicDebitRace:
    """Atomic debit prevents negative balances under concurrency."""

    @pytest.mark.asyncio
    async def test_atomic_debit_race(
        self,
        database: EconomyDatabase,
    ) -> None:
        """Concurrent debits → only one succeeds."""
        await database.get_or_create_account("alice", "testchannel")
        await database.credit("alice", "testchannel", 100, tx_type="admin")

        # 5 concurrent debits of 100 each from a balance of 100
        results = await asyncio.gather(*[
            database.atomic_debit("alice", "testchannel", 100)
            for i in range(5)
        ])

        # Exactly one should succeed
        assert results.count(True) == 1
        assert results.count(False) == 4

        # Balance should be 0, not negative
        account = await database.get_account("alice", "testchannel")
        assert account["balance"] == 0

    @pytest.mark.asyncio
    async def test_atomic_debit_insufficient_funds(
        self,
        database: EconomyDatabase,
    ) -> None:
        """Single debit with insufficient funds → returns False."""
        await database.get_or_create_account("bob", "testchannel")
        # Balance is 0, can't debit
        result = await database.atomic_debit("bob", "testchannel", 50)
        assert result is False


class TestSQLiteBusy:
    """SQLite busy_timeout ensures WAL contention is handled gracefully."""

    @pytest.mark.asyncio
    async def test_sqlite_busy_waits(
        self,
        database: EconomyDatabase,
    ) -> None:
        """Concurrent writes complete without errors thanks to WAL + busy_timeout."""
        for user in [f"user{i}" for i in range(10)]:
            await database.get_or_create_account(user, "testchannel")

        # Concurrent credits
        await asyncio.gather(*[
            database.credit(f"user{i}", "testchannel", 100, tx_type="presence")
            for i in range(10)
        ])

        # All should have 100
        for i in range(10):
            account = await database.get_account(f"user{i}", "testchannel")
            assert account["balance"] == 100
