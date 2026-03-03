"""Tests for Sprint 8 Prometheus metrics expansion."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.metrics_collector import MetricsCollector
from kryten_economy.metrics_server import EconomyMetricsServer

CH = "testchannel"


class FakeApp:
    """Minimal stand-in for EconomyApp for metrics tests."""

    def __init__(self, config: EconomyConfig, database: EconomyDatabase):
        self.config = config
        self.db = database
        self.client = MagicMock()
        self.logger = logging.getLogger("test")

        # Shared MetricsCollector with pre-set values
        self.metrics = MetricsCollector()
        self.metrics.events_processed = 42
        self.metrics.commands_processed = 10
        self.metrics.z_earned_total = 5000
        self.metrics.z_spent_total = 2000
        self.metrics.tips_total = 15
        self.metrics.tips_z_total = 300
        self.metrics.queues_total = 3
        self.metrics.vanity_purchases_total = 1
        self.metrics.fortunes_total = 5
        self.metrics.shoutouts_total = 2
        self.metrics.spins_total = 20
        self.metrics.flips_total = 10
        self.metrics.challenges_total = 4
        self.metrics.heists_total = 6
        self.metrics.gambling_z_wagered_total = 800
        self.metrics.gambling_z_won_total = 650
        self.metrics.achievements_awarded_total = 7
        self.metrics.rank_promotions_total = 2
        self.metrics.competition_awards_total = 1
        self.metrics.bounties_created_total = 4
        self.metrics.bounties_claimed_total = 3
        self.metrics.rain_drops_total = 8
        self.metrics.rain_z_distributed_total = 4000

        # Mock presence_tracker
        self.presence_tracker = MagicMock()
        self.presence_tracker.get_connected_count = MagicMock(return_value=5)

        # Mock multiplier engine
        self.multiplier_engine = MagicMock()
        self.multiplier_engine.get_combined_multiplier = MagicMock(return_value=(1.5, []))

        # No pm_handler in minimal fake
        self.pm_handler = None


@pytest.mark.asyncio
async def test_metrics_counters_present(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """All counter metrics in output with HELP/TYPE."""
    await database.get_or_create_account("alice", CH)

    app = FakeApp(sample_config, database)
    server = EconomyMetricsServer(app, port=0)

    lines = await server._collect_custom_metrics()
    text = "\n".join(lines)

    # Original counters
    assert "economy_events_processed_total 42" in text
    assert "economy_commands_processed_total 10" in text
    assert "economy_z_earned_total 5000" in text
    assert "economy_z_spent_total 2000" in text
    assert "economy_tips_total 15" in text
    assert "economy_tips_z_total 300" in text
    assert "economy_queues_total 3" in text
    assert "economy_vanity_purchases_total 1" in text
    assert "economy_achievements_awarded_total 7" in text
    assert "economy_rank_promotions_total 2" in text
    assert "economy_competition_awards_total 1" in text
    assert "economy_bounties_created_total 4" in text
    assert "economy_bounties_claimed_total 3" in text

    # New counters
    assert "economy_fortunes_total 5" in text
    assert "economy_shoutouts_total 2" in text
    assert "economy_gambling_spins_total 20" in text
    assert "economy_gambling_flips_total 10" in text
    assert "economy_gambling_challenges_total 4" in text
    assert "economy_gambling_heists_total 6" in text
    assert "economy_gambling_z_wagered_total 800" in text
    assert "economy_gambling_z_won_total 650" in text
    assert "economy_rain_drops_total 8" in text
    assert "economy_rain_z_distributed_total 4000" in text

    # Every counter should have HELP and TYPE declarations
    for counter_name in [
        "economy_events_processed_total",
        "economy_commands_processed_total",
        "economy_z_earned_total",
        "economy_z_spent_total",
        "economy_tips_total",
        "economy_tips_z_total",
        "economy_queues_total",
        "economy_vanity_purchases_total",
        "economy_fortunes_total",
        "economy_shoutouts_total",
        "economy_gambling_spins_total",
        "economy_gambling_flips_total",
        "economy_gambling_challenges_total",
        "economy_gambling_heists_total",
        "economy_gambling_z_wagered_total",
        "economy_gambling_z_won_total",
        "economy_achievements_awarded_total",
        "economy_rank_promotions_total",
        "economy_competition_awards_total",
        "economy_bounties_created_total",
        "economy_bounties_claimed_total",
        "economy_rain_drops_total",
        "economy_rain_z_distributed_total",
    ]:
        assert f"# HELP {counter_name}" in text, f"missing HELP for {counter_name}"
        assert f"# TYPE {counter_name} counter" in text, f"missing TYPE for {counter_name}"


@pytest.mark.asyncio
async def test_metrics_gauges_present(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """All gauge metrics in output with HELP/TYPE."""
    await database.get_or_create_account("alice", CH)

    app = FakeApp(sample_config, database)
    server = EconomyMetricsServer(app, port=0)

    lines = await server._collect_custom_metrics()
    text = "\n".join(lines)

    gauge_names = [
        "economy_active_users",
        "economy_total_circulation",
        "economy_total_accounts",
        "economy_median_balance",
        "economy_participation_rate",
        "economy_active_multiplier",
        "economy_rank_distribution",
        "economy_daily_z_earned",
        "economy_daily_z_spent",
        "economy_daily_z_gambled_in",
        "economy_daily_z_gambled_out",
        "economy_daily_active_economy_users",
        "economy_gambling_lifetime_wagered",
        "economy_gambling_lifetime_won",
        "economy_gambling_active_gamblers",
        "economy_gambling_total_games",
        "economy_open_bounties",
    ]
    for gauge_name in gauge_names:
        assert f"# HELP {gauge_name}" in text, f"missing HELP for {gauge_name}"
        assert f"# TYPE {gauge_name} gauge" in text, f"missing TYPE for {gauge_name}"


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
