"""Sprint 9 — GreetingHandler tests.

Tests:
- Genuine arrival with custom greeting → greeting posted
- WS bounce (absent < threshold) → no greeting
- No custom greeting in DB → no greeting
- Config disabled → no greeting
- Batch simultaneous joins → combined greeting
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.event_announcer import EventAnnouncer
from kryten_economy.greeting_handler import GreetingHandler
from kryten_economy.presence_tracker import PresenceTracker


class TestGreetingHandler:
    """Tests for custom greeting execution on genuine arrivals."""

    @pytest.mark.asyncio
    async def test_genuine_arrival_with_greeting(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """User absent > greeting_absence_minutes → greeting posted."""
        # Set up presence tracker with a long-gone departure
        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        presence._last_departure[("alice", "testchannel")] = past

        # Insert a custom greeting via the database
        await database.get_or_create_account("alice", "testchannel")
        await database.set_vanity_item("alice", "testchannel", "custom_greeting", "Hello world!")

        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )

        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        handler._batch_delay = 0.05  # Speed up for testing

        await handler.on_user_join("testchannel", "alice")

        # Wait for batch flush
        await asyncio.sleep(0.15)

        # Greeting should be queued in announcer
        assert not announcer._queue.empty()
        _, msg = await announcer._queue.get()
        assert "Hello world!" in msg

    @pytest.mark.asyncio
    async def test_bounce_no_greeting(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """WS bounce (absent < greeting_absence_minutes) → no greeting."""
        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )
        # Departed just 5 minutes ago — below 30min threshold
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        presence._last_departure[("alice", "testchannel")] = recent

        await database.get_or_create_account("alice", "testchannel")
        await database.set_vanity_item("alice", "testchannel", "custom_greeting", "Hey!")

        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )

        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        handler._batch_delay = 0.05

        await handler.on_user_join("testchannel", "alice")
        await asyncio.sleep(0.15)

        # No greeting should be queued
        assert announcer._queue.empty()

    @pytest.mark.asyncio
    async def test_no_custom_greeting(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """User has no vanity greeting → no greeting posted."""
        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )
        # Long absence — would qualify
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        presence._last_departure[("alice", "testchannel")] = past

        await database.get_or_create_account("alice", "testchannel")
        # No vanity item set

        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )

        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        handler._batch_delay = 0.05

        await handler.on_user_join("testchannel", "alice")
        await asyncio.sleep(0.15)

        assert announcer._queue.empty()

    @pytest.mark.asyncio
    async def test_disabled_greetings(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """Config custom_greeting=False → no greeting posted."""
        sample_config.announcements.custom_greeting = False

        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )

        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )

        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        handler._batch_delay = 0.05

        await handler.on_user_join("testchannel", "alice")
        await asyncio.sleep(0.15)

        assert announcer._queue.empty()

    @pytest.mark.asyncio
    async def test_batch_simultaneous_joins(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """3 joins within batch window → combined greeting."""
        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )
        # All users have long absence
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        for user in ["alice", "bob", "charlie"]:
            presence._last_departure[(user, "testchannel")] = past
            await database.get_or_create_account(user, "testchannel")
            await database.set_vanity_item(user, "testchannel", "custom_greeting", f"Hi from {user}!")

        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )

        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        handler._batch_delay = 0.1

        # Rapid-fire joins within batch window
        await handler.on_user_join("testchannel", "alice")
        await handler.on_user_join("testchannel", "bob")
        await handler.on_user_join("testchannel", "charlie")

        # Wait for batch
        await asyncio.sleep(0.3)

        # Should produce a single combined greeting
        assert not announcer._queue.empty()
        _, msg = await announcer._queue.get()
        assert " | " in msg  # Combined format
        assert "Hi from alice!" in msg
        assert "Hi from bob!" in msg
        assert "Hi from charlie!" in msg

    @pytest.mark.asyncio
    async def test_first_time_user_gets_greeting(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """User with no departure record (first time ever) → treated as long absence."""
        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )
        # No _last_departure entry → was_absent_longer_than returns True

        await database.get_or_create_account("newuser", "testchannel")
        await database.set_vanity_item("newuser", "testchannel", "custom_greeting", "I'm new!")

        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )

        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        handler._batch_delay = 0.05

        await handler.on_user_join("testchannel", "newuser")
        await asyncio.sleep(0.15)

        assert not announcer._queue.empty()
        _, msg = await announcer._queue.get()
        assert "I'm new!" in msg

    @pytest.mark.asyncio
    async def test_update_config(
        self,
        sample_config: EconomyConfig,
        database: EconomyDatabase,
        mock_client: MagicMock,
    ) -> None:
        """update_config swaps the config reference."""
        presence = PresenceTracker(
            config=sample_config, database=database,
            client=mock_client, logger=logging.getLogger("test"),
        )
        announcer = EventAnnouncer(
            config=sample_config, client=mock_client,
            logger=logging.getLogger("test"),
        )
        handler = GreetingHandler(
            config=sample_config, database=database,
            presence_tracker=presence, announcer=announcer,
            logger=logging.getLogger("test"),
        )
        new_config = MagicMock()
        handler.update_config(new_config)
        assert handler._config is new_config
