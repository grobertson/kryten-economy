"""Rank engine — manages named rank progression based on lifetime earnings.

Sprint 6: Achievements, Named Ranks & CyTube Promotion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig, RankTierConfig
    from .database import EconomyDatabase


class RankEngine:
    """Manages named rank progression based on lifetime earnings."""

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

        # Pre-sort tiers by min_lifetime_earned ascending
        self._tiers: list[RankTierConfig] = sorted(
            config.ranks.tiers,
            key=lambda t: t.min_lifetime_earned,
        )

        # Buffered rank-up announcements: list of (username, channel, tier)
        self._pending_announcements: list[tuple[str, str, RankTierConfig]] = []

        # Throttle state per channel:
        # {channel: (last_announce_utc, highest_tier_index_today, today_str)}
        self._announce_tracker: dict[str, tuple[datetime, int, str]] = {}

    def update_config(self, new_config) -> None:
        """Hot-swap the config reference. Re-sort tiers."""
        self._config = new_config
        self._tiers = sorted(
            new_config.ranks.tiers,
            key=lambda t: t.min_lifetime_earned,
        )

    # ══════════════════════════════════════════════════════════
    #  Public API
    # ══════════════════════════════════════════════════════════

    def get_rank_for_lifetime(self, lifetime_earned: int) -> tuple[int, RankTierConfig]:
        """Determine rank tier for a given lifetime earned amount.

        Returns ``(tier_index, RankTierConfig)``.
        """
        tier_index = 0
        for i, tier in enumerate(self._tiers):
            if lifetime_earned >= tier.min_lifetime_earned:
                tier_index = i
        return tier_index, self._tiers[tier_index]

    def get_next_tier(self, current_index: int) -> RankTierConfig | None:
        """Get the next rank tier, or ``None`` if at max."""
        if current_index + 1 < len(self._tiers):
            return self._tiers[current_index + 1]
        return None

    async def check_rank_promotion(
        self, username: str, channel: str,
    ) -> RankTierConfig | None:
        """Check if a user should be promoted. Returns new tier or ``None``.

        Call this after any earn event.
        """
        account = await self._db.get_account(username, channel)
        if not account:
            return None

        lifetime = account.get("lifetime_earned", 0)
        current_rank = account.get("rank_name", "")

        new_index, new_tier = self.get_rank_for_lifetime(lifetime)

        if new_tier.name != current_rank:
            # Promotion!
            await self._db.update_account_rank(username, channel, new_tier.name)
            await self._notify_rank_promotion(username, channel, new_tier)

            # Auto CyTube level promotion if configured
            if new_tier.cytube_level_promotion is not None:
                await self._promote_cytube_level(
                    username, channel, new_tier.cytube_level_promotion,
                )

            return new_tier

        return None

    # ══════════════════════════════════════════════════════════
    #  Notifications
    # ══════════════════════════════════════════════════════════

    async def _notify_rank_promotion(
        self,
        username: str,
        channel: str,
        tier: RankTierConfig,
    ) -> None:
        """PM user and buffer public announcement."""
        # Respect quiet mode
        if not await self._db.get_quiet_mode(username, channel):
            perks_str = ", ".join(tier.perks) if tier.perks else "No additional perks"
            try:
                await self._client.send_pm(
                    channel,
                    username,
                    f"\u2b50 Rank Up! You are now a **{tier.name}**!\n"
                    f"Perks: {perks_str}\n"
                    f"(PM 'quiet' to mute notifications)",
                )
            except Exception as e:
                self._logger.warning("Rank-up PM failed for %s: %s", username, e)

        # Public announcement always buffered (regardless of quiet)
        if self._config.announcements.rank_promotion:
            self._pending_announcements.append((username, channel, tier))

    def _get_tier_index(self, tier: RankTierConfig) -> int:
        """Return the index of a tier (higher = more prestigious)."""
        for i, t in enumerate(self._tiers):
            if t.name == tier.name:
                return i
        return 0

    async def flush_pending_announcements(self) -> None:
        """Announce buffered rank-ups, batched per channel with throttle.

        Rules (per channel):
        - At most one announcement per hour.
        - OR if a user reached a higher rank than today's previous best.
        - Multiple users can be batched into one message.
        - Resets daily.
        """
        if not self._pending_announcements:
            return

        # Group by channel
        by_channel: dict[str, list[tuple[str, RankTierConfig]]] = {}
        for username, channel, tier in self._pending_announcements:
            by_channel.setdefault(channel, []).append((username, tier))
        self._pending_announcements.clear()

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        for channel, promotions in by_channel.items():
            # Find the highest tier index in this batch
            max_tier_idx = max(self._get_tier_index(t) for _, t in promotions)

            tracker = self._announce_tracker.get(channel)
            if tracker and tracker[2] == today:
                last_time, best_today, _ = tracker
                elapsed = (now - last_time).total_seconds()

                # Skip if within cooldown AND not a new daily high
                if elapsed < 3600 and max_tier_idx <= best_today:
                    continue

                new_best = max(best_today, max_tier_idx)
            else:
                # First of the day
                new_best = max_tier_idx

            self._announce_tracker[channel] = (now, new_best, today)

            # Build message
            template = self._config.announcements.templates.rank_up
            if len(promotions) == 1:
                user, tier = promotions[0]
                msg = template.format(user=user, rank=tier.name)
            else:
                # Batch: group by rank name
                by_rank: dict[str, list[str]] = {}
                for user, tier in promotions:
                    by_rank.setdefault(tier.name, []).append(user)
                parts = []
                for rank_name, users in by_rank.items():
                    if len(users) == 1:
                        parts.append(f"{users[0]} \u2192 {rank_name}")
                    else:
                        parts.append(f"{', '.join(users)} \u2192 {rank_name}")
                msg = f"\u2b50 Rank ups! {' \u00b7 '.join(parts)}"

            try:
                await self._client.send_chat(channel, msg)
                self._logger.debug("Rank-up announcement: %s", msg)
            except Exception as e:
                self._logger.warning("Rank-up chat announcement failed: %s", e)

    async def _promote_cytube_level(
        self,
        username: str,
        channel: str,
        level: int,
    ) -> None:
        """Promote user to a CyTube level via kryten-py wrapper."""
        try:
            result = await self._client.safe_set_channel_rank(
                channel, username, level,
            )
            if result.get("success"):
                self._logger.info(
                    "CyTube level %d granted to %s in %s",
                    level,
                    username,
                    channel,
                )
                # Respect quiet mode
                if not await self._db.get_quiet_mode(username, channel):
                    await self._client.send_pm(
                        channel,
                        username,
                        f"🎬 You've been promoted to CyTube Level {level}! "
                        f"Look at that shiny name in the user list!\n"
                        f"(PM 'quiet' to mute notifications)",
                    )
            else:
                self._logger.warning(
                    "CyTube rank change failed for %s: %s",
                    username,
                    result.get("error"),
                )
        except Exception as e:
            self._logger.error("CyTube rank promotion error: %s", e)
