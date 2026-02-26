"""Tests for laugh_received trigger."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_lol_detected_as_laugh(earning_engine):
    """'lol' → laugh detected."""
    assert earning_engine._is_laugh("lol")


@pytest.mark.asyncio
async def test_haha_detected_as_laugh(earning_engine):
    """'hahaha' → laugh detected."""
    assert earning_engine._is_laugh("hahaha")


@pytest.mark.asyncio
async def test_emoji_laugh_detected(earning_engine):
    """'😂' → laugh detected."""
    assert earning_engine._is_laugh("😂")


@pytest.mark.asyncio
async def test_normal_message_not_laugh(earning_engine):
    """'hello there' → not detected."""
    assert not earning_engine._is_laugh("hello there")


@pytest.mark.asyncio
async def test_laugh_credits_joke_teller(earning_engine, channel_state, database):
    """Laugher says 'lol' → previous sender gets 2 Z."""
    # alice tells a joke
    channel_state.record_message(CH, "alice", NOW - timedelta(seconds=10))

    # bob laughs
    await earning_engine.evaluate_chat_message("bob", CH, "lol", NOW)

    # alice should have received credit
    bal = await database.get_balance("alice", CH)
    assert bal >= 2


@pytest.mark.asyncio
async def test_laugh_self_excluded(earning_engine, channel_state, database):
    """User laughs at own message → no credit."""
    channel_state.record_message(CH, "alice", NOW - timedelta(seconds=10))

    # alice laughs at herself
    await earning_engine.evaluate_chat_message("alice", CH, "lol", NOW)

    # alice's balance should only be from other triggers (first_message, conversation_starter)
    # NOT from laugh_received
    bal = await database.get_balance("alice", CH)
    # If she got a laugh credit of 2, we'd see that. Check no laugh transaction:
    txns = await database.get_recent_transactions("alice", CH, limit=50)
    laugh_txns = [t for t in txns if t.get("trigger_id") == "chat.laugh_received"]
    assert len(laugh_txns) == 0


@pytest.mark.asyncio
async def test_laugh_max_laughers_cap(earning_engine, channel_state, database):
    """11th laugher at same joke → blocked."""
    # alice tells a joke
    channel_state.record_message(CH, "alice", NOW - timedelta(seconds=30))

    # 10 people laugh (cap = 10)
    for i in range(10):
        ts = NOW + timedelta(seconds=i)
        await earning_engine.evaluate_chat_message(f"user{i}", CH, "lol", ts)

    # 11th laugh should not credit alice further
    bal_before = await database.get_balance("alice", CH)
    ts11 = NOW + timedelta(seconds=10)
    await earning_engine.evaluate_chat_message("user10", CH, "lol", ts11)
    bal_after = await database.get_balance("alice", CH)
    assert bal_after == bal_before


@pytest.mark.asyncio
async def test_laugh_no_previous_sender(earning_engine, database):
    """Laugh with no prior message → no credit."""
    # No previous message recorded
    await earning_engine.evaluate_chat_message("bob", CH, "lol", NOW)

    txns = await database.get_recent_transactions("bob", CH, limit=50)
    laugh_txns = [t for t in txns if t.get("trigger_id") == "chat.laugh_received"]
    assert len(laugh_txns) == 0


@pytest.mark.asyncio
async def test_laugh_ignored_user_no_joke_credit(
    earning_engine, channel_state, database,
):
    """If joke-teller is ignored user → no credit (they can't receive)."""
    # Ignored user tells a joke
    channel_state.record_message(CH, "IgnoredBot", NOW - timedelta(seconds=10))

    # bob laughs
    await earning_engine.evaluate_chat_message("bob", CH, "lol", NOW)

    # IgnoredBot should have no transactions
    txns = await database.get_recent_transactions("IgnoredBot", CH, limit=50)
    assert len(txns) == 0
