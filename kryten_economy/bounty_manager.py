"""Bounty manager — user-created Z-funded bounties.

Sprint 7: Competitive Events, Multipliers & Bounties.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BountyConfig, EconomyConfig
    from .database import EconomyDatabase


class BountyManager:
    """Manages user-created bounties."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        client: object,
        logger: logging.Logger,
    ) -> None:
        self._config = config.bounties
        self._full_config = config
        self._db = database
        self._client = client
        self._logger = logger

    def update_config(self, new_config) -> None:
        """Hot-swap the config reference."""
        self._config = new_config.bounties
        self._full_config = new_config

    async def create_bounty(
        self,
        creator: str,
        channel: str,
        amount: int,
        description: str,
    ) -> dict:
        """Create a new bounty. Debits creator's balance.

        Returns ``{success: bool, bounty_id: int, message: str}``.
        """
        cfg = self._config

        if not cfg.enabled:
            return {"success": False, "bounty_id": 0, "message": "Bounties are disabled."}

        if amount < cfg.min_amount:
            return {"success": False, "bounty_id": 0, "message": f"Minimum bounty: {cfg.min_amount:,} Z"}
        if amount > cfg.max_amount:
            return {"success": False, "bounty_id": 0, "message": f"Maximum bounty: {cfg.max_amount:,} Z"}
        if len(description) > cfg.description_max_length:
            return {
                "success": False,
                "bounty_id": 0,
                "message": f"Description max {cfg.description_max_length} chars.",
            }

        # Check open bounty limit
        open_bounties = await self._db.get_open_bounties(channel)
        user_open = [b for b in open_bounties if b["creator"] == creator]
        if len(user_open) >= cfg.max_open_per_user:
            return {
                "success": False,
                "bounty_id": 0,
                "message": (
                    f"You already have {len(user_open)} open bounties "
                    f"(max {cfg.max_open_per_user})."
                ),
            }

        # Debit the bounty amount (debit returns new balance or None)
        new_balance = await self._db.debit(
            creator,
            channel,
            amount,
            tx_type="bounty_create",
            trigger_id="bounty.create",
            reason=f"Bounty: {description[:50]}",
        )
        if new_balance is None:
            return {"success": False, "bounty_id": 0, "message": "Insufficient funds."}

        # Calculate expiry
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=cfg.default_expiry_hours)
        ).isoformat()

        bounty_id = await self._db.create_bounty(
            creator, channel, description, amount, expires_at,
        )

        self._logger.info(
            "Bounty #%d created by %s: %d Z '%s'",
            bounty_id,
            creator,
            amount,
            description,
        )

        return {
            "success": True,
            "bounty_id": bounty_id,
            "message": (
                f"📌 Bounty #{bounty_id} created! {amount:,} Z\n"
                f'   "{description}"\n'
                f"   Expires in {cfg.default_expiry_hours} hours."
            ),
        }

    async def claim_bounty(
        self,
        bounty_id: int,
        channel: str,
        winner: str,
        admin: str,
    ) -> str:
        """Admin claims a bounty for a winner. Credits winner."""
        bounty = await self._db.get_bounty(bounty_id, channel)
        if not bounty:
            return f"Bounty #{bounty_id} not found."
        if bounty["status"] != "open":
            return f"Bounty #{bounty_id} is already {bounty['status']}."

        # Claim it
        claimed = await self._db.claim_bounty(bounty_id, channel, winner, admin)
        if not claimed:
            return "Failed to claim bounty."

        # Credit the winner
        await self._db.credit(
            winner,
            channel,
            bounty["amount"],
            tx_type="bounty_claim",
            trigger_id=f"bounty.claim.{bounty_id}",
            reason=f"Bounty #{bounty_id}: {bounty['description'][:50]}",
        )

        # Notify creator
        await self._client.send_pm(
            channel,
            bounty["creator"],
            f"📌 Your bounty #{bounty_id} was claimed by {winner}! "
            f"({bounty['amount']:,} Z awarded)",
        )

        # Notify winner
        await self._client.send_pm(
            channel,
            winner,
            f"🎯 You earned bounty #{bounty_id}: {bounty['description']}! "
            f"+{bounty['amount']:,} Z",
        )

        # Public announcement
        await self._client.send_chat(
            channel,
            f'🎯 {winner} claimed bounty #{bounty_id}: '
            f'"{bounty["description"]}" (+{bounty["amount"]:,} Z)',
        )

        return f"Bounty #{bounty_id} claimed by {winner}. {bounty['amount']:,} Z awarded."

    async def process_expired_bounties(self, channel: str) -> int:
        """Expire old bounties and refund creators partially.

        Called periodically by scheduler. Returns count of expired bounties.
        """
        expired = await self._db.expire_bounties(channel)
        refund_pct = self._config.expiry_refund_percent

        for bounty in expired:
            refund = int(bounty["amount"] * refund_pct / 100)
            if refund > 0:
                await self._db.credit(
                    bounty["creator"],
                    channel,
                    refund,
                    tx_type="bounty_expired_refund",
                    trigger_id=f"bounty.expired.{bounty['id']}",
                    reason=f"Bounty #{bounty['id']} expired — {refund_pct}% refund",
                )
                await self._client.send_pm(
                    channel,
                    bounty["creator"],
                    f"📌 Bounty #{bounty['id']} expired. "
                    f"Refund: {refund:,} Z ({refund_pct}% of {bounty['amount']:,} Z)",
                )

            self._logger.info(
                "Bounty #%d expired. Refund %d Z to %s",
                bounty["id"],
                refund,
                bounty["creator"],
            )

        return len(expired)
