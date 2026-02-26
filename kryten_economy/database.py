"""SQLite database module for kryten-economy.

Follows the kryten-userstats pattern: each public method is async and wraps
a synchronous inner function via asyncio.run_in_executor(None, _sync).
A new connection is created per call (WAL mode, 30s busy timeout, Row factory).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any


class EconomyDatabase:
    """SQLite-backed persistence for the economy microservice."""

    def __init__(self, db_path: str, logger: logging.Logger) -> None:
        self._db_path = db_path
        self._logger = logger

    def _get_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with standard settings."""
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    # ══════════════════════════════════════════════════════════
    #  Initialization
    # ══════════════════════════════════════════════════════════

    async def initialize(self) -> None:
        """Create all tables and indexes. Idempotent."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._create_tables)

    def _create_tables(self) -> None:
        conn = self._get_connection()
        try:
            # ── Sprint 1: Core tables ────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    balance INTEGER DEFAULT 0,
                    lifetime_earned INTEGER DEFAULT 0,
                    lifetime_spent INTEGER DEFAULT 0,
                    lifetime_gambled_in INTEGER DEFAULT 0,
                    lifetime_gambled_out INTEGER DEFAULT 0,
                    rank_name TEXT DEFAULT 'Extra',
                    cytube_level INTEGER DEFAULT 1,
                    chat_color TEXT,
                    custom_greeting TEXT,
                    custom_title TEXT,
                    channel_gif_url TEXT,
                    channel_gif_approved BOOLEAN DEFAULT 0,
                    personal_currency_name TEXT,
                    welcome_wallet_claimed BOOLEAN DEFAULT 0,
                    economy_banned BOOLEAN DEFAULT 0,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP,
                    UNIQUE(username, channel)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    reason TEXT,
                    trigger_id TEXT,
                    related_user TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_activity (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    date TEXT NOT NULL,
                    minutes_present INTEGER DEFAULT 0,
                    minutes_active INTEGER DEFAULT 0,
                    messages_sent INTEGER DEFAULT 0,
                    long_messages INTEGER DEFAULT 0,
                    gifs_posted INTEGER DEFAULT 0,
                    unique_emotes_used INTEGER DEFAULT 0,
                    kudos_given INTEGER DEFAULT 0,
                    kudos_received INTEGER DEFAULT 0,
                    laughs_received INTEGER DEFAULT 0,
                    bot_interactions INTEGER DEFAULT 0,
                    z_earned INTEGER DEFAULT 0,
                    z_spent INTEGER DEFAULT 0,
                    z_gambled_in INTEGER DEFAULT 0,
                    z_gambled_out INTEGER DEFAULT 0,
                    first_message_claimed BOOLEAN DEFAULT 0,
                    free_spin_used BOOLEAN DEFAULT 0,
                    queues_used INTEGER DEFAULT 0,
                    UNIQUE(username, channel, date)
                )
            """)

            # Sprint 1 indexes
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_username_channel "
                "ON transactions(username, channel)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_created_at "
                "ON transactions(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_type "
                "ON transactions(type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_activity_date "
                "ON daily_activity(date)"
            )

            # ── Sprint 2: Streaks & milestones tables ────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS streaks (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    current_daily_streak INTEGER DEFAULT 0,
                    longest_daily_streak INTEGER DEFAULT 0,
                    last_streak_date TEXT,
                    weekend_seen_this_week BOOLEAN DEFAULT 0,
                    weekday_seen_this_week BOOLEAN DEFAULT 0,
                    bridge_claimed_this_week BOOLEAN DEFAULT 0,
                    week_number TEXT,
                    UNIQUE(username, channel)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS hourly_milestones (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    date TEXT NOT NULL,
                    hours_1 BOOLEAN DEFAULT 0,
                    hours_3 BOOLEAN DEFAULT 0,
                    hours_6 BOOLEAN DEFAULT 0,
                    hours_12 BOOLEAN DEFAULT 0,
                    hours_24 BOOLEAN DEFAULT 0,
                    UNIQUE(username, channel, date)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS trigger_cooldowns (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    window_start TIMESTAMP,
                    UNIQUE(username, channel, trigger_id)
                )
            """)

            # ── Sprint 3: Trigger analytics ──────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trigger_analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    hit_count INTEGER DEFAULT 0,
                    unique_users INTEGER DEFAULT 0,
                    total_z_awarded INTEGER DEFAULT 0,
                    UNIQUE(channel, trigger_id, date)
                )
            """)

            # ── Sprint 4: Gambling tables ────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gambling_stats (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    total_spins INTEGER DEFAULT 0,
                    total_flips INTEGER DEFAULT 0,
                    total_challenges INTEGER DEFAULT 0,
                    total_heists INTEGER DEFAULT 0,
                    biggest_win INTEGER DEFAULT 0,
                    biggest_loss INTEGER DEFAULT 0,
                    net_gambling INTEGER DEFAULT 0,
                    UNIQUE(username, channel)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_challenges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    challenger TEXT NOT NULL,
                    target TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    wager INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)

            # ── Sprint 5: Spending tables ────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tip_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tip_sender ON tip_history(sender, channel)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tip_receiver ON tip_history(receiver, channel)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tip_date ON tip_history(created_at)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_by TEXT,
                    resolved_at TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_status ON pending_approvals(status, channel)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approval_user ON pending_approvals(username, channel)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanity_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    active BOOLEAN DEFAULT 1,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(username, channel, item_type)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vanity_user ON vanity_items(username, channel)"
            )

            # ── Sprint 6: Achievements table ─────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS achievements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    achievement_id TEXT NOT NULL,
                    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(username, channel, achievement_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_achievements_user ON achievements(username, channel)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_achievements_id ON achievements(achievement_id, channel)"
            )

            # ── Sprint 7: Bounties table ─────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bounties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT DEFAULT 'open',
                    winner TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    resolved_by TEXT,
                    resolved_at TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(channel, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bounties_creator ON bounties(creator, channel)"
            )

            # ── Sprint 8: Snapshots & Bans ───────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS economy_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    snapshot_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_accounts INTEGER,
                    total_z_circulation INTEGER,
                    active_economy_users_today INTEGER,
                    z_earned_today INTEGER,
                    z_spent_today INTEGER,
                    z_gambled_net_today INTEGER,
                    median_balance INTEGER,
                    participation_rate REAL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_channel "
                "ON economy_snapshots(channel, snapshot_time)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    banned_by TEXT NOT NULL,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT,
                    UNIQUE(username, channel)
                )
            """)

            conn.commit()
            self._logger.info("Database tables created/verified")
        finally:
            conn.close()

    # ══════════════════════════════════════════════════════════
    #  Account Operations
    # ══════════════════════════════════════════════════════════

    async def get_or_create_account(self, username: str, channel: str) -> dict:
        """Return account row as dict. Creates with defaults if not exists."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO accounts (username, channel) VALUES (?, ?)",
                    (username, channel),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_account(self, username: str, channel: str) -> dict | None:
        """Return account row as dict, or None if not exists."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_balance(self, username: str, channel: str) -> int:
        """Return balance integer, 0 if account doesn't exist."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT balance FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["balance"] if row else 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def update_last_seen(self, username: str, channel: str) -> None:
        """Set last_seen to CURRENT_TIMESTAMP."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET last_seen = CURRENT_TIMESTAMP WHERE username = ? AND channel = ?",
                    (username, channel),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def update_last_active(self, username: str, channel: str) -> None:
        """Set last_active to CURRENT_TIMESTAMP."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET last_active = CURRENT_TIMESTAMP WHERE username = ? AND channel = ?",
                    (username, channel),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Balance Operations
    # ══════════════════════════════════════════════════════════

    async def credit(
        self,
        username: str,
        channel: str,
        amount: int,
        tx_type: str,
        reason: str | None = None,
        trigger_id: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """Atomically credit Z to account and log transaction.
        Updates balance and lifetime_earned. Returns new balance.
        Creates account if not exists."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                # Ensure account exists
                conn.execute(
                    "INSERT OR IGNORE INTO accounts (username, channel) VALUES (?, ?)",
                    (username, channel),
                )
                conn.execute(
                    "UPDATE accounts SET balance = balance + ?, lifetime_earned = lifetime_earned + ? "
                    "WHERE username = ? AND channel = ?",
                    (amount, amount, username, channel),
                )
                conn.execute(
                    "INSERT INTO transactions (username, channel, amount, type, reason, trigger_id, "
                    "related_user, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (username, channel, amount, tx_type, reason, trigger_id, related_user, metadata),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT balance FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["balance"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def debit(
        self,
        username: str,
        channel: str,
        amount: int,
        tx_type: str,
        reason: str | None = None,
        trigger_id: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> int | None:
        """Atomically debit Z from account and log transaction.
        Returns new balance on success, None on insufficient funds."""
        loop = asyncio.get_running_loop()

        def _sync() -> int | None:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "UPDATE accounts SET balance = balance - ?, lifetime_spent = lifetime_spent + ? "
                    "WHERE username = ? AND channel = ? AND balance >= ?",
                    (amount, amount, username, channel, amount),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    return None  # Insufficient funds or account doesn't exist
                conn.execute(
                    "INSERT INTO transactions (username, channel, amount, type, reason, trigger_id, "
                    "related_user, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (username, channel, -amount, tx_type, reason, trigger_id, related_user, metadata),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT balance FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["balance"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Daily Activity
    # ══════════════════════════════════════════════════════════

    async def increment_daily_minutes_present(
        self, username: str, channel: str, date: str, minutes: int = 1
    ) -> None:
        """Add minutes to daily_activity.minutes_present via UPSERT."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, minutes_present) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET minutes_present = minutes_present + excluded.minutes_present",
                    (username, channel, date, minutes),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_z_earned(
        self, username: str, channel: str, date: str, amount: int
    ) -> None:
        """Add to daily_activity.z_earned."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, z_earned) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET z_earned = z_earned + excluded.z_earned",
                    (username, channel, date, amount),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Population Queries
    # ══════════════════════════════════════════════════════════

    async def get_total_circulation(self, channel: str) -> int:
        """SUM(balance) for all accounts in channel."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(balance), 0) AS total FROM accounts WHERE channel = ?",
                    (channel,),
                ).fetchone()
                return row["total"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_account_count(self, channel: str) -> int:
        """COUNT of accounts in channel."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM accounts WHERE channel = ?",
                    (channel,),
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Welcome Wallet
    # ══════════════════════════════════════════════════════════

    async def claim_welcome_wallet(self, username: str, channel: str, amount: int) -> bool:
        """Atomically credit welcome wallet if not already claimed.
        Returns True if credited, False if already claimed."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "UPDATE accounts SET balance = balance + ?, lifetime_earned = lifetime_earned + ?, "
                    "welcome_wallet_claimed = 1 "
                    "WHERE username = ? AND channel = ? AND welcome_wallet_claimed = 0",
                    (amount, amount, username, channel),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    return False
                conn.execute(
                    "INSERT INTO transactions (username, channel, amount, type, trigger_id) "
                    "VALUES (?, ?, ?, 'welcome_wallet', 'onboarding.wallet')",
                    (username, channel, amount),
                )
                conn.commit()
                return True
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Streaks
    # ══════════════════════════════════════════════════════════

    async def get_or_create_streak(self, username: str, channel: str) -> dict:
        """Return streak row, creating with defaults if not exists."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO streaks (username, channel) VALUES (?, ?)",
                    (username, channel),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM streaks WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def update_streak(
        self,
        username: str,
        channel: str,
        current_streak: int,
        longest_streak: int,
        last_date: str,
    ) -> None:
        """Update streak counters."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE streaks SET current_daily_streak = ?, longest_daily_streak = ?, "
                    "last_streak_date = ? WHERE username = ? AND channel = ?",
                    (current_streak, longest_streak, last_date, username, channel),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def update_bridge_fields(
        self,
        username: str,
        channel: str,
        weekend_seen: bool | None = None,
        weekday_seen: bool | None = None,
        bridge_claimed: bool | None = None,
        week_number: str | None = None,
    ) -> None:
        """Update weekend/weekday bridge tracking fields."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                updates: list[str] = []
                params: list[Any] = []
                if weekend_seen is not None:
                    updates.append("weekend_seen_this_week = ?")
                    params.append(int(weekend_seen))
                if weekday_seen is not None:
                    updates.append("weekday_seen_this_week = ?")
                    params.append(int(weekday_seen))
                if bridge_claimed is not None:
                    updates.append("bridge_claimed_this_week = ?")
                    params.append(int(bridge_claimed))
                if week_number is not None:
                    updates.append("week_number = ?")
                    params.append(week_number)
                if not updates:
                    return
                params.extend([username, channel])
                conn.execute(
                    f"UPDATE streaks SET {', '.join(updates)} WHERE username = ? AND channel = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Hourly Milestones
    # ══════════════════════════════════════════════════════════

    async def get_or_create_hourly_milestones(
        self, username: str, channel: str, date: str
    ) -> dict:
        """Return milestones row for today, creating if needed."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO hourly_milestones (username, channel, date) VALUES (?, ?, ?)",
                    (username, channel, date),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM hourly_milestones WHERE username = ? AND channel = ? AND date = ?",
                    (username, channel, date),
                ).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def mark_hourly_milestone(
        self, username: str, channel: str, date: str, hours: int
    ) -> None:
        """Set the hours_N column to 1."""
        loop = asyncio.get_running_loop()
        col = f"hours_{hours}"

        def _sync() -> None:
            conn = self._get_connection()
            try:
                # Validate column name to prevent SQL injection
                valid_cols = {"hours_1", "hours_3", "hours_6", "hours_12", "hours_24"}
                if col not in valid_cols:
                    self._logger.warning("Invalid milestone column: %s", col)
                    return
                conn.execute(
                    f"UPDATE hourly_milestones SET {col} = 1 "
                    "WHERE username = ? AND channel = ? AND date = ?",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Balance Maintenance
    # ══════════════════════════════════════════════════════════

    async def get_accounts_with_min_balance(self, channel: str, min_balance: int) -> list[dict]:
        """Return all accounts in channel with balance >= min_balance."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM accounts WHERE channel = ? AND balance >= ?",
                    (channel, min_balance),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def apply_interest_batch(
        self, channel: str, rate: float, cap: int, min_balance: int
    ) -> int:
        """Apply interest to all qualifying accounts. Returns total interest paid."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, balance FROM accounts WHERE channel = ? AND balance >= ?",
                    (channel, min_balance),
                ).fetchall()
                total = 0
                for row in rows:
                    interest = min(math.floor(row["balance"] * rate), cap)
                    if interest > 0:
                        conn.execute(
                            "UPDATE accounts SET balance = balance + ?, lifetime_earned = lifetime_earned + ? "
                            "WHERE username = ? AND channel = ?",
                            (interest, interest, row["username"], channel),
                        )
                        conn.execute(
                            "INSERT INTO transactions (username, channel, amount, type, trigger_id) "
                            "VALUES (?, ?, ?, 'interest', 'maintenance.interest')",
                            (row["username"], channel, interest),
                        )
                        total += interest
                conn.commit()
                return total
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def apply_decay_batch(
        self, channel: str, rate: float, exempt_below: int
    ) -> int:
        """Apply decay to all qualifying accounts. Returns total decay collected."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, balance FROM accounts WHERE channel = ? AND balance >= ?",
                    (channel, exempt_below),
                ).fetchall()
                total = 0
                for row in rows:
                    decay_amount = math.floor(row["balance"] * rate)
                    if decay_amount > 0:
                        conn.execute(
                            "UPDATE accounts SET balance = balance - ?, lifetime_spent = lifetime_spent + ? "
                            "WHERE username = ? AND channel = ?",
                            (decay_amount, decay_amount, row["username"], channel),
                        )
                        conn.execute(
                            "INSERT INTO transactions (username, channel, amount, type, trigger_id, reason) "
                            "VALUES (?, ?, ?, 'decay', 'maintenance.decay', 'Vault maintenance fee')",
                            (row["username"], channel, -decay_amount),
                        )
                        total += decay_amount
                conn.commit()
                return total
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 3: Daily Activity (Chat Triggers)
    # ══════════════════════════════════════════════════════════

    async def get_or_create_daily_activity(
        self, username: str, channel: str, date: str,
    ) -> dict:
        """Return daily_activity row as dict, creating with defaults if needed."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO daily_activity (username, channel, date) VALUES (?, ?, ?)",
                    (username, channel, date),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM daily_activity WHERE username = ? AND channel = ? AND date = ?",
                    (username, channel, date),
                ).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def mark_first_message_claimed(
        self, username: str, channel: str, date: str,
    ) -> None:
        """Set first_message_claimed = 1 for the given day."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, first_message_claimed) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET first_message_claimed = 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_messages_sent(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, messages_sent) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET messages_sent = messages_sent + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_long_messages(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, long_messages) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET long_messages = long_messages + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_gifs_posted(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, gifs_posted) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET gifs_posted = gifs_posted + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_kudos_given(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, kudos_given) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET kudos_given = kudos_given + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_kudos_received(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, kudos_received) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET kudos_received = kudos_received + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_laughs_received(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, laughs_received) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET laughs_received = laughs_received + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_bot_interactions(
        self, username: str, channel: str, date: str,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, bot_interactions) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET bot_interactions = bot_interactions + 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def set_daily_unique_emotes(
        self, username: str, channel: str, date: str, count: int,
    ) -> None:
        """Set unique_emotes_used to given count."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, unique_emotes_used) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username, channel, date) DO UPDATE "
                    "SET unique_emotes_used = ?",
                    (username, channel, date, count, count),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 3: Trigger Cooldowns
    # ══════════════════════════════════════════════════════════

    async def get_trigger_cooldown(
        self, username: str, channel: str, trigger_id: str,
    ) -> dict | None:
        """Return cooldown row, or None if not exists."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM trigger_cooldowns WHERE username = ? AND channel = ? AND trigger_id = ?",
                    (username, channel, trigger_id),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def set_trigger_cooldown(
        self, username: str, channel: str, trigger_id: str,
        count: int, window_start: Any,
    ) -> None:
        """Insert or replace cooldown entry."""
        loop = asyncio.get_running_loop()
        ts = window_start.isoformat() if hasattr(window_start, "isoformat") else str(window_start)

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO trigger_cooldowns (username, channel, trigger_id, count, window_start) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(username, channel, trigger_id) DO UPDATE "
                    "SET count = excluded.count, window_start = excluded.window_start",
                    (username, channel, trigger_id, count, ts),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_trigger_cooldown(
        self, username: str, channel: str, trigger_id: str,
    ) -> None:
        """Increment count by 1 for an existing cooldown entry."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE trigger_cooldowns SET count = count + 1 "
                    "WHERE username = ? AND channel = ? AND trigger_id = ?",
                    (username, channel, trigger_id),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 3: Trigger Analytics
    # ══════════════════════════════════════════════════════════

    async def record_trigger_analytics(
        self, channel: str, trigger_id: str, date: str, z_awarded: int,
    ) -> None:
        """Upsert trigger analytics: increment hit_count, add to total_z_awarded."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO trigger_analytics (channel, trigger_id, date, hit_count, unique_users, total_z_awarded) "
                    "VALUES (?, ?, ?, 1, 1, ?) "
                    "ON CONFLICT(channel, trigger_id, date) DO UPDATE SET "
                    "hit_count = hit_count + 1, total_z_awarded = total_z_awarded + ?",
                    (channel, trigger_id, date, z_awarded, z_awarded),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 4: Gambling Stats
    # ══════════════════════════════════════════════════════════

    async def update_gambling_stats(
        self, username: str, channel: str, game_type: str,
        net: int, biggest_win: int = 0, biggest_loss: int = 0,
    ) -> None:
        """Upsert gambling stats for a game outcome."""
        loop = asyncio.get_running_loop()
        game_col = f"total_{game_type}s"

        def _sync() -> None:
            conn = self._get_connection()
            try:
                valid_cols = {"total_spins", "total_flips", "total_challenges", "total_heists"}
                if game_col not in valid_cols:
                    self._logger.warning("Invalid gambling stat column: %s", game_col)
                    return
                conn.execute(
                    f"INSERT INTO gambling_stats (username, channel, {game_col}, biggest_win, biggest_loss, net_gambling) "
                    f"VALUES (?, ?, 1, ?, ?, ?) "
                    f"ON CONFLICT(username, channel) DO UPDATE SET "
                    f"{game_col} = {game_col} + 1, "
                    "biggest_win = MAX(biggest_win, excluded.biggest_win), "
                    "biggest_loss = MAX(biggest_loss, excluded.biggest_loss), "
                    "net_gambling = net_gambling + excluded.net_gambling",
                    (username, channel, biggest_win, biggest_loss, net),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def get_gambling_stats(self, username: str, channel: str) -> dict | None:
        """Return gambling_stats row, or None."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM gambling_stats WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def increment_lifetime_gambled(
        self, username: str, channel: str, wagered: int, payout: int,
    ) -> None:
        """Update lifetime_gambled_in and lifetime_gambled_out on accounts."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET lifetime_gambled_in = lifetime_gambled_in + ?, "
                    "lifetime_gambled_out = lifetime_gambled_out + ? "
                    "WHERE username = ? AND channel = ?",
                    (wagered, payout, username, channel),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def atomic_debit(self, username: str, channel: str, amount: int) -> bool:
        """Debit balance atomically; return True if succeeded, False if insufficient."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "UPDATE accounts SET balance = balance - ? "
                    "WHERE username = ? AND channel = ? AND balance >= ?",
                    (amount, username, channel, amount),
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 4: Challenges
    # ══════════════════════════════════════════════════════════

    async def create_challenge(
        self, challenger: str, target: str, channel: str,
        wager: int, expires_at: Any,
    ) -> int:
        """Insert a pending challenge. Returns the challenge ID."""
        loop = asyncio.get_running_loop()
        ts = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)

        def _sync() -> int:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "INSERT INTO pending_challenges (challenger, target, channel, wager, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (challenger, target, channel, wager, ts),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_pending_challenge(
        self, challenger: str, target: str, channel: str,
    ) -> dict | None:
        """Return the latest pending challenge between two users, or None."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM pending_challenges "
                    "WHERE challenger = ? AND target = ? AND channel = ? AND status = 'pending' "
                    "ORDER BY id DESC LIMIT 1",
                    (challenger, target, channel),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_pending_challenge_for_target(
        self, target: str, channel: str,
    ) -> dict | None:
        """Return the latest pending challenge targeting a user, or None."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM pending_challenges "
                    "WHERE target = ? AND channel = ? AND status = 'pending' "
                    "ORDER BY id DESC LIMIT 1",
                    (target, channel),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def resolve_challenge(self, challenge_id: int, status: str) -> None:
        """Update challenge status to 'accepted', 'declined', or 'expired'."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE pending_challenges SET status = ? WHERE id = ?",
                    (status, challenge_id),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def expire_old_challenges(self) -> list[dict]:
        """Expire all pending challenges past their expires_at. Returns expired rows."""
        loop = asyncio.get_running_loop()
        now = datetime.now(timezone.utc).isoformat()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM pending_challenges WHERE status = 'pending' "
                    "AND expires_at < ?",
                    (now,),
                ).fetchall()
                if rows:
                    conn.execute(
                        "UPDATE pending_challenges SET status = 'expired' "
                        "WHERE status = 'pending' AND expires_at < ?",
                        (now,),
                    )
                    conn.commit()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def mark_free_spin_used(self, username: str, channel: str, date: str) -> None:
        """Set free_spin_used = 1 in daily_activity for today."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, free_spin_used) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(username, channel, date) DO UPDATE SET free_spin_used = 1",
                    (username, channel, date),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def increment_daily_gambled(
        self, username: str, channel: str, date: str, wagered: int, payout: int,
    ) -> None:
        """Update daily_activity.z_gambled_in += wagered, z_gambled_out += payout."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO daily_activity (username, channel, date, z_gambled_in, z_gambled_out) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(username, channel, date) DO UPDATE SET "
                    "z_gambled_in = z_gambled_in + ?, z_gambled_out = z_gambled_out + ?",
                    (username, channel, date, wagered, payout, wagered, payout),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Tips
    # ══════════════════════════════════════════════════════════

    async def record_tip(
        self, sender: str, receiver: str, channel: str, amount: int,
    ) -> None:
        """Record a tip in tip_history."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO tip_history (sender, receiver, channel, amount) VALUES (?, ?, ?, ?)",
                    (sender, receiver, channel, amount),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def get_tips_sent_today(self, username: str, channel: str) -> int:
        """Sum of tips sent by username today."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM tip_history "
                    "WHERE sender = ? AND channel = ? AND DATE(created_at) = DATE('now')",
                    (username, channel),
                ).fetchone()
                return row["total"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_tip_count_today(self, username: str, channel: str) -> int:
        """Number of distinct tips sent today."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM tip_history "
                    "WHERE sender = ? AND channel = ? AND DATE(created_at) = DATE('now')",
                    (username, channel),
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Vanity Items
    # ══════════════════════════════════════════════════════════

    async def set_vanity_item(
        self, username: str, channel: str, item_type: str, value: str,
    ) -> None:
        """Upsert a vanity item."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO vanity_items (username, channel, item_type, value) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username, channel, item_type) DO UPDATE "
                    "SET value = excluded.value, active = 1, purchased_at = CURRENT_TIMESTAMP",
                    (username, channel, item_type, value),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def get_vanity_item(
        self, username: str, channel: str, item_type: str,
    ) -> str | None:
        """Get active vanity value, or None."""
        loop = asyncio.get_running_loop()

        def _sync() -> str | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT value FROM vanity_items WHERE username = ? AND channel = ? "
                    "AND item_type = ? AND active = 1",
                    (username, channel, item_type),
                ).fetchone()
                return row["value"] if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_custom_greeting(self, username: str, channel: str) -> str | None:
        """Get custom_greeting vanity value."""
        return await self.get_vanity_item(username, channel, "custom_greeting")

    async def get_all_vanity_items(
        self, username: str, channel: str,
    ) -> dict[str, str]:
        """Return all active vanity items as {item_type: value}."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict[str, str]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT item_type, value FROM vanity_items "
                    "WHERE username = ? AND channel = ? AND active = 1",
                    (username, channel),
                ).fetchall()
                return {r["item_type"]: r["value"] for r in rows}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_users_with_custom_greetings(self, channel: str) -> dict[str, str]:
        """Return {username: greeting_text} for all users with active greetings."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict[str, str]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, value FROM vanity_items "
                    "WHERE channel = ? AND item_type = 'custom_greeting' AND active = 1",
                    (channel,),
                ).fetchall()
                return {r["username"]: r["value"] for r in rows}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Approvals
    # ══════════════════════════════════════════════════════════

    async def create_pending_approval(
        self, username: str, channel: str, approval_type: str,
        data: dict | str, cost: int,
    ) -> int:
        """Insert a pending approval. Returns the approval ID."""
        loop = asyncio.get_running_loop()
        data_str = json.dumps(data) if isinstance(data, dict) else data

        def _sync() -> int:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "INSERT INTO pending_approvals (username, channel, type, data, cost) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (username, channel, approval_type, data_str, cost),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_pending_approvals(
        self, channel: str, approval_type: str | None = None,
    ) -> list[dict]:
        """List pending approvals, optionally filtered by type."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                if approval_type:
                    rows = conn.execute(
                        "SELECT * FROM pending_approvals "
                        "WHERE channel = ? AND status = 'pending' AND type = ? "
                        "ORDER BY id DESC",
                        (channel, approval_type),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM pending_approvals "
                        "WHERE channel = ? AND status = 'pending' ORDER BY id DESC",
                        (channel,),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def resolve_approval(
        self, approval_id: int, resolved_by: str, approved: bool,
    ) -> dict | None:
        """Resolve an approval. Returns the approval record or None."""
        loop = asyncio.get_running_loop()
        status = "approved" if approved else "rejected"
        now = datetime.now(timezone.utc).isoformat()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM pending_approvals WHERE id = ? AND status = 'pending'",
                    (approval_id,),
                ).fetchone()
                if not row:
                    return None
                conn.execute(
                    "UPDATE pending_approvals SET status = ?, resolved_by = ?, resolved_at = ? "
                    "WHERE id = ?",
                    (status, resolved_by, now, approval_id),
                )
                conn.commit()
                return dict(row)
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Queue Tracking
    # ══════════════════════════════════════════════════════════

    async def get_queues_today(self, username: str, channel: str) -> int:
        """Count queue transactions today."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM transactions "
                    "WHERE username = ? AND channel = ? "
                    "AND trigger_id LIKE 'spend.queue%' "
                    "AND DATE(created_at) = DATE('now')",
                    (username, channel),
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_last_queue_time(self, username: str, channel: str) -> datetime | None:
        """Last queue transaction timestamp (for cooldown)."""
        loop = asyncio.get_running_loop()

        def _sync() -> datetime | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT created_at FROM transactions "
                    "WHERE username = ? AND channel = ? "
                    "AND trigger_id LIKE 'spend.queue%' "
                    "ORDER BY id DESC LIMIT 1",
                    (username, channel),
                ).fetchone()
                if not row:
                    return None
                ts = row["created_at"]
                if isinstance(ts, str):
                    # Parse ISO or SQLite timestamp format
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S+00:00"):
                        try:
                            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                        except ValueError:
                            continue
                    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                return ts
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Transaction History
    # ══════════════════════════════════════════════════════════

    async def get_recent_transactions(
        self, username: str, channel: str, limit: int = 10,
    ) -> list[dict]:
        """Return last N transactions for a user, newest first."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE username = ? AND channel = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (username, channel, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 6: Achievements
    # ══════════════════════════════════════════════════════════

    async def has_achievement(self, username: str, channel: str, achievement_id: str) -> bool:
        """Check if a user already has a specific achievement."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT 1 FROM achievements WHERE username = ? AND channel = ? AND achievement_id = ?",
                    (username, channel, achievement_id),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def award_achievement(self, username: str, channel: str, achievement_id: str) -> bool:
        """Award an achievement. Returns True if newly awarded, False if already held."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                try:
                    conn.execute(
                        "INSERT INTO achievements (username, channel, achievement_id) VALUES (?, ?, ?)",
                        (username, channel, achievement_id),
                    )
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_user_achievements(self, username: str, channel: str) -> list[dict]:
        """List all achievements for a user."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT achievement_id, awarded_at FROM achievements "
                    "WHERE username = ? AND channel = ? ORDER BY awarded_at",
                    (username, channel),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_achievement_count(self, username: str, channel: str) -> int:
        """Count achievements earned by a user."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM achievements WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 6: Rank / Progression Queries
    # ══════════════════════════════════════════════════════════

    async def get_lifetime_earned(self, username: str, channel: str) -> int:
        """Get lifetime_earned from accounts table."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT lifetime_earned FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["lifetime_earned"] if row else 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_lifetime_presence_hours(self, username: str, channel: str) -> float:
        """Calculate cumulative presence hours from daily activity data."""
        loop = asyncio.get_running_loop()

        def _sync() -> float:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(minutes_present), 0) AS total "
                    "FROM daily_activity WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["total"] / 60.0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_lifetime_messages(self, username: str, channel: str) -> int:
        """Calculate cumulative messages sent from daily activity data."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(messages_sent), 0) AS total "
                    "FROM daily_activity WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["total"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_unique_tip_recipients(self, username: str, channel: str) -> int:
        """Count distinct receivers in tip_history for this sender."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT receiver) AS cnt FROM tip_history "
                    "WHERE sender = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_unique_tip_senders(self, username: str, channel: str) -> int:
        """Count distinct senders in tip_history for this receiver."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT sender) AS cnt FROM tip_history "
                    "WHERE receiver = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_lifetime_gambled(self, username: str, channel: str) -> int:
        """Sum of all wagers from accounts lifetime_gambled_in."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COALESCE(lifetime_gambled_in, 0) AS total "
                    "FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["total"] if row else 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_biggest_gambling_win(self, username: str, channel: str) -> int:
        """Max single win from gambling_stats."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COALESCE(biggest_win, 0) AS bw FROM gambling_stats "
                    "WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row["bw"] if row else 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def update_account_rank(self, username: str, channel: str, rank_name: str) -> None:
        """Update the rank_name field on an account."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET rank_name = ? WHERE username = ? AND channel = ?",
                    (rank_name, username, channel),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 6: Leaderboard Queries
    # ══════════════════════════════════════════════════════════

    async def get_top_earners_today(self, channel: str, limit: int = 10) -> list[dict]:
        """Top Z earned today. Returns [{username, earned_today}, ...]"""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, z_earned AS earned_today FROM daily_activity "
                    "WHERE channel = ? AND date = DATE('now') AND z_earned > 0 "
                    "ORDER BY z_earned DESC LIMIT ?",
                    (channel, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_richest_users(self, channel: str, limit: int = 10) -> list[dict]:
        """Highest current balances. Returns [{username, balance, rank_name}, ...]"""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, balance, rank_name FROM accounts "
                    "WHERE channel = ? ORDER BY balance DESC LIMIT ?",
                    (channel, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_highest_lifetime(self, channel: str, limit: int = 10) -> list[dict]:
        """Highest lifetime earned. Returns [{username, lifetime_earned, rank_name}, ...]"""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, lifetime_earned, rank_name FROM accounts "
                    "WHERE channel = ? ORDER BY lifetime_earned DESC LIMIT ?",
                    (channel, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_rank_distribution(self, channel: str) -> dict[str, int]:
        """Count users at each rank tier. Returns {rank_name: count}."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict[str, int]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT rank_name, COUNT(*) AS cnt FROM accounts "
                    "WHERE channel = ? GROUP BY rank_name",
                    (channel,),
                ).fetchall()
                return {r["rank_name"]: r["cnt"] for r in rows}
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_gambling_summary(self, username: str, channel: str) -> dict | None:
        """Get gambling summary: total games and net profit."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT total_spins + total_flips + total_challenges + total_heists AS total_games, "
                    "net_gambling AS net_profit FROM gambling_stats "
                    "WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 7: Bounties
    # ══════════════════════════════════════════════════════════

    async def create_bounty(
        self, creator: str, channel: str, description: str,
        amount: int, expires_at: str | None = None,
    ) -> int:
        """Create a bounty. Returns bounty ID."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "INSERT INTO bounties (creator, channel, description, amount, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (creator, channel, description, amount, expires_at),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_open_bounties(self, channel: str, limit: int = 20) -> list[dict]:
        """List open bounties."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT id, creator, description, amount, created_at, expires_at "
                    "FROM bounties WHERE channel = ? AND status = 'open' "
                    "ORDER BY id DESC LIMIT ?",
                    (channel, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_bounty(self, bounty_id: int, channel: str) -> dict | None:
        """Get a single bounty by ID."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM bounties WHERE id = ? AND channel = ?",
                    (bounty_id, channel),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def claim_bounty(
        self, bounty_id: int, channel: str, winner: str, resolved_by: str,
    ) -> bool:
        """Claim a bounty. Returns True if updated."""
        loop = asyncio.get_running_loop()
        now = datetime.now(timezone.utc).isoformat()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "UPDATE bounties SET status = 'claimed', winner = ?, resolved_by = ?, "
                    "resolved_at = ? WHERE id = ? AND channel = ? AND status = 'open'",
                    (winner, resolved_by, now, bounty_id, channel),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def cancel_bounty(
        self, bounty_id: int, channel: str, resolved_by: str,
    ) -> bool:
        """Cancel a bounty. Returns True if updated."""
        loop = asyncio.get_running_loop()
        now = datetime.now(timezone.utc).isoformat()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "UPDATE bounties SET status = 'cancelled', resolved_by = ?, "
                    "resolved_at = ? WHERE id = ? AND channel = ? AND status = 'open'",
                    (resolved_by, now, bounty_id, channel),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def expire_bounties(self, channel: str) -> list[dict]:
        """Find and expire all open bounties past expires_at. Returns expired bounties."""
        loop = asyncio.get_running_loop()
        now = datetime.now(timezone.utc).isoformat()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM bounties WHERE channel = ? AND status = 'open' "
                    "AND expires_at IS NOT NULL AND expires_at < ?",
                    (channel, now),
                ).fetchall()
                expired = [dict(r) for r in rows]
                if expired:
                    conn.execute(
                        "UPDATE bounties SET status = 'expired' "
                        "WHERE channel = ? AND status = 'open' "
                        "AND expires_at IS NOT NULL AND expires_at < ?",
                        (channel, now),
                    )
                    conn.commit()
                return expired
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 7: Daily Competition Queries
    # ══════════════════════════════════════════════════════════

    async def get_daily_activity_all(self, channel: str, date: str) -> list[dict]:
        """Get all daily_activity rows for a channel+date."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM daily_activity WHERE channel = ? AND date = ?",
                    (channel, date),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_daily_top(
        self, channel: str, date: str, field: str, limit: int = 1,
    ) -> list[dict]:
        """Get top users for a specific daily_activity field."""
        loop = asyncio.get_running_loop()
        valid_fields = {
            "messages_sent", "long_messages", "gifs_posted", "unique_emotes_used",
            "kudos_given", "kudos_received", "laughs_received", "bot_interactions",
            "z_earned", "z_spent", "z_gambled_in", "z_gambled_out",
            "minutes_present", "minutes_active",
        }

        def _sync() -> list[dict]:
            if field not in valid_fields:
                return []
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    f"SELECT username, {field} AS value FROM daily_activity "
                    f"WHERE channel = ? AND date = ? AND {field} > 0 "
                    f"ORDER BY {field} DESC LIMIT ?",
                    (channel, date, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_daily_threshold_qualifiers(
        self, channel: str, date: str, field: str, threshold: int,
    ) -> list[str]:
        """Get usernames where daily_activity.{field} >= threshold."""
        loop = asyncio.get_running_loop()
        valid_fields = {
            "messages_sent", "long_messages", "gifs_posted", "unique_emotes_used",
            "kudos_given", "kudos_received", "laughs_received", "bot_interactions",
            "z_earned", "z_spent", "z_gambled_in", "z_gambled_out",
            "minutes_present", "minutes_active",
        }

        def _sync() -> list[str]:
            if field not in valid_fields:
                return []
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    f"SELECT username FROM daily_activity "
                    f"WHERE channel = ? AND date = ? AND {field} >= ?",
                    (channel, date, threshold),
                ).fetchall()
                return [r["username"] for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Economy Snapshots
    # ══════════════════════════════════════════════════════════

    async def write_snapshot(self, channel: str, data: dict) -> None:
        """Insert an economy snapshot row."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO economy_snapshots "
                    "(channel, total_accounts, total_z_circulation, active_economy_users_today, "
                    "z_earned_today, z_spent_today, z_gambled_net_today, median_balance, participation_rate) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        channel,
                        data.get("total_accounts", 0),
                        data.get("total_z_circulation", 0),
                        data.get("active_economy_users_today", 0),
                        data.get("z_earned_today", 0),
                        data.get("z_spent_today", 0),
                        data.get("z_gambled_net_today", 0),
                        data.get("median_balance", 0),
                        data.get("participation_rate", 0.0),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def get_latest_snapshot(self, channel: str) -> dict | None:
        """Get the most recent snapshot for a channel."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM economy_snapshots "
                    "WHERE channel = ? ORDER BY snapshot_time DESC LIMIT 1",
                    (channel,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_snapshot_history(self, channel: str, days: int = 7) -> list[dict]:
        """Get recent snapshots for trend analysis."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM economy_snapshots "
                    "WHERE channel = ? AND snapshot_time >= datetime('now', ?||' days') "
                    "ORDER BY snapshot_time ASC",
                    (channel, f"-{days}"),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Trigger Analytics Enhancements
    # ══════════════════════════════════════════════════════════

    async def increment_trigger_analytics(
        self, channel: str, trigger_id: str, date: str, z_awarded: int,
    ) -> None:
        """Upsert trigger analytics: increment hit_count and total_z_awarded."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO trigger_analytics (channel, trigger_id, date, hit_count, unique_users, total_z_awarded) "
                    "VALUES (?, ?, ?, 1, 1, ?) "
                    "ON CONFLICT(channel, trigger_id, date) DO UPDATE SET "
                    "hit_count = hit_count + 1, total_z_awarded = total_z_awarded + ?",
                    (channel, trigger_id, date, z_awarded, z_awarded),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def get_trigger_analytics(self, channel: str, date: str) -> list[dict]:
        """Get all trigger analytics for a date."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM trigger_analytics WHERE channel = ? AND date = ?",
                    (channel, date),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_trigger_analytics_range(
        self, channel: str, start_date: str, end_date: str,
    ) -> list[dict]:
        """Get trigger analytics across a date range."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM trigger_analytics "
                    "WHERE channel = ? AND date >= ? AND date <= ? "
                    "ORDER BY date, trigger_id",
                    (channel, start_date, end_date),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Ban Methods
    # ══════════════════════════════════════════════════════════

    async def ban_user(
        self, username: str, channel: str, banned_by: str, reason: str = "",
    ) -> bool:
        """Ban a user from the economy. Returns True if newly banned."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO banned_users (username, channel, banned_by, reason) "
                    "VALUES (?, ?, ?, ?)",
                    (username, channel, banned_by, reason),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def unban_user(self, username: str, channel: str) -> bool:
        """Remove economy ban. Returns True if was banned."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "DELETE FROM banned_users WHERE username = ? AND channel = ?",
                    (username, channel),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def is_banned(self, username: str, channel: str) -> bool:
        """Check if a user is banned from the economy."""
        loop = asyncio.get_running_loop()

        def _sync() -> bool:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT 1 FROM banned_users WHERE username = ? AND channel = ?",
                    (username, channel),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Aggregate Queries for Reporting
    # ══════════════════════════════════════════════════════════

    async def get_median_balance(self, channel: str) -> int:
        """Median balance across all accounts."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT balance FROM accounts WHERE channel = ? ORDER BY balance",
                    (channel,),
                ).fetchall()
                if not rows:
                    return 0
                n = len(rows)
                mid = n // 2
                if n % 2 == 0:
                    return (rows[mid - 1]["balance"] + rows[mid]["balance"]) // 2
                return rows[mid]["balance"]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_active_economy_users_today(self, channel: str, date: str) -> int:
        """Count users who earned or spent today."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM daily_activity "
                    "WHERE channel = ? AND date = ? AND (z_earned > 0 OR z_spent > 0)",
                    (channel, date),
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_daily_totals(self, channel: str, date: str) -> dict:
        """Get {z_earned, z_spent, z_gambled_in, z_gambled_out} for a date."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT "
                    "COALESCE(SUM(z_earned), 0) AS z_earned, "
                    "COALESCE(SUM(z_spent), 0) AS z_spent, "
                    "COALESCE(SUM(z_gambled_in), 0) AS z_gambled_in, "
                    "COALESCE(SUM(z_gambled_out), 0) AS z_gambled_out "
                    "FROM daily_activity WHERE channel = ? AND date = ?",
                    (channel, date),
                ).fetchone()
                return dict(row) if row else {
                    "z_earned": 0, "z_spent": 0, "z_gambled_in": 0, "z_gambled_out": 0,
                }
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_weekly_totals(
        self, channel: str, start_date: str, end_date: str,
    ) -> dict:
        """Aggregate totals across a week for admin digest."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT "
                    "COALESCE(SUM(z_earned), 0) AS z_earned, "
                    "COALESCE(SUM(z_spent), 0) AS z_spent, "
                    "COALESCE(SUM(z_gambled_in), 0) AS z_gambled_in, "
                    "COALESCE(SUM(z_gambled_out), 0) AS z_gambled_out "
                    "FROM daily_activity WHERE channel = ? AND date >= ? AND date <= ?",
                    (channel, start_date, end_date),
                ).fetchone()
                return dict(row) if row else {
                    "z_earned": 0, "z_spent": 0, "z_gambled_in": 0, "z_gambled_out": 0,
                }
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_top_earners_range(
        self, channel: str, start_date: str, end_date: str, limit: int = 5,
    ) -> list[dict]:
        """Top earners over a date range."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, SUM(z_earned) AS earned "
                    "FROM daily_activity WHERE channel = ? AND date >= ? AND date <= ? "
                    "GROUP BY username ORDER BY earned DESC LIMIT ?",
                    (channel, start_date, end_date, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_top_spenders_range(
        self, channel: str, start_date: str, end_date: str, limit: int = 5,
    ) -> list[dict]:
        """Top spenders over a date range."""
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT username, SUM(z_spent) AS spent "
                    "FROM daily_activity WHERE channel = ? AND date >= ? AND date <= ? "
                    "GROUP BY username ORDER BY spent DESC LIMIT ?",
                    (channel, start_date, end_date, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_gambling_summary_global(self, channel: str) -> dict:
        """Global gambling stats: total_in, total_out, active_gamblers, actual_house_edge."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT "
                    "COALESCE(SUM(lifetime_gambled_in), 0) AS total_in, "
                    "COALESCE(SUM(lifetime_gambled_out), 0) AS total_out, "
                    "COUNT(*) AS active_gamblers, "
                    "COALESCE(SUM(total_spins + total_flips + total_challenges + total_heists), 0) AS total_games "
                    "FROM gambling_stats gs "
                    "JOIN accounts a ON gs.username = a.username AND gs.channel = a.channel "
                    "WHERE gs.channel = ?",
                    (channel,),
                ).fetchone()
                return dict(row) if row else {
                    "total_in": 0, "total_out": 0, "active_gamblers": 0, "total_games": 0,
                }
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_all_accounts_count(self, channel: str) -> int:
        """Total number of accounts."""
        loop = asyncio.get_running_loop()

        def _sync() -> int:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM accounts WHERE channel = ?",
                    (channel,),
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    async def get_participation_rate(
        self, channel: str, total_channel_users: int,
    ) -> float:
        """Percentage of channel users who have economy accounts."""
        if total_channel_users <= 0:
            return 0.0
        count = await self.get_all_accounts_count(channel)
        return (count / total_channel_users) * 100

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Balance / Transaction Admin Helpers
    # ══════════════════════════════════════════════════════════

    async def set_balance(self, username: str, channel: str, amount: int) -> None:
        """Hard-set a user's balance."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET balance = ? WHERE username = ? AND channel = ?",
                    (amount, username, channel),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def log_transaction(
        self, username: str, channel: str, amount: int, *,
        tx_type: str = "admin", trigger_id: str = "", reason: str = "",
        metadata: str | None = None,
    ) -> None:
        """Insert a transaction log entry without modifying balance."""
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT INTO transactions (username, channel, amount, type, trigger_id, reason, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (username, channel, amount, tx_type, trigger_id, reason, metadata),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)

    async def get_pending_approval(
        self, username: str, channel: str, approval_type: str,
    ) -> dict | None:
        """Get a single pending approval for a user+type."""
        loop = asyncio.get_running_loop()

        def _sync() -> dict | None:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM pending_approvals "
                    "WHERE username = ? AND channel = ? AND type = ? AND status = 'pending' "
                    "ORDER BY id DESC LIMIT 1",
                    (username, channel, approval_type),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        return await loop.run_in_executor(None, _sync)

    # ── Sprint 9: Batch Presence Credit ──────────────────────

    async def batch_credit_presence(
        self, credits: list[tuple[str, str, int]],
    ) -> None:
        """Batch-credit presence Z in a single transaction.

        Args:
            credits: [(username, channel, amount), ...]
        """
        loop = asyncio.get_running_loop()

        def _sync() -> None:
            conn = self._get_connection()
            try:
                for username, channel, amount in credits:
                    conn.execute(
                        "UPDATE accounts SET balance = balance + ?, "
                        "lifetime_earned = lifetime_earned + ? "
                        "WHERE username = ? AND channel = ?",
                        (amount, amount, username, channel),
                    )
                    conn.execute(
                        "INSERT INTO transactions "
                        "(username, channel, amount, type, trigger_id, reason) "
                        "VALUES (?, ?, ?, 'presence', 'presence.base', 'Presence earning')",
                        (username, channel, amount),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        await loop.run_in_executor(None, _sync)