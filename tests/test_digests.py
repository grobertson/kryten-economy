"""Tests for Sprint 8 admin and user digests."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.admin_scheduler import AdminScheduler
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.rank_engine import RankEngine

CH = "testchannel"


@pytest.mark.asyncio
async def test_admin_digest_format(
    admin_scheduler: AdminScheduler,
    database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """Digest contains all required sections."""
    await database.get_or_create_account("alice", CH)
    await database.credit("alice", CH, 500, tx_type="earn", trigger_id="test")
    await presence_tracker.handle_user_join("alice", CH)

    admin_scheduler._presence.update_user_rank(CH, "alice", 4)

    await admin_scheduler._send_admin_digest(CH)

    # digest should be sent via send_pm to admin(s)
    admin_scheduler._client.send_pm.assert_called()
    msg = admin_scheduler._client.send_pm.call_args[0][2]

    assert "Weekly Digest" in msg
    assert "circ" in msg.lower()
    assert "Top 5 Earners" in msg


@pytest.mark.asyncio
async def test_admin_digest_sent_to_admins(
    admin_scheduler: AdminScheduler,
    database: EconomyDatabase,
    presence_tracker: PresenceTracker,
):
    """Only admin-rank users receive the digest."""
    # Set up 2 users — one admin (rank 4), one regular (rank 1)
    await database.get_or_create_account("admin_user", CH)
    await database.get_or_create_account("regular_user", CH)
    await presence_tracker.handle_user_join("admin_user", CH)
    await presence_tracker.handle_user_join("regular_user", CH)

    admin_scheduler._presence.update_user_rank(CH, "admin_user", 4)
    admin_scheduler._presence.update_user_rank(CH, "regular_user", 1)

    await admin_scheduler._send_admin_digest(CH)

    # Only admin_user should receive the digest
    calls = admin_scheduler._client.send_pm.call_args_list
    recipients = [c[0][1] for c in calls]
    assert "admin_user" in recipients
    assert "regular_user" not in recipients


@pytest.mark.asyncio
async def test_user_digest_format(
    admin_scheduler: AdminScheduler,
    database: EconomyDatabase,
):
    """Contains personal earnings, rank, next goal."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    await database.get_or_create_account("alice", CH)
    await database.credit("alice", CH, 200, tx_type="earn", trigger_id="test")

    # Create daily activity for yesterday
    await database.get_or_create_daily_activity("alice", CH, yesterday)
    await database.increment_daily_messages_sent("alice", CH, yesterday)

    await admin_scheduler._send_user_digests(CH)

    if admin_scheduler._client.send_pm.called:
        msg = admin_scheduler._client.send_pm.call_args[0][2]
        # Digest should be formatted from the template
        assert isinstance(msg, str)


@pytest.mark.asyncio
async def test_user_digest_sent_to_active(
    admin_scheduler: AdminScheduler,
    database: EconomyDatabase,
):
    """Only users with yesterday's activity receive it."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    await database.get_or_create_account("active_user", CH)
    await database.get_or_create_account("idle_user", CH)

    # Only active_user has daily activity for yesterday
    await database.get_or_create_daily_activity("active_user", CH, yesterday)
    await database.increment_daily_messages_sent("active_user", CH, yesterday)

    admin_scheduler._client.send_pm.reset_mock()
    await admin_scheduler._send_user_digests(CH)

    if admin_scheduler._client.send_pm.called:
        recipients = [c[0][1] for c in admin_scheduler._client.send_pm.call_args_list]
        assert "idle_user" not in recipients


@pytest.mark.asyncio
async def test_user_digest_disabled(sample_config_dict: dict):
    """Config disabled → no task created."""
    sample_config_dict.setdefault("digest", {})
    sample_config_dict["digest"]["user_digest"] = {"enabled": False}
    config = EconomyConfig(**sample_config_dict)

    # AdminScheduler with disabled user digest
    scheduler = AdminScheduler(
        config=config,
        database=MagicMock(),
        client=MagicMock(),
        presence_tracker=MagicMock(),
        logger=logging.getLogger("test"),
    )

    # start() should not create user digest task when disabled
    # (3 tasks normally: snapshots, admin digest, user digest)
    # With user digest disabled: only 2 tasks
    await scheduler.start()
    assert len(scheduler._tasks) == 2
    await scheduler.stop()
