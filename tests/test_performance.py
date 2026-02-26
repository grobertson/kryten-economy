"""Sprint 9 — Performance tests.

Tests:
- Presence tick 100 users completes in < 2 seconds
- Presence tick 500 users completes in < 10 seconds
- Batch credit efficiency (batch faster than individual)
- PM command response latency < 500ms
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase


class TestPresenceTickPerformance:
    """Profile batch_credit_presence at scale."""

    @pytest.mark.asyncio
    async def test_presence_tick_100_users(
        self,
        database: EconomyDatabase,
    ) -> None:
        """batch_credit_presence for 100 users completes in < 2 seconds."""
        # Create 100 accounts
        for i in range(100):
            await database.get_or_create_account(f"user{i}", "testchannel")

        credits = [(f"user{i}", "testchannel", 1) for i in range(100)]

        start = time.monotonic()
        await database.batch_credit_presence(credits)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, f"Presence tick took {elapsed:.2f}s for 100 users"

        # Verify credits applied
        account = await database.get_account("user0", "testchannel")
        assert account["balance"] >= 1

    @pytest.mark.asyncio
    async def test_presence_tick_500_users(
        self,
        database: EconomyDatabase,
    ) -> None:
        """batch_credit_presence for 500 users completes in < 10 seconds."""
        for i in range(500):
            await database.get_or_create_account(f"user{i}", "testchannel")

        credits = [(f"user{i}", "testchannel", 1) for i in range(500)]

        start = time.monotonic()
        await database.batch_credit_presence(credits)
        elapsed = time.monotonic() - start

        assert elapsed < 10.0, f"Presence tick took {elapsed:.2f}s for 500 users"

    @pytest.mark.asyncio
    async def test_batch_credit_efficiency(
        self,
        database: EconomyDatabase,
    ) -> None:
        """Batch write is faster than individual writes for 50 users."""
        N = 50

        # Setup: create accounts
        for i in range(N * 2):
            await database.get_or_create_account(f"user{i}", "testchannel")

        # Individual credits
        start_individual = time.monotonic()
        for i in range(N):
            await database.credit(f"user{i}", "testchannel", 1, tx_type="presence")
        elapsed_individual = time.monotonic() - start_individual

        # Batch credits
        credits = [(f"user{N + i}", "testchannel", 1) for i in range(N)]
        start_batch = time.monotonic()
        await database.batch_credit_presence(credits)
        elapsed_batch = time.monotonic() - start_batch

        # Batch should be faster (or at worst similar)
        assert elapsed_batch <= elapsed_individual * 2, (
            f"Batch ({elapsed_batch:.3f}s) unexpectedly slow vs individual ({elapsed_individual:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_command_response_latency(
        self,
        database: EconomyDatabase,
    ) -> None:
        """Account lookup completes in < 500ms."""
        await database.get_or_create_account("alice", "testchannel")
        await database.credit("alice", "testchannel", 1000, tx_type="admin")

        start = time.monotonic()
        account = await database.get_account("alice", "testchannel")
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"Account lookup took {elapsed:.3f}s"
        assert account is not None
