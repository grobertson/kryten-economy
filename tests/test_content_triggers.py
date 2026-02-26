"""Tests for content engagement triggers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.channel_state import MediaInfo
from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
#  first_after_media_change
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_first_after_media_change_within_window(
    earning_engine, channel_state,
):
    """Message within 30s → 3 Z."""
    # Simulate media change
    channel_state.handle_media_change(
        CH, "Test", "vid1", 300, {"alice"}, NOW - timedelta(seconds=10),
    )

    outcome = await earning_engine.evaluate_chat_message("alice", CH, "nice", NOW)
    results = [r for r in outcome.results if r.trigger_id == "content.first_after_media_change"]
    assert len(results) == 1
    assert results[0].amount == 3


@pytest.mark.asyncio
async def test_first_after_media_change_too_late(earning_engine, channel_state):
    """Message at 31s → 0 Z."""
    channel_state.handle_media_change(
        CH, "Test", "vid1", 300, set(), NOW - timedelta(seconds=31),
    )

    outcome = await earning_engine.evaluate_chat_message("alice", CH, "nice", NOW)
    results = [r for r in outcome.results if r.trigger_id == "content.first_after_media_change"]
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_first_after_media_change_second_user(earning_engine, channel_state):
    """Second message within window → 0 Z (already claimed)."""
    channel_state.handle_media_change(
        CH, "Test", "vid1", 300, set(), NOW - timedelta(seconds=10),
    )

    # alice claims first
    await earning_engine.evaluate_chat_message("alice", CH, "first!", NOW)

    # bob tries
    outcome = await earning_engine.evaluate_chat_message(
        "bob", CH, "second!", NOW + timedelta(seconds=1),
    )
    results = [r for r in outcome.results if r.trigger_id == "content.first_after_media_change"]
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_first_after_media_change_no_media(earning_engine, channel_state):
    """No media change recorded → 0 Z."""
    outcome = await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "content.first_after_media_change"]
    assert results[0].amount == 0


# ═══════════════════════════════════════════════════════════
#  comment_during_media
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_comment_during_media_earns(earning_engine, channel_state):
    """Message during media → 0.5 Z accumulated."""
    channel_state.handle_media_change(
        CH, "Test", "vid1", 1800, {"alice"}, NOW - timedelta(seconds=30),
    )

    outcome = await earning_engine.evaluate_chat_message("alice", CH, "nice vid", NOW)
    results = [r for r in outcome.results if r.trigger_id == "content.comment_during_media"]
    assert len(results) == 1
    # 0.5 Z → accumulator = 0.5, credit = 0 (fractional)
    assert results[0].amount == 0


@pytest.mark.asyncio
async def test_comment_during_media_fractional(earning_engine, channel_state):
    """Two messages → 1 Z credited."""
    channel_state.handle_media_change(
        CH, "Test", "vid1", 1800, {"alice"}, NOW - timedelta(seconds=30),
    )

    await earning_engine.evaluate_chat_message(
        "alice", CH, "msg1", NOW,
    )
    outcome = await earning_engine.evaluate_chat_message(
        "alice", CH, "msg2", NOW + timedelta(seconds=5),
    )

    results = [r for r in outcome.results if r.trigger_id == "content.comment_during_media"]
    assert results[0].amount == 1  # 0.5 + 0.5 = 1.0


@pytest.mark.asyncio
async def test_comment_during_media_cap(earning_engine, channel_state):
    """Exceeding cap → blocked."""
    # Short video with base cap of 10
    channel_state.handle_media_change(
        CH, "Test", "vid1", 300, {"alice"}, NOW - timedelta(seconds=30),
    )

    for i in range(10):
        await earning_engine.evaluate_chat_message(
            "alice", CH, f"msg{i}", NOW + timedelta(seconds=i),
        )

    # 11th message should be capped
    outcome = await earning_engine.evaluate_chat_message(
        "alice", CH, "msg10", NOW + timedelta(seconds=10),
    )
    results = [r for r in outcome.results if r.trigger_id == "content.comment_during_media"]
    assert results[0].amount == 0
    assert results[0].blocked_by == "cap"


@pytest.mark.asyncio
async def test_comment_during_media_cap_scales(earning_engine, channel_state):
    """60-min media: cap = 10 × (60/30) = 20."""
    channel_state.handle_media_change(
        CH, "Test Movie", "vid2", 3600, {"alice"}, NOW,
    )
    cap = channel_state.get_media_comment_cap(CH)
    assert cap == 20


@pytest.mark.asyncio
async def test_comment_during_media_no_media(earning_engine, channel_state):
    """No media playing → 0 Z."""
    outcome = await earning_engine.evaluate_chat_message("alice", CH, "hello", NOW)
    results = [r for r in outcome.results if r.trigger_id == "content.comment_during_media"]
    assert results[0].amount == 0
    assert results[0].blocked_by == "condition"


# ═══════════════════════════════════════════════════════════
#  like_current
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_like_current_earns(earning_engine, channel_state, database):
    """PM 'like' with media → 2 Z."""
    channel_state.handle_media_change(
        CH, "Cool Vid", "vid1", 300, {"alice"}, NOW,
    )

    result = await earning_engine.evaluate_like_current("alice", CH)
    assert result.amount == 2


@pytest.mark.asyncio
async def test_like_current_double_blocked(earning_engine, channel_state):
    """Second 'like' same media → 0 Z."""
    channel_state.handle_media_change(
        CH, "Cool Vid", "vid1", 300, {"alice"}, NOW,
    )

    await earning_engine.evaluate_like_current("alice", CH)
    result = await earning_engine.evaluate_like_current("alice", CH)
    assert result.amount == 0
    assert result.blocked_by == "cap"


@pytest.mark.asyncio
async def test_like_current_no_media(earning_engine, channel_state):
    """PM 'like' with nothing playing → 0 Z."""
    result = await earning_engine.evaluate_like_current("alice", CH)
    assert result.amount == 0


@pytest.mark.asyncio
async def test_like_current_resets_on_media_change(earning_engine, channel_state):
    """New media → can like again."""
    channel_state.handle_media_change(
        CH, "Vid 1", "vid1", 300, {"alice"}, NOW,
    )
    await earning_engine.evaluate_like_current("alice", CH)

    # New media
    channel_state.handle_media_change(
        CH, "Vid 2", "vid2", 300, {"alice"}, NOW + timedelta(minutes=5),
    )
    result = await earning_engine.evaluate_like_current("alice", CH)
    assert result.amount == 2


# ═══════════════════════════════════════════════════════════
#  survived_full_media
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_survived_full_media_qualifies(earning_engine, database):
    """Present at start + still connected + ≥80% played → 5 Z."""
    media = MediaInfo(
        title="Test", media_id="vid1", duration_seconds=600,
        started_at=NOW, users_present_at_start={"alice", "bob"},
    )

    # 600s later (100% played)
    end = NOW + timedelta(seconds=600)
    connected = {"alice", "bob"}
    rewarded = await earning_engine.evaluate_survived_full_media(
        CH, media, connected, end,
    )
    assert "alice" in rewarded
    assert "bob" in rewarded


@pytest.mark.asyncio
async def test_survived_full_media_left_early(earning_engine, database):
    """User left before end → 0 Z."""
    media = MediaInfo(
        title="Test", media_id="vid1", duration_seconds=600,
        started_at=NOW, users_present_at_start={"alice", "bob"},
    )

    end = NOW + timedelta(seconds=600)
    connected = {"alice"}  # bob left
    rewarded = await earning_engine.evaluate_survived_full_media(
        CH, media, connected, end,
    )
    assert "alice" in rewarded
    assert "bob" not in rewarded


@pytest.mark.asyncio
async def test_survived_full_media_joined_late(earning_engine, database):
    """User joined after media start → 0 Z."""
    media = MediaInfo(
        title="Test", media_id="vid1", duration_seconds=600,
        started_at=NOW, users_present_at_start={"alice"},
    )

    end = NOW + timedelta(seconds=600)
    connected = {"alice", "charlie"}  # charlie wasn't at start
    rewarded = await earning_engine.evaluate_survived_full_media(
        CH, media, connected, end,
    )
    assert "alice" in rewarded
    assert "charlie" not in rewarded


@pytest.mark.asyncio
async def test_survived_full_media_skipped(earning_engine, database):
    """Media skipped at 50% → nobody qualifies."""
    media = MediaInfo(
        title="Test", media_id="vid1", duration_seconds=600,
        started_at=NOW, users_present_at_start={"alice"},
    )

    end = NOW + timedelta(seconds=300)  # Only 50% played
    connected = {"alice"}
    rewarded = await earning_engine.evaluate_survived_full_media(
        CH, media, connected, end,
    )
    assert len(rewarded) == 0


@pytest.mark.asyncio
async def test_survived_full_media_zero_duration(earning_engine, database):
    """Duration 0 (unknown) → skipped."""
    media = MediaInfo(
        title="Test", media_id="vid1", duration_seconds=0,
        started_at=NOW, users_present_at_start={"alice"},
    )

    end = NOW + timedelta(seconds=600)
    connected = {"alice"}
    rewarded = await earning_engine.evaluate_survived_full_media(
        CH, media, connected, end,
    )
    assert len(rewarded) == 0


@pytest.mark.asyncio
async def test_survived_full_media_ignored_user(earning_engine, database):
    """Ignored user present throughout → 0 Z."""
    media = MediaInfo(
        title="Test", media_id="vid1", duration_seconds=600,
        started_at=NOW, users_present_at_start={"IgnoredBot", "alice"},
    )

    end = NOW + timedelta(seconds=600)
    connected = {"IgnoredBot", "alice"}
    rewarded = await earning_engine.evaluate_survived_full_media(
        CH, media, connected, end,
    )
    assert "alice" in rewarded
    assert "IgnoredBot" not in rewarded
