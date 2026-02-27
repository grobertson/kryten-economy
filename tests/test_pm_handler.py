"""Tests for kryten_economy.pm_handler module."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker


@pytest.fixture
def pm_handler(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> PmHandler:
    """Create PmHandler with test dependencies."""
    tracker = PresenceTracker(
        config=sample_config,
        database=database,
        client=mock_client,
        logger=logging.getLogger("test.presence"),
    )
    return PmHandler(
        config=sample_config,
        database=database,
        client=mock_client,
        presence_tracker=tracker,
        logger=logging.getLogger("test.pm"),
    )


def make_pm_event(username: str, message: str, channel: str = "testchannel") -> MagicMock:
    """Create a mock ChatMessageEvent for PM testing."""
    event = MagicMock()
    event.username = username
    event.message = message
    event.channel = channel
    event.domain = "cytu.be"
    event.timestamp = "2026-01-01T00:00:00"
    event.rank = 1
    event.correlation_id = "test-corr-1"
    return event


class TestPmDispatch:
    """PM command routing and filtering."""

    async def test_help_command(self, pm_handler: PmHandler, mock_client: MagicMock):
        """'help' should send help text via PM."""
        event = make_pm_event("Alice", "help")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_called()
        # Help may be split into multiple PMs; first chunk has header
        args = mock_client.send_pm.call_args_list[0]
        assert "Economy Bot" in args[0][2]

    async def test_balance_command(self, pm_handler: PmHandler, mock_client: MagicMock, database: EconomyDatabase):
        """'balance' should show current balance."""
        await database.get_or_create_account("Alice", "testchannel")
        await database.credit("Alice", "testchannel", 500, "earn")
        event = make_pm_event("Alice", "balance")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_called_once()
        response = mock_client.send_pm.call_args[0][2]
        assert "500" in response

    async def test_bal_alias(self, pm_handler: PmHandler, mock_client: MagicMock, database: EconomyDatabase):
        """'bal' should work as alias for 'balance'."""
        await database.get_or_create_account("Alice", "testchannel")
        event = make_pm_event("Alice", "bal")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_called_once()
        response = mock_client.send_pm.call_args[0][2]
        assert "Balance" in response

    async def test_unknown_command(self, pm_handler: PmHandler, mock_client: MagicMock):
        """Unknown command should get error response."""
        event = make_pm_event("Alice", "foobar")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_called_once()
        response = mock_client.send_pm.call_args[0][2]
        assert "Unknown" in response

    async def test_empty_message(self, pm_handler: PmHandler, mock_client: MagicMock):
        """Empty message should be ignored."""
        event = make_pm_event("Alice", "   ")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_not_called()

    async def test_ignored_user(self, pm_handler: PmHandler, mock_client: MagicMock):
        """Ignored users should be silently skipped."""
        event = make_pm_event("IgnoredBot", "balance")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_not_called()

    async def test_self_message(self, pm_handler: PmHandler, mock_client: MagicMock):
        """Messages from bot itself should be ignored."""
        event = make_pm_event("TestBot", "balance")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_not_called()

    async def test_case_insensitive_command(self, pm_handler: PmHandler, mock_client: MagicMock):
        """Commands should be case-insensitive."""
        event = make_pm_event("Alice", "HELP")
        await pm_handler.handle_pm(event)
        mock_client.send_pm.assert_called()
        # Help may be split; first chunk has header
        response = mock_client.send_pm.call_args_list[0][0][2]
        assert "Economy Bot" in response

    async def test_balance_shows_rank(self, pm_handler: PmHandler, mock_client: MagicMock, database: EconomyDatabase):
        """Balance response should include rank name."""
        await database.get_or_create_account("Alice", "testchannel")
        event = make_pm_event("Alice", "balance")
        await pm_handler.handle_pm(event)
        response = mock_client.send_pm.call_args[0][2]
        assert "Extra" in response  # Default rank
