"""Tests for kryten_economy.presence_tracker module."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker, UserSession


@pytest.fixture
def tracker(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    """Create a PresenceTracker with test config and mock client."""
    return PresenceTracker(
        config=sample_config,
        database=database,
        client=mock_client,
        logger=logging.getLogger("test.presence"),
    )


class TestJoinLeave:
    """Join/leave event handling."""

    async def test_genuine_join(self, tracker: PresenceTracker):
        """First join should be a genuine arrival."""
        result = await tracker.handle_user_join("Alice", "testchannel")
        assert result is True
        assert tracker.is_connected("Alice", "testchannel")

    async def test_ignored_user_join(self, tracker: PresenceTracker):
        """Ignored users should be rejected."""
        result = await tracker.handle_user_join("IgnoredBot", "testchannel")
        assert result is False
        assert not tracker.is_connected("IgnoredBot", "testchannel")

    async def test_duplicate_join(self, tracker: PresenceTracker):
        """Duplicate join (already connected) should return False."""
        await tracker.handle_user_join("Alice", "testchannel")
        result = await tracker.handle_user_join("Alice", "testchannel")
        assert result is False

    async def test_leave_removes_after_debounce(self, tracker: PresenceTracker):
        """User leave should schedule deferred cleanup."""
        await tracker.handle_user_join("Alice", "testchannel")
        await tracker.handle_user_leave("Alice", "testchannel")
        # Session still exists during debounce window
        # (departure is recorded but session not immediately removed)
        assert ("alice", "testchannel") in tracker._last_departure

    async def test_ignored_user_leave(self, tracker: PresenceTracker):
        """Leave for ignored user should be no-op."""
        await tracker.handle_user_leave("IgnoredBot", "testchannel")
        # No error expected

    async def test_leave_for_unknown_user(self, tracker: PresenceTracker):
        """Leave for user who never joined should be no-op."""
        await tracker.handle_user_leave("Ghost", "testchannel")

    async def test_case_insensitive_tracking(self, tracker: PresenceTracker):
        """Session keys should be case-insensitive."""
        await tracker.handle_user_join("Alice", "testchannel")
        assert tracker.is_connected("alice", "testchannel")
        assert tracker.is_connected("ALICE", "testchannel")


class TestSessionQueries:
    """Session query methods."""

    async def test_get_connected_users(self, tracker: PresenceTracker):
        """get_connected_users should return set of usernames."""
        await tracker.handle_user_join("Alice", "testchannel")
        await tracker.handle_user_join("Bob", "testchannel")
        users = tracker.get_connected_users("testchannel")
        assert users == {"Alice", "Bob"}

    async def test_get_connected_count(self, tracker: PresenceTracker):
        """get_connected_count should return count of users."""
        await tracker.handle_user_join("Alice", "testchannel")
        await tracker.handle_user_join("Bob", "testchannel")
        assert tracker.get_connected_count("testchannel") == 2

    async def test_get_connected_count_empty(self, tracker: PresenceTracker):
        """Empty channel should have count 0."""
        assert tracker.get_connected_count("emptychannel") == 0

    async def test_is_connected_false(self, tracker: PresenceTracker):
        """is_connected should return False for non-connected user."""
        assert not tracker.is_connected("Ghost", "testchannel")


class TestDebounce:
    """Join debounce logic."""

    async def test_bounce_detection(self, tracker: PresenceTracker):
        """Quick reconnect within debounce window should not be genuine."""
        await tracker.handle_user_join("Alice", "testchannel")
        # Simulate departure
        from kryten_economy.utils import now_utc
        tracker._last_departure[("alice", "testchannel")] = now_utc()
        del tracker._sessions[("alice", "testchannel")]

        # Immediately rejoin — should be detected as bounce
        result = await tracker.handle_user_join("Alice", "testchannel")
        assert result is False  # Bounce, not genuine

    async def test_genuine_after_debounce(self, tracker: PresenceTracker):
        """Reconnect after debounce window should be genuine."""
        await tracker.handle_user_join("Alice", "testchannel")
        # Simulate departure long ago
        from kryten_economy.utils import now_utc
        old_time = now_utc() - timedelta(minutes=10)
        tracker._last_departure[("alice", "testchannel")] = old_time
        del tracker._sessions[("alice", "testchannel")]

        result = await tracker.handle_user_join("Alice", "testchannel")
        assert result is True  # Genuine arrival


class TestWelcomeWallet:
    """Welcome wallet on first join."""

    async def test_welcome_wallet_on_first_join(self, tracker: PresenceTracker, database: EconomyDatabase):
        """New user should receive welcome wallet."""
        await tracker.handle_user_join("NewUser", "testchannel")
        balance = await database.get_balance("NewUser", "testchannel")
        assert balance == 100  # From onboarding.welcome_wallet

    async def test_no_double_welcome_wallet(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Welcome wallet should not be given twice."""
        await tracker.handle_user_join("NewUser", "testchannel")
        first_balance = await database.get_balance("NewUser", "testchannel")

        # Force session removal and rejoin (genuine)
        old_time = tracker._sessions[("newuser", "testchannel")].connected_at - timedelta(hours=1)
        tracker._last_departure[("newuser", "testchannel")] = old_time
        del tracker._sessions[("newuser", "testchannel")]

        await tracker.handle_user_join("NewUser", "testchannel")
        second_balance = await database.get_balance("NewUser", "testchannel")
        assert second_balance == first_balance  # No double wallet


class TestStartStop:
    """Tracker lifecycle."""

    async def test_start_stop(self, tracker: PresenceTracker):
        """start() and stop() should work without error."""
        await tracker.start()
        assert tracker._running is True
        assert tracker._tick_task is not None
        await tracker.stop()
        assert tracker._running is False

    async def test_stop_updates_last_seen(self, tracker: PresenceTracker, database: EconomyDatabase):
        """stop() should update last_seen for all active sessions."""
        await tracker.handle_user_join("Alice", "testchannel")
        await tracker.stop()
        acct = await database.get_account("Alice", "testchannel")
        assert acct is not None
        assert acct["last_seen"] is not None
