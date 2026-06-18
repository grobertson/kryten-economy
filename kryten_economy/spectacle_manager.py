"""Spectacle Game Manager — mutual exclusion for channel-wide games.

Ensures only one "spectacle" game (heist, race, trivia) runs per channel
at a time, with a shared post-game cooldown to prevent chat flooding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig


@dataclass
class _ActiveGame:
    """Tracks a currently running spectacle game in a channel."""

    game_type: str
    started_at: datetime


class SpectacleManager:
    """Central gatekeeper for spectacle (multi-player public) games.

    Only one spectacle game may be active per channel at a time.
    After a game ends, a shared cooldown prevents another from starting
    immediately — this keeps chat output from being dominated by games.
    """

    def __init__(self, config: EconomyConfig, logger: logging.Logger) -> None:
        self._config = config
        self._logger = logger

        # channel → active game
        self._active: dict[str, _ActiveGame] = {}

        # channel → datetime when the last spectacle ended
        self._last_ended: dict[str, datetime] = {}

    # ── Properties ────────────────────────────────────────────

    @property
    def shared_cooldown_seconds(self) -> int:
        return self._config.gambling.spectacle_cooldown_seconds

    # ── Public API ────────────────────────────────────────────

    def try_acquire(self, channel: str, game_type: str) -> bool:
        """Attempt to start a spectacle game.

        Returns True if the game was successfully acquired.
        Returns False if another game is active or cooldown is in effect.
        """
        if channel in self._active:
            return False

        cooldown = self.cooldown_remaining(channel)
        if cooldown > 0:
            return False

        self._active[channel] = _ActiveGame(
            game_type=game_type,
            started_at=datetime.now(timezone.utc),
        )
        self._logger.info(
            "Spectacle acquired: %s in %s", game_type, channel,
        )
        return True

    def release(self, channel: str) -> None:
        """Mark the current spectacle game as finished."""
        game = self._active.pop(channel, None)
        if game:
            self._last_ended[channel] = datetime.now(timezone.utc)
            self._logger.info(
                "Spectacle released: %s in %s (ran %.1fs)",
                game.game_type,
                channel,
                (datetime.now(timezone.utc) - game.started_at).total_seconds(),
            )

    def active_game(self, channel: str) -> str | None:
        """Return the game_type of the active spectacle, or None."""
        entry = self._active.get(channel)
        return entry.game_type if entry else None

    def cooldown_remaining(self, channel: str) -> int:
        """Seconds remaining on post-game cooldown. 0 = ready."""
        ended = self._last_ended.get(channel)
        if ended is None:
            return 0
        elapsed = (datetime.now(timezone.utc) - ended).total_seconds()
        remaining = self.shared_cooldown_seconds - elapsed
        return max(0, int(remaining))

    def status_text(self, channel: str) -> str:
        """Human-readable status for the channel."""
        active = self.active_game(channel)
        if active:
            return f"A {active} is currently in progress."
        cd = self.cooldown_remaining(channel)
        if cd > 0:
            return f"Spectacle cooldown: {cd}s remaining."
        return "Ready for a new game."

    def update_config(self, new_config: EconomyConfig) -> None:
        """Hot-swap config reference."""
        self._config = new_config
