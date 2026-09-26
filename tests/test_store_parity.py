"""Backend parity, concurrency, and atomicity tests for the economy store.

These tests are the acceptance evidence for SPEC-Sortie-3. They run against a
**real** PostgreSQL server (the store is too high-stakes to validate with mocks),
obtained from ``KRYTEN_ECONOMY_TEST_DSN`` (or the legacy ``KRYTEN_ECONOMY_PG_DSN``).
When neither is set, every test here is skipped with a clear reason so a developer
machine without a database still gets a green suite rather than a confusing failure.

Set it like so (chandra-1 example, password supplied out of band):

    export KRYTEN_ECONOMY_TEST_DSN='postgresql://kryten:...@chandra-1.local:5432/kryten_economy_test'
    uv run pytest tests/test_store_parity.py -v

The schema is expected to be applied already (``alembic upgrade head``); these
tests never create or drop schema. Every row they create is scoped to a
per-test unique channel and removed on teardown.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kryten_economy.database import EconomyDatabase
from kryten_economy.db.database_pg import EconomyDatabasePg
from tests.conftest import PG_AVAILABLE

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not PG_AVAILABLE,
        reason="No PostgreSQL DSN; PostgreSQL backend tests skipped",
    ),
]

LOGGER = logging.getLogger("test.store_parity")


def _channel() -> str:
    """Unique channel per test so runs never collide."""
    return f"pgtest_{uuid.uuid4().hex[:10]}"


# ``pg_store`` now lives in tests/conftest.py so every PostgreSQL-marked test
# module can share it. It was previously duplicated here, and the local copy
# silently skipped economy_snapshots / bounties / service_metrics during
# cleanup, so those rows leaked between tests.


# ══════════════════════════════════════════════════════════════
#  Interface parity
# ══════════════════════════════════════════════════════════════


class TestInterfaceParity:
    """Both backends expose the same public coroutine surface."""

    def test_pg_backend_covers_sqlite_surface(self) -> None:
        def public_coroutines(cls: type) -> set[str]:
            return {
                name
                for name in dir(cls)
                if not name.startswith("_") and callable(getattr(cls, name, None))
            }

        sqlite_methods = public_coroutines(EconomyDatabase)
        pg_methods = public_coroutines(EconomyDatabasePg)

        missing = sqlite_methods - pg_methods
        assert not missing, f"EconomyDatabasePg is missing: {sorted(missing)}"

    def test_pg_methods_are_all_coroutines(self) -> None:
        """Every public method on the asyncpg store must be awaitable."""
        import inspect

        for name in dir(EconomyDatabasePg):
            if name.startswith("_"):
                continue
            attr = getattr(EconomyDatabasePg, name)
            if callable(attr):
                assert inspect.iscoroutinefunction(attr), f"{name} must be async"

    def test_pg_store_satisfies_protocol(self) -> None:
        """The asyncpg store structurally satisfies the shared protocol."""
        from kryten_economy.db.protocol import EconomyStore

        # runtime_checkable verifies method presence, which is the structural
        # guarantee available at runtime (mypy checks the signatures).
        assert isinstance(EconomyDatabasePg.__new__(EconomyDatabasePg), EconomyStore)


# ══════════════════════════════════════════════════════════════
#  Value-shape parity (the boundary contract)
# ══════════════════════════════════════════════════════════════


class TestValueParity:
    """Returned Python types must match the SQLite store exactly.

    These assert the boundary policy, not implementation detail: a ``datetime``
    or ``bool`` leaking out of the store breaks real callers (``pm_handler``
    calls ``datetime.fromisoformat`` on account rows).
    """

    async def test_account_row_keys_match_sqlite(self, pg_store, tmp_path) -> None:
        store, ch = pg_store
        pg_account = await store.get_or_create_account("Alice", ch)

        sqlite = EconomyDatabase(str(tmp_path / "parity.db"), LOGGER)
        await sqlite.initialize()
        sq_account = await sqlite.get_or_create_account("Alice", ch)

        assert set(pg_account) == set(sq_account)
        for key in ("username", "channel", "balance", "lifetime_earned", "rank_name"):
            assert pg_account[key] == sq_account[key], key

    async def test_timestamps_are_iso_strings_not_datetime(self, pg_store) -> None:
        from datetime import datetime

        store, ch = pg_store
        account = await store.get_or_create_account("Bob", ch)

        for key in ("first_seen", "last_seen"):
            assert isinstance(
                account[key], str
            ), f"{key} must be str, got {type(account[key])}"
            datetime.fromisoformat(account[key])

    async def test_boolean_flags_are_ints(self, pg_store) -> None:
        store, ch = pg_store
        account = await store.get_or_create_account("Carol", ch)

        for key in ("welcome_wallet_claimed", "economy_banned", "quiet_mode"):
            assert account[key] == 0, f"{key} must be 0/1, got {account[key]!r}"
            assert isinstance(account[key], int), f"{key} must be int"

    async def test_claim_welcome_wallet_sets_flag_to_int_one(self, pg_store) -> None:
        store, ch = pg_store
        await store.get_or_create_account("Dave", ch)
        assert await store.claim_welcome_wallet("Dave", ch, 100) is True
        assert await store.claim_welcome_wallet("Dave", ch, 100) is False

        account = await store.get_account("Dave", ch)
        assert account["balance"] == 100
        assert account["welcome_wallet_claimed"] == 1
        assert isinstance(account["welcome_wallet_claimed"], int)

    async def test_daily_activity_roundtrip(self, pg_store) -> None:
        store, ch = pg_store
        await store.increment_daily_minutes_present("Eve", ch, "2026-01-01", 5)
        await store.increment_daily_z_earned("Eve", ch, "2026-01-01", 25)
        assert await store.get_daily_minutes_present("Eve", ch, "2026-01-01") == 5

    async def test_aggregates_are_ints_not_decimal(self, pg_store) -> None:
        """SUM() over bigint returns Decimal in asyncpg; SQLite returns int.

        A ``Decimal`` would break int formatting and standard ``json.dumps``,
        so the boundary layer must collapse integral aggregates to ``int``.
        """
        import json
        from decimal import Decimal

        store, ch = pg_store
        await store.save_race_result("race-dec", ch, "Red", 500, 2)
        await store.save_race_bet("race-dec", "Sum", ch, "Red", 100, 300, "final")
        await store.save_race_bet("race-dec", "Sum", ch, "Blue", 50, 0, "final")

        stats = await store.get_race_stats("Sum", ch)
        for key, value in stats.items():
            assert not isinstance(value, Decimal), f"{key} leaked Decimal"
            assert isinstance(
                value, int
            ), f"{key} must be int, got {type(value).__name__}"
        assert stats["total_wagered"] == 150

        await store.credit("Cir", ch, 100, "a")
        await store.credit("Cir", ch, 250, "b")
        circulation = await store.get_total_circulation(ch)
        assert circulation == 350 and isinstance(circulation, int)
        # Must survive the standard JSON encoder used by the NATS command path.
        json.dumps({"circulation": circulation, "wagered": stats["total_wagered"]})

    async def test_daily_counters_accumulate(self, pg_store) -> None:
        store, ch = pg_store
        for _ in range(3):
            await store.increment_daily_messages_sent("Count", ch, "2026-03-01")
        await store.set_daily_unique_emotes("Count", ch, "2026-03-01", 7)
        await store.mark_first_message_claimed("Count", ch, "2026-03-01")

        row = await store.get_or_create_daily_activity("Count", ch, "2026-03-01")
        assert row["messages_sent"] == 3
        assert row["unique_emotes_used"] == 7
        assert row["first_message_claimed"] == 1
        assert isinstance(row["first_message_claimed"], int)
        assert row["date"] == "2026-03-01", "date must cross as YYYY-MM-DD"

    async def test_streak_and_bridge_fields(self, pg_store) -> None:
        store, ch = pg_store
        # update_streak must work even before the row exists.
        await store.update_streak("Streak", ch, 3, 9, "2026-03-01")
        await store.update_bridge_fields(
            "Streak", ch, weekend_seen=True, week_number="2026-W09"
        )

        row = await store.get_or_create_streak("Streak", ch)
        assert row["current_daily_streak"] == 3
        assert row["longest_daily_streak"] == 9
        assert row["last_streak_date"] == "2026-03-01"
        assert row["weekend_seen_this_week"] == 1
        assert isinstance(row["weekend_seen_this_week"], int)
        assert row["week_number"] == "2026-W09"

    async def test_gambling_stats_use_greatest_for_records(self, pg_store) -> None:
        """SQLite scalar MAX(a,b) becomes GREATEST(a,b) on PostgreSQL."""
        store, ch = pg_store
        await store.update_gambling_stats("Gambler", ch, "spin", 100, 500, 0)
        await store.update_gambling_stats("Gambler", ch, "spin", -20, 0, 10)

        stats = await store.get_gambling_stats("Gambler", ch)
        assert stats["total_spins"] == 2
        assert stats["biggest_win"] == 500, "records must not regress"
        assert stats["biggest_loss"] == 10
        assert stats["net_gambling"] == 80

    async def test_creating_methods_on_missing_account_do_not_silently_noop(
        self, pg_store
    ) -> None:
        """A bare UPDATE against a missing row would silently discard the write."""
        store, ch = pg_store
        await store.increment_lifetime_gambled("Fresh", ch, 100, 90)
        account = await store.get_account("Fresh", ch)
        assert account["lifetime_gambled_in"] == 100
        assert account["lifetime_gambled_out"] == 90

        await store.set_balance("Fresh2", ch, 777)
        assert (await store.get_account("Fresh2", ch))["balance"] == 777

    async def test_challenge_lifecycle_and_expiry(self, pg_store) -> None:
        from datetime import datetime, timedelta, timezone

        store, ch = pg_store
        challenge_id = await store.create_challenge(
            "Challenger",
            "Target",
            ch,
            100,
            datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        assert isinstance(challenge_id, int)

        pending = await store.get_pending_challenge("Challenger", "Target", ch)
        assert pending is not None
        assert isinstance(
            pending["expires_at"], str
        ), "expires_at must cross as a string"
        assert await store.get_pending_challenge_for_target("Target", ch) is not None

        await store.resolve_challenge(challenge_id, "accepted")
        assert await store.get_pending_challenge("Challenger", "Target", ch) is None

        stale = await store.create_challenge(
            "Old", "Target", ch, 50, datetime.now(timezone.utc) - timedelta(hours=1)
        )
        expired = await store.expire_old_challenges()
        assert [row["id"] for row in expired] == [stale]

    async def test_tip_totals_and_uniques(self, pg_store) -> None:
        store, ch = pg_store
        await store.record_tip("Sender", "R1", ch, 100)
        await store.record_tip("Sender", "R2", ch, 50)
        await store.record_tip("Other", "R1", ch, 25)

        assert await store.get_tips_sent_today("Sender", ch) == 150
        assert isinstance(await store.get_tips_sent_today("Sender", ch), int)
        assert await store.get_tip_count_today("Sender", ch) == 2
        assert await store.get_unique_tip_recipients("Sender", ch) == 2
        assert await store.get_unique_tip_senders("R1", ch) == 2

    async def test_spectacle_game_stats(self, pg_store) -> None:
        store, ch = pg_store
        for outcome in ("win", "loss", "push", "blackjack", "win"):
            await store.update_blackjack_stats(
                "BJ",
                ch,
                outcome=outcome,
                wagered=20,
                won=40 if outcome in ("win", "blackjack") else 0,
            )
        stats = await store.get_blackjack_stats("BJ", ch)
        assert stats["games_played"] == 5
        assert stats["wins"] == 3
        assert stats["losses"] == 1
        assert stats["pushes"] == 1
        assert stats["blackjacks"] == 1

        await store.update_trivia_stats("Triv", ch, correct=True, wagered=10, won=25)
        await store.update_trivia_stats("Triv", ch, correct=True, wagered=10, won=25)
        await store.update_trivia_stats("Triv", ch, correct=False, wagered=10, won=0)
        trivia = await store.get_trivia_stats("Triv", ch)
        assert trivia["correct"] == 2
        assert trivia["incorrect"] == 1
        assert trivia["best_streak"] == 2
        assert trivia["streak"] == 0, "a wrong answer must reset the streak"

    async def test_vanity_lookup_is_case_insensitive_and_preserves_casing(
        self, pg_store
    ) -> None:
        """Identity is case-insensitive; stored casing is canonical.

        The account row holds the canonical CyTube casing, and chat-color CSS
        selectors are case-sensitive, so a later purchase written in a different
        case must not clobber the stored casing. Both backends enforce this.
        """
        store, ch = pg_store
        # The account exists with canonical casing, as presence tracking writes it.
        await store.get_or_create_account("TeenageDraculerX", ch)

        await store.set_vanity_item("TeenageDraculerX", ch, "chat_color", "#C5A1F7")
        # Lookup by any casing finds it.
        assert (
            await store.get_vanity_item("teenagedraculerx", ch, "chat_color")
            == "#C5A1F7"
        )
        assert (
            await store.get_vanity_item("TEENAGEDRACULERX", ch, "chat_color")
            == "#C5A1F7"
        )

        # A differently-cased purchase updates the value, not the stored casing.
        await store.set_vanity_item("teenagedraculerx", ch, "chat_color", "#A6FFAA")
        colors = await store.get_users_with_chat_colors(ch)
        assert colors == {
            "TeenageDraculerX": "#A6FFAA"
        }, "exactly one row, canonical casing preserved, newest value"

        assert await store.get_custom_greeting("x", ch) is None
        await store.set_vanity_item(
            "TeenageDraculerX", ch, "custom_greeting", "hi there"
        )
        assert await store.get_custom_greeting("teenagedraculerx", ch) == "hi there"
        assert (await store.get_all_vanity_items("TEENAGEDRACULERX", ch)) == {
            "chat_color": "#A6FFAA",
            "custom_greeting": "hi there",
        }

        await store.deactivate_vanity_item("TEENAGEDRACULERX", ch, "custom_greeting")
        assert await store.get_custom_greeting("TeenageDraculerX", ch) is None

    async def test_achievements_are_awarded_once(self, pg_store) -> None:
        store, ch = pg_store
        assert await store.has_achievement("Ach", ch, "ach1") is False
        assert await store.award_achievement("Ach", ch, "ach1") is True
        assert await store.award_achievement("Ach", ch, "ach1") is False
        assert await store.has_achievement("Ach", ch, "ach1") is True
        assert await store.get_achievement_count("Ach", ch) == 1

        rows = await store.get_user_achievements("Ach", ch)
        assert [r["achievement_id"] for r in rows] == ["ach1"]
        assert isinstance(rows[0]["awarded_at"], str)

    async def test_queue_spend_idempotency_and_refund(self, pg_store) -> None:
        """A duplicate request id must not double-charge."""
        store, ch = pg_store
        assert (
            await store.insert_queue_spend_request("rq1", "U", ch, 500, "tier") is True
        )
        assert (
            await store.insert_queue_spend_request("rq1", "U", ch, 500, "tier") is False
        )

        record = await store.get_queue_spend_request("rq1")
        assert record["cost_z"] == 500
        assert record["refunded"] == 0 and isinstance(record["refunded"], int)

        await store.mark_queue_spend_refunded("rq1")
        record = await store.get_queue_spend_request("rq1")
        assert record["refunded"] == 1
        assert isinstance(record["refunded_at"], str)

    async def test_daily_queue_counter_clamps_at_zero(self, pg_store) -> None:
        store, ch = pg_store
        for _ in range(2):
            await store.increment_daily_queues_used("Q", ch, "2026-05-05")
        for _ in range(3):
            await store.decrement_daily_queues_used("Q", ch, "2026-05-05")

        row = await store.get_or_create_daily_activity("Q", ch, "2026-05-05")
        assert row["queues_used"] == 0, "decrement must clamp at zero, not go negative"

    async def test_approval_resolves_once(self, pg_store) -> None:
        store, ch = pg_store
        approval_id = await store.create_pending_approval(
            "U", ch, "channel_gif", {"url": "x"}, 50000
        )
        assert isinstance(approval_id, int)

        pending = await store.get_pending_approval("U", ch, "channel_gif")
        assert pending is not None
        assert pending["cost"] == 50000
        assert pending["data"] == '{"url": "x"}', "dict payload is JSON-encoded"
        assert isinstance(pending["created_at"], str)

        assert await store.resolve_approval(approval_id, "admin", True) is not None
        assert await store.resolve_approval(approval_id, "other", True) is None
        assert await store.get_pending_approval("U", ch, "channel_gif") is None

    async def test_batch_presence_credit_creates_missing_accounts(
        self, pg_store
    ) -> None:
        """A presence tick must not silently credit nobody."""
        store, ch = pg_store
        await store.batch_credit_presence([("P1", ch, 5), ("P2", ch, 7)])

        assert await store.get_balance("P1", ch) == 5
        assert await store.get_balance("P2", ch) == 7

        rows = await store.get_recent_transactions("P1", ch)
        assert len(rows) == 1
        assert rows[0]["type"] == "presence"
        assert rows[0]["trigger_id"] == "presence.base"

    async def test_interest_and_decay_batches(self, pg_store) -> None:
        """Maintenance batches are all-or-nothing and log a ledger row each."""
        store, ch = pg_store
        await store.credit("R1", ch, 1000, "seed")
        await store.credit("R2", ch, 10, "seed")  # below min_balance: untouched

        paid = await store.apply_interest_batch(ch, 0.01, 100, 100)
        assert paid == 10
        assert await store.get_balance("R1", ch) == 1010
        assert await store.get_balance("R2", ch) == 10

        collected = await store.apply_decay_batch(ch, 0.05, 100)
        assert collected == 50
        assert await store.get_balance("R1", ch) == 960
        assert await store.get_balance("R2", ch) == 10

        ledger = await store.get_recent_transactions("R1", ch, limit=10)
        kinds = [row["type"] for row in ledger]
        assert kinds.count("interest") == 1
        assert kinds.count("decay") == 1

    async def test_snapshots_roundtrip(self, pg_store) -> None:
        store, ch = pg_store
        await store.write_snapshot(
            ch,
            {
                "total_accounts": 4,
                "total_z_circulation": 1000,
                "participation_rate": 42.5,
            },
        )
        latest = await store.get_latest_snapshot(ch)
        assert latest["total_accounts"] == 4
        assert abs(latest["participation_rate"] - 42.5) < 1e-6
        assert isinstance(latest["snapshot_time"], str)

        history = await store.get_snapshot_history(ch, days=7)
        assert len(history) == 1

    async def test_pruner_never_touches_banned_or_purchasing_accounts(
        self, pg_store
    ) -> None:
        """Pruning must exclude moderation records and real purchases."""
        store, ch = pg_store
        old = datetime.now(timezone.utc) - timedelta(days=90)
        for name in ("Ghost", "Banned", "Spender"):
            await store.get_or_create_account(name, ch)

        async with store._pool.acquire() as con:  # noqa: SLF001 - test-only seeding
            await con.execute(
                "UPDATE accounts SET last_seen = $1 WHERE username = 'Ghost' AND channel = $2",
                old,
                ch,
            )
            await con.execute(
                "UPDATE accounts SET last_seen = $1, economy_banned = true "
                "WHERE username = 'Banned' AND channel = $2",
                old,
                ch,
            )
            await con.execute(
                "UPDATE accounts SET last_seen = $1, chat_color = '#ffffff' "
                "WHERE username = 'Spender' AND channel = $2",
                old,
                ch,
            )

        names = {
            row["username"]
            for row in await store.find_purgeable_accounts(ch, 30, 0, None, None)
        }
        assert "Ghost" in names
        assert "Banned" not in names, "banned accounts must never be pruned"
        assert (
            "Spender" not in names
        ), "accounts with a vanity purchase must not be pruned"

    async def test_cascade_delete_removes_child_rows(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("Ghost", ch, 100, "seed")
        await store.record_tip("Ghost", "Other", ch, 5)
        await store.increment_daily_minutes_present("Ghost", ch, "2026-03-01", 10)

        counts = await store.delete_account_and_cascade("Ghost", ch)
        assert counts["accounts"] == 1
        assert counts["transactions"] == 1
        assert counts["daily_activity"] == 1
        assert (
            counts["tip_history"] == 1
        ), "tips sent by OR received by the user are removed"
        assert await store.get_account("Ghost", ch) is None

    async def test_leaderboards_and_rank_distribution(self, pg_store) -> None:
        store, ch = pg_store
        for name, balance in (("a", 100), ("b", 300), ("c", 200)):
            await store.credit(name, ch, balance, "seed")
        await store.update_account_rank("c", ch, "Producer")

        assert [r["username"] for r in await store.get_richest_users(ch)] == [
            "b",
            "c",
            "a",
        ]
        assert await store.get_median_balance(ch) == 200
        assert (await store.get_rank_distribution(ch))["Producer"] == 1

    async def test_daily_and_weekly_totals(self, pg_store) -> None:
        store, ch = pg_store
        await store.increment_daily_z_earned("a", ch, "2026-04-01", 10)
        await store.increment_daily_z_earned("a", ch, "2026-04-02", 5)

        daily = await store.get_daily_totals(ch, "2026-04-01")
        assert daily["z_earned"] == 10 and isinstance(daily["z_earned"], int)
        weekly = await store.get_weekly_totals(ch, "2026-04-01", "2026-04-02")
        assert weekly["z_earned"] == 15

        top = await store.get_top_earners_range(ch, "2026-04-01", "2026-04-02")
        assert top[0]["username"] == "a" and top[0]["earned"] == 15
        assert await store.get_active_economy_users_today(ch, "2026-04-01") == 1

    async def test_daily_competition_rejects_unknown_field(self, pg_store) -> None:
        """The interpolated field name is allowlisted, so injection is refused."""
        store, ch = pg_store
        assert (
            await store.get_daily_top(ch, "2026-04-01", "bogus; DROP TABLE accounts", 5)
            == []
        )
        assert (
            await store.get_daily_threshold_qualifiers(
                ch, "2026-04-01", "bogus; DROP TABLE accounts", 1
            )
            == []
        )

    async def test_bounty_lifecycle(self, pg_store) -> None:

        store, ch = pg_store
        bounty_id = await store.create_bounty(
            "U", ch, "do the thing", 500, "2026-06-01T00:00:00+00:00"
        )
        assert isinstance(bounty_id, int)
        assert len(await store.get_open_bounties(ch)) == 1

        assert await store.claim_bounty(bounty_id, ch, "W", "admin") is True
        assert await store.claim_bounty(bounty_id, ch, "W2", "admin") is False
        assert (await store.get_bounty(bounty_id, ch))["status"] == "claimed"

        cancel_id = await store.create_bounty("U", ch, "cancel me", 100)
        assert await store.cancel_bounty(cancel_id, ch, "admin") is True

        stale_id = await store.create_bounty(
            "U", ch, "stale", 100, "2020-01-01T00:00:00+00:00"
        )
        expired = await store.expire_bounties(ch)
        assert [row["id"] for row in expired] == [stale_id]
        assert await store.get_open_bounties(ch) == []

    async def test_get_last_queue_time_skips_refunded_requests(self, pg_store) -> None:
        """A refunded NATS spend must not count as a queue submission."""
        store, ch = pg_store
        await store.log_transaction("U", ch, -500, "spend", trigger_id="spend.queue")
        first = await store.get_last_queue_time("U", ch)
        assert first is not None

        # A refunded NATS spend (newer row) must be skipped.
        await store.insert_queue_spend_request("rq9", "U", ch, 100, "tier")
        await store.log_transaction(
            "U", ch, -100, "spend", trigger_id="spend.queue.rq9"
        )
        await store.mark_queue_spend_refunded("rq9")
        assert await store.get_last_queue_time("U", ch) == first

        # Once refunded it no longer blocks; an unrefunded one counts.
        await store.insert_queue_spend_request("rq10", "U", ch, 100, "tier")
        await store.log_transaction(
            "U", ch, -100, "spend", trigger_id="spend.queue.rq10"
        )
        assert await store.get_last_queue_time("U", ch) != first


# ══════════════════════════════════════════════════════════════
#  Currency integrity
# ══════════════════════════════════════════════════════════════


class TestCurrencyIntegrity:
    """Ledger and balance must stay consistent, and never go negative."""

    async def test_credit_logs_ledger_row(self, pg_store) -> None:
        store, ch = pg_store
        assert await store.credit("Alice", ch, 100, "earn", reason="test") == 100
        assert await store.get_balance("Alice", ch) == 100

        rows = await store.get_recent_transactions("Alice", ch)
        assert len(rows) == 1
        assert rows[0]["amount"] == 100
        assert rows[0]["type"] == "earn"
        assert isinstance(rows[0]["created_at"], str)

    async def test_debit_deducts_and_logs_negative_amount(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("Alice", ch, 100, "earn")
        assert await store.debit("Alice", ch, 40, "spend") == 60
        assert await store.get_balance("Alice", ch) == 60

        rows = await store.get_recent_transactions("Alice", ch)
        assert rows[0]["amount"] == -40

    async def test_debit_insufficient_funds_writes_nothing(self, pg_store) -> None:
        """A refused debit must not mutate balance OR the ledger."""
        store, ch = pg_store
        await store.credit("Alice", ch, 30, "earn")
        before = await store.get_recent_transactions("Alice", ch)

        assert await store.debit("Alice", ch, 50, "spend") is None
        assert await store.get_balance("Alice", ch) == 30

        after = await store.get_recent_transactions("Alice", ch)
        assert len(after) == len(before), "refused debit must not write a ledger row"

    async def test_debit_nonexistent_account_returns_none(self, pg_store) -> None:
        store, ch = pg_store
        assert await store.debit("ghost", ch, 10, "spend") is None

    async def test_refund_clamps_lifetime_spent_at_zero(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("Bob", ch, 100, "seed")
        await store.refund("Bob", ch, 100, reason="overshoot")

        account = await store.get_account("Bob", ch)
        assert account["lifetime_spent"] == 0
        assert account["balance"] == 200

    async def test_atomic_debit_reports_success(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("Cara", ch, 100, "seed")
        assert await store.atomic_debit("Cara", ch, 60) is True
        assert await store.get_balance("Cara", ch) == 40
        assert await store.atomic_debit("Cara", ch, 500) is False
        assert await store.get_balance("Cara", ch) == 40


class TestConcurrency:
    """The reason for moving off SQLite: correctness under write contention."""

    async def test_concurrent_debits_never_overdraw(self, pg_store) -> None:
        """Fire concurrent debits against a balance too small to cover them.

        Asserts: exactly ``balance // amount`` succeed, the balance never goes
        negative, and the ledger row count matches the number of successes
        (i.e. no partial or phantom writes).
        """
        store, ch = pg_store
        start_balance = 100
        amount = 30
        attempts = 10

        await store.credit("Racer", ch, start_balance, "seed")

        results = await asyncio.gather(
            *(store.debit("Racer", ch, amount, "spend") for _ in range(attempts)),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, int)]
        failures = [r for r in results if r is None]
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"unexpected errors: {errors!r}"

        expected_successes = start_balance // amount
        assert (
            len(successes) == expected_successes
        ), f"expected {expected_successes} successful debits, got {len(successes)}"
        assert len(failures) == attempts - expected_successes

        final_balance = await store.get_balance("Racer", ch)
        assert final_balance >= 0, "balance must never go negative"
        assert final_balance == start_balance - expected_successes * amount

        ledger = await store.get_recent_transactions("Racer", ch, limit=100)
        debits = [row for row in ledger if row["type"] == "spend"]
        assert (
            len(debits) == expected_successes
        ), "ledger row count must match the number of successful debits"

    async def test_concurrent_credits_do_not_lose_updates(self, pg_store) -> None:
        """Concurrent credits must accumulate without lost updates."""
        store, ch = pg_store
        contributors = 25
        each = 10

        await store.get_or_create_account("Accum", ch)
        await asyncio.gather(
            *(store.credit("Accum", ch, each, "earn") for _ in range(contributors))
        )

        assert await store.get_balance("Accum", ch) == contributors * each
        ledger = await store.get_recent_transactions("Accum", ch, limit=100)
        assert len(ledger) == contributors

    async def test_concurrent_welcome_claims_grant_once(self, pg_store) -> None:
        """A one-time grant must be granted exactly once under contention."""
        store, ch = pg_store
        await store.get_or_create_account("Onetime", ch)

        results = await asyncio.gather(
            *(store.claim_welcome_wallet("Onetime", ch, 100) for _ in range(8))
        )
        assert results.count(True) == 1, "welcome wallet must be claimed exactly once"
        assert await store.get_balance("Onetime", ch) == 100


# ══════════════════════════════════════════════════════════════
#  Reporting parity
# ══════════════════════════════════════════════════════════════


class TestReportingParity:
    async def test_circulation_and_counts(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("a", ch, 100, "seed")
        await store.credit("b", ch, 200, "seed")
        await store.credit("c", "other-channel", 999, "seed")

        assert await store.get_total_circulation(ch) == 300
        assert await store.get_account_count(ch) == 2

    async def test_search_accounts_ordering_and_scope(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("Alice", ch, 300, "seed")
        await store.credit("Alicia", ch, 200, "seed")
        await store.credit("Hidden", "other-channel", 999, "seed")

        rows = await store.search_accounts(ch, "Ali", 50)
        assert [r["username"] for r in rows] == ["Alice", "Alicia"]
        assert [r["balance"] for r in rows] == [300, 200]

        all_rows = await store.search_accounts(ch, "", 50)
        assert [r["username"] for r in all_rows] == ["Alice", "Alicia"]

    async def test_transaction_pagination(self, pg_store) -> None:
        store, ch = pg_store
        for i in range(5):
            await store.credit("Pager", ch, 10 + i, "seed")
        await store.credit("Pager", "other", 999, "seed")

        page1 = await store.get_recent_transactions("Pager", ch, limit=2, offset=0)
        page2 = await store.get_recent_transactions("Pager", ch, limit=2, offset=2)
        assert [r["amount"] for r in page1] == [14, 13]
        assert [r["amount"] for r in page2] == [12, 11]

    async def test_channel_recent_transactions_scoped(self, pg_store) -> None:
        store, ch = pg_store
        await store.credit("Alice", ch, 10, "seed")
        await store.credit("Bob", ch, 20, "seed")
        await store.credit("Eve", "other", 999, "seed")

        rows = await store.get_recent_channel_transactions(ch, limit=10)
        assert {r["channel"] for r in rows} == {ch}
        assert [r["amount"] for r in rows] == [20, 10]

    async def test_metrics_roundtrip(self, pg_store) -> None:
        store, ch = pg_store
        await store.save_metrics({"pg_events_processed": 42, "pg_z_earned_total": 1000})
        data = await store.restore_metrics()
        assert data["pg_events_processed"] == 42
        assert data["pg_z_earned_total"] == 1000

    async def test_quiet_mode_and_bans(self, pg_store) -> None:
        store, ch = pg_store
        await store.get_or_create_account("Quiet", ch)

        assert await store.get_quiet_mode("Quiet", ch) is False
        await store.set_quiet_mode("Quiet", ch, True)
        assert await store.get_quiet_mode("Quiet", ch) is True
        await store.set_quiet_mode("Quiet", ch, 0)  # int, as SQLite callers pass
        assert await store.get_quiet_mode("Quiet", ch) is False

        assert await store.is_banned("Banned", ch) is False
        await store.ban_user("Banned", ch, "admin", "testing")
        assert await store.is_banned("Banned", ch) is True
        assert await store.unban_user("Banned", ch) is True
        assert await store.is_banned("Banned", ch) is False
