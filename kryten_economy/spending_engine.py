"""Spending engine — centralised validation and price calculation.

Every spend action (queue, tip, vanity purchase) flows through this engine
for consistent balance / permission / blackout / discount checking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig
    from .database import EconomyDatabase
    from .float_price_scaler import FloatPriceScaler
    from .media_client import MediaCMSClient


class SpendResult(Enum):
    SUCCESS = "success"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    DAILY_LIMIT = "daily_limit"
    COOLDOWN = "cooldown"
    BLACKOUT = "blackout"
    NOT_FOUND = "not_found"
    DISABLED = "disabled"
    REQUIRES_APPROVAL = "requires_approval"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ARGS = "invalid_args"


@dataclass(frozen=True)
class SpendOutcome:
    result: SpendResult
    message: str
    amount_charged: int = 0
    original_amount: int = 0
    discount_percent: float = 0.0


class SpendingEngine:
    """Centralised spending validation and pricing."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        media_client: MediaCMSClient | None,
        logger: logging.Logger,
        price_scaler: FloatPriceScaler | None = None,
    ) -> None:
        self._config = config
        self._db = database
        self._media = media_client
        self._logger = logger
        self._scaler = price_scaler

    def update_config(self, new_config, price_scaler: FloatPriceScaler | None = None) -> None:
        """Hot-swap the config reference."""
        self._config = new_config
        if price_scaler is not None:
            self._scaler = price_scaler

    # ══════════════════════════════════════════════════════════
    #  Rank Discount
    # ══════════════════════════════════════════════════════════

    def get_rank_discount(self, rank_tier_index: int) -> float:
        """Calculate rank discount fraction (e.g. tier 5 × 0.02 = 0.10)."""
        return self._config.ranks.spend_discount_per_rank * rank_tier_index

    def apply_discount(
        self,
        base_cost: int,
        rank_tier_index: int,
    ) -> tuple[int, float]:
        """Return (final_cost, discount_fraction). Minimum cost is 1."""
        discount = self.get_rank_discount(rank_tier_index)
        discounted = max(1, int(base_cost * (1 - discount)))
        return discounted, discount

    # ══════════════════════════════════════════════════════════
    #  Price Tiers
    # ══════════════════════════════════════════════════════════

    def get_price_tier(self, duration_seconds: int) -> tuple[str, int]:
        """Find the tier label and base cost for a given duration."""
        duration_minutes = duration_seconds / 60
        for tier in self._config.spending.queue_tiers:
            if duration_minutes <= tier.max_minutes:
                return tier.label, tier.cost
        # Fallback to last tier
        last = self._config.spending.queue_tiers[-1]
        return last.label, last.cost

    # ════════════════════════════════════════════════════════
    #  Inflation-Adjusted Pricing (Sprint 10)
    # ════════════════════════════════════════════════════════

    def get_inflated_price(self, base_cost: int) -> int:
        """Apply inflation multiplier to a base cost.

        Returns ``base_cost`` unchanged if the governor is disabled or not wired.
        """
        if self._scaler is None:
            return base_cost
        return self._scaler.scale(base_cost)

    def get_effective_price_tier(self, duration_seconds: int) -> tuple[str, int, int]:
        """Return (label, base_cost, effective_cost) for a video duration.

        ``base_cost`` is the configured value; ``effective_cost`` has inflation applied.
        """
        label, base_cost = self.get_price_tier(duration_seconds)
        return label, base_cost, self.get_inflated_price(base_cost)

    def get_interrupt_play_next_price(self) -> int:
        """Effective price for interrupt_play_next (inflation-adjusted)."""
        return self.get_inflated_price(self._config.spending.interrupt_play_next)

    def get_force_play_now_price(self) -> int:
        """Effective price for force_play_now (inflation-adjusted)."""
        return self.get_inflated_price(self._config.spending.force_play_now)

    def get_vanity_item_price(self, base_cost: int) -> int:
        """Effective price for a vanity shop item (inflation-adjusted)."""
        return self.get_inflated_price(base_cost)

    # ══════════════════════════════════════════════════════════
    #  Validation
    # ══════════════════════════════════════════════════════════

    async def validate_spend(
        self,
        username: str,
        channel: str,
        amount: int,
        spend_type: str,
    ) -> SpendOutcome | None:
        """Common pre-spend validation (account, banned, balance).

        Returns SpendOutcome on failure, None if all checks pass.
        """
        account = await self._db.get_account(username, channel)
        if not account:
            return SpendOutcome(
                result=SpendResult.INSUFFICIENT_FUNDS,
                message="You don't have an account yet. Stick around to earn some Z!",
            )
        if account.get("economy_banned"):
            return SpendOutcome(
                result=SpendResult.PERMISSION_DENIED,
                message="Your economy access has been suspended.",
            )
        if account["balance"] < amount:
            return SpendOutcome(
                result=SpendResult.INSUFFICIENT_FUNDS,
                message=(
                    f"Insufficient funds. You have {account['balance']:,} Z "
                    f"but need {amount:,} Z."
                ),
            )
        return None  # All checks passed

    # ══════════════════════════════════════════════════════════
    #  Rank Tier Lookup
    # ══════════════════════════════════════════════════════════

    def get_rank_tier_index(self, account: dict) -> int:
        """0-based tier index for a user's lifetime earnings."""
        lifetime = account.get("lifetime_earned", 0)
        tier_index = 0
        for i, tier in enumerate(self._config.ranks.tiers):
            if lifetime >= tier.min_lifetime_earned:
                tier_index = i
        return tier_index
