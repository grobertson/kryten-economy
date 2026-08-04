"""Tests for Sprint 10 — FloatPriceScaler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


from kryten_economy.config import EconomyConfig
from kryten_economy.float_price_scaler import FloatPriceScaler


def make_config(
    enabled: bool = True,
    anchor: int = 100_000_000,
    min_m: float = 0.25,
    max_m: float = 8.0,
) -> EconomyConfig:
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
            "inflation": {
                "enabled": enabled,
                "anchor_float": anchor,
                "min_multiplier": min_m,
                "max_multiplier": max_m,
                "update_interval_seconds": 3600,
            },
        }
    )
    return cfg


def make_db(circulation: int) -> MagicMock:
    db = MagicMock()
    db.get_total_circulation = AsyncMock(return_value=circulation)
    return db


# ── scale() ──────────────────────────────────────────────────────────


async def test_scale_at_anchor_returns_base():
    """float == anchor → multiplier 1.0 → scale returns base cost."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(100_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(10_000) == 10_000


async def test_scale_above_anchor_inflates():
    """float 4× anchor → multiplier 4.0 → scale returns 4× base."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(400_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(100_000) == 400_000


async def test_scale_below_anchor_deflates():
    """float 0.5× anchor → multiplier 0.5 → scale returns 50% of base."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(50_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(10_000) == 5_000


async def test_max_multiplier_cap():
    """float 100× anchor → raw=100.0 → clamped to max_multiplier=8.0."""
    cfg = make_config(anchor=100_000_000, max_m=8.0)
    db = make_db(10_000_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.multiplier == 8.0
    assert scaler.scale(100_000) == 800_000


async def test_min_multiplier_floor():
    """float 0.01× anchor → raw=0.01 → clamped to min_multiplier=0.25."""
    cfg = make_config(anchor=100_000_000, min_m=0.25)
    db = make_db(1_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.multiplier == 0.25
    assert scaler.scale(10_000) == 2_500


async def test_disabled_returns_base():
    """When enabled=False, scale() is a no-op."""
    cfg = make_config(enabled=False)
    db = make_db(999_999_999)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.multiplier == 1.0
    assert scaler.scale(100_000) == 100_000


async def test_scale_always_returns_at_least_one():
    """scale(1) with min_multiplier fractional still returns at least 1."""
    cfg = make_config(anchor=100_000_000, min_m=0.01, max_m=8.0)
    db = make_db(50_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(1) >= 1


async def test_db_failure_retains_last_multiplier():
    """If the DB refresh throws, the previous multiplier is kept."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(200_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()  # multiplier = 2.0
    assert scaler.multiplier == 2.0

    db.get_total_circulation = AsyncMock(side_effect=RuntimeError("DB down"))
    await scaler._refresh()  # should not raise; multiplier unchanged
    assert scaler.multiplier == 2.0


async def test_update_config_recalculates():
    """update_config() with a new anchor recalculates without a new DB read."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(200_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()  # float=200M, anchor=100M → 2.0
    assert scaler.multiplier == 2.0

    new_cfg = make_config(anchor=200_000_000)  # anchor matches float → should be 1.0
    scaler.update_config(new_cfg)
    assert scaler.multiplier == 1.0
