"""Sprint 9 — PmRateLimiter tests.

Tests:
- Within-limit commands allowed
- Exceeds limit → blocked
- Window resets after 60s
- Per-user isolation
- Cleanup of stale entries
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from kryten_economy.pm_handler import PmRateLimiter


class TestPmRateLimiter:
    """Tests for PmRateLimiter sliding-window enforcement."""

    def test_within_limit(self, rate_limiter: PmRateLimiter) -> None:
        """5 commands → all allowed (limit is 10)."""
        for _ in range(5):
            assert rate_limiter.check("alice") is True

    def test_exceeds_limit(self, rate_limiter: PmRateLimiter) -> None:
        """15 rapid commands → first 10 allowed, rest blocked."""
        results = [rate_limiter.check("alice") for _ in range(15)]
        assert results.count(True) == 10
        assert results.count(False) == 5
        # The first 10 must be allowed, last 5 blocked
        assert all(results[:10])
        assert not any(results[10:])

    def test_window_reset(self, rate_limiter: PmRateLimiter) -> None:
        """After 60s the limit resets — user can send again."""
        # Exhaust the limit
        for _ in range(10):
            rate_limiter.check("alice")
        assert rate_limiter.check("alice") is False

        # Simulate time passing 61s
        now = datetime.now(timezone.utc).timestamp()
        # Set all timestamps to 61 seconds ago
        rate_limiter._counters["alice"] = [now - 61] * 10

        assert rate_limiter.check("alice") is True

    def test_per_user_isolation(self, rate_limiter: PmRateLimiter) -> None:
        """User A rate-limited, User B unaffected."""
        # Exhaust Alice's limit
        for _ in range(10):
            rate_limiter.check("alice")
        assert rate_limiter.check("alice") is False

        # Bob should still be allowed
        assert rate_limiter.check("bob") is True

    def test_cleanup(self, rate_limiter: PmRateLimiter) -> None:
        """Stale entries are removed by cleanup()."""
        # Add some commands
        rate_limiter.check("alice")
        rate_limiter.check("bob")

        # Make all entries old (> 120s ago)
        now = datetime.now(timezone.utc).timestamp()
        for user in rate_limiter._counters:
            rate_limiter._counters[user] = [now - 200]

        rate_limiter.cleanup()

        # Both should be cleaned up
        assert "alice" not in rate_limiter._counters
        assert "bob" not in rate_limiter._counters

    def test_cleanup_preserves_recent(self, rate_limiter: PmRateLimiter) -> None:
        """Cleanup doesn't remove users with recent activity."""
        rate_limiter.check("alice")
        rate_limiter.check("bob")

        # Make only Bob's entries old
        now = datetime.now(timezone.utc).timestamp()
        rate_limiter._counters["bob"] = [now - 200]

        rate_limiter.cleanup()

        assert "alice" in rate_limiter._counters
        assert "bob" not in rate_limiter._counters

    def test_custom_limit(self) -> None:
        """Custom max_per_minute is respected."""
        limiter = PmRateLimiter(max_per_minute=3)
        assert limiter.check("alice") is True
        assert limiter.check("alice") is True
        assert limiter.check("alice") is True
        assert limiter.check("alice") is False
