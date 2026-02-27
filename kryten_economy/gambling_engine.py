"""Gambling engine — slot machine, coin flip, challenge duels, heist.

Central module for all gambling operations. Every game type validates balance,
enforces cooldowns/caps, executes the game, records outcomes, and returns a
result to be formatted as a PM response.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from .database import EconomyDatabase
from .utils import parse_timestamp

if TYPE_CHECKING:
    from .config import EconomyConfig


# ═══════════════════════════════════════════════════════════════
#  Data types
# ═══════════════════════════════════════════════════════════════


class GambleOutcome(Enum):
    WIN = "win"
    LOSS = "loss"
    JACKPOT = "jackpot"
    PUSH = "push"


@dataclass
class GambleResult:
    """Result of a single gambling action."""

    outcome: GambleOutcome
    wager: int
    payout: int
    net: int
    display: str
    announce_public: bool
    message: str


@dataclass
class PayoutEntry:
    symbols: str
    multiplier: float
    cumulative_probability: float


@dataclass
class ActiveHeist:
    channel: str
    initiator: str
    participants: dict[str, int]
    started_at: datetime
    expires_at: datetime


# ═══════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════

SLOT_SYMBOLS = ["🍒", "🍋", "💎", "7️⃣", "🍊", "🍇", "⭐", "🔔"]


# ═══════════════════════════════════════════════════════════════
#  Engine
# ═══════════════════════════════════════════════════════════════


class GamblingEngine:
    """Evaluates all gambling operations."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._db = database
        self._logger = logger
        self._currency = config.currency.name
        self._symbol = config.currency.symbol

        # Build payout table for slots
        self._slot_payouts = self._build_payout_table(config.gambling.spin.payouts)

        # In-memory cooldowns: (username_lower, game_type) → last_play_time
        self._cooldowns: dict[tuple[str, str], datetime] = {}

        # Ignored users (bots)
        self._ignored_users: set[str] = {u.lower() for u in config.ignored_users}

        # Active heists: channel → ActiveHeist
        self._active_heists: dict[str, ActiveHeist] = {}

    def update_config(self, new_config) -> None:
        """Hot-swap the config reference. Rebuild payout table."""
        self._config = new_config
        self._currency = new_config.currency.name
        self._symbol = new_config.currency.symbol
        self._slot_payouts = self._build_payout_table(new_config.gambling.spin.payouts)
        self._ignored_users = {u.lower() for u in new_config.ignored_users}

    # ══════════════════════════════════════════════════════════
    #  Payout table
    # ══════════════════════════════════════════════════════════

    def _build_payout_table(self, payouts: list[Any]) -> list[PayoutEntry]:
        """Build cumulative probability table from config."""
        table: list[PayoutEntry] = []
        cumulative = 0.0
        for p in payouts:
            cumulative += p.probability
            table.append(PayoutEntry(
                symbols=p.symbols,
                multiplier=p.multiplier,
                cumulative_probability=cumulative,
            ))
        if abs(cumulative - 1.0) > 0.01:
            self._logger.warning(
                "Slot payout probabilities sum to %.4f (expected 1.0)", cumulative,
            )
        return table

    def _resolve_payout(self, roll: float) -> PayoutEntry:
        """Resolve a random roll to a payout entry."""
        for entry in self._slot_payouts:
            if roll <= entry.cumulative_probability:
                return entry
        return self._slot_payouts[-1]

    @staticmethod
    def _generate_loss_display(result_type: str) -> str:
        """Generate a display string for non-matching spins."""
        if result_type == "partial":
            symbol = random.choice(SLOT_SYMBOLS)
            other = random.choice([s for s in SLOT_SYMBOLS if s != symbol])
            return f"{symbol}{symbol}{other}"
        symbols = random.sample(SLOT_SYMBOLS, 3)
        return "".join(symbols)

    # ══════════════════════════════════════════════════════════
    #  Common validation
    # ══════════════════════════════════════════════════════════

    async def _validate_gamble(
        self,
        username: str,
        channel: str,
        wager: int,
        game_type: str,
        min_wager: int,
        max_wager: int,
        cooldown_seconds: int,
        daily_limit: int | None = None,
    ) -> str | None:
        """Returns error message string, or None if valid."""
        if not self._config.gambling.enabled:
            return "Gambling is currently disabled."

        account = await self._db.get_account(username, channel)
        if not account:
            return "You need an account first. Stick around a bit!"

        if account.get("economy_banned"):
            return "Your economy access is restricted."

        min_age = self._config.gambling.min_account_age_minutes
        first_seen = parse_timestamp(account.get("first_seen"))
        if first_seen:
            age_minutes = (datetime.now(timezone.utc) - first_seen).total_seconds() / 60
            if age_minutes < min_age:
                remaining = int(min_age - age_minutes)
                return f"You need to be around for {remaining} more minutes before gambling."

        if wager < min_wager:
            return f"Minimum wager: {min_wager} {self._symbol}."
        if wager > max_wager:
            return f"Maximum wager: {max_wager} {self._symbol}."

        if account.get("balance", 0) < wager:
            return f"Insufficient funds. Balance: {account['balance']} {self._symbol}."

        if cooldown_seconds > 0:
            cooldown_key = (username.lower(), game_type)
            last_play = self._cooldowns.get(cooldown_key)
            if last_play:
                elapsed = (datetime.now(timezone.utc) - last_play).total_seconds()
                if elapsed < cooldown_seconds:
                    remaining = int(cooldown_seconds - elapsed)
                    return f"Cooldown: {remaining}s remaining."

        if daily_limit is not None:
            count_today = await self._get_daily_game_count(username, channel, game_type)
            if count_today >= daily_limit:
                return f"Daily limit reached ({daily_limit} {game_type}s per day)."

        return None

    # ══════════════════════════════════════════════════════════
    #  Daily game count (via trigger_cooldowns)
    # ══════════════════════════════════════════════════════════

    async def _get_daily_game_count(
        self, username: str, channel: str, game_type: str,
    ) -> int:
        trigger_id = f"gambling.{game_type}.daily"
        row = await self._db.get_trigger_cooldown(username, channel, trigger_id)
        if row is None:
            return 0
        window_start = parse_timestamp(row["window_start"])
        if window_start and window_start.date() == datetime.now(timezone.utc).date():
            return row["count"]
        return 0

    async def _increment_daily_game_count(
        self, username: str, channel: str, game_type: str,
    ) -> None:
        trigger_id = f"gambling.{game_type}.daily"
        now = datetime.now(timezone.utc)
        row = await self._db.get_trigger_cooldown(username, channel, trigger_id)
        if row is None:
            await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
        else:
            ws = parse_timestamp(row["window_start"])
            if ws is None or ws.date() != now.date():
                await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
            else:
                await self._db.increment_trigger_cooldown(username, channel, trigger_id)

    # ══════════════════════════════════════════════════════════
    #  Slot Machine
    # ══════════════════════════════════════════════════════════

    async def spin(self, username: str, channel: str, wager: int) -> GambleResult:
        """Execute a slot machine spin."""
        cfg = self._config.gambling.spin

        error = await self._validate_gamble(
            username, channel, wager, "spin",
            cfg.min_wager, cfg.max_wager, cfg.cooldown_seconds, cfg.daily_limit,
        )
        if error:
            return GambleResult(
                outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
                display="", announce_public=False, message=error,
            )

        success = await self._db.atomic_debit(username, channel, wager)
        if not success:
            return GambleResult(
                outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
                display="", announce_public=False, message="Insufficient funds.",
            )

        roll = random.random()
        result_entry = self._resolve_payout(roll)
        payout = int(wager * result_entry.multiplier)
        net = payout - wager

        if payout > 0:
            tx_type = "gamble_win" if net > 0 else "gamble_push"
            await self._db.credit(
                username, channel, payout,
                tx_type=tx_type,
                trigger_id="gambling.spin",
                reason=f"Spin: {result_entry.symbols}",
                metadata=json.dumps({
                    "multiplier": result_entry.multiplier,
                    "roll": round(roll, 4),
                }),
            )

        if result_entry.multiplier >= 50:
            outcome = GambleOutcome.JACKPOT
        elif net > 0:
            outcome = GambleOutcome.WIN
        elif net == 0:
            outcome = GambleOutcome.PUSH
        else:
            outcome = GambleOutcome.LOSS

        display = (
            result_entry.symbols
            if result_entry.symbols not in ("partial", "loss")
            else self._generate_loss_display(result_entry.symbols)
        )

        announce = (
            cfg.announce_jackpots_public
            and payout >= cfg.jackpot_announce_threshold
        )

        # Record stats
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        await self._db.update_gambling_stats(
            username, channel, "spin", net=net,
            biggest_win=max(0, net), biggest_loss=abs(min(0, net)),
        )
        await self._db.increment_lifetime_gambled(username, channel, wager, payout)
        await self._db.increment_daily_gambled(username, channel, today, wager, payout)
        self._cooldowns[(username.lower(), "spin")] = now
        await self._increment_daily_game_count(username, channel, "spin")

        account = await self._db.get_account(username, channel)
        balance = account.get("balance", 0) if account else 0

        if net > 0:
            message = f"🎰 {display} — WIN! +{net} {self._symbol} (Payout: {payout}). Balance: {balance} {self._symbol}"
        elif net == 0:
            message = f"🎰 {display} — Push. Balance: {balance} {self._symbol}"
        else:
            message = f"🎰 {display} — Loss. -{wager} {self._symbol}. Balance: {balance} {self._symbol}"

        return GambleResult(
            outcome=outcome, wager=wager, payout=payout, net=net,
            display=display, announce_public=announce, message=message,
        )

    # ══════════════════════════════════════════════════════════
    #  Coin Flip
    # ══════════════════════════════════════════════════════════

    async def flip(self, username: str, channel: str, wager: int) -> GambleResult:
        """Execute a coin flip — double-or-nothing."""
        cfg = self._config.gambling.flip

        error = await self._validate_gamble(
            username, channel, wager, "flip",
            cfg.min_wager, cfg.max_wager, cfg.cooldown_seconds, cfg.daily_limit,
        )
        if error:
            return GambleResult(
                outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
                display="", announce_public=False, message=error,
            )

        success = await self._db.atomic_debit(username, channel, wager)
        if not success:
            return GambleResult(
                outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
                display="", announce_public=False, message="Insufficient funds.",
            )

        won = random.random() < cfg.win_chance

        if won:
            payout = wager * 2
            net = wager
            display = "🪙 Heads!"
            await self._db.credit(
                username, channel, payout,
                tx_type="gamble_win",
                trigger_id="gambling.flip",
                reason=f"Flip win: {payout}",
            )
            outcome = GambleOutcome.WIN
        else:
            payout = 0
            net = -wager
            display = "🪙 Tails!"
            outcome = GambleOutcome.LOSS

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        await self._db.update_gambling_stats(
            username, channel, "flip", net=net,
            biggest_win=max(0, net), biggest_loss=abs(min(0, net)),
        )
        await self._db.increment_lifetime_gambled(username, channel, wager, payout)
        await self._db.increment_daily_gambled(username, channel, today, wager, payout)
        self._cooldowns[(username.lower(), "flip")] = now
        await self._increment_daily_game_count(username, channel, "flip")

        account = await self._db.get_account(username, channel)
        balance = account.get("balance", 0) if account else 0

        if won:
            message = f"{display} WIN! +{net} {self._symbol}. Balance: {balance} {self._symbol}"
        else:
            message = f"{display} Loss. -{wager} {self._symbol}. Balance: {balance} {self._symbol}"

        return GambleResult(
            outcome=outcome, wager=wager, payout=payout, net=net,
            display=display, announce_public=False, message=message,
        )

    # ══════════════════════════════════════════════════════════
    #  Daily Free Spin
    # ══════════════════════════════════════════════════════════

    async def daily_free_spin(self, username: str, channel: str) -> GambleResult:
        """Execute a daily free spin."""
        cfg = self._config.gambling.daily_free_spin

        if not cfg.enabled:
            return GambleResult(
                outcome=GambleOutcome.LOSS, wager=0, payout=0, net=0,
                display="", announce_public=False, message="Free spins are disabled.",
            )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        activity = await self._db.get_or_create_daily_activity(username, channel, today)
        if activity.get("free_spin_used"):
            return GambleResult(
                outcome=GambleOutcome.LOSS, wager=0, payout=0, net=0,
                display="", announce_public=False,
                message="You've already used your free spin today. Come back tomorrow!",
            )

        wager = cfg.equivalent_wager
        roll = random.random()
        result_entry = self._resolve_payout(roll)
        payout = int(wager * result_entry.multiplier)

        await self._db.mark_free_spin_used(username, channel, today)

        if payout > 0:
            await self._db.credit(
                username, channel, payout,
                tx_type="gamble_win",
                trigger_id="gambling.free_spin",
                reason=f"Free spin: {result_entry.symbols}",
            )

        display = (
            result_entry.symbols
            if result_entry.symbols not in ("partial", "loss")
            else self._generate_loss_display(result_entry.symbols)
        )

        account = await self._db.get_account(username, channel)
        balance = account.get("balance", 0) if account else 0

        announce = (
            self._config.gambling.spin.announce_jackpots_public
            and payout >= self._config.gambling.spin.jackpot_announce_threshold
        )

        if payout > 0:
            message = f"🎁🎰 {display} — FREE SPIN WIN! +{payout} {self._symbol}. Balance: {balance} {self._symbol}"
        else:
            message = f"🎁🎰 {display} — No luck on the free spin. Try again tomorrow!"

        return GambleResult(
            outcome=GambleOutcome.WIN if payout > 0 else GambleOutcome.LOSS,
            wager=0, payout=payout, net=payout,
            display=display, announce_public=announce, message=message,
        )

    # ══════════════════════════════════════════════════════════
    #  Challenge
    # ══════════════════════════════════════════════════════════

    async def create_challenge(
        self, challenger: str, target: str, channel: str, wager: int,
    ) -> str:
        """Create a new challenge. Returns PM response for the challenger.

        Sentinel format ``challenge_created:<id>:<target>`` tells the PM handler
        to send a PM to the target AND a confirmation to the challenger.
        """
        cfg = self._config.gambling.challenge

        if not cfg.enabled:
            return "Challenges are currently disabled."

        error = await self._validate_gamble(
            challenger, channel, wager, "challenge",
            cfg.min_wager, cfg.max_wager, 0, None,
        )
        if error:
            return error

        if challenger.lower() == target.lower():
            return "You can't challenge yourself."

        if target.lower() in self._ignored_users:
            return "That user can't be challenged."

        target_account = await self._db.get_account(target, channel)
        if not target_account:
            return f"{target} doesn't have an account."
        if target_account.get("balance", 0) < wager:
            return f"{target} can't afford that wager."
        if target_account.get("economy_banned"):
            return f"{target}'s economy access is restricted."

        existing = await self._db.get_pending_challenge(challenger, target, channel)
        if existing:
            return f"You already have a pending challenge with {target}."

        success = await self._db.atomic_debit(challenger, channel, wager)
        if not success:
            return "Insufficient funds."

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=cfg.accept_timeout_seconds,
        )
        challenge_id = await self._db.create_challenge(
            challenger, target, channel, wager, expires_at,
        )

        return f"challenge_created:{challenge_id}:{target}"

    async def accept_challenge(
        self, target: str, channel: str,
    ) -> tuple[str, str | None, str | None]:
        """Accept a pending challenge.

        Returns ``(pm_to_target, pm_to_challenger, public_announce)``.
        """
        cfg = self._config.gambling.challenge

        challenge = await self._db.get_pending_challenge_for_target(target, channel)
        if not challenge:
            return ("No pending challenge to accept.", None, None)

        challenger = challenge["challenger"]
        wager = challenge["wager"]
        challenge_id = challenge["id"]

        expires_at = parse_timestamp(challenge["expires_at"])
        if expires_at and datetime.now(timezone.utc) > expires_at:
            await self._expire_challenge(challenge_id, challenger, channel, wager)
            return ("That challenge has expired.", None, None)

        success = await self._db.atomic_debit(target, channel, wager)
        if not success:
            return ("You can't afford the wager anymore.", None, None)

        challenger_wins = random.random() < 0.5
        total_pot = wager * 2
        rake = int(total_pot * (cfg.rake_percent / 100))
        prize = total_pot - rake

        if challenger_wins:
            winner, loser = challenger, target
        else:
            winner, loser = target, challenger

        await self._db.credit(
            winner, channel, prize,
            tx_type="gamble_win",
            trigger_id="gambling.challenge",
            reason=f"Challenge win vs {loser}",
            related_user=loser,
            metadata=json.dumps({"rake": rake, "pot": total_pot}),
        )

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        for player, is_winner in [(winner, True), (loser, False)]:
            player_net = prize - wager if is_winner else -wager
            await self._db.update_gambling_stats(
                player, channel, "challenge",
                net=player_net,
                biggest_win=max(0, player_net),
                biggest_loss=abs(min(0, player_net)),
            )
            await self._db.increment_lifetime_gambled(
                player, channel, wager, prize if is_winner else 0,
            )
            await self._db.increment_daily_gambled(
                player, channel, today, wager, prize if is_winner else 0,
            )

        await self._db.resolve_challenge(challenge_id, "accepted")

        winner_bal = (await self._db.get_account(winner, channel) or {}).get("balance", 0)
        loser_bal = (await self._db.get_account(loser, channel) or {}).get("balance", 0)

        target_msg = (
            f"⚔️ {'You win!' if target == winner else 'You lost!'} "
            f"{'+'  if target == winner else '-'}{wager} {self._symbol}. "
            f"{'Rake: ' + str(rake) + ' ' + self._symbol + '. ' if rake > 0 else ''}"
            f"Balance: {winner_bal if target == winner else loser_bal} {self._symbol}"
        )

        challenger_msg = (
            f"⚔️ {'You win!' if challenger == winner else 'You lost!'} "
            f"{'+'  if challenger == winner else '-'}{wager} {self._symbol}. "
            f"Balance: {winner_bal if challenger == winner else loser_bal} {self._symbol}"
        )

        public_msg = None
        if cfg.announce_public:
            public_msg = (
                f"⚔️ {winner} defeated {loser} in a {wager} {self._symbol} duel! "
                f"(Prize: {prize} {self._symbol}, Rake: {rake} {self._symbol})"
            )

        return (target_msg, challenger_msg, public_msg)

    async def decline_challenge(
        self, target: str, channel: str,
    ) -> tuple[str, str | None]:
        """Decline a pending challenge. Returns ``(pm_to_target, pm_to_challenger)``."""
        challenge = await self._db.get_pending_challenge_for_target(target, channel)
        if not challenge:
            return ("No pending challenge to decline.", None)

        challenger = challenge["challenger"]
        wager = challenge["wager"]
        challenge_id = challenge["id"]

        await self._db.credit(
            challenger, channel, wager,
            tx_type="gamble_win",
            trigger_id="gambling.challenge.refund",
            reason=f"Challenge declined by {target}",
        )
        await self._db.resolve_challenge(challenge_id, "declined")

        return (
            f"Challenge declined. {challenger} has been refunded.",
            f"{target} declined your challenge. {wager} {self._symbol} refunded.",
        )

    async def _expire_challenge(
        self, challenge_id: int, challenger: str, channel: str, wager: int,
    ) -> None:
        """Expire a timed-out challenge and refund the challenger."""
        await self._db.credit(
            challenger, channel, wager,
            tx_type="gamble_win",
            trigger_id="gambling.challenge.refund",
            reason="Challenge expired",
        )
        await self._db.resolve_challenge(challenge_id, "expired")

    async def cleanup_expired_challenges(self, channel: str) -> list[dict]:
        """Find and expire all timed-out pending challenges.

        Returns list of expired challenge info (for PM notifications).
        """
        expired = await self._db.expire_old_challenges()
        results = []
        for challenge in expired:
            if challenge.get("channel") != channel:
                continue
            # Refund challenger
            await self._db.credit(
                challenge["challenger"], channel, challenge["wager"],
                tx_type="gamble_win",
                trigger_id="gambling.challenge.refund",
                reason="Challenge expired",
            )
            results.append(challenge)
        return results

    # ══════════════════════════════════════════════════════════
    #  Heist
    # ══════════════════════════════════════════════════════════

    def get_active_heist(self, channel: str) -> ActiveHeist | None:
        return self._active_heists.get(channel)

    async def start_heist(self, username: str, channel: str, wager: int) -> str:
        """Start a new heist. Returns PM response."""
        cfg = self._config.gambling.heist

        if not cfg.enabled:
            return "Heists are currently disabled."

        if channel in self._active_heists:
            return "A heist is already in progress! Use 'heist join' to join."

        error = await self._validate_gamble(
            username, channel, wager, "heist",
            self._config.gambling.spin.min_wager,
            self._config.gambling.spin.max_wager,
            0, None,
        )
        if error:
            return error

        success = await self._db.atomic_debit(username, channel, wager)
        if not success:
            return "Insufficient funds."

        now = datetime.now(timezone.utc)
        self._active_heists[channel] = ActiveHeist(
            channel=channel,
            initiator=username,
            participants={username: wager},
            started_at=now,
            expires_at=now + timedelta(seconds=cfg.join_window_seconds),
        )

        return f"heist_started:{channel}"

    async def join_heist(self, username: str, channel: str, wager: int) -> str:
        """Join an active heist."""
        if channel not in self._active_heists:
            return "No active heist. Start one with 'heist <wager>'."

        heist = self._active_heists[channel]

        if username in heist.participants:
            return "You're already in this heist."

        if datetime.now(timezone.utc) > heist.expires_at:
            return "The join window has closed."

        success = await self._db.atomic_debit(username, channel, wager)
        if not success:
            return "Insufficient funds."

        heist.participants[username] = wager
        return f"You joined the heist! ({len(heist.participants)} participants so far)"

    async def resolve_heist(self, channel: str) -> tuple[str, list[str]] | None:
        """Resolve an active heist.

        Returns ``(public_message, [participant_usernames])`` or ``None``.
        """
        cfg = self._config.gambling.heist

        if channel not in self._active_heists:
            return None

        heist = self._active_heists.pop(channel)

        if len(heist.participants) < cfg.min_participants:
            for user, wager in heist.participants.items():
                await self._db.credit(
                    user, channel, wager,
                    tx_type="gamble_win",
                    trigger_id="gambling.heist.refund",
                    reason="Heist cancelled — not enough participants",
                )
            return (
                f"🏦 Heist cancelled — only {len(heist.participants)} participants "
                f"(need {cfg.min_participants}). Everyone refunded.",
                list(heist.participants.keys()),
            )

        success = random.random() < cfg.success_chance

        if success:
            for user, wager in heist.participants.items():
                payout = int(wager * cfg.payout_multiplier)
                await self._db.credit(
                    user, channel, payout,
                    tx_type="gamble_win",
                    trigger_id="gambling.heist",
                    reason="Heist success!",
                )
                net = payout - wager
                await self._db.update_gambling_stats(
                    user, channel, "heist", net=net,
                    biggest_win=max(0, net), biggest_loss=0,
                )

            total_pot = sum(heist.participants.values())
            return (
                f"🏦💰 HEIST SUCCESS! {len(heist.participants)} participants split "
                f"{int(total_pot * cfg.payout_multiplier)} {self._symbol}!",
                list(heist.participants.keys()),
            )
        else:
            for user, wager in heist.participants.items():
                await self._db.update_gambling_stats(
                    user, channel, "heist", net=-wager,
                    biggest_win=0, biggest_loss=wager,
                )

            total_lost = sum(heist.participants.values())
            return (
                f"🏦🚨 HEIST FAILED! {len(heist.participants)} participants lost "
                f"{total_lost} {self._symbol} total!",
                list(heist.participants.keys()),
            )

    # ══════════════════════════════════════════════════════════
    #  Gambling Stats
    # ══════════════════════════════════════════════════════════

    async def get_stats_message(self, username: str, channel: str) -> str:
        """Show personal gambling statistics."""
        stats = await self._db.get_gambling_stats(username, channel)

        if not stats:
            return "You haven't gambled yet. Try 'spin' for a free daily spin!"

        net = stats.get("net_gambling", 0)
        net_display = f"+{net}" if net >= 0 else str(net)

        lines = [
            f"🎰 Gambling Stats for {username}:",
            "━" * 15,
            "",
            f"  Spins: {stats.get('total_spins', 0)}",
            f"  Flips: {stats.get('total_flips', 0)}",
            f"  Challenges: {stats.get('total_challenges', 0)}",
            f"  Heists: {stats.get('total_heists', 0)}",
            "",
            f"  Biggest win: {stats.get('biggest_win', 0)} {self._symbol}",
            f"  Biggest loss: {stats.get('biggest_loss', 0)} {self._symbol}",
            f"  Net P&L: {net_display} {self._symbol}",
        ]

        return "\n".join(lines)
