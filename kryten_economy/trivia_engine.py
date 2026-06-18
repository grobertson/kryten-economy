"""Trivia gamble engine — spectacle game with wagered Q&A.

Users bet on their ability to answer a trivia question correctly.
Correct answer pays out based on difficulty; wrong answer forfeits the wager.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .database import EconomyDatabase
from .gambling_common import validate_gamble_account
from .trivia_client import TriviaClient, TriviaQuestion
from .utils import today_str

if TYPE_CHECKING:
    from .config import EconomyConfig


@dataclass
class ActiveTrivia:
    """In-memory state for a running trivia round."""

    channel: str
    question: TriviaQuestion
    wagers: dict[str, int]  # username → amount
    answers: dict[str, str]  # username → letter (A/B/C/D)
    started_at: datetime
    betting_closes_at: datetime
    answer_deadline: datetime
    resolved: bool = False


class TriviaEngine:
    """Manages the trivia round lifecycle."""

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

        self._client = TriviaClient(
            cache_size=config.gambling.trivia.question_cache_size,
            logger=logger,
        )

        # channel → ActiveTrivia
        self._active: dict[str, ActiveTrivia] = {}

    def update_config(self, new_config: EconomyConfig) -> None:
        self._config = new_config
        self._symbol = new_config.currency.symbol

    async def close(self) -> None:
        await self._client.close()

    # ── Queries ───────────────────────────────────────────────

    def get_active_trivia(self, channel: str) -> ActiveTrivia | None:
        return self._active.get(channel)

    # ── Start trivia ──────────────────────────────────────────

    async def start_trivia(
        self,
        channel: str,
        initiator: str,
        wager: int,
    ) -> str:
        """Start a trivia round.

        Returns sentinel ``trivia_started:<channel>`` on success,
        or an error message string.
        """
        cfg = self._config.gambling.trivia
        if not cfg.enabled:
            return "Trivia is currently disabled."

        if channel in self._active:
            return "A trivia round is already in progress."

        # Validate wager
        if wager < cfg.min_wager:
            return f"Minimum wager: {cfg.min_wager} {self._symbol}."
        if wager > cfg.max_wager:
            return f"Maximum wager: {cfg.max_wager} {self._symbol}."

        # Account validation (shared across all gambling engines)
        error = await validate_gamble_account(
            self._db, self._config.gambling, self._symbol,
            initiator, channel, wager,
        )
        if error:
            return error

        # Fetch a question
        difficulty = cfg.difficulty
        if difficulty == "random":
            difficulty = random.choice(["easy", "medium", "hard"])

        question = await self._client.get_question(
            category=cfg.category,
            difficulty=difficulty,
        )
        if question is None:
            return "Trivia is temporarily unavailable — couldn't fetch a question."

        # Debit initiator
        success = await self._db.atomic_debit(initiator, channel, wager)
        if not success:
            return "Insufficient funds."

        now = datetime.now(timezone.utc)
        trivia = ActiveTrivia(
            channel=channel,
            question=question,
            wagers={initiator: wager},
            answers={},
            started_at=now,
            betting_closes_at=now + timedelta(seconds=cfg.betting_window_seconds),
            answer_deadline=now + timedelta(seconds=cfg.answer_window_seconds),
        )
        self._active[channel] = trivia

        self._logger.info(
            "Trivia started in %s by %s (difficulty=%s)",
            channel, initiator, question.difficulty,
        )
        return f"trivia_started:{channel}"

    # ── Join / bet ────────────────────────────────────────────

    async def place_bet(
        self,
        username: str,
        channel: str,
        amount: int,
    ) -> str:
        """Place a trivia bet (join a round).

        Returns sentinel or error string.
        """
        cfg = self._config.gambling.trivia
        trivia = self._active.get(channel)
        if not trivia:
            return "No active trivia round."

        now = datetime.now(timezone.utc)
        if now > trivia.betting_closes_at:
            return "Betting window has closed. Answer the question!"

        if username.lower() in {u.lower() for u in trivia.wagers}:
            return "You've already bet in this trivia round."

        if amount < cfg.min_wager:
            return f"Minimum wager: {cfg.min_wager} {self._symbol}."
        if amount > cfg.max_wager:
            return f"Maximum wager: {cfg.max_wager} {self._symbol}."

        # Account validation (shared; includes the min-account-age gate that
        # start_trivia also enforces, so joining can't bypass it)
        error = await validate_gamble_account(
            self._db, self._config.gambling, self._symbol,
            username, channel, amount,
        )
        if error:
            return error

        success = await self._db.atomic_debit(username, channel, amount)
        if not success:
            return "Insufficient funds."

        trivia.wagers[username] = amount
        return f"trivia_bet:{channel}:{username}:{amount}"

    # ── Answer submission ─────────────────────────────────────

    def submit_answer(
        self,
        username: str,
        channel: str,
        answer: str,
    ) -> str | None:
        """Record a user's answer. Returns None if not relevant.

        Only users who have bet can answer. First answer per user only.
        Accepts A/B/C/D (case-insensitive).
        """
        trivia = self._active.get(channel)
        if not trivia:
            return None

        now = datetime.now(timezone.utc)
        if now > trivia.answer_deadline:
            return None

        # Must have bet
        if username not in trivia.wagers:
            return None

        # First answer only
        if username in trivia.answers:
            return None

        # Accept single letter A-D
        letter = answer.strip().upper()
        if len(letter) == 1 and letter in "ABCD":
            trivia.answers[username] = letter
            return f"trivia_answer:{channel}:{username}:{letter}"

        # Try matching full answer text
        for i, ans in enumerate(trivia.question.all_answers):
            if answer.strip().lower() == ans.lower():
                trivia.answers[username] = chr(65 + i)
                return f"trivia_answer:{channel}:{username}:{chr(65 + i)}"

        return None

    # ── Resolve ───────────────────────────────────────────────

    async def resolve_trivia(
        self,
        channel: str,
    ) -> tuple[list[str], dict[str, str]] | None:
        """Grade answers and pay out.

        Returns (public_lines, {username: pm_text}) or None.
        """
        trivia = self._active.pop(channel, None)
        if not trivia or trivia.resolved:
            return None

        trivia.resolved = True
        cfg = self._config.gambling.trivia
        q = trivia.question
        correct_letter = q.correct_letter

        # Difficulty multiplier
        multiplier = getattr(cfg.payout_multipliers, q.difficulty, 2.0)

        per_user_pm: dict[str, str] = {}
        winners: list[str] = []
        losers: list[str] = []
        today = today_str()

        for username, wager in trivia.wagers.items():
            user_answer = trivia.answers.get(username)

            if user_answer == correct_letter:
                # Correct!
                payout = int(wager * multiplier)
                net = payout - wager
                await self._db.credit(
                    username, channel, payout,
                    tx_type="gamble_win",
                    trigger_id="gambling.trivia",
                    reason=f"Trivia correct: {q.correct_answer}",
                )
                await self._db.update_gambling_stats(
                    username, channel, "trivia", net=net,
                    biggest_win=max(0, net),
                )
                await self._db.increment_lifetime_gambled(username, channel, wager, payout)
                await self._db.increment_daily_gambled(username, channel, today, wager, payout)
                await self._db.update_trivia_stats(
                    username, channel, correct=True, wagered=wager, won=payout,
                )
                per_user_pm[username] = (
                    f"✅ Correct! The answer was {correct_letter}) {q.correct_answer}. "
                    f"You won {payout:,} {self._symbol} (+{net:,} net, {multiplier}x)."
                )
                winners.append(f"@{username} (+{net:,})")
            else:
                # Wrong or no answer
                reason = "no answer" if user_answer is None else f"answered {user_answer}"
                await self._db.update_gambling_stats(
                    username, channel, "trivia", net=-wager,
                    biggest_loss=wager,
                )
                await self._db.increment_lifetime_gambled(username, channel, wager, 0)
                await self._db.increment_daily_gambled(username, channel, today, wager, 0)
                await self._db.update_trivia_stats(
                    username, channel, correct=False, wagered=wager, won=0,
                )
                per_user_pm[username] = (
                    f"❌ Wrong! The answer was {correct_letter}) {q.correct_answer}. "
                    f"You lost {wager:,} {self._symbol} ({reason})."
                )
                losers.append(f"@{username} (-{wager:,})")

        # Build public lines
        lines = [
            f"✅ The answer is: {correct_letter}) {q.correct_answer}",
        ]
        if winners:
            lines.append(f"Winners: {', '.join(winners)}")
        if losers:
            lines.append(f"Losers: {', '.join(losers)}")
        if not winners:
            lines.append("Nobody got it right! 😬")

        return lines, per_user_pm

    # ── Display ───────────────────────────────────────────────

    def get_question_display(self, channel: str) -> str | None:
        """Format the active trivia question for chat."""
        trivia = self._active.get(channel)
        if not trivia:
            return None

        remaining = max(0, int(
            (trivia.answer_deadline - datetime.now(timezone.utc)).total_seconds()
        ))

        display = trivia.question.format_display()
        display += f"\n\nBet now: !trivia <amount> — Answer in chat within {remaining}s!"
        return display

    # ── Prefetch ──────────────────────────────────────────────

    async def prefetch_questions(self) -> int:
        """Pre-fill the question cache (call on startup)."""
        cfg = self._config.gambling.trivia
        return await self._client.prefetch(
            category=cfg.category,
            difficulty=cfg.difficulty if cfg.difficulty != "random" else None,
        )
