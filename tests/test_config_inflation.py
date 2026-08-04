"""Tests for Sprint 10 — InflationConfig and EconomyConfig inflation field."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kryten_economy.config import EconomyConfig, InflationConfig


def test_defaults():
    cfg = InflationConfig()
    assert cfg.enabled is False
    assert cfg.anchor_float == 100_000_000
    assert cfg.min_multiplier == 0.25
    assert cfg.max_multiplier == 8.0
    assert cfg.update_interval_seconds == 3600


def test_min_exceeds_max_raises():
    with pytest.raises(ValidationError):
        InflationConfig(min_multiplier=5.0, max_multiplier=2.0)


def test_economy_config_includes_inflation():
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
        }
    )
    assert hasattr(cfg, "inflation")
    assert isinstance(cfg.inflation, InflationConfig)


def test_inflation_loaded_from_yaml_dict():
    """EconomyConfig can be constructed with an inline inflation dict (simulates YAML load)."""
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
            "inflation": {"enabled": True, "anchor_float": 80_000_000, "max_multiplier": 10.0},
        }
    )
    assert cfg.inflation.enabled is True
    assert cfg.inflation.anchor_float == 80_000_000
    assert cfg.inflation.max_multiplier == 10.0


def test_inflation_none_coercion():
    """YAML `inflation:` with all sub-keys commented out parses as None → default."""
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
            "inflation": None,
        }
    )
    assert isinstance(cfg.inflation, InflationConfig)
    assert cfg.inflation.enabled is False


def test_economy_config_defaults_inflation_disabled():
    """EconomyConfig() with no args has inflation.enabled = False."""
    cfg = EconomyConfig(
        **{
            "nats": {"servers": ["nats://localhost:4222"]},
            "channels": [{"domain": "cytu.be", "channel": "test"}],
            "service": {"name": "economy"},
            "database": {"path": ":memory:"},
        }
    )
    assert cfg.inflation.enabled is False
