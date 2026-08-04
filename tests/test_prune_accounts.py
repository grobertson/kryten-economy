"""Tests for Sprint 11 — Account Pruner CLI and database prune methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


from kryten_economy.prune_cli import _run, build_parser


# ── Database method tests ─────────────────────────────────────────────


async def test_find_purgeable_excludes_banned(db_with_accounts):
    """economy_banned accounts are never returned."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=0,
        balance_max=None,
        max_lifetime_earned=None,
    )
    usernames = [r["username"] for r in results]
    assert "banned_user" not in usernames


async def test_find_purgeable_excludes_spenders(db_with_accounts):
    """Accounts with lifetime_spent > 0 are never returned."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=0,
        balance_max=None,
        max_lifetime_earned=None,
    )
    assert all(r["lifetime_spent"] == 0 for r in results)


async def test_find_purgeable_excludes_vanity(db_with_accounts):
    """Accounts with a chat_color (or any vanity column) are never returned."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=0,
        balance_max=None,
        max_lifetime_earned=None,
    )
    assert all((r.get("chat_color") or "") == "" for r in results)


async def test_find_purgeable_returns_ghost(db_with_accounts):
    """ghost_user is the only eligible account in the fixture."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=0,
        balance_max=None,
        max_lifetime_earned=None,
    )
    usernames = [r["username"] for r in results]
    assert "ghost_user" in usernames


async def test_find_purgeable_excludes_recent(db_with_accounts):
    """active_user (last_seen today) is excluded with inactive_days=1."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=0,
        balance_max=None,
        max_lifetime_earned=None,
    )
    usernames = [r["username"] for r in results]
    assert "active_user" not in usernames


async def test_find_purgeable_balance_range_filter(db_with_accounts):
    """balance_min / balance_max are inclusive bounds."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=50,
        balance_max=200,
        max_lifetime_earned=None,
    )
    for r in results:
        assert 50 <= r["balance"] <= 200


async def test_find_purgeable_max_lifetime_earned(db_with_accounts):
    """max_lifetime_earned filters out high earners."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test",
        inactive_days=1,
        balance_min=0,
        balance_max=None,
        max_lifetime_earned=200,
    )
    assert all(r["lifetime_earned"] <= 200 for r in results)


async def test_delete_cascade_removes_child_rows(db_with_accounts):
    """delete_account_and_cascade removes accounts + all related rows."""
    counts = await db_with_accounts.delete_account_and_cascade("ghost_user", "test")
    assert counts["accounts"] == 1
    # Verify account is gone
    account = await db_with_accounts.get_account("ghost_user", "test")
    assert account is None


# ── CLI argument tests ────────────────────────────────────────────────


def test_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["--channel", "my-channel"])
    assert args.inactive_days == 30
    assert args.balance_min == 0
    assert args.balance_max is None
    assert args.execute is False
    assert args.yes is False


def test_parser_full_args():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--channel",
            "my-channel",
            "--inactive-days",
            "60",
            "--balance-min",
            "100",
            "--balance-max",
            "50000",
            "--max-lifetime-earned",
            "500",
            "--execute",
            "--yes",
        ]
    )
    assert args.inactive_days == 60
    assert args.balance_min == 100
    assert args.balance_max == 50000
    assert args.max_lifetime_earned == 500
    assert args.execute is True
    assert args.yes is True


async def test_dry_run_makes_no_changes(tmp_path, monkeypatch):
    """_run with --execute not set should never call delete_account_and_cascade."""
    mock_db = MagicMock()
    mock_db.initialize = AsyncMock()
    mock_db.find_purgeable_accounts = AsyncMock(
        return_value=[
            {
                "username": "ghost",
                "channel": "test",
                "balance": 100,
                "lifetime_earned": 100,
                "lifetime_spent": 0,
                "first_seen": "2026-01-01",
                "last_seen": "2026-01-01",
                "last_active": None,
                "welcome_wallet_claimed": 1,
            }
        ]
    )
    mock_db.delete_account_and_cascade = AsyncMock()

    config_file = tmp_path / "config.yaml"
    config_file.write_text("database:\n  path: test.db\n")

    parser = build_parser()
    args = parser.parse_args(
        [
            "--channel",
            "test",
            "--config",
            str(config_file),
        ]
    )

    with (
        patch("kryten_economy.prune_cli.load_config"),
        patch("kryten_economy.prune_cli.EconomyDatabase", return_value=mock_db),
    ):
        rc = await _run(args)

    mock_db.delete_account_and_cascade.assert_not_called()
    assert rc == 0
