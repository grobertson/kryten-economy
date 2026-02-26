"""Tests for social earning triggers: greeted_newcomer, mentioned_by_other, bot_interaction."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
#  greeted_newcomer
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_greeted_newcomer_within_window(earning_engine, channel_state):
    """Existing user says newcomer's name within 60s of join → 3 Z."""
    channel_state.record_genuine_join(CH, "newbie", NOW - timedelta(seconds=30))

    outcome = await earning_engine.evaluate_chat_message(
        "alice", CH, "hey newbie welcome!", NOW,
    )
    results = [r for r in outcome.results if r.trigger_id == "social.greeted_newcomer"]
    assert len(results) == 1
    assert results[0].amount == 3


@pytest.mark.asyncio
async def test_greeted_newcomer_after_window(earning_engine, channel_state):
    """Greeting 61s after join → 0 Z."""
    channel_state.record_genuine_join(CH, "newbie", NOW - timedelta(seconds=61))

    outcome = await earning_engine.evaluate_chat_message(
        "alice", CH, "hey newbie!", NOW,
    )
    results = [r for r in outcome.results if r.trigger_id == "social.greeted_newcomer"]
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_greeted_newcomer_only_first_greeter(earning_engine, channel_state):
    """Second person greeting same newcomer → 0 Z."""
    channel_state.record_genuine_join(CH, "newbie", NOW - timedelta(seconds=10))

    # alice greets first
    await earning_engine.evaluate_chat_message("alice", CH, "hi newbie", NOW)

    # bob tries after
    outcome = await earning_engine.evaluate_chat_message(
        "bob", CH, "hello newbie", NOW + timedelta(seconds=5),
    )
    results = [r for r in outcome.results if r.trigger_id == "social.greeted_newcomer"]
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_greeted_newcomer_self_greet(earning_engine, channel_state):
    """Newcomer's own message mentioning own name → 0 Z."""
    channel_state.record_genuine_join(CH, "newbie", NOW - timedelta(seconds=10))

    outcome = await earning_engine.evaluate_chat_message(
        "newbie", CH, "hey everyone, newbie here!", NOW,
    )
    results = [r for r in outcome.results if r.trigger_id == "social.greeted_newcomer"]
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_greeted_newcomer_bot_join_excluded(earning_engine, channel_state):
    """Bot joins don't appear in recent_joins → no greeting reward."""
    # IgnoredBot is in the ignored_users list
    channel_state.record_genuine_join(CH, "IgnoredBot", NOW - timedelta(seconds=10))

    outcome = await earning_engine.evaluate_chat_message(
        "alice", CH, "hey IgnoredBot", NOW,
    )
    results = [r for r in outcome.results if r.trigger_id == "social.greeted_newcomer"]
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_greeted_newcomer_bounced_join_excluded(earning_engine, channel_state):
    """Non-genuine (debounced) join → not in recent_joins.
    Only record_genuine_join calls add to the tracker."""
    # We do NOT call record_genuine_join
    outcome = await earning_engine.evaluate_chat_message(
        "alice", CH, "hey newbie", NOW,
    )
    results = [r for r in outcome.results if r.trigger_id == "social.greeted_newcomer"]
    assert results[0].amount == 0


# ═══════════════════════════════════════════════════════════
#  mentioned_by_other
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mentioned_by_other_earns(
    sample_config, database, channel_state,
):
    """'hey alice' when alice is connected → alice gets 1 Z."""
    presence = MagicMock()
    presence.get_connected_users.return_value = {"alice", "bob"}

    engine = EarningEngine(
        sample_config, database, channel_state,
        logging.getLogger("test"), presence_tracker=presence,
    )

    await engine.evaluate_chat_message("bob", CH, "hey alice nice one", NOW)

    bal = await database.get_balance("alice", CH)
    assert bal >= 1  # at least the mention reward


@pytest.mark.asyncio
async def test_mentioned_self_no_earn(
    sample_config, database, channel_state,
):
    """'hey alice' sent by alice → 0 Z."""
    presence = MagicMock()
    presence.get_connected_users.return_value = {"alice"}

    engine = EarningEngine(
        sample_config, database, channel_state,
        logging.getLogger("test"), presence_tracker=presence,
    )

    await engine.evaluate_chat_message("alice", CH, "hey alice", NOW)

    bal = await database.get_balance("alice", CH)
    # alice should not earn mention reward for self-mention
    # (she may earn other triggers, so check transactions)
    txns = await database.get_recent_transactions("alice", CH, 50)
    mention_txns = [t for t in txns if t.get("trigger_id") == "social.mentioned_by_other"]
    assert len(mention_txns) == 0


@pytest.mark.asyncio
async def test_mentioned_by_other_hourly_cap(
    sample_config, database, channel_state,
):
    """6th mention from same sender → blocked (cap is 5)."""
    presence = MagicMock()
    presence.get_connected_users.return_value = {"alice", "bob"}

    engine = EarningEngine(
        sample_config, database, channel_state,
        logging.getLogger("test"), presence_tracker=presence,
    )

    for i in range(5):
        await engine.evaluate_chat_message(
            "bob", CH, "hey alice", NOW + timedelta(seconds=i * 10),
        )

    # 6th mention from bob to alice
    await engine.evaluate_chat_message(
        "bob", CH, "hey alice again", NOW + timedelta(seconds=60),
    )

    txns = await database.get_recent_transactions("alice", CH, 50)
    mention_txns = [t for t in txns if t.get("trigger_id") == "social.mentioned_by_other"]
    assert len(mention_txns) == 5  # capped at 5


@pytest.mark.asyncio
async def test_mentioned_multiple_users(
    sample_config, database, channel_state,
):
    """'hey alice and bob' → both credited."""
    presence = MagicMock()
    presence.get_connected_users.return_value = {"alice", "bob", "charlie"}

    engine = EarningEngine(
        sample_config, database, channel_state,
        logging.getLogger("test"), presence_tracker=presence,
    )

    await engine.evaluate_chat_message("charlie", CH, "hey alice and bob", NOW)

    txns_alice = await database.get_recent_transactions("alice", CH, 50)
    txns_bob = await database.get_recent_transactions("bob", CH, 50)
    assert any(t.get("trigger_id") == "social.mentioned_by_other" for t in txns_alice)
    assert any(t.get("trigger_id") == "social.mentioned_by_other" for t in txns_bob)


@pytest.mark.asyncio
async def test_mentioned_ignored_user(
    sample_config, database, channel_state,
):
    """Mentioning ignored user → 0 Z."""
    presence = MagicMock()
    presence.get_connected_users.return_value = {"alice", "IgnoredBot"}

    engine = EarningEngine(
        sample_config, database, channel_state,
        logging.getLogger("test"), presence_tracker=presence,
    )

    await engine.evaluate_chat_message("alice", CH, "hey IgnoredBot", NOW)

    txns = await database.get_recent_transactions("IgnoredBot", CH, 50)
    mention_txns = [t for t in txns if t.get("trigger_id") == "social.mentioned_by_other"]
    assert len(mention_txns) == 0


# ═══════════════════════════════════════════════════════════
#  bot_interaction
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bot_interaction_earns(earning_engine, database):
    """Bot response after user message → user gets 2 Z."""
    result = await earning_engine.evaluate_bot_interaction("alice", CH, NOW)
    assert result.amount == 2

    bal = await database.get_balance("alice", CH)
    assert bal >= 2


@pytest.mark.asyncio
async def test_bot_interaction_daily_cap(earning_engine, database):
    """11th bot interaction → blocked (max_per_day=10)."""
    for i in range(10):
        result = await earning_engine.evaluate_bot_interaction(
            "alice", CH, NOW + timedelta(seconds=i),
        )
        assert result.amount == 2

    result = await earning_engine.evaluate_bot_interaction(
        "alice", CH, NOW + timedelta(seconds=100),
    )
    assert result.amount == 0
    assert result.blocked_by == "cap"


@pytest.mark.asyncio
async def test_bot_interaction_disabled(sample_config_dict, database, channel_state):
    """Config disabled → no credit."""
    sample_config_dict["social_triggers"]["bot_interaction"]["enabled"] = False
    from kryten_economy.config import EconomyConfig
    config = EconomyConfig(**sample_config_dict)

    engine = EarningEngine(config, database, channel_state, logging.getLogger("test"))
    result = await engine.evaluate_bot_interaction("alice", CH, NOW)
    assert result.amount == 0
    assert result.blocked_by == "disabled"
