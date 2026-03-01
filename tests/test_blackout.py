"""Tests for blackout window handling in queue commands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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
) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    spending_engine: SpendingEngine,
    mock_media_client: MagicMock,
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
        media_client=mock_media_client,
    )


def _fake_media(mid: str = "abc123") -> dict:
    return {
        "id": mid, "title": "Test Video", "duration": 600,
        "media_type": "cm",
        "media_id": f"https://media.test.com/api/v1/media/cytube/{mid}.json?format=json",
    }


# ═══════════════════════════════════════════════════════════════
#  Blackout tests — note: blackout is currently a pass-through
#  in the code (placeholder). These tests verify the config
#  structure works and queue still functions without blackouts.
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_queue_no_blackout(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
    mock_client: MagicMock,
):
    """Queue works fine with default empty blackout_windows."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media())
    await _seed_account(database, "Alice", 5000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client, mock_client)

    resp = await handler._cmd_queue("Alice", CH, ["abc123"])
    assert "You selected" in resp

    # Confirm with YES
    pending = handler._pending_confirm.pop("alice")
    resp = await handler._execute_confirmed_queue("Alice", CH, pending)
    assert "queued" in resp.lower()


@pytest.mark.asyncio
async def test_blackout_config_loads():
    """BlackoutWindowConfig can be loaded in SpendingConfig."""
    from tests.conftest import make_config_dict
    cfg = EconomyConfig(**make_config_dict(
        spending={
            "blackout_windows": [
                {"name": "Movie Night", "cron": "0 20 * * 5", "duration_hours": 3},
            ],
        },
    ))
    assert len(cfg.spending.blackout_windows) == 1
    assert cfg.spending.blackout_windows[0].name == "Movie Night"
    assert cfg.spending.blackout_windows[0].duration_hours == 3


@pytest.mark.asyncio
async def test_multiple_blackout_windows():
    """Multiple blackout windows are parsed."""
    from tests.conftest import make_config_dict
    cfg = EconomyConfig(**make_config_dict(
        spending={
            "blackout_windows": [
                {"name": "Movie Night", "cron": "0 20 * * 5", "duration_hours": 3},
                {"name": "Event Night", "cron": "0 19 * * 3", "duration_hours": 4},
            ],
        },
    ))
    assert len(cfg.spending.blackout_windows) == 2


@pytest.mark.asyncio
async def test_forcenow_bypasses_blackout(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """forcenow should not check blackout (by design)."""
    mock_media_client.get_by_id = AsyncMock(return_value=_fake_media())
    await _seed_account(database, "Alice", 2000000)
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    # forcenow with admin gate → creates approval (but doesn't check blackout)
    resp = await handler._cmd_forcenow("Alice", CH, ["abc123"])
    assert "approval" in resp.lower() or "submitted" in resp.lower()
