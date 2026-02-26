"""Tests for Sprint 2 — Balance Maintenance (Interest / Decay)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.scheduler import Scheduler

from conftest import make_config_dict


@pytest.fixture
def presence(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    return PresenceTracker(
        config=sample_config, database=database, client=mock_client,
        logger=logging.getLogger("test.presence"),
    )


def make_scheduler(config: EconomyConfig, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock) -> Scheduler:
    return Scheduler(
        config=config, database=database, presence_tracker=presence,
        client=mock_client, logger=logging.getLogger("test.maint"),
    )


class TestInterest:
    """Interest mode balance maintenance."""

    async def test_interest_applied(self, sample_config: EconomyConfig, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock):
        """Interest should be applied to accounts above min_balance."""
        # Create accounts: one qualifying, one below min
        await database.credit("rich", "testchannel", 10000, "earn")
        await database.credit("poor", "testchannel", 50, "earn")

        sched = make_scheduler(sample_config, database, presence, mock_client)
        await sched._execute_balance_maintenance()

        rich_bal = await database.get_balance("rich", "testchannel")
        poor_bal = await database.get_balance("poor", "testchannel")

        # rich: 10000 * 0.001 = 10 interest (capped at 10)
        assert rich_bal == 10010
        # poor: below min_balance_to_earn (100), no interest
        assert poor_bal == 50

    async def test_interest_cap(self, sample_config: EconomyConfig, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock):
        """Interest should be capped at max_daily_interest."""
        # 1M * 0.001 = 1000, but cap is 10
        await database.credit("whale", "testchannel", 1000000, "earn")

        sched = make_scheduler(sample_config, database, presence, mock_client)
        await sched._execute_balance_maintenance()

        bal = await database.get_balance("whale", "testchannel")
        assert bal == 1000010  # Only 10 interest despite huge balance

    async def test_interest_transaction_logged(self, sample_config: EconomyConfig, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock):
        """Interest should be logged as transaction."""
        await database.credit("rich", "testchannel", 5000, "earn")

        sched = make_scheduler(sample_config, database, presence, mock_client)
        await sched._execute_balance_maintenance()

        import sqlite3
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        tx = conn.execute(
            "SELECT * FROM transactions WHERE type = 'interest' AND username = 'rich'"
        ).fetchone()
        conn.close()
        assert tx is not None
        assert tx["amount"] > 0


class TestDecay:
    """Decay mode balance maintenance."""

    async def test_decay_applied(self, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock):
        """Decay should reduce accounts above exempt_below."""
        d = make_config_dict()
        d["balance_maintenance"] = {
            "mode": "decay",
            "decay": {"enabled": True, "daily_rate": 0.01, "exempt_below": 1000},
        }
        cfg = EconomyConfig(**d)

        # Create accounts: one exempt, one qualifying
        await database.credit("whale", "testchannel", 10000, "earn")
        await database.credit("small", "testchannel", 500, "earn")

        sched = make_scheduler(cfg, database, presence, mock_client)
        await sched._execute_balance_maintenance()

        whale_bal = await database.get_balance("whale", "testchannel")
        small_bal = await database.get_balance("small", "testchannel")

        # whale: 10000 * 0.01 = 100 decay → 9900
        assert whale_bal == 9900
        # small: below exempt_below (1000), no decay
        assert small_bal == 500

    async def test_decay_transaction_logged(self, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock):
        """Decay should log negative transactions."""
        d = make_config_dict()
        d["balance_maintenance"] = {
            "mode": "decay",
            "decay": {"enabled": True, "daily_rate": 0.01, "exempt_below": 100},
        }
        cfg = EconomyConfig(**d)

        await database.credit("whale", "testchannel", 5000, "earn")
        sched = make_scheduler(cfg, database, presence, mock_client)
        await sched._execute_balance_maintenance()

        import sqlite3
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        tx = conn.execute(
            "SELECT * FROM transactions WHERE type = 'decay' AND username = 'whale'"
        ).fetchone()
        conn.close()
        assert tx is not None
        assert tx["amount"] < 0  # Negative for debit


class TestMaintenanceNone:
    """No-op mode."""

    async def test_none_mode_no_changes(self, database: EconomyDatabase, presence: PresenceTracker, mock_client: MagicMock):
        """mode=none should not change any balances."""
        d = make_config_dict()
        d["balance_maintenance"] = {"mode": "none"}
        cfg = EconomyConfig(**d)

        await database.credit("user", "testchannel", 5000, "earn")
        sched = make_scheduler(cfg, database, presence, mock_client)
        await sched._execute_balance_maintenance()

        assert await database.get_balance("user", "testchannel") == 5000
