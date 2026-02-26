"""Tests for CyTube Level 2 purchase — Sprint 6."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.spending_engine import SpendingEngine
from tests.conftest import make_config_dict

CH = "testchannel"


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 0) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    spending_engine: SpendingEngine | None = None,
) -> PmHandler:
    logger = logging.getLogger("test")
    presence = PresenceTracker(config, database, logger)
    return PmHandler(
        config=config,
        database=database,
        client=mock_client,
        presence_tracker=presence,
        logger=logger,
        spending_engine=spending_engine,
    )


# ═══════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_purchase_success(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_client: MagicMock, spending_engine: SpendingEngine,
):
    """Debits account and calls safe_set_channel_rank(channel, user, 2)."""
    # Associate Producer (100000 lifetime) is the min_rank for purchase
    await _seed_account(database, "Alice", 200_000)
    # Set lifetime high enough for Associate Producer
    await database.credit("Alice", CH, 100_000, tx_type="earn", reason="seed")

    handler = _make_handler(sample_config, database, mock_client, spending_engine)

    response = await handler._buy_cytube2("Alice", CH, "")
    assert "Congratulations" in response or "Level 2" in response

    mock_client.safe_set_channel_rank.assert_called_once_with(CH, "Alice", 2)


@pytest.mark.asyncio
async def test_purchase_min_rank_gate(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_client: MagicMock, spending_engine: SpendingEngine,
):
    """Below min rank → rejected."""
    # Bob has funds but low lifetime (1000) — only Grip rank, not Associate Producer
    await database.get_or_create_account("Bob", CH)
    await database.credit("Bob", CH, 1000, tx_type="seed", reason="test seed")
    # Give Bob enough raw balance (by directly updating) without adding lifetime
    import asyncio, sqlite3
    def _set_balance():
        conn = sqlite3.connect(database._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE accounts SET balance = 200000 WHERE username = 'Bob' AND channel = ?",
            (CH,),
        )
        conn.commit()
        conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _set_balance)

    handler = _make_handler(sample_config, database, mock_client, spending_engine)

    response = await handler._buy_cytube2("Bob", CH, "")
    assert "rank" in response.lower() or "need" in response.lower()
    mock_client.safe_set_channel_rank.assert_not_called()


@pytest.mark.asyncio
async def test_purchase_insufficient_funds(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_client: MagicMock, spending_engine: SpendingEngine,
):
    """Low balance → rejected."""
    await _seed_account(database, "Alice", 100)
    # Give enough lifetime but not enough balance
    await database.credit("Alice", CH, 100_000, tx_type="earn", reason="test")
    # Now debit most of the balance
    await database.atomic_debit("Alice", CH, 100_000)

    handler = _make_handler(sample_config, database, mock_client, spending_engine)

    response = await handler._buy_cytube2("Alice", CH, "")
    assert "insufficient" in response.lower() or "funds" in response.lower()


@pytest.mark.asyncio
async def test_purchase_failure_refund(
    sample_config: EconomyConfig, database: EconomyDatabase,
    mock_client: MagicMock, spending_engine: SpendingEngine,
):
    """safe_set_channel_rank fails → refund."""
    mock_client.safe_set_channel_rank = AsyncMock(
        return_value={"success": False, "error": "not owner"},
    )

    await _seed_account(database, "Alice", 200_000)
    await database.credit("Alice", CH, 100_000, tx_type="earn", reason="test")

    handler = _make_handler(sample_config, database, mock_client, spending_engine)

    balance_before = (await database.get_account("Alice", CH))["balance"]
    response = await handler._buy_cytube2("Alice", CH, "")

    assert "failed" in response.lower() or "refund" in response.lower()

    # Balance should be restored
    balance_after = (await database.get_account("Alice", CH))["balance"]
    assert balance_after == balance_before


@pytest.mark.asyncio
async def test_purchase_disabled(
    database: EconomyDatabase, mock_client: MagicMock,
):
    """Config disabled → rejected."""
    cfg = EconomyConfig(**make_config_dict(cytube_promotion={"enabled": False}))
    spending = SpendingEngine(cfg, database, MagicMock(), logging.getLogger("test"))

    await _seed_account(database, "Alice", 200_000)
    handler = _make_handler(cfg, database, mock_client, spending)

    response = await handler._buy_cytube2("Alice", CH, "")
    assert "not available" in response.lower() or "disabled" in response.lower()
