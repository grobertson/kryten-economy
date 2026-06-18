"""Shared helpers for all gambling engines.

Single source of truth for pre-wager account validation and daily game-count
tracking, reused by GamblingEngine (spin/flip/challenge/heist) and the
spectacle/solo engines (race, trivia, blackjack).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .utils import now_utc, parse_timestamp

if TYPE_CHECKING:
    from .config import GamblingConfig
    from .database import EconomyDatabase


async def validate_gamble_account(
    db: EconomyDatabase,
    gambling_cfg: GamblingConfig,
    symbol: str,
    username: str,
    channel: str,
    wager: int,
    *,
    require_age: bool = True,
) -> str | None:
    """Validate that a user may place a wager.

    Centralizes: enabled gate, account existence, economy ban, minimum
    account age, and balance check. Returns an error message string, or
    ``None`` if the wager may proceed.
    """
    if not gambling_cfg.enabled:
        return "Gambling is currently disabled."

    account = await db.get_account(username, channel)
    if not account:
        return "You need an account first. Stick around a bit!"

    if account.get("economy_banned"):
        return "Your economy access is restricted."

    if require_age:
        min_age = gambling_cfg.min_account_age_minutes
        first_seen = parse_timestamp(account.get("first_seen"))
        if first_seen:
            age_minutes = (now_utc() - first_seen).total_seconds() / 60
            if age_minutes < min_age:
                remaining = int(min_age - age_minutes)
                return (
                    f"You need to be around for {remaining} more minutes "
                    f"before gambling."
                )

    if account.get("balance", 0) < wager:
        return f"Insufficient funds. Balance: {account['balance']} {symbol}."

    return None


async def get_daily_game_count(
    db: EconomyDatabase, username: str, channel: str, game_type: str,
) -> int:
    """Return how many times ``game_type`` was played today (UTC)."""
    trigger_id = f"gambling.{game_type}.daily"
    row = await db.get_trigger_cooldown(username, channel, trigger_id)
    if row is None:
        return 0
    window_start = parse_timestamp(row["window_start"])
    if window_start and window_start.date() == now_utc().date():
        return row["count"]
    return 0


async def increment_daily_game_count(
    db: EconomyDatabase, username: str, channel: str, game_type: str,
) -> None:
    """Increment the daily play counter for ``game_type`` (UTC window)."""
    trigger_id = f"gambling.{game_type}.daily"
    now = now_utc()
    row = await db.get_trigger_cooldown(username, channel, trigger_id)
    if row is None:
        await db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
        return
    window_start = parse_timestamp(row["window_start"])
    if window_start is None or window_start.date() != now.date():
        await db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
    else:
        await db.increment_trigger_cooldown(username, channel, trigger_id)
