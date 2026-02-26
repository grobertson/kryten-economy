"""Tests for daily activity tracking in the earning engine."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_messages_sent_incremented(earning_engine, database):
    """Each message → messages_sent += 1."""
    await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    await earning_engine.evaluate_chat_message(
        "alice", CH, "world", NOW + timedelta(seconds=5),
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["messages_sent"] == 2


@pytest.mark.asyncio
async def test_long_messages_counted(earning_engine, database):
    """30+ char message → long_messages += 1."""
    short = "hi"
    long_msg = "a" * 30

    await earning_engine.evaluate_chat_message("alice", CH, short, NOW)
    await earning_engine.evaluate_chat_message(
        "alice", CH, long_msg, NOW + timedelta(seconds=5),
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["long_messages"] == 1


@pytest.mark.asyncio
async def test_gif_detected(earning_engine, database):
    """Message with .gif URL → gifs_posted += 1."""
    await earning_engine.evaluate_chat_message(
        "alice", CH, "check this https://example.com/funny.gif out", NOW,
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["gifs_posted"] == 1


@pytest.mark.asyncio
async def test_non_gif_url_ignored(earning_engine, database):
    """Regular URL → gifs_posted unchanged."""
    await earning_engine.evaluate_chat_message(
        "alice", CH, "visit https://example.com/page.html", NOW,
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["gifs_posted"] == 0


@pytest.mark.asyncio
async def test_giphy_detected(earning_engine, database):
    """giphy.com link → gifs_posted += 1."""
    await earning_engine.evaluate_chat_message(
        "alice", CH, "https://media.giphy.com/media/abc123/giphy.gif", NOW,
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["gifs_posted"] == 1


@pytest.mark.asyncio
async def test_tenor_detected(earning_engine, database):
    """tenor.com link → gifs_posted += 1."""
    await earning_engine.evaluate_chat_message(
        "alice", CH, "https://tenor.com/view/funny-123", NOW,
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["gifs_posted"] == 1


@pytest.mark.asyncio
async def test_unique_emotes_counted(earning_engine, database):
    """3 different emotes → unique_emotes_used = 3."""
    # Pre-populate known emotes
    earning_engine._known_emotes = {"PogChamp", "Kappa", "LUL"}

    await earning_engine.evaluate_chat_message(
        "alice", CH, "PogChamp Kappa LUL", NOW,
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["unique_emotes_used"] == 3


@pytest.mark.asyncio
async def test_duplicate_emote_not_double_counted(earning_engine, database):
    """Same emote twice → unique_emotes_used = 1."""
    earning_engine._known_emotes = {"PogChamp"}

    await earning_engine.evaluate_chat_message(
        "alice", CH, "PogChamp", NOW,
    )
    await earning_engine.evaluate_chat_message(
        "alice", CH, "PogChamp again", NOW + timedelta(seconds=5),
    )

    activity = await database.get_or_create_daily_activity("alice", CH, TODAY)
    assert activity["unique_emotes_used"] == 1


@pytest.mark.asyncio
async def test_emote_set_resets_on_new_day(earning_engine, database):
    """New date → emote tracking starts fresh."""
    earning_engine._known_emotes = {"PogChamp"}

    await earning_engine.evaluate_chat_message(
        "alice", CH, "PogChamp", NOW,
    )

    # Next day
    tomorrow = NOW + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    await earning_engine.evaluate_chat_message(
        "alice", CH, "PogChamp", tomorrow,
    )

    activity_today = await database.get_or_create_daily_activity("alice", CH, TODAY)
    activity_tomorrow = await database.get_or_create_daily_activity(
        "alice", CH, tomorrow_str,
    )
    assert activity_today["unique_emotes_used"] == 1
    assert activity_tomorrow["unique_emotes_used"] == 1
