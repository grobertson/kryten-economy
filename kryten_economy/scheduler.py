"""Scheduler module — periodic and scheduled tasks.

Sprint 2: Rain drops, balance maintenance (interest/decay).
Later sprints add: daily digest, competition eval, daily resets, etc.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kryten import KrytenClient

    from .config import EconomyConfig
    from .database import EconomyDatabase
    from .gambling_engine import GamblingEngine
    from .multiplier_engine import MultiplierEngine
    from .presence_tracker import PresenceTracker


class Scheduler:
    """Central module for all periodic and scheduled tasks."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        presence_tracker: PresenceTracker,
        client: KrytenClient,
        logger: logging.Logger | None = None,
        gambling_engine: GamblingEngine | None = None,
        multiplier_engine: MultiplierEngine | None = None,
    ) -> None:
        self._config = config
        self._db = database
        self._presence_tracker = presence_tracker
        self._client = client
        self._gambling_engine = gambling_engine
        self._multiplier_engine = multiplier_engine
        self._logger = logger or logging.getLogger("economy.scheduler")
        self._metrics = None  # Wired by EconomyApp after construction
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start all scheduled tasks."""
        if self._config.rain.enabled:
            self._tasks.append(asyncio.create_task(self._rain_loop()))
            self._logger.info("Rain drops task started (interval: ~%d min)", self._config.rain.interval_minutes)

        if self._config.balance_maintenance.mode != "none":
            self._tasks.append(asyncio.create_task(self._daily_maintenance_loop()))
            self._logger.info("Balance maintenance task started (mode: %s)", self._config.balance_maintenance.mode)

        # Sprint 4: challenge expiry + heist check
        if self._gambling_engine and self._config.gambling.enabled:
            self._tasks.append(asyncio.create_task(self._challenge_expiry_loop()))
            self._logger.info("Challenge expiry task started")
            if self._config.gambling.heist.enabled:
                self._tasks.append(asyncio.create_task(self._heist_check_loop()))
                self._logger.info("Heist check task started")

    async def stop(self) -> None:
        """Cancel all tasks."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ══════════════════════════════════════════════════════════
    #  Rain Drops
    # ══════════════════════════════════════════════════════════

    async def _rain_loop(self) -> None:
        """Periodic rain drop distribution."""
        while True:
            interval = self._config.rain.interval_minutes
            jitter = random.uniform(-0.3, 0.3) * interval
            wait_seconds = (interval + jitter) * 60
            await asyncio.sleep(max(wait_seconds, 60))  # Minimum 1 minute
            try:
                await self._execute_rain()
            except Exception:
                self._logger.exception("Rain execution failed")

    async def _execute_rain(self) -> None:
        """Distribute rain to all connected users across all channels."""
        rain_cfg = self._config.rain

        for ch_config in self._config.channels:
            channel = ch_config.channel
            users = self._presence_tracker.get_connected_users(channel)
            if not users:
                continue

            amount = random.randint(rain_cfg.min_amount, rain_cfg.max_amount)

            event_multiplier = 1.0
            if self._multiplier_engine is not None:
                for mul in self._multiplier_engine.get_active_multipliers(channel):
                    if mul.source.startswith("scheduled:"):
                        event_multiplier *= mul.multiplier

            for username in users:
                # Scheduled event multipliers apply to rain drops.
                rain_amount = max(1, int(round(amount * event_multiplier)))

                await self._db.credit(
                    username,
                    channel,
                    rain_amount,
                    tx_type="rain",
                    trigger_id="rain.ambient",
                    reason=f"Rain drop: {rain_amount}",
                )

                if rain_cfg.pm_notification:
                    msg = rain_cfg.message.format(
                        amount=rain_amount,
                        currency=self._config.currency.name,
                    )
                    await self._send_pm(channel, username, msg)

            self._logger.info(
                "Rain: base=%d, event_multiplier=%.2f, final=%d to %d users in %s",
                amount,
                event_multiplier,
                max(1, int(round(amount * event_multiplier))),
                len(users),
                channel,
            )
            if self._metrics:
                self._metrics.record_rain(amount, len(users))

    # ══════════════════════════════════════════════════════════
    #  Balance Maintenance (Interest / Decay)
    # ══════════════════════════════════════════════════════════

    async def _daily_maintenance_loop(self) -> None:
        """Runs once per day at 03:00 UTC."""
        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            try:
                await self._execute_balance_maintenance()
            except Exception:
                self._logger.exception("Balance maintenance failed")

    async def _execute_balance_maintenance(self) -> None:
        """Apply interest or decay to all accounts."""
        mode = self._config.balance_maintenance.mode

        for ch_config in self._config.channels:
            channel = ch_config.channel

            if mode == "interest":
                cfg = self._config.balance_maintenance.interest
                total = await self._db.apply_interest_batch(
                    channel, cfg.daily_rate, cfg.max_daily_interest, cfg.min_balance_to_earn
                )
                self._logger.info("Interest: %d Z total in %s", total, channel)
            elif mode == "decay":
                cfg = self._config.balance_maintenance.decay
                total = await self._db.apply_decay_batch(channel, cfg.daily_rate, cfg.exempt_below)
                self._logger.info("Decay: %d Z total in %s", total, channel)

    # ══════════════════════════════════════════════════════════
    #  Challenge Expiry
    # ══════════════════════════════════════════════════════════

    async def _challenge_expiry_loop(self) -> None:
        """Expire timed-out challenges and refund challengers."""
        while True:
            await asyncio.sleep(60)  # Check every 60 seconds
            try:
                for ch_config in self._config.channels:
                    channel = ch_config.channel
                    expired = await self._gambling_engine.cleanup_expired_challenges(channel)
                    for challenge in expired:
                        await self._send_pm(
                            channel,
                            challenge["challenger"],
                            f"⚔️ Your challenge to {challenge['target']} expired. "
                            f"{challenge['wager']} {self._config.currency.symbol} refunded.",
                        )
                        await self._send_pm(
                            channel,
                            challenge["target"],
                            f"⚔️ Challenge from {challenge['challenger']} expired.",
                        )
            except Exception:
                self._logger.exception("Challenge expiry failed")

    # ══════════════════════════════════════════════════════════
    #  Heist Check
    # ══════════════════════════════════════════════════════════

    async def _heist_check_loop(self) -> None:
        """Resolve heists when join window expires."""
        while True:
            await asyncio.sleep(10)  # Check every 10 seconds
            try:
                now = datetime.now(timezone.utc)
                for ch_config in self._config.channels:
                    channel = ch_config.channel
                    heist = self._gambling_engine.get_active_heist(channel)
                    if heist and now > heist.expires_at:
                        # Capture heist wager total before resolution
                        heist_total_wagered = sum(heist.participants.values())
                        heist_participants = list(heist.participants.keys())
                        result = await self._gambling_engine.resolve_heist(channel)
                        if result:
                            if self._metrics:
                                # Count one heist per participant
                                for _ in heist_participants:
                                    self._metrics.heists_total += 1
                                self._metrics.gambling_z_wagered_total += heist_total_wagered
                            lines, participants, per_user_pm = result
                            if self._config.gambling.heist.announce_public and lines:
                                # Send scenario line first
                                await self._announce_chat(channel, lines[0])
                                if len(lines) > 1:
                                    # Dramatic pause before revealing outcome
                                    await asyncio.sleep(6)
                                    for line in lines[1:]:
                                        await self._announce_chat(channel, line)
                                        await asyncio.sleep(2)
                            # PM each participant only their personal result (win/loss/push + amount)
                            for user in participants:
                                pm_text = per_user_pm.get(user)
                                if pm_text:
                                    await self._send_pm(channel, user, pm_text)
            except Exception:
                self._logger.exception("Heist check failed")

    # ══════════════════════════════════════════════════════════
    #  PM Sending
    # ══════════════════════════════════════════════════════════

    async def _send_pm(self, channel: str, username: str, message: str) -> None:
        """Send PM via kryten-py client."""
        try:
            await self._client.send_pm(channel, username, message)
        except Exception:
            self._logger.debug("Failed to send PM to %s", username)

    async def _announce_chat(self, channel: str, message: str) -> None:
        """Post a message in public chat via kryten-py."""
        try:
            await self._client.send_chat(channel, message)
        except Exception:
            self._logger.debug("Failed to send chat to %s", channel)
