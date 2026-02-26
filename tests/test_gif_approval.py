"""Tests for Sprint 8 GIF approval/rejection commands."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler

CH = "testchannel"


async def _create_pending_gif(database: EconomyDatabase, username: str) -> dict:
    """Helper: create a pending GIF approval entry."""
    await database.get_or_create_account(username, CH)
    # Debit the cost first
    await database.credit(username, CH, 500, tx_type="earn", trigger_id="test")
    await database.debit(username, CH, 200, tx_type="spend", trigger_id="vanity.channel_gif")
    # Insert pending approval
    approval_id = await database.create_pending_approval(
        username, CH, "channel_gif", cost=200,
        data='{"gif_url": "https://example.com/test.gif"}',
    )
    return {"id": approval_id, "username": username, "cost": 200}


@pytest.mark.asyncio
async def test_approve_gif(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Resolves pending, PMs user."""
    await _create_pending_gif(database, "alice")

    result = await pm_handler._cmd_approve_gif("admin", CH, ["alice"])

    assert "Approved" in result
    assert "alice" in result
    mock_client.send_pm.assert_called()


@pytest.mark.asyncio
async def test_approve_no_pending(pm_handler: PmHandler, database: EconomyDatabase):
    """No pending → error."""
    result = await pm_handler._cmd_approve_gif("admin", CH, ["bob"])
    assert "No pending" in result


@pytest.mark.asyncio
async def test_reject_gif_refund(
    pm_handler: PmHandler, database: EconomyDatabase, mock_client: MagicMock,
):
    """Resolves rejected, refunds cost, PMs user."""
    await _create_pending_gif(database, "charlie")

    before = (await database.get_account("charlie", CH))["balance"]
    result = await pm_handler._cmd_reject_gif("admin", CH, ["charlie"])

    assert "Rejected" in result
    assert "200" in result  # refund amount

    after = (await database.get_account("charlie", CH))["balance"]
    assert after == before + 200  # refund applied

    mock_client.send_pm.assert_called()
