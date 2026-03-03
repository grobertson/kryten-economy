"""Prometheus metrics server for kryten-economy.

Subclasses BaseMetricsServer from kryten-py to expose
economy-specific metrics and health details.  Every metric family
includes ``# HELP`` and ``# TYPE`` declarations per the Prometheus
exposition format.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from kryten import BaseMetricsServer

if TYPE_CHECKING:
    from .main import EconomyApp

# ── helpers ──────────────────────────────────────────────────────────
_METRIC_PREFIX = "economy"


def _counter(name: str, helptext: str, value: int | float) -> list[str]:
    """Emit HELP, TYPE and a single counter sample."""
    fqn = f"{_METRIC_PREFIX}_{name}"
    return [
        f"# HELP {fqn} {helptext}",
        f"# TYPE {fqn} counter",
        f"{fqn} {value}",
    ]


def _gauge(name: str, helptext: str, value: int | float, fmt: str = "") -> list[str]:
    """Emit HELP, TYPE and a single gauge sample (no labels)."""
    fqn = f"{_METRIC_PREFIX}_{name}"
    v = f"{value:{fmt}}" if fmt else str(value)
    return [
        f"# HELP {fqn} {helptext}",
        f"# TYPE {fqn} gauge",
        f"{fqn} {v}",
    ]


def _gauge_header(name: str, helptext: str) -> list[str]:
    """Emit HELP + TYPE for a gauge that will have labelled samples."""
    fqn = f"{_METRIC_PREFIX}_{name}"
    return [
        f"# HELP {fqn} {helptext}",
        f"# TYPE {fqn} gauge",
    ]


def _gauge_sample(name: str, labels: str, value: int | float, fmt: str = "") -> str:
    """One labelled gauge sample (caller must emit header first)."""
    fqn = f"{_METRIC_PREFIX}_{name}"
    v = f"{value:{fmt}}" if fmt else str(value)
    return f"{fqn}{{{labels}}} {v}"


# ────────────────────────────────────────────────────────────────────


class EconomyMetricsServer(BaseMetricsServer):
    """Economy-specific Prometheus metrics endpoint."""

    def __init__(self, app: "EconomyApp", port: int = 28286) -> None:
        super().__init__(
            service_name="economy",
            port=port,
            client=app.client,
            logger=app.logger,
        )
        self._app = app

    # ------------------------------------------------------------------
    # Custom metrics
    # ------------------------------------------------------------------

    async def _collect_custom_metrics(self) -> list[str]:  # noqa: C901
        """Collect economy-specific Prometheus metrics.

        Structure: counters first (global), then per-channel gauges.
        Each metric family is emitted as HELP → TYPE → all samples
        before the next family begins, matching the Prometheus exposition
        format exactly.
        """
        lines: list[str] = []
        m = self._app.metrics  # MetricsCollector shortcut

        # ── Section 1: Lifetime counters (global, from MetricsCollector) ──
        #
        # Economy flow
        lines += _counter("z_earned_total",
                           "Cumulative Ƶ earned across all channels (lifetime, persisted).",
                           m.z_earned_total)
        lines += _counter("z_spent_total",
                           "Cumulative Ƶ spent across all channels (lifetime, persisted).",
                           m.z_spent_total)

        # Events
        lines += _counter("events_processed_total",
                           "Total NATS events processed since service start.",
                           m.events_processed)
        lines += _counter("commands_processed_total",
                           "Total PM commands executed by users (lifetime, persisted).",
                           m.commands_processed)

        # User actions
        lines += _counter("tips_total",
                           "Total tip transactions (lifetime, persisted).",
                           m.tips_total)
        lines += _counter("tips_z_total",
                           "Total Ƶ transferred via tips (lifetime, persisted).",
                           m.tips_z_total)
        lines += _counter("queues_total",
                           "Total media queue purchases (lifetime, persisted).",
                           m.queues_total)
        lines += _counter("vanity_purchases_total",
                           "Total vanity / shop item purchases (lifetime, persisted).",
                           m.vanity_purchases_total)
        lines += _counter("fortunes_total",
                           "Total fortune cookie purchases (lifetime, persisted).",
                           m.fortunes_total)
        lines += _counter("shoutouts_total",
                           "Total shoutout purchases (lifetime, persisted).",
                           m.shoutouts_total)
        lines += _counter("rain_drops_total",
                           "Total rain events triggered (lifetime, persisted).",
                           m.rain_drops_total)
        lines += _counter("rain_z_distributed_total",
                           "Cumulative Ƶ distributed via rain (lifetime, persisted).",
                           m.rain_z_distributed_total)

        # Gambling
        lines += _counter("gambling_spins_total",
                           "Total slot-machine spins (lifetime, persisted).",
                           m.spins_total)
        lines += _counter("gambling_flips_total",
                           "Total coin flips (lifetime, persisted).",
                           m.flips_total)
        lines += _counter("gambling_challenges_total",
                           "Total PvP challenge rounds (lifetime, persisted).",
                           m.challenges_total)
        lines += _counter("gambling_heists_total",
                           "Total heist participant-rounds resolved (lifetime, persisted).",
                           m.heists_total)
        lines += _counter("gambling_z_wagered_total",
                           "Cumulative Ƶ wagered across all games (lifetime, persisted).",
                           m.gambling_z_wagered_total)
        lines += _counter("gambling_z_won_total",
                           "Cumulative Ƶ paid out from gambling wins (lifetime, persisted).",
                           m.gambling_z_won_total)

        # Progression
        lines += _counter("achievements_awarded_total",
                           "Total achievements awarded to users (lifetime, persisted).",
                           m.achievements_awarded_total)
        lines += _counter("rank_promotions_total",
                           "Total rank promotions (lifetime, persisted).",
                           m.rank_promotions_total)
        lines += _counter("competition_awards_total",
                           "Total daily-competition prizes awarded (lifetime, persisted).",
                           m.competition_awards_total)
        lines += _counter("bounties_created_total",
                           "Total bounties created (lifetime, persisted).",
                           m.bounties_created_total)
        lines += _counter("bounties_claimed_total",
                           "Total bounties claimed (lifetime, persisted).",
                           m.bounties_claimed_total)

        # ── Section 2: Operational gauges (current snapshot, global) ──────
        if self._app.pm_handler:
            lines += _gauge("pm_queue_depth",
                             "Outbound PMs currently queued for delivery (snapshot).",
                             self._app.pm_handler._pm_queue.qsize())
            lines += _gauge("pending_confirms",
                             "Users with a pending confirmation prompt (snapshot).",
                             len(self._app.pm_handler._pending_confirm))

        # ── Section 3: Per-channel gauges ─────────────────────────────────
        #
        # Strategy: gather all channel data in one async pass, then emit
        # each metric family (HELP → TYPE → all-channel samples) so the
        # output is correctly structured for Prometheus scrapers.

        today = datetime.date.today().isoformat()

        # Gather data for every configured channel.
        ch_data: list[dict] = []
        for ch_cfg in self._app.config.channels:
            channel = ch_cfg.channel
            tag = f'channel="{channel}"'
            d: dict = {"channel": channel, "tag": tag}

            d["present"] = self._app.presence_tracker.get_connected_count(channel)
            d["circ"]    = await self._app.db.get_total_circulation(channel)
            d["count"]   = await self._app.db.get_account_count(channel)
            d["median"]  = await self._app.db.get_median_balance(channel)

            # Participation: connected users who *have* an account.
            # Capped at 100 — account count regularly exceeds connected users
            # since most accounts belong to users not currently online.
            d["participation"] = min(
                (d["count"] / d["present"] * 100) if d["present"] > 0 else 0.0,
                100.0,
            )

            if self._app.multiplier_engine:
                combined, _ = self._app.multiplier_engine.get_combined_multiplier(channel)
                d["multiplier"] = combined
            else:
                d["multiplier"] = 1.0

            try:
                d["rank_dist"] = await self._app.db.get_rank_distribution(channel)
            except Exception:
                d["rank_dist"] = {}

            try:
                d["daily"] = await self._app.db.get_daily_totals(channel, today)
            except Exception:
                d["daily"] = None

            try:
                d["dau"] = await self._app.db.get_active_economy_users_today(channel, today)
            except Exception:
                d["dau"] = None

            try:
                d["gamble_summary"] = await self._app.db.get_gambling_summary_global(channel)
            except Exception:
                d["gamble_summary"] = None

            try:
                open_b = await self._app.db.get_open_bounties(channel)
                d["open_bounties"] = len(open_b)
            except Exception:
                d["open_bounties"] = None

            ch_data.append(d)

        # Emit each metric family: HELP → TYPE → all channels → blank line.

        # -- active_users
        lines += _gauge_header("active_users",
                                "Connected users in the channel right now (snapshot).")
        for d in ch_data:
            lines.append(_gauge_sample("active_users", d["tag"], d["present"]))

        # -- total_accounts
        lines += _gauge_header("total_accounts",
                                "Total economy accounts ever registered.")
        for d in ch_data:
            lines.append(_gauge_sample("total_accounts", d["tag"], d["count"]))

        # -- total_circulation
        lines += _gauge_header("total_circulation",
                                "Total Ƶ in circulation (sum of all balances, snapshot).")
        for d in ch_data:
            lines.append(_gauge_sample("total_circulation", d["tag"], d["circ"]))

        # -- median_balance
        lines += _gauge_header("median_balance",
                                "Median Ƶ balance across all accounts (snapshot).")
        for d in ch_data:
            lines.append(_gauge_sample("median_balance", d["tag"], d["median"]))

        # -- participation_rate
        lines += _gauge_header("participation_rate",
                                "Percentage of connected users with economy accounts, capped at 100 (snapshot).")
        for d in ch_data:
            lines.append(_gauge_sample("participation_rate", d["tag"], d["participation"], ".2f"))

        # -- active_multiplier
        lines += _gauge_header("active_multiplier",
                                "Combined active earning multiplier (snapshot).")
        for d in ch_data:
            lines.append(_gauge_sample("active_multiplier", d["tag"], d["multiplier"], ".2f"))

        # -- rank_distribution
        lines += _gauge_header("rank_distribution",
                                "Number of users at each rank (snapshot).")
        for d in ch_data:
            for rank_name, rcount in d["rank_dist"].items():
                lines.append(
                    _gauge_sample("rank_distribution",
                                  f'{d["tag"]},rank="{rank_name}"', rcount)
                )

        # -- daily z flow
        lines += _gauge_header("daily_z_earned",
                                "Ƶ earned today across all users (resets at midnight, from DB).")
        for d in ch_data:
            if d["daily"] is not None:
                lines.append(_gauge_sample("daily_z_earned", d["tag"], d["daily"]["z_earned"]))

        lines += _gauge_header("daily_z_spent",
                                "Ƶ spent today across all users (resets at midnight, from DB).")
        for d in ch_data:
            if d["daily"] is not None:
                lines.append(_gauge_sample("daily_z_spent", d["tag"], d["daily"]["z_spent"]))

        lines += _gauge_header("daily_z_gambled_in",
                                "Ƶ wagered today (resets at midnight, from DB).")
        for d in ch_data:
            if d["daily"] is not None:
                lines.append(_gauge_sample("daily_z_gambled_in", d["tag"], d["daily"]["z_gambled_in"]))

        lines += _gauge_header("daily_z_gambled_out",
                                "Ƶ paid out from gambling today (resets at midnight, from DB).")
        for d in ch_data:
            if d["daily"] is not None:
                lines.append(_gauge_sample("daily_z_gambled_out", d["tag"], d["daily"]["z_gambled_out"]))

        lines += _gauge_header("daily_active_economy_users",
                                "Users who earned or spent today (resets at midnight, from DB).")
        for d in ch_data:
            if d["dau"] is not None:
                lines.append(_gauge_sample("daily_active_economy_users", d["tag"], d["dau"]))

        # -- gambling lifetime (from gambling_stats DB table)
        lines += _gauge_header("gambling_lifetime_wagered",
                                "All-time Ƶ wagered per channel (from gambling_stats DB).")
        for d in ch_data:
            if d["gamble_summary"] is not None:
                lines.append(_gauge_sample("gambling_lifetime_wagered", d["tag"],
                                           d["gamble_summary"]["total_in"]))

        lines += _gauge_header("gambling_lifetime_won",
                                "All-time Ƶ paid out from gambling per channel (from gambling_stats DB).")
        for d in ch_data:
            if d["gamble_summary"] is not None:
                lines.append(_gauge_sample("gambling_lifetime_won", d["tag"],
                                           d["gamble_summary"]["total_out"]))

        lines += _gauge_header("gambling_active_gamblers",
                                "Users who have gambled at least once (from gambling_stats DB).")
        for d in ch_data:
            if d["gamble_summary"] is not None:
                lines.append(_gauge_sample("gambling_active_gamblers", d["tag"],
                                           d["gamble_summary"]["active_gamblers"]))

        lines += _gauge_header("gambling_total_games",
                                "Lifetime gambling rounds played (from gambling_stats DB).")
        for d in ch_data:
            if d["gamble_summary"] is not None:
                lines.append(_gauge_sample("gambling_total_games", d["tag"],
                                           d["gamble_summary"]["total_games"]))

        # -- open bounties
        lines += _gauge_header("open_bounties",
                                "Unclaimed bounties currently active (snapshot).")
        for d in ch_data:
            if d["open_bounties"] is not None:
                lines.append(_gauge_sample("open_bounties", d["tag"], d["open_bounties"]))

        return lines

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

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
