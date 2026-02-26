"""Tests for BountyManager — Sprint 7."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from kryten_economy.bounty_manager import BountyManager
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from tests.conftest import make_config_dict
from unittest.mock import AsyncMock, MagicMock

CH = "testchannel"


def _make_bounty_config(**overrides) -> EconomyConfig:
    bounties = {
        "enabled": True,
        "min_amount": 100,
        "max_amount": 50000,
        "max_open_per_user": 3,
        "default_expiry_hours": 168,
        "expiry_refund_percent": 50,
        "description_max_length": 200,
    }
    bounties.update(overrides)
    return EconomyConfig(**make_config_dict(bounties=bounties))


async def _seed_account(db: EconomyDatabase, username: str, balance: int) -> None:
    await db.get_or_create_account(username, CH)
    if balance > 0:
        await db.credit(username, CH, balance, tx_type="seed", reason="test seed")


# ═══════════════════════════════════════════════════════════════
#  Creation Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_success(database: EconomyDatabase, mock_client: MagicMock):
    """Debits creator, creates row, returns ID."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", 1000)

    result = await mgr.create_bounty("Alice", CH, 500, "Find the lost reel")

    assert result["success"] is True
    assert result["bounty_id"] >= 1
    assert "500" in result["message"]

    # Balance should be debited
    acc = await database.get_account("Alice", CH)
    assert acc["balance"] == 500  # 1000 - 500


@pytest.mark.asyncio
async def test_create_insufficient_funds(database: EconomyDatabase, mock_client: MagicMock):
    """Low balance → rejected."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Broke", 50)

    result = await mgr.create_bounty("Broke", CH, 500, "Can't afford this")

    assert result["success"] is False
    assert "Insufficient" in result["message"]


@pytest.mark.asyncio
async def test_create_below_min(database: EconomyDatabase, mock_client: MagicMock):
    """Amount < min → rejected."""
    cfg = _make_bounty_config(min_amount=100)
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", 10000)

    result = await mgr.create_bounty("Alice", CH, 50, "Too small")

    assert result["success"] is False
    assert "Minimum" in result["message"]


@pytest.mark.asyncio
async def test_create_above_max(database: EconomyDatabase, mock_client: MagicMock):
    """Amount > max → rejected."""
    cfg = _make_bounty_config(max_amount=50000)
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", 999999)

    result = await mgr.create_bounty("Alice", CH, 60000, "Too big")

    assert result["success"] is False
    assert "Maximum" in result["message"]


@pytest.mark.asyncio
async def test_create_max_open_reached(database: EconomyDatabase, mock_client: MagicMock):
    """Already 3 open → rejected."""
    cfg = _make_bounty_config(max_open_per_user=3)
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", 100000)

    # Create 3 bounties
    for i in range(3):
        r = await mgr.create_bounty("Alice", CH, 100, f"Bounty {i}")
        assert r["success"] is True

    # 4th should fail
    result = await mgr.create_bounty("Alice", CH, 100, "One too many")
    assert result["success"] is False
    assert "max" in result["message"].lower()


# ═══════════════════════════════════════════════════════════════
#  Claim Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_claim_success(database: EconomyDatabase, mock_client: MagicMock):
    """Status → claimed, winner credited, both notified."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Creator", 5000)
    await _seed_account(database, "Winner", 0)

    create_result = await mgr.create_bounty("Creator", CH, 1000, "Find it")
    bounty_id = create_result["bounty_id"]

    reply = await mgr.claim_bounty(bounty_id, CH, "Winner", "Admin")

    assert "claimed" in reply.lower()
    assert "1,000" in reply

    # Winner should have the bounty amount
    acc = await database.get_account("Winner", CH)
    assert acc["balance"] == 1000

    # Both should be PMed
    assert mock_client.send_pm.call_count >= 2

    # Public announcement
    mock_client.send_chat.assert_called()


@pytest.mark.asyncio
async def test_claim_nonexistent(database: EconomyDatabase, mock_client: MagicMock):
    """Invalid ID → error."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))

    reply = await mgr.claim_bounty(9999, CH, "Nobody", "Admin")
    assert "not found" in reply.lower()


@pytest.mark.asyncio
async def test_claim_already_claimed(database: EconomyDatabase, mock_client: MagicMock):
    """Double claim → rejected."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Creator", 5000)
    await _seed_account(database, "W1", 0)
    await _seed_account(database, "W2", 0)

    r = await mgr.create_bounty("Creator", CH, 500, "Once only")
    bid = r["bounty_id"]

    await mgr.claim_bounty(bid, CH, "W1", "Admin")
    reply = await mgr.claim_bounty(bid, CH, "W2", "Admin")
    assert "already" in reply.lower()


# ═══════════════════════════════════════════════════════════════
#  Expiry Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_expire_refund(database: EconomyDatabase, mock_client: MagicMock):
    """Past expiry → status expired, 50% refund."""
    cfg = _make_bounty_config(expiry_refund_percent=50)
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Creator", 5000)

    r = await mgr.create_bounty("Creator", CH, 1000, "Will expire")
    bid = r["bounty_id"]

    # Manually set the bounty to have already expired
    import sqlite3
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(database._db_path)
    conn.execute(
        "UPDATE bounties SET expires_at = ? WHERE id = ?",
        (past, bid),
    )
    conn.commit()
    conn.close()

    count = await mgr.process_expired_bounties(CH)
    assert count == 1

    # Creator should get 50% refund (500 Z)
    acc = await database.get_account("Creator", CH)
    assert acc["balance"] == 4000 + 500  # 5000 - 1000 + 500

    # PM sent about refund
    pm_calls = [
        c for c in mock_client.send_pm.call_args_list
        if "expired" in str(c).lower() or "refund" in str(c).lower()
    ]
    assert len(pm_calls) >= 1


@pytest.mark.asyncio
async def test_expire_no_refund_if_zero_percent(database: EconomyDatabase, mock_client: MagicMock):
    """Config refund 0% → no credit."""
    cfg = _make_bounty_config(expiry_refund_percent=0)
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Creator", 5000)

    r = await mgr.create_bounty("Creator", CH, 1000, "No refund")
    bid = r["bounty_id"]

    # Force expiry
    import sqlite3
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(database._db_path)
    conn.execute("UPDATE bounties SET expires_at = ? WHERE id = ?", (past, bid))
    conn.commit()
    conn.close()

    count = await mgr.process_expired_bounties(CH)
    assert count == 1

    # Creator should NOT get refund — balance stays at 4000 (5000 - 1000)
    acc = await database.get_account("Creator", CH)
    assert acc["balance"] == 4000


# ═══════════════════════════════════════════════════════════════
#  List / Announcement Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bounty_list_open_only(database: EconomyDatabase, mock_client: MagicMock):
    """Only open bounties returned by get_open_bounties."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "A", 50000)
    await _seed_account(database, "W", 0)

    # Create 2, claim 1
    r1 = await mgr.create_bounty("A", CH, 200, "Open one")
    r2 = await mgr.create_bounty("A", CH, 300, "Claimed one")
    await mgr.claim_bounty(r2["bounty_id"], CH, "W", "Admin")

    open_bounties = await database.get_open_bounties(CH)
    assert len(open_bounties) == 1
    assert open_bounties[0]["description"] == "Open one"


@pytest.mark.asyncio
async def test_public_announcement_on_create(database: EconomyDatabase, mock_client: MagicMock):
    """Chat message on creation (done by PM handler, but verify structure)."""
    # The public announcement on create is done in _cmd_bounty (PM handler),
    # so here we just verify that bounty_manager.create_bounty returns the
    # right data for the handler to announce.
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Alice", 5000)

    result = await mgr.create_bounty("Alice", CH, 500, "Public bounty")
    assert result["success"] is True
    assert result["bounty_id"] >= 1
    # The message includes key info for announcement
    assert "500" in result["message"]


@pytest.mark.asyncio
async def test_public_announcement_on_claim(database: EconomyDatabase, mock_client: MagicMock):
    """Chat message sent on claim."""
    cfg = _make_bounty_config()
    mgr = BountyManager(cfg, database, mock_client, logging.getLogger("test"))
    await _seed_account(database, "Creator", 5000)
    await _seed_account(database, "Winner", 0)

    r = await mgr.create_bounty("Creator", CH, 500, "Claim me")
    mock_client.send_chat.reset_mock()

    await mgr.claim_bounty(r["bounty_id"], CH, "Winner", "Admin")

    mock_client.send_chat.assert_called_once()
    msg = mock_client.send_chat.call_args[0][1]
    assert "Winner" in msg
    assert "Claim me" in msg
