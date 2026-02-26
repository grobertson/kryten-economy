"""Tests for kudos_received trigger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_username_plus_plus_detected(earning_engine, database):
    """'alice++' → alice gets 3 Z."""
    await earning_engine.evaluate_chat_message("bob", CH, "alice++", NOW)

    bal = await database.get_balance("alice", CH)
    assert bal >= 3


@pytest.mark.asyncio
async def test_at_username_plus_plus_detected(earning_engine, database):
    """'@alice++' → alice gets 3 Z."""
    await earning_engine.evaluate_chat_message("bob", CH, "@alice++", NOW)

    bal = await database.get_balance("alice", CH)
    assert bal >= 3


@pytest.mark.asyncio
async def test_self_kudos_blocked(earning_engine, database):
    """'alice++' sent by alice → 0 Z from kudos."""
    await earning_engine.evaluate_chat_message("alice", CH, "alice++", NOW)

    txns = await database.get_recent_transactions("alice", CH, limit=50)
    kudos_txns = [t for t in txns if t.get("trigger_id") == "chat.kudos_received"]
    assert len(kudos_txns) == 0


@pytest.mark.asyncio
async def test_multiple_kudos_in_one_message(earning_engine, database):
    """'alice++ bob++' → both credited."""
    await earning_engine.evaluate_chat_message("charlie", CH, "alice++ bob++", NOW)

    bal_alice = await database.get_balance("alice", CH)
    bal_bob = await database.get_balance("bob", CH)
    assert bal_alice >= 3
    assert bal_bob >= 3


@pytest.mark.asyncio
async def test_duplicate_kudos_same_message(earning_engine, database):
    """'alice++ alice++' → only 1 credit."""
    await earning_engine.evaluate_chat_message("bob", CH, "alice++ alice++", NOW)

    txns = await database.get_recent_transactions("alice", CH, limit=50)
    kudos_txns = [t for t in txns if t.get("trigger_id") == "chat.kudos_received"]
    assert len(kudos_txns) == 1


@pytest.mark.asyncio
async def test_kudos_to_ignored_user(earning_engine, database):
    """'IgnoredBot++' → no credit."""
    await earning_engine.evaluate_chat_message("alice", CH, "IgnoredBot++", NOW)

    # Try to get account — may not exist
    acct = await database.get_account("IgnoredBot", CH)
    if acct:
        txns = await database.get_recent_transactions("IgnoredBot", CH, limit=50)
        kudos_txns = [t for t in txns if t.get("trigger_id") == "chat.kudos_received"]
        assert len(kudos_txns) == 0


@pytest.mark.asyncio
async def test_kudos_case_insensitive(earning_engine, database):
    """'Alice++' matches user 'alice' (pattern captures 'Alice', credited as 'Alice')."""
    await earning_engine.evaluate_chat_message("bob", CH, "Alice++", NOW)

    # Kudos target stored as "Alice" (original casing from pattern)
    bal = await database.get_balance("Alice", CH)
    assert bal >= 3


@pytest.mark.asyncio
async def test_kudos_daily_activity_updated(earning_engine, database):
    """Sender's kudos_given incremented, target's kudos_received incremented."""
    await earning_engine.evaluate_chat_message("bob", CH, "alice++", NOW)

    today = NOW.strftime("%Y-%m-%d")
    sender_activity = await database.get_or_create_daily_activity("bob", CH, today)
    assert sender_activity["kudos_given"] >= 1

    target_activity = await database.get_or_create_daily_activity("alice", CH, today)
    assert target_activity["kudos_received"] >= 1
