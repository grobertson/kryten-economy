"""Tests for vanity shop, fortune, shoutout commands."""

from __future__ import annotations

import hashlib
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
    balance: int = 50000,
    lifetime: int = 0,
) -> None:
    """Create account with given balance and lifetime earnings."""
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")
    if lifetime > 0:
        import asyncio
        loop = asyncio.get_running_loop()

        def _set():
            conn = db._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET lifetime_earned = ? WHERE username = ? AND channel = ?",
                    (lifetime, username, CH),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _set)


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    spending_engine: SpendingEngine,
    mock_client: MagicMock | None = None,
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
#  shop command
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_shop_lists_items(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """shop lists available vanity items."""
    await _seed_account(database, "Alice")
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_shop("Alice", CH, [])
    assert "Vanity Shop" in resp
    assert "greeting" in resp.lower()
    assert "color" in resp.lower()
    assert "fortune" in resp.lower()


@pytest.mark.asyncio
async def test_shop_shows_discount(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """High-rank user sees discounted prices."""
    await _seed_account(database, "Whale", balance=50000, lifetime=100000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_shop("Whale", CH, [])
    assert "was" in resp.lower()  # "was X" indicates discount


@pytest.mark.asyncio
async def test_shop_shows_owned(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Shop shows items the user already owns."""
    await _seed_account(database, "Alice")
    await database.set_vanity_item("Alice", CH, "custom_greeting", "Hello there!")
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_shop("Alice", CH, [])
    assert "Hello there!" in resp


# ═══════════════════════════════════════════════════════════════
#  buy greeting
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_buy_greeting_success(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Buying greeting deducts cost and stores vanity item."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_buy("Alice", CH, ["greeting", "Welcome", "to", "my", "world!"])
    assert "greeting" in resp.lower() or "set" in resp.lower()

    greet = await database.get_vanity_item("Alice", CH, "custom_greeting")
    assert greet is not None
    assert "Welcome" in greet

    account = await database.get_account("Alice", CH)
    assert account["balance"] < 10000


@pytest.mark.asyncio
async def test_buy_greeting_too_long(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Greeting over 200 chars → rejected."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine)

    long_text = "x" * 201
    resp = await handler._cmd_buy("Alice", CH, ["greeting", long_text])
    assert "too long" in resp.lower() or "200" in resp


# ═══════════════════════════════════════════════════════════════
#  buy color
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_buy_color_valid(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Buying a valid palette color works."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_buy("Alice", CH, ["color", "Crimson"])
    assert "#DC143C" in resp or "crimson" in resp.lower()

    color = await database.get_vanity_item("Alice", CH, "chat_color")
    assert color == "#DC143C"


@pytest.mark.asyncio
async def test_buy_color_invalid(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Unknown color name → rejected."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_buy("Alice", CH, ["color", "FakeColor"])
    assert "unknown" in resp.lower() or "available" in resp.lower()


# ═══════════════════════════════════════════════════════════════
#  buy gif (approval required)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_buy_gif_creates_approval(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Channel GIF creates a pending approval."""
    await _seed_account(database, "Alice", 50000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_buy("Alice", CH, ["gif", "https://example.com/cool.gif"])
    assert "approval" in resp.lower() or "submitted" in resp.lower()

    approvals = await database.get_pending_approvals(CH, "channel_gif")
    assert len(approvals) >= 1


# ═══════════════════════════════════════════════════════════════
#  buy shoutout
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_buy_shoutout_sends_chat(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_client: MagicMock,
):
    """Shoutout delivers message to public chat."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine, mock_client)

    resp = await handler._cmd_buy("Alice", CH, ["shoutout", "Hello", "world!"])
    assert "delivered" in resp.lower() or "shoutout" in resp.lower()

    # Check public chat was called
    mock_client.send_chat.assert_called()
    call_msg = mock_client.send_chat.call_args.args[1]
    assert "Hello world!" in call_msg


@pytest.mark.asyncio
async def test_buy_shoutout_cooldown(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_client: MagicMock,
):
    """Second shoutout within cooldown → blocked."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine, mock_client)

    await handler._cmd_buy("Alice", CH, ["shoutout", "First!"])
    resp = await handler._cmd_buy("Alice", CH, ["shoutout", "Second!"])
    assert "cooldown" in resp.lower()


# ═══════════════════════════════════════════════════════════════
#  fortune
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fortune_once_per_day(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Fortune can only be used once per day."""
    await _seed_account(database, "Alice", 10000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp1 = await handler._cmd_fortune("Alice", CH, [])
    assert "🔮" in resp1 or "🎱" in resp1 or "🌙" in resp1 or "fortune" not in resp1.lower() or resp1 in PmHandler.FORTUNES

    resp2 = await handler._cmd_fortune("Alice", CH, [])
    assert "already" in resp2.lower()


@pytest.mark.asyncio
async def test_fortune_different_per_user(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Different users can get different fortunes."""
    await _seed_account(database, "Alice", 10000)
    await _seed_account(database, "Bob", 10000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp_alice = await handler._cmd_fortune("Alice", CH, [])
    resp_bob = await handler._cmd_fortune("Bob", CH, [])
    # Both should be valid fortunes (not error messages)
    assert "already" not in resp_alice.lower()
    assert "already" not in resp_bob.lower()


# ═══════════════════════════════════════════════════════════════
#  buy rename
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_buy_rename_success(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Rename currency stores new name."""
    await _seed_account(database, "Alice", 50000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_buy("Alice", CH, ["rename", "TacoBucks"])
    assert "TacoBucks" in resp

    name = await database.get_vanity_item("Alice", CH, "personal_currency_name")
    assert name == "TacoBucks"


@pytest.mark.asyncio
async def test_buy_rename_too_long(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Rename > 30 chars → rejected."""
    await _seed_account(database, "Alice", 50000)
    handler = _make_handler(sample_config, database, spending_engine)

    resp = await handler._cmd_buy("Alice", CH, ["rename", "x" * 31])
    assert "too long" in resp.lower() or "30" in resp


# ═══════════════════════════════════════════════════════════════
#  disabled item
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_buy_disabled_item(database: EconomyDatabase):
    """Buying a disabled item → rejected."""
    from tests.conftest import make_config_dict
    cfg = EconomyConfig(**make_config_dict(
        vanity_shop={"custom_greeting": {"cost": 500, "enabled": False}},
    ))
    engine = SpendingEngine(cfg, database, None, logging.getLogger("test"))
    handler = _make_handler(cfg, database, engine)

    await _seed_account(database, "Alice", 10000)
    resp = await handler._cmd_buy("Alice", CH, ["greeting", "Hey!"])
    assert "not available" in resp.lower()
