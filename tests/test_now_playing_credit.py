"""Tests for now-playing queue credit announcements."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.main import EconomyApp


@pytest.mark.asyncio
async def test_resolve_queued_by_from_event(sample_config, mock_client):
    app = EconomyApp("config.example.yaml")
    app.config = sample_config
    app.client = mock_client

    event = MagicMock()
    event.queueby = "Alice"

    queued_by = await app._resolve_queued_by("testchannel", 101, event)
    assert queued_by == "Alice"


@pytest.mark.asyncio
async def test_resolve_queued_by_from_playlist(sample_config, mock_client):
    app = EconomyApp("config.example.yaml")
    app.config = sample_config
    app.client = mock_client

    event = MagicMock()
    event.queueby = None
    mock_client.get_state_playlist_items = AsyncMock(return_value=[
        {"uid": 99, "queueby": "Bob"},
        {"uid": 101, "queueby": "Carol"},
    ])

    queued_by = await app._resolve_queued_by("testchannel", 101, event)
    assert queued_by == "Carol"


@pytest.mark.asyncio
async def test_announce_now_playing_credit_posts_chat(sample_config, mock_client):
    app = EconomyApp("config.example.yaml")
    app.config = sample_config
    app.client = mock_client

    event = MagicMock()
    event.queueby = "Alice"

    await app._announce_now_playing_credit("testchannel", "My Video", 101, event)

    mock_client.send_chat.assert_awaited_once()
    args = mock_client.send_chat.await_args.args
    assert args[0] == "testchannel"
    assert "My Video" in args[1]
    assert "Alice" in args[1]
