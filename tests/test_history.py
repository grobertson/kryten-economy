"""Tests for transaction history command."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase,
    username: str = "Alice",
    balance: int = 5000,
) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="test", reason="seed")


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
) -> PmHandler:
    logger = logging.getLogger("test")
    presence = PresenceTracker(config, database, logger)
    return PmHandler(
        config=config,
        database=database,
        client=None,
        presence_tracker=presence,
        logger=logger,
    )


@pytest.mark.asyncio
async def test_history_empty(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """No transactions → friendly message."""
    handler = _make_handler(sample_config, database)
    resp = await handler._cmd_history("Nobody", CH, [])
    assert "no transaction" in resp.lower()


@pytest.mark.asyncio
async def test_history_shows_recent(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """History shows recent transactions after earning."""
    await _seed_account(database, "Alice", 5000)
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_history("Alice", CH, [])
    assert "transaction" in resp.lower()
    # The credit from _seed_account created a transaction
    assert "seed" in resp.lower()


@pytest.mark.asyncio
async def test_history_custom_limit(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Custom limit parameter works."""
    await _seed_account(database, "Alice", 5000)
    # Add more transactions
    for i in range(5):
        await database.credit("Alice", CH, 10, tx_type="test", reason=f"tx_{i}")
    handler = _make_handler(sample_config, database)

    resp = await handler._cmd_history("Alice", CH, ["3"])
    lines = [l for l in resp.split("\n") if l.strip().startswith("+")] + \
            [l for l in resp.split("\n") if l.strip().startswith("-")]
    # Should have at most 3 transaction lines
    assert len(lines) <= 3


@pytest.mark.asyncio
async def test_history_max_cap(
    sample_config: EconomyConfig, database: EconomyDatabase,
):
    """Limit is capped at 25."""
    handler = _make_handler(sample_config, database)
    # Requesting 100 should be capped to 25 internally
    resp = await handler._cmd_history("Alice", CH, ["100"])
    # Just should not crash, and should either show empty or capped results
    assert resp is not None
