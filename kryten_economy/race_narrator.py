"""Race narrator — static commentary for races.

Picks race commentary lines from built-in + custom pools. (LLM/hybrid
generation, as used by heists, is intentionally not wired up for races yet.)
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from . import race_narratives

if TYPE_CHECKING:
    from .config import RaceCommentaryConfig


class RaceNarrator:
    """Generate static commentary lines for race events.

    Lines are drawn randomly from the built-in pools in ``race_narratives``
    merged with any operator-supplied custom lines.
    """

    def __init__(
        self,
        config: RaceCommentaryConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cfg = config
        self._log = logger or logging.getLogger(__name__)

        # Merge built-in + custom pools
        self._start_lines = list(race_narratives.START_LINES) + list(config.custom_start_lines)
        self._finish_lines = list(race_narratives.FINISH_LINES) + list(config.custom_finish_lines)
        self._event_lines = dict(race_narratives.EVENT_LINES)
        # custom event lines get appended to the "speed_boost" bucket
        if config.custom_event_lines:
            self._event_lines.setdefault("speed_boost", ())
            self._event_lines["speed_boost"] = (
                tuple(self._event_lines["speed_boost"]) + tuple(config.custom_event_lines)
            )

        self._lead_change_lines = list(race_narratives.LEAD_CHANGE_LINES)
        self._close_finish_lines = list(race_narratives.CLOSE_FINISH_LINES)
        self._payout_lines = list(race_narratives.PAYOUT_LINES)

        self._commentary_count = 0

    @property
    def max_lines(self) -> int:
        return self._cfg.max_lines_per_race

    def reset_for_race(self) -> None:
        """Reset per-race commentary budget."""
        self._commentary_count = 0

    def update_config(self, new_config: RaceCommentaryConfig) -> None:
        self._cfg = new_config
        self._start_lines = list(race_narratives.START_LINES) + list(new_config.custom_start_lines)
        self._finish_lines = list(race_narratives.FINISH_LINES) + list(new_config.custom_finish_lines)

    def _can_emit(self) -> bool:
        """Check if we've already emitted the max commentary for this race."""
        return self._commentary_count < self._cfg.max_lines_per_race

    # ── Static pickers ────────────────────────────────────────

    def get_start_line(self) -> str:
        return random.choice(self._start_lines)

    def get_lead_change_line(self, racer: str, emoji: str) -> str | None:
        if not self._can_emit():
            return None
        self._commentary_count += 1
        line = random.choice(self._lead_change_lines)
        return line.format(racer=racer, emoji=emoji)

    def get_event_line(self, event_type: str, racer: str, emoji: str) -> str | None:
        if not self._can_emit():
            return None
        pool = self._event_lines.get(event_type, self._event_lines.get("speed_boost", ()))
        if not pool:
            return None
        self._commentary_count += 1
        line = random.choice(pool)
        return line.format(racer=racer, emoji=emoji)

    def get_close_finish_line(self) -> str | None:
        if not self._can_emit():
            return None
        self._commentary_count += 1
        return random.choice(self._close_finish_lines)

    def get_finish_line(self, racer: str, emoji: str) -> str:
        line = random.choice(self._finish_lines)
        return line.format(racer=racer, emoji=emoji)

    def get_payout_line(self, user: str, payout: str, symbol: str) -> str:
        line = random.choice(self._payout_lines)
        return line.format(user=user, payout=payout, symbol=symbol)
