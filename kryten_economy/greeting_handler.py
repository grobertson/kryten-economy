"""Custom greeting handler — posts greetings on genuine user arrivals.

Sprint 9: Replaces any inline greeting logic with a dedicated handler that
routes through the EventAnnouncer for deduplication and batching. Multiple
simultaneous joins are combined to reduce chat spam.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig
    from .database import EconomyDatabase
    from .event_announcer import EventAnnouncer
    from .presence_tracker import PresenceTracker


class GreetingHandler:
    """Posts custom greetings on genuine user arrivals."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        presence_tracker: PresenceTracker,
        announcer: EventAnnouncer,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._db = database
        self._presence = presence_tracker
        self._announcer = announcer
        self._logger = logger

        # Pending greetings: [(channel, username, greeting_text)]
        self._pending: list[tuple[str, str, str]] = []
        self._batch_task: asyncio.Task | None = None
        self._batch_delay = 3.0  # seconds to wait before flushing

    async def on_user_join(self, channel: str, username: str) -> None:
        """Called on adduser AFTER presence_tracker confirms genuine arrival.

        Applies the *greeting-specific* absence threshold
        (``greeting_absence_minutes``, default 30) which is longer than the
        join debounce window.
        """
        if not self._config.announcements.custom_greeting:
            return

        # Longer absence threshold for greetings
        threshold = self._config.presence.greeting_absence_minutes
        if not self._presence.was_absent_longer_than(username, channel, threshold):
            return

        greeting = await self._db.get_custom_greeting(username, channel)
        if not greeting:
            return

        self._pending.append((channel, username, greeting))

        # (Re)start batch timer
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()
        self._batch_task = asyncio.create_task(self._flush_greetings())

    async def _flush_greetings(self) -> None:
        """Wait briefly then post all pending greetings."""
        await asyncio.sleep(self._batch_delay)

        if not self._pending:
            return

        # Group by channel
        by_channel: dict[str, list[tuple[str, str]]] = {}
        for channel, username, greeting in self._pending:
            by_channel.setdefault(channel, []).append((username, greeting))
        self._pending.clear()

        for channel, greetings in by_channel.items():
            if len(greetings) == 1:
                username, greeting = greetings[0]
                template = getattr(
                    self._config.announcements.templates, "greeting", "👋 {greeting}"
                )
                msg = template.format(greeting=greeting, user=username)
                await self._announcer.announce_raw(channel, msg)
            else:
                # Combine multiple to reduce spam
                msgs = [f"👋 {g}" for _, g in greetings]
                combined = " | ".join(msgs)
                await self._announcer.announce_raw(channel, combined)

    def update_config(self, new_config: EconomyConfig) -> None:
        """Hot-swap the config reference."""
        self._config = new_config
