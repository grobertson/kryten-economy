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

    from .blackjack_engine import BlackjackEngine
    from .config import EconomyConfig
    from .database import EconomyDatabase
    from .gambling_engine import GamblingEngine
    from .multiplier_engine import MultiplierEngine
    from .presence_tracker import PresenceTracker
    from .race_engine import RaceEngine
    from .spectacle_manager import SpectacleManager
    from .trivia_engine import TriviaEngine


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
        race_engine: RaceEngine | None = None,
        trivia_engine: TriviaEngine | None = None,
        blackjack_engine: BlackjackEngine | None = None,
        spectacle_manager: SpectacleManager | None = None,
    ) -> None:
        self._config = config
        self._db = database
        self._presence_tracker = presence_tracker
        self._client = client
        self._gambling_engine = gambling_engine
        self._multiplier_engine = multiplier_engine
        self._race_engine = race_engine
        self._trivia_engine = trivia_engine
        self._blackjack_engine = blackjack_engine
        self._spectacle_manager = spectacle_manager
        self._logger = logger or logging.getLogger("economy.scheduler")
        self._metrics = None  # Wired by EconomyApp after construction
        self._tasks: list[asyncio.Task] = []
        # Fire-and-forget announcement tasks (paced chat output) kept off the
        # per-channel loops so one channel's pacing can't delay others.
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        """Run a coroutine as a tracked fire-and-forget task."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_task_done)

    def _on_bg_task_done(self, task: asyncio.Task) -> None:
        """Discard a finished background task and surface any exception."""
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.error(
                "Background task failed", exc_info=exc,
            )

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

        # Race tick loop
        if self._race_engine and self._config.gambling.race.enabled:
            self._tasks.append(asyncio.create_task(self._race_tick_loop()))
            self._logger.info("Race tick task started")

        # Trivia deadline loop
        if self._trivia_engine and self._config.gambling.trivia.enabled:
            self._tasks.append(asyncio.create_task(self._trivia_check_loop()))
            self._logger.info("Trivia check task started")

        # Blackjack timeout loop
        if self._blackjack_engine and self._config.gambling.blackjack.enabled:
            self._tasks.append(asyncio.create_task(self._blackjack_timeout_loop()))
            self._logger.info("Blackjack timeout task started")

    async def stop(self) -> None:
        """Cancel all tasks."""
        for task in self._tasks:
            task.cancel()
        for task in self._bg_tasks:
            task.cancel()
        await asyncio.gather(
            *self._tasks, *self._bg_tasks, return_exceptions=True,
        )
        self._tasks.clear()
        self._bg_tasks.clear()

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
    #  Race Tick
    # ══════════════════════════════════════════════════════════

    async def _race_tick_loop(self) -> None:
        """Advance active races and handle betting windows."""
        while True:
            tick_interval = self._config.gambling.race.tick_interval_seconds
            await asyncio.sleep(tick_interval)
            try:
                now = datetime.now(timezone.utc)
                for ch_config in self._config.channels:
                    channel = ch_config.channel
                    race = self._race_engine.get_active_race(channel)
                    if not race:
                        continue

                    from .race_engine import RacePhase

                    # Kick off LLM commentary prep once, during the betting
                    # window, so a themed story is ready before the race-start
                    # line. Runs as a background task (a no-op for static mode)
                    # to keep the per-channel loop responsive.
                    if race.phase == RacePhase.BETTING and not race.commentary_prepared:
                        race.commentary_prepared = True
                        self._spawn(self._race_engine.prepare_commentary(channel))

                    # Betting window expired → transition to racing
                    if race.phase == RacePhase.BETTING and now > race.betting_closes_at:
                        started = self._race_engine.close_betting(channel)
                        if not started:
                            # No bets — race cancelled
                            if self._spectacle_manager:
                                self._spectacle_manager.release(channel)
                            await self._announce_chat(
                                channel,
                                "🏁 Race cancelled — no bets placed.",
                            )
                            continue
                        await self._announce_chat(
                            channel,
                            self._race_engine.get_race_start_line(channel),
                        )
                        # Let the first tick land on the next loop pass (natural
                        # pause without blocking other channels with a sleep).
                        continue

                    # Racing phase — advance simulation
                    if race.phase == RacePhase.RACING:
                        progress_lines, events, finished = self._race_engine.tick(channel)

                        # Announce events
                        for event in events:
                            if event.message:
                                await self._announce_chat(channel, event.message)

                        # Send progress display
                        if progress_lines:
                            await self._announce_chat(
                                channel, "\n".join(progress_lines),
                            )

                        # Finished! Mark synchronously so subsequent ticks skip
                        # this race, then offload paced resolution/announcements.
                        if finished:
                            race.phase = RacePhase.FINISHED
                            self._spawn(self._finish_race(channel))
            except Exception:
                self._logger.exception("Race tick failed")

    async def _finish_race(self, channel: str) -> None:
        """Resolve a finished race and deliver paced announcements off the loop."""
        try:
            await asyncio.sleep(1)  # brief dramatic pause
            result = await self._race_engine.resolve_race(channel)
            if self._spectacle_manager:
                self._spectacle_manager.release(channel)
            if not result:
                return
            lines, _bets, per_user_pm = result
            for line in lines:
                await self._announce_chat(channel, line)
                await asyncio.sleep(1.5)
            # PM each bettor
            for username, pm_text in per_user_pm.items():
                await self._send_pm(channel, username, pm_text)
        except Exception:
            self._logger.exception("Race finish handling failed")

    # ══════════════════════════════════════════════════════════
    #  Trivia Check
    # ══════════════════════════════════════════════════════════

    async def _trivia_check_loop(self) -> None:
        """Resolve trivia rounds when answer deadline expires."""
        while True:
            await asyncio.sleep(5)
            try:
                now = datetime.now(timezone.utc)
                for ch_config in self._config.channels:
                    channel = ch_config.channel
                    trivia = self._trivia_engine.get_active_trivia(channel)
                    if not trivia or trivia.resolved:
                        continue
                    if now > trivia.answer_deadline:
                        result = await self._trivia_engine.resolve_trivia(channel)
                        if self._spectacle_manager:
                            self._spectacle_manager.release(channel)
                        if result:
                            # Offload paced announcements so a chatty channel
                            # can't delay resolving others past their deadline.
                            self._spawn(
                                self._announce_trivia_result(channel, result),
                            )
            except Exception:
                self._logger.exception("Trivia check failed")

    async def _announce_trivia_result(
        self, channel: str, result: tuple[list[str], dict[str, str]],
    ) -> None:
        """Deliver trivia result announcements + PMs off the deadline loop."""
        try:
            lines, per_user_pm = result
            for line in lines:
                await self._announce_chat(channel, line)
                await asyncio.sleep(1)
            for username, pm_text in per_user_pm.items():
                await self._send_pm(channel, username, pm_text)
        except Exception:
            self._logger.exception("Trivia announcement failed")

    # ══════════════════════════════════════════════════════════
    #  Blackjack Timeout
    # ══════════════════════════════════════════════════════════

    async def _blackjack_timeout_loop(self) -> None:
        """Check for timed-out blackjack sessions."""
        while True:
            await asyncio.sleep(15)
            try:
                for ch_config in self._config.channels:
                    channel = ch_config.channel
                    results = await self._blackjack_engine.check_timeouts(channel)
                    for username, msg in results:
                        await self._send_pm(channel, username, msg)
            except Exception:
                self._logger.exception("Blackjack timeout check failed")

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
