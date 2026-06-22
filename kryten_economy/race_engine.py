"""Race betting engine — weighted simulation with live betting, traits & events.

The centerpiece spectacle game. Supports pari-mutuel (pool) and fixed-odds
modes, racer traits, random mid-race events, live betting, and static / LLM /
hybrid commentary (see ``RaceNarrator``).
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

from . import race_narratives
from .database import EconomyDatabase
from .gambling_common import validate_gamble_account
from .race_narrator import RaceNarrator
from .utils import today_str

if TYPE_CHECKING:
    from .config import EconomyConfig


# ═══════════════════════════════════════════════════════════════
#  Data types
# ═══════════════════════════════════════════════════════════════


class RacePhase(Enum):
    BETTING = "betting"
    RACING = "racing"
    FINISHED = "finished"


class RacerTrait(Enum):
    SPRINTER = "sprinter"
    STEADY = "steady"
    CLOSER = "closer"
    WILDCARD = "wildcard"
    RESILIENT = "resilient"


TRAIT_POOL: list[RacerTrait] = list(RacerTrait)


# ── Game-balance constants ───────────────────────────────────
# Trait speed modifiers keyed by trait → (early-race mult, late-race mult).
# Applied when progress is below EARLY_PHASE_PCT or above LATE_PHASE_PCT.
EARLY_PHASE_PCT = 0.3
LATE_PHASE_PCT = 0.7
TRAIT_SPEED_MODIFIERS: dict[RacerTrait, tuple[float, float]] = {
    RacerTrait.SPRINTER: (1.5, 0.85),
    RacerTrait.CLOSER: (0.75, 1.5),
}
WILDCARD_SPEED_RANGE = (0.5, 2.0)

# Random-event tuning
EVENT_TYPE_WEIGHTS = (35, 30, 15, 20)  # speed_boost, stumble, mudslide, shortcut
SPEED_BOOST_TICKS = 2
SPEED_BOOST_MULTIPLIER = 2.0
MUDSLIDE_TICKS = 1
MUDSLIDE_MULTIPLIER = 0.5
STUMBLE_FREEZE_TICKS = 1
SHORTCUT_BONUS_PCT = 0.15

# Close-finish commentary thresholds
CLOSE_FINISH_GAP_PCT = 0.05
CLOSE_FINISH_MIN_PROGRESS_PCT = 0.7


@dataclass
class RacerState:
    """State of one racer during a race."""

    color: str
    emoji: str
    speed_base: float
    win_chance: float
    trait: RacerTrait
    progress: float = 0.0
    # Temporary modifiers applied by events (ticks remaining → multiplier)
    speed_buff_ticks: int = 0
    speed_buff_multiplier: float = 1.0
    frozen_ticks: int = 0

    @property
    def odds_display(self) -> str:
        """Human-readable odds derived from win chance."""
        if self.win_chance <= 0:
            return "∞"
        return f"{1.0 / self.win_chance:.1f}x"

    @property
    def trait_label(self) -> str:
        return race_narratives.TRAIT_DESCRIPTIONS.get(self.trait.value, "")


@dataclass
class RaceBet:
    """A single bet placed by a user."""

    username: str
    color: str
    amount: int
    phase: str  # "pre" or "live"


@dataclass
class ActiveRace:
    """In-memory state for a running race."""

    race_id: str
    channel: str
    initiator: str
    phase: RacePhase
    racers: dict[str, RacerState]  # color → state
    bets: list[RaceBet]
    started_at: datetime
    betting_closes_at: datetime
    tick_count: int = 0
    leader: str | None = None  # current leader color
    commentary_prepared: bool = False  # LLM story prep kicked off (scheduler)


# ═══════════════════════════════════════════════════════════════
#  Random events
# ═══════════════════════════════════════════════════════════════


class _EventType(Enum):
    SPEED_BOOST = "speed_boost"
    STUMBLE = "stumble"
    MUDSLIDE = "mudslide"
    SHORTCUT = "shortcut"


@dataclass
class RaceEvent:
    """A random event that occurred during a tick."""

    event_type: _EventType
    target_color: str | None  # None for track-wide events
    message: str


# ═══════════════════════════════════════════════════════════════
#  Engine
# ═══════════════════════════════════════════════════════════════


class RaceEngine:
    """Manages race lifecycle: setup → betting → simulation → payout."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._db = database
        self._logger = logger
        self._symbol = config.currency.symbol

        # Active races: channel → ActiveRace
        self._active_races: dict[str, ActiveRace] = {}

        # Narrator
        self._narrator = RaceNarrator(
            config.gambling.race.commentary, logger,
        )

    def update_config(self, new_config: EconomyConfig) -> None:
        self._config = new_config
        self._symbol = new_config.currency.symbol
        self._narrator.update_config(new_config.gambling.race.commentary)

    # ── Queries ───────────────────────────────────────────────

    def get_active_race(self, channel: str) -> ActiveRace | None:
        return self._active_races.get(channel)

    # ── Commentary (LLM) ──────────────────────────────────────

    async def prepare_commentary(self, channel: str) -> None:
        """Pre-generate LLM race commentary for a channel.

        A no-op in static mode. The scheduler kicks this off (as a background
        task) during the betting window so a themed story is ready before the
        race-start line.
        """
        race = self._active_races.get(channel)
        if race is None:
            return
        await self._narrator.prepare_story(channel, race.race_id)

    def get_race_start_line(self, channel: str) -> str:
        """The 'and they're off!' line shown when betting closes.

        Uses the LLM story's start line when one was generated, otherwise a
        default static announcement.
        """
        llm_start = self._narrator.get_story_start(channel)
        return llm_start or "⏳ Betting is closed! The race is starting… 🏁"

    # ── Start race ────────────────────────────────────────────

    def start_race(self, channel: str, initiator: str) -> str:
        """Create a new race with randomised racers.

        Returns sentinel string for the PM handler:
        - ``race_started:<channel>`` on success
        - error message string on failure
        """
        cfg = self._config.gambling.race
        if not cfg.enabled:
            return "Race betting is currently disabled."

        if channel in self._active_races:
            return "A race is already in progress in this channel."

        # Pick a random odds profile
        profile = random.choice(cfg.odds_profiles)
        traits = list(TRAIT_POOL)
        random.shuffle(traits)

        racers: dict[str, RacerState] = {}
        for i, rp in enumerate(profile.racers):
            trait = traits[i % len(traits)] if cfg.traits.enabled else RacerTrait.STEADY
            racers[rp.color] = RacerState(
                color=rp.color,
                emoji=rp.emoji or "🏃",
                speed_base=rp.speed_base,
                win_chance=rp.win_chance,
                trait=trait,
            )

        now = datetime.now(timezone.utc)
        race = ActiveRace(
            race_id=uuid.uuid4().hex[:12],
            channel=channel,
            initiator=initiator,
            phase=RacePhase.BETTING,
            racers=racers,
            bets=[],
            started_at=now,
            betting_closes_at=now + timedelta(seconds=cfg.betting_window_seconds),
        )
        self._active_races[channel] = race
        self._narrator.reset_for_race(channel, race.race_id)

        self._logger.info(
            "Race %s started in %s by %s (%d racers)",
            race.race_id, channel, initiator, len(racers),
        )
        return f"race_started:{channel}"

    # ── Betting ───────────────────────────────────────────────

    async def place_bet(
        self,
        username: str,
        channel: str,
        amount: int,
        color: str,
    ) -> str:
        """Place a bet on a racer.

        Returns sentinel or error string.
        """
        cfg = self._config.gambling.race
        race = self._active_races.get(channel)
        if not race:
            return "No active race. Start one with 'race'."

        # Normalise color lookup (case-insensitive)
        color_match = None
        for c in race.racers:
            if c.lower() == color.lower():
                color_match = c
                break
        if not color_match:
            valid = ", ".join(race.racers.keys())
            return f"Invalid racer. Choose from: {valid}"

        # Determine phase
        if race.phase == RacePhase.BETTING:
            bet_phase = "pre"
        elif race.phase == RacePhase.RACING and cfg.live_betting.enabled:
            # Check if live betting is still open
            finish = cfg.finish_distance
            max_progress = max(r.progress for r in race.racers.values())
            if max_progress >= finish * cfg.live_betting.cutoff_pct:
                return "Live betting has closed — racers are too close to the finish."
            bet_phase = "live"
        else:
            return "Betting is closed for this race."

        # Validate wager
        if amount < cfg.min_bet:
            return f"Minimum bet: {cfg.min_bet} {self._symbol}."
        if amount > cfg.max_bet:
            return f"Maximum bet: {cfg.max_bet} {self._symbol}."

        # Check if user already bet this race
        for b in race.bets:
            if b.username.lower() == username.lower():
                return "You've already placed a bet in this race."

        # Account validation (shared across all gambling engines)
        error = await validate_gamble_account(
            self._db, self._config.gambling, self._symbol,
            username, channel, amount,
        )
        if error:
            return error

        # Debit
        success = await self._db.atomic_debit(username, channel, amount)
        if not success:
            return "Insufficient funds."

        race.bets.append(RaceBet(
            username=username,
            color=color_match,
            amount=amount,
            phase=bet_phase,
        ))

        return f"race_bet:{channel}:{username}:{color_match}:{amount}:{bet_phase}"

    # ── Transition to racing ──────────────────────────────────

    def close_betting(self, channel: str) -> bool:
        """Transition from BETTING → RACING. Returns False if no bets."""
        race = self._active_races.get(channel)
        if not race or race.phase != RacePhase.BETTING:
            return False

        if not race.bets:
            # No bets placed — cancel
            self._active_races.pop(channel, None)
            self._narrator.consume_story(channel)
            return False

        race.phase = RacePhase.RACING
        return True

    # ── Simulation tick ───────────────────────────────────────

    def tick(self, channel: str) -> tuple[list[str], list[RaceEvent], bool]:
        """Advance all racers by one tick.

        Returns (progress_lines, events, race_finished).
        """
        cfg = self._config.gambling.race
        race = self._active_races.get(channel)
        if not race or race.phase != RacePhase.RACING:
            return [], [], False

        race.tick_count += 1
        events: list[RaceEvent] = []
        finish = cfg.finish_distance
        commentary_lines: list[str] = []

        # ── Random events ────────────────────────────────────
        if cfg.random_events.enabled and random.random() < cfg.random_events.chance_per_tick:
            event = self._generate_event(race)
            if event:
                events.append(event)

        # ── Move racers ──────────────────────────────────────
        old_leader = race.leader
        for racer in race.racers.values():
            # Frozen check (from stumble)
            if racer.frozen_ticks > 0:
                racer.frozen_ticks -= 1
                continue

            # Base movement
            speed = racer.speed_base

            # Trait modifiers
            progress_pct = racer.progress / finish if finish > 0 else 0
            speed = self._apply_trait(racer, speed, progress_pct)

            # Event buff
            if racer.speed_buff_ticks > 0:
                speed *= racer.speed_buff_multiplier
                racer.speed_buff_ticks -= 1

            # Random component
            movement = random.random() * speed
            racer.progress += movement

        # ── Determine new leader ─────────────────────────────
        sorted_racers = sorted(
            race.racers.values(),
            key=lambda r: r.progress,
            reverse=True,
        )
        new_leader = sorted_racers[0].color
        race.leader = new_leader

        # Lead change commentary
        if old_leader and new_leader != old_leader:
            lr = race.racers[new_leader]
            line = self._narrator.get_lead_change_line(channel, lr.color, lr.emoji)
            if line:
                commentary_lines.append(line)

        # Close finish commentary
        if len(sorted_racers) >= 2:
            gap = sorted_racers[0].progress - sorted_racers[1].progress
            if (
                gap < finish * CLOSE_FINISH_GAP_PCT
                and sorted_racers[0].progress > finish * CLOSE_FINISH_MIN_PROGRESS_PCT
            ):
                line = self._narrator.get_close_finish_line(channel)
                if line:
                    commentary_lines.append(line)

        # ── Build progress bar display ───────────────────────
        progress_lines = self._build_progress_display(race)

        # ── Check finish ─────────────────────────────────────
        finished = any(r.progress >= finish for r in race.racers.values())

        return progress_lines + commentary_lines, events, finished

    # ── Resolve race ──────────────────────────────────────────

    async def resolve_race(
        self, channel: str,
    ) -> tuple[list[str], list[RaceBet], dict[str, str]] | None:
        """Resolve a finished race: determine winner, calculate payouts.

        Returns (public_lines, all_bets, {username: pm_text}) or None.
        """
        cfg = self._config.gambling.race
        race = self._active_races.pop(channel, None)
        if not race:
            return None

        race.phase = RacePhase.FINISHED

        # Determine winner (most progress)
        winner = max(race.racers.values(), key=lambda r: r.progress)
        winner_color = winner.color

        # ── Calculate payouts ────────────────────────────────
        total_pool = sum(b.amount for b in race.bets)
        rake = int(total_pool * cfg.house_rake_pct)
        distributable = total_pool - rake

        winning_bets = [b for b in race.bets if b.color == winner_color]
        losing_bets = [b for b in race.bets if b.color != winner_color]
        total_on_winner = sum(b.amount for b in winning_bets)

        per_user_pm: dict[str, str] = {}
        today = today_str()
        odds = 1.0 / winner.win_chance if winner.win_chance > 0 else 1.0

        # Compute each winner's payout exactly once (single source of truth).
        winner_payouts: list[tuple[RaceBet, int, int]] = []  # (bet, payout, net)
        if total_on_winner > 0:
            for bet in winning_bets:
                if cfg.odds_mode == "pool":
                    # Pari-mutuel: split distributable pool proportionally
                    payout = int(distributable * (bet.amount / total_on_winner))
                else:
                    # Fixed odds: payout = bet × (1/win_chance) less rake
                    payout = int(bet.amount * odds * (1.0 - cfg.house_rake_pct))
                winner_payouts.append((bet, payout, payout - bet.amount))

        # Credit winners + record stats
        for bet, payout, net in winner_payouts:
            reason = (
                f"Race win: {winner_color}"
                if cfg.odds_mode == "pool"
                else f"Race win: {winner_color} ({odds:.1f}x)"
            )
            await self._db.credit(
                bet.username, channel, payout,
                tx_type="gamble_win",
                trigger_id="gambling.race",
                reason=reason,
            )
            await self._db.update_gambling_stats(
                bet.username, channel, "race", net=net,
                biggest_win=max(0, net),
            )
            await self._db.increment_lifetime_gambled(bet.username, channel, bet.amount, payout)
            await self._db.increment_daily_gambled(bet.username, channel, today, bet.amount, payout)
            await self._db.save_race_bet(
                race.race_id, bet.username, channel,
                bet.color, bet.amount, payout, bet.phase,
            )
            suffix = "" if cfg.odds_mode == "pool" else f" at {odds:.1f}x"
            per_user_pm[bet.username] = (
                f"✅ {winner.emoji} {winner_color} wins! "
                f"You won {payout:,} {self._symbol} (+{net:,} net{suffix})."
            )

        # Record losses
        for bet in losing_bets:
            await self._db.update_gambling_stats(
                bet.username, channel, "race", net=-bet.amount,
                biggest_loss=bet.amount,
            )
            await self._db.increment_lifetime_gambled(bet.username, channel, bet.amount, 0)
            await self._db.increment_daily_gambled(bet.username, channel, today, bet.amount, 0)
            await self._db.save_race_bet(
                race.race_id, bet.username, channel,
                bet.color, bet.amount, 0, bet.phase,
            )
            per_user_pm[bet.username] = (
                f"❌ {winner.emoji} {winner_color} wins — your bet on "
                f"{bet.color} lost {bet.amount:,} {self._symbol}."
            )

        # Persist race result
        await self._db.save_race_result(
            race.race_id, channel, winner_color,
            total_pool, len(race.bets),
        )

        # ── Build public announcement lines ──────────────────
        # Brief: a headline finish line + one combined summary line, to keep the
        # channel terse (the full play-by-play lives on the web race view).
        finish_line = self._narrator.get_finish_line(channel, winner.color, winner.emoji)
        lines = [finish_line]

        summary_bits: list[str] = []
        if winner_payouts:
            top_winners = sorted(winner_payouts, key=lambda wp: wp[1], reverse=True)[:3]
            winner_strs = [f"@{bet.username} (+{net:,})" for bet, _payout, net in top_winners]
            summary_bits.append(f"Winners: {', '.join(winner_strs)}")
        elif race.bets:
            summary_bits.append("💸 Nobody backed the winner — house takes all")
        summary_bits.append(
            f"Pool {total_pool:,} {self._symbol} · {len(race.bets)} bettor(s)"
        )
        lines.append(" | ".join(summary_bits))

        # Clear any cached LLM commentary for this channel.
        self._narrator.consume_story(channel)

        return lines, race.bets, per_user_pm

    # ── Display helpers ───────────────────────────────────────

    def get_betting_display(self, channel: str) -> list[str]:
        """Brief betting-phase announcement for public chat (one message).

        Deliberately terse to save chat real-estate: a single headline listing
        every racer with odds inline, plus the bet instruction. The full
        animated play-by-play lives on the web race view, not in chat.
        """
        race = self._active_races.get(channel)
        if not race:
            return []

        cfg = self._config.gambling.race
        remaining = max(0, int(
            (race.betting_closes_at - datetime.now(timezone.utc)).total_seconds()
        ))

        racers = " · ".join(
            f"{racer.emoji} {color} {racer.odds_display}"
            for color, racer in race.racers.items()
        )
        return [
            f"🏁 Race OPEN! Betting closes in {remaining}s — {racers}",
            f"Bet in chat: !race <amount> <color> (min {cfg.min_bet} {self._symbol})",
        ]

    def get_live_odds(self, channel: str) -> list[str]:
        """Show current odds based on race positions (for live betting)."""
        race = self._active_races.get(channel)
        if not race or race.phase != RacePhase.RACING:
            return ["No race in progress."]

        cfg = self._config.gambling.race
        finish = cfg.finish_distance
        lines = ["📊 Live odds:"]
        sorted_racers = sorted(race.racers.values(), key=lambda r: r.progress, reverse=True)
        for racer in sorted_racers:
            pct = min(100, int(racer.progress / finish * 100))
            lines.append(f"  {racer.emoji} {racer.color} — {pct}% ({racer.odds_display})")
        return lines

    def _build_progress_display(self, race: ActiveRace) -> list[str]:
        """Build progress bar strings for all racers."""
        cfg = self._config.gambling.race
        finish = cfg.finish_distance
        bar_width = 14
        lines = []
        for color, racer in race.racers.items():
            filled = min(bar_width, int(racer.progress / finish * bar_width))
            empty = bar_width - filled
            bar = "█" * filled + "░" * empty
            trait_short = racer.trait.value[:3].title() if cfg.traits.enabled else ""
            lines.append(f"{racer.emoji} {color:<7}|{bar}| {trait_short}")
        return lines

    # ── Trait system ──────────────────────────────────────────

    @staticmethod
    def _apply_trait(racer: RacerState, speed: float, progress_pct: float) -> float:
        """Apply trait-based speed modifiers."""
        modifiers = TRAIT_SPEED_MODIFIERS.get(racer.trait)
        if modifiers is not None:
            early_mult, late_mult = modifiers
            if progress_pct < EARLY_PHASE_PCT:
                speed *= early_mult
            elif progress_pct > LATE_PHASE_PCT:
                speed *= late_mult
        elif racer.trait == RacerTrait.WILDCARD:
            speed *= random.uniform(*WILDCARD_SPEED_RANGE)
        # STEADY has no modifier (naturally low-variance); RESILIENT immunity
        # is handled during event generation.
        return speed

    # ── Random events ─────────────────────────────────────────

    def _generate_event(self, race: ActiveRace) -> RaceEvent | None:
        """Generate a random mid-race event."""
        cfg = self._config.gambling.race
        event_type = random.choices(
            [_EventType.SPEED_BOOST, _EventType.STUMBLE, _EventType.MUDSLIDE, _EventType.SHORTCUT],
            weights=list(EVENT_TYPE_WEIGHTS),
            k=1,
        )[0]

        racers_list = list(race.racers.values())

        if event_type == _EventType.SPEED_BOOST:
            target = random.choice(racers_list)
            target.speed_buff_ticks = SPEED_BOOST_TICKS
            target.speed_buff_multiplier = SPEED_BOOST_MULTIPLIER
            msg = self._narrator.get_event_line(race.channel, "speed_boost", target.color, target.emoji)
            return RaceEvent(event_type, target.color, msg or f"⚡ {target.emoji} {target.color} boosts!")

        elif event_type == _EventType.STUMBLE:
            # Can't stumble Resilient racers
            eligible = [r for r in racers_list if r.trait != RacerTrait.RESILIENT]
            if not eligible:
                return None
            target = random.choice(eligible)
            target.frozen_ticks = STUMBLE_FREEZE_TICKS
            msg = self._narrator.get_event_line(race.channel, "stumble", target.color, target.emoji)
            return RaceEvent(event_type, target.color, msg or f"💥 {target.emoji} {target.color} stumbles!")

        elif event_type == _EventType.MUDSLIDE:
            # Affects all (except Resilient)
            for r in racers_list:
                if r.trait != RacerTrait.RESILIENT:
                    r.speed_buff_ticks = MUDSLIDE_TICKS
                    r.speed_buff_multiplier = MUDSLIDE_MULTIPLIER
            msg = self._narrator.get_event_line(race.channel, "mudslide", "", "")
            return RaceEvent(event_type, None, msg or "🌊 Mudslide! Everyone slows down!")

        elif event_type == _EventType.SHORTCUT:
            # Give boost to trailing racer
            sorted_racers = sorted(racers_list, key=lambda r: r.progress)
            target = sorted_racers[0]  # most behind
            bonus = cfg.finish_distance * SHORTCUT_BONUS_PCT
            target.progress += bonus
            msg = self._narrator.get_event_line(race.channel, "shortcut", target.color, target.emoji)
            return RaceEvent(event_type, target.color, msg or f"🎯 {target.emoji} {target.color} finds a shortcut!")

        return None
