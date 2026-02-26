"""Tests for cooldown/cap system in the earning engine."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
#  Basic cooldown flow
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_first_hit_allowed(earning_engine):
    """No prior cooldown → allowed, count = 1."""
    allowed = await earning_engine._check_cooldown(
        "alice", CH, "test.trigger", max_count=5, window_seconds=3600, now=NOW,
    )
    assert allowed is True


@pytest.mark.asyncio
async def test_within_cap_allowed(earning_engine):
    """count < max → allowed, count incremented."""
    for i in range(4):
        result = await earning_engine._check_cooldown(
            "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
            now=NOW + timedelta(seconds=i),
        )
        assert result is True


@pytest.mark.asyncio
async def test_at_cap_blocked(earning_engine):
    """count == max → blocked."""
    for i in range(5):
        await earning_engine._check_cooldown(
            "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
            now=NOW + timedelta(seconds=i),
        )

    blocked = await earning_engine._check_cooldown(
        "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )
    assert blocked is False


@pytest.mark.asyncio
async def test_window_expired_resets(earning_engine):
    """After window_seconds → reset, allowed."""
    for i in range(5):
        await earning_engine._check_cooldown(
            "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
            now=NOW + timedelta(seconds=i),
        )

    # Blocked within window
    blocked = await earning_engine._check_cooldown(
        "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
        now=NOW + timedelta(seconds=100),
    )
    assert blocked is False

    # Window expired → allowed again
    allowed = await earning_engine._check_cooldown(
        "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
        now=NOW + timedelta(hours=1, seconds=1),
    )
    assert allowed is True


# ═══════════════════════════════════════════════════════════
#  Independence
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_different_triggers_independent(earning_engine):
    """Cooldown for trigger A doesn't affect trigger B."""
    for i in range(5):
        await earning_engine._check_cooldown(
            "alice", CH, "trigger.A", max_count=5, window_seconds=3600,
            now=NOW + timedelta(seconds=i),
        )

    # Trigger A capped
    assert not await earning_engine._check_cooldown(
        "alice", CH, "trigger.A", max_count=5, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )

    # Trigger B still fresh
    assert await earning_engine._check_cooldown(
        "alice", CH, "trigger.B", max_count=5, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )


@pytest.mark.asyncio
async def test_different_users_independent(earning_engine):
    """User A's cooldown doesn't affect user B."""
    for i in range(5):
        await earning_engine._check_cooldown(
            "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
            now=NOW + timedelta(seconds=i),
        )

    # Alice capped
    assert not await earning_engine._check_cooldown(
        "alice", CH, "test.trigger", max_count=5, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )

    # Bob still fresh
    assert await earning_engine._check_cooldown(
        "bob", CH, "test.trigger", max_count=5, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )


@pytest.mark.asyncio
async def test_compound_cooldown_key(earning_engine):
    """Compound key 'mentioned_by_other.alice.bob' keyed per pair."""
    key_ab = "social.mentioned_by_other.alice.bob"
    key_ac = "social.mentioned_by_other.alice.charlie"

    for i in range(3):
        await earning_engine._check_cooldown(
            "bob", CH, key_ab, max_count=3, window_seconds=3600,
            now=NOW + timedelta(seconds=i),
        )

    # alice→bob capped
    assert not await earning_engine._check_cooldown(
        "bob", CH, key_ab, max_count=3, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )

    # alice→charlie still fresh
    assert await earning_engine._check_cooldown(
        "charlie", CH, key_ac, max_count=3, window_seconds=3600,
        now=NOW + timedelta(seconds=10),
    )
