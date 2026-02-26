"""Tests for core earning engine pipeline."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_ignored_user_earns_nothing(earning_engine, database):
    """Message from ignored user → empty outcome, no DB writes."""
    outcome = await earning_engine.evaluate_chat_message(
        "IgnoredBot", CH, "hello world this is a long message indeed!", NOW,
    )
    assert outcome.total_earned == 0
    assert len(outcome.results) == 0


@pytest.mark.asyncio
async def test_ignored_user_case_insensitive(earning_engine, database):
    """'IgnoredBot' in config matches 'ignoredbot' message sender."""
    outcome = await earning_engine.evaluate_chat_message(
        "ignoredbot", CH, "hello world this is a long message indeed!", NOW,
    )
    assert outcome.total_earned == 0


@pytest.mark.asyncio
async def test_multiple_triggers_fire(earning_engine, database):
    """Single message triggers long_message + first_message_of_day → total is sum."""
    msg = "x" * 30  # Exactly 30 chars
    outcome = await earning_engine.evaluate_chat_message("alice", CH, msg, NOW)

    trigger_ids = {r.trigger_id for r in outcome.results if r.amount > 0}
    assert "chat.long_message" in trigger_ids
    assert "chat.first_message_of_day" in trigger_ids
    # long_message=1 + first_message_of_day=5 + conversation_starter=10 = 16 (base, without content)
    assert outcome.total_earned >= 6  # At least long_message + first_message_of_day


@pytest.mark.asyncio
async def test_disabled_trigger_skipped(sample_config, database, channel_state):
    """Trigger with enabled: false → not evaluated."""
    sample_config.chat_triggers.long_message.enabled = False
    engine = EarningEngine(sample_config, database, channel_state, logging.getLogger("test"))

    msg = "x" * 50
    outcome = await engine.evaluate_chat_message("alice", CH, msg, NOW)

    trigger_ids = {r.trigger_id for r in outcome.results}
    assert "chat.long_message" not in trigger_ids


@pytest.mark.asyncio
async def test_transactions_logged_per_trigger(earning_engine, database):
    """Each awarded trigger creates a separate transaction."""
    msg = "x" * 30
    await earning_engine.evaluate_chat_message("alice", CH, msg, NOW)

    txns = await database.get_recent_transactions("alice", CH, limit=50)
    trigger_ids = [t["trigger_id"] for t in txns]
    # Should have individual transactions for each awarded trigger
    assert len(trigger_ids) >= 2


@pytest.mark.asyncio
async def test_trigger_analytics_updated(earning_engine, database):
    """Successful trigger → analytics table incremented."""
    msg = "x" * 30
    await earning_engine.evaluate_chat_message("alice", CH, msg, NOW)

    # Check analytics table via raw query
    import asyncio

    def _check():
        conn = database._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM trigger_analytics WHERE trigger_id = 'chat.long_message'"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    result = await asyncio.get_running_loop().run_in_executor(None, _check)
    assert result is not None
    assert result["hit_count"] >= 1
    assert result["total_z_awarded"] >= 1


@pytest.mark.asyncio
async def test_empty_message_no_triggers(earning_engine, database):
    """Empty string message → no trigger fires (except conversation_starter/first_message)."""
    outcome = await earning_engine.evaluate_chat_message("alice", CH, "", NOW)

    # long_message should not fire
    long_msg_results = [r for r in outcome.results if r.trigger_id == "chat.long_message"]
    assert all(r.amount == 0 for r in long_msg_results)
