"""Tests for ScheduledEventManager — Sprint 7."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kryten_economy.config import EconomyConfig, ScheduledEventConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.multiplier_engine import MultiplierEngine
from kryten_economy.scheduled_event_manager import ScheduledEventManager
from tests.conftest import make_config_dict

CH = "testchannel"


def _make_config_with_events(events: list[dict]) -> EconomyConfig:
    return EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
        "scheduled_events": events,
    }))


def _make_deps(config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    multiplier_engine = MultiplierEngine(config, mock_presence, logging.getLogger("test"))
    return multiplier_engine, mock_presence


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_event_start_on_cron(database: EconomyDatabase, mock_client: MagicMock):
    """Cron fires → event registered, multiplier set."""
    cfg = _make_config_with_events([{
        "name": "Movie Night",
        "cron": "* * * * *",  # every minute — will match now
        "duration_hours": 2,
        "multiplier": 2.0,
        "presence_bonus": 0,
        "announce": False,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    # Manually call _check_event
    event_cfg = cfg.multipliers.scheduled_events[0]
    now = datetime.now(timezone.utc)
    await manager._check_event(event_cfg, CH, now)

    # Multiplier should be registered
    _, active = mult_engine.get_combined_multiplier(CH)
    sched = [m for m in active if "Movie Night" in m.source]
    assert len(sched) == 1


@pytest.mark.asyncio
async def test_event_end_after_duration(database: EconomyDatabase, mock_client: MagicMock):
    """Duration elapsed → event cleared, multiplier removed."""
    cfg = _make_config_with_events([{
        "name": "Short Event",
        "cron": "* * * * *",
        "duration_hours": 1,
        "multiplier": 2.0,
        "presence_bonus": 0,
        "announce": False,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    # Start the event
    event_cfg = cfg.multipliers.scheduled_events[0]
    key = f"{CH}:{event_cfg.name}"
    now = datetime.now(timezone.utc)
    await manager._start_event(event_cfg, CH, key, now)

    # Verify active
    assert key in manager._active

    # Simulate time passing by setting end_time to the past
    manager._active[key]["end_time"] = datetime.now(timezone.utc) - timedelta(minutes=1)

    # End it
    await manager._end_event(event_cfg, CH, key)

    assert key not in manager._active
    _, active = mult_engine.get_combined_multiplier(CH)
    sched = [m for m in active if "Short Event" in m.source]
    assert len(sched) == 0


@pytest.mark.asyncio
async def test_presence_bonus_distributed(database: EconomyDatabase, mock_client: MagicMock):
    """500 Z split among 5 users → 100 each."""
    cfg = _make_config_with_events([{
        "name": "Bonus Event",
        "cron": "* * * * *",
        "duration_hours": 2,
        "multiplier": 1.5,
        "presence_bonus": 500,
        "announce": False,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)
    users = {"Alice", "Bob", "Charlie", "Dave", "Eve"}
    mock_presence.get_connected_users.return_value = users

    # Seed accounts
    for name in users:
        await database.get_or_create_account(name, CH)

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    event_cfg = cfg.multipliers.scheduled_events[0]
    await manager._distribute_presence_bonus(event_cfg, CH)

    # Each user should have 100 Z (500 / 5)
    for name in users:
        acc = await database.get_account(name, CH)
        assert acc["balance"] == 100


@pytest.mark.asyncio
async def test_presence_bonus_zero_users(database: EconomyDatabase, mock_client: MagicMock):
    """No users → no error."""
    cfg = _make_config_with_events([{
        "name": "Empty Event",
        "cron": "* * * * *",
        "duration_hours": 2,
        "multiplier": 1.5,
        "presence_bonus": 500,
        "announce": False,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)
    mock_presence.get_connected_users.return_value = set()

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    event_cfg = cfg.multipliers.scheduled_events[0]
    # Should not raise
    await manager._distribute_presence_bonus(event_cfg, CH)


@pytest.mark.asyncio
async def test_announcement_on_start(database: EconomyDatabase, mock_client: MagicMock):
    """Public chat message on start."""
    cfg = _make_config_with_events([{
        "name": "Announced Event",
        "cron": "* * * * *",
        "duration_hours": 2,
        "multiplier": 2.0,
        "presence_bonus": 0,
        "announce": True,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    event_cfg = cfg.multipliers.scheduled_events[0]
    key = f"{CH}:{event_cfg.name}"
    now = datetime.now(timezone.utc)
    await manager._start_event(event_cfg, CH, key, now)

    mock_client.send_chat.assert_called()
    msg = mock_client.send_chat.call_args[0][1]
    assert "Announced Event" in msg


@pytest.mark.asyncio
async def test_announcement_on_end(database: EconomyDatabase, mock_client: MagicMock):
    """Public chat message on end."""
    cfg = _make_config_with_events([{
        "name": "Ending Event",
        "cron": "* * * * *",
        "duration_hours": 2,
        "multiplier": 2.0,
        "presence_bonus": 0,
        "announce": True,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    event_cfg = cfg.multipliers.scheduled_events[0]
    key = f"{CH}:{event_cfg.name}"
    manager._active[key] = {
        "event_name": event_cfg.name,
        "end_time": datetime.now(timezone.utc),
    }

    await manager._end_event(event_cfg, CH, key)

    mock_client.send_chat.assert_called()
    msg = mock_client.send_chat.call_args[0][1]
    assert "Ending Event" in msg


@pytest.mark.asyncio
async def test_no_refire_same_cycle(database: EconomyDatabase, mock_client: MagicMock):
    """Event doesn't start twice in same cron window."""
    cfg = _make_config_with_events([{
        "name": "Once Event",
        "cron": "* * * * *",
        "duration_hours": 2,
        "multiplier": 2.0,
        "presence_bonus": 0,
        "announce": False,
    }])
    mult_engine, mock_presence = _make_deps(cfg, database, mock_client)

    manager = ScheduledEventManager(
        cfg, mult_engine, mock_presence, database, mock_client, logging.getLogger("test"),
    )

    event_cfg = cfg.multipliers.scheduled_events[0]
    now = datetime.now(timezone.utc)

    # First call starts the event
    await manager._check_event(event_cfg, CH, now)
    assert f"{CH}:{event_cfg.name}" in manager._active

    # Second call with same active event should NOT restart
    mult_engine.clear_scheduled_event(CH)
    await manager._check_event(event_cfg, CH, now)

    # The event is still in _active (wasn't re-started)
    assert f"{CH}:{event_cfg.name}" in manager._active
