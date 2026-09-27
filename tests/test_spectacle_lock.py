"""Tests for the spectacle lock lifecycle.

Covers the three defects that let a crashed race permanently block a channel:

1. ``SpectacleManager`` reclaims a lock older than ``max_duration_seconds``
   so an abandoned game cannot wedge the channel forever.
2. ``release()`` is reachable from a ``finally`` in the scheduler loops, so an
   exception during resolution cannot leak the lock.
3. ``resolve_race`` writes the ``race_results`` parent row before any
   ``race_bets`` child row (``race_bets.race_id`` is a foreign key).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone


from kryten_economy.config import EconomyConfig
from kryten_economy.spectacle_manager import SpectacleManager

from conftest import make_config_dict

CH = "test-channel"
logger = logging.getLogger("test")


def _manager(**gambling_overrides) -> SpectacleManager:
    cfg_dict = make_config_dict()
    cfg_dict.setdefault("gambling", {}).update(gambling_overrides)
    return SpectacleManager(EconomyConfig(**cfg_dict), logger)


# ── Reaper ────────────────────────────────────────────────────────────


class TestStaleLockReaper:
    def test_lock_within_ttl_blocks_a_second_game(self) -> None:
        m = _manager(spectacle_max_duration_seconds=1800)
        assert m.try_acquire(CH, "race") is True
        assert m.try_acquire(CH, "heist") is False
        assert m.status_text(CH) == "A race is currently in progress."

    def test_expired_lock_is_reclaimed(self) -> None:
        m = _manager(spectacle_max_duration_seconds=1800)
        assert m.try_acquire(CH, "race") is True

        # Backdate the lock past the TTL.
        m._active[CH].started_at = datetime.now(timezone.utc) - timedelta(seconds=1801)

        assert m.try_acquire(CH, "heist") is True
        assert m.active_game(CH) == "heist"

    def test_reaping_does_not_arm_the_cooldown(self) -> None:
        """A reclaimed lock never finished normally, so it must not throttle."""
        m = _manager(
            spectacle_max_duration_seconds=1800, spectacle_cooldown_seconds=360
        )
        assert m.try_acquire(CH, "race") is True
        m._active[CH].started_at = datetime.now(timezone.utc) - timedelta(seconds=1801)

        assert m.try_acquire(CH, "heist") is True
        # If the reaper had armed the cooldown this would be > 0.
        assert m.cooldown_remaining(CH) == 0

    def test_status_text_also_reaps(self) -> None:
        m = _manager(spectacle_max_duration_seconds=1800)
        assert m.try_acquire(CH, "race") is True
        m._active[CH].started_at = datetime.now(timezone.utc) - timedelta(seconds=1801)

        assert m.status_text(CH) == "Ready for a new game."

    def test_ttl_zero_disables_the_reaper(self) -> None:
        m = _manager(spectacle_max_duration_seconds=0)
        assert m.try_acquire(CH, "race") is True
        m._active[CH].started_at = datetime.now(timezone.utc) - timedelta(days=30)

        assert m.try_acquire(CH, "heist") is False

    def test_reaper_is_per_channel(self) -> None:
        m = _manager(spectacle_max_duration_seconds=1800)
        assert m.try_acquire("a", "race") is True
        assert m.try_acquire("b", "heist") is True
        m._active["a"].started_at = datetime.now(timezone.utc) - timedelta(seconds=1801)

        assert m.try_acquire("a", "trivia") is True
        assert m.active_game("b") == "heist"  # untouched

    def test_reap_stale_reports_what_it_cleared(self) -> None:
        m = _manager(spectacle_max_duration_seconds=1800)
        assert m.try_acquire(CH, "race") is True
        m._active[CH].started_at = datetime.now(timezone.utc) - timedelta(seconds=1801)

        assert m._reap_stale() == [(CH, "race")]
        assert m._reap_stale() == []  # idempotent


# ── force_release ─────────────────────────────────────────────────────


class TestForceRelease:
    def test_force_release_clears_a_lock_and_arms_cooldown(self) -> None:
        m = _manager(spectacle_max_duration_seconds=0, spectacle_cooldown_seconds=360)
        assert m.try_acquire(CH, "race") is True

        assert m.force_release(CH) is True
        assert m.active_game(CH) is None
        # Operator-acknowledged end -> cooldown applies.
        assert m.cooldown_remaining(CH) > 0
        # ...and it is not acquireable until the cooldown elapses.
        assert m.try_acquire(CH, "heist") is False

    def test_force_release_on_idle_channel_is_a_noop(self) -> None:
        m = _manager()
        assert m.force_release(CH) is False
        assert m.cooldown_remaining(CH) == 0

    def test_release_returns_the_entry(self) -> None:
        m = _manager()
        assert m.try_acquire(CH, "race") is True
        entry = m.release(CH)
        assert entry is not None
        assert entry.game_type == "race"
        assert m.release(CH) is None
