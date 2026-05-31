"""Tests for queue spending commands (Gaps 4, 5, 6).

Covers spending.queue_preview, spending.queue, and spending.queue_refund
command handlers in CommandHandler.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.command_handler import CommandHandler
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.spending_engine import SpendingEngine

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase,
    username: str = "alice",
    balance: int = 50000,
    lifetime: int = 0,
) -> None:
    """Create account with given balance."""
    import asyncio

    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")
    if lifetime > 0:
        loop = asyncio.get_running_loop()

        def _set():
            conn = db._get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET lifetime_earned = ? WHERE username = ? AND channel = ?",
                    (lifetime, username, CH),
                )
                conn.commit()
            finally:
                conn.close()

        await loop.run_in_executor(None, _set)


@pytest.fixture
def spending_app(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> MagicMock:
    """Mock EconomyApp with real DB and SpendingEngine."""
    app = MagicMock()
    app.config = sample_config
    app.db = database
    app.client = mock_client
    app.logger = logging.getLogger("test.app")
    app.commands_processed = 0
    app.uptime_seconds = 1.0
    app.spending_engine = SpendingEngine(
        sample_config, database, None, logging.getLogger("test.spending"),
    )
    return app


@pytest.fixture
def handler(spending_app: MagicMock, mock_client: MagicMock) -> CommandHandler:
    """Create CommandHandler with spending engine."""
    return CommandHandler(spending_app, mock_client, logging.getLogger("test.cmd"))


# ═══════════════════════════════════════════════════════════════
#  spending.queue_preview
# ═══════════════════════════════════════════════════════════════

class TestQueuePreview:
    """Tests for Gap 4: spending.queue_preview."""

    async def test_preview_returns_cost(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Happy path: correct cost_z, tier_label, available: true."""
        await _seed_account(database, "alice", balance=50000)
        result = await handler._handle_command({
            "command": "spending.queue_preview",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,  # 10 minutes — Short tier
        })
        assert result["success"] is True
        data = result["data"]
        assert data["available"] is True
        assert data["cost_z"] > 0
        assert data["tier_label"] == "Short / Music Video"
        assert data["error_code"] is None

    async def test_preview_insufficient_balance(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """available: false, error_code: insufficient_balance."""
        await _seed_account(database, "alice", balance=5)  # too poor
        result = await handler._handle_command({
            "command": "spending.queue_preview",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
        })
        assert result["success"] is True
        data = result["data"]
        assert data["available"] is False
        assert data["error_code"] == "insufficient_balance"

    async def test_preview_daily_limit(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Seed daily_activity.queues_used = max; expect daily_limit_reached."""
        await _seed_account(database, "alice", balance=50000)
        today = datetime.now(timezone.utc).date().isoformat()
        # Seed queues_used to 3 (the default max)
        for _ in range(3):
            await database.increment_daily_queues_used("alice", CH, today)

        result = await handler._handle_command({
            "command": "spending.queue_preview",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
        })
        assert result["success"] is True
        data = result["data"]
        assert data["available"] is False
        assert data["error_code"] == "daily_limit_reached"

    async def test_preview_cooldown(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Seed a recent queue transaction; expect cooldown_active."""
        await _seed_account(database, "alice", balance=50000)
        # Insert a queue transaction (simulating recent spend)
        await database.debit(
            "alice", CH, 100, "spend", trigger_id="spend.queue.test1"
        )
        result = await handler._handle_command({
            "command": "spending.queue_preview",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
        })
        assert result["success"] is True
        data = result["data"]
        assert data["available"] is False
        assert data["error_code"] == "cooldown_active"
        assert "cooldown_remaining_sec" in data


# ═══════════════════════════════════════════════════════════════
#  spending.queue
# ═══════════════════════════════════════════════════════════════

class TestQueueSpend:
    """Tests for Gap 5: spending.queue."""

    async def test_queue_happy_path(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Debit succeeds; idempotency row inserted; daily counter incremented."""
        await _seed_account(database, "alice", balance=50000)
        result = await handler._handle_command({
            "command": "spending.queue",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
            "tier": "queue",
            "request_id": "req-001",
        })
        assert result["success"] is True
        data = result["data"]
        assert data["success"] is True
        assert data["cost_z"] > 0
        assert data["request_id"] == "req-001"
        assert "new_balance" in data

        # Verify idempotency row was created
        row = await database.get_queue_spend_request("req-001")
        assert row is not None
        assert row["username"] == "alice"
        assert row["cost_z"] == data["cost_z"]

        # Verify daily counter incremented
        today = datetime.now(timezone.utc).date().isoformat()
        activity = await database.get_or_create_daily_activity("alice", CH, today)
        assert activity["queues_used"] >= 1

    async def test_queue_idempotent(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Call spending.queue twice with same request_id; balance debited only once."""
        await _seed_account(database, "alice", balance=50000)
        req = {
            "command": "spending.queue",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
            "tier": "queue",
            "request_id": "req-idem",
        }
        result1 = await handler._handle_command(req)
        assert result1["success"] is True
        balance_after_first = result1["data"]["new_balance"]

        result2 = await handler._handle_command(req)
        assert result2["success"] is True
        assert result2["data"].get("idempotent_replay") is True

        # Balance should not change on second call
        account = await database.get_account("alice", CH)
        assert account["balance"] == balance_after_first

    async def test_queue_insufficient_balance(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """success: false when balance too low."""
        await _seed_account(database, "alice", balance=5)
        result = await handler._handle_command({
            "command": "spending.queue",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
            "tier": "queue",
            "request_id": "req-poor",
        })
        assert result["success"] is True
        data = result["data"]
        assert data["success"] is False
        assert data["error_code"] == "insufficient_balance"


# ═══════════════════════════════════════════════════════════════
#  spending.queue_refund
# ═══════════════════════════════════════════════════════════════

class TestQueueRefund:
    """Tests for Gap 6: spending.queue_refund."""

    async def test_refund_happy_path(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Spend first, then refund; balance restored."""
        await _seed_account(database, "alice", balance=50000)
        # Spend
        spend_result = await handler._handle_command({
            "command": "spending.queue",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
            "tier": "queue",
            "request_id": "req-refund1",
        })
        assert spend_result["success"] is True
        cost = spend_result["data"]["cost_z"]
        balance_after_spend = spend_result["data"]["new_balance"]

        # Refund
        refund_result = await handler._handle_command({
            "command": "spending.queue_refund",
            "username": "alice",
            "channel": CH,
            "request_id": "req-refund1",
            "reason": "video broken",
        })
        assert refund_result["success"] is True
        data = refund_result["data"]
        assert data["refunded"] == cost
        assert data["new_balance"] == balance_after_spend + cost

        # Row marked refunded
        row = await database.get_queue_spend_request("req-refund1")
        assert row["refunded"] == 1

    async def test_refund_idempotent(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """Call spending.queue_refund twice; balance credited only once."""
        await _seed_account(database, "alice", balance=50000)
        # Spend
        await handler._handle_command({
            "command": "spending.queue",
            "username": "alice",
            "channel": CH,
            "duration_sec": 600,
            "tier": "queue",
            "request_id": "req-idem-refund",
        })
        # First refund
        r1 = await handler._handle_command({
            "command": "spending.queue_refund",
            "username": "alice",
            "channel": CH,
            "request_id": "req-idem-refund",
            "reason": "test",
        })
        balance_after_refund = r1["data"]["new_balance"]

        # Second refund (should be replay)
        r2 = await handler._handle_command({
            "command": "spending.queue_refund",
            "username": "alice",
            "channel": CH,
            "request_id": "req-idem-refund",
            "reason": "test",
        })
        assert r2["success"] is True
        assert r2["data"].get("idempotent_replay") is True

        # Balance unchanged
        account = await database.get_account("alice", CH)
        assert account["balance"] == balance_after_refund

    async def test_refund_unknown_request_id(
        self, handler: CommandHandler, database: EconomyDatabase
    ):
        """success: false, error: unknown_request_id."""
        await _seed_account(database, "alice", balance=50000)
        result = await handler._handle_command({
            "command": "spending.queue_refund",
            "username": "alice",
            "channel": CH,
            "request_id": "req-nonexistent",
            "reason": "test",
        })
        assert result["success"] is True
        data = result["data"]
        assert data["success"] is False
        assert data["error"] == "unknown_request_id"
