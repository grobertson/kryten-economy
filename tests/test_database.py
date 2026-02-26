"""Tests for kryten_economy.database module."""

from __future__ import annotations

import pytest

from kryten_economy.database import EconomyDatabase


class TestInitialization:
    """Database initialization and table creation."""

    async def test_initialize_creates_tables(self, database: EconomyDatabase):
        """All tables should be created after initialize()."""
        # Verify tables exist by querying them
        account = await database.get_account("nobody", "ch")
        assert account is None

    async def test_initialize_idempotent(self, database: EconomyDatabase):
        """Calling initialize() twice should not error."""
        await database.initialize()
        account = await database.get_account("nobody", "ch")
        assert account is None


class TestAccountOperations:
    """Account CRUD operations."""

    async def test_get_or_create_account_new(self, database: EconomyDatabase):
        """get_or_create_account should create a new account with defaults."""
        acct = await database.get_or_create_account("alice", "ch1")
        assert acct["username"] == "alice"
        assert acct["channel"] == "ch1"
        assert acct["balance"] == 0
        assert acct["rank_name"] == "Extra"
        assert acct["welcome_wallet_claimed"] == 0

    async def test_get_or_create_account_existing(self, database: EconomyDatabase):
        """get_or_create_account should return existing account unchanged."""
        await database.get_or_create_account("alice", "ch1")
        await database.credit("alice", "ch1", 50, "earn")
        acct = await database.get_or_create_account("alice", "ch1")
        assert acct["balance"] == 50  # Not reset

    async def test_get_account_nonexistent(self, database: EconomyDatabase):
        """get_account should return None for nonexistent user."""
        assert await database.get_account("ghost", "ch1") is None

    async def test_get_balance_default(self, database: EconomyDatabase):
        """get_balance should return 0 for nonexistent accounts."""
        assert await database.get_balance("nobody", "ch1") == 0

    async def test_update_last_seen(self, database: EconomyDatabase):
        """update_last_seen should not error on existing account."""
        await database.get_or_create_account("alice", "ch1")
        await database.update_last_seen("alice", "ch1")

    async def test_update_last_active(self, database: EconomyDatabase):
        """update_last_active should not error on existing account."""
        await database.get_or_create_account("alice", "ch1")
        await database.update_last_active("alice", "ch1")


class TestBalanceOperations:
    """Credit and debit operations."""

    async def test_credit_increases_balance(self, database: EconomyDatabase):
        """credit() should increase balance and lifetime_earned."""
        new_bal = await database.credit("alice", "ch1", 100, "earn", reason="test")
        assert new_bal == 100
        acct = await database.get_account("alice", "ch1")
        assert acct["lifetime_earned"] == 100

    async def test_credit_creates_account(self, database: EconomyDatabase):
        """credit() should create account if not exists."""
        new_bal = await database.credit("newuser", "ch1", 50, "earn")
        assert new_bal == 50

    async def test_credit_logs_transaction(self, database: EconomyDatabase):
        """credit() should log a transaction."""
        await database.credit("alice", "ch1", 100, "earn", reason="presence", trigger_id="presence.base")
        # Verify via a direct query helper
        import sqlite3
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM transactions WHERE username = 'alice'").fetchone()
        conn.close()
        assert row is not None
        assert row["amount"] == 100
        assert row["type"] == "earn"
        assert row["trigger_id"] == "presence.base"

    async def test_debit_sufficient_funds(self, database: EconomyDatabase):
        """debit() should reduce balance when funds are sufficient."""
        await database.credit("alice", "ch1", 100, "earn")
        new_bal = await database.debit("alice", "ch1", 40, "spend")
        assert new_bal == 60

    async def test_debit_insufficient_funds(self, database: EconomyDatabase):
        """debit() should return None when funds are insufficient."""
        await database.credit("alice", "ch1", 30, "earn")
        result = await database.debit("alice", "ch1", 50, "spend")
        assert result is None
        # Balance unchanged
        assert await database.get_balance("alice", "ch1") == 30

    async def test_debit_exact_balance(self, database: EconomyDatabase):
        """debit() should succeed when debiting exact balance."""
        await database.credit("alice", "ch1", 100, "earn")
        result = await database.debit("alice", "ch1", 100, "spend")
        assert result == 0

    async def test_debit_nonexistent_account(self, database: EconomyDatabase):
        """debit() on nonexistent account should return None."""
        result = await database.debit("ghost", "ch1", 10, "spend")
        assert result is None

    async def test_multiple_credits_accumulate(self, database: EconomyDatabase):
        """Multiple credits should accumulate."""
        await database.credit("alice", "ch1", 10, "earn")
        await database.credit("alice", "ch1", 20, "earn")
        await database.credit("alice", "ch1", 30, "earn")
        assert await database.get_balance("alice", "ch1") == 60


class TestDailyActivity:
    """Daily activity tracking."""

    async def test_increment_daily_minutes(self, database: EconomyDatabase):
        """Incrementing minutes should accumulate via UPSERT."""
        await database.increment_daily_minutes_present("alice", "ch1", "2026-01-01", 1)
        await database.increment_daily_minutes_present("alice", "ch1", "2026-01-01", 1)
        # Verify
        import sqlite3
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT minutes_present FROM daily_activity WHERE username = 'alice' AND date = '2026-01-01'"
        ).fetchone()
        conn.close()
        assert row["minutes_present"] == 2

    async def test_increment_daily_z_earned(self, database: EconomyDatabase):
        """z_earned should accumulate via UPSERT."""
        await database.increment_daily_z_earned("alice", "ch1", "2026-01-01", 10)
        await database.increment_daily_z_earned("alice", "ch1", "2026-01-01", 5)
        import sqlite3
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT z_earned FROM daily_activity WHERE username = 'alice' AND date = '2026-01-01'"
        ).fetchone()
        conn.close()
        assert row["z_earned"] == 15


class TestPopulationQueries:
    """Population and circulation queries."""

    async def test_get_total_circulation(self, database: EconomyDatabase):
        """Total circulation should sum all balances in channel."""
        await database.credit("alice", "ch1", 100, "earn")
        await database.credit("bob", "ch1", 200, "earn")
        await database.credit("eve", "ch2", 999, "earn")  # Different channel
        assert await database.get_total_circulation("ch1") == 300

    async def test_get_total_circulation_empty(self, database: EconomyDatabase):
        """Empty channel should have 0 circulation."""
        assert await database.get_total_circulation("empty") == 0

    async def test_get_account_count(self, database: EconomyDatabase):
        """Account count should reflect channel population."""
        await database.get_or_create_account("alice", "ch1")
        await database.get_or_create_account("bob", "ch1")
        await database.get_or_create_account("eve", "ch2")
        assert await database.get_account_count("ch1") == 2
        assert await database.get_account_count("ch2") == 1


class TestWelcomeWallet:
    """Welcome wallet claiming."""

    async def test_claim_welcome_wallet_first_time(self, database: EconomyDatabase):
        """First claim should succeed and credit balance."""
        await database.get_or_create_account("alice", "ch1")
        result = await database.claim_welcome_wallet("alice", "ch1", 100)
        assert result is True
        assert await database.get_balance("alice", "ch1") == 100

    async def test_claim_welcome_wallet_duplicate(self, database: EconomyDatabase):
        """Second claim should fail (already claimed)."""
        await database.get_or_create_account("alice", "ch1")
        await database.claim_welcome_wallet("alice", "ch1", 100)
        result = await database.claim_welcome_wallet("alice", "ch1", 100)
        assert result is False
        # Balance should not double
        assert await database.get_balance("alice", "ch1") == 100
