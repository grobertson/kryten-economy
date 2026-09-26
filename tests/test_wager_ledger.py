"""Wager accounting: every balance movement must leave a ledger row.

These are the regression tests for the defect recorded in
``docs/KNOWN-ISSUES-wager-ledger-gap.md``: ``atomic_debit`` moved ``balance``
without writing a ``transactions`` row or touching ``lifetime_spent``, so every
gambling wager was invisible in the ledger. Nothing asserted the accounting
invariant, which is why it survived for the lifetime of the feature.

The invariant now holds:

    balance == lifetime_earned - lifetime_spent        (per account)
    SUM(accounts.balance) == SUM(transactions.amount)  (per channel)

The SQLite tests run everywhere. The ``postgres``-marked class asserts the same
invariants against the asyncpg store when a DSN is configured.
"""

from __future__ import annotations

import uuid

import pytest

from kryten_economy.database import EconomyDatabase
from tests.conftest import PG_AVAILABLE


def _channel() -> str:
    return f"ledger_{uuid.uuid4().hex[:10]}"


async def _float(db: EconomyDatabase, channel: str) -> int:
    """Total spendable Z in the channel.

    Uses the same aggregate the ETL verifies on both sides of the migration, so
    this test and the migration's correctness check agree on what "the float"
    means.
    """
    return int(await db.get_total_circulation(channel))


async def _ledger_net(db: EconomyDatabase, channel: str) -> int:
    rows = await db.get_recent_channel_transactions(channel, limit=100_000)
    return sum(int(r.get("amount", 0)) for r in rows)


class TestWagerLedger:
    """``atomic_debit`` must be a complete accounting operation."""

    async def test_wager_writes_a_ledger_row(self, database: EconomyDatabase) -> None:
        ch = _channel()
        await database.get_or_create_account("Alice", ch)
        await database.credit("Alice", ch, 1000, tx_type="admin_grant")

        before = len(await database.get_recent_channel_transactions(ch, limit=1000))
        assert await database.atomic_debit("Alice", ch, 250, tx_type="wager_spin")
        after = await database.get_recent_channel_transactions(ch, limit=1000)

        assert len(after) == before + 1, "a wager must add exactly one ledger row"
        assert after[0]["type"] == "wager_spin"
        assert int(after[0]["amount"]) == -250, "the wager must be a negative row"

    async def test_wager_increments_lifetime_spent(
        self, database: EconomyDatabase
    ) -> None:
        ch = _channel()
        await database.get_or_create_account("Bob", ch)
        await database.credit("Bob", ch, 1000, tx_type="admin_grant")

        acct = await database.get_account("Bob", ch)
        assert acct["lifetime_spent"] == 0

        await database.atomic_debit("Bob", ch, 250, tx_type="wager_spin")
        acct = await database.get_account("Bob", ch)
        assert acct["lifetime_spent"] == 250
        assert acct["balance"] == 750

    async def test_accounting_invariant_holds_after_a_wager(
        self, database: EconomyDatabase
    ) -> None:
        """The invariant that was silently broken for the whole feature."""
        ch = _channel()
        await database.get_or_create_account("Cara", ch)
        await database.credit("Cara", ch, 5000, tx_type="admin_grant")
        for _ in range(5):
            assert await database.atomic_debit("Cara", ch, 100, tx_type="wager_flip")

        acct = await database.get_account("Cara", ch)
        assert acct["balance"] == 4500
        assert acct["balance"] == acct["lifetime_earned"] - acct["lifetime_spent"]

    async def test_ledger_sum_matches_float(self, database: EconomyDatabase) -> None:
        ch = _channel()
        await database.get_or_create_account("Dave", ch)
        await database.credit("Dave", ch, 5000, tx_type="admin_grant")
        await database.atomic_debit("Dave", ch, 300, tx_type="wager_spin")
        await database.atomic_debit("Dave", ch, 200, tx_type="wager_spin")

        assert await _float(database, ch) == await _ledger_net(database, ch)
        assert await _float(database, ch) == 4500

    async def test_declined_wager_writes_nothing(
        self, database: EconomyDatabase
    ) -> None:
        """A rejected wager must leave no trace at all."""
        ch = _channel()
        await database.get_or_create_account("Eve", ch)
        await database.credit("Eve", ch, 100, tx_type="admin_grant")

        before = len(await database.get_recent_channel_transactions(ch, limit=1000))
        assert not await database.atomic_debit("Eve", ch, 500, tx_type="wager_spin")
        after = await database.get_recent_channel_transactions(ch, limit=1000)

        assert len(after) == before, "a declined wager must not log a row"
        acct = await database.get_account("Eve", ch)
        assert acct["balance"] == 100
        assert acct["lifetime_spent"] == 0

    async def test_tx_type_is_recorded(self, database: EconomyDatabase) -> None:
        """The engine passes a meaningful type so the ledger is queryable."""
        ch = _channel()
        await database.get_or_create_account("Frank", ch)
        await database.credit("Frank", ch, 1000, tx_type="admin_grant")
        await database.atomic_debit("Frank", ch, 100, tx_type="wager_blackjack")

        rows = await database.get_recent_channel_transactions(ch, limit=10)
        assert rows[0]["type"] == "wager_blackjack"

    async def test_default_tx_type_is_wager(self, database: EconomyDatabase) -> None:
        """Omitting the type still produces a ledger row, not a silent write."""
        ch = _channel()
        await database.get_or_create_account("Gina", ch)
        await database.credit("Gina", ch, 1000, tx_type="admin_grant")
        assert await database.atomic_debit("Gina", ch, 100)

        rows = await database.get_recent_channel_transactions(ch, limit=10)
        assert rows[0]["type"] == "wager"
        assert int(rows[0]["amount"]) == -100


class TestWagerRefund:
    """A refunded wager must unwind the spend, not inflate earnings."""

    async def test_refund_restores_balance_and_underspends(
        self, database: EconomyDatabase
    ) -> None:
        ch = _channel()
        await database.get_or_create_account("Hank", ch)
        await database.credit("Hank", ch, 1000, tx_type="admin_grant")
        await database.atomic_debit("Hank", ch, 400, tx_type="wager_heist")

        acct = await database.get_account("Hank", ch)
        assert acct["balance"] == 600
        assert acct["lifetime_spent"] == 400
        earned_before = acct["lifetime_earned"]

        await database.refund("Hank", ch, 400, reason="Heist cancelled")

        acct = await database.get_account("Hank", ch)
        assert acct["balance"] == 1000, "refund returns the escrowed wager"
        assert acct["lifetime_spent"] == 0, "refund reverses the spend"
        assert (
            acct["lifetime_earned"] == earned_before
        ), "a refund must not count as earnings - the money was never won"
        assert acct["balance"] == acct["lifetime_earned"] - acct["lifetime_spent"]

    async def test_partial_refund_keeps_the_fee_spent(
        self, database: EconomyDatabase
    ) -> None:
        """A push refunds less than the wager; the difference stays spent."""
        ch = _channel()
        await database.get_or_create_account("Iris", ch)
        await database.credit("Iris", ch, 1000, tx_type="admin_grant")
        await database.atomic_debit("Iris", ch, 200, tx_type="wager_heist")
        await database.refund("Iris", ch, 190, reason="Heist push")

        acct = await database.get_account("Iris", ch)
        assert acct["balance"] == 990, "1000 - 200 wager + 190 returned"
        assert acct["lifetime_spent"] == 10, "the 10 fee stays counted as spent"
        assert acct["balance"] == acct["lifetime_earned"] - acct["lifetime_spent"]


class TestWagerLedgerRegression:
    """Guards against the specific regression returning."""

    async def test_no_balance_change_is_ever_ledgerless(
        self, database: EconomyDatabase
    ) -> None:
        """Walk every mutating store method and assert the invariant survives.

        This is the test that was missing when the defect was introduced: it does
        not care which method moved the money, only that the books still balance.
        """
        ch = _channel()
        await database.get_or_create_account("Sam", ch)
        await database.credit("Sam", ch, 10_000, tx_type="admin_grant")
        await database.atomic_debit("Sam", ch, 1_000, tx_type="wager_spin")
        await database.debit("Sam", ch, 500, tx_type="spend", reason="test")
        await database.refund("Sam", ch, 500, reason="test refund")

        acct = await database.get_account("Sam", ch)
        assert acct["balance"] == await _ledger_net(
            database, ch
        ), "balance and ledger diverged - some mutation is not logged"
        assert acct["balance"] == 9_000
        assert acct["balance"] == acct["lifetime_earned"] - acct["lifetime_spent"]


@pytest.mark.postgres
@pytest.mark.skipif(not PG_AVAILABLE, reason="No PostgreSQL DSN configured")
class TestWagerLedgerOnPostgres:
    """The same invariants, against the asyncpg store."""

    @staticmethod
    async def _fresh(pg_store):
        store, ch = pg_store
        await store.get_or_create_account("Pat", ch)
        await store.credit("Pat", ch, 10_000, tx_type="admin_grant")
        return store, ch

    async def test_wager_writes_a_ledger_row(self, pg_store) -> None:
        store, ch = await self._fresh(pg_store)
        assert await store.atomic_debit("Pat", ch, 250, tx_type="wager_spin")

        rows = await store.get_recent_channel_transactions(ch, limit=1000)
        assert rows[0]["type"] == "wager_spin"
        assert int(rows[0]["amount"]) == -250

    async def test_invariant_holds(self, pg_store) -> None:
        store, ch = await self._fresh(pg_store)
        for _ in range(3):
            assert await store.atomic_debit("Pat", ch, 100, tx_type="wager_flip")

        acct = await store.get_account("Pat", ch)
        rows = await store.get_recent_channel_transactions(ch, limit=1000)
        ledger = sum(int(r["amount"]) for r in rows)
        assert acct["balance"] == ledger
        assert acct["balance"] == acct["lifetime_earned"] - acct["lifetime_spent"]
        assert acct["lifetime_spent"] == 300

    async def test_declined_wager_writes_nothing(self, pg_store) -> None:
        store, ch = await self._fresh(pg_store)
        before = len(await store.get_recent_channel_transactions(ch, limit=1000))
        assert not await store.atomic_debit("Pat", ch, 999_999, tx_type="wager_spin")
        after = await store.get_recent_channel_transactions(ch, limit=1000)

        assert len(after) == before
        acct = await store.get_account("Pat", ch)
        assert acct["balance"] == 10_000
        assert acct["lifetime_spent"] == 0

    async def test_refund_unwinds_the_wager(self, pg_store) -> None:
        store, ch = await self._fresh(pg_store)
        await store.atomic_debit("Pat", ch, 400, tx_type="wager_heist")
        await store.refund("Pat", ch, 400, reason="cancelled")

        acct = await store.get_account("Pat", ch)
        assert acct["balance"] == 10_000
        assert acct["lifetime_spent"] == 0
        assert acct["balance"] == acct["lifetime_earned"] - acct["lifetime_spent"]
