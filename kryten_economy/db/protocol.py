"""The shared persistence interface for the economy data layer.

``EconomyDatabase`` (SQLite) and ``EconomyDatabasePg`` (asyncpg) both implement
this protocol, which lets the orchestrator and every domain component stay
backend-agnostic. mypy checks both implementations against it, so a method
missing from one backend is a type error rather than a runtime ``AttributeError``
in production.

The protocol is intentionally the *whole* economy persistence surface, not a
convenient subset: the goal of Sprint 12 is a complete, swappable store.

Type contract
-------------
Implementations must return values that are interchangeable with the SQLite
store's, because that is what every existing caller already handles:

- rows are plain ``dict`` (never ``sqlite3.Row`` / ``asyncpg.Record``)
- timestamps are ISO-8601 ``str`` (never ``datetime``)
- boolean flags are ``int`` 0/1 (never ``bool``) in row dicts
- ``date`` values are ``YYYY-MM-DD`` ``str``
- ``None`` is used for "no row" / "not found", mirroring the SQLite behaviour

``db/boundary.py`` documents the conversion rules and the measured asyncpg
constraints that make them necessary.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# NOTE: the method list below mirrors ``kryten_economy/database.py``. When a
# method is added there, add it here in the same position so the two stay
# reviewable side by side. ``tests/test_store_parity.py`` asserts that every
# public coroutine on the SQLite class appears on this protocol AND on the
# PostgreSQL class, so drift is caught by the suite rather than by a reader.


@runtime_checkable
class EconomyStore(Protocol):
    """Persistence operations required by the economy service."""

    # ── Lifecycle ──────────────────────────────────────────────
    async def initialize(self) -> None: ...

    # ── Service metrics ────────────────────────────────────────
    async def save_metrics(self, data: dict[str, int]) -> None: ...

    async def restore_metrics(self) -> dict[str, int]: ...

    # ── Accounts ──────────────────────────────────────────────
    async def get_or_create_account(self, username: str, channel: str) -> dict: ...

    async def get_account(self, username: str, channel: str) -> dict | None: ...

    async def get_balance(self, username: str, channel: str) -> int: ...

    async def search_accounts(
        self, channel: str, pattern: str = "", limit: int = 50
    ) -> list[dict[str, Any]]: ...

    async def update_last_seen(self, username: str, channel: str) -> None: ...

    async def update_last_active(self, username: str, channel: str) -> None: ...

    # ── Currency mutations (transactional) ─────────────────────
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
    ) -> int: ...

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
    ) -> int | None: ...

    async def refund(
        self,
        username: str,
        channel: str,
        amount: int,
        reason: str | None = None,
        trigger_id: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> int: ...

    async def set_balance(self, username: str, channel: str, amount: int) -> None: ...

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
    ) -> None: ...

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
        """Debit a wager, increment ``lifetime_spent``, and log it — atomically.

        Returns False and changes nothing when the balance cannot cover it.
        """
        ...

    # ── Transactions ───────────────────────────────────────────
    async def get_recent_transactions(
        self,
        username: str,
        channel: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    async def get_recent_channel_transactions(
        self, channel: str, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    # ── Population / reporting ────────────────────────────────
    async def get_total_circulation(self, channel: str) -> int: ...

    async def get_account_count(self, channel: str) -> int: ...

    async def get_all_accounts_count(self, channel: str) -> int: ...

    # ── Onboarding ────────────────────────────────────────────
    async def claim_welcome_wallet(self, username: str, channel: str, amount: int) -> bool: ...

    # ── Daily activity ─────────────────────────────────────────
    async def get_daily_minutes_present(self, username: str, channel: str, date: str) -> int: ...

    async def increment_daily_minutes_present(
        self, username: str, channel: str, date: str, minutes: int = 1
    ) -> None: ...

    async def increment_daily_z_earned(
        self, username: str, channel: str, date: str, amount: int
    ) -> None: ...
