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


class TestChatColorContrastGuard:
    async def test_low_contrast_red_is_rejected_without_charge(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice", 100_000)
        before = await database.get_balance("Alice", CH)
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#FF0000",  # pure red: rejected by the combined metric
        })
        assert result["success"] is False
        # Not charged, no color stored, no CSS pushed.
        assert await database.get_balance("Alice", CH) == before
        assert await database.get_vanity_item("Alice", CH, "chat_color") is None
        css_client.set_channel_css.assert_not_awaited()

    async def test_dark_color_is_rejected(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice", 100_000)
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#000080",  # navy: too dark
        })
        assert result["success"] is False
        css_client.set_channel_css.assert_not_awaited()

    async def test_good_color_passes_guard(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice", 100_000)
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#FF69B4",  # pink: passes
        })
        assert result["success"] is True
        assert await database.get_vanity_item("Alice", CH, "chat_color") == "#FF69B4"

    async def test_warn_color_is_allowed(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice", 100_000)
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#FF1493",  # deep pink: warn band, still allowed
        })
        assert result["success"] is True

    async def test_guard_disabled_allows_low_contrast(
        self, handler: CommandHandler, database: EconomyDatabase,
        css_client: MagicMock, sample_config: EconomyConfig,
    ):
        sample_config.vanity_shop.chat_color.enforce_contrast = False
        await _fund(database, "Alice", 100_000)
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#FF0000",
        })
        assert result["success"] is True

    async def test_check_color_reports_reject(self, handler: CommandHandler):
        result = await handler._handle_command({
            "command": "vanity.check_color",
            "channel": CH,
            "value": "#FF0000",
        })
        assert result["success"] is True
        data = result["data"]
        assert data["valid"] is True
        assert data["level"] == "reject"
        assert data["acceptable"] is False
        assert data["message"]

    async def test_check_color_reports_ok(self, handler: CommandHandler):
        result = await handler._handle_command({
            "command": "vanity.check_color",
            "channel": CH,
            "value": "#FF69B4",
        })
        data = result["data"]
        assert data["level"] == "ok"
        assert data["acceptable"] is True

    async def test_check_color_invalid_hex(self, handler: CommandHandler):
        result = await handler._handle_command({
            "command": "vanity.check_color",
            "channel": CH,
            "value": "nope",
        })
        data = result["data"]
        assert data["valid"] is False
        assert data["acceptable"] is False

    async def test_check_color_does_not_charge_or_store(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice", 100_000)
        before = await database.get_balance("Alice", CH)
        await handler._handle_command({
            "command": "vanity.check_color",
            "channel": CH,
            "username": "Alice",
            "value": "#FF69B4",
        })
        assert await database.get_balance("Alice", CH) == before
        css_client.set_channel_css.assert_not_awaited()


class TestChatColorCssApply:
    async def test_purchase_writes_managed_block_with_original_casing(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        await _fund(database, "Alice")
        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })
        assert result["success"] is True
        css_client.set_channel_css.assert_awaited_once()
        pushed = css_client.set_channel_css.await_args.args[1]
        assert ".chat-msg-Alice { color: #66CCFF; }" in pushed
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

    async def test_empty_css_is_not_written_and_refunds(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        # REGRESSION (0.10.2): an empty CSS read means the channel's real CSS is
        # unavailable (the robot hasn't seeded it). Writing a managed-block-only
        # document would WIPE the channel's hand-maintained CSS. So we must NOT
        # write, and — since the purchase couldn't take effect — must refund.
        css_client.get_state_channel_css.return_value = ""
        await _fund(database, "Alice", 100_000)
        before = await database.get_balance("Alice", CH)

        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })

        # The channel CSS is never touched …
        css_client.set_channel_css.assert_not_awaited()
        # … the command reports failure with a refund message …
        assert result["success"] is False
        assert "refund" in result["error"].lower()
        # … the buyer is made whole …
        assert await database.get_balance("Alice", CH) == before
        # … and the colour is rolled back (not left active for a later rebuild).
        assert await database.get_vanity_item("Alice", CH, "chat_color") is None

    async def test_css_write_failure_refunds_and_rolls_back(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
        sample_config: EconomyConfig,
    ):
        # Non-empty CSS so we proceed to the write, which then fails (robot/NATS
        # outage). The charge must be refunded and the colour rolled back.
        css_client.get_state_channel_css.return_value = "body { color: #fff; }\n"
        css_client.set_channel_css.side_effect = RuntimeError("nats down")
        await _fund(database, "Alice", 100_000)
        before = await database.get_balance("Alice", CH)

        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })

        assert result["success"] is False
        assert "refund" in result["error"].lower()
        # Fully refunded (no net change) …
        assert await database.get_balance("Alice", CH) == before
        # … and the colour was rolled back so it isn't applied on a later rebuild.
        assert await database.get_vanity_item("Alice", CH, "chat_color") is None

    async def test_css_write_failure_restores_previous_color(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        # Alice already had a colour; a failed re-colour must restore the old one
        # (not leave the new, unpaid-for value active).
        await database.set_vanity_item("Alice", CH, "chat_color", "#AAAAAA")
        css_client.get_state_channel_css.return_value = "body{}\n"
        css_client.set_channel_css.side_effect = RuntimeError("nats down")
        await _fund(database, "Alice", 100_000)
        before = await database.get_balance("Alice", CH)

        result = await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })

        assert result["success"] is False
        assert await database.get_balance("Alice", CH) == before
        assert await database.get_vanity_item("Alice", CH, "chat_color") == "#AAAAAA"

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
            "value": "#66CCFF",
        })
        css_client.set_channel_css.assert_not_awaited()


class TestUpgradeImport:
    async def test_legacy_css_color_is_preserved_and_imported(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        # OldTimer's color lives only in the CSS (legacy rule), not the DB.
        css_client.get_state_channel_css.return_value = (
            "body{}\n/* ZCoin purchased vanity colors */\n"
            ".chat-msg-OldTimer { color: #abcdef; }\n"
        )
        await _fund(database, "Alice")
        await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })
        pushed = css_client.set_channel_css.await_args.args[1]
        # Preserved in the rewritten CSS (original casing kept)…
        assert ".chat-msg-OldTimer { color: #ABCDEF; }" in pushed
        assert ".chat-msg-Alice { color: #66CCFF; }" in pushed
        # …and imported into OldTimer's account (canonical casing) so it's editable.
        assert await database.get_vanity_item("OldTimer", CH, "chat_color") == "#ABCDEF"

    async def test_import_skips_protected_users(
        self, handler: CommandHandler, database: EconomyDatabase,
        css_client: MagicMock, sample_config: EconomyConfig,
    ):
        sample_config.vanity_shop.chat_color.protected_users = ["VHSOracle"]
        css_client.get_state_channel_css.return_value = (
            "body{}\n/* ZCoin purchased vanity colors */\n"
            ".chat-msg-VHSOracle { color: #cccccc; }\n"
        )
        await _fund(database, "Alice")
        await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })
        # Protected bot color is never imported into the DB…
        assert await database.get_vanity_item("vhsoracle", CH, "chat_color") is None
        # …nor written into the managed block.
        pushed = css_client.set_channel_css.await_args.args[1]
        begin = sample_config.vanity_shop.chat_color.css_block_begin
        assert "VHSOracle" not in pushed.split(begin, 1)[1]

    async def test_import_disabled_does_not_persist(
        self, handler: CommandHandler, database: EconomyDatabase,
        css_client: MagicMock, sample_config: EconomyConfig,
    ):
        sample_config.vanity_shop.chat_color.import_existing_colors = False
        css_client.get_state_channel_css.return_value = (
            "body{}\n/* ZCoin purchased vanity colors */\n"
            ".chat-msg-OldTimer { color: #abcdef; }\n"
        )
        await _fund(database, "Alice")
        await handler._handle_command({
            "command": "vanity.set_color",
            "username": "Alice",
            "channel": CH,
            "value": "#66CCFF",
        })
        assert await database.get_vanity_item("oldtimer", CH, "chat_color") is None


class TestResyncColorsCommand:
    async def test_resync_imports_all_legacy_colors(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        css_client.get_state_channel_css.return_value = (
            "body{}\n"
            "/* ZCoin purchased vanity colors */\n.chat-msg-Rat-Bastard { color: #cf28fd; }\n"
            "/* ZCoin purchased vanity colors */\n.chat-msg-TeenageDraculerX { color: #c5a1f7; }\n"
        )
        result = await handler._handle_command({
            "command": "vanity.resync_colors",
            "channel": CH,
        })
        assert result["success"] is True
        assert result["data"]["imported"] == 2
        assert result["data"]["css_reapplied"] is True
        assert await database.get_vanity_item("Rat-Bastard", CH, "chat_color") == "#CF28FD"
        assert await database.get_vanity_item("TeenageDraculerX", CH, "chat_color") == "#C5A1F7"

    async def test_resync_is_idempotent(
        self, handler: CommandHandler, database: EconomyDatabase, css_client: MagicMock,
    ):
        css_client.get_state_channel_css.return_value = (
            "body{}\n/* ZCoin purchased vanity colors */\n"
            ".chat-msg-OldTimer { color: #abcdef; }\n"
        )
        first = await handler._handle_command({"command": "vanity.resync_colors", "channel": CH})
        assert first["data"]["imported"] == 1
        second = await handler._handle_command({"command": "vanity.resync_colors", "channel": CH})
        assert second["data"]["imported"] == 0

    async def test_resync_errors_on_empty_css(
        self, handler: CommandHandler, css_client: MagicMock,
    ):
        css_client.get_state_channel_css.return_value = ""
        result = await handler._handle_command({"command": "vanity.resync_colors", "channel": CH})
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()


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
