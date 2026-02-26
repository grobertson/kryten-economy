"""Sprint 9 — Deployment tests.

Tests:
- config.example.yaml parses without errors
- config.example.yaml has all expected sections
- systemd service file has required sections
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
import yaml

from kryten_economy.config import EconomyConfig


# Resolve project root (tests/ is one level inside)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestConfigExample:
    """Validate config.example.yaml against the Pydantic schema."""

    def test_config_example_valid(self) -> None:
        """config.example.yaml parses without errors into EconomyConfig."""
        config_path = PROJECT_ROOT / "config.example.yaml"
        assert config_path.exists(), f"Missing {config_path}"

        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # Should not raise any validation errors
        config = EconomyConfig(**raw)
        assert config.currency.symbol == "Z"

    def test_config_example_all_sections(self) -> None:
        """All expected top-level sections are present."""
        config_path = PROJECT_ROOT / "config.example.yaml"
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        expected_sections = {
            "nats", "channels", "service", "database", "currency",
            "presence", "announcements", "admin",
        }
        for section in expected_sections:
            assert section in raw, f"Missing config section: {section}"

    def test_config_example_commands_section(self) -> None:
        """Sprint 9 commands section is present."""
        config_path = PROJECT_ROOT / "config.example.yaml"
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        assert "commands" in raw, "Missing Sprint 9 'commands' section"
        assert "rate_limit_per_minute" in raw["commands"]

    def test_config_example_metrics_section(self) -> None:
        """Metrics section is present with port."""
        config_path = PROJECT_ROOT / "config.example.yaml"
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        assert "metrics" in raw, "Missing 'metrics' section"
        assert "port" in raw["metrics"]


class TestSystemdService:
    """Validate systemd service file structure."""

    def test_systemd_unit_syntax(self) -> None:
        """Service file has required [Unit], [Service], [Install] sections."""
        service_path = PROJECT_ROOT / "systemd" / "kryten-economy.service"
        assert service_path.exists(), f"Missing {service_path}"

        content = service_path.read_text()

        # Must have all three standard systemd sections
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content

    def test_systemd_restart_policy(self) -> None:
        """Service has restart-on-failure policy."""
        service_path = PROJECT_ROOT / "systemd" / "kryten-economy.service"
        content = service_path.read_text()

        assert "Restart=on-failure" in content

    def test_systemd_exec_start(self) -> None:
        """Service has an ExecStart directive."""
        service_path = PROJECT_ROOT / "systemd" / "kryten-economy.service"
        content = service_path.read_text()

        assert "ExecStart=" in content

    def test_systemd_resource_limits(self) -> None:
        """Service has Sprint-9 resource limits."""
        service_path = PROJECT_ROOT / "systemd" / "kryten-economy.service"
        content = service_path.read_text()

        assert "MemoryMax=" in content
        assert "CPUQuota=" in content

    def test_systemd_security_hardening(self) -> None:
        """Service has basic security hardening."""
        service_path = PROJECT_ROOT / "systemd" / "kryten-economy.service"
        content = service_path.read_text()

        assert "NoNewPrivileges=true" in content
        assert "ProtectSystem=" in content
