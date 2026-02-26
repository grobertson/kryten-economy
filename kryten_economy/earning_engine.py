"""Earning engine — central evaluation pipeline for chat-based earning triggers.

Evaluates every chatmsg against 12 configurable triggers across three
categories (chat, content engagement, social) and awards bonus Z.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .channel_state import ChannelStateTracker, MediaInfo
from .database import EconomyDatabase
from .utils import parse_timestamp

if TYPE_CHECKING:
    from .config import EconomyConfig
    from .presence_tracker import PresenceTracker


# ═══════════════════════════════════════════════════════════════
#  Detection patterns
# ═══════════════════════════════════════════════════════════════

LAUGH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:lol|lmao|lmfao|rofl|haha|hahaha+|hehe+|kek|dead)\b",
        re.IGNORECASE,
    ),
    re.compile(r"[💀😂🤣]"),
    re.compile(r"\b(?:ha){2,}\b", re.IGNORECASE),
    re.compile(r"^(?:lol|lmao|lmfao|rofl)$", re.IGNORECASE),
]

KUDOS_PATTERN = re.compile(r"(?:^|\s)@?(\S+)\+\+", re.IGNORECASE)

GIF_PATTERN = re.compile(
    r"https?://\S+\.gif(?:\?\S*)?"
    r"|https?://(?:media\.)?giphy\.com/\S+"
    r"|https?://tenor\.com/\S+"
    r"|https?://i\.imgur\.com/\S+\.gif",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════
#  Data classes
# ═══════════════════════════════════════════════════════════════


@dataclass
class TriggerResult:
    """Outcome of a single trigger evaluation."""

    trigger_id: str
    amount: int  # Whole Z awarded (0 if cooldown/cap blocked)
    blocked_by: str | None = None  # "cooldown", "cap", "disabled", "condition", None


@dataclass
class EarningOutcome:
    """Result of evaluating all triggers for a single chat message."""

    username: str
    channel: str
    results: list[TriggerResult] = field(default_factory=list)

    @property
    def total_earned(self) -> int:
        return sum(r.amount for r in self.results)

    @property
    def awarded_triggers(self) -> list[TriggerResult]:
        return [r for r in self.results if r.amount > 0]


# ═══════════════════════════════════════════════════════════════
#  Engine
# ═══════════════════════════════════════════════════════════════


class EarningEngine:
    """Evaluates chat messages against all configured earning triggers."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        channel_state: ChannelStateTracker,
        logger: logging.Logger,
        presence_tracker: PresenceTracker | None = None,
    ) -> None:
        self._config = config
        self._db = database
        self._channel_state = channel_state
        self._logger = logger
        self._presence_tracker = presence_tracker

        # Fractional earning accumulators: (username, channel, trigger_id) → float
        self._fractional: dict[tuple[str, str, str], float] = {}

        # Ignored users (lowercase) for fast lookup
        self._ignored_users: set[str] = {
            u.lower() for u in (config.ignored_users or [])
        }

        # Unique emote tracking: (username, channel, date) → set[str]
        self._emote_sets: dict[tuple[str, str, str], set[str]] = {}

        # Known channel emotes — populated externally by EconomyApp
        self._known_emotes: set[str] = set()

    def update_config(self, new_config: EconomyConfig) -> None:
        """Hot-swap the config reference."""
        self._config = new_config
        self._ignored_users = {u.lower() for u in new_config.ignored_users}

    # ══════════════════════════════════════════════════════════
    #  Main evaluation method
    # ══════════════════════════════════════════════════════════

    async def evaluate_chat_message(
        self,
        username: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> EarningOutcome:
        """Evaluate a chat message against all configured triggers.

        First gate: reject if username is in ignored_users (case-insensitive).
        Then per-trigger: enabled → cooldown → cap → condition → award.
        """
        outcome = EarningOutcome(username=username, channel=channel)

        # ── Gate: Ignored users earn nothing ────────────────
        if username.lower() in self._ignored_users:
            return outcome

        # ── Gate: Banned users earn nothing ─────────────────
        if await self._db.is_banned(username, channel):
            return outcome

        # ── Evaluate chat triggers ──────────────────────────
        chat_cfg = self._config.chat_triggers

        if chat_cfg.long_message.enabled:
            outcome.results.append(
                await self._eval_long_message(username, channel, message, timestamp)
            )

        if chat_cfg.first_message_of_day.enabled:
            outcome.results.append(
                await self._eval_first_message_of_day(username, channel, timestamp)
            )

        if chat_cfg.conversation_starter.enabled:
            outcome.results.append(
                await self._eval_conversation_starter(username, channel, timestamp)
            )

        # first_after_media_change (under content_triggers in config)
        if self._config.content_triggers.first_after_media_change.enabled:
            outcome.results.append(
                await self._eval_first_after_media_change(username, channel, timestamp)
            )

        # ── Evaluate content engagement triggers ────────────
        content_cfg = self._config.content_triggers

        if content_cfg.comment_during_media.enabled:
            outcome.results.append(
                await self._eval_comment_during_media(username, channel, message, timestamp)
            )

        # survived_full_media is NOT evaluated per-message

        # ── Evaluate social triggers ────────────────────────
        social_cfg = self._config.social_triggers

        if social_cfg.greeted_newcomer.enabled:
            outcome.results.append(
                await self._eval_greeted_newcomer(username, channel, message, timestamp)
            )

        if social_cfg.mentioned_by_other.enabled:
            await self._eval_mentioned_by_other(
                username, channel, message, timestamp, outcome,
            )

        # bot_interaction is evaluated externally (see evaluate_bot_interaction)

        # ── Reactive triggers (laugh + kudos) ───────────────
        if chat_cfg.laugh_received.enabled:
            laugh_result = await self._eval_laugh_received(
                username, channel, message, timestamp,
            )
            if laugh_result and laugh_result.amount > 0:
                joke_teller = self._channel_state.get_last_non_self_message_user(
                    channel, username,
                )
                if joke_teller and joke_teller.lower() not in self._ignored_users:
                    await self._db.credit(
                        joke_teller,
                        channel,
                        laugh_result.amount,
                        tx_type="earn",
                        trigger_id=laugh_result.trigger_id,
                        reason=f"Laugh from {username}",
                        related_user=username,
                    )
                    await self._record_analytics(
                        channel, laugh_result.trigger_id, laugh_result.amount, timestamp,
                    )
                    today = timestamp.strftime("%Y-%m-%d")
                    await self._db.increment_daily_laughs_received(
                        joke_teller, channel, today,
                    )

        if chat_cfg.kudos_received.enabled:
            kudos_results = await self._eval_kudos_received(
                username, channel, message, timestamp,
            )
            for target, result in kudos_results:
                if result.amount > 0:
                    await self._db.credit(
                        target,
                        channel,
                        result.amount,
                        tx_type="earn",
                        trigger_id=result.trigger_id,
                        reason=f"Kudos from {username}",
                        related_user=username,
                    )
                    await self._record_analytics(
                        channel, result.trigger_id, result.amount, timestamp,
                    )
                    today = timestamp.strftime("%Y-%m-%d")
                    await self._db.increment_daily_kudos_received(target, channel, today)
                    await self._db.increment_daily_kudos_given(username, channel, today)

        # ── Track daily activity ────────────────────────────
        await self._update_daily_activity(username, channel, message, timestamp)

        # ── Credit earned Z (standard pipeline results) ─────
        for result in outcome.awarded_triggers:
            await self._db.credit(
                username,
                channel,
                result.amount,
                tx_type="earn",
                trigger_id=result.trigger_id,
                reason=f"Chat trigger: {result.trigger_id}",
            )
            await self._record_analytics(
                channel, result.trigger_id, result.amount, timestamp,
            )

        # ── Update last message time (AFTER trigger eval) ───
        self._channel_state.record_message(channel, username, timestamp)

        return outcome

    # ══════════════════════════════════════════════════════════
    #  Chat triggers
    # ══════════════════════════════════════════════════════════

    async def _eval_long_message(
        self,
        username: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> TriggerResult:
        trigger_id = "chat.long_message"
        cfg = self._config.chat_triggers.long_message

        if len(message) < cfg.min_chars:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        if not await self._check_cooldown(
            username, channel, trigger_id, cfg.max_per_hour, 3600, timestamp,
        ):
            return TriggerResult(trigger_id, 0, blocked_by="cap")

        return TriggerResult(trigger_id, cfg.reward)

    async def _eval_first_message_of_day(
        self,
        username: str,
        channel: str,
        timestamp: datetime,
    ) -> TriggerResult:
        trigger_id = "chat.first_message_of_day"
        cfg = self._config.chat_triggers.first_message_of_day
        today = timestamp.strftime("%Y-%m-%d")

        activity = await self._db.get_or_create_daily_activity(username, channel, today)
        if activity.get("first_message_claimed"):
            return TriggerResult(trigger_id, 0, blocked_by="cap")

        await self._db.mark_first_message_claimed(username, channel, today)
        return TriggerResult(trigger_id, cfg.reward)

    async def _eval_conversation_starter(
        self,
        username: str,
        channel: str,
        timestamp: datetime,
    ) -> TriggerResult:
        trigger_id = "chat.conversation_starter"
        cfg = self._config.chat_triggers.conversation_starter

        silence = self._channel_state.get_silence_seconds(channel, timestamp)

        # None means no messages recorded yet (fresh start) — qualifies
        if silence is not None and silence < cfg.min_silence_minutes * 60:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        return TriggerResult(trigger_id, cfg.reward)

    # ══════════════════════════════════════════════════════════
    #  Reactive chat triggers (credit other users)
    # ══════════════════════════════════════════════════════════

    async def _eval_laugh_received(
        self,
        laughing_user: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> TriggerResult | None:
        """Evaluate if this message is a laugh reaction.
        Returns a TriggerResult for the JOKE-TELLER (not the laugher), or None.
        """
        trigger_id = "chat.laugh_received"
        cfg = self._config.chat_triggers.laugh_received

        if not self._is_laugh(message):
            return None

        joke_teller = self._channel_state.get_last_non_self_message_user(
            channel, laughing_user,
        )
        if joke_teller is None:
            return None

        # Self-exclusion
        if cfg.self_excluded and laughing_user.lower() == joke_teller.lower():
            return None

        # Cap: max laughers per joke (per joke-teller, rolling 5-min window)
        if not await self._check_cooldown(
            joke_teller, channel, trigger_id, cfg.max_laughers_per_joke, 300, timestamp,
        ):
            return None

        return TriggerResult(trigger_id, cfg.reward_per_laugher)

    async def _eval_kudos_received(
        self,
        sender: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> list[tuple[str, TriggerResult]]:
        """Detect kudos targets in message. Returns list of (target, TriggerResult)."""
        trigger_id = "chat.kudos_received"
        cfg = self._config.chat_triggers.kudos_received
        results: list[tuple[str, TriggerResult]] = []

        matches = KUDOS_PATTERN.findall(message)
        seen_targets: set[str] = set()

        for target_raw in matches:
            target = target_raw.strip().lower()

            if target in seen_targets:
                continue
            seen_targets.add(target)

            # Self-exclusion
            if cfg.self_excluded and target == sender.lower():
                continue

            # Ignored users cannot receive kudos
            if target in self._ignored_users:
                continue

            results.append((
                target_raw,  # Preserve original casing
                TriggerResult(trigger_id, cfg.reward),
            ))

        return results

    # ══════════════════════════════════════════════════════════
    #  Content engagement triggers
    # ══════════════════════════════════════════════════════════

    async def _eval_first_after_media_change(
        self,
        username: str,
        channel: str,
        timestamp: datetime,
    ) -> TriggerResult:
        trigger_id = "content.first_after_media_change"
        cfg = self._config.content_triggers.first_after_media_change

        claimed = self._channel_state.try_claim_first_after_media(
            channel, username, timestamp,
        )
        if not claimed:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        return TriggerResult(trigger_id, cfg.reward)

    async def _eval_comment_during_media(
        self,
        username: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> TriggerResult:
        trigger_id = "content.comment_during_media"
        cfg = self._config.content_triggers.comment_during_media

        media = self._channel_state.get_current_media(channel)
        if media is None:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        comment_count = self._channel_state.increment_media_comments(channel, username)
        cap = self._channel_state.get_media_comment_cap(channel)
        if comment_count > cap:
            return TriggerResult(trigger_id, 0, blocked_by="cap")

        amount = self._accumulate_fractional(
            username, channel, trigger_id, cfg.reward_per_message,
        )
        return TriggerResult(trigger_id, amount)

    async def evaluate_like_current(
        self, username: str, channel: str,
    ) -> TriggerResult:
        """Called by PM handler when user sends 'like'."""
        trigger_id = "content.like_current"
        cfg = self._config.content_triggers.like_current

        if not cfg.enabled:
            return TriggerResult(trigger_id, 0, blocked_by="disabled")

        if username.lower() in self._ignored_users:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        if not self._channel_state.try_like_current(channel, username):
            return TriggerResult(trigger_id, 0, blocked_by="cap")

        await self._db.credit(
            username,
            channel,
            cfg.reward,
            tx_type="earn",
            trigger_id=trigger_id,
            reason="Liked current media",
        )
        now = datetime.now(timezone.utc)
        await self._record_analytics(channel, trigger_id, cfg.reward, now)

        return TriggerResult(trigger_id, cfg.reward)

    async def evaluate_survived_full_media(
        self,
        channel: str,
        previous_media: MediaInfo,
        currently_connected: set[str],
        now: datetime,
    ) -> list[str]:
        """Evaluate survived_full_media for the media that just ended.
        Returns list of usernames who earned the reward.
        """
        trigger_id = "content.survived_full_media"
        cfg = self._config.content_triggers.survived_full_media

        if not cfg.enabled:
            return []

        if previous_media.duration_seconds <= 0:
            return []

        # How long did the media actually play?
        actual_seconds = (now - previous_media.started_at).total_seconds()
        presence_ratio = actual_seconds / previous_media.duration_seconds

        if presence_ratio < (cfg.min_presence_percent / 100):
            return []

        # Users present at start AND still connected now
        survivors = previous_media.users_present_at_start & currently_connected
        survivors = {u for u in survivors if u.lower() not in self._ignored_users}

        # Filter out banned users
        unbanned = set()
        for u in survivors:
            if not await self._db.is_banned(u, channel):
                unbanned.add(u)
        survivors = unbanned

        rewarded: list[str] = []
        today = now.strftime("%Y-%m-%d")
        for username in survivors:
            await self._db.credit(
                username,
                channel,
                cfg.reward,
                tx_type="earn",
                trigger_id=trigger_id,
                reason=f"Survived: {previous_media.title}",
            )
            await self._record_analytics(channel, trigger_id, cfg.reward, now)
            rewarded.append(username)

        return rewarded

    # ══════════════════════════════════════════════════════════
    #  Social triggers
    # ══════════════════════════════════════════════════════════

    async def _eval_greeted_newcomer(
        self,
        username: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> TriggerResult:
        trigger_id = "social.greeted_newcomer"
        cfg = self._config.social_triggers.greeted_newcomer

        recent = self._channel_state.get_recent_joiners(
            channel, timestamp, cfg.window_seconds,
        )
        if not recent:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        message_lower = message.lower()

        for joiner_name, _join_time in recent.items():
            # Can't greet yourself
            if joiner_name == username.lower():
                continue

            if joiner_name in message_lower:
                self._channel_state.consume_greeting(channel, joiner_name)
                return TriggerResult(trigger_id, cfg.reward)

        return TriggerResult(trigger_id, 0, blocked_by="condition")

    async def _eval_mentioned_by_other(
        self,
        sender: str,
        channel: str,
        message: str,
        timestamp: datetime,
        outcome: EarningOutcome,
    ) -> None:
        """Credits mentioned users directly. Does not return a TriggerResult."""
        trigger_id = "social.mentioned_by_other"
        cfg = self._config.social_triggers.mentioned_by_other
        message_lower = message.lower()

        if self._presence_tracker is None:
            return

        connected = self._presence_tracker.get_connected_users(channel)

        for target in connected:
            if target.lower() == sender.lower():
                continue
            if target.lower() in self._ignored_users:
                continue

            if target.lower() in message_lower:
                cooldown_key = f"{trigger_id}.{sender.lower()}.{target.lower()}"
                if not await self._check_cooldown(
                    target, channel, cooldown_key, cfg.max_per_hour_same_user, 3600, timestamp,
                ):
                    continue

                await self._db.credit(
                    target,
                    channel,
                    cfg.reward,
                    tx_type="earn",
                    trigger_id=trigger_id,
                    reason=f"Mentioned by {sender}",
                    related_user=sender,
                )
                await self._record_analytics(
                    channel, trigger_id, cfg.reward, timestamp,
                )

    async def evaluate_bot_interaction(
        self,
        responding_to_user: str,
        channel: str,
        timestamp: datetime,
    ) -> TriggerResult:
        """Called when the bot responds to a user."""
        trigger_id = "social.bot_interaction"
        cfg = self._config.social_triggers.bot_interaction

        if not cfg.enabled:
            return TriggerResult(trigger_id, 0, blocked_by="disabled")

        if responding_to_user.lower() in self._ignored_users:
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        if await self._db.is_banned(responding_to_user, channel):
            return TriggerResult(trigger_id, 0, blocked_by="condition")

        today = timestamp.strftime("%Y-%m-%d")
        activity = await self._db.get_or_create_daily_activity(
            responding_to_user, channel, today,
        )
        if activity.get("bot_interactions", 0) >= cfg.max_per_day:
            return TriggerResult(trigger_id, 0, blocked_by="cap")

        await self._db.increment_daily_bot_interactions(
            responding_to_user, channel, today,
        )

        await self._db.credit(
            responding_to_user,
            channel,
            cfg.reward,
            tx_type="earn",
            trigger_id=trigger_id,
            reason="Bot interaction",
        )
        await self._record_analytics(channel, trigger_id, cfg.reward, timestamp)

        return TriggerResult(trigger_id, cfg.reward)

    # ══════════════════════════════════════════════════════════
    #  Cooldown & cap system
    # ══════════════════════════════════════════════════════════

    async def _check_cooldown(
        self,
        username: str,
        channel: str,
        trigger_id: str,
        max_count: int,
        window_seconds: int,
        now: datetime,
    ) -> bool:
        """Check if user is within cooldown/cap. Returns True if ALLOWED."""
        row = await self._db.get_trigger_cooldown(username, channel, trigger_id)

        if row is None:
            await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
            return True

        window_start = parse_timestamp(row["window_start"])
        if window_start is None:
            await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
            return True

        elapsed = (now - window_start).total_seconds()

        if elapsed >= window_seconds:
            await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
            return True

        if row["count"] >= max_count:
            return False

        await self._db.increment_trigger_cooldown(username, channel, trigger_id)
        return True

    # ══════════════════════════════════════════════════════════
    #  Fractional accumulator
    # ══════════════════════════════════════════════════════════

    def _accumulate_fractional(
        self,
        username: str,
        channel: str,
        trigger_id: str,
        amount: float,
    ) -> int:
        """Add fractional amount. Returns whole Z to credit (may be 0)."""
        key = (username, channel, trigger_id)
        current = self._fractional.get(key, 0.0)
        current += amount
        whole = int(current)
        self._fractional[key] = current - whole
        return whole

    # ══════════════════════════════════════════════════════════
    #  Analytics
    # ══════════════════════════════════════════════════════════

    async def _record_analytics(
        self,
        channel: str,
        trigger_id: str,
        amount: int,
        timestamp: datetime,
    ) -> None:
        date = timestamp.strftime("%Y-%m-%d")
        await self._db.record_trigger_analytics(channel, trigger_id, date, amount)

    # ══════════════════════════════════════════════════════════
    #  Daily activity tracking
    # ══════════════════════════════════════════════════════════

    async def _update_daily_activity(
        self,
        username: str,
        channel: str,
        message: str,
        timestamp: datetime,
    ) -> None:
        today = timestamp.strftime("%Y-%m-%d")

        await self._db.increment_daily_messages_sent(username, channel, today)

        if len(message) >= self._config.chat_triggers.long_message.min_chars:
            await self._db.increment_daily_long_messages(username, channel, today)

        if self._is_gif(message):
            await self._db.increment_daily_gifs_posted(username, channel, today)

        # Unique emote tracking
        emotes_in_message = self._extract_emotes(message)
        if emotes_in_message:
            key = (username, channel, today)
            if key not in self._emote_sets:
                self._emote_sets[key] = set()
            new_emotes = emotes_in_message - self._emote_sets[key]
            if new_emotes:
                self._emote_sets[key] |= new_emotes
                await self._db.set_daily_unique_emotes(
                    username, channel, today, len(self._emote_sets[key]),
                )

        # Prune old date emote sets
        self._prune_emote_sets(today)

    # ══════════════════════════════════════════════════════════
    #  Detection helpers
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _is_laugh(message: str) -> bool:
        return any(p.search(message) for p in LAUGH_PATTERNS)

    @staticmethod
    def _is_gif(message: str) -> bool:
        return bool(GIF_PATTERN.search(message))

    def _extract_emotes(self, message: str) -> set[str]:
        """Extract known emote names from the message."""
        found: set[str] = set()
        for emote_name in self._known_emotes:
            if emote_name in message:
                found.add(emote_name)
        return found

    def _prune_emote_sets(self, current_date: str) -> None:
        """Remove emote sets for past dates."""
        expired = [k for k in self._emote_sets if k[2] != current_date]
        for k in expired:
            del self._emote_sets[k]
