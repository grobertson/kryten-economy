"""Tests for kryten_economy.config module."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from kryten_economy.config import (
    CurrencyConfig,
    DatabaseConfig,
    EconomyConfig,
    NightWatchConfig,
    PresenceConfig,
    load_config,
)


class TestEconomyConfig:
    """Test EconomyConfig model parsing and validation."""

    def test_minimal_config(self, sample_config_dict: dict):
        """Config with only required fields (nats, channels) should parse."""
        cfg = EconomyConfig(
            nats={"servers": ["nats://localhost:4222"]},
            channels=[{"domain": "cytu.be", "channel": "test"}],
        )
        assert cfg.database.path == "economy.db"
        assert cfg.currency.name == "Z-Coin"
        assert cfg.bot.username == "ZCoinBot"

    def test_full_config(self, sample_config_dict: dict):
        """Full config dict should parse correctly."""
        cfg = EconomyConfig(**sample_config_dict)
        assert cfg.currency.symbol == "Z"
        assert cfg.onboarding.welcome_wallet == 100
        assert cfg.presence.base_rate_per_minute == 1
        assert len(cfg.channels) == 1
        assert cfg.channels[0].channel == "testchannel"

    def test_defaults_applied(self):
        """Defaults should be applied for optional sections."""
        cfg = EconomyConfig(
            nats={"servers": ["nats://localhost:4222"]},
            channels=[{"domain": "cytu.be", "channel": "t"}],
        )
        assert cfg.presence.join_debounce_minutes == 5
        assert cfg.streaks.daily.enabled is True
        assert cfg.rain.interval_minutes == 45

    def test_night_watch_config(self):
        """NightWatchConfig should parse hours list and multiplier."""
        nw = NightWatchConfig(enabled=True, hours=[0, 1, 2], multiplier=2.0)
        assert nw.enabled is True
        assert nw.hours == [0, 1, 2]
        assert nw.multiplier == 2.0

    def test_night_watch_defaults(self):
        """NightWatchConfig defaults should match master plan."""
        nw = NightWatchConfig()
        assert nw.enabled is False
        assert nw.hours == [2, 3, 4, 5, 6, 7]
        assert nw.multiplier == 1.5

    def test_currency_config(self):
        """CurrencyConfig should handle custom values."""
        cc = CurrencyConfig(name="TacoBucks", symbol="T", plural="TacoBucks")
        assert cc.name == "TacoBucks"
        assert cc.symbol == "T"

    def test_ignored_users_default_empty(self):
        """ignored_users should default to empty list."""
        cfg = EconomyConfig(
            nats={"servers": ["nats://localhost:4222"]},
            channels=[{"domain": "cytu.be", "channel": "t"}],
        )
        assert cfg.ignored_users == []

    def test_balance_maintenance_modes(self):
        """Balance maintenance should accept interest/decay/none modes."""
        cfg = EconomyConfig(
            nats={"servers": ["nats://localhost:4222"]},
            channels=[{"domain": "cytu.be", "channel": "t"}],
            balance_maintenance={"mode": "none"},
        )
        assert cfg.balance_maintenance.mode == "none"


class TestLoadConfig:
    """Test YAML file loading with environment variable expansion."""

    def test_load_valid_yaml(self, sample_config_dict: dict, tmp_path: Path):
        """Load a valid YAML config file."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))

        cfg = load_config(str(config_path))
        assert cfg.currency.symbol == "Z"
        assert len(cfg.channels) == 1

    def test_load_missing_file(self):
        """Loading a nonexistent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_env_var_expansion(self, sample_config_dict: dict, tmp_path: Path):
        """Environment variables in ${VAR} format should be expanded."""
        os.environ["TEST_DB_PATH"] = "/tmp/test.db"
        sample_config_dict["database"] = {"path": "${TEST_DB_PATH}"}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))

        cfg = load_config(str(config_path))
        assert cfg.database.path == "/tmp/test.db"
        del os.environ["TEST_DB_PATH"]

    def test_env_var_with_default(self, sample_config_dict: dict, tmp_path: Path):
        """${VAR:-default} should use default when VAR is unset."""
        # Ensure the variable is unset
        os.environ.pop("UNSET_TEST_VAR", None)
        sample_config_dict["database"] = {"path": "${UNSET_TEST_VAR:-fallback.db}"}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))

        cfg = load_config(str(config_path))
        assert cfg.database.path == "fallback.db"

    def test_invalid_yaml_structure(self, tmp_path: Path):
        """Non-mapping YAML should raise ValueError."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("- just\n- a\n- list\n")

        with pytest.raises(ValueError, match="YAML mapping"):
            load_config(str(config_path))
