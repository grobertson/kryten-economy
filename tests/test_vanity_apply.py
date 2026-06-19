"""Tests for chat-color CSS application and the vanity.shoutout command."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.command_handler import CommandHandler
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.spending_engine import SpendingEngine

CH = "testchannel"


@pytest.fixture
def css_client() -> MagicMock:
    """Mock KrytenClient exposing the CSS + chat methods used by the handler."""
    client = MagicMock()
    client.send_chat = AsyncMock(return_value="cid")
    client.subscribe_request_reply = AsyncMock()
    client.get_state_channel_css = AsyncMock(return_value="body { color: #fff; }\n")
    client.set_channel_css = AsyncMock(return_value="cid-css")
    return client


@pytest.fixture
def app(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    spending_engine: SpendingEngine,
    css_client: MagicMock,
) -> MagicMock:
    a = MagicMock()
    a.config = sample_config
    a.db = database
    a.client = css_client
    a.spending_engine = spending_engine
    a.metrics = MagicMock()
    a.commands_processed = 0
    return a


@pytest.fixture
def handler(app: MagicMock, css_client: MagicMock) -> CommandHandler:
    return CommandHandler(app, css_client, logging.getLogger("test.cmd"))


async def _fund(db: EconomyDatabase, user: str, amount: int = 100_000) -> None:
    await db.get_or_create_account(user, CH)
    await db.credit(user, CH, amount, tx_type="test", reason="seed")


class TestChatColorCssApply:
    async def test_purchase_writes_managed_block_with_original_casing(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice")
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#112233",
        })
        assert result["success"] is True
        css_client.set_channel_css.assert_awaited_once()
        pushed = css_client.set_channel_css.await_args.args[1]
        assert ".chat-msg-Alice { color: #112233; }" in pushed
        # Hand-maintained CSS preserved
        assert "body { color: #fff; }" in pushed

    async def test_protected_user_is_never_written(
        self, handler: CommandHandler, database: EconomyDatabase,
        css_client: MagicMock, sample_config: EconomyConfig,
    ):
        sample_config.vanity_shop.chat_color.protected_users = ["FaxyBrown"]
        # FaxyBrown already has a stored color (e.g. a bot), and is present in CSS.
        await database.set_vanity_item("FaxyBrown", CH, "chat_color", "#ff8a24")
        css_client.get_state_channel_css.return_value = (
            "body{}\n.chat-msg-FaxyBrown { color: #ff8a24; }\n"
        )
        await _fund(database, "Alice")
        await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#abcdef",
        })
        pushed = css_client.set_channel_css.await_args.args[1]
        # The managed block must not contain a FaxyBrown rule…
        begin = sample_config.vanity_shop.chat_color.css_block_begin
        managed = pushed.split(begin, 1)[1]
        assert "FaxyBrown" not in managed
        # …but Alice should be there (hex is normalized to upper-case).
        assert ".chat-msg-Alice { color: #ABCDEF; }" in pushed

    async def test_empty_css_read_is_not_written_back(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        css_client.get_state_channel_css.return_value = ""
        await _fund(database, "Alice")
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#112233",
        })
        # Purchase still succeeds, but CSS is never clobbered.
        assert result["success"] is True
        css_client.set_channel_css.assert_not_awaited()

    async def test_apply_disabled_skips_css(
        self, handler: CommandHandler, database: EconomyDatabase,
        css_client: MagicMock, sample_config: EconomyConfig,
    ):
        sample_config.vanity_shop.chat_color.apply_css = False
        await _fund(database, "Alice")
        await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#112233",
        })
        css_client.set_channel_css.assert_not_awaited()


class TestShoutoutCommand:
    async def test_shoutout_delivers_and_debits(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Bob", 100_000)
        result = await handler._handle_command({
            "command": "vanity.shoutout",
            "username": "Bob",
            "channel": CH,
            "value": "hello world",
        })
        assert result["success"] is True
        assert result["data"]["new_balance"] < 100_000
        css_client.send_chat.assert_awaited_once()
        sent = css_client.send_chat.await_args.args[1]
        assert sent == "📢 Bob: hello world"

    async def test_cooldown_blocks_second_shoutout(
        self, handler: CommandHandler, database: EconomyDatabase,
    ):
        await _fund(database, "Bob", 100_000)
        first = await handler._handle_command({
            "command": "vanity.shoutout", "username": "Bob", "channel": CH, "value": "one",
        })
        assert first["success"] is True
        second = await handler._handle_command({
            "command": "vanity.shoutout", "username": "Bob", "channel": CH, "value": "two",
        })
        assert second["success"] is False
        assert "cooldown" in second["error"].lower()

    async def test_message_too_long_is_rejected(
        self, handler: CommandHandler, database: EconomyDatabase, sample_config: EconomyConfig,
    ):
        await _fund(database, "Bob", 100_000)
        too_long = "x" * (sample_config.vanity_shop.shoutout.max_length + 1)
        result = await handler._handle_command({
            "command": "vanity.shoutout", "username": "Bob", "channel": CH, "value": too_long,
        })
        assert result["success"] is False
        assert "too long" in result["error"].lower()

    async def test_insufficient_funds_is_rejected(
        self, handler: CommandHandler, database: EconomyDatabase,
    ):
        await database.get_or_create_account("Broke", CH)
        result = await handler._handle_command({
            "command": "vanity.shoutout", "username": "Broke", "channel": CH, "value": "hi",
        })
        assert result["success"] is False
