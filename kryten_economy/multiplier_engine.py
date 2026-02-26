"""Multiplier engine — calculates combined active multiplier at any moment.

Sprint 7: Competitive Events, Multipliers & Bounties.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .config import EconomyConfig
    from .presence_tracker import PresenceTracker


class ActiveMultiplier(NamedTuple):
    source: str  # e.g. "off_peak", "population", "holiday:Christmas", "scheduled:...", "adhoc:..."
    multiplier: float
    hidden: bool


class MultiplierEngine:
    """Calculates the combined active multiplier at any given moment."""

    def __init__(
        self,
        config: EconomyConfig,
        presence_tracker: PresenceTracker,
        logger: logging.Logger,
    ) -> None:
        self._config = config.multipliers
        self._presence = presence_tracker
        self._logger = logger
        # Ad-hoc event state (set by admin commands)
        self._adhoc_event: dict | None = None  # {name, multiplier, end_time}
        # Scheduled event state (set by ScheduledEventManager)
        self._scheduled_events: dict[str, dict] = {}  # channel → {name, multiplier, end_time}

    def update_config(self, new_config) -> None:
        """Hot-swap the config reference."""
        self._config = new_config.multipliers

    def get_active_multipliers(self, channel: str) -> list[ActiveMultiplier]:
        """Return all currently active multipliers for the channel."""
        now = datetime.now(timezone.utc)
        active: list[ActiveMultiplier] = []

        # Off-peak
        if self._config.off_peak.enabled:
            # Convert weekday: Python weekday() → Mon=0..Sun=6
            # Config uses 0=Sun, so convert:  (py_weekday + 1) % 7
            if (now.weekday() + 1) % 7 in self._config.off_peak.days:
                if now.hour in self._config.off_peak.hours:
                    active.append(ActiveMultiplier(
                        source="off_peak",
                        multiplier=self._config.off_peak.multiplier,
                        hidden=False,
                    ))

        # High population
        if self._config.high_population.enabled:
            user_count = len(self._presence.get_connected_users(channel))
            if user_count >= self._config.high_population.min_users:
                active.append(ActiveMultiplier(
                    source="population",
                    multiplier=self._config.high_population.multiplier,
                    hidden=self._config.high_population.hidden,
                ))

        # Holidays
        if self._config.holidays.enabled:
            today_mmdd = now.strftime("%m-%d")
            for holiday in self._config.holidays.dates:
                if holiday.date == today_mmdd:
                    active.append(ActiveMultiplier(
                        source=f"holiday:{holiday.name}",
                        multiplier=holiday.multiplier,
                        hidden=False,
                    ))

        # Scheduled events
        sched = self._get_scheduled_multiplier(channel)
        if sched:
            active.append(sched)

        # Ad-hoc event
        if self._adhoc_event:
            if now < self._adhoc_event["end_time"]:
                active.append(ActiveMultiplier(
                    source=f"adhoc:{self._adhoc_event['name']}",
                    multiplier=self._adhoc_event["multiplier"],
                    hidden=False,
                ))
            else:
                # Auto-expire
                self._adhoc_event = None

        return active

    def get_combined_multiplier(
        self, channel: str,
    ) -> tuple[float, list[ActiveMultiplier]]:
        """Return the combined multiplier and the list of active sources.

        Multipliers are MULTIPLICATIVE: 2.0 × 1.5 = 3.0× total.
        """
        active = self.get_active_multipliers(channel)
        combined = 1.0
        for m in active:
            combined *= m.multiplier
        return combined, active

    # ── Scheduled event registration ─────────────────────────

    def set_scheduled_event(
        self, channel: str, name: str, multiplier: float, end_time: datetime,
    ) -> None:
        """Register an active scheduled event."""
        self._scheduled_events[channel] = {
            "name": name,
            "multiplier": multiplier,
            "end_time": end_time,
        }

    def clear_scheduled_event(self, channel: str) -> None:
        """Deregister the active scheduled event."""
        self._scheduled_events.pop(channel, None)

    def _get_scheduled_multiplier(self, channel: str) -> ActiveMultiplier | None:
        """Check for an active scheduled event."""
        ev = self._scheduled_events.get(channel)
        if ev and datetime.now(timezone.utc) < ev["end_time"]:
            return ActiveMultiplier(
                source=f"scheduled:{ev['name']}",
                multiplier=ev["multiplier"],
                hidden=False,
            )
        elif ev:
            del self._scheduled_events[channel]
        return None

    # ── Ad-hoc event management ──────────────────────────────

    def start_adhoc_event(
        self, name: str, multiplier: float, duration_minutes: int,
    ) -> None:
        """Start an admin-triggered ad-hoc multiplier event."""
        self._adhoc_event = {
            "name": name,
            "multiplier": multiplier,
            "end_time": datetime.now(timezone.utc) + timedelta(minutes=duration_minutes),
        }

    def stop_adhoc_event(self) -> bool:
        """Stop the current ad-hoc event. Returns ``True`` if there was one to stop."""
        if self._adhoc_event:
            self._adhoc_event = None
            return True
        return False
