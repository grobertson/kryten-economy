"""Tests for queue/search/playnext/forcenow commands."""

from __future__ import annotations

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
    mock_media_client: MagicMock,
    mock_client: MagicMock | None = None,
) -> PmHandler:
    """Build PmHandler with Sprint 5 dependencies."""
    logger = logging.getLogger("test")
    presence = PresenceTracker(config, database, logger)
    return PmHandler(
        config=config,
        database=database,
        client=mock_client,
        presence_tracker=presence,
        logger=logger,
        spending_engine=spending_engine,
        media_client=mock_media_client,
    )


def _fake_media(mid: str = "abc123", title: str = "Test Video", dur: int = 600) -> dict:
    return {
        "id": mid,
        "title": title,
        "duration": dur,
        "media_type": "yt",
        "media_id": mid,
    }


# ═══════════════════════════════════════════════════════════════
#  search
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_search_no_mediacms(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine,
):
    """Search with no media client → not configured."""
    handler = PmHandler(
        config=sample_config, database=database, client=None,
        presence_tracker=PresenceTracker(sample_config, database, logging.getLogger("test")),
        logger=logging.getLogger("test"),
        spending_engine=spending_engine, media_client=None,
    )
    resp = await handler._cmd_search("Alice", CH, ["test"])
    assert "not configured" in resp.lower()


@pytest.mark.asyncio
async def test_search_shows_results(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """Search returns formatted results."""
    mock_media_client.search = AsyncMock(return_value=[
        _fake_media("v1", "Cool Video", 600),
        _fake_media("v2", "Nice Movie", 7200),
    ])
    await _seed_account(database, "Alice")
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_search("Alice", CH, ["cool"])
    assert "Cool Video" in resp
    assert "Nice Movie" in resp
    assert "v1" in resp


@pytest.mark.asyncio
async def test_search_shows_discount(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """High-rank user sees discount in search results."""
    mock_media_client.search = AsyncMock(return_value=[_fake_media("v1", "Video", 600)])
    await _seed_account(database, "Whale", balance=50000, lifetime=100000)  # tier 5 = 10%
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_search("Whale", CH, ["video"])
    assert "off" in resp.lower()


# ═══════════════════════════════════════════════════════════════
#  queue
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_queue_success(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
    mock_client: MagicMock,
):
    """Successful queue deducts funds and calls add_media."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Hit Song", 180))
    await _seed_account(database, "Alice", 5000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client, mock_client)

    resp = await handler._cmd_queue("Alice", CH, ["v1"])
    assert "Queued" in resp or "queued" in resp
    assert "Hit Song" in resp

    # Balance reduced
    account = await database.get_account("Alice", CH)
    assert account["balance"] < 5000

    # add_media called
    mock_client.add_media.assert_called_once()


@pytest.mark.asyncio
async def test_queue_not_found(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """Queue with unknown ID → not found."""
    mock_media_client.get_by_id = AsyncMock(return_value=None)
    await _seed_account(database, "Alice")
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_queue("Alice", CH, ["unknown"])
    assert "not found" in resp.lower()


@pytest.mark.asyncio
async def test_queue_insufficient_funds(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """Queue with too little Z → insufficient funds."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Movie", 7200))
    await _seed_account(database, "Broke", 100)  # only 100 Z, movie costs 1000
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_queue("Broke", CH, ["v1"])
    assert "insufficient" in resp.lower() or "funds" in resp.lower()


@pytest.mark.asyncio
async def test_queue_daily_limit(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
    mock_client: MagicMock,
):
    """Queue past daily limit → blocked."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Song", 180))
    await _seed_account(database, "Alice", 500000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client, mock_client)

    # Bypass cooldown by making get_last_queue_time return None
    original_get_last = database.get_last_queue_time

    async def _no_cooldown(username, channel):
        return None

    # Queue max_queues_per_day times (default 3), bypassing cooldown
    for i in range(3):
        database.get_last_queue_time = _no_cooldown
        resp = await handler._cmd_queue("Alice", CH, [f"v{i}"])
        assert "queued" in resp.lower() or "Queued" in resp

    database.get_last_queue_time = _no_cooldown
    # 4th should be rejected by daily limit
    resp = await handler._cmd_queue("Alice", CH, ["v99"])
    assert "limit" in resp.lower()

    database.get_last_queue_time = original_get_last


@pytest.mark.asyncio
async def test_queue_cooldown(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
    mock_client: MagicMock,
):
    """Second queue within cooldown → blocked."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Song", 180))
    await _seed_account(database, "Alice", 50000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client, mock_client)

    await handler._cmd_queue("Alice", CH, ["v1"])
    resp = await handler._cmd_queue("Alice", CH, ["v2"])
    assert "cooldown" in resp.lower()


# ═══════════════════════════════════════════════════════════════
#  playnext
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_playnext_uses_position(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
    mock_client: MagicMock,
):
    """playnext calls add_media with position='next'."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Priority", 300))
    await _seed_account(database, "Alice", 500000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client, mock_client)

    resp = await handler._cmd_playnext("Alice", CH, ["v1"])
    assert "Queued" in resp or "queued" in resp

    # Check position kwarg
    call_args = mock_client.add_media.call_args
    assert call_args.kwargs.get("position") == "next" or (len(call_args.args) > 3 and call_args.args[3] == "next")


@pytest.mark.asyncio
async def test_playnext_higher_cost(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
    mock_client: MagicMock,
):
    """playnext costs interrupt_play_next (100000) regardless of duration."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Short", 60))
    await _seed_account(database, "Alice", 500000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client, mock_client)

    resp = await handler._cmd_playnext("Alice", CH, ["v1"])
    account = await database.get_account("Alice", CH)
    # Charged interrupt_play_next (100000), balance was 500000 → ~400000
    assert account["balance"] <= 500000 - 80000  # At least 80000 charged (maybe discount)


# ═══════════════════════════════════════════════════════════════
#  forcenow
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_forcenow_creates_approval(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """forcenow with admin gate → creates pending approval."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Urgent", 300))
    await _seed_account(database, "Rich", 2000000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_forcenow("Rich", CH, ["v1"])
    assert "approval" in resp.lower() or "submitted" in resp.lower()

    # Pending approval created
    approvals = await database.get_pending_approvals(CH, "force_play")
    assert len(approvals) >= 1


@pytest.mark.asyncio
async def test_forcenow_without_admin_gate(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_media_client: MagicMock, mock_client: MagicMock,
):
    """forcenow with admin gate disabled → queues directly."""
    # Override config to disable admin gate
    from tests.conftest import make_config_dict
    cfg_dict = make_config_dict(spending={"force_play_requires_admin": False})
    config = EconomyConfig(**cfg_dict)
    engine = SpendingEngine(config, database, mock_media_client, logging.getLogger("test"))

    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media("v1", "Direct", 300))
    await _seed_account(database, "Rich", 2000000)
    handler = _make_handler(config, database, engine, mock_media_client, mock_client)

    resp = await handler._cmd_forcenow("Rich", CH, ["v1"])
    assert "Queued" in resp or "queued" in resp
    mock_client.add_media.assert_called_once()
