"""Value-boundary conversion between the SQLite and PostgreSQL stores.

Why this module exists
----------------------
``EconomyDatabase`` (SQLite) and ``EconomyDatabasePg`` (asyncpg) must be
interchangeable behind the same interface, so *both* must return the same Python
types to every caller. PostgreSQL storage is native and richer than SQLite, so
asyncpg hands back values SQLite never would:

- ``timestamptz`` -> ``datetime`` (SQLite gave back a text string)
- ``boolean``    -> ``bool``     (SQLite gave back ``0``/``1`` ints)
- ``date``       -> ``date``     (SQLite gave back a ``YYYY-MM-DD`` string)

Leaving those native types to escape the store would break real consumers:

- ``kryten_economy/pm_handler.py`` calls ``datetime.fromisoformat(first_seen)``
  on an account row, which raises ``TypeError`` for a ``datetime`` object.
- ``tests/test_database.py`` asserts ``acct["welcome_wallet_claimed"] == 0``.

So the rule enforced here, and relied on by the rest of the port:

    PostgreSQL-native storage, SQLite-identical Python types at the store edge.

Every conversion in this module was verified against the live server rather than
assumed (see the module docstring of ``database_pg`` for the measured behaviour).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

# Column families that need normalisation when a row crosses the store edge.
# Keeping this explicit (rather than reflecting over ``asyncpg`` types) makes the
# boundary auditable: if a new boolean/timestamp column is added to the schema,
# it must be added here too or it will leak a native type.
BOOLEAN_COLUMNS: frozenset[str] = frozenset(
    {
        "accounts.channel_gif_approved",
        "accounts.welcome_wallet_claimed",
        "accounts.economy_banned",
        "accounts.quiet_mode",
        "daily_activity.first_message_claimed",
        "daily_activity.free_spin_used",
        "streaks.weekend_seen_this_week",
        "streaks.weekday_seen_this_week",
        "streaks.bridge_claimed_this_week",
        "hourly_milestones.hours_1",
        "hourly_milestones.hours_3",
        "hourly_milestones.hours_6",
        "hourly_milestones.hours_12",
        "hourly_milestones.hours_24",
        "vanity_items.active",
        "queue_spend_requests.refunded",
    }
)

TIMESTAMP_COLUMNS: frozenset[str] = frozenset(
    {
        "accounts.first_seen",
        "accounts.last_seen",
        "accounts.last_active",
        "transactions.created_at",
        "trigger_cooldowns.window_start",
        "pending_challenges.created_at",
        "pending_challenges.expires_at",
        "race_results.created_at",
        "race_bets.placed_at",
        "tip_history.created_at",
        "pending_approvals.created_at",
        "pending_approvals.resolved_at",
        "vanity_items.purchased_at",
        "achievements.awarded_at",
        "bounties.created_at",
        "bounties.expires_at",
        "bounties.resolved_at",
        "economy_snapshots.snapshot_time",
        "banned_users.banned_at",
        "queue_spend_requests.created_at",
        "queue_spend_requests.refunded_at",
        "service_metrics.updated_at",
    }
)

DATE_COLUMNS: frozenset[str] = frozenset(
    {
        "daily_activity.date",
        "streaks.last_streak_date",
        "hourly_milestones.date",
        "trigger_analytics.date",
    }
)

# Timestamp formats the SQLite schema may have stored, in priority order.
_SQLITE_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S+00:00",
)


def to_datetime(value: Any) -> datetime | None:
    """Coerce a value to a timezone-aware ``datetime`` for binding.

    Accepts ``datetime``, ``date``, ISO-8601 text, and the legacy SQLite
    timestamp formats. Naive values are interpreted as UTC, matching the
    service's existing convention (``SQLite stored naive timestamps as UTC``).
    Returns ``None`` for empty/None input so it can be bound as SQL NULL.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _SQLITE_TIMESTAMP_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"Unrecognized timestamp format: {value!r}") from None
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    raise TypeError(f"Unsupported timestamp value: {value!r}")


def to_date(value: Any) -> date | None:
    """Coerce a value to ``datetime.date`` for binding, or ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            parsed = to_datetime(text)
            return parsed.date() if parsed else None
    raise TypeError(f"Unsupported date value: {value!r}")


def to_bool(value: Any) -> bool | None:
    """Coerce a value to ``bool`` for binding, or ``None``.

    The SQLite store writes flags as ``int`` (``0``/``1``) and several call
    sites pass ``int(bool)`` straight through, so ints must be accepted here.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "t", "yes", "y"}:
            return True
        if text in {"0", "false", "f", "no", "n", ""}:
            return False
    raise TypeError(f"Unsupported boolean value: {value!r}")


def to_datetime_str(value: Any) -> str | None:
    """Render a timestamp as an ISO-8601 string, matching the SQLite store.

    Naive ``datetime`` values are assumed to be UTC (the service-wide
    convention) and are emitted without an offset, so the string is byte-for-byte
    the shape ``datetime.fromisoformat`` callers already expect.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat(sep=" ")
    return str(value)


def to_date_str(value: Any) -> str | None:
    """Render a date as ``YYYY-MM-DD``, matching the SQLite store."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def to_int(value: Any) -> int:
    """Render a boolean flag as the ``0``/``1`` int the SQLite store returns."""
    return 1 if value else 0


def to_number(value: Any) -> Any:
    """Normalise an aggregate result to the numeric type SQLite would return.

    asyncpg maps PostgreSQL ``NUMERIC`` to :class:`decimal.Decimal`, so every
    ``SUM()``/``AVG()`` over an integer column comes back as ``Decimal`` while
    SQLite's ``SUM()`` returns ``int``. Left alone this would leak ``Decimal``
    into balances, PM formatting, and JSON responses (``Decimal`` is not
    JSON-serialisable by the standard encoder, and ``Decimal(1) == 1`` masks
    the problem in assertions).

    Integral ``Decimal``/``float`` values collapse to ``int``; genuine
    fractional results (a ``REAL`` average) stay ``float``. Non-numeric values
    and ``None`` pass through untouched.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def normalize_row(table: str, row: Any) -> dict[str, Any] | None:
    """Normalise one asyncpg ``Record`` into SQLite-compatible dict values."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        qualified = f"{table}.{key}"
        if qualified in BOOLEAN_COLUMNS:
            out[key] = to_int(value)
        elif qualified in TIMESTAMP_COLUMNS:
            out[key] = to_datetime_str(value)
        elif qualified in DATE_COLUMNS:
            out[key] = to_date_str(value)
        else:
            out[key] = to_number(value)
    return out


def normalize_rows(table: str, rows: Any) -> list[dict[str, Any]]:
    """Normalise a sequence of asyncpg ``Record`` objects."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        value = normalize_row(table, row)
        if value is not None:
            normalized.append(value)
    return normalized


def normalize_columns(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a dict-based row (e.g. from ``fetchrow`` into a dict)."""
    return normalize_row(table, row) or {}
