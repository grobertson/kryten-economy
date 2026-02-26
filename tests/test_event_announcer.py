"""Sprint 9 — EventAnnouncer tests.

Tests:
- Template rendering with variable substitution
- Missing template → no announcement
- Disabled announcement gate → suppressed
- Deduplication within window
- Rate limiting (>10/min)
- Batch delay (messages delayed)
- Raw announcement (bypasses templates)
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.event_announcer import EventAnnouncer


def _make_announcer(
    mock_client: MagicMock,
    sample_config: EconomyConfig,
    **overrides,
) -> EventAnnouncer:
    """Create an EventAnnouncer with optional overrides."""
    announcer = EventAnnouncer(
        config=sample_config,
        client=mock_client,
        logger=logging.getLogger("test.announcer"),
    )
    for k, v in overrides.items():
        setattr(announcer, k, v)
    return announcer


class TestEventAnnouncer:
    """Tests for EventAnnouncer template, dedup, and rate-limit logic."""

    @pytest.mark.asyncio
    async def test_template_rendering(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Variables substituted correctly in the template."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce(
            "testchannel", "rank_up",
            {"user": "alice", "rank": "Grip"},
        )

        # Message should be in the queue
        assert not announcer._queue.empty()
        channel, message = await announcer._queue.get()
        assert channel == "testchannel"
        assert "alice" in message
        assert "Grip" in message

    @pytest.mark.asyncio
    async def test_missing_template(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Missing template key with no fallback → no announcement queued."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce(
            "testchannel", "nonexistent_template_key_xyz",
            {"user": "alice"},
        )

        assert announcer._queue.empty()

    @pytest.mark.asyncio
    async def test_missing_template_with_fallback(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Missing template key with fallback → fallback used."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce(
            "testchannel", "nonexistent_key",
            {"user": "alice"},
            fallback="Fallback: {user}",
        )

        assert not announcer._queue.empty()
        _, message = await announcer._queue.get()
        assert message == "Fallback: alice"

    @pytest.mark.asyncio
    async def test_disabled_announcement(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Config gate off → announcement suppressed."""
        # Disable custom_greeting
        sample_config.announcements.custom_greeting = False
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce(
            "testchannel", "custom_greeting",
            {"greeting": "Hello!"},
        )

        assert announcer._queue.empty()

    @pytest.mark.asyncio
    async def test_deduplication(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Same message within dedup window → only first queued."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce(
            "testchannel", "rank_up",
            {"user": "alice", "rank": "Grip"},
        )
        await announcer.announce(
            "testchannel", "rank_up",
            {"user": "alice", "rank": "Grip"},
        )

        # Should be only 1 in the queue
        assert announcer._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_different_messages_not_deduped(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Different messages are not suppressed by dedup."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce(
            "testchannel", "rank_up",
            {"user": "alice", "rank": "Grip"},
        )
        await announcer.announce(
            "testchannel", "rank_up",
            {"user": "bob", "rank": "Gaffer"},
        )

        assert announcer._queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_rate_limiting(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """15 rapid announcements → max 10 sent via flush loop."""
        announcer = _make_announcer(mock_client, sample_config)
        # Minimize delays for test speed
        announcer._batch_delay_seconds = 0.01
        announcer._dedup_window_seconds = 0  # Disable dedup for this test

        # Queue 15 unique messages
        for i in range(15):
            await announcer._queue.put(("testchannel", f"Message {i}"))

        # Run flush loop briefly
        await announcer.start()
        await asyncio.sleep(0.5)
        await announcer.stop()

        # Should have sent at most 10 (rate limit per minute)
        assert mock_client.send_chat.call_count <= 10

    @pytest.mark.asyncio
    async def test_raw_announcement(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """announce_raw bypasses template rendering."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce_raw("testchannel", "Raw message here")

        assert not announcer._queue.empty()
        channel, message = await announcer._queue.get()
        assert message == "Raw message here"

    @pytest.mark.asyncio
    async def test_raw_announcement_dedup(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """announce_raw is still subject to dedup."""
        announcer = _make_announcer(mock_client, sample_config)

        await announcer.announce_raw("testchannel", "Same message")
        await announcer.announce_raw("testchannel", "Same message")

        assert announcer._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_batch_delay(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """Messages are delayed by the batch window before sending."""
        announcer = _make_announcer(mock_client, sample_config)
        announcer._batch_delay_seconds = 0.05

        await announcer.start()
        await announcer._queue.put(("testchannel", "Delayed msg"))

        # Immediately after queueing, nothing sent yet
        # (we need to give the loop a chance to pick it up but not enough for batch delay)
        await asyncio.sleep(0.01)
        # The message is waiting for batch_delay
        # After batch_delay + transit, it should be sent
        await asyncio.sleep(0.15)
        await announcer.stop()

        assert mock_client.send_chat.call_count >= 1

    @pytest.mark.asyncio
    async def test_update_config(
        self, mock_client: MagicMock, sample_config: EconomyConfig,
    ) -> None:
        """update_config swaps the config reference."""
        announcer = _make_announcer(mock_client, sample_config)
        old_config = announcer._config
        new_config = MagicMock()
        announcer.update_config(new_config)
        assert announcer._config is new_config
        assert announcer._config is not old_config
