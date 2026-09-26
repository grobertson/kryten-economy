"""PostgreSQL-backed implementation of the economy persistence layer.

Implements the same surface as :class:`kryten_economy.database.EconomyDatabase`
(SQLite) using an ``asyncpg`` pool, so the orchestrator and every domain
component stay backend-agnostic. See ``db/protocol.py`` for the interface and
``db/boundary.py`` for the value-conversion contract.

Design notes
------------
**No blocking calls.** The SQLite store wraps synchronous work in
``run_in_executor``; this one awaits asyncpg directly, so there is no thread
pool and no blocking work in the event loop.

**Transactional currency mutations.** ``credit``/``debit``/``refund`` update the
account row and insert the ``transactions`` row inside a single
``con.transaction()``, so the ledger can never drift from the balance. ``debit``
uses a conditional ``UPDATE ... WHERE balance >= $n RETURNING balance`` so the
balance check and the write are one atomic statement — no read-modify-write race,
and concurrent debits cannot drive a balance negative.

**Boundary conversions.** asyncpg was measured (against the live server) to
require exact Python types: a ``boolean`` column rejects an ``int`` bind, and a
``timestamptz`` column rejects a ``str`` bind. It also *returns* ``datetime``,
``bool`` and ``date`` objects, which would break callers such as
``pm_handler._cmd_tip`` that call ``datetime.fromisoformat(...)`` on a raw
account row. Every bind therefore goes through ``boundary`` helpers, and every
returned row is normalised back to SQLite-compatible Python types.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg

from .boundary import (
    normalize_row,
    normalize_rows,
    to_bool,
    to_date,
    to_datetime,
)

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy

    # ``pool.acquire()`` yields a ``PoolConnectionProxy``, while a few call paths hold a bare
    # ``Connection``. The helpers below only ever use the shared ``execute`` surface, so they
    # are typed against this union rather than the narrower ``Connection`` — annotating the
    # latter would misdescribe every pooled call site. The alias is defined only for the type
    # checker: asyncpg's runtime ``Connection`` is not subscriptable, so evaluating the
    # subscript at import time would raise ``TypeError``.
    AcquirableConnection = asyncpg.Connection[Any] | PoolConnectionProxy[Any]


class _InsufficientFunds(Exception):
    """Internal signal: the conditional UPDATE matched no row.

    Raised inside ``async with con.transaction():`` purely so the transaction
    rolls back. It never escapes the store — :meth:`EconomyDatabasePg.atomic_debit`
    catches it and returns ``False``, matching the SQLite store's contract.
    """

    def __init__(self, username: str, channel: str, amount: int) -> None:
        super().__init__(f"{username}@{channel} cannot cover {amount}")
        self.username = username
        self.channel = channel
        self.amount = amount


# Column allowlists. The SQLite store interpolated these names into SQL after
# validating them against an inline set; the same guard is kept here so a bad
# caller value is logged and ignored rather than reaching the database. The
# identifiers themselves are fixed literals, never caller-supplied text.
_DAILY_COUNTER_COLUMNS: frozenset[str] = frozenset(
    {
        "minutes_present",
        "minutes_active",
        "messages_sent",
        "long_messages",
        "gifs_posted",
        "unique_emotes_used",
        "kudos_given",
        "kudos_received",
        "laughs_received",
        "bot_interactions",
        "queues_used",
    }
)

_DAILY_FLAG_COLUMNS: frozenset[str] = frozenset(
    {"first_message_claimed", "free_spin_used"}
)

_HOURLY_MILESTONE_HOURS: frozenset[int] = frozenset({1, 3, 6, 12, 24})

_GAMBLING_GAME_TYPES: frozenset[str] = frozenset(
    {"spin", "flip", "challenge", "heist", "race", "trivia", "blackjack"}
)

# ``daily_activity`` counters that daily competitions may rank on. Used by
# ``get_daily_top`` / ``get_daily_threshold_qualifiers``, which interpolate the
# identifier into SQL, so the allowlist is what keeps that safe.
_COMPETITION_FIELDS: frozenset[str] = frozenset(
    {
        "messages_sent",
        "long_messages",
        "gifs_posted",
        "unique_emotes_used",
        "kudos_given",
        "kudos_received",
        "laughs_received",
        "bot_interactions",
        "z_earned",
        "z_spent",
        "z_gambled_in",
        "z_gambled_out",
        "minutes_present",
        "minutes_active",
    }
)


def _affected_rows(status: str) -> int:
    """Parse the row count out of an asyncpg command status string.

    asyncpg returns e.g. ``'DELETE 3'``; this keeps the callers readable and
    mirrors the ``cursor.rowcount`` values the SQLite store returns.
    """
    parts = status.split()
    return int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else 0


class EconomyDatabasePg:
    """asyncpg-backed persistence for the economy microservice."""

    def __init__(
        self, pool: asyncpg.Pool, logger: logging.Logger | None = None
    ) -> None:
        self._pool = pool
        self._logger = logger or logging.getLogger("economy.database.pg")

    # ══════════════════════════════════════════════════════════
    #  Schema
    # ══════════════════════════════════════════════════════════

    async def initialize(self) -> None:
        """PostgreSQL schema is applied by Alembic, so there is nothing to do.

        The SQLite store creates its tables in-process. For PostgreSQL the schema
        is versioned and applied out-of-band by Alembic (Sprint 12 decision:
        Alembic is the single schema authority), so this method intentionally
        performs no DDL. It verifies connectivity instead, so a misconfigured
        database fails fast at startup rather than on the first query.
        """
        async with self._pool.acquire() as con:
            await con.execute("SELECT 1")
        self._logger.info(
            "PostgreSQL economy store connected (schema managed by Alembic)"
        )

    # ══════════════════════════════════════════════════════════
    #  Service Metrics
    # ══════════════════════════════════════════════════════════

    async def save_metrics(self, data: dict[str, int]) -> None:
        """Upsert lifetime counter key/value pairs."""
        if not data:
            return
        async with self._pool.acquire() as con:
            await con.executemany(
                """
                INSERT INTO service_metrics (key, value, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                [(k, int(v)) for k, v in data.items()],
            )

    async def restore_metrics(self) -> dict[str, int]:
        """Load all lifetime counters as a ``{key: value}`` dict."""
        async with self._pool.acquire() as con:
            rows = await con.fetch("SELECT key, value FROM service_metrics")
        return {row["key"]: int(row["value"]) for row in rows}

    # ══════════════════════════════════════════════════════════
    #  Accounts
    # ══════════════════════════════════════════════════════════

    async def _ensure_account(
        self, con: AcquirableConnection, username: str, channel: str
    ) -> None:
        """Create the account row if absent (mirrors ``INSERT OR IGNORE``)."""
        await con.execute(
            """
            INSERT INTO accounts (username, channel)
            VALUES ($1, $2)
            ON CONFLICT (username, channel) DO NOTHING
            """,
            username,
            channel,
        )

    async def get_or_create_account(self, username: str, channel: str) -> dict:
        """Return the account row, creating it with defaults if absent."""
        async with self._pool.acquire() as con:
            await self._ensure_account(con, username, channel)
            row = await con.fetchrow(
                "SELECT * FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return normalize_row("accounts", row) or {}

    async def get_account(self, username: str, channel: str) -> dict | None:
        """Return the account row, or ``None`` if it does not exist."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return normalize_row("accounts", row)

    async def get_balance(self, username: str, channel: str) -> int:
        """Return the balance, or ``0`` if the account does not exist."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT balance FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value) if value is not None else 0

    async def search_accounts(
        self,
        channel: str,
        pattern: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return channel accounts by balance, optionally filtered by username."""
        async with self._pool.acquire() as con:
            if pattern:
                rows = await con.fetch(
                    """
                    SELECT username, balance, lifetime_earned, rank_name
                    FROM accounts
                    WHERE channel = $1 AND username ILIKE $2
                    ORDER BY balance DESC
                    LIMIT $3
                    """,
                    channel,
                    f"%{pattern}%",
                    limit,
                )
            else:
                rows = await con.fetch(
                    """
                    SELECT username, balance, lifetime_earned, rank_name
                    FROM accounts
                    WHERE channel = $1
                    ORDER BY balance DESC
                    LIMIT $2
                    """,
                    channel,
                    limit,
                )
        return normalize_rows("accounts", rows)

    async def update_last_seen(self, username: str, channel: str) -> None:
        """Set ``last_seen`` to now."""
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE accounts SET last_seen = now() WHERE username = $1 AND channel = $2",
                username,
                channel,
            )

    async def update_last_active(self, username: str, channel: str) -> None:
        """Set ``last_active`` to now."""
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE accounts SET last_active = now() WHERE username = $1 AND channel = $2",
                username,
                channel,
            )

    # ══════════════════════════════════════════════════════════
    #  Currency mutations
    #
    #  These are the highest-stakes operations in the service: each one
    #  must apply the balance change and write its ledger row atomically.
    # ══════════════════════════════════════════════════════════

    async def _log_tx(
        self,
        con: AcquirableConnection,
        username: str,
        channel: str,
        amount: int,
        tx_type: str,
        reason: str | None,
        trigger_id: str | None,
        related_user: str | None,
        metadata: str | None,
    ) -> None:
        """Insert a ledger row on an existing connection/transaction."""
        await con.execute(
            """
            INSERT INTO transactions
                (username, channel, amount, type, reason, trigger_id, related_user, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            username,
            channel,
            amount,
            tx_type,
            reason,
            trigger_id,
            related_user,
            metadata,
        )

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
        """Credit Z and log the transaction atomically. Returns the new balance."""
        async with self._pool.acquire() as con, con.transaction():
            await self._ensure_account(con, username, channel)
            balance = await con.fetchval(
                """
                UPDATE accounts
                   SET balance = balance + $3,
                       lifetime_earned = lifetime_earned + $3
                 WHERE username = $1 AND channel = $2
             RETURNING balance
                """,
                username,
                channel,
                amount,
            )
            await self._log_tx(
                con,
                username,
                channel,
                amount,
                tx_type,
                reason,
                trigger_id,
                related_user,
                metadata,
            )
        return int(balance)

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
        """Debit Z and log the transaction atomically.

        Returns the new balance, or ``None`` when the funds are insufficient
        (or the account does not exist) — matching the SQLite contract that
        callers already branch on.

        The balance check and the write are a single conditional ``UPDATE``, so
        concurrent debits serialise on the row lock and cannot overdraw it or
        lose an update. Nothing is written when the guard fails.
        """
        async with self._pool.acquire() as con, con.transaction():
            balance = await con.fetchval(
                """
                UPDATE accounts
                   SET balance = balance - $3,
                       lifetime_spent = lifetime_spent + $3,
                       last_active = now()
                 WHERE username = $1 AND channel = $2 AND balance >= $3
             RETURNING balance
                """,
                username,
                channel,
                amount,
            )
            if balance is None:
                # Insufficient funds (or missing account): roll back and signal.
                return None
            await self._log_tx(
                con,
                username,
                channel,
                -amount,
                tx_type,
                reason,
                trigger_id,
                related_user,
                metadata,
            )
        return int(balance)

    async def refund(
        self,
        username: str,
        channel: str,
        amount: int,
        reason: str | None = None,
        trigger_id: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """Reverse a prior spend atomically and log a ``refund`` row.

        ``lifetime_spent`` is clamped at zero (SQLite used a scalar
        ``MAX(0, ...)``; PostgreSQL has no scalar ``MAX``, so this is
        ``GREATEST``) so a refund can never drive the counter negative.
        """
        async with self._pool.acquire() as con, con.transaction():
            await self._ensure_account(con, username, channel)
            balance = await con.fetchval(
                """
                UPDATE accounts
                   SET balance = balance + $3,
                       lifetime_spent = GREATEST(0, lifetime_spent - $3)
                 WHERE username = $1 AND channel = $2
             RETURNING balance
                """,
                username,
                channel,
                amount,
            )
            await self._log_tx(
                con,
                username,
                channel,
                amount,
                "refund",
                reason,
                trigger_id,
                related_user,
                metadata,
            )
        return int(balance)

    async def set_balance(self, username: str, channel: str, amount: int) -> None:
        """Hard-set an account balance (admin operation).

        Ensures the account row exists so setting a balance for a not-yet-seen
        user is recorded rather than silently discarded.
        """
        async with self._pool.acquire() as con:
            await self._ensure_account(con, username, channel)
            await con.execute(
                "UPDATE accounts SET balance = $3 WHERE username = $1 AND channel = $2",
                username,
                channel,
                amount,
            )

    async def log_transaction(
        self,
        username: str,
        channel: str,
        amount: int,
        tx_type: str,
        trigger_id: str | None = None,
        reason: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> None:
        """Append a ledger row without changing the balance."""
        async with self._pool.acquire() as con:
            await self._log_tx(
                con,
                username,
                channel,
                amount,
                tx_type,
                reason,
                trigger_id,
                related_user,
                metadata,
            )

    async def atomic_debit(
        self,
        username: str,
        channel: str,
        amount: int,
        tx_type: str = "wager",
        reason: str | None = None,
        trigger_id: str | None = None,
        metadata: str | None = None,
    ) -> bool:
        """Debit a wager atomically and write its ledger row.

        The conditional ``UPDATE`` (which takes the row lock and therefore
        serialises concurrent wagers) and the ``transactions`` insert share one
        transaction. A wager can never move the balance without leaving a ledger
        entry, and vice versa. Returns False (rolling back) when the balance is
        insufficient.
        """
        try:
            async with self._pool.acquire() as con, con.transaction():
                balance = await con.fetchval(
                    """
                    UPDATE accounts
                       SET balance = balance - $3,
                           lifetime_spent = lifetime_spent + $3
                     WHERE username = $1 AND channel = $2 AND balance >= $3
                 RETURNING balance
                    """,
                    username,
                    channel,
                    amount,
                )
                if balance is None:
                    # Nothing matched: the balance is short. Raise to roll back,
                    # then convert to the store's False contract below.
                    raise _InsufficientFunds(username, channel, amount)
                await self._log_tx(
                    con,
                    username,
                    channel,
                    -amount,
                    tx_type,
                    reason,
                    trigger_id,
                    None,
                    metadata,
                )
        except _InsufficientFunds:
            self._logger.debug(
                "atomic_debit declined: %s@%s cannot cover %s",
                username,
                channel,
                amount,
            )
            return False
        return True

    # ══════════════════════════════════════════════════════════
    #  Transactions
    # ══════════════════════════════════════════════════════════

    async def get_recent_transactions(
        self,
        username: str,
        channel: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return a user's transactions, newest first."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM transactions
                 WHERE username = $1 AND channel = $2
                 ORDER BY id DESC
                 LIMIT $3 OFFSET $4
                """,
                username,
                channel,
                limit,
                offset,
            )
        return normalize_rows("transactions", rows)

    async def get_recent_channel_transactions(
        self, channel: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return a channel's most recent transactions, newest first."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM transactions WHERE channel = $1 ORDER BY id DESC LIMIT $2",
                channel,
                limit,
            )
        return normalize_rows("transactions", rows)

    # ══════════════════════════════════════════════════════════
    #  Population
    # ══════════════════════════════════════════════════════════

    async def get_total_circulation(self, channel: str) -> int:
        """Total currency in circulation for a channel."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE channel = $1",
                channel,
            )
        return int(value or 0)

    async def get_account_count(self, channel: str) -> int:
        """Count of accounts in a channel."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COUNT(*) FROM accounts WHERE channel = $1", channel
            )
        return int(value or 0)

    async def get_all_accounts_count(self, channel: str) -> int:
        """Alias of :meth:`get_account_count` kept for reporting parity."""
        return await self.get_account_count(channel)

    # ══════════════════════════════════════════════════════════
    #  Onboarding
    # ══════════════════════════════════════════════════════════

    async def claim_welcome_wallet(
        self, username: str, channel: str, amount: int
    ) -> bool:
        """Credit the one-time welcome wallet. Returns False if already claimed."""
        async with self._pool.acquire() as con, con.transaction():
            balance = await con.fetchval(
                """
                UPDATE accounts
                   SET balance = balance + $3,
                       lifetime_earned = lifetime_earned + $3,
                       welcome_wallet_claimed = true
                 WHERE username = $1 AND channel = $2 AND welcome_wallet_claimed = false
             RETURNING balance
                """,
                username,
                channel,
                amount,
            )
            if balance is None:
                return False
            await con.execute(
                """
                INSERT INTO transactions (username, channel, amount, type, trigger_id)
                VALUES ($1, $2, $3, 'welcome_wallet', 'onboarding.wallet')
                """,
                username,
                channel,
                amount,
            )
        return True

    # ══════════════════════════════════════════════════════════
    #  Daily activity
    # ══════════════════════════════════════════════════════════

    async def get_daily_minutes_present(
        self, username: str, channel: str, date: str
    ) -> int:
        """Minutes present on a given day, or ``0``."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                """
                SELECT minutes_present FROM daily_activity
                 WHERE username = $1 AND channel = $2 AND date = $3
                """,
                username,
                channel,
                to_date(date),
            )
        return int(value or 0)

    async def increment_daily_minutes_present(
        self, username: str, channel: str, date: str, minutes: int = 1
    ) -> None:
        """Add dwell minutes for a day."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO daily_activity (username, channel, date, minutes_present)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (username, channel, date) DO UPDATE
                    SET minutes_present = daily_activity.minutes_present
                                          + EXCLUDED.minutes_present
                """,
                username,
                channel,
                to_date(date),
                minutes,
            )

    async def increment_daily_z_earned(
        self, username: str, channel: str, date: str, amount: int
    ) -> None:
        """Add to a day's ``z_earned`` total."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO daily_activity (username, channel, date, z_earned)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (username, channel, date) DO UPDATE
                    SET z_earned = daily_activity.z_earned + EXCLUDED.z_earned
                """,
                username,
                channel,
                to_date(date),
                amount,
            )

    async def _bump_daily(
        self,
        username: str,
        channel: str,
        date: str,
        column: str,
        *,
        increment: int | None = None,
        value: int | None = None,
    ) -> None:
        """Upsert a single ``daily_activity`` counter column.

        ``increment`` adds to the current value; ``value`` sets it outright.
        Exactly one of the two must be given.
        """
        if column not in _DAILY_COUNTER_COLUMNS:
            self._logger.warning("Invalid daily_activity column: %s", column)
            return
        if (increment is None) == (value is None):
            raise ValueError("pass exactly one of increment/value")

        if increment is not None:
            # PostgreSQL forbids referencing the target column unqualified in
            # ON CONFLICT DO UPDATE, so it is qualified with the table name.
            new_value = f"daily_activity.{column} + EXCLUDED.{column}"
        else:
            new_value = f"EXCLUDED.{column}"

        sql = f"""
            INSERT INTO daily_activity (username, channel, date, {column})
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (username, channel, date) DO UPDATE
                SET {column} = {new_value}
        """
        payload = increment if increment is not None else value
        async with self._pool.acquire() as con:
            await con.execute(sql, username, channel, to_date(date), payload)

    async def mark_first_message_claimed(
        self, username: str, channel: str, date: str
    ) -> None:
        """Record that the user claimed the first-message-of-day bonus."""
        await self._set_daily_flag(username, channel, date, "first_message_claimed")

    async def increment_daily_messages_sent(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "messages_sent", increment=1)

    async def increment_daily_long_messages(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "long_messages", increment=1)

    async def increment_daily_gifs_posted(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "gifs_posted", increment=1)

    async def increment_daily_kudos_given(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "kudos_given", increment=1)

    async def increment_daily_kudos_received(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "kudos_received", increment=1)

    async def increment_daily_laughs_received(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "laughs_received", increment=1)

    async def increment_daily_bot_interactions(
        self, username: str, channel: str, date: str
    ) -> None:
        await self._bump_daily(username, channel, date, "bot_interactions", increment=1)

    async def set_daily_unique_emotes(
        self, username: str, channel: str, date: str, count: int
    ) -> None:
        """Set the unique-emote counter outright."""
        await self._bump_daily(
            username, channel, date, "unique_emotes_used", value=count
        )

    async def _set_daily_flag(
        self, username: str, channel: str, date: str, column: str
    ) -> None:
        """Set a boolean ``daily_activity`` column to true."""
        if column not in _DAILY_FLAG_COLUMNS:
            self._logger.warning("Invalid daily_activity flag: %s", column)
            return
        sql = f"""
            INSERT INTO daily_activity (username, channel, date, {column})
            VALUES ($1, $2, $3, true)
            ON CONFLICT (username, channel, date) DO UPDATE
                SET {column} = true
        """
        async with self._pool.acquire() as con:
            await con.execute(sql, username, channel, to_date(date))

    async def mark_free_spin_used(self, username: str, channel: str, date: str) -> None:
        """Consume the daily free spin."""
        await self._set_daily_flag(username, channel, date, "free_spin_used")

    async def increment_daily_gambled(
        self,
        username: str,
        channel: str,
        date: str,
        wagered: int,
        payout: int,
    ) -> None:
        """Add wager/payout to a day's gambling totals."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO daily_activity
                    (username, channel, date, z_gambled_in, z_gambled_out)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (username, channel, date) DO UPDATE
                    SET z_gambled_in = daily_activity.z_gambled_in + EXCLUDED.z_gambled_in,
                        z_gambled_out = daily_activity.z_gambled_out + EXCLUDED.z_gambled_out
                """,
                username,
                channel,
                to_date(date),
                wagered,
                payout,
            )

    async def get_or_create_daily_activity(
        self, username: str, channel: str, date: str
    ) -> dict:
        """Return the day's activity row, creating it with defaults if absent."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO daily_activity (username, channel, date)
                VALUES ($1, $2, $3)
                ON CONFLICT (username, channel, date) DO NOTHING
                """,
                username,
                channel,
                to_date(date),
            )
            row = await con.fetchrow(
                "SELECT * FROM daily_activity WHERE username = $1 AND channel = $2 AND date = $3",
                username,
                channel,
                to_date(date),
            )
        return normalize_row("daily_activity", row) or {}

    async def get_daily_activity_all(
        self, channel: str, date: str
    ) -> list[dict[str, Any]]:
        """Return every account's activity for a given day."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM daily_activity
                 WHERE channel = $1 AND date = $2
                 ORDER BY z_earned DESC
                """,
                channel,
                to_date(date),
            )
        return normalize_rows("daily_activity", rows)

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Streaks & milestones
    # ══════════════════════════════════════════════════════════

    async def get_or_create_streak(self, username: str, channel: str) -> dict:
        """Return the streak row, creating it with defaults if absent."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO streaks (username, channel)
                VALUES ($1, $2)
                ON CONFLICT (username, channel) DO NOTHING
                """,
                username,
                channel,
            )
            row = await con.fetchrow(
                "SELECT * FROM streaks WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return normalize_row("streaks", row) or {}

    async def update_streak(
        self,
        username: str,
        channel: str,
        current_streak: int,
        longest_streak: int,
        last_date: str,
    ) -> None:
        """Update streak counters.

        The row is created first when missing, so this behaves like the SQLite
        version (a bare ``UPDATE`` there silently no-ops on a missing row, which
        callers did not rely on and which reads as a lost update here).
        """
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO streaks (username, channel)
                VALUES ($1, $2)
                ON CONFLICT (username, channel) DO NOTHING
                """,
                username,
                channel,
            )
            await con.execute(
                """
                UPDATE streaks
                   SET current_daily_streak = $3,
                       longest_daily_streak = $4,
                       last_streak_date = $5
                 WHERE username = $1 AND channel = $2
                """,
                username,
                channel,
                current_streak,
                longest_streak,
                to_date(last_date),
            )

    async def update_bridge_fields(
        self,
        username: str,
        channel: str,
        weekend_seen: bool | None = None,
        weekday_seen: bool | None = None,
        bridge_claimed: bool | None = None,
        week_number: str | None = None,
    ) -> None:
        """Update weekend/weekday bridge tracking fields (partial update)."""
        # Identifiers are fixed literals; only values are bound, so this is not
        # a SQL-injection surface. Each entry is (column, value-or-None).
        fields: list[tuple[str, Any]] = [
            (
                "weekend_seen_this_week",
                to_bool(weekend_seen) if weekend_seen is not None else None,
            ),
            (
                "weekday_seen_this_week",
                to_bool(weekday_seen) if weekday_seen is not None else None,
            ),
            (
                "bridge_claimed_this_week",
                to_bool(bridge_claimed) if bridge_claimed is not None else None,
            ),
            ("week_number", week_number),
        ]
        updates = [(col, val) for col, val in fields if val is not None]
        if not updates:
            return

        assignments = ", ".join(
            f"{col} = ${idx}" for idx, (col, _v) in enumerate(updates, start=3)
        )
        params: list[Any] = [username, channel]
        params.extend(val for _col, val in updates)

        async with self._pool.acquire() as con:
            await con.execute(
                f"UPDATE streaks SET {assignments} WHERE username = $1 AND channel = $2",
                *params,
            )

    async def get_or_create_hourly_milestones(
        self, username: str, channel: str, date: str
    ) -> dict:
        """Return the hourly-milestone row for a day, creating it if absent."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO hourly_milestones (username, channel, date)
                VALUES ($1, $2, $3)
                ON CONFLICT (username, channel, date) DO NOTHING
                """,
                username,
                channel,
                to_date(date),
            )
            row = await con.fetchrow(
                "SELECT * FROM hourly_milestones WHERE username = $1 AND channel = $2 AND date = $3",
                username,
                channel,
                to_date(date),
            )
        return normalize_row("hourly_milestones", row) or {}

    async def mark_hourly_milestone(
        self, username: str, channel: str, date: str, hours: int
    ) -> None:
        """Mark an hourly dwell milestone as reached."""
        if hours not in _HOURLY_MILESTONE_HOURS:
            self._logger.warning("Invalid milestone column: hours_%s", hours)
            return
        column = f"hours_{hours}"
        async with self._pool.acquire() as con:
            await con.execute(
                f"""
                INSERT INTO hourly_milestones (username, channel, date, {column})
                VALUES ($1, $2, $3, true)
                ON CONFLICT (username, channel, date) DO UPDATE SET {column} = true
                """,
                username,
                channel,
                to_date(date),
            )

    # ══════════════════════════════════════════════════════════
    #  Sprint 3: Trigger cooldowns & analytics
    # ══════════════════════════════════════════════════════════

    async def get_trigger_cooldown(
        self, username: str, channel: str, trigger_id: str
    ) -> dict | None:
        """Return a trigger cooldown row, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT * FROM trigger_cooldowns
                 WHERE username = $1 AND channel = $2 AND trigger_id = $3
                """,
                username,
                channel,
                trigger_id,
            )
        return normalize_row("trigger_cooldowns", row)

    async def set_trigger_cooldown(
        self,
        username: str,
        channel: str,
        trigger_id: str,
        count: int,
        window_start: Any,
    ) -> None:
        """Create or replace a cooldown entry."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO trigger_cooldowns
                    (username, channel, trigger_id, count, window_start)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (username, channel, trigger_id) DO UPDATE
                    SET count = EXCLUDED.count, window_start = EXCLUDED.window_start
                """,
                username,
                channel,
                trigger_id,
                count,
                to_datetime(window_start),
            )

    async def increment_trigger_cooldown(
        self, username: str, channel: str, trigger_id: str
    ) -> None:
        """Increment a cooldown counter by one."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                UPDATE trigger_cooldowns SET count = count + 1
                 WHERE username = $1 AND channel = $2 AND trigger_id = $3
                """,
                username,
                channel,
                trigger_id,
            )

    async def record_trigger_analytics(
        self, channel: str, trigger_id: str, date: str, z_awarded: int
    ) -> None:
        """Increment a trigger's daily hit count and awarded total."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO trigger_analytics
                    (channel, trigger_id, date, hit_count, unique_users, total_z_awarded)
                VALUES ($1, $2, $3, 1, 1, $4)
                ON CONFLICT (channel, trigger_id, date) DO UPDATE
                    SET hit_count = trigger_analytics.hit_count + 1,
                        total_z_awarded = trigger_analytics.total_z_awarded + EXCLUDED.total_z_awarded
                """,
                channel,
                trigger_id,
                to_date(date),
                z_awarded,
            )

    async def increment_trigger_analytics(
        self, channel: str, trigger_id: str, date: str, z_awarded: int
    ) -> None:
        """Increment a trigger's daily hit count and awarded total.

        Mirrors :meth:`record_trigger_analytics` (same signature and effect);
        the SQLite store exposes both names for the same upsert.
        """
        await self.record_trigger_analytics(channel, trigger_id, date, z_awarded)

    async def get_trigger_analytics(
        self, channel: str, date: str
    ) -> list[dict[str, Any]]:
        """Return a channel's trigger analytics for one day."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM trigger_analytics WHERE channel = $1 AND date = $2 ORDER BY trigger_id",
                channel,
                to_date(date),
            )
        return normalize_rows("trigger_analytics", rows)

    async def get_trigger_analytics_range(
        self, channel: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Return a channel's trigger analytics across an inclusive date range."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM trigger_analytics
                 WHERE channel = $1 AND date >= $2 AND date <= $3
                 ORDER BY trigger_id, date
                """,
                channel,
                to_date(start_date),
                to_date(end_date),
            )
        return normalize_rows("trigger_analytics", rows)

    # ══════════════════════════════════════════════════════════
    #  Sprint 4: Gambling
    # ══════════════════════════════════════════════════════════

    async def update_gambling_stats(
        self,
        username: str,
        channel: str,
        game_type: str,
        net: int,
        biggest_win: int = 0,
        biggest_loss: int = 0,
    ) -> None:
        """Upsert a gambling outcome for a user."""
        if game_type not in _GAMBLING_GAME_TYPES:
            self._logger.warning("Invalid gambling stat column: total_%ss", game_type)
            return
        column = f"total_{game_type}s"
        async with self._pool.acquire() as con:
            await con.execute(
                f"""
                INSERT INTO gambling_stats
                    (username, channel, {column}, biggest_win, biggest_loss, net_gambling)
                VALUES ($1, $2, 1, $3, $4, $5)
                ON CONFLICT (username, channel) DO UPDATE
                    SET {column} = gambling_stats.{column} + 1,
                        biggest_win = GREATEST(gambling_stats.biggest_win, EXCLUDED.biggest_win),
                        biggest_loss = GREATEST(gambling_stats.biggest_loss, EXCLUDED.biggest_loss),
                        net_gambling = gambling_stats.net_gambling + EXCLUDED.net_gambling
                """,
                username,
                channel,
                biggest_win,
                biggest_loss,
                net,
            )

    async def get_gambling_stats(self, username: str, channel: str) -> dict | None:
        """Return a user's gambling stats, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM gambling_stats WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return normalize_row("gambling_stats", row)

    async def get_gambling_summary(self, username: str, channel: str) -> dict | None:
        """Return aggregate gambling numbers for a user, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT total_spins + total_flips + total_challenges + total_heists AS total_games,
                       net_gambling AS net_profit
                  FROM gambling_stats
                 WHERE username = $1 AND channel = $2
                """,
                username,
                channel,
            )
        return normalize_row("gambling_stats", row)

    async def get_gambling_summary_global(self, channel: str) -> dict:
        """Return channel-wide gambling totals."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT COALESCE(SUM(lifetime_gambled_in), 0) AS total_in,
                       COALESCE(SUM(lifetime_gambled_out), 0) AS total_out,
                       COALESCE(SUM(net_gambling), 0) AS net_gambling,
                       COALESCE(SUM(total_spins + total_flips + total_challenges + total_heists), 0)
                           AS total_games
                  FROM gambling_stats
                 WHERE channel = $1
                """,
                channel,
            )
        return normalize_row("gambling_stats", row) or {}

    async def increment_lifetime_gambled(
        self, username: str, channel: str, wagered: int, payout: int
    ) -> None:
        """Add to lifetime gambling wagered/paid-out totals.

        Ensures the account row exists first: a bare ``UPDATE`` would otherwise
        silently drop the amounts for an account that has no row yet.
        """
        async with self._pool.acquire() as con:
            await self._ensure_account(con, username, channel)
            await con.execute(
                """
                UPDATE accounts
                   SET lifetime_gambled_in = lifetime_gambled_in + $3,
                       lifetime_gambled_out = lifetime_gambled_out + $4
                 WHERE username = $1 AND channel = $2
                """,
                username,
                channel,
                wagered,
                payout,
            )

    async def get_lifetime_gambled(self, username: str, channel: str) -> int:
        """Total wagered amount across the account's lifetime."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COALESCE(lifetime_gambled_in, 0) FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    async def get_biggest_gambling_win(self, username: str, channel: str) -> int:
        """Largest single gambling win, or ``0``."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COALESCE(biggest_win, 0) FROM gambling_stats WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    # ══════════════════════════════════════════════════════════
    #  Sprint 4: Races, trivia, blackjack, challenges
    # ══════════════════════════════════════════════════════════

    async def save_race_result(
        self,
        race_id: str,
        channel: str,
        winner_color: str,
        total_pool: int,
        participants: int,
    ) -> None:
        """Persist a finished race result."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO race_results
                    (race_id, channel, winner_color, total_pool, participants)
                VALUES ($1, $2, $3, $4, $5)
                """,
                race_id,
                channel,
                winner_color,
                total_pool,
                participants,
            )

    async def save_race_bet(
        self,
        race_id: str,
        username: str,
        channel: str,
        color: str,
        amount: int,
        payout: int,
        phase: str,
    ) -> None:
        """Persist a bet placed on a race."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO race_bets (race_id, username, channel, color, amount, payout, phase)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                race_id,
                username,
                channel,
                color,
                amount,
                payout,
                phase,
            )

    async def get_race_stats(self, username: str, channel: str) -> dict:
        """Aggregate a user's race betting stats (never ``None``)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT COUNT(*) AS races_bet,
                       COALESCE(SUM(amount), 0) AS total_wagered,
                       COALESCE(SUM(payout), 0) AS total_won,
                       COALESCE(MAX(payout), 0) AS biggest_win
                  FROM race_bets
                 WHERE username = $1 AND channel = $2
                """,
                username,
                channel,
            )
        return normalize_row("race_bets", row) or {
            "races_bet": 0,
            "total_wagered": 0,
            "total_won": 0,
            "biggest_win": 0,
        }

    async def update_trivia_stats(
        self,
        username: str,
        channel: str,
        *,
        correct: bool,
        wagered: int,
        won: int,
    ) -> None:
        """Update trivia stats for a correct or incorrect answer."""
        async with self._pool.acquire() as con:
            if correct:
                await con.execute(
                    """
                    INSERT INTO trivia_stats
                        (username, channel, correct, streak, best_streak, total_wagered, total_won)
                    VALUES ($1, $2, 1, 1, 1, $3, $4)
                    ON CONFLICT (username, channel) DO UPDATE
                        SET correct = trivia_stats.correct + 1,
                            streak = trivia_stats.streak + 1,
                            best_streak = GREATEST(
                                trivia_stats.best_streak, trivia_stats.streak + 1),
                            total_wagered = trivia_stats.total_wagered + EXCLUDED.total_wagered,
                            total_won = trivia_stats.total_won + EXCLUDED.total_won
                    """,
                    username,
                    channel,
                    wagered,
                    won,
                )
            else:
                await con.execute(
                    """
                    INSERT INTO trivia_stats (username, channel, incorrect, total_wagered)
                    VALUES ($1, $2, 1, $3)
                    ON CONFLICT (username, channel) DO UPDATE
                        SET incorrect = trivia_stats.incorrect + 1,
                            streak = 0,
                            total_wagered = trivia_stats.total_wagered + EXCLUDED.total_wagered
                    """,
                    username,
                    channel,
                    wagered,
                )

    async def get_trivia_stats(self, username: str, channel: str) -> dict | None:
        """Return trivia stats, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM trivia_stats WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return normalize_row("trivia_stats", row)

    async def update_blackjack_stats(
        self,
        username: str,
        channel: str,
        *,
        outcome: str,
        wagered: int,
        won: int,
    ) -> None:
        """Update blackjack stats. ``outcome`` is win/loss/push/blackjack."""
        increments = {
            "win": (1, 0, 0, 0),
            "loss": (0, 1, 0, 0),
            "push": (0, 0, 1, 0),
            "blackjack": (1, 0, 0, 1),
        }
        if outcome not in increments:
            self._logger.warning("Unknown blackjack outcome: %s", outcome)
            return
        wins, losses, pushes, blackjacks = increments[outcome]

        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO blackjack_stats
                    (username, channel, games_played, wins, losses, pushes,
                     blackjacks, total_wagered, total_won)
                VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (username, channel) DO UPDATE
                    SET games_played = blackjack_stats.games_played + 1,
                        wins = blackjack_stats.wins + EXCLUDED.wins,
                        losses = blackjack_stats.losses + EXCLUDED.losses,
                        pushes = blackjack_stats.pushes + EXCLUDED.pushes,
                        blackjacks = blackjack_stats.blackjacks + EXCLUDED.blackjacks,
                        total_wagered = blackjack_stats.total_wagered + EXCLUDED.total_wagered,
                        total_won = blackjack_stats.total_won + EXCLUDED.total_won
                """,
                username,
                channel,
                wins,
                losses,
                pushes,
                blackjacks,
                wagered,
                won,
            )

    async def get_blackjack_stats(self, username: str, channel: str) -> dict | None:
        """Return blackjack stats, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM blackjack_stats WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return normalize_row("blackjack_stats", row)

    async def create_challenge(
        self,
        challenger: str,
        target: str,
        channel: str,
        wager: int,
        expires_at: Any,
    ) -> int:
        """Insert a pending challenge. Returns the challenge id."""
        async with self._pool.acquire() as con:
            challenge_id = await con.fetchval(
                """
                INSERT INTO pending_challenges (challenger, target, channel, wager, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                challenger,
                target,
                channel,
                wager,
                to_datetime(expires_at),
            )
        return int(challenge_id)

    async def get_pending_challenge(
        self, challenger: str, target: str, channel: str
    ) -> dict | None:
        """Return the newest pending challenge between two users, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT * FROM pending_challenges
                 WHERE challenger = $1 AND target = $2 AND channel = $3 AND status = 'pending'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                challenger,
                target,
                channel,
            )
        return normalize_row("pending_challenges", row)

    async def get_pending_challenge_for_target(
        self, target: str, channel: str
    ) -> dict | None:
        """Return the newest pending challenge aimed at a user, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT * FROM pending_challenges
                 WHERE target = $1 AND channel = $2 AND status = 'pending'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                target,
                channel,
            )
        return normalize_row("pending_challenges", row)

    async def resolve_challenge(self, challenge_id: int, status: str) -> None:
        """Set a challenge's status (accepted/declined/expired)."""
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE pending_challenges SET status = $2 WHERE id = $1",
                challenge_id,
                status,
            )

    async def expire_old_challenges(self) -> list[dict[str, Any]]:
        """Expire past-due pending challenges. Returns the rows that expired."""
        async with self._pool.acquire() as con, con.transaction():
            rows = await con.fetch(
                "SELECT * FROM pending_challenges WHERE status = 'pending' AND expires_at < now()"
            )
            if rows:
                await con.execute(
                    "UPDATE pending_challenges SET status = 'expired' "
                    "WHERE status = 'pending' AND expires_at < now()"
                )
        return normalize_rows("pending_challenges", rows)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Tips
    # ══════════════════════════════════════════════════════════

    async def record_tip(
        self, sender: str, receiver: str, channel: str, amount: int
    ) -> None:
        """Record a tip in the history table."""
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO tip_history (sender, receiver, channel, amount) VALUES ($1, $2, $3, $4)",
                sender,
                receiver,
                channel,
                amount,
            )

    async def get_tips_sent_today(self, username: str, channel: str) -> int:
        """Total value of tips sent by a user today (UTC)."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                """
                SELECT COALESCE(SUM(amount), 0) FROM tip_history
                 WHERE sender = $1 AND channel = $2
                   AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                """,
                username,
                channel,
            )
        return int(value or 0)

    async def get_tip_count_today(self, username: str, channel: str) -> int:
        """Number of tips sent by a user today (UTC)."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                """
                SELECT COUNT(*) FROM tip_history
                 WHERE sender = $1 AND channel = $2
                   AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                """,
                username,
                channel,
            )
        return int(value or 0)

    async def get_unique_tip_recipients(self, username: str, channel: str) -> int:
        """Number of distinct users a user has ever tipped."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COUNT(DISTINCT receiver) FROM tip_history WHERE sender = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    async def get_unique_tip_senders(self, username: str, channel: str) -> int:
        """Number of distinct users who have ever tipped this user."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COUNT(DISTINCT sender) FROM tip_history WHERE receiver = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Queue spend idempotency
    # ══════════════════════════════════════════════════════════

    async def insert_queue_spend_request(
        self,
        request_id: str,
        username: str,
        channel: str,
        cost_z: int,
        tier: str,
        transaction_id: int | None = None,
    ) -> bool:
        """Record a spend attempt. ``False`` if the id was already recorded.

        This is the queue-spend idempotency guard: a duplicate request id must
        not double-charge the user, so a losing ``INSERT`` returns ``False``
        rather than raising.
        """
        async with self._pool.acquire() as con:
            status = await con.execute(
                """
                INSERT INTO queue_spend_requests
                    (request_id, username, channel, cost_z, tier, transaction_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                username,
                channel,
                cost_z,
                tier,
                transaction_id,
            )
        return status.endswith(" 1")

    async def get_queue_spend_request(self, request_id: str) -> dict | None:
        """Return a recorded spend request, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM queue_spend_requests WHERE request_id = $1", request_id
            )
        return normalize_row("queue_spend_requests", row)

    async def mark_queue_spend_refunded(self, request_id: str) -> None:
        """Mark a spend request refunded."""
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE queue_spend_requests SET refunded = true, refunded_at = now() "
                "WHERE request_id = $1",
                request_id,
            )

    async def increment_daily_queues_used(
        self, username: str, channel: str, date: str
    ) -> None:
        """Count one queue submission against the user's daily allowance."""
        await self._bump_daily(username, channel, date, "queues_used", increment=1)

    async def decrement_daily_queues_used(
        self, username: str, channel: str, date: str
    ) -> None:
        """Give back one queue slot (used when refunding a failed attempt).

        Clamped at zero, and only applied to an existing row.
        """
        async with self._pool.acquire() as con:
            await con.execute(
                """
                UPDATE daily_activity
                   SET queues_used = GREATEST(0, queues_used - 1)
                 WHERE username = $1 AND channel = $2 AND date = $3
                """,
                username,
                channel,
                to_date(date),
            )

    async def get_queues_today(self, username: str, channel: str) -> int:
        """Number of queue submissions made today (UTC)."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                """
                SELECT COALESCE(
                    (SELECT queues_used FROM daily_activity
                      WHERE username = $1 AND channel = $2
                        AND date = (now() AT TIME ZONE 'UTC')::date),
                    0)
                """,
                username,
                channel,
            )
        return int(value or 0)

    async def get_last_queue_time(self, username: str, channel: str) -> datetime | None:
        """Return the last *valid* (non-refunded) queue spend time, or ``None``.

        PM-handler queue submissions use ``trigger_id = 'spend.queue'`` and are
        always valid. NATS/web submissions use ``spend.queue.<request_id>`` and
        are only valid while their idempotency row is absent or not refunded.
        Rows are scanned newest-first and the first valid one wins.
        """
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT t.created_at, t.trigger_id
                  FROM transactions t
                 WHERE t.username = $1 AND t.channel = $2
                   AND t.trigger_id LIKE 'spend.queue%'
                 ORDER BY t.id DESC
                """,
                username,
                channel,
            )
            for row in rows:
                trigger_id = row["trigger_id"]
                if trigger_id == "spend.queue":
                    return row["created_at"]
                if trigger_id.startswith("spend.queue."):
                    request_id = trigger_id[len("spend.queue.") :]
                    refund = await con.fetchval(
                        "SELECT refunded FROM queue_spend_requests WHERE request_id = $1",
                        request_id,
                    )
                    if refund is None or not refund:
                        return row["created_at"]
        return None

    # ══════════════════════════════════════════════════════════
    #  Sprint 6: Achievements
    # ══════════════════════════════════════════════════════════

    async def has_achievement(
        self, username: str, channel: str, achievement_id: str
    ) -> bool:
        """Whether a user already holds an achievement."""
        async with self._pool.acquire() as con:
            exists = await con.fetchval(
                "SELECT 1 FROM achievements WHERE username = $1 AND channel = $2 AND achievement_id = $3",
                username,
                channel,
                achievement_id,
            )
        return exists is not None

    async def award_achievement(
        self, username: str, channel: str, achievement_id: str
    ) -> bool:
        """Award an achievement. ``False`` if already held (no duplicate)."""
        async with self._pool.acquire() as con:
            status = await con.execute(
                """
                INSERT INTO achievements (username, channel, achievement_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (username, channel, achievement_id) DO NOTHING
                """,
                username,
                channel,
                achievement_id,
            )
        return status.endswith(" 1")

    async def get_user_achievements(
        self, username: str, channel: str
    ) -> list[dict[str, Any]]:
        """Return a user's achievements, newest first."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM achievements WHERE username = $1 AND channel = $2 ORDER BY id DESC",
                username,
                channel,
            )
        return normalize_rows("achievements", rows)

    async def get_achievement_count(self, username: str, channel: str) -> int:
        """Number of achievements a user holds."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COUNT(*) FROM achievements WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Vanity items
    #
    #  Usernames are stored with canonical CyTube casing (so the case-sensitive
    #  CSS selector ``.chat-msg-<User>`` renders) while lookups are
    #  case-insensitive. SQLite achieved the latter with ``COLLATE NOCASE``;
    #  here it is an explicit ``lower()`` comparison.
    # ══════════════════════════════════════════════════════════

    async def set_vanity_item(
        self, username: str, channel: str, item_type: str, value: str
    ) -> None:
        """Upsert a vanity item, matching any existing row case-insensitively.

        A plain ``ON CONFLICT`` would create a second row when the stored
        casing differs from the caller's, which is what previously made
        chat-color changes silently no-op while still charging the user. So an
        existing case-insensitive match is updated in place and a row is
        inserted only when the user has none yet.

        The stored username is the **canonical** casing held on ``accounts``
        (falling back to the caller's string when no account row exists).
        Storing whatever casing the caller happened to use would break the
        case-sensitive CyTube CSS selector ``.chat-msg-<User>`` on a later
        differently-cased purchase. The SQLite store shares this rule now.
        """
        async with self._pool.acquire() as con, con.transaction():
            existing = await con.fetchval(
                """
                SELECT id FROM vanity_items
                 WHERE lower(username) = lower($1) AND channel = $2 AND item_type = $3
                 ORDER BY purchased_at DESC, id DESC
                 LIMIT 1
                """,
                username,
                channel,
                item_type,
            )
            canonical = await con.fetchval(
                "SELECT username FROM accounts WHERE lower(username) = lower($1) AND channel = $2 "
                "LIMIT 1",
                username,
                channel,
            )
            stored_username = canonical if canonical is not None else username

            if existing is not None:
                await con.execute(
                    """
                    UPDATE vanity_items
                       SET username = $2, value = $3, active = true, purchased_at = now()
                     WHERE id = $1
                    """,
                    existing,
                    stored_username,
                    value,
                )
            else:
                await con.execute(
                    """
                    INSERT INTO vanity_items (username, channel, item_type, value)
                    VALUES ($1, $2, $3, $4)
                    """,
                    stored_username,
                    channel,
                    item_type,
                    value,
                )

    async def deactivate_vanity_item(
        self, username: str, channel: str, item_type: str
    ) -> None:
        """Deactivate a vanity item (used to roll back a failed purchase)."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                UPDATE vanity_items SET active = false
                 WHERE lower(username) = lower($1) AND channel = $2 AND item_type = $3
                """,
                username,
                channel,
                item_type,
            )

    async def get_vanity_item(
        self, username: str, channel: str, item_type: str
    ) -> str | None:
        """Return an active vanity value (case-insensitive identity), or ``None``."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                """
                SELECT value FROM vanity_items
                 WHERE lower(username) = lower($1) AND channel = $2
                   AND item_type = $3 AND active = true
                """,
                username,
                channel,
                item_type,
            )
        return value

    async def get_custom_greeting(self, username: str, channel: str) -> str | None:
        """Return the user's custom greeting, or ``None``."""
        return await self.get_vanity_item(username, channel, "custom_greeting")

    async def get_all_vanity_items(self, username: str, channel: str) -> dict[str, str]:
        """Return all active vanity items as ``{item_type: value}``."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT item_type, value FROM vanity_items
                 WHERE lower(username) = lower($1) AND channel = $2 AND active = true
                """,
                username,
                channel,
            )
        return {row["item_type"]: row["value"] for row in rows}

    async def get_users_with_custom_greetings(self, channel: str) -> dict[str, str]:
        """Return ``{username: greeting}`` for all users with active greetings."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT username, value FROM vanity_items
                 WHERE channel = $1 AND item_type = 'custom_greeting' AND active = true
                """,
                channel,
            )
        return {row["username"]: row["value"] for row in rows}

    async def get_users_with_chat_colors(self, channel: str) -> dict[str, str]:
        """Return ``{username: hex_color}`` for all users with an active chat color.

        Usernames are stored with canonical casing so case-sensitive CSS
        selectors render.
        """
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT username, value FROM vanity_items
                 WHERE channel = $1 AND item_type = 'chat_color' AND active = true
                """,
                channel,
            )
        return {row["username"]: row["value"] for row in rows}

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Approvals
    # ══════════════════════════════════════════════════════════

    async def create_pending_approval(
        self,
        username: str,
        channel: str,
        approval_type: str,
        data: dict | str,
        cost: int,
    ) -> int:
        """Insert a pending approval. Returns the approval id."""
        payload = json.dumps(data) if isinstance(data, dict) else data
        async with self._pool.acquire() as con:
            approval_id = await con.fetchval(
                """
                INSERT INTO pending_approvals (username, channel, type, data, cost)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                username,
                channel,
                approval_type,
                payload,
                cost,
            )
        return int(approval_id)

    async def get_pending_approval(
        self, username: str, channel: str, approval_type: str
    ) -> dict | None:
        """Return a user's newest pending approval of a type, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT * FROM pending_approvals
                 WHERE username = $1 AND channel = $2 AND type = $3 AND status = 'pending'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                username,
                channel,
                approval_type,
            )
        return normalize_row("pending_approvals", row)

    async def get_pending_approvals(
        self, channel: str, approval_type: str | None = None
    ) -> list[dict[str, Any]]:
        """List pending approvals, optionally filtered by type."""
        async with self._pool.acquire() as con:
            if approval_type:
                rows = await con.fetch(
                    """
                    SELECT * FROM pending_approvals
                     WHERE channel = $1 AND status = 'pending' AND type = $2
                     ORDER BY id DESC
                    """,
                    channel,
                    approval_type,
                )
            else:
                rows = await con.fetch(
                    """
                    SELECT * FROM pending_approvals
                     WHERE channel = $1 AND status = 'pending'
                     ORDER BY id DESC
                    """,
                    channel,
                )
        return normalize_rows("pending_approvals", rows)

    async def resolve_approval(
        self, approval_id: int, resolved_by: str, approved: bool
    ) -> dict | None:
        """Resolve a pending approval. Returns the pre-update record, or ``None``.

        The select and the update share a row lock so two admins cannot both
        resolve the same approval.
        """
        status = "approved" if approved else "rejected"
        async with self._pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                "SELECT * FROM pending_approvals WHERE id = $1 AND status = 'pending' FOR UPDATE",
                approval_id,
            )
            if row is None:
                return None
            await con.execute(
                """
                UPDATE pending_approvals
                   SET status = $2, resolved_by = $3, resolved_at = now()
                 WHERE id = $1
                """,
                approval_id,
                status,
                resolved_by,
            )
        return normalize_row("pending_approvals", row)

    # ══════════════════════════════════════════════════════════
    #  Sprint 7: Bounties
    # ══════════════════════════════════════════════════════════

    async def create_bounty(
        self,
        creator: str,
        channel: str,
        description: str,
        amount: int,
        expires_at: str | None = None,
    ) -> int:
        """Create a bounty. Returns the bounty id."""
        async with self._pool.acquire() as con:
            bounty_id = await con.fetchval(
                """
                INSERT INTO bounties (creator, channel, description, amount, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                creator,
                channel,
                description,
                amount,
                to_datetime(expires_at),
            )
        return int(bounty_id)

    async def get_open_bounties(
        self, channel: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List open bounties, newest first."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT id, creator, description, amount, created_at, expires_at
                  FROM bounties
                 WHERE channel = $1 AND status = 'open'
                 ORDER BY id DESC
                 LIMIT $2
                """,
                channel,
                limit,
            )
        return normalize_rows("bounties", rows)

    async def get_bounty(self, bounty_id: int, channel: str) -> dict | None:
        """Return a single bounty, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM bounties WHERE id = $1 AND channel = $2",
                bounty_id,
                channel,
            )
        return normalize_row("bounties", row)

    async def _close_bounty(
        self, bounty_id: int, channel: str, status: str, resolved_by: str
    ) -> bool:
        """Set a bounty to a terminal status. ``False`` if it was not open."""
        async with self._pool.acquire() as con:
            tag = await con.fetchval(
                """
                UPDATE bounties
                   SET status = $3, resolved_by = $4, resolved_at = now()
                 WHERE id = $1 AND channel = $2 AND status = 'open'
             RETURNING id
                """,
                bounty_id,
                channel,
                status,
                resolved_by,
            )
        return tag is not None

    async def claim_bounty(
        self, bounty_id: int, channel: str, winner: str, resolved_by: str
    ) -> bool:
        """Claim an open bounty. ``False`` if it was not open."""
        async with self._pool.acquire() as con:
            tag = await con.fetchval(
                """
                UPDATE bounties
                   SET status = 'claimed', winner = $3, resolved_by = $4, resolved_at = now()
                 WHERE id = $1 AND channel = $2 AND status = 'open'
             RETURNING id
                """,
                bounty_id,
                channel,
                winner,
                resolved_by,
            )
        return tag is not None

    async def cancel_bounty(
        self, bounty_id: int, channel: str, resolved_by: str
    ) -> bool:
        """Cancel an open bounty. ``False`` if it was not open."""
        return await self._close_bounty(bounty_id, channel, "cancelled", resolved_by)

    async def expire_bounties(self, channel: str) -> list[dict[str, Any]]:
        """Expire past-due open bounties. Returns the rows that expired."""
        async with self._pool.acquire() as con, con.transaction():
            rows = await con.fetch(
                """
                SELECT * FROM bounties
                 WHERE channel = $1 AND status = 'open'
                   AND expires_at IS NOT NULL AND expires_at < now()
                """,
                channel,
            )
            if rows:
                await con.execute(
                    "UPDATE bounties SET status = 'expired' "
                    "WHERE channel = $1 AND status = 'open' "
                    "AND expires_at IS NOT NULL AND expires_at < now()",
                    channel,
                )
        return normalize_rows("bounties", rows)

    # ══════════════════════════════════════════════════════════
    #  Sprint 7: Daily competitions
    # ══════════════════════════════════════════════════════════

    async def get_daily_top(
        self, channel: str, date: str, field: str, limit: int = 1
    ) -> list[dict[str, Any]]:
        """Return the top users for one ``daily_activity`` counter."""
        if field not in _COMPETITION_FIELDS:
            self._logger.warning("Unknown daily_top field: %s", field)
            return []
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                f"""
                SELECT * FROM daily_activity
                 WHERE channel = $1 AND date = $2 AND {field} > 0
                 ORDER BY {field} DESC
                 LIMIT $3
                """,
                channel,
                to_date(date),
                limit,
            )
        return normalize_rows("daily_activity", rows)

    async def get_daily_threshold_qualifiers(
        self, channel: str, date: str, field: str, threshold: int
    ) -> list[str]:
        """Usernames whose ``daily_activity.{field}`` meets a threshold."""
        if field not in _COMPETITION_FIELDS:
            self._logger.warning("Unknown competition field: %s", field)
            return []
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                f"SELECT username FROM daily_activity "
                f"WHERE channel = $1 AND date = $2 AND {field} >= $3",
                channel,
                to_date(date),
                threshold,
            )
        return [row["username"] for row in rows]

    # ══════════════════════════════════════════════════════════
    #  Sprint 6: Leaderboards
    # ══════════════════════════════════════════════════════════

    async def get_top_earners_today(
        self, channel: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Top earners today (UTC), as ``[{username, earned_today}]``."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT username, z_earned AS earned_today
                  FROM daily_activity
                 WHERE channel = $1 AND date = (now() AT TIME ZONE 'UTC')::date AND z_earned > 0
                 ORDER BY z_earned DESC
                 LIMIT $2
                """,
                channel,
                limit,
            )
        return normalize_rows("daily_activity", rows)

    async def get_richest_users(
        self, channel: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Highest current balances, as ``[{username, balance, rank_name}]``."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT username, balance, rank_name FROM accounts
                 WHERE channel = $1
                 ORDER BY balance DESC
                 LIMIT $2
                """,
                channel,
                limit,
            )
        return normalize_rows("accounts", rows)

    async def get_highest_lifetime(
        self, channel: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Highest lifetime earners, as ``[{username, lifetime_earned, rank_name}]``."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT username, lifetime_earned, rank_name FROM accounts
                 WHERE channel = $1
                 ORDER BY lifetime_earned DESC
                 LIMIT $2
                """,
                channel,
                limit,
            )
        return normalize_rows("accounts", rows)

    async def get_rank_distribution(self, channel: str) -> dict[str, int]:
        """Count users per rank name."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT rank_name, COUNT(*) AS cnt FROM accounts WHERE channel = $1 "
                "GROUP BY rank_name",
                channel,
            )
        return {row["rank_name"]: int(row["cnt"]) for row in rows}

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Reporting aggregates
    # ══════════════════════════════════════════════════════════

    async def get_median_balance(self, channel: str) -> int:
        """Median account balance (integer division for an even population)."""
        async with self._pool.acquire() as con:
            balances = await con.fetch(
                "SELECT balance FROM accounts WHERE channel = $1 ORDER BY balance",
                channel,
            )
        if not balances:
            return 0
        values = [int(row["balance"]) for row in balances]
        mid = len(values) // 2
        if len(values) % 2 == 0:
            return (values[mid - 1] + values[mid]) // 2
        return values[mid]

    async def get_active_economy_users_today(self, channel: str, date: str) -> int:
        """Count users who earned or spent on a given day."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                """
                SELECT COUNT(*) FROM daily_activity
                 WHERE channel = $1 AND date = $2 AND (z_earned > 0 OR z_spent > 0)
                """,
                channel,
                to_date(date),
            )
        return int(value or 0)

    async def _daily_totals(
        self, channel: str, where: str, *params: Any
    ) -> dict[str, Any]:
        """Shared shape for daily/weekly totals."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                f"""
                SELECT COALESCE(SUM(z_earned), 0) AS z_earned,
                       COALESCE(SUM(z_spent), 0) AS z_spent,
                       COALESCE(SUM(z_gambled_in), 0) AS z_gambled_in,
                       COALESCE(SUM(z_gambled_out), 0) AS z_gambled_out
                  FROM daily_activity
                 WHERE channel = $1 AND {where}
                """,
                channel,
                *params,
            )
        return normalize_row("daily_activity", row) or {
            "z_earned": 0,
            "z_spent": 0,
            "z_gambled_in": 0,
            "z_gambled_out": 0,
        }

    async def get_daily_totals(self, channel: str, date: str) -> dict:
        """Daily earned/spent/gambled totals."""
        return await self._daily_totals(channel, "date = $2", to_date(date))

    async def get_weekly_totals(
        self, channel: str, start_date: str, end_date: str
    ) -> dict:
        """Totals across an inclusive date range (admin digest)."""
        return await self._daily_totals(
            channel, "date >= $2 AND date <= $3", to_date(start_date), to_date(end_date)
        )

    async def _top_by_field(
        self, channel: str, start_date: str, end_date: str, field: str, limit: int
    ) -> list[dict[str, Any]]:
        """Shared shape for ranged top-earner/spender lists."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                f"""
                SELECT username, SUM({field}) AS {field.replace('z_', '')}
                  FROM daily_activity
                 WHERE channel = $1 AND date >= $2 AND date <= $3
                 GROUP BY username
                 ORDER BY {field.replace('z_', '')} DESC
                 LIMIT $4
                """,
                channel,
                to_date(start_date),
                to_date(end_date),
                limit,
            )
        return normalize_rows("daily_activity", rows)

    async def get_top_earners_range(
        self, channel: str, start_date: str, end_date: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Top earners over a date range, as ``[{username, earned}]``."""
        return await self._top_by_field(
            channel, start_date, end_date, "z_earned", limit
        )

    async def get_top_spenders_range(
        self, channel: str, start_date: str, end_date: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Top spenders over a date range, as ``[{username, spent}]``."""
        return await self._top_by_field(channel, start_date, end_date, "z_spent", limit)

    async def get_lifetime_earned(self, username: str, channel: str) -> int:
        """A user's lifetime earned total."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT lifetime_earned FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    async def get_lifetime_messages(self, username: str, channel: str) -> int:
        """Total messages sent across all days."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COALESCE(SUM(messages_sent), 0) FROM daily_activity "
                "WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0)

    async def get_lifetime_presence_hours(self, username: str, channel: str) -> float:
        """Lifetime dwell time in hours."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT COALESCE(SUM(minutes_present), 0) FROM daily_activity "
                "WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return int(value or 0) / 60.0

    async def get_accounts_with_min_balance(
        self, channel: str, min_balance: int
    ) -> list[dict[str, Any]]:
        """Accounts in a channel holding at least a minimum balance."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM accounts WHERE channel = $1 AND balance >= $2",
                channel,
                min_balance,
            )
        return normalize_rows("accounts", rows)

    async def get_participation_rate(
        self, channel: str, total_channel_users: int
    ) -> float:
        """Percentage of channel users holding an economy account."""
        if total_channel_users <= 0:
            return 0.0
        count = await self.get_all_accounts_count(channel)
        return (count / total_channel_users) * 100

    # ══════════════════════════════════════════════════════════
    #  Sprint 2: Balance maintenance
    # ══════════════════════════════════════════════════════════

    async def apply_interest_batch(
        self, channel: str, rate: float, cap: int, min_balance: int
    ) -> int:
        """Pay daily interest to qualifying accounts. Returns the total paid.

        Each account's update and its ledger row commit together, and the whole
        batch is one transaction, so a partial run cannot leave a balance
        credited without a matching transaction (or vice versa).
        """
        total = 0
        async with self._pool.acquire() as con, con.transaction():
            rows = await con.fetch(
                "SELECT username, balance FROM accounts WHERE channel = $1 AND balance >= $2",
                channel,
                min_balance,
            )
            for row in rows:
                interest = min(int(row["balance"] * rate // 1), cap)
                if interest <= 0:
                    continue
                await con.execute(
                    """
                    UPDATE accounts
                       SET balance = balance + $3, lifetime_earned = lifetime_earned + $3
                     WHERE username = $1 AND channel = $2
                    """,
                    row["username"],
                    channel,
                    interest,
                )
                await con.execute(
                    """
                    INSERT INTO transactions (username, channel, amount, type, trigger_id)
                    VALUES ($1, $2, $3, 'interest', 'maintenance.interest')
                    """,
                    row["username"],
                    channel,
                    interest,
                )
                total += interest
        return total

    async def apply_decay_batch(
        self, channel: str, rate: float, exempt_below: int
    ) -> int:
        """Apply decay to qualifying accounts. Returns the total collected."""
        total = 0
        async with self._pool.acquire() as con, con.transaction():
            rows = await con.fetch(
                "SELECT username, balance FROM accounts WHERE channel = $1 AND balance >= $2",
                channel,
                exempt_below,
            )
            for row in rows:
                decay = int(row["balance"] * rate // 1)
                if decay <= 0:
                    continue
                await con.execute(
                    """
                    UPDATE accounts
                       SET balance = balance - $3, lifetime_spent = lifetime_spent + $3
                     WHERE username = $1 AND channel = $2
                    """,
                    row["username"],
                    channel,
                    decay,
                )
                await con.execute(
                    """
                    INSERT INTO transactions
                        (username, channel, amount, type, trigger_id, reason)
                    VALUES ($1, $2, $3, 'decay', 'maintenance.decay', 'Vault maintenance fee')
                    """,
                    row["username"],
                    channel,
                    -decay,
                )
                total += decay
        return total

    # ══════════════════════════════════════════════════════════
    #  Sprint 9: Batch presence credit
    # ══════════════════════════════════════════════════════════

    async def batch_credit_presence(self, credits: list[tuple[str, str, int]]) -> None:
        """Credit presence Z for many users in one transaction.

        ``credits`` is ``[(username, channel, amount), ...]``. All-or-nothing:
        any failure rolls the whole batch back. Accounts are created on demand
        so a presence tick never silently credits nobody.
        """
        async with self._pool.acquire() as con, con.transaction():
            for username, channel, amount in credits:
                await self._ensure_account(con, username, channel)
                await con.execute(
                    """
                    UPDATE accounts
                       SET balance = balance + $3, lifetime_earned = lifetime_earned + $3
                     WHERE username = $1 AND channel = $2
                    """,
                    username,
                    channel,
                    amount,
                )
                await con.execute(
                    """
                    INSERT INTO transactions
                        (username, channel, amount, type, trigger_id, reason)
                    VALUES ($1, $2, $3, 'presence', 'presence.base', 'Presence earning')
                    """,
                    username,
                    channel,
                    amount,
                )

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Snapshots
    # ══════════════════════════════════════════════════════════

    async def write_snapshot(self, channel: str, data: dict) -> None:
        """Insert an economy snapshot row."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO economy_snapshots
                    (channel, total_accounts, total_z_circulation, active_economy_users_today,
                     z_earned_today, z_spent_today, z_gambled_net_today, median_balance,
                     participation_rate, inflation_multiplier)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                channel,
                data.get("total_accounts", 0),
                data.get("total_z_circulation", 0),
                data.get("active_economy_users_today", 0),
                data.get("z_earned_today", 0),
                data.get("z_spent_today", 0),
                data.get("z_gambled_net_today", 0),
                data.get("median_balance", 0),
                data.get("participation_rate", 0.0),
                data.get("inflation_multiplier", 1.0),
            )

    async def get_latest_snapshot(self, channel: str) -> dict | None:
        """Return the most recent snapshot for a channel, or ``None``."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM economy_snapshots WHERE channel = $1 "
                "ORDER BY snapshot_time DESC LIMIT 1",
                channel,
            )
        return normalize_row("economy_snapshots", row)

    async def get_snapshot_history(
        self, channel: str, days: int = 7
    ) -> list[dict[str, Any]]:
        """Return snapshots from the last N days, oldest first."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM economy_snapshots WHERE channel = $1 "
                "AND snapshot_time >= now() - $2::interval ORDER BY snapshot_time ASC",
                channel,
                timedelta(days=days),
            )
        return normalize_rows("economy_snapshots", rows)

    # ══════════════════════════════════════════════════════════
    #  Sprint 11: Account pruner
    # ══════════════════════════════════════════════════════════

    async def find_purgeable_accounts(
        self,
        channel: str,
        inactive_days: int,
        balance_min: int,
        balance_max: int | None,
        max_lifetime_earned: int | None,
    ) -> list[dict[str, Any]]:
        """Accounts matching every pruning criterion (all must hold).

        Never touches moderation or real-purchase records: banned accounts,
        accounts that ever spent, and accounts with any vanity value are
        excluded regardless of the numeric filters.
        """
        # Parameter order must match the placeholders exactly:
        #   $1 channel, $2 inactivity window, $3 balance_min,
        #   $4 balance_max (optional), $5 max_lifetime_earned (optional).
        params: list[Any] = [channel, timedelta(days=inactive_days), balance_min]
        query = """
            SELECT username, channel, balance, lifetime_earned, lifetime_spent,
                   first_seen, last_seen, last_active,
                   welcome_wallet_claimed, economy_banned
              FROM accounts
             WHERE channel = $1
               AND economy_banned = false
               AND lifetime_spent = 0
               AND (custom_greeting IS NULL OR custom_greeting = '')
               AND (custom_title IS NULL OR custom_title = '')
               AND (chat_color IS NULL OR chat_color = '')
               AND (channel_gif_url IS NULL OR channel_gif_url = '')
               AND (personal_currency_name IS NULL OR personal_currency_name = '')
               AND last_seen < now() - $2::interval
               AND balance >= $3
        """
        if balance_max is not None:
            params.append(balance_max)
            query += " AND balance <= $4"
        if max_lifetime_earned is not None:
            params.append(max_lifetime_earned)
            query += " AND lifetime_earned <= $%d" % len(params)
        query += " ORDER BY last_seen ASC"

        async with self._pool.acquire() as con:
            rows = await con.fetch(query, *params)
        return normalize_rows("accounts", rows)

    async def delete_account_and_cascade(self, username: str, channel: str) -> dict:
        """Delete an account and all its child rows atomically.

        Returns per-table row counts, matching the SQLite store's contract.
        """
        counts: dict[str, int] = {}
        async with self._pool.acquire() as con, con.transaction():
            for table in ("daily_activity", "transactions"):
                status = await con.execute(
                    f"DELETE FROM {table} WHERE username = $1 AND channel = $2",
                    username,
                    channel,
                )
                counts[table] = _affected_rows(status)

            status = await con.execute(
                "DELETE FROM tip_history WHERE channel = $3 AND (sender = $1 OR receiver = $2)",
                username,
                username,
                channel,
            )
            counts["tip_history"] = _affected_rows(status)

            status = await con.execute(
                "DELETE FROM vanity_items WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
            counts["vanity_items"] = _affected_rows(status)

            status = await con.execute(
                "DELETE FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
            counts["accounts"] = _affected_rows(status)
        return counts

    # ══════════════════════════════════════════════════════════
    #  Bans & quiet mode
    # ══════════════════════════════════════════════════════════

    async def ban_user(
        self, username: str, channel: str, banned_by: str, reason: str | None = None
    ) -> None:
        """Ban a user from the economy."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO banned_users (username, channel, banned_by, reason)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (username, channel) DO UPDATE
                    SET banned_by = EXCLUDED.banned_by,
                        banned_at = now(),
                        reason = EXCLUDED.reason
                """,
                username,
                channel,
                banned_by,
                reason,
            )

    async def unban_user(self, username: str, channel: str) -> bool:
        """Lift a ban. Returns True if a ban existed."""
        async with self._pool.acquire() as con:
            status = await con.execute(
                "DELETE FROM banned_users WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return status.endswith(" 1")

    async def is_banned(self, username: str, channel: str) -> bool:
        """Whether a user is economy-banned."""
        async with self._pool.acquire() as con:
            exists = await con.fetchval(
                "SELECT 1 FROM banned_users WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return exists is not None

    async def get_quiet_mode(self, username: str, channel: str) -> bool:
        """Whether the user opted out of trigger PMs."""
        async with self._pool.acquire() as con:
            value = await con.fetchval(
                "SELECT quiet_mode FROM accounts WHERE username = $1 AND channel = $2",
                username,
                channel,
            )
        return bool(value) if value is not None else False

    async def set_quiet_mode(self, username: str, channel: str, enabled: bool) -> None:
        """Toggle quiet mode. ``enabled`` may be any truthy value."""
        async with self._pool.acquire() as con:
            await self._ensure_account(con, username, channel)
            await con.execute(
                "UPDATE accounts SET quiet_mode = $3 WHERE username = $1 AND channel = $2",
                username,
                channel,
                to_bool(enabled),
            )

    async def update_account_rank(
        self, username: str, channel: str, rank_name: str
    ) -> None:
        """Set the account's rank name, creating the account if needed."""
        async with self._pool.acquire() as con:
            await self._ensure_account(con, username, channel)
            await con.execute(
                "UPDATE accounts SET rank_name = $3 WHERE username = $1 AND channel = $2",
                username,
                channel,
                rank_name,
            )
