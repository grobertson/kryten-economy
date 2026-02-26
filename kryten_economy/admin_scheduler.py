"""Admin scheduler — economy snapshots and scheduled digests.

Manages three periodic tasks:
1. Economy snapshots (every 6 hours)
2. Weekly admin digest (Monday at configured hour)
3. User daily digest (daily at configured hour)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kryten import KrytenClient

    from .config import EconomyConfig
    from .database import EconomyDatabase
    from .presence_tracker import PresenceTracker
    from .rank_engine import RankEngine


class AdminScheduler:
    """Runs snapshot capture, admin digests, and user digests."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        client: KrytenClient,
        presence_tracker: PresenceTracker,
        rank_engine: RankEngine | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._db = database
        self._client = client
        self._presence = presence_tracker
        self._rank_engine = rank_engine
        self._logger = logger or logging.getLogger("economy.admin_scheduler")
        self._tasks: list[asyncio.Task] = []

    # ──────────────────────────────────────────────────────────
    #  Lifecycle
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all scheduled tasks."""
        self._tasks.append(asyncio.create_task(self._schedule_snapshots()))
        self._tasks.append(asyncio.create_task(self._schedule_admin_digest()))
        if self._config.digest.user_digest.enabled:
            self._tasks.append(asyncio.create_task(self._schedule_user_digest()))
        self._logger.info("Admin scheduler started (%d tasks)", len(self._tasks))

    async def stop(self) -> None:
        """Cancel all running tasks."""
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._logger.info("Admin scheduler stopped")

    def _active_channels(self) -> list[str]:
        return [ch.channel for ch in self._config.channels]

    # ──────────────────────────────────────────────────────────
    #  Economy Snapshots (every 6 hours)
    # ──────────────────────────────────────────────────────────

    async def _schedule_snapshots(self) -> None:
        interval = 6 * 3600  # 6 hours
        while True:
            await asyncio.sleep(interval)
            for channel in self._active_channels():
                try:
                    await self._capture_snapshot(channel)
                except Exception as e:
                    self._logger.error("Snapshot error for %s: %s", channel, e)

    async def _capture_snapshot(self, channel: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        totals = await self._db.get_daily_totals(channel, today)
        present_count = len(self._presence.get_present_users(channel))

        data = {
            "total_accounts": await self._db.get_all_accounts_count(channel),
            "total_z_circulation": await self._db.get_total_circulation(channel),
            "active_economy_users_today": await self._db.get_active_economy_users_today(channel, today),
            "z_earned_today": totals.get("z_earned", 0),
            "z_spent_today": totals.get("z_spent", 0),
            "z_gambled_net_today": totals.get("z_gambled_out", 0) - totals.get("z_gambled_in", 0),
            "median_balance": await self._db.get_median_balance(channel),
            "participation_rate": await self._db.get_participation_rate(channel, present_count),
        }

        await self._db.write_snapshot(channel, data)
        self._logger.debug("Snapshot captured for %s", channel)

    # ──────────────────────────────────────────────────────────
    #  Weekly Admin Digest (Monday at configured hour)
    # ──────────────────────────────────────────────────────────

    async def _schedule_admin_digest(self) -> None:
        send_hour = self._config.digest.admin_digest.send_hour_utc
        while True:
            now = datetime.now(timezone.utc)
            days_until_monday = (7 - now.weekday()) % 7
            target = now.replace(
                hour=send_hour, minute=0, second=0, microsecond=0,
            ) + timedelta(days=days_until_monday)
            if target <= now:
                target += timedelta(weeks=1)

            delay = (target - now).total_seconds()
            await asyncio.sleep(delay)

            for channel in self._active_channels():
                try:
                    await self._send_admin_digest(channel)
                except Exception as e:
                    self._logger.error("Admin digest error for %s: %s", channel, e)

    async def _send_admin_digest(self, channel: str) -> None:
        now = datetime.now(timezone.utc)
        end = now.strftime("%Y-%m-%d")
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d")

        weekly = await self._db.get_weekly_totals(channel, start, end)
        top_earners = await self._db.get_top_earners_range(channel, start, end, limit=5)
        top_spenders = await self._db.get_top_spenders_range(channel, start, end, limit=5)
        gambling = await self._db.get_gambling_summary_global(channel)
        circulation = await self._db.get_total_circulation(channel)
        snapshots = await self._db.get_snapshot_history(channel, days=7)

        if snapshots and len(snapshots) >= 2:
            circ_change = (
                snapshots[-1].get("total_z_circulation", 0)
                - snapshots[0].get("total_z_circulation", 0)
            )
        else:
            circ_change = 0

        lines = [
            f"📊 Weekly Economy Digest ({start} → {end})",
            "━" * 40,
            f"Total Z minted: {weekly.get('z_earned', 0):,}",
            f"Total Z spent: {weekly.get('z_spent', 0):,}",
            f"Total Z gambled: {weekly.get('z_gambled_in', 0):,}",
            f"Net circulation change: {circ_change:+,} Z",
            f"Current circulation: {circulation:,} Z",
            "",
            "🏆 Top 5 Earners:",
        ]
        for i, e in enumerate(top_earners, 1):
            lines.append(f"  {i}. {e['username']} — {e['earned']:,} Z")

        lines.append("\n💸 Top 5 Spenders:")
        for i, s in enumerate(top_spenders, 1):
            lines.append(f"  {i}. {s['username']} — {s['spent']:,} Z")

        if gambling and gambling.get("total_games", 0) > 0:
            total_in = gambling.get("total_in", 0)
            total_out = gambling.get("total_out", 0)
            edge = ((total_in - total_out) / total_in * 100) if total_in > 0 else 0
            lines.append(
                f"\n🎰 Gambling: {gambling['total_games']:,} games, actual edge: {edge:.1f}%"
            )

        digest_msg = "\n".join(lines)

        admins = self._presence.get_admin_users(channel, self._config.admin.owner_level)
        for admin in admins:
            await self._client.send_pm(channel, admin, digest_msg)

        self._logger.info("Admin digest sent to %d admins in %s", len(admins), channel)

    # ──────────────────────────────────────────────────────────
    #  User Daily Digest (daily at configured hour)
    # ──────────────────────────────────────────────────────────

    async def _schedule_user_digest(self) -> None:
        send_hour = self._config.digest.user_digest.send_hour_utc
        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=send_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)

            delay = (target - now).total_seconds()
            await asyncio.sleep(delay)

            for channel in self._active_channels():
                try:
                    await self._send_user_digests(channel)
                except Exception as e:
                    self._logger.error("User digest error for %s: %s", channel, e)

    async def _send_user_digests(self, channel: str) -> None:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        activities = await self._db.get_daily_activity_all(channel, yesterday)
        template = self._config.digest.user_digest.message

        sent = 0
        for activity in activities:
            username = activity["username"]
            account = await self._db.get_account(username, channel)
            if not account:
                continue

            if self._rank_engine:
                tier_index, tier = self._rank_engine.get_rank_for_lifetime(
                    account.get("lifetime_earned", 0)
                )
                next_tier = self._rank_engine.get_next_tier(tier_index)
            else:
                tier = type("T", (), {"name": "Unknown"})()
                tier_index = 0
                next_tier = None

            if next_tier:
                remaining = next_tier.min_lifetime_earned - account.get("lifetime_earned", 0)
                daily_avg = activity.get("z_earned", 1) or 1
                days_away = max(1, remaining // daily_avg)
                next_goal = f"{next_tier.name} ({remaining:,} Z away, ~{days_away} days)"
            else:
                next_goal = "Maximum rank achieved! 🏆"
                days_away = 0

            msg = template.format(
                earned=activity.get("z_earned", 0),
                spent=activity.get("z_spent", 0),
                balance=account["balance"],
                rank=tier.name,
                streak=account.get("current_streak", 0),
                currency=self._config.currency.symbol,
                next_goal_description=next_goal,
                days_away=days_away,
            )

            await self._client.send_pm(channel, username, msg)
            sent += 1

        self._logger.info("User digests sent to %d users in %s", sent, channel)
