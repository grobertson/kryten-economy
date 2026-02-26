"""Tests for kryten_economy.metrics_server module."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.metrics_server import EconomyMetricsServer
from kryten_economy.presence_tracker import PresenceTracker


@pytest.fixture
def mock_app(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> MagicMock:
    """Mock EconomyApp with real database."""
    app = MagicMock()
    app.config = sample_config
    app.db = database
    app.client = mock_client
    app.logger = logging.getLogger("test.app")
    app.events_processed = 42
    app.commands_processed = 7
    type(app).z_earned_total = PropertyMock(return_value=1000)
    app.presence_tracker = PresenceTracker(
        config=sample_config,
        database=database,
        client=mock_client,
        logger=logging.getLogger("test.presence"),
    )
    # Sprint 8 expanded metrics need these attributes
    app.z_spent_total = 0
    app.tips_total = 0
    app.queues_total = 0
    app.vanity_purchases_total = 0
    app.achievements_awarded_total = 0
    app.rank_promotions_total = 0
    app.competition_awards_total = 0
    app.bounties_created_total = 0
    app.bounties_claimed_total = 0
    # multiplier_engine must return a tuple from get_combined_multiplier
    mult = MagicMock()
    mult.get_combined_multiplier.return_value = (1.0, [])
    app.multiplier_engine = mult
    return app


@pytest.fixture
def metrics_server(mock_app: MagicMock) -> EconomyMetricsServer:
    """Create EconomyMetricsServer."""
    return EconomyMetricsServer(mock_app, port=28286)


class TestMetricsServer:
    """Metrics collection tests."""

    async def test_collect_custom_metrics(self, metrics_server: EconomyMetricsServer, database: EconomyDatabase):
        """_collect_custom_metrics should return Prometheus lines."""
        await database.credit("alice", "testchannel", 100, "earn")
        await database.credit("bob", "testchannel", 200, "earn")

        lines = await metrics_server._collect_custom_metrics()
        assert any("economy_active_users" in line for line in lines)
        assert any("economy_total_circulation" in line for line in lines)
        assert any("economy_total_accounts" in line for line in lines)
        assert any("economy_events_processed_total 42" in line for line in lines)
        assert any("economy_commands_processed_total 7" in line for line in lines)
        assert any("economy_z_earned_total 1000" in line for line in lines)

    async def test_get_health_details(self, metrics_server: EconomyMetricsServer):
        """_get_health_details should return health dict."""
        details = await metrics_server._get_health_details()
        assert details["database"] == "connected"
        assert "channels_configured" in details
        assert "active_sessions" in details

    async def test_metrics_include_circulation(self, metrics_server: EconomyMetricsServer, database: EconomyDatabase):
        """Circulation metric should reflect actual database state."""
        await database.credit("user1", "testchannel", 500, "earn")
        lines = await metrics_server._collect_custom_metrics()
        circ_lines = [l for l in lines if "economy_total_circulation" in l]
        assert len(circ_lines) >= 1
        assert "500" in circ_lines[0]
