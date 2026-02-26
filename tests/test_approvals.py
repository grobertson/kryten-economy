"""Tests for pending approval DB operations."""

from __future__ import annotations

import json

import pytest

from kryten_economy.database import EconomyDatabase

CH = "testchannel"


async def _seed_account(
    db: EconomyDatabase,
    username: str = "Alice",
    balance: int = 5000,
) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 100:
        await db.credit(username, CH, balance - 100, tx_type="test", reason="seed")


@pytest.mark.asyncio
async def test_create_pending_approval(database: EconomyDatabase):
    """create_pending_approval inserts and returns an ID."""
    aid = await database.create_pending_approval(
        "Alice", CH, "channel_gif",
        data={"gif_url": "https://example.com/cool.gif"},
        cost=5000,
    )
    assert isinstance(aid, int)
    assert aid >= 1


@pytest.mark.asyncio
async def test_resolve_approval_approved(database: EconomyDatabase):
    """Resolving as approved returns the record."""
    aid = await database.create_pending_approval(
        "Alice", CH, "force_play",
        data={"media_id": "v1", "title": "Test"},
        cost=100000,
    )
    record = await database.resolve_approval(aid, "Admin", approved=True)
    assert record is not None
    assert record["username"] == "Alice"
    assert record["type"] == "force_play"

    # Should no longer be pending
    pending = await database.get_pending_approvals(CH, "force_play")
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_resolve_approval_rejected(database: EconomyDatabase):
    """Resolving as rejected returns the record."""
    aid = await database.create_pending_approval(
        "Bob", CH, "channel_gif",
        data={"gif_url": "https://example.com/bad.gif"},
        cost=5000,
    )
    record = await database.resolve_approval(aid, "Admin", approved=False)
    assert record is not None

    # Should no longer be pending
    pending = await database.get_pending_approvals(CH, "channel_gif")
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_resolve_already_resolved(database: EconomyDatabase):
    """Resolving an already-resolved approval returns None."""
    aid = await database.create_pending_approval(
        "Alice", CH, "force_play",
        data={"media_id": "v1"},
        cost=100000,
    )
    await database.resolve_approval(aid, "Admin", approved=True)
    # Try again
    result = await database.resolve_approval(aid, "Admin2", approved=False)
    assert result is None


@pytest.mark.asyncio
async def test_list_pending_approvals(database: EconomyDatabase):
    """get_pending_approvals lists only pending items."""
    await database.create_pending_approval("Alice", CH, "channel_gif", data={"url": "a"}, cost=5000)
    await database.create_pending_approval("Bob", CH, "force_play", data={"id": "b"}, cost=100000)
    aid3 = await database.create_pending_approval("Charlie", CH, "channel_gif", data={"url": "c"}, cost=5000)

    # Resolve one
    await database.resolve_approval(aid3, "Admin", approved=True)

    # All pending
    all_pending = await database.get_pending_approvals(CH)
    assert len(all_pending) == 2

    # Filtered by type
    gif_pending = await database.get_pending_approvals(CH, "channel_gif")
    assert len(gif_pending) == 1
    assert gif_pending[0]["username"] == "Alice"

    force_pending = await database.get_pending_approvals(CH, "force_play")
    assert len(force_pending) == 1
    assert force_pending[0]["username"] == "Bob"
