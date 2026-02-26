"""Tests for Sprint 2 — Welcome Wallet & Welcome-Back Bonus."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.presence_tracker import PresenceTracker


@pytest.fixture
def tracker(sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock) -> PresenceTracker:
    return PresenceTracker(
        config=sample_config, database=database, client=mock_client,
        logger=logging.getLogger("test.welcome"),
    )


class TestWelcomeWallet:
    """Welcome wallet on first genuine join."""

    async def test_new_user_gets_wallet(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Brand new user should receive welcome wallet."""
        await tracker.handle_user_join("NewUser", "testchannel")
        balance = await database.get_balance("NewUser", "testchannel")
        assert balance == 100

    async def test_wallet_pm_sent(self, tracker: PresenceTracker, database: EconomyDatabase, mock_client: MagicMock):
        """Welcome wallet should trigger a PM."""
        await tracker.handle_user_join("NewUser", "testchannel")
        mock_client.send_pm.assert_called()
        msg = mock_client.send_pm.call_args[0][2]
        assert "Welcome" in msg or "100" in msg

    async def test_no_wallet_on_bounce(self, tracker: PresenceTracker, database: EconomyDatabase):
        """Bounce (non-genuine join) should not trigger wallet."""
        # First genuine join
        await tracker.handle_user_join("NewUser", "testchannel")
        first_balance = await database.get_balance("NewUser", "testchannel")

        # Simulate bounce
        from kryten_economy.utils import now_utc
        tracker._last_departure[("newuser", "testchannel")] = now_utc()
        del tracker._sessions[("newuser", "testchannel")]

        # Rejoining within debounce = bounce
        await tracker.handle_user_join("NewUser", "testchannel")
        assert await database.get_balance("NewUser", "testchannel") == first_balance

    async def test_zero_wallet_amount(self, sample_config: EconomyConfig, database: EconomyDatabase, mock_client: MagicMock):
        """Zero welcome_wallet should not attempt credit."""
        from conftest import make_config_dict
        d = make_config_dict()
        d["onboarding"]["welcome_wallet"] = 0
        cfg = EconomyConfig(**d)

        t = PresenceTracker(config=cfg, database=database, client=mock_client,
                            logger=logging.getLogger("test"))
        await t.handle_user_join("NewUser", "testchannel")
        assert await database.get_balance("NewUser", "testchannel") == 0


class TestWelcomeBack:
    """Welcome-back bonus for returning users."""

    async def test_welcome_back_after_absence(self, tracker: PresenceTracker, database: EconomyDatabase):
        """User absent for >= days_absent should get welcome-back bonus."""
        # Create account with old last_seen
        await database.get_or_create_account("OldUser", "testchannel")
        await database.claim_welcome_wallet("OldUser", "testchannel", 100)
        # Set last_seen to 10 days ago
        import sqlite3
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn = sqlite3.connect(database._db_path)
        conn.execute(
            "UPDATE accounts SET last_seen = ? WHERE username = 'OldUser'",
            (old_date,),
        )
        conn.commit()
        conn.close()

        await tracker.handle_user_join("OldUser", "testchannel")
        balance = await database.get_balance("OldUser", "testchannel")
        assert balance == 200  # 100 (wallet) + 100 (welcome back)

    async def test_no_welcome_back_recent(self, tracker: PresenceTracker, database: EconomyDatabase):
        """User absent for < days_absent should not get bonus."""
        await database.get_or_create_account("RecentUser", "testchannel")
        await database.claim_welcome_wallet("RecentUser", "testchannel", 100)
        # last_seen is CURRENT_TIMESTAMP (just now)
        await tracker.handle_user_join("RecentUser", "testchannel")
        balance = await database.get_balance("RecentUser", "testchannel")
        assert balance == 100  # Only wallet, no welcome-back

    async def test_welcome_back_pm(self, tracker: PresenceTracker, database: EconomyDatabase, mock_client: MagicMock):
        """Welcome-back should send PM."""
        await database.get_or_create_account("OldUser", "testchannel")
        await database.claim_welcome_wallet("OldUser", "testchannel", 100)
        import sqlite3
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn = sqlite3.connect(database._db_path)
        conn.execute("UPDATE accounts SET last_seen = ? WHERE username = 'OldUser'", (old_date,))
        conn.commit()
        conn.close()

        mock_client.send_pm.reset_mock()
        await tracker.handle_user_join("OldUser", "testchannel")
        mock_client.send_pm.assert_called()
        msg = mock_client.send_pm.call_args[0][2]
        assert "back" in msg.lower() or "100" in msg

    async def test_welcome_back_disabled(self, database: EconomyDatabase, mock_client: MagicMock):
        """Welcome-back disabled should not send bonus."""
        from conftest import make_config_dict
        d = make_config_dict()
        d["retention"]["welcome_back"]["enabled"] = False
        cfg = EconomyConfig(**d)

        t = PresenceTracker(config=cfg, database=database, client=mock_client,
                            logger=logging.getLogger("test"))
        await database.get_or_create_account("OldUser", "testchannel")
        await database.claim_welcome_wallet("OldUser", "testchannel", 100)
        import sqlite3
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn = sqlite3.connect(database._db_path)
        conn.execute("UPDATE accounts SET last_seen = ? WHERE username = 'OldUser'", (old_date,))
        conn.commit()
        conn.close()

        await t.handle_user_join("OldUser", "testchannel")
        balance = await database.get_balance("OldUser", "testchannel")
        assert balance == 100  # No welcome-back bonus
