"""Channel state tracker — volatile per-channel state for the earning engine.

All state is in-memory only. On restart, triggers that depend on this state
simply start fresh (no historical credit, no harm).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig


@dataclass
class MediaInfo:
    """Currently playing media item."""

    title: str
    media_id: str
    duration_seconds: float
    started_at: datetime
    users_present_at_start: set[str] = field(default_factory=set)


@dataclass
class ChannelState:
    """Volatile state for a single channel."""

    # Media tracking
    current_media: MediaInfo | None = None

    # Conversation starter
    last_message_time: datetime | None = None
    last_message_user: str | None = None

    # First-after-media-change
    first_comment_after_media: str | None = None
    media_change_time: datetime | None = None

    # Newcomer greeting detection: {username_lower: join_time}
    recent_joins: dict[str, datetime] = field(default_factory=dict)

    # Per-media-item state for comment_during_media: {username: count}
    comment_counts_this_media: dict[str, int] = field(default_factory=dict)

    # Per-media-item state for like_current
    users_liked_current: set[str] = field(default_factory=set)

    # Per-media-item state for survived_full_media
    users_at_media_start: set[str] = field(default_factory=set)


class ChannelStateTracker:
    """Manages volatile per-channel state for the earning engine."""

    def __init__(self, config: EconomyConfig, logger: logging.Logger) -> None:
        self._config = config
        self._logger = logger
        self._states: dict[str, ChannelState] = {}
        self._ignored_users: set[str] = {u.lower() for u in (config.ignored_users or [])}

    def _get(self, channel: str) -> ChannelState:
        if channel not in self._states:
            self._states[channel] = ChannelState()
        return self._states[channel]

    # ══════════════════════════════════════════════════════════
    #  Message tracking
    # ══════════════════════════════════════════════════════════

    def record_message(self, channel: str, username: str, timestamp: datetime) -> None:
        """Record that a message was sent. Updates conversation-starter tracking."""
        state = self._get(channel)
        state.last_message_time = timestamp
        state.last_message_user = username

    def get_silence_seconds(self, channel: str, now: datetime) -> float | None:
        """Seconds since last message in channel. None if no messages recorded."""
        state = self._get(channel)
        if state.last_message_time is None:
            return None
        return (now - state.last_message_time).total_seconds()

    def get_last_non_self_message_user(self, channel: str, current_user: str) -> str | None:
        """Return the username who sent the last message before this one,
        excluding current_user. Returns None if no qualifying message found."""
        state = self._get(channel)
        if state.last_message_user and state.last_message_user.lower() != current_user.lower():
            return state.last_message_user
        return None

    # ══════════════════════════════════════════════════════════
    #  Media tracking
    # ══════════════════════════════════════════════════════════

    def handle_media_change(
        self,
        channel: str,
        title: str,
        media_id: str,
        duration_seconds: float,
        connected_users: set[str],
        timestamp: datetime,
    ) -> MediaInfo | None:
        """Process a media change event. Returns the PREVIOUS media info
        (for survived_full_media evaluation), or None if there was none."""
        state = self._get(channel)
        previous = state.current_media

        non_ignored = {u for u in connected_users if u.lower() not in self._ignored_users}

        state.current_media = MediaInfo(
            title=title,
            media_id=media_id,
            duration_seconds=duration_seconds,
            started_at=timestamp,
            users_present_at_start=non_ignored,
        )

        # Reset per-media counters
        state.first_comment_after_media = None
        state.media_change_time = timestamp
        state.comment_counts_this_media.clear()
        state.users_liked_current.clear()
        state.users_at_media_start = non_ignored.copy()

        return previous

    def get_current_media(self, channel: str) -> MediaInfo | None:
        return self._get(channel).current_media

    # ══════════════════════════════════════════════════════════
    #  First-after-media-change
    # ══════════════════════════════════════════════════════════

    def try_claim_first_after_media(
        self, channel: str, username: str, now: datetime,
    ) -> bool:
        """Attempt to claim 'first comment after media change'.
        Returns True if this user is the first (and within the window)."""
        state = self._get(channel)
        if state.first_comment_after_media is not None:
            return False
        if state.media_change_time is None:
            return False

        window = self._config.content_triggers.first_after_media_change.window_seconds
        elapsed = (now - state.media_change_time).total_seconds()
        if elapsed > window:
            return False

        state.first_comment_after_media = username
        return True

    # ══════════════════════════════════════════════════════════
    #  Comment-during-media tracking
    # ══════════════════════════════════════════════════════════

    def increment_media_comments(self, channel: str, username: str) -> int:
        """Increment and return the user's comment count for the current media."""
        state = self._get(channel)
        count = state.comment_counts_this_media.get(username, 0) + 1
        state.comment_counts_this_media[username] = count
        return count

    def get_media_comment_cap(self, channel: str) -> int:
        """Calculate the comment cap for the current media."""
        cfg = self._config.content_triggers.comment_during_media
        base_cap = cfg.max_per_item_base
        state = self._get(channel)

        if cfg.scale_with_duration and state.current_media:
            duration_min = state.current_media.duration_seconds / 60
            scaled = int(base_cap * (duration_min / 30))
            return max(scaled, base_cap)

        return base_cap

    # ══════════════════════════════════════════════════════════
    #  Like tracking
    # ══════════════════════════════════════════════════════════

    def try_like_current(self, channel: str, username: str) -> bool:
        """Attempt to like the current media. Returns True if this is a new like."""
        state = self._get(channel)
        if state.current_media is None:
            return False
        if username in state.users_liked_current:
            return False
        state.users_liked_current.add(username)
        return True

    # ══════════════════════════════════════════════════════════
    #  Newcomer tracking
    # ══════════════════════════════════════════════════════════

    def record_genuine_join(self, channel: str, username: str, timestamp: datetime) -> None:
        """Called by presence_tracker when a genuine (debounced) arrival occurs."""
        if username.lower() in self._ignored_users:
            return
        state = self._get(channel)
        state.recent_joins[username.lower()] = timestamp

    def get_recent_joiners(
        self, channel: str, now: datetime, window_seconds: int,
    ) -> dict[str, datetime]:
        """Return {username_lower: join_time} for users who joined within window.
        Prunes expired entries."""
        state = self._get(channel)
        active: dict[str, datetime] = {}
        expired: list[str] = []
        for uname, join_time in state.recent_joins.items():
            if (now - join_time).total_seconds() <= window_seconds:
                active[uname] = join_time
            else:
                expired.append(uname)
        for uname in expired:
            del state.recent_joins[uname]
        return active

    def consume_greeting(self, channel: str, joiner_name_lower: str) -> None:
        """Remove a joiner from recent_joins after they've been greeted."""
        state = self._get(channel)
        state.recent_joins.pop(joiner_name_lower, None)

    # ══════════════════════════════════════════════════════════
    #  Survived-full-media helpers
    # ══════════════════════════════════════════════════════════

    def get_users_at_media_start(self, channel: str) -> set[str]:
        return self._get(channel).users_at_media_start
