"""Tests for kryten_economy.command_handler module."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.command_handler import CommandHandler
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker


@pytest.fixture
def mock_app(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> MagicMock:
    """Mock EconomyApp with real database."""
    app = MagicMock()
    app.config = sample_config
    app.db = database
    app.client = mock_client
    app.logger = logging.getLogger("test.app")
    app.commands_processed = 0
    app.uptime_seconds = 42.5
    app.presence_tracker = PresenceTracker(
        config=sample_config,
        database=database,
        client=mock_client,
        logger=logging.getLogger("test.presence"),
    )
    return app


@pytest.fixture
def handler(mock_app: MagicMock, mock_client: MagicMock) -> CommandHandler:
    """Create CommandHandler."""
    return CommandHandler(mock_app, mock_client, logging.getLogger("test.cmd"))


class TestCommandHandler:
    """Request-reply command tests."""

    async def test_ping(self, handler: CommandHandler):
        """system.ping should return pong."""
        result = await handler._handle_command({"command": "system.ping"})
        assert result["success"] is True
        assert result["data"]["pong"] is True

    async def test_health(self, handler: CommandHandler):
        """system.health should return status details."""
        result = await handler._handle_command({"command": "system.health"})
        assert result["success"] is True
        assert result["data"]["status"] == "healthy"
        assert "uptime_seconds" in result["data"]

    async def test_balance_get(self, handler: CommandHandler, database: EconomyDatabase):
        """balance.get should return account details."""
        await database.get_or_create_account("alice", "testchannel")
        await database.credit("alice", "testchannel", 999, "earn")
        result = await handler._handle_command({
            "command": "balance.get",
            "username": "alice",
            "channel": "testchannel",
        })
        assert result["success"] is True
        assert result["data"]["found"] is True
        assert result["data"]["balance"] == 999

    async def test_balance_get_not_found(self, handler: CommandHandler):
        """balance.get for nonexistent user should return found=False."""
        result = await handler._handle_command({
            "command": "balance.get",
            "username": "ghost",
            "channel": "testchannel",
        })
        assert result["success"] is True
        assert result["data"]["found"] is False

    async def test_balance_get_missing_params(self, handler: CommandHandler):
        """balance.get without required params should error."""
        result = await handler._handle_command({"command": "balance.get"})
        assert result["success"] is False
        assert "required" in result["error"].lower()

    async def test_unknown_command(self, handler: CommandHandler):
        """Unknown command should return error."""
        result = await handler._handle_command({"command": "nonexistent"})
        assert result["success"] is False
        assert "Unknown" in result["error"]

    async def test_connect(self, handler: CommandHandler, mock_client: MagicMock):
        """connect() should subscribe on the command subject."""
        await handler.connect()
        mock_client.subscribe_request_reply.assert_called_once_with(
            "kryten.economy.command",
            handler._handle_command,
        )
