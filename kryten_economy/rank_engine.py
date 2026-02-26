"""Rank engine — manages named rank progression based on lifetime earnings.

Sprint 6: Achievements, Named Ranks & CyTube Promotion.
"""

from __future__ import annotations

import logging
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
        """PM user and optionally announce publicly."""
        perks_str = ", ".join(tier.perks) if tier.perks else "No additional perks"
        await self._client.send_pm(
            channel,
            username,
            f"⭐ Rank Up! You are now a **{tier.name}**!\n"
            f"Perks: {perks_str}",
        )

        if self._config.announcements.rank_promotion:
            template = self._config.announcements.templates.rank_up
            msg = template.format(user=username, rank=tier.name)
            await self._client.send_chat(channel, msg)

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
                await self._client.send_pm(
                    channel,
                    username,
                    f"🎬 You've been promoted to CyTube Level {level}! "
                    f"Look at that shiny name in the user list!",
                )
            else:
                self._logger.warning(
                    "CyTube rank change failed for %s: %s",
                    username,
                    result.get("error"),
                )
        except Exception as e:
            self._logger.error("CyTube rank promotion error: %s", e)
