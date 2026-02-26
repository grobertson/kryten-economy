"""Shared utility helpers for kryten-economy."""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_channel(channel: str) -> str:
    """Normalize channel name for NATS subject use.
    Follow kryten-py convention (lowercase, strip special chars)."""
    return channel.lower().replace(" ", "_")


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse SQLite TIMESTAMP string to timezone-aware datetime, or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        # Ensure timezone-aware (SQLite stores naive timestamps as UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def today_str() -> str:
    """Return today's date as YYYY-MM-DD string (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_utc() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def iso_week_str(dt: datetime | None = None) -> str:
    """Return ISO week string like '2026-W09'."""
    if dt is None:
        dt = now_utc()
    return dt.strftime("%G-W%V")
