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


#: Fallback TTL when the config does not supply ``spectacle_max_duration_seconds``.
_DEFAULT_MAX_DURATION_SECONDS = 1800


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

        A lock older than ``spectacle_max_duration_seconds`` is treated as
        abandoned and reclaimed, so a crashed or wedged game loop cannot block
        a channel indefinitely.
        """
        self._reap_stale(channel)

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
            "Spectacle acquired: %s in %s",
            game_type,
            channel,
        )
        return True

    @property
    def max_duration_seconds(self) -> int:
        """Hard ceiling on how long one spectacle game may hold the lock.

        Falls back to the default if the config does not carry the field
        (partial configs, or a stubbed config object in tests), so the reaper
        can never itself become the reason a channel stays locked.
        """
        try:
            value = self._config.gambling.spectacle_max_duration_seconds
        except AttributeError:
            return _DEFAULT_MAX_DURATION_SECONDS
        if not isinstance(value, int) or isinstance(value, bool):
            return _DEFAULT_MAX_DURATION_SECONDS
        return value

    def _reap_stale(self, channel: str | None = None) -> list[tuple[str, str]]:
        """Drop locks older than ``max_duration_seconds``.

        Returns the ``(channel, game_type)`` pairs that were reaped, for
        logging and tests. Called automatically by :meth:`try_acquire`, so a
        stale lock clears on the next game attempt for that channel.
        """
        max_seconds = self.max_duration_seconds
        if max_seconds <= 0:
            return []  # 0 disables the reaper entirely

        now = datetime.now(timezone.utc)
        channels = [channel] if channel is not None else list(self._active)
        reaped: list[tuple[str, str]] = []
        for ch in channels:
            entry = self._active.get(ch)
            if entry is None:
                continue
            age = (now - entry.started_at).total_seconds()
            if age > max_seconds:
                self._active.pop(ch, None)
                # A reclaimed lock does NOT arm the cooldown: the game never
                # finished normally, so throttling the next attempt would just
                # extend the outage.
                reaped.append((ch, entry.game_type))
                self._logger.warning(
                    "Spectacle lock expired after %.0fs: %s in %s (reclaimed)",
                    age,
                    entry.game_type,
                    ch,
                )
        return reaped

    def force_release(self, channel: str) -> bool:
        """Unconditionally clear a channel's lock.

        Escape hatch for operators (and for a stuck channel) when a lock is
        known to be orphaned. Unlike the TTL reaper this also arms the normal
        cooldown, because it represents an operator-acknowledged game end.

        Returns True if a lock was actually cleared.
        """
        return self.release(channel) is not None

    def release(self, channel: str) -> _ActiveGame | None:
        """Mark the current spectacle game as finished.

        Returns the released entry, or None if the channel held no lock.
        """
        game = self._active.pop(channel, None)
        if game:
            self._last_ended[channel] = datetime.now(timezone.utc)
            self._logger.info(
                "Spectacle released: %s in %s (ran %.1fs)",
                game.game_type,
                channel,
                (datetime.now(timezone.utc) - game.started_at).total_seconds(),
            )
        return game

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
        self._reap_stale(channel)
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
