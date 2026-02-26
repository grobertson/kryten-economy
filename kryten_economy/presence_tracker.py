"""Presence tracker — the heart of dwell-time earning.

Tracks connected users, implements join debounce, periodic presence tick,
and (Sprint 2) streaks, hourly milestones, night watch, welcome wallet/back.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .config import (
    EconomyConfig,
    OnboardingConfig,
    PresenceConfig,
    RetentionConfig,
    StreaksConfig,
)
from .database import EconomyDatabase
from .utils import now_utc, parse_timestamp, today_str

if TYPE_CHECKING:
    from .channel_state import ChannelStateTracker


@dataclass
class UserSession:
    """Tracks a single user's current connection state."""

    username: str
    channel: str
    connected_at: datetime
    last_tick_at: datetime
    is_afk: bool = False
    cumulative_minutes_today: int = 0
    is_genuine_arrival: bool = False
    _current_date: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))


class PresenceTracker:
    """Manages user presence, earning ticks, streaks, and milestones."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        client: object | None = None,
        logger: logging.Logger | None = None,
        channel_state: ChannelStateTracker | None = None,
    ) -> None:
        self._config: EconomyConfig = config
        self._presence_config: PresenceConfig = config.presence
        self._streak_config: StreaksConfig = config.streaks
        self._onboarding_config: OnboardingConfig = config.onboarding
        self._retention_config: RetentionConfig = config.retention
        self._db = database
        self._client = client
        self._channel_state = channel_state
        self._logger = logger or logging.getLogger("economy.presence")

        # Currency info for PM messages
        self._currency_name = config.currency.name
        self._currency_symbol = config.currency.symbol

        # Active sessions: {(username_lower, channel): UserSession}
        self._sessions: dict[tuple[str, str], UserSession] = {}
        # Departure timestamps for debounce: {(username_lower, channel): datetime}
        self._last_departure: dict[tuple[str, str], datetime] = {}
        # Normalized ignored-user set for O(1) lookup
        self._ignored_users: set[str] = {u.lower() for u in config.ignored_users}

        # CyTube rank tracking: {(channel, username_lower): rank}
        self._user_ranks: dict[tuple[str, str], int] = {}

        # Periodic tick task handle
        self._tick_task: asyncio.Task | None = None
        self._running = False

        # Metrics counters (exposed to metrics_server)
        self.metrics_z_earned: int = 0

    # ══════════════════════════════════════════════════════════
    #  Public API
    # ══════════════════════════════════════════════════════════

    async def handle_user_join(self, username: str, channel: str) -> bool:
        """Process adduser event. Returns True if genuine arrival."""
        if self._is_ignored(username):
            return False

        key = (username.lower(), channel)

        # If session already exists, don't update connected_at (handles duplicate adduser)
        if key in self._sessions:
            return False

        genuine = await self._is_genuine_arrival(username, channel)
        now = now_utc()

        if genuine:
            session = UserSession(
                username=username,
                channel=channel,
                connected_at=now,
                last_tick_at=now,
                is_genuine_arrival=True,
            )
            self._sessions[key] = session

            # Remove from departure tracking
            self._last_departure.pop(key, None)

            # Ensure account exists
            account = await self._db.get_or_create_account(username, channel)
            await self._db.update_last_seen(username, channel)

            # ── Welcome wallet (Sprint 2: new users) ────────
            if not account.get("welcome_wallet_claimed"):
                wallet_amount = self._onboarding_config.welcome_wallet
                if wallet_amount > 0:
                    claimed = await self._db.claim_welcome_wallet(username, channel, wallet_amount)
                    if claimed:
                        msg = self._onboarding_config.welcome_message.format(
                            amount=wallet_amount,
                            currency=self._currency_name,
                        )
                        await self._send_pm(channel, username, msg)

            # ── Welcome-back bonus (Sprint 2: returning users) ──
            elif self._retention_config.welcome_back.enabled:
                last_seen = parse_timestamp(account.get("last_seen"))
                if last_seen:
                    days_absent = (now - last_seen).days
                    if days_absent >= self._retention_config.welcome_back.days_absent:
                        bonus = self._retention_config.welcome_back.bonus
                        await self._db.credit(
                            username,
                            channel,
                            bonus,
                            tx_type="welcome_back",
                            trigger_id="retention.welcome_back",
                            reason=f"Welcome back ({days_absent} days absent)",
                        )
                        msg = self._retention_config.welcome_back.message.format(
                            amount=bonus,
                            currency=self._currency_name,
                        )
                        await self._send_pm(channel, username, msg)

            # ── Sprint 3: Notify channel state of genuine arrival ──
            if self._channel_state is not None:
                self._channel_state.record_genuine_join(channel, username, now)
        else:
            # Bounce — preserve session continuity
            # Use original connection time if available
            departure_time = self._last_departure.get(key)
            connected_at = departure_time if departure_time else now
            session = UserSession(
                username=username,
                channel=channel,
                connected_at=connected_at,
                last_tick_at=now,
                is_genuine_arrival=False,
            )
            self._sessions[key] = session
            self._logger.debug(
                "Debounced join for %s in %s",
                username,
                channel,
            )

        return genuine

    async def handle_user_leave(self, username: str, channel: str) -> None:
        """Process userleave event."""
        if self._is_ignored(username):
            return

        key = (username.lower(), channel)
        if key not in self._sessions:
            return

        now = now_utc()
        self._last_departure[key] = now

        # Schedule deferred cleanup after debounce window
        debounce_seconds = self._presence_config.join_debounce_minutes * 60
        loop = asyncio.get_running_loop()
        loop.call_later(
            debounce_seconds,
            lambda u=username, c=channel: asyncio.ensure_future(self._finalize_departure(u, c)),
        )

    def get_connected_users(self, channel: str) -> set[str]:
        """Return set of currently connected usernames for channel."""
        return {
            session.username
            for (_, ch), session in self._sessions.items()
            if ch == channel
        }

    def get_connected_count(self, channel: str) -> int:
        """Return count of connected users (excludes ignored)."""
        return sum(1 for (_, ch) in self._sessions if ch == channel)

    def is_connected(self, username: str, channel: str) -> bool:
        """Check if a specific user is currently connected."""
        return (username.lower(), channel) in self._sessions

    def get_present_users(self, channel: str) -> list[str]:
        """Return list of currently connected usernames for channel."""
        return list(self.get_connected_users(channel))

    def update_user_rank(self, channel: str, username: str, rank: int) -> None:
        """Track the latest known CyTube rank for a user."""
        self._user_ranks[(channel, username.lower())] = rank

    def get_admin_users(self, channel: str, min_rank: int) -> list[str]:
        """Get present users with CyTube rank >= min_rank."""
        present = self.get_connected_users(channel)
        return [
            u for u in present
            if self._user_ranks.get((channel, u.lower()), 0) >= min_rank
        ]

    def update_config(self, new_config: EconomyConfig) -> None:
        """Hot-swap the config reference."""
        self._config = new_config
        self._presence_config = new_config.presence
        self._streak_config = new_config.streaks
        self._onboarding_config = new_config.onboarding
        self._retention_config = new_config.retention
        self._currency_name = new_config.currency.name
        self._currency_symbol = new_config.currency.symbol
        self._ignored_users = {u.lower() for u in new_config.ignored_users}

    def was_absent_longer_than(self, username: str, channel: str, minutes: int) -> bool:
        """Return True if the user was absent for at least *minutes* minutes.

        Used by GreetingHandler to apply the longer greeting_absence_minutes
        threshold rather than the short join_debounce_minutes window.

        Returns True if no departure record exists (truly new or long gone).
        """
        key = (username.lower(), channel)
        departure_time = self._last_departure.get(key)
        if departure_time is None:
            return True  # No record → treat as long absence
        from datetime import timedelta as _td
        return now_utc() - departure_time >= _td(minutes=minutes)

    async def start(self) -> None:
        """Start the periodic tick task."""
        self._running = True
        self._tick_task = asyncio.create_task(self._presence_tick())
        self._logger.info("Presence tracker started")

    async def stop(self) -> None:
        """Cancel the tick task, finalize all sessions."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

        # Final last_seen update for all active sessions
        for (_username_lower, channel), session in list(self._sessions.items()):
            try:
                await self._db.update_last_seen(session.username, channel)
            except Exception:
                pass
        self._sessions.clear()
        self._logger.info("Presence tracker stopped")

    # ══════════════════════════════════════════════════════════
    #  Internal: Ignored Users
    # ══════════════════════════════════════════════════════════

    def _is_ignored(self, username: str) -> bool:
        return username.lower() in self._ignored_users

    # ══════════════════════════════════════════════════════════
    #  Internal: Join Debounce
    # ══════════════════════════════════════════════════════════

    async def _is_genuine_arrival(self, username: str, channel: str) -> bool:
        """Return True if this join represents a user who was genuinely absent."""
        key = (username.lower(), channel)
        threshold = timedelta(minutes=self._presence_config.join_debounce_minutes)

        # Check in-memory first (fast path)
        departure_time = self._last_departure.get(key)
        if departure_time is not None:
            if now_utc() - departure_time < threshold:
                return False  # bounce
            return True  # genuinely gone

        # Fallback: check DB last_seen (for service restarts)
        account = await self._db.get_account(username, channel)
        if account and account.get("last_seen"):
            last_seen = parse_timestamp(account["last_seen"])
            if last_seen and now_utc() - last_seen < threshold:
                return False  # likely a bounce around service restart

        return True  # no record — treat as genuine

    async def _finalize_departure(self, username: str, channel: str) -> None:
        """Finalize departure after debounce window expires."""
        key = (username.lower(), channel)
        session = self._sessions.get(key)
        departure = self._last_departure.get(key)

        # If user has reconnected since departure was recorded, do nothing
        if session and departure and session.connected_at > departure:
            return

        # If session still references the old connection (user didn't rejoin)
        if key in self._sessions:
            del self._sessions[key]
            try:
                await self._db.update_last_seen(username, channel)
            except Exception:
                self._logger.exception("Failed to update last_seen on departure for %s", username)

        # Clean up departure record
        self._last_departure.pop(key, None)

    # ══════════════════════════════════════════════════════════
    #  Internal: Presence Tick
    # ══════════════════════════════════════════════════════════

    async def _presence_tick(self) -> None:
        """Award presence Z to all connected users. Runs every 60 seconds."""
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break

            now = now_utc()
            today = now.strftime("%Y-%m-%d")
            current_hour = now.hour

            for key, session in list(self._sessions.items()):
                username, channel = session.username, session.channel

                try:
                    # ── 0. Calendar day reset (Sprint 2) ────────
                    if session._current_date != today:
                        session.cumulative_minutes_today = 0
                        session._current_date = today

                    # ── 1. Base presence earning ─────────────────
                    amount = self._presence_config.base_rate_per_minute

                    # ── 2. Night watch multiplier (Sprint 2) ─────
                    metadata: dict = {}
                    nw = self._presence_config.night_watch
                    if nw.enabled and current_hour in nw.hours:
                        amount = int(amount * nw.multiplier)
                        metadata["multiplier"] = "night_watch"
                        metadata["factor"] = nw.multiplier

                    # ── 3. Credit presence Z ─────────────────────
                    if amount > 0:
                        await self._db.credit(
                            username,
                            channel,
                            amount,
                            tx_type="earn",
                            reason="Presence",
                            trigger_id="presence.base",
                            metadata=json.dumps(metadata) if metadata else None,
                        )
                        await self._db.increment_daily_minutes_present(username, channel, today)
                        await self._db.increment_daily_z_earned(username, channel, today, amount)

                    # ── 4. Update session tracking ───────────────
                    session.cumulative_minutes_today += 1
                    session.last_tick_at = now
                    await self._db.update_last_seen(username, channel)

                    # ── 5. Hourly dwell milestones (Sprint 2) ────
                    await self._check_hourly_milestones(
                        username, channel, today, session.cumulative_minutes_today
                    )

                    # ── 6. Daily streak check (Sprint 2) ─────────
                    streak_cfg = self._streak_config.daily
                    if (
                        streak_cfg.enabled
                        and session.cumulative_minutes_today == streak_cfg.min_presence_minutes
                    ):
                        # Exact threshold crossing — evaluate streak once
                        await self._evaluate_daily_streak(username, channel, today)
                        await self._evaluate_bridge(username, channel, today)

                    # Update metrics counter
                    self.metrics_z_earned += amount
                except Exception:
                    self._logger.exception("Presence tick error for %s/%s", username, channel)

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Hourly Milestones
    # ══════════════════════════════════════════════════════════

    async def _check_hourly_milestones(
        self, username: str, channel: str, date: str, cumulative_minutes: int
    ) -> None:
        """Award hourly milestones that haven't been claimed today."""
        milestones = self._presence_config.hourly_milestones  # {hours: reward}
        for hours, reward in sorted(milestones.items()):
            threshold_minutes = hours * 60
            if cumulative_minutes >= threshold_minutes:
                row = await self._db.get_or_create_hourly_milestones(username, channel, date)
                col = f"hours_{hours}"
                if not row.get(col):
                    await self._db.credit(
                        username,
                        channel,
                        reward,
                        tx_type="milestone",
                        trigger_id=f"dwell.{hours}h",
                        reason=f"{hours}-hour dwell milestone",
                    )
                    await self._db.mark_hourly_milestone(username, channel, date, hours)
                    await self._send_pm(
                        channel,
                        username,
                        f"⏰ {hours}-hour milestone! +{reward} {self._currency_symbol}. Keep it up!",
                    )

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Daily Streaks
    # ══════════════════════════════════════════════════════════

    async def _evaluate_daily_streak(self, username: str, channel: str, today: str) -> None:
        """Called once per user per day when they hit min_presence_minutes."""
        streak = await self._db.get_or_create_streak(username, channel)
        last_date = streak.get("last_streak_date")
        current = streak.get("current_daily_streak", 0)
        longest = streak.get("longest_daily_streak", 0)

        if last_date == today:
            return  # Already counted today

        yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

        if last_date == yesterday:
            current += 1  # Streak continues
        else:
            current = 1  # Streak resets

        longest = max(current, longest)
        await self._db.update_streak(username, channel, current, longest, today)

        # Award streak bonus (day 2+)
        cfg = self._streak_config.daily
        if current >= 2:
            rewards = cfg.rewards
            # For days > max defined reward, use day-7 reward or the highest defined
            reward = rewards.get(current)
            if reward is None:
                reward = rewards.get(7, 100)  # Default to day-7 reward for day 8+
            if reward > 0:
                await self._db.credit(
                    username,
                    channel,
                    reward,
                    tx_type="streak_bonus",
                    trigger_id=f"streak.day{current}",
                    reason=f"Day {current} streak bonus",
                )
                await self._send_pm(
                    channel,
                    username,
                    f"🔥 Day {current} streak! +{reward} {self._currency_symbol}!",
                )

        # Milestone bonuses (on top of daily reward)
        if current == 7 and cfg.milestone_7_bonus > 0:
            await self._db.credit(
                username,
                channel,
                cfg.milestone_7_bonus,
                tx_type="streak_bonus",
                trigger_id="streak.milestone.7",
                reason="7-day streak milestone",
            )
            await self._send_pm(
                channel,
                username,
                f"🔥🔥 7-DAY STREAK! +{cfg.milestone_7_bonus} {self._currency_symbol}! You're on fire!",
            )

        if current == 30 and cfg.milestone_30_bonus > 0:
            await self._db.credit(
                username,
                channel,
                cfg.milestone_30_bonus,
                tx_type="streak_bonus",
                trigger_id="streak.milestone.30",
                reason="30-day streak milestone",
            )
            await self._send_pm(
                channel,
                username,
                f"🔥🔥🔥 30-DAY STREAK! +{cfg.milestone_30_bonus} {self._currency_symbol}! LEGENDARY!",
            )

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Weekend-Weekday Bridge
    # ══════════════════════════════════════════════════════════

    async def _evaluate_bridge(self, username: str, channel: str, today: str) -> None:
        """Check and award weekend→weekday bridge bonus."""
        bridge_cfg = self._streak_config.weekend_weekday_bridge
        if not bridge_cfg.enabled:
            return

        today_dt = datetime.strptime(today, "%Y-%m-%d")
        iso_week = today_dt.strftime("%G-W%V")
        is_weekend = today_dt.weekday() >= 5  # Sat=5, Sun=6

        streak = await self._db.get_or_create_streak(username, channel)

        # Reset if new week
        if streak.get("week_number") != iso_week:
            await self._db.update_bridge_fields(
                username,
                channel,
                weekend_seen=False,
                weekday_seen=False,
                bridge_claimed=False,
                week_number=iso_week,
            )
            streak["weekend_seen_this_week"] = 0
            streak["weekday_seen_this_week"] = 0
            streak["bridge_claimed_this_week"] = 0

        # Update seen flags
        if is_weekend and not streak.get("weekend_seen_this_week"):
            await self._db.update_bridge_fields(username, channel, weekend_seen=True)
            streak["weekend_seen_this_week"] = 1
        elif not is_weekend and not streak.get("weekday_seen_this_week"):
            await self._db.update_bridge_fields(username, channel, weekday_seen=True)
            streak["weekday_seen_this_week"] = 1

        # Check for bridge
        if (
            streak.get("weekend_seen_this_week")
            and streak.get("weekday_seen_this_week")
            and not streak.get("bridge_claimed_this_week")
        ):
            bonus = bridge_cfg.bonus
            await self._db.credit(
                username,
                channel,
                bonus,
                tx_type="earn",
                trigger_id="bridge.weekly",
                reason="Weekend-weekday bridge bonus",
            )
            await self._db.update_bridge_fields(username, channel, bridge_claimed=True)
            await self._send_pm(
                channel,
                username,
                f"🌉 Weekend→weekday bridge bonus! +{bonus} {self._currency_symbol}!",
            )

    # ══════════════════════════════════════════════════════════
    #  PM Sending
    # ══════════════════════════════════════════════════════════

    async def _send_pm(self, channel: str, username: str, message: str) -> None:
        """Send PM via kryten-py client. Safe to call if client is None (testing)."""
        if self._client is None:
            return
        try:
            await self._client.send_pm(channel, username, message)
        except Exception:
            self._logger.debug("Failed to send PM to %s: %s", username, message[:50])
