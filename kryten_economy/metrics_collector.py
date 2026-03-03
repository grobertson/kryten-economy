"""Shared in-process metrics collector.

A lightweight singleton-style object that any engine can import and
increment without needing a reference to the top-level ``EconomyApp``.

All counters are plain ``int`` attributes — thread-safe under the GIL and
trivially serialisable for NATS KV persistence.
"""

from __future__ import annotations


class MetricsCollector:
    """Accumulates operational counters for Prometheus export.

    Instantiated once in ``EconomyApp`` and passed (or wired) into every
    engine that produces countable events.
    """

    __slots__ = (
        # ── Event / Command ─────────────────────────────────
        "events_processed",
        "commands_processed",
        # ── Economy flow ────────────────────────────────────
        "z_earned_total",
        "z_spent_total",
        # ── User actions ────────────────────────────────────
        "tips_total",
        "tips_z_total",
        "queues_total",
        "vanity_purchases_total",
        "fortunes_total",
        "shoutouts_total",
        # ── Gambling ────────────────────────────────────────
        "spins_total",
        "flips_total",
        "challenges_total",
        "heists_total",
        "gambling_z_wagered_total",
        "gambling_z_won_total",
        # ── Progression ─────────────────────────────────────
        "achievements_awarded_total",
        "rank_promotions_total",
        # ── Competitions & Bounties ─────────────────────────
        "competition_awards_total",
        "bounties_created_total",
        "bounties_claimed_total",
        # ── Rain ────────────────────────────────────────────
        "rain_drops_total",
        "rain_z_distributed_total",
    )

    def __init__(self) -> None:
        for attr in self.__slots__:
            setattr(self, attr, 0)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def record_tip(self, amount: int) -> None:
        """Record a successful tip."""
        self.tips_total += 1
        self.tips_z_total += amount
        self.z_spent_total += amount

    def record_queue(self, cost: int) -> None:
        """Record a successful media queue."""
        self.queues_total += 1
        self.z_spent_total += cost

    def record_vanity_purchase(self, cost: int) -> None:
        """Record a successful vanity/shop purchase."""
        self.vanity_purchases_total += 1
        self.z_spent_total += cost

    def record_shoutout(self, cost: int) -> None:
        """Record a shoutout purchase."""
        self.shoutouts_total += 1
        self.z_spent_total += cost

    def record_fortune(self, cost: int) -> None:
        """Record a fortune purchase."""
        self.fortunes_total += 1
        self.z_spent_total += cost

    def record_gamble(self, game: str, wager: int, payout: int) -> None:
        """Record a gambling outcome (spin, flip, challenge, heist).

        *game*: one of ``"spin"``, ``"flip"``, ``"challenge"``, ``"heist"``
        *wager*: amount wagered (debited)
        *payout*: amount paid out (0 on loss, >0 on win/push)
        """
        if game == "spin":
            self.spins_total += 1
        elif game == "flip":
            self.flips_total += 1
        elif game == "challenge":
            self.challenges_total += 1
        elif game == "heist":
            self.heists_total += 1
        self.gambling_z_wagered_total += wager
        self.gambling_z_won_total += payout

    def record_achievement(self) -> None:
        self.achievements_awarded_total += 1

    def record_rank_promotion(self) -> None:
        self.rank_promotions_total += 1

    def record_competition_award(self) -> None:
        self.competition_awards_total += 1

    def record_bounty_created(self, cost: int) -> None:
        self.bounties_created_total += 1
        self.z_spent_total += cost

    def record_bounty_claimed(self) -> None:
        self.bounties_claimed_total += 1

    def record_rain(self, amount: int, user_count: int) -> None:
        """Record a rain event (amount per user × user count)."""
        self.rain_drops_total += 1
        self.rain_z_distributed_total += amount * user_count

    # ------------------------------------------------------------------
    # Serialisation (for NATS KV persistence)
    # ------------------------------------------------------------------

    _PERSISTED_FIELDS: tuple[str, ...] = tuple(
        s for s in __slots__  # type: ignore[arg-type]
    )

    def to_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in self._PERSISTED_FIELDS}

    def restore(self, data: dict[str, int]) -> None:
        for f in self._PERSISTED_FIELDS:
            if f in data:
                setattr(self, f, int(data[f]))
