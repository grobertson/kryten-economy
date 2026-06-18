"""Blackjack Lite engine — PM-only card game.

Standard blackjack rules with dealer hitting soft 17, natural blackjack
paying 3:2, and a double-down option. Each user has their own independent
session; multiple concurrent games across users are supported.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from .database import EconomyDatabase
from .gambling_common import (
    get_daily_game_count,
    increment_daily_game_count,
    validate_gamble_account,
)
from .utils import now_utc, today_str

if TYPE_CHECKING:
    from .config import EconomyConfig


# ═══════════════════════════════════════════════════════════════
#  Card representation
# ═══════════════════════════════════════════════════════════════

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")

RANK_VALUES: dict[str, int] = {
    "A": 11, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "8": 8, "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10,
}


@dataclass
class Card:
    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"

    @property
    def value(self) -> int:
        return RANK_VALUES[self.rank]

    @property
    def is_ace(self) -> bool:
        return self.rank == "A"


def _new_deck() -> list[Card]:
    """Create and shuffle a standard 52-card deck."""
    deck = [Card(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


# ═══════════════════════════════════════════════════════════════
#  Hand evaluation
# ═══════════════════════════════════════════════════════════════


@dataclass
class Hand:
    cards: list[Card] = field(default_factory=list)

    @property
    def value(self) -> int:
        """Best hand value, reducing aces from 11 to 1 as needed."""
        total = sum(c.value for c in self.cards)
        aces = sum(1 for c in self.cards if c.is_ace)
        while total > 21 and aces > 0:
            total -= 10
            aces -= 1
        return total

    @property
    def soft(self) -> bool:
        """Whether the hand contains a usable ace (counted as 11)."""
        total = sum(c.value for c in self.cards)
        aces = sum(1 for c in self.cards if c.is_ace)
        reduced = 0
        while total > 21 and aces > 0:
            total -= 10
            aces -= 1
            reduced += 1
        # Soft if at least one ace is still counted as 11
        return (sum(1 for c in self.cards if c.is_ace) - reduced) > 0 and total <= 21

    @property
    def busted(self) -> bool:
        return self.value > 21

    @property
    def is_blackjack(self) -> bool:
        return len(self.cards) == 2 and self.value == 21

    def display(self, hide_second: bool = False) -> str:
        """Display cards. If hide_second, show only the first card + [?]."""
        if hide_second and len(self.cards) >= 2:
            return f"{self.cards[0]} [?]"
        return " ".join(str(c) for c in self.cards)

    def display_with_value(self, hide_second: bool = False) -> str:
        if hide_second and len(self.cards) >= 2:
            return f"🃏 {self.cards[0]} [?]"
        return f"🃏 {self.display()} ({self.value})"


# ═══════════════════════════════════════════════════════════════
#  Game outcome
# ═══════════════════════════════════════════════════════════════


class BJOutcome(Enum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    BLACKJACK = "blackjack"


# ═══════════════════════════════════════════════════════════════
#  Active game session
# ═══════════════════════════════════════════════════════════════


@dataclass
class ActiveBlackjack:
    username: str
    channel: str
    wager: int
    deck: list[Card]
    player_hand: Hand
    dealer_hand: Hand
    doubled: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_action_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    warned: bool = False  # whether timeout warning was sent


# ═══════════════════════════════════════════════════════════════
#  Engine
# ═══════════════════════════════════════════════════════════════


class BlackjackEngine:
    """Manages per-user blackjack sessions."""

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

        # (username_lower, channel) → ActiveBlackjack
        self._games: dict[tuple[str, str], ActiveBlackjack] = {}

        # (username_lower, channel) → last deal time (rate limiting)
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    def update_config(self, new_config: EconomyConfig) -> None:
        self._config = new_config
        self._symbol = new_config.currency.symbol

    # ── Queries ───────────────────────────────────────────────

    def get_game(self, username: str, channel: str) -> ActiveBlackjack | None:
        return self._games.get((username.lower(), channel))

    def active_game_count(self) -> int:
        return len(self._games)

    # ── Deal ──────────────────────────────────────────────────

    async def deal(
        self, username: str, channel: str, wager: int,
    ) -> str:
        """Start a new blackjack hand.

        Returns a formatted message string (not a sentinel — BJ is PM-only).
        """
        cfg = self._config.gambling.blackjack
        if not cfg.enabled:
            return "Blackjack is currently disabled."

        key = (username.lower(), channel)
        if key in self._games:
            game = self._games[key]
            return (
                f"You already have an active hand!\n"
                f"Your hand: {game.player_hand.display_with_value()}\n"
                f"Dealer: {game.dealer_hand.display_with_value(hide_second=True)}\n"
                f"Use 'hit', 'stand', or 'double'."
            )

        # Validate wager
        if wager < cfg.min_wager:
            return f"Minimum wager: {cfg.min_wager} {self._symbol}."
        if wager > cfg.max_wager:
            return f"Maximum wager: {cfg.max_wager} {self._symbol}."

        # Account validation (shared across all gambling engines)
        error = await validate_gamble_account(
            self._db, self._config.gambling, self._symbol,
            username, channel, wager,
        )
        if error:
            return error

        # Cooldown — Blackjack is PM-only and not governed by SpectacleManager,
        # so it must enforce its own rate limit.
        last_deal = self._cooldowns.get(key)
        if last_deal and cfg.cooldown_seconds > 0:
            elapsed = (now_utc() - last_deal).total_seconds()
            if elapsed < cfg.cooldown_seconds:
                remaining = int(cfg.cooldown_seconds - elapsed)
                return f"Cooldown: {remaining}s remaining."

        # Daily limit
        if cfg.daily_limit > 0:
            count_today = await get_daily_game_count(
                self._db, username, channel, "blackjack",
            )
            if count_today >= cfg.daily_limit:
                return f"Daily blackjack limit reached ({cfg.daily_limit}/day)."

        success = await self._db.atomic_debit(username, channel, wager)
        if not success:
            return "Insufficient funds."

        # Deal cards
        deck = _new_deck()
        player_hand = Hand([deck.pop(), deck.pop()])
        dealer_hand = Hand([deck.pop(), deck.pop()])

        game = ActiveBlackjack(
            username=username,
            channel=channel,
            wager=wager,
            deck=deck,
            player_hand=player_hand,
            dealer_hand=dealer_hand,
        )
        self._games[key] = game
        self._cooldowns[key] = now_utc()
        await increment_daily_game_count(self._db, username, channel, "blackjack")

        # Check for natural blackjack
        if player_hand.is_blackjack:
            return await self._resolve(game, natural=True)

        return (
            f"🃏 Blackjack — Wager: {wager} {self._symbol}\n"
            f"Your hand: {player_hand.display_with_value()}\n"
            f"Dealer shows: {dealer_hand.display_with_value(hide_second=True)}\n"
            f"'hit', 'stand', or 'double'"
        )

    # ── Hit ───────────────────────────────────────────────────

    async def hit(self, username: str, channel: str) -> str:
        """Draw a card."""
        key = (username.lower(), channel)
        game = self._games.get(key)
        if not game:
            return "No active blackjack hand. Start one with 'blackjack <wager>'."

        game.last_action_at = datetime.now(timezone.utc)
        card = game.deck.pop()
        game.player_hand.cards.append(card)

        if game.player_hand.busted:
            return await self._resolve(game)

        return (
            f"Drew: {card}\n"
            f"Your hand: {game.player_hand.display_with_value()}\n"
            f"Dealer shows: {game.dealer_hand.display_with_value(hide_second=True)}\n"
            f"'hit' or 'stand'"
        )

    # ── Stand ─────────────────────────────────────────────────

    async def stand(self, username: str, channel: str) -> str:
        """Stand — dealer plays out."""
        key = (username.lower(), channel)
        game = self._games.get(key)
        if not game:
            return "No active blackjack hand."

        game.last_action_at = datetime.now(timezone.utc)
        return await self._resolve(game)

    # ── Double down ───────────────────────────────────────────

    async def double_down(self, username: str, channel: str) -> str:
        """Double the wager, take exactly one more card, then stand."""
        key = (username.lower(), channel)
        game = self._games.get(key)
        if not game:
            return "No active blackjack hand."

        if len(game.player_hand.cards) != 2:
            return "You can only double down on your initial two cards."

        if game.doubled:
            return "You've already doubled down."

        # Debit additional wager
        account = await self._db.get_account(username, channel)
        if not account or account.get("balance", 0) < game.wager:
            return f"Insufficient funds to double down. Need {game.wager} {self._symbol} more."

        success = await self._db.atomic_debit(username, channel, game.wager)
        if not success:
            return "Insufficient funds to double down."

        game.doubled = True
        game.wager *= 2
        game.last_action_at = datetime.now(timezone.utc)

        # Draw one card
        card = game.deck.pop()
        game.player_hand.cards.append(card)

        return await self._resolve(game)

    # ── Dealer play & resolution ──────────────────────────────

    async def _resolve(self, game: ActiveBlackjack, *, natural: bool = False) -> str:
        """Play out dealer hand and determine outcome."""
        cfg = self._config.gambling.blackjack
        key = (game.username.lower(), game.channel)

        # Dealer plays (unless player busted or has natural BJ)
        dealer_play_lines: list[str] = []
        if not game.player_hand.busted and not natural:
            while True:
                dval = game.dealer_hand.value
                if dval > 17:
                    break
                if dval == 17 and game.dealer_hand.soft and cfg.dealer_hits_soft_17:
                    pass  # keep going
                elif dval >= 17:
                    break
                card = game.deck.pop()
                game.dealer_hand.cards.append(card)
                dealer_play_lines.append(f"Dealer draws: {card}")

        player_val = game.player_hand.value
        dealer_val = game.dealer_hand.value

        # Determine outcome
        if natural:
            if game.dealer_hand.is_blackjack:
                outcome = BJOutcome.PUSH
            else:
                outcome = BJOutcome.BLACKJACK
        elif game.player_hand.busted:
            outcome = BJOutcome.LOSS
        elif game.dealer_hand.busted:
            outcome = BJOutcome.WIN
        elif player_val > dealer_val:
            outcome = BJOutcome.WIN
        elif player_val < dealer_val:
            outcome = BJOutcome.LOSS
        else:
            outcome = BJOutcome.PUSH

        # Calculate payout
        wager = game.wager
        if outcome == BJOutcome.BLACKJACK:
            payout = wager + int(wager * cfg.blackjack_payout)
        elif outcome == BJOutcome.WIN:
            payout = wager * 2
        elif outcome == BJOutcome.PUSH:
            payout = wager  # return wager
        else:
            payout = 0

        net = payout - wager

        # Credit winnings
        if payout > 0:
            tx_type = "gamble_win" if net > 0 else "gamble_push"
            await self._db.credit(
                game.username, game.channel, payout,
                tx_type=tx_type,
                trigger_id="gambling.blackjack",
                reason=f"Blackjack {outcome.value}: player {player_val} vs dealer {dealer_val}",
            )

        # Record stats
        today = today_str()
        await self._db.update_gambling_stats(
            game.username, game.channel, "blackjack",
            net=net, biggest_win=max(0, net), biggest_loss=abs(min(0, net)),
        )
        await self._db.increment_lifetime_gambled(game.username, game.channel, wager, payout)
        await self._db.increment_daily_gambled(game.username, game.channel, today, wager, payout)
        await self._db.update_blackjack_stats(
            game.username, game.channel,
            outcome=outcome.value, wagered=wager, won=payout,
        )

        # Clean up session
        self._games.pop(key, None)

        # Build result message
        account = await self._db.get_account(game.username, game.channel)
        balance = account.get("balance", 0) if account else 0

        lines: list[str] = []
        for dl in dealer_play_lines:
            lines.append(dl)
        lines.append(f"Your hand: {game.player_hand.display_with_value()}")
        lines.append(f"Dealer: {game.dealer_hand.display_with_value()}")

        if outcome == BJOutcome.BLACKJACK:
            lines.append(f"🎉 BLACKJACK! +{net:,} {self._symbol} ({cfg.blackjack_payout}:1)")
        elif outcome == BJOutcome.WIN:
            lines.append(f"✅ You win! +{net:,} {self._symbol}")
        elif outcome == BJOutcome.PUSH:
            lines.append("↩️ Push. Wager returned.")
        else:
            lines.append(f"❌ Bust! -{wager:,} {self._symbol}")

        lines.append(f"Balance: {balance:,} {self._symbol}")
        return "\n".join(lines)

    # ── Timeout handling ──────────────────────────────────────

    async def check_timeouts(self, channel: str) -> list[tuple[str, str]]:
        """Check for timed-out games. Returns [(username, result_message), ...]."""
        cfg = self._config.gambling.blackjack
        now = datetime.now(timezone.utc)
        results: list[tuple[str, str]] = []

        expired_keys = []
        for key, game in self._games.items():
            if game.channel != channel:
                continue
            elapsed = (now - game.last_action_at).total_seconds()

            if elapsed >= cfg.timeout_seconds:
                expired_keys.append(key)
            elif elapsed >= cfg.timeout_warning_seconds and not game.warned:
                game.warned = True
                remaining = cfg.timeout_seconds - int(elapsed)
                results.append((
                    game.username,
                    f"⏰ Your blackjack hand will auto-stand in {remaining}s!",
                ))

        for key in expired_keys:
            game = self._games.get(key)
            if game:
                result = await self._resolve(game)
                results.append((game.username, f"⏰ Auto-stand (timeout):\n{result}"))

        return results
