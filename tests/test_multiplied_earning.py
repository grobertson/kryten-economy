"""Tests for earning path with multipliers applied — Sprint 7 integration.

These tests verify that the multiplier-aware earning path correctly
scales base amounts by the active multiplier stack, logs metadata,
and updates daily_activity.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.multiplier_engine import MultiplierEngine
from tests.conftest import make_config_dict

CH = "testchannel"


def _make_config(**overrides) -> EconomyConfig:
    return EconomyConfig(**make_config_dict(**overrides))


def _make_engine(config: EconomyConfig, connected_users: int = 0) -> MultiplierEngine:
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(
        return_value=set(f"user{i}" for i in range(connected_users)),
    )
    return MultiplierEngine(config, mock_presence, logging.getLogger("test"))


async def _seed_account(db: EconomyDatabase, username: str, balance: int) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="seed", reason="test seed")


async def _credit_with_multiplier(
    db: EconomyDatabase,
    mult_engine: MultiplierEngine,
    username: str,
    channel: str,
    base_amount: int,
    tx_type: str = "earn",
    trigger_id: str = "test.trigger",
    reason: str = "test credit",
) -> tuple[int, dict | None]:
    """Simulate multiplier-aware credit. Returns (final_amount, metadata)."""
    combined, active = mult_engine.get_combined_multiplier(channel)
    final_amount = int(base_amount * combined)

    meta = None
    if combined > 1.0:
        meta = {
            "base": base_amount,
            "multiplier": combined,
            "sources": [{"source": m.source, "mult": m.multiplier} for m in active],
        }

    meta_str = json.dumps(meta) if meta else None

    await db.credit(
        username, channel, final_amount,
        tx_type=tx_type,
        trigger_id=trigger_id,
        reason=reason,
        metadata=meta_str,
    )

    # Update daily z_earned
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await db.increment_daily_z_earned(username, channel, today, final_amount)

    return final_amount, meta


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_earn_with_2x_multiplier(database: EconomyDatabase):
    """Base 5 × 2.0 = 10 Z credited."""
    cfg = _make_config(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    })
    mult = _make_engine(cfg)
    # Start an adhoc event with 2×
    mult.start_adhoc_event("Double Time", 2.0, 60)

    await _seed_account(database, "Alice", 0)
    final, _meta = await _credit_with_multiplier(
        database, mult, "Alice", CH, 5,
    )

    assert final == 10
    acc = await database.get_account("Alice", CH)
    assert acc["balance"] == 10


@pytest.mark.asyncio
async def test_earn_with_stacked_3x(database: EconomyDatabase):
    """Base 5 with 2.0 × 1.5 = 3.0× → 15 Z credited."""
    cfg = _make_config(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": True, "min_users": 2, "multiplier": 1.5, "hidden": False},
        "holidays": {"enabled": False},
    })
    mult = _make_engine(cfg, connected_users=5)  # triggers population multiplier
    mult.start_adhoc_event("Double Time", 2.0, 60)

    await _seed_account(database, "Alice", 0)
    final, _meta = await _credit_with_multiplier(
        database, mult, "Alice", CH, 5,
    )

    assert final == 15  # 5 × 2.0 × 1.5 = 15
    acc = await database.get_account("Alice", CH)
    assert acc["balance"] == 15


@pytest.mark.asyncio
async def test_earn_no_multiplier(database: EconomyDatabase):
    """Base 5 × 1.0 = 5 Z credited."""
    cfg = _make_config(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    })
    mult = _make_engine(cfg)

    await _seed_account(database, "Alice", 0)
    final, meta = await _credit_with_multiplier(
        database, mult, "Alice", CH, 5,
    )

    assert final == 5
    assert meta is None  # No multiplier → no metadata


@pytest.mark.asyncio
async def test_multiplier_metadata_logged(database: EconomyDatabase):
    """Transaction metadata contains multiplier sources."""
    cfg = _make_config(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    })
    mult = _make_engine(cfg)
    mult.start_adhoc_event("Bonus Night", 2.5, 60)

    await _seed_account(database, "Alice", 0)
    final, meta = await _credit_with_multiplier(
        database, mult, "Alice", CH, 10,
    )

    assert final == 25  # 10 × 2.5
    assert meta is not None
    assert meta["base"] == 10
    assert meta["multiplier"] == 2.5
    assert len(meta["sources"]) == 1
    assert "Bonus Night" in meta["sources"][0]["source"]


@pytest.mark.asyncio
async def test_daily_z_earned_updated(database: EconomyDatabase):
    """Multiplied amount reflected in daily_activity."""
    cfg = _make_config(multipliers={
        "off_peak": {"enabled": False},
        "high_population": {"enabled": False},
        "holidays": {"enabled": False},
    })
    mult = _make_engine(cfg)
    mult.start_adhoc_event("Double Night", 2.0, 60)

    await _seed_account(database, "Alice", 0)
    final, _meta = await _credit_with_multiplier(
        database, mult, "Alice", CH, 5,
    )

    assert final == 10

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_activity = await database.get_daily_activity_all(CH, today)
    alice_activity = [a for a in all_activity if a["username"] == "Alice"]
    assert len(alice_activity) == 1
    assert alice_activity[0]["z_earned"] == 10
