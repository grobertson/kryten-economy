"""Competition engine — evaluates daily competitions at end-of-day and awards prizes.

Sprint 7: Competitive Events, Multipliers & Bounties.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import CompetitionConfig, EconomyConfig
    from .database import EconomyDatabase


class CompetitionEngine:
    """Evaluates daily competitions at end-of-day and awards prizes."""

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
        self._competitions = config.daily_competitions

    def update_config(self, new_config) -> None:
        """Hot-swap the config reference."""
        self._config = new_config
        self._competitions = new_config.daily_competitions

    async def evaluate_daily_competitions(
        self, channel: str, date: str,
    ) -> list[dict]:
        """Run all configured daily competitions.

        Called by scheduler at end-of-day (23:59 UTC).
        Returns list of award records.
        """
        all_awards: list[dict] = []

        for comp in self._competitions:
            try:
                awards = await self._evaluate_one(comp, channel, date)
                all_awards.extend(awards)
            except Exception as e:
                self._logger.error(
                    "Competition %s evaluation failed: %s", comp.id, e,
                )

        # Credit all awards
        symbol = self._config.currency.symbol
        for award in all_awards:
            await self._db.credit(
                award["username"],
                channel,
                award["reward"],
                tx_type="competition",
                trigger_id=f"competition.{award['competition_id']}",
                reason=award["reason"],
            )
            await self._client.send_pm(
                channel,
                award["username"],
                f"🏅 {award['reason']} — +{award['reward']:,} {symbol}",
            )

        # Public announcement summary
        if all_awards:
            await self._announce_daily_results(channel, all_awards)

        return all_awards

    async def _evaluate_one(
        self, comp: CompetitionConfig, channel: str, date: str,
    ) -> list[dict]:
        """Evaluate a single competition. Returns list of awards."""
        awards: list[dict] = []
        ctype = comp.condition.type

        if ctype == "daily_threshold":
            qualifiers = await self._db.get_daily_threshold_qualifiers(
                channel, date, comp.condition.field or "", comp.condition.threshold or 0,
            )
            for username in qualifiers:
                awards.append({
                    "competition_id": comp.id,
                    "username": username,
                    "reward": comp.reward,
                    "reason": comp.description,
                })

        elif ctype == "daily_top":
            top_users = await self._db.get_daily_top(
                channel, date, comp.condition.field or "", limit=1,
            )
            if top_users:
                winner = top_users[0]
                if comp.reward_percent_of_earnings and comp.reward_percent_of_earnings > 0:
                    day_earned = winner.get("value", 0)
                    reward = max(1, int(day_earned * comp.reward_percent_of_earnings / 100))
                else:
                    reward = comp.reward

                awards.append({
                    "competition_id": comp.id,
                    "username": winner["username"],
                    "reward": reward,
                    "reason": comp.description,
                })

        return awards

    async def _announce_daily_results(
        self, channel: str, awards: list[dict],
    ) -> None:
        """Public announcement of daily competition results."""
        by_comp: dict[str, list[dict]] = {}
        for a in awards:
            by_comp.setdefault(a["competition_id"], []).append(a)

        lines = ["📊 Daily Competition Results:"]
        for comp_id, comp_awards in by_comp.items():
            comp_cfg = next(
                (c for c in self._competitions if c.id == comp_id), None,
            )
            desc = comp_cfg.description if comp_cfg else comp_id
            if len(comp_awards) == 1:
                a = comp_awards[0]
                lines.append(f"  🏅 {desc}: {a['username']} (+{a['reward']:,} Z)")
            else:
                names = ", ".join(a["username"] for a in comp_awards)
                reward = comp_awards[0]["reward"]
                lines.append(f"  🏅 {desc}: {names} (+{reward:,} Z each)")

        await self._client.send_chat(channel, "\n".join(lines))
