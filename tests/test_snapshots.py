"""Tests for Sprint 8 economy snapshots."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from kryten_economy.database import EconomyDatabase
from kryten_economy.admin_scheduler import AdminScheduler
from kryten_economy.presence_tracker import PresenceTracker

CH = "testchannel"


@pytest.mark.asyncio
async def test_snapshot_capture(
    admin_scheduler: AdminScheduler,
    database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """All fields written correctly."""
    # Seed data
    await database.get_or_create_account("alice", CH)
    await database.credit("alice", CH, 500, tx_type="earn", trigger_id="test")
    await presence_tracker.handle_user_join("alice", CH)

    await admin_scheduler._capture_snapshot(CH)

    snapshot = await database.get_latest_snapshot(CH)
    assert snapshot is not None
    assert snapshot["total_accounts"] >= 1
    assert snapshot["total_z_circulation"] >= 500
    assert "median_balance" in snapshot
    assert "participation_rate" in snapshot


@pytest.mark.asyncio
async def test_snapshot_history(
    admin_scheduler: AdminScheduler,
    database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """Returns chronological snapshots."""
    await database.get_or_create_account("alice", CH)
    await presence_tracker.handle_user_join("alice", CH)

    # Capture multiple snapshots
    await admin_scheduler._capture_snapshot(CH)
    await database.credit("alice", CH, 100, tx_type="earn", trigger_id="test")
    await admin_scheduler._capture_snapshot(CH)

    history = await database.get_snapshot_history(CH, days=7)
    assert len(history) >= 2


@pytest.mark.asyncio
async def test_latest_snapshot(
    database: EconomyDatabase,
):
    """Most recent snapshot returned."""
    await database.write_snapshot(CH, {
        "total_accounts": 10,
        "total_z_circulation": 5000,
        "active_economy_users_today": 3,
        "z_earned_today": 500,
        "z_spent_today": 200,
        "z_gambled_net_today": -50,
        "median_balance": 400,
        "participation_rate": 30.0,
    })

    latest = await database.get_latest_snapshot(CH)
    assert latest is not None
    assert latest["total_accounts"] == 10
    assert latest["total_z_circulation"] == 5000
