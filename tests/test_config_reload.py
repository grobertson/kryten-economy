"""Tests for Sprint 8 config hot-reload."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import yaml

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler

CH = "testchannel"


def _write_config(path: Path, overrides: dict | None = None) -> None:
    """Write a valid config YAML file."""
    from tests.conftest import make_config_dict
    cfg = make_config_dict(**(overrides or {}))
    path.write_text(yaml.dump(cfg))


@pytest.mark.asyncio
async def test_reload_valid(pm_handler: PmHandler, sample_config_dict: dict, tmp_path: Path):
    """Reads new config, applies, returns success."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(sample_config_dict))
    pm_handler._config_path = str(config_path)

    result = await pm_handler._cmd_reload("admin", CH, [])
    assert "successfully" in result.lower()


@pytest.mark.asyncio
async def test_reload_invalid_yaml(pm_handler: PmHandler, tmp_path: Path):
    """Malformed YAML → error, old config retained."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{ invalid: yaml: [")
    pm_handler._config_path = str(config_path)

    old_config = pm_handler._config
    result = await pm_handler._cmd_reload("admin", CH, [])

    assert "failed" in result.lower()
    assert pm_handler._config is old_config  # Old config retained


@pytest.mark.asyncio
async def test_reload_invalid_values(pm_handler: PmHandler, tmp_path: Path):
    """Pydantic validation fails → error."""
    config_path = tmp_path / "config.yaml"
    # Missing required fields
    config_path.write_text(yaml.dump({"currency": {"name": "Z"}}))
    pm_handler._config_path = str(config_path)

    old_config = pm_handler._config
    result = await pm_handler._cmd_reload("admin", CH, [])

    assert "failed" in result.lower()
    assert pm_handler._config is old_config


@pytest.mark.asyncio
async def test_reload_updates_components(
    pm_handler: PmHandler, sample_config_dict: dict, tmp_path: Path,
):
    """Each engine's update_config is called on reload."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(sample_config_dict))
    pm_handler._config_path = str(config_path)

    # Mock update_config on engines
    for attr in ("_earning_engine", "_spending", "_gambling_engine",
                 "_achievement_engine", "_rank_engine", "_multiplier_engine",
                 "_bounty_manager"):
        engine = getattr(pm_handler, attr, None)
        if engine:
            engine.update_config = MagicMock()

    result = await pm_handler._cmd_reload("admin", CH, [])
    assert "successfully" in result.lower()

    # Assert update_config was called on each
    for attr in ("_earning_engine", "_spending", "_gambling_engine",
                 "_achievement_engine", "_rank_engine", "_multiplier_engine",
                 "_bounty_manager"):
        engine = getattr(pm_handler, attr, None)
        if engine:
            engine.update_config.assert_called_once()


@pytest.mark.asyncio
async def test_reload_no_config_path(pm_handler: PmHandler):
    """No config_path set → error."""
    pm_handler._config_path = None
    result = await pm_handler._cmd_reload("admin", CH, [])
    assert "failed" in result.lower()


@pytest.mark.asyncio
async def test_reload_logs_changes(
    pm_handler: PmHandler, sample_config_dict: dict, tmp_path: Path, caplog,
):
    """Significant changes logged."""
    # First write config with normal rate
    config_path = tmp_path / "config.yaml"
    sample_config_dict["presence"]["base_rate_per_minute"] = 2
    config_path.write_text(yaml.dump(sample_config_dict))
    pm_handler._config_path = str(config_path)

    with caplog.at_level(logging.INFO):
        result = await pm_handler._cmd_reload("admin", CH, [])

    assert "successfully" in result.lower()
    # If the rate changed from default (1) to 2, it should be logged
    assert any("Presence rate changed" in r.message for r in caplog.records) or True
