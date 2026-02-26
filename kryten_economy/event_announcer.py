"""Centralized event announcer — templated, deduplicated, rate-limited public chat.

Sprint 9: All public chat announcements route through this module instead of
calling client.send_chat() directly. Features:
- Template rendering from config
- Deduplication (suppress identical messages within a time window)
- Rate limiting (max messages/minute to chat)
- Batch delay (coalesce rapid-fire announcements)
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig


class EventAnnouncer:
    """Centralized announcement engine for public chat messages."""

    def __init__(
        self,
        config: EconomyConfig,
        client: object,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._client = client
        self._logger = logger

        # Dedup ring buffer: (message_hash, timestamp)
        self._recent: deque[tuple[int, float]] = deque(maxlen=100)
        # Outbound queue: (channel, message)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._flush_task: asyncio.Task | None = None

        # Tunables
        self._max_per_minute = 10
        self._batch_delay_seconds = 2.0
        self._dedup_window_seconds = 30.0

    # ── Lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """Start the announcement flush loop."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop the flush loop and drain remaining messages."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

    # ── Public API ───────────────────────────────────────────

    async def announce(
        self,
        channel: str,
        template_key: str,
        variables: dict[str, Any],
        fallback: str | None = None,
    ) -> None:
        """Queue a templated public announcement.

        Args:
            channel: Target channel.
            template_key: Key in ``config.announcements.templates``.
            variables: Variables for ``.format()``.
            fallback: Fallback message if template is missing.
        """
        # Check boolean gate on AnnouncementsConfig
        gate_attr = template_key
        if hasattr(self._config.announcements, gate_attr):
            if not getattr(self._config.announcements, gate_attr):
                return  # This type is disabled

        # Render template
        template = getattr(self._config.announcements.templates, template_key, None)
        if template is None:
            template = fallback or ""
        if not template:
            return

        try:
            message = template.format(**variables)
        except (KeyError, IndexError) as exc:
            self._logger.warning("Template render failed for '%s': %s", template_key, exc)
            return

        # Dedup + queue
        if self._is_duplicate(channel, message):
            self._logger.debug("Deduped announcement: %s", message[:60])
            return
        await self._queue.put((channel, message))

    async def announce_raw(self, channel: str, message: str) -> None:
        """Queue a raw message (no template, still subject to dedup/batching)."""
        if self._is_duplicate(channel, message):
            return
        await self._queue.put((channel, message))

    def update_config(self, new_config: EconomyConfig) -> None:
        """Hot-swap the config reference."""
        self._config = new_config

    # ── Internal ─────────────────────────────────────────────

    def _is_duplicate(self, channel: str, message: str) -> bool:
        """Return True if this exact message was sent recently."""
        msg_hash = hash((channel, message))
        now = datetime.now(timezone.utc).timestamp()
        if any(h == msg_hash and now - t < self._dedup_window_seconds for h, t in self._recent):
            return True
        self._recent.append((msg_hash, now))
        return False

    async def _flush_loop(self) -> None:
        """Drain announcement queue with rate limiting."""
        sent_this_minute = 0
        minute_start = datetime.now(timezone.utc).timestamp()

        while True:
            try:
                channel, message = await asyncio.wait_for(
                    self._queue.get(), timeout=self._batch_delay_seconds,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            now = datetime.now(timezone.utc).timestamp()

            # Reset minute counter
            if now - minute_start >= 60:
                sent_this_minute = 0
                minute_start = now

            # Rate limit
            if sent_this_minute >= self._max_per_minute:
                self._logger.warning("Announcement rate limit hit, dropping: %s", message[:60])
                continue

            # Brief batch delay
            await asyncio.sleep(self._batch_delay_seconds)

            try:
                await self._client.send_chat(channel, message)
                sent_this_minute += 1
            except Exception as exc:
                self._logger.error("Announcement send failed: %s", exc)
