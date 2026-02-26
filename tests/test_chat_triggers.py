"""Tests for chat triggers: long_message, first_message_of_day, conversation_starter."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
#  long_message
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_long_message_qualifies(earning_engine):
    """30-char message → 1 Z."""
    msg = "x" * 30
    outcome = await earning_engine.evaluate_chat_message("alice", CH, msg, NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.long_message"]
    assert len(results) == 1
    assert results[0].amount == 1


@pytest.mark.asyncio
async def test_short_message_rejected(earning_engine):
    """29-char message → 0 Z."""
    msg = "x" * 29
    outcome = await earning_engine.evaluate_chat_message("alice", CH, msg, NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.long_message"]
    assert len(results) == 1
    assert results[0].amount == 0
    assert results[0].blocked_by == "condition"


@pytest.mark.asyncio
async def test_long_message_hourly_cap(earning_engine):
    """31st long message in hour → blocked."""
    msg = "x" * 30
    for i in range(30):
        ts = NOW + timedelta(seconds=i)
        await earning_engine.evaluate_chat_message("alice", CH, msg, ts)

    # 31st should be capped
    ts31 = NOW + timedelta(seconds=30)
    outcome = await earning_engine.evaluate_chat_message("alice", CH, msg, ts31)
    results = [r for r in outcome.results if r.trigger_id == "chat.long_message"]
    assert results[0].amount == 0
    assert results[0].blocked_by == "cap"


@pytest.mark.asyncio
async def test_long_message_cap_resets_after_hour(earning_engine):
    """After 1 hour, cap resets."""
    msg = "x" * 30
    for i in range(30):
        ts = NOW + timedelta(seconds=i)
        await earning_engine.evaluate_chat_message("alice", CH, msg, ts)

    # After 1 hour should work again
    ts_later = NOW + timedelta(hours=1, seconds=1)
    outcome = await earning_engine.evaluate_chat_message("alice", CH, msg, ts_later)
    results = [r for r in outcome.results if r.trigger_id == "chat.long_message"]
    assert results[0].amount == 1


# ═══════════════════════════════════════════════════════════
#  first_message_of_day
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_first_message_of_day_awarded(earning_engine):
    """First message → 5 Z, flag set."""
    outcome = await earning_engine.evaluate_chat_message("bob", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.first_message_of_day"]
    assert len(results) == 1
    assert results[0].amount == 5


@pytest.mark.asyncio
async def test_first_message_of_day_no_double(earning_engine):
    """Second message same day → 0 Z."""
    await earning_engine.evaluate_chat_message("bob", CH, "hello", NOW)
    outcome = await earning_engine.evaluate_chat_message(
        "bob", CH, "hello again", NOW + timedelta(minutes=1),
    )
    results = [r for r in outcome.results if r.trigger_id == "chat.first_message_of_day"]
    assert results[0].amount == 0
    assert results[0].blocked_by == "cap"


@pytest.mark.asyncio
async def test_first_message_of_day_resets_next_day(earning_engine):
    """New calendar day → eligible again."""
    await earning_engine.evaluate_chat_message("bob", CH, "hello", NOW)
    next_day = NOW + timedelta(days=1)
    outcome = await earning_engine.evaluate_chat_message("bob", CH, "morning", next_day)
    results = [r for r in outcome.results if r.trigger_id == "chat.first_message_of_day"]
    assert results[0].amount == 5


# ═══════════════════════════════════════════════════════════
#  conversation_starter
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_conversation_starter_after_silence(earning_engine, channel_state):
    """10 min silence → 10 Z."""
    # Set up a message 11 minutes ago
    old_time = NOW - timedelta(minutes=11)
    channel_state.record_message(CH, "someone", old_time)

    outcome = await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.conversation_starter"]
    assert len(results) == 1
    assert results[0].amount == 10


@pytest.mark.asyncio
async def test_conversation_starter_no_silence(earning_engine, channel_state):
    """Message 5 min after last → 0 Z."""
    recent = NOW - timedelta(minutes=5)
    channel_state.record_message(CH, "someone", recent)

    outcome = await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.conversation_starter"]
    assert results[0].amount == 0
    assert results[0].blocked_by == "condition"


@pytest.mark.asyncio
async def test_conversation_starter_first_ever_message(earning_engine):
    """No prior messages (None silence) → qualifies."""
    outcome = await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.conversation_starter"]
    assert results[0].amount == 10


@pytest.mark.asyncio
async def test_conversation_starter_ignored_user_no_silence_reset(
    earning_engine, channel_state,
):
    """Ignored user's message doesn't update last_message_time."""
    # Old message
    old_time = NOW - timedelta(minutes=11)
    channel_state.record_message(CH, "someone", old_time)

    # Ignored user sends (but engine rejects & doesn't record)
    await earning_engine.evaluate_chat_message(
        "IgnoredBot", CH, "beep", NOW - timedelta(minutes=5),
    )

    # Next real user should still see silence
    outcome = await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "chat.conversation_starter"]
    assert results[0].amount == 10
