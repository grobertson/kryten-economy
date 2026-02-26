"""Scheduled event manager — cron-based scheduled events.

Sprint 7: Competitive Events, Multipliers & Bounties.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from croniter import croniter

if TYPE_CHECKING:
    from .config import EconomyConfig, ScheduledEventConfig
    from .database import EconomyDatabase
    from .multiplier_engine import MultiplierEngine
    from .presence_tracker import PresenceTracker


class ScheduledEventManager:
    """Manages cron-based scheduled events: start/end, presence bonuses, announcements."""

    def __init__(
        self,
        config: EconomyConfig,
        multiplier_engine: MultiplierEngine,
        presence_tracker: PresenceTracker,
        database: EconomyDatabase,
        client: object,
        logger: logging.Logger,
    ) -> None:
        self._events = config.multipliers.scheduled_events
        self._multiplier = multiplier_engine
        self._presence = presence_tracker
        self._db = database
        self._client = client
        self._logger = logger
        self._active: dict[str, dict] = {}  # key → {event_name, end_time}
        self._check_task: asyncio.Task | None = None
        self._channels: list[str] = []

    async def start(self, channels: list[str]) -> None:
        """Start the event monitoring loop."""
        self._channels = channels
        self._check_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the event monitoring loop."""
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        """Check every 60 seconds for events that should start or end."""
        while True:
            try:
                now = datetime.now(timezone.utc)
                for event_cfg in self._events:
                    for channel in self._channels:
                        await self._check_event(event_cfg, channel, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.error("Scheduled event monitor error: %s", e)
            await asyncio.sleep(60)

    async def _check_event(
        self,
        event_cfg: ScheduledEventConfig,
        channel: str,
        now: datetime,
    ) -> None:
        """Check if a specific event should start or has ended."""
        key = f"{channel}:{event_cfg.name}"

        # Is this event currently active?
        if key in self._active:
            active = self._active[key]
            if now >= active["end_time"]:
                await self._end_event(event_cfg, channel, key)
            return

        # Should this event start now?
        cron = croniter(event_cfg.cron, now - timedelta(minutes=1))
        next_fire = cron.get_next(datetime)
        if abs((next_fire - now).total_seconds()) < 90:
            await self._start_event(event_cfg, channel, key, next_fire)

    async def _start_event(
        self,
        event_cfg: ScheduledEventConfig,
        channel: str,
        key: str,
        fire_time: datetime,
    ) -> None:
        """Activate a scheduled event."""
        end_time = fire_time + timedelta(hours=event_cfg.duration_hours)
        self._active[key] = {"event_name": event_cfg.name, "end_time": end_time}

        # Register multiplier
        self._multiplier.set_scheduled_event(
            channel, event_cfg.name, event_cfg.multiplier, end_time,
        )

        self._logger.info(
            "Scheduled event started: %s in %s", event_cfg.name, channel,
        )

        # Announce start
        if event_cfg.announce:
            await self._client.send_chat(
                channel,
                f"🎉 **{event_cfg.name}** has started! "
                f"{event_cfg.multiplier}× earning for {event_cfg.duration_hours:.0f} hours!",
            )

        # Presence bonus
        if event_cfg.presence_bonus > 0:
            await self._distribute_presence_bonus(event_cfg, channel)

    async def _end_event(
        self,
        event_cfg: ScheduledEventConfig,
        channel: str,
        key: str,
    ) -> None:
        """Deactivate a scheduled event."""
        del self._active[key]
        self._multiplier.clear_scheduled_event(channel)

        self._logger.info(
            "Scheduled event ended: %s in %s", event_cfg.name, channel,
        )

        if event_cfg.announce:
            await self._client.send_chat(
                channel,
                f"⏰ **{event_cfg.name}** has ended. Thanks for participating!",
            )

    async def _distribute_presence_bonus(
        self,
        event_cfg: ScheduledEventConfig,
        channel: str,
    ) -> None:
        """Split presence bonus among all connected users at event start."""
        present_users = self._presence.get_connected_users(channel)
        if not present_users:
            return

        per_user = max(1, event_cfg.presence_bonus // len(present_users))

        for username in present_users:
            await self._db.credit(
                username,
                channel,
                per_user,
                tx_type="event_bonus",
                trigger_id=f"event.{event_cfg.name}.presence",
                reason=f"Present at {event_cfg.name} start",
            )
            await self._client.send_pm(
                channel,
                username,
                f"🎁 You were here when **{event_cfg.name}** started! +{per_user:,} Z",
            )

        self._logger.info(
            "Presence bonus for %s: %d Z each to %d users",
            event_cfg.name,
            per_user,
            len(present_users),
        )
