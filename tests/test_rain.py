"""Tests for Sprint 2 — Rain Drops."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.scheduler import Scheduler


@pytest.fixture
def presence(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    return PresenceTracker(
        config=sample_config, database=database, client=mock_client,
        logger=logging.getLogger("test.presence"),
    )


@pytest.fixture
def scheduler(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    presence: PresenceTracker,
    mock_client: MagicMock,
) -> Scheduler:
    return Scheduler(
        config=sample_config,
        database=database,
        presence_tracker=presence,
        client=mock_client,
        logger=logging.getLogger("test.scheduler"),
    )


class TestRain:
    """Rain drop distribution tests."""

    async def test_rain_distributes_to_connected(
        self, scheduler: Scheduler, presence: PresenceTracker, database: EconomyDatabase
    ):
        """Rain should credit all connected users."""
        await presence.handle_user_join("Alice", "testchannel")
        await presence.handle_user_join("Bob", "testchannel")

        with patch("kryten_economy.scheduler.random") as mock_random:
            mock_random.randint.return_value = 15
            await scheduler._execute_rain()

        alice_bal = await database.get_balance("Alice", "testchannel")
        bob_bal = await database.get_balance("Bob", "testchannel")
        # Welcome wallet (100) + rain (15)
        assert alice_bal == 115
        assert bob_bal == 115

    async def test_rain_no_users(self, scheduler: Scheduler, database: EconomyDatabase):
        """Rain with no connected users should be no-op."""
        await scheduler._execute_rain()
        # No error expected

    async def test_rain_pm_notification(
        self, scheduler: Scheduler, presence: PresenceTracker, mock_client: MagicMock
    ):
        """Rain should send PM notifications when enabled."""
        await presence.handle_user_join("Alice", "testchannel")
        mock_client.send_pm.reset_mock()

        with patch("kryten_economy.scheduler.random") as mock_random:
            mock_random.randint.return_value = 10
            await scheduler._execute_rain()

        # PM for rain notification (plus any from join)
        calls = [c for c in mock_client.send_pm.call_args_list if "Rain" in str(c)]
        assert len(calls) >= 1

    async def test_rain_amount_range(self, scheduler: Scheduler):
        """Rain config should define min/max amount."""
        cfg = scheduler._config.rain
        assert cfg.min_amount < cfg.max_amount
        assert cfg.min_amount >= 0

    async def test_rain_transaction_logged(
        self, scheduler: Scheduler, presence: PresenceTracker, database: EconomyDatabase
    ):
        """Rain should log transactions with type 'rain'."""
        await presence.handle_user_join("Alice", "testchannel")
        with patch("kryten_economy.scheduler.random") as mock_random:
            mock_random.randint.return_value = 20
            await scheduler._execute_rain()

        import sqlite3
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        rain_tx = conn.execute(
            "SELECT * FROM transactions WHERE type = 'rain' AND username = 'Alice'"
        ).fetchone()
        conn.close()
        assert rain_tx is not None
        assert rain_tx["amount"] == 20
