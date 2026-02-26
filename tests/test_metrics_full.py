"""Tests for Sprint 8 Prometheus metrics expansion."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.metrics_server import EconomyMetricsServer

CH = "testchannel"


class FakeApp:
    """Minimal stand-in for EconomyApp for metrics tests."""

    def __init__(self, config: EconomyConfig, database: EconomyDatabase):
        self.config = config
        self.db = database
        self.client = MagicMock()
        self.logger = logging.getLogger("test")
        self.events_processed = 42
        self.commands_processed = 10
        self.z_earned_total = 5000
        self.z_spent_total = 2000
        self.tips_total = 15
        self.queues_total = 3
        self.vanity_purchases_total = 1
        self.achievements_awarded_total = 7
        self.rank_promotions_total = 2
        self.competition_awards_total = 1
        self.bounties_created_total = 4
        self.bounties_claimed_total = 3

        # Mock presence_tracker
        self.presence_tracker = MagicMock()
        self.presence_tracker.get_connected_count = MagicMock(return_value=5)

        # Mock multiplier engine
        self.multiplier_engine = MagicMock()
        self.multiplier_engine.get_combined_multiplier = MagicMock(return_value=(1.5, []))


@pytest.mark.asyncio
async def test_metrics_counters_present(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """All counter metrics in output."""
    await database.get_or_create_account("alice", CH)

    app = FakeApp(sample_config, database)
    server = EconomyMetricsServer(app, port=0)

    lines = await server._collect_custom_metrics()
    text = "\n".join(lines)

    assert "economy_events_processed_total 42" in text
    assert "economy_commands_processed_total 10" in text
    assert "economy_z_earned_total 5000" in text
    assert "economy_z_spent_total 2000" in text
    assert "economy_tips_total 15" in text
    assert "economy_queues_total 3" in text
    assert "economy_vanity_purchases_total 1" in text
    assert "economy_achievements_awarded_total 7" in text
    assert "economy_rank_promotions_total 2" in text
    assert "economy_competition_awards_total 1" in text
    assert "economy_bounties_created_total 4" in text
    assert "economy_bounties_claimed_total 3" in text


@pytest.mark.asyncio
async def test_metrics_gauges_present(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """All gauge metrics in output."""
    await database.get_or_create_account("alice", CH)

    app = FakeApp(sample_config, database)
    server = EconomyMetricsServer(app, port=0)

    lines = await server._collect_custom_metrics()
    text = "\n".join(lines)

    assert "economy_active_users" in text
    assert "economy_total_circulation" in text
    assert "economy_total_accounts" in text
    assert "economy_median_balance" in text
    assert "economy_participation_rate" in text
    assert "economy_active_multiplier" in text


@pytest.mark.asyncio
async def test_metrics_by_channel(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Channel label on per-channel gauges."""
    await database.get_or_create_account("alice", CH)

    app = FakeApp(sample_config, database)
    server = EconomyMetricsServer(app, port=0)

    lines = await server._collect_custom_metrics()
    text = "\n".join(lines)

    assert f'channel="{CH}"' in text


@pytest.mark.asyncio
async def test_metrics_rank_distribution(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Rank labels on distribution gauge."""
    await database.get_or_create_account("alice", CH)

    app = FakeApp(sample_config, database)
    server = EconomyMetricsServer(app, port=0)

    lines = await server._collect_custom_metrics()
    text = "\n".join(lines)

    # Should have rank distribution metric with rank label
    assert "economy_rank_distribution" in text
