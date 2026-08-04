"""Tests for Sprint 10 — SpendingEngine inflation methods."""

from __future__ import annotations

from unittest.mock import MagicMock


from kryten_economy.config import EconomyConfig
from kryten_economy.float_price_scaler import FloatPriceScaler
from kryten_economy.spending_engine import SpendingEngine


def make_scaler(multiplier: float, enabled: bool = True) -> MagicMock:
    scaler = MagicMock(spec=FloatPriceScaler)
    scaler.enabled = enabled
    scaler.multiplier = multiplier
    scaler.scale = MagicMock(
        side_effect=lambda base: max(1, round(base * multiplier)) if enabled else base
    )
    return scaler


def make_engine(multiplier: float = 1.0, enabled: bool = True) -> SpendingEngine:
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
            "inflation": {"enabled": enabled, "anchor_float": 100_000_000},
            "spending": {
                "queue_tiers": [
                    {"max_minutes": 15, "label": "Short", "cost": 25_000},
                    {"max_minutes": 999, "label": "Movie", "cost": 100_000},
                ],
                "interrupt_play_next": 1_000_000,
                "force_play_now": 10_000_000,
            },
        }
    )
    scaler = make_scaler(multiplier, enabled)
    return SpendingEngine(cfg, MagicMock(), None, MagicMock(), price_scaler=scaler)


def test_get_inflated_price_applies_multiplier():
    engine = make_engine(multiplier=2.0)
    assert engine.get_inflated_price(100_000) == 200_000


def test_get_inflated_price_disabled_is_passthrough():
    engine = make_engine(multiplier=5.0, enabled=False)
    assert engine.get_inflated_price(100_000) == 100_000


def test_get_effective_price_tier_returns_three_tuple():
    engine = make_engine(multiplier=3.0)
    label, base, effective = engine.get_effective_price_tier(30 * 60)  # 30 min → Movie tier
    assert label == "Movie"
    assert base == 100_000
    assert effective == 300_000


def test_interrupt_price_inflated():
    engine = make_engine(multiplier=4.0)
    assert engine.get_interrupt_play_next_price() == 4_000_000


def test_force_play_price_inflated():
    engine = make_engine(multiplier=4.0)
    assert engine.get_force_play_now_price() == 40_000_000


def test_no_scaler_returns_base():
    """When price_scaler is None, all methods return base prices unchanged."""
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
            "spending": {
                "queue_tiers": [
                    {"max_minutes": 15, "label": "Short", "cost": 25_000},
                    {"max_minutes": 999, "label": "Movie", "cost": 100_000},
                ],
                "interrupt_play_next": 1_000_000,
                "force_play_now": 10_000_000,
            },
        }
    )
    engine = SpendingEngine(cfg, MagicMock(), None, MagicMock(), price_scaler=None)
    assert engine.get_inflated_price(100_000) == 100_000
    assert engine.get_interrupt_play_next_price() == 1_000_000
    assert engine.get_force_play_now_price() == 10_000_000


def test_rank_discount_applied_after_inflation():
    """Rank discount is applied on the inflated price, not base."""
    engine = make_engine(multiplier=2.0)
    inflated = engine.get_inflated_price(100_000)
    assert inflated == 200_000
    # Any non-zero rank tier will reduce from the inflated price
    final, discount = engine.apply_discount(inflated, rank_tier_index=0)
    assert final == 200_000  # tier 0 = no discount
    assert discount == 0.0
