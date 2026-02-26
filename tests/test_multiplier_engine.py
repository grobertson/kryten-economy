"""Tests for MultiplierEngine — Sprint 7."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.multiplier_engine import ActiveMultiplier, MultiplierEngine
from tests.conftest import make_config_dict

CH = "testchannel"


def _make_engine(
    config: EconomyConfig | None = None,
    connected_users: set | None = None,
) -> MultiplierEngine:
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(
        return_value=connected_users or set(),
    )
    cfg = config or EconomyConfig(**make_config_dict())
    return MultiplierEngine(cfg, mock_presence, logging.getLogger("test"))


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_multipliers():
    """Normal time → combined = 1.0, empty list."""
    # Use a config with multipliers all disabled
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    combined, active = engine.get_combined_multiplier(CH)
    assert combined == 1.0
    assert active == []


@pytest.mark.asyncio
async def test_off_peak_active():
    """During off-peak hours → 2.0×."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": True, "days": [0, 1, 2, 3, 4, 5, 6], "hours": list(range(24)), "multiplier": 2.0},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    combined, active = engine.get_combined_multiplier(CH)
    assert combined == 2.0
    off_peak = [m for m in active if m.source == "off_peak"]
    assert len(off_peak) == 1


@pytest.mark.asyncio
async def test_off_peak_inactive():
    """Outside off-peak → not in list."""
    # Set off-peak to only day 6 (invalid for most test runs)
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": True, "days": [], "hours": [], "multiplier": 2.0},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    combined, active = engine.get_combined_multiplier(CH)
    off_peak = [m for m in active if m.source == "off_peak"]
    assert len(off_peak) == 0


@pytest.mark.asyncio
async def test_population_active():
    """10+ users → 1.5×."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": True, "min_users": 10, "multiplier": 1.5, "hidden": True},
        "holidays": {"enabled": False},
    }))
    users = {f"user{i}" for i in range(12)}
    engine = _make_engine(cfg, users)

    combined, active = engine.get_combined_multiplier(CH)
    pop = [m for m in active if m.source == "population"]
    assert len(pop) == 1
    assert pop[0].multiplier == 1.5
    assert pop[0].hidden is True


@pytest.mark.asyncio
async def test_population_below():
    """5 users → not in list."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": True, "min_users": 10, "multiplier": 1.5},
        "holidays": {"enabled": False},
    }))
    users = {f"user{i}" for i in range(5)}
    engine = _make_engine(cfg, users)

    combined, active = engine.get_combined_multiplier(CH)
    pop = [m for m in active if m.source == "population"]
    assert len(pop) == 0


@pytest.mark.asyncio
async def test_holiday_match():
    """Dec 25 → 3.0× Christmas."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": True, "dates": [{"date": "12-25", "name": "Christmas", "multiplier": 3.0}]},
    }))
    engine = _make_engine(cfg)

    # Mock the current date to be Dec 25
    fake_now = datetime(2025, 12, 25, 14, 0, 0, tzinfo=timezone.utc)
    with patch("kryten_economy.multiplier_engine.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        combined, active = engine.get_combined_multiplier(CH)

    holiday = [m for m in active if "Christmas" in m.source]
    assert len(holiday) == 1
    assert holiday[0].multiplier == 3.0


@pytest.mark.asyncio
async def test_holiday_no_match():
    """Regular day → no holiday."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": True, "dates": [{"date": "12-25", "name": "Christmas", "multiplier": 3.0}]},
    }))
    engine = _make_engine(cfg)

    fake_now = datetime(2025, 7, 15, 14, 0, 0, tzinfo=timezone.utc)
    with patch("kryten_economy.multiplier_engine.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        combined, active = engine.get_combined_multiplier(CH)

    holiday = [m for m in active if "holiday" in m.source]
    assert len(holiday) == 0


@pytest.mark.asyncio
async def test_scheduled_event_active():
    """Registered event not expired → in list."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    end_time = datetime.now(timezone.utc) + timedelta(hours=2)
    engine.set_scheduled_event(CH, "Movie Night", 2.0, end_time)

    combined, active = engine.get_combined_multiplier(CH)
    sched = [m for m in active if "Movie Night" in m.source]
    assert len(sched) == 1
    assert sched[0].multiplier == 2.0


@pytest.mark.asyncio
async def test_scheduled_event_expired():
    """Past end_time → auto-cleared."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    end_time = datetime.now(timezone.utc) - timedelta(hours=1)
    engine.set_scheduled_event(CH, "Expired Event", 2.0, end_time)

    combined, active = engine.get_combined_multiplier(CH)
    sched = [m for m in active if "Expired" in m.source]
    assert len(sched) == 0


@pytest.mark.asyncio
async def test_adhoc_event_active():
    """Admin-started event → in list."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    engine.start_adhoc_event("Triple Z Friday", 3.0, 120)

    combined, active = engine.get_combined_multiplier(CH)
    adhoc = [m for m in active if "Triple Z Friday" in m.source]
    assert len(adhoc) == 1
    assert adhoc[0].multiplier == 3.0


@pytest.mark.asyncio
async def test_adhoc_event_expired():
    """Past end_time → auto-cleared."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    }))
    engine = _make_engine(cfg)

    engine._adhoc_event = {
        "name": "Old Event",
        "multiplier": 2.0,
        "end_time": datetime.now(timezone.utc) - timedelta(hours=1),
    }

    combined, active = engine.get_combined_multiplier(CH)
    adhoc = [m for m in active if "Old Event" in m.source]
    assert len(adhoc) == 0
    assert engine._adhoc_event is None


@pytest.mark.asyncio
async def test_stacking_multiplicative():
    """off_peak 2.0 × population 1.5 = 3.0."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": True, "days": list(range(7)), "hours": list(range(24)), "multiplier": 2.0},
        "high_population": {"enabled": True, "min_users": 2, "multiplier": 1.5},
        "holidays": {"enabled": False},
    }))
    users = {f"user{i}" for i in range(5)}
    engine = _make_engine(cfg, users)

    combined, active = engine.get_combined_multiplier(CH)
    assert abs(combined - 3.0) < 0.01
    assert len(active) == 2


@pytest.mark.asyncio
async def test_hidden_not_shown_in_events_cmd():
    """Hidden multiplier has hidden=True which the PM handler filters."""
    cfg = EconomyConfig(**make_config_dict(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": True, "min_users": 1, "multiplier": 1.5, "hidden": True},
        "holidays": {"enabled": False},
    }))
    users = {"user1", "user2"}
    engine = _make_engine(cfg, users)

    combined, active = engine.get_combined_multiplier(CH)
    hidden = [m for m in active if m.hidden]
    assert len(hidden) == 1
    assert hidden[0].source == "population"
