"""Tests for kryten_economy.database module."""

from __future__ import annotations

import logging
import sqlite3

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


# Columns added to the pre-existing gambling_stats table in v0.9.0 (spectacle
# games). Databases created before that release lack them.
_LEGACY_GAMBLING_STATS_DDL = """
    CREATE TABLE gambling_stats (
        username TEXT NOT NULL,
        channel TEXT NOT NULL,
        total_spins INTEGER DEFAULT 0,
        total_flips INTEGER DEFAULT 0,
        total_challenges INTEGER DEFAULT 0,
        total_heists INTEGER DEFAULT 0,
        biggest_win INTEGER DEFAULT 0,
        biggest_loss INTEGER DEFAULT 0,
        net_gambling INTEGER DEFAULT 0,
        UNIQUE(username, channel)
    )
"""


class TestGamblingStatsMigration:
    """Migration of the gambling_stats table for the v0.9.0 spectacle games."""

    def _legacy_columns(self, db_path: str) -> set[str]:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("PRAGMA table_info(gambling_stats)").fetchall()
            return {r[1] for r in rows}
        finally:
            conn.close()

    async def test_initialize_adds_missing_game_columns(self, tmp_db_path: str):
        """initialize() adds total_races/trivias/blackjacks to a legacy table."""
        # Build a pre-0.9.0 database: gambling_stats without the new columns.
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(_LEGACY_GAMBLING_STATS_DDL)
            conn.commit()
        finally:
            conn.close()

        before = self._legacy_columns(tmp_db_path)
        assert "total_blackjacks" not in before

        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()

        after = self._legacy_columns(tmp_db_path)
        assert {"total_races", "total_trivias", "total_blackjacks"} <= after

    async def test_update_gambling_stats_works_after_migration(self, tmp_db_path: str):
        """The blackjack/race/trivia resolve path no longer crashes on a legacy DB.

        Regression for ``sqlite3.OperationalError: table gambling_stats has no
        column named total_blackjacks`` which broke every blackjack resolution
        (stand/double/bust/timeout) and race/trivia resolution.
        """
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(_LEGACY_GAMBLING_STATS_DDL)
            conn.commit()
        finally:
            conn.close()

        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()

        # These all previously raised OperationalError on a legacy DB.
        for game in ("blackjack", "race", "trivia"):
            await db.update_gambling_stats(
                "alice", "ch1", game, net=50, biggest_win=50,
            )

        stats = await db.get_gambling_stats("alice", "ch1")
        assert stats is not None
        assert stats["total_blackjacks"] == 1
        assert stats["total_races"] == 1
        assert stats["total_trivias"] == 1
        assert stats["net_gambling"] == 150


class TestVanityItemCaseSensitivity:
    """vanity_items preserves canonical CyTube username casing for storage/display
    (so case-sensitive chat-color CSS selectors render), while identity lookups
    are case-insensitive (a username is the same person regardless of case).

    Earlier versions lowercased on write, which destroyed the casing the CSS
    selectors (``.chat-msg-<User>``) need.
    """

    async def test_storage_preserves_canonical_case(self, database: EconomyDatabase):
        await database.set_vanity_item("TeenageDraculerX", "ch", "chat_color", "#C5A1F7")
        # The stored row keeps the exact canonical casing (what CSS rendering reads).
        colors = await database.get_users_with_chat_colors("ch")
        assert colors == {"TeenageDraculerX": "#C5A1F7"}

    async def test_lookup_is_case_insensitive(self, database: EconomyDatabase):
        # Identity lookups match regardless of case (greetings/shop rely on this).
        await database.set_vanity_item("TeenageDraculerX", "ch", "chat_color", "#C5A1F7")
        assert await database.get_vanity_item("TeenageDraculerX", "ch", "chat_color") == "#C5A1F7"
        assert await database.get_vanity_item("teenagedraculerx", "ch", "chat_color") == "#C5A1F7"
        assert await database.get_vanity_item("TEENAGEDRACULERX", "ch", "chat_color") == "#C5A1F7"

    async def test_managed_colors_keep_canonical_case(self, database: EconomyDatabase):
        await database.set_vanity_item("TacoBelmont", "ch", "chat_color", "#4AEAFF")
        await database.set_vanity_item("DoodooButtchump", "ch", "chat_color", "#C5B358")
        colors = await database.get_users_with_chat_colors("ch")
        assert colors == {"TacoBelmont": "#4AEAFF", "DoodooButtchump": "#C5B358"}

    async def test_upsert_is_case_insensitive_no_duplicate_row(
        self, database: EconomyDatabase,
    ):
        # REGRESSION: a later purchase with different casing must UPDATE the same
        # row, not create a second one. A case-collision (two active rows) made
        # chat-color changes silently no-op (stale row won the CSS merge) while
        # still charging the user.
        await database.set_vanity_item("teenagedraculerx", "ch", "chat_color", "#50C878")
        await database.set_vanity_item("TeenageDraculerX", "ch", "chat_color", "#A6FFAA")

        colors = await database.get_users_with_chat_colors("ch")
        # Exactly one managed row, canonical casing, newest value.
        assert colors == {"TeenageDraculerX": "#A6FFAA"}
        assert await database.get_vanity_item("TeenageDraculerX", "ch", "chat_color") == "#A6FFAA"

    async def test_upsert_refreshes_casing_on_existing_row(
        self, database: EconomyDatabase,
    ):
        # If the only row was lowercased, a canonical-cased write recases it.
        await database.set_vanity_item("oldname", "ch", "chat_color", "#111111")
        await database.set_vanity_item("OldName", "ch", "chat_color", "#222222")
        colors = await database.get_users_with_chat_colors("ch")
        assert colors == {"OldName": "#222222"}


class TestVanityCaseCollisionMigration:
    """initialize() dedupes case-collision vanity rows and recases survivors."""

    async def test_dedupe_keeps_newest_and_recases(self, tmp_db_path: str):
        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()
        # Account carries canonical casing.
        await db.get_or_create_account("TeenageDraculerX", "Channel-Z")

        # Simulate the broken state: two active rows differing only by case,
        # the stale (older) one lowercased.
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value, purchased_at) "
                "VALUES ('teenagedraculerx', 'Channel-Z', 'chat_color', '#50C878', '2026-03-18 12:56:45')"
            )
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value, purchased_at) "
                "VALUES ('TeenageDraculerX', 'Channel-Z', 'chat_color', '#A6FFAA', '2026-06-21 12:18:10')"
            )
            conn.commit()
        finally:
            conn.close()

        # Re-initialize: migration dedupes + recases.
        db2 = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db2.initialize()

        colors = await db2.get_users_with_chat_colors("Channel-Z")
        # One row, canonical case, newest value wins.
        assert colors == {"TeenageDraculerX": "#A6FFAA"}

        # And it's a single physical row (no lingering duplicate).
        conn = sqlite3.connect(tmp_db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM vanity_items "
                "WHERE LOWER(username)='teenagedraculerx' AND item_type='chat_color'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 1

    async def test_dedupe_is_idempotent(self, tmp_db_path: str):
        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()
        await db.get_or_create_account("Bob", "ch")
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value, purchased_at) "
                "VALUES ('bob', 'ch', 'chat_color', '#000001', '2026-01-01 00:00:00')"
            )
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value, purchased_at) "
                "VALUES ('Bob', 'ch', 'chat_color', '#000002', '2026-02-01 00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        for _ in range(2):
            dbn = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
            await dbn.initialize()

        colors = await dbn.get_users_with_chat_colors("ch")
        assert colors == {"Bob": "#000002"}

    async def test_dedupe_preserves_distinct_item_types(self, tmp_db_path: str):
        # A user with a greeting AND a color must keep both (dedupe is per item_type).
        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()
        await db.get_or_create_account("Carol", "ch")
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value) "
                "VALUES ('carol', 'ch', 'chat_color', '#abcdef')"
            )
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value) "
                "VALUES ('Carol', 'ch', 'custom_greeting', 'hi there')"
            )
            conn.commit()
        finally:
            conn.close()

        db2 = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db2.initialize()
        assert await db2.get_vanity_item("Carol", "ch", "chat_color") == "#abcdef"
        assert await db2.get_vanity_item("Carol", "ch", "custom_greeting") == "hi there"



class TestRefund:
    """EconomyDatabase.refund reverses a prior spend."""

    async def test_refund_restores_balance_and_reverses_lifetime_spent(
        self, database: EconomyDatabase,
    ):
        await database.credit("Alice", "ch", 1000, tx_type="seed")
        await database.debit("Alice", "ch", 300, tx_type="spend")
        acct = await database.get_account("Alice", "ch")
        assert acct["balance"] == 700
        assert acct["lifetime_spent"] == 300

        new_balance = await database.refund("Alice", "ch", 300, reason="undo")
        assert new_balance == 1000
        acct = await database.get_account("Alice", "ch")
        assert acct["balance"] == 1000
        # Refund reverses lifetime_spent rather than inflating lifetime_earned.
        assert acct["lifetime_spent"] == 0
        assert acct["lifetime_earned"] == 1000

    async def test_refund_logs_refund_transaction(self, database: EconomyDatabase):
        await database.credit("Bob", "ch", 500, tx_type="seed")
        await database.debit("Bob", "ch", 200, tx_type="spend")
        await database.refund("Bob", "ch", 200, reason="undo")
        txns = await database.get_recent_transactions("Bob", "ch", limit=10)
        assert any(t["type"] == "refund" and t["amount"] == 200 for t in txns)

    async def test_refund_clamps_lifetime_spent_at_zero(self, database: EconomyDatabase):
        # A refund larger than recorded spend must not drive lifetime_spent < 0.
        await database.credit("Cara", "ch", 100, tx_type="seed")
        await database.refund("Cara", "ch", 100, reason="overshoot")
        acct = await database.get_account("Cara", "ch")
        assert acct["lifetime_spent"] == 0
        assert acct["balance"] == 200


class TestVanityCaseMigration:
    """Existing lowercased vanity_items rows recover canonical case on init."""

    async def test_lowercased_rows_recased_from_accounts(self, tmp_db_path: str):
        # Build a DB the modern way, then simulate legacy lowercased vanity rows.
        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()
        # Accounts carry canonical case (this path never lowercased).
        await db.get_or_create_account("TeenageDraculerX", "ch")
        await db.get_or_create_account("TacoBelmont", "ch")

        # Insert legacy-style lowercased vanity rows directly.
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value) "
                "VALUES ('teenagedraculerx', 'ch', 'chat_color', '#C5A1F7')"
            )
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value) "
                "VALUES ('tacobelmont', 'ch', 'chat_color', '#4AEAFF')"
            )
            conn.commit()
        finally:
            conn.close()

        # Re-initialize: the migration recases the rows from accounts.
        db2 = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db2.initialize()

        colors = await db2.get_users_with_chat_colors("ch")
        assert colors == {
            "TeenageDraculerX": "#C5A1F7",
            "TacoBelmont": "#4AEAFF",
        }

    async def test_migration_is_idempotent_and_skips_unknown(self, tmp_db_path: str):
        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()
        await db.get_or_create_account("KnownUser", "ch")

        conn = sqlite3.connect(tmp_db_path)
        try:
            # One row with a matching account, one with no account at all.
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value) "
                "VALUES ('knownuser', 'ch', 'chat_color', '#ABCDEF')"
            )
            conn.execute(
                "INSERT INTO vanity_items (username, channel, item_type, value) "
                "VALUES ('ghostuser', 'ch', 'chat_color', '#123456')"
            )
            conn.commit()
        finally:
            conn.close()

        # Two re-inits must converge (idempotent) and not crash.
        for _ in range(2):
            db_n = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
            await db_n.initialize()

        assert await db_n.get_vanity_item("KnownUser", "ch", "chat_color") == "#ABCDEF"
        # Unknown user (no account) is left as-is, never dropped.
        assert await db_n.get_vanity_item("ghostuser", "ch", "chat_color") == "#123456"


    async def test_migration_is_idempotent(self, tmp_db_path: str):
        """Running initialize() repeatedly on an already-migrated DB is a no-op."""
        db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
        await db.initialize()
        await db.initialize()  # would raise if ALTER weren't guarded
        await db.update_gambling_stats("bob", "ch1", "blackjack", net=10)
        stats = await db.get_gambling_stats("bob", "ch1")
        assert stats is not None
        assert stats["total_blackjacks"] == 1
