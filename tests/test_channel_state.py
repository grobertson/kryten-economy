"""Tests for ChannelStateTracker methods."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.channel_state import ChannelStateTracker, MediaInfo


CH = "testchannel"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
#  Media change
# ═══════════════════════════════════════════════════════════

def test_media_change_returns_previous(channel_state):
    """handle_media_change() returns old MediaInfo."""
    # First change: no previous
    prev = channel_state.handle_media_change(
        CH, "First", "vid1", 300, {"alice"}, NOW,
    )
    assert prev is None

    prev = channel_state.handle_media_change(
        CH, "Second", "vid2", 600, {"alice"}, NOW + timedelta(minutes=5),
    )
    assert prev is not None
    assert prev.title == "First"
    assert prev.media_id == "vid1"


def test_media_change_resets_counters(channel_state):
    """Comment counts, likes, first-claim all reset on media change."""
    channel_state.handle_media_change(CH, "Vid1", "v1", 300, set(), NOW)

    # Build up some state
    channel_state.increment_media_comments(CH, "alice")
    channel_state.try_like_current(CH, "alice")
    channel_state.try_claim_first_after_media(CH, "bob", NOW)

    # New media resets all
    channel_state.handle_media_change(
        CH, "Vid2", "v2", 300, set(), NOW + timedelta(minutes=5),
    )

    assert channel_state.increment_media_comments(CH, "alice") == 1  # fresh count
    assert channel_state.try_like_current(CH, "alice") is True
    assert channel_state.try_claim_first_after_media(
        CH, "charlie", NOW + timedelta(minutes=5),
    ) is True


# ═══════════════════════════════════════════════════════════
#  First-after-media-change
# ═══════════════════════════════════════════════════════════

def test_first_claim_once_per_media(channel_state):
    """try_claim_first_after_media() returns True once, then False."""
    channel_state.handle_media_change(CH, "Vid", "v1", 300, set(), NOW)

    assert channel_state.try_claim_first_after_media(CH, "alice", NOW) is True
    assert channel_state.try_claim_first_after_media(CH, "bob", NOW) is False


# ═══════════════════════════════════════════════════════════
#  Comment-during-media tracking
# ═══════════════════════════════════════════════════════════

def test_comment_count_increments(channel_state):
    """increment_media_comments() returns sequential counts."""
    channel_state.handle_media_change(CH, "Vid", "v1", 300, set(), NOW)

    assert channel_state.increment_media_comments(CH, "alice") == 1
    assert channel_state.increment_media_comments(CH, "alice") == 2
    assert channel_state.increment_media_comments(CH, "alice") == 3
    # Different user starts at 1
    assert channel_state.increment_media_comments(CH, "bob") == 1


# ═══════════════════════════════════════════════════════════
#  Like tracking
# ═══════════════════════════════════════════════════════════

def test_like_once_per_media(channel_state):
    """try_like_current() returns True once per user per media."""
    channel_state.handle_media_change(CH, "Vid", "v1", 300, set(), NOW)

    assert channel_state.try_like_current(CH, "alice") is True
    assert channel_state.try_like_current(CH, "alice") is False  # duplicate
    assert channel_state.try_like_current(CH, "bob") is True  # different user


# ═══════════════════════════════════════════════════════════
#  Newcomer tracking
# ═══════════════════════════════════════════════════════════

def test_genuine_join_recorded(channel_state):
    """record_genuine_join() adds to recent_joins."""
    channel_state.record_genuine_join(CH, "newuser", NOW)

    joiners = channel_state.get_recent_joiners(CH, NOW, window_seconds=60)
    assert "newuser" in joiners


def test_ignored_user_join_not_recorded(channel_state):
    """Ignored user join → not in recent_joins."""
    channel_state.record_genuine_join(CH, "IgnoredBot", NOW)

    joiners = channel_state.get_recent_joiners(CH, NOW, window_seconds=60)
    assert "ignoredbot" not in joiners


def test_recent_joiners_pruned(channel_state):
    """Old joins (outside window) removed on query."""
    channel_state.record_genuine_join(CH, "olduser", NOW - timedelta(seconds=120))
    channel_state.record_genuine_join(CH, "newuser", NOW - timedelta(seconds=10))

    joiners = channel_state.get_recent_joiners(CH, NOW, window_seconds=60)
    assert "newuser" in joiners
    assert "olduser" not in joiners


# ═══════════════════════════════════════════════════════════
#  Silence tracking
# ═══════════════════════════════════════════════════════════

def test_silence_tracking(channel_state):
    """get_silence_seconds() returns correct duration."""
    assert channel_state.get_silence_seconds(CH, NOW) is None  # No messages yet

    channel_state.record_message(CH, "alice", NOW)
    silence = channel_state.get_silence_seconds(CH, NOW + timedelta(seconds=30))
    assert silence == pytest.approx(30.0)


# ═══════════════════════════════════════════════════════════
#  Comment cap scaling
# ═══════════════════════════════════════════════════════════

def test_media_comment_cap_scales(channel_state):
    """60-min media with scale=true → cap = 10 × (60/30) = 20."""
    channel_state.handle_media_change(CH, "Movie", "m1", 3600, set(), NOW)
    cap = channel_state.get_media_comment_cap(CH)
    assert cap == 20


def test_media_comment_cap_no_scale(sample_config_dict):
    """scale=false → base cap."""
    sample_config_dict["content_triggers"]["comment_during_media"]["scale_with_duration"] = False
    from kryten_economy.config import EconomyConfig
    config = EconomyConfig(**sample_config_dict)
    tracker = ChannelStateTracker(config, logging.getLogger("test"))

    tracker.handle_media_change(CH, "Movie", "m1", 3600, set(), NOW)
    cap = tracker.get_media_comment_cap(CH)
    assert cap == 10  # base cap, no scaling
