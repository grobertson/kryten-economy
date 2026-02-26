"""Achievement engine — evaluates configurable conditions and awards one-time badges.

Sprint 6: Achievements, Named Ranks & CyTube Promotion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AchievementConditionConfig, AchievementConfig, EconomyConfig
    from .database import EconomyDatabase


class AchievementEngine:
    """Evaluates achievement conditions and awards one-time badges."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        client: object,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._db = database
        self._client = client
        self._logger = logger

        # Pre-index achievements by condition type for efficient lookup
        self._by_condition_type: dict[str, list[AchievementConfig]] = {}
        for ach in config.achievements:
            ctype = ach.condition.type
            self._by_condition_type.setdefault(ctype, []).append(ach)

    # ── Condition type → evaluator method name ───────────────

    _CONDITION_MAP: dict[str, str] = {
        "lifetime_messages": "_eval_lifetime_messages",
        "lifetime_presence_hours": "_eval_lifetime_presence_hours",
        "daily_streak": "_eval_daily_streak",
        "unique_tip_recipients": "_eval_unique_tip_recipients",
        "unique_tip_senders": "_eval_unique_tip_senders",
        "lifetime_earned": "_eval_lifetime_earned",
        "lifetime_spent": "_eval_lifetime_spent",
        "lifetime_gambled": "_eval_lifetime_gambled",
        "gambling_biggest_win": "_eval_gambling_biggest_win",
        "rank_reached": "_eval_rank_reached",
        "unique_emotes_used_lifetime": "_eval_unique_emotes",
    }

    def update_config(self, new_config) -> None:
        """Hot-swap the config reference. Re-index condition map."""
        self._config = new_config
        self._by_condition_type = {}
        for ach in new_config.achievements:
            ctype = ach.condition.type
            self._by_condition_type.setdefault(ctype, []).append(ach)

    # ══════════════════════════════════════════════════════════
    #  Public API
    # ══════════════════════════════════════════════════════════

    async def check_achievements(
        self,
        username: str,
        channel: str,
        relevant_types: list[str] | None = None,
    ) -> list[AchievementConfig]:
        """Check all achievements (or those matching *relevant_types*) for a user.

        Returns list of newly awarded achievements.
        """
        awarded: list[AchievementConfig] = []
        types_to_check = relevant_types or list(self._by_condition_type.keys())

        for ctype in types_to_check:
            for ach in self._by_condition_type.get(ctype, []):
                # Skip if already earned
                if await self._db.has_achievement(username, channel, ach.id):
                    continue

                # Evaluate condition
                if await self._evaluate_condition(username, channel, ach.condition):
                    newly = await self._db.award_achievement(username, channel, ach.id)
                    if newly:
                        # Credit reward
                        if ach.reward > 0:
                            await self._db.credit(
                                username,
                                channel,
                                ach.reward,
                                tx_type="achievement",
                                trigger_id=f"achievement.{ach.id}",
                                reason=f"Achievement: {ach.description}",
                            )
                        awarded.append(ach)
                        self._logger.info(
                            "Achievement awarded: %s → %s (+%d Z) in %s",
                            username,
                            ach.id,
                            ach.reward,
                            channel,
                        )

        # Notify for each awarded achievement
        for ach in awarded:
            await self._notify_achievement(username, channel, ach)

        return awarded

    # ══════════════════════════════════════════════════════════
    #  Condition Dispatch
    # ══════════════════════════════════════════════════════════

    async def _evaluate_condition(
        self,
        username: str,
        channel: str,
        condition: AchievementConditionConfig,
    ) -> bool:
        """Evaluate a single achievement condition."""
        evaluator_name = self._CONDITION_MAP.get(condition.type)
        if not evaluator_name:
            self._logger.warning("Unknown achievement condition type: %s", condition.type)
            return False
        evaluator = getattr(self, evaluator_name)
        return await evaluator(username, channel, condition)

    # ══════════════════════════════════════════════════════════
    #  Condition Evaluators
    # ══════════════════════════════════════════════════════════

    async def _eval_lifetime_messages(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        total = await self._db.get_lifetime_messages(username, channel)
        return total >= condition.threshold

    async def _eval_lifetime_presence_hours(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        hours = await self._db.get_lifetime_presence_hours(username, channel)
        return hours >= condition.threshold

    async def _eval_daily_streak(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        streak = await self._db.get_or_create_streak(username, channel)
        return streak.get("current_daily_streak", 0) >= condition.threshold

    async def _eval_unique_tip_recipients(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        count = await self._db.get_unique_tip_recipients(username, channel)
        return count >= condition.threshold

    async def _eval_unique_tip_senders(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        count = await self._db.get_unique_tip_senders(username, channel)
        return count >= condition.threshold

    async def _eval_lifetime_earned(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        earned = await self._db.get_lifetime_earned(username, channel)
        return earned >= condition.threshold

    async def _eval_lifetime_spent(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        account = await self._db.get_account(username, channel)
        if not account:
            return False
        return account.get("lifetime_spent", 0) >= condition.threshold

    async def _eval_lifetime_gambled(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        total = await self._db.get_lifetime_gambled(username, channel)
        return total >= condition.threshold

    async def _eval_gambling_biggest_win(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        biggest = await self._db.get_biggest_gambling_win(username, channel)
        return biggest >= condition.threshold

    async def _eval_rank_reached(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        """Check if user has reached a specific rank tier index."""
        account = await self._db.get_account(username, channel)
        if not account:
            return False
        current_tier = self._get_rank_tier_index(account)
        return current_tier >= condition.threshold

    async def _eval_unique_emotes(
        self, username: str, channel: str, condition: AchievementConditionConfig,
    ) -> bool:
        account = await self._db.get_account(username, channel)
        if not account:
            return False
        return account.get("unique_emotes_used", 0) >= condition.threshold

    # ══════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════

    def _get_rank_tier_index(self, account: dict) -> int:
        """0-based tier index for a user's lifetime earnings."""
        lifetime = account.get("lifetime_earned", 0)
        tier_index = 0
        for i, tier in enumerate(self._config.ranks.tiers):
            if lifetime >= tier.min_lifetime_earned:
                tier_index = i
        return tier_index

    async def _notify_achievement(
        self,
        username: str,
        channel: str,
        achievement: AchievementConfig,
    ) -> None:
        """Send PM and optional public announcement for an achievement."""
        symbol = self._config.currency.symbol
        await self._client.send_pm(
            channel,
            username,
            f"🏆 Achievement Unlocked: {achievement.description}! "
            f"+{achievement.reward:,} {symbol}",
        )

        # Public announcement if configured
        if self._config.announcements.achievement_milestone:
            msg = f"🏆 {username} unlocked: {achievement.description}!"
            await self._client.send_chat(channel, msg)
