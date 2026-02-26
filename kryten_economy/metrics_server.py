"""Prometheus metrics server for kryten-economy.

Subclasses BaseMetricsServer from kryten-py to expose
economy-specific metrics and health details.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kryten import BaseMetricsServer

if TYPE_CHECKING:
    from .main import EconomyApp


class EconomyMetricsServer(BaseMetricsServer):
    """Economy-specific Prometheus metrics endpoint."""

    def __init__(self, app: EconomyApp, port: int = 28286) -> None:
        super().__init__(
            service_name="economy",
            port=port,
            client=app.client,
            logger=app.logger,
        )
        self._app = app

    async def _collect_custom_metrics(self) -> list[str]:
        """Collect economy-specific Prometheus metrics."""
        lines: list[str] = []

        # ── Counters ─────────────────────────────────────────
        lines.append(f"economy_events_processed_total {self._app.events_processed}")
        lines.append(f"economy_commands_processed_total {self._app.commands_processed}")
        lines.append(f"economy_z_earned_total {self._app.z_earned_total}")
        lines.append(f"economy_z_spent_total {self._app.z_spent_total}")
        lines.append(f"economy_tips_total {self._app.tips_total}")
        lines.append(f"economy_queues_total {self._app.queues_total}")
        lines.append(f"economy_vanity_purchases_total {self._app.vanity_purchases_total}")

        # Sprint 6 counters
        lines.append(
            f"economy_achievements_awarded_total "
            f"{getattr(self._app, 'achievements_awarded_total', 0)}"
        )
        lines.append(
            f"economy_rank_promotions_total "
            f"{getattr(self._app, 'rank_promotions_total', 0)}"
        )

        # Sprint 7 counters
        lines.append(
            f"economy_competition_awards_total "
            f"{getattr(self._app, 'competition_awards_total', 0)}"
        )
        lines.append(
            f"economy_bounties_created_total "
            f"{getattr(self._app, 'bounties_created_total', 0)}"
        )
        lines.append(
            f"economy_bounties_claimed_total "
            f"{getattr(self._app, 'bounties_claimed_total', 0)}"
        )

        # ── Per-channel gauges ───────────────────────────────
        for ch in self._app.config.channels:
            channel = ch.channel
            tag = f'channel="{channel}"'

            # Active users
            present = self._app.presence_tracker.get_connected_count(channel)
            lines.append(f"economy_active_users{{{tag}}} {present}")

            # Circulation & accounts
            circ = await self._app.db.get_total_circulation(channel)
            lines.append(f"economy_total_circulation{{{tag}}} {circ}")

            count = await self._app.db.get_account_count(channel)
            lines.append(f"economy_total_accounts{{{tag}}} {count}")

            # Median balance
            median = await self._app.db.get_median_balance(channel)
            lines.append(f"economy_median_balance{{{tag}}} {median}")

            # Participation rate
            participation = (count / present * 100) if present > 0 else 0
            lines.append(f"economy_participation_rate{{{tag}}} {participation:.2f}")

            # Active multiplier
            if self._app.multiplier_engine:
                combined, _ = self._app.multiplier_engine.get_combined_multiplier(channel)
                lines.append(f"economy_active_multiplier{{{tag}}} {combined:.2f}")

            # Rank distribution
            try:
                rank_dist = await self._app.db.get_rank_distribution(channel)
                for rank_name, rcount in rank_dist.items():
                    lines.append(
                        f'economy_rank_distribution{{{tag},rank="{rank_name}"}} {rcount}'
                    )
            except Exception:
                pass  # get_rank_distribution may not exist yet

        return lines

    async def _get_health_details(self) -> dict:
        """Return health details for the /health endpoint."""
        return {
            "database": "connected" if self._app.db else "disconnected",
            "channels_configured": len(self._app.config.channels),
            "active_sessions": sum(
                self._app.presence_tracker.get_connected_count(ch.channel)
                for ch in self._app.config.channels
            ),
        }
