"""Tests for Sprint 8 trigger analytics."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from kryten_economy.database import EconomyDatabase

CH = "testchannel"


@pytest.mark.asyncio
async def test_increment_new_trigger(database: EconomyDatabase):
    """Creates row with hit_count=1."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await database.increment_trigger_analytics(CH, "presence.base", today, 10)

    analytics = await database.get_trigger_analytics(CH, today)
    assert len(analytics) == 1
    assert analytics[0]["trigger_id"] == "presence.base"
    assert analytics[0]["hit_count"] == 1
    assert analytics[0]["total_z_awarded"] == 10


@pytest.mark.asyncio
async def test_increment_existing(database: EconomyDatabase):
    """Updates hit_count and total_z_awarded."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await database.increment_trigger_analytics(CH, "chat.long_message", today, 5)
    await database.increment_trigger_analytics(CH, "chat.long_message", today, 3)
    await database.increment_trigger_analytics(CH, "chat.long_message", today, 7)

    analytics = await database.get_trigger_analytics(CH, today)
    trigger = [a for a in analytics if a["trigger_id"] == "chat.long_message"][0]
    assert trigger["hit_count"] == 3
    assert trigger["total_z_awarded"] == 15


@pytest.mark.asyncio
async def test_analytics_by_date(database: EconomyDatabase):
    """Returns all triggers for a specific date."""
    await database.increment_trigger_analytics(CH, "presence.base", "2026-01-01", 10)
    await database.increment_trigger_analytics(CH, "chat.long_message", "2026-01-01", 5)
    await database.increment_trigger_analytics(CH, "presence.base", "2026-01-02", 20)

    jan1 = await database.get_trigger_analytics(CH, "2026-01-01")
    assert len(jan1) == 2

    jan2 = await database.get_trigger_analytics(CH, "2026-01-02")
    assert len(jan2) == 1
    assert jan2[0]["trigger_id"] == "presence.base"


@pytest.mark.asyncio
async def test_analytics_range(database: EconomyDatabase):
    """Get trigger analytics across a date range."""
    await database.increment_trigger_analytics(CH, "presence.base", "2026-01-01", 10)
    await database.increment_trigger_analytics(CH, "presence.base", "2026-01-02", 20)
    await database.increment_trigger_analytics(CH, "presence.base", "2026-01-03", 30)

    result = await database.get_trigger_analytics_range(CH, "2026-01-01", "2026-01-03")
    assert len(result) >= 3
