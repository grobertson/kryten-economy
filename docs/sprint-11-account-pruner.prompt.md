# Sprint 11 — Account Pruner CLI

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 11 (post-launch utility)
> **Goal:** Standalone CLI tool that identifies and removes ghost accounts — users who
> received the welcome wallet but never engaged — to keep the DB and float lean.
> **Depends on:** Sprint 1 (Core Foundation — `EconomyDatabase`), Sprint 5 (transactions,
> vanity tables that must cascade)
> **Enables:** Sprint 10 (Inflation Governor) — a cleaner float makes the anchor easier
> to set; also reduces DB size and snapshot noise

---

## Table of Contents

1. [Background & Safety Principles](#1-background--safety-principles)
2. [Deliverable Summary](#2-deliverable-summary)
3. [New Database Methods](#3-new-database-methods)
4. [CLI: `kryten-economy-prune`](#4-cli-kryten-economy-prune)
5. [Output & Reporting](#5-output--reporting)
6. [Test Specifications](#6-test-specifications)
7. [Acceptance Criteria](#7-acceptance-criteria)

---

## 1. Background & Safety Principles

Every user who joins the channel gets an account created on first presence and a welcome
wallet credit (default 100 Z). Users who leave after a few minutes and never return hold
a small but nonzero balance forever, inflating the float and cluttering the leaderboard.

**Safety rules that the tool must enforce unconditionally:**

| Rule | Rationale |
|---|---|
| Dry-run is the default | Deletion is irreversible. Execute mode requires an explicit flag. |
| Never delete economy-banned accounts | A banned account is a moderation record; pruning it would erase the ban. |
| Never delete accounts with vanity purchases | `custom_greeting`, `custom_title`, `chat_color`, `channel_gif_url` etc. indicate real investment. |
| Never delete accounts with nonzero `lifetime_spent` | They actually used the economy. |
| Balance filter is inclusive: `min ≤ balance ≤ max` | Prevents accidentally catching a whale who went inactive temporarily. |
| Confirm in execute mode | Print the match count and total Z reclaimed, then require `--yes` or an interactive confirmation before proceeding. |
| Produce an audit log | Execute mode always writes a separate timestamped audit CSV. When `--output-csv` is set, the audit file is placed in the same directory with a `_deleted_TIMESTAMP` suffix (e.g. `report_deleted_20260804T141523.csv`). When `--output-csv` is not set, the audit file is written to CWD as `prune_audit_{channel}_{TIMESTAMP}.csv`. The audit path is printed at completion in both cases. |

---

## 2. Deliverable Summary

At the end of this sprint:

- `kryten_economy/prune_accounts.py` — the pruner logic (DB query + cascade delete)
- `kryten_economy/prune_cli.py` — the `argparse` CLI entry point
- `pyproject.toml` — new `[project.scripts]` entry: `kryten-economy-prune`
- Two new `EconomyDatabase` methods: `find_purgeable_accounts` and
  `delete_account_and_cascade`
- Dry-run output shows a table of matches; execute mode deletes and writes an audit CSV
- Existing service code is **not modified** — the pruner is a standalone offline tool

---

## 3. New Database Methods

Add to `kryten_economy/database.py`.

### 3.1 `find_purgeable_accounts`

```python
async def find_purgeable_accounts(
    self,
    channel: str,
    inactive_days: int,
    balance_min: int,
    balance_max: int | None,
    max_lifetime_earned: int | None,
) -> list[dict]:
    """Return accounts that match all pruning criteria.

    Criteria (all must match):
    - channel matches
    - economy_banned is FALSE (never touch moderation records)
    - lifetime_spent == 0 (never actually spent anything)
    - The following vanity columns are all NULL or empty string: custom_greeting,
      custom_title, chat_color, channel_gif_url, personal_currency_name.
      No other vanity columns exist in the schema at this time.
    - last_seen is older than inactive_days (uses last_seen as the
      authoritative activity clock; last_active can be NULL for users
      who joined but never actively posted)
    - balance is between balance_min and balance_max (inclusive)
    - lifetime_earned is <= max_lifetime_earned if provided
    """
    loop = asyncio.get_running_loop()

    def _sync() -> list[dict]:
        conn = self._get_connection()
        try:
            cutoff = f"-{inactive_days} days"
            params: list[object] = [channel, cutoff]
            query = """
                SELECT username, channel, balance, lifetime_earned, lifetime_spent,
                       first_seen, last_seen, last_active,
                       welcome_wallet_claimed, economy_banned
                FROM accounts
                WHERE channel = ?
                  AND economy_banned = 0
                  AND lifetime_spent = 0
                  AND (custom_greeting IS NULL OR custom_greeting = '')
                  AND (custom_title IS NULL OR custom_title = '')
                  AND (chat_color IS NULL OR chat_color = '')
                  AND (channel_gif_url IS NULL OR channel_gif_url = '')
                  AND (personal_currency_name IS NULL OR personal_currency_name = '')
                  AND last_seen < datetime('now', ?)
                  AND balance >= ?
            """
            params.append(balance_min)
            if balance_max is not None:
                query += " AND balance <= ?"
                params.append(balance_max)
            if max_lifetime_earned is not None:
                query += " AND lifetime_earned <= ?"
                params.append(max_lifetime_earned)
            query += " ORDER BY last_seen ASC"
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _sync)
```

### 3.2 `delete_account_and_cascade`

```python
async def delete_account_and_cascade(self, username: str, channel: str) -> dict:
    """Delete one account and all its child records atomically.

    Deletes from (in order):
    - daily_activity
    - transactions
    - tip_history (sender or receiver)
    - vanity_items (if table exists)
    - accounts

    Returns a dict of row counts per table, e.g.:
    {"daily_activity": 12, "transactions": 3, "tip_history": 0,
     "vanity_items": 0, "accounts": 1}
    """
    loop = asyncio.get_running_loop()

    def _sync() -> dict:
        conn = self._get_connection()
        try:
            counts: dict[str, int] = {}
            with conn:
                for table in ("daily_activity", "transactions"):
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE username = ? AND channel = ?",
                        (username, channel),
                    )
                    counts[table] = cur.rowcount

                # tip_history references sender OR receiver
                cur = conn.execute(
                    "DELETE FROM tip_history "
                    "WHERE channel = ? AND (sender = ? OR receiver = ?)",
                    (channel, username, username),
                )
                counts["tip_history"] = cur.rowcount

                # vanity_items may not exist in all deployments
                try:
                    cur = conn.execute(
                        "DELETE FROM vanity_items WHERE username = ? AND channel = ?",
                        (username, channel),
                    )
                    counts["vanity_items"] = cur.rowcount
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e):
                        raise
                    counts["vanity_items"] = 0

                cur = conn.execute(
                    "DELETE FROM accounts WHERE username = ? AND channel = ?",
                    (username, channel),
                )
                counts["accounts"] = cur.rowcount
            return counts
        finally:
            conn.close()

    return await loop.run_in_executor(None, _sync)
```

---

## 4. CLI: `kryten-economy-prune`

### 4.1 File: `kryten_economy/prune_cli.py`

```python
"""Account pruner CLI — kryten-economy-prune.

Removes ghost accounts: users who received the welcome wallet but never
engaged with the economy. Dry-run by default; use --execute to commit.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kryten-economy-prune",
        description=(
            "Find and optionally remove inactive ghost accounts from the economy DB.\n"
            "Dry-run by default — pass --execute to commit deletions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview: inactive 30+ days, balance 0–100,000 Z
  kryten-economy-prune --channel my-channel --inactive-days 30 --balance-max 100000

  # Same filter but really delete (prompts for confirmation)
  kryten-economy-prune --channel my-channel --inactive-days 30 --balance-max 100000 --execute

  # Also cap lifetime_earned to catch accounts that farmed a bit then left
  kryten-economy-prune --channel my-channel --inactive-days 60 \\
      --balance-min 100 --balance-max 250000 --max-lifetime-earned 500 --execute --yes
        """,
    )

    # ── Required ─────────────────────────────────────────────
    p.add_argument(
        "--channel", required=True,
        help="Channel name to prune (must match the value stored in accounts.channel)",
    )

    # ── Filter options ────────────────────────────────────────
    p.add_argument(
        "--inactive-days", type=int, default=30, metavar="DAYS",
        help="Minimum days since last_seen (default: 30)",
    )
    p.add_argument(
        "--balance-min", type=int, default=0, metavar="Z",
        help="Minimum balance to consider for pruning (default: 0)",
    )
    p.add_argument(
        "--balance-max", type=int, default=None, metavar="Z",
        help="Maximum balance to consider for pruning (default: no upper limit)",
    )
    p.add_argument(
        "--max-lifetime-earned", type=int, default=None, metavar="Z",
        help=(
            "Only prune accounts whose total lifetime_earned is at or below this value. "
            "Useful for targeting accounts that never progressed past the welcome wallet. "
            "(default: no limit)"
        ),
    )

    # ── Execution mode ────────────────────────────────────────
    p.add_argument(
        "--execute", action="store_true",
        help="Actually delete matched accounts. Default is dry-run (no changes made).",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt in --execute mode.",
    )

    # ── Output ────────────────────────────────────────────────
    p.add_argument(
        "--output-csv", type=str, default=None, metavar="FILE",
        help=(
            "Write matched accounts to a CSV file before any deletion. "
            "In dry-run mode this is the only output. In execute mode it serves "
            "as the audit trail."
        ),
    )

    # ── Config / infra ────────────────────────────────────────
    p.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    p.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: WARNING — keeps output clean)",
    )

    return p


async def _run(args: argparse.Namespace) -> int:
    """Async main — returns exit code."""
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # ── Config / DB path resolution ───────────────────────────
    config_path = args.config
    if not config_path:
        for candidate in ("/etc/kryten/kryten-economy/config.yaml", "./config.yaml"):
            if Path(candidate).exists():
                config_path = candidate
                break
    if not config_path:
        print("ERROR: No config file found. Use --config or place config.yaml in CWD.")
        return 1

    from .config import load_config
    from .database import EconomyDatabase

    try:
        cfg = load_config(config_path)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        return 1

    db = EconomyDatabase(cfg.database.path)
    await db.initialize()

    # ── Find matches ──────────────────────────────────────────
    mode_label = "DRY RUN" if not args.execute else "EXECUTE"
    print(
        f"\n{'─' * 60}\n"
        f"  kryten-economy-prune  [{mode_label}]\n"
        f"{'─' * 60}\n"
        f"  Channel          : {args.channel}\n"
        f"  Inactive ≥        : {args.inactive_days} days\n"
        f"  Balance range    : {args.balance_min:,} Z"
        + (f" – {args.balance_max:,} Z" if args.balance_max is not None else " – (no limit)")
        + "\n"
        + (
            f"  Max lifetime earned: {args.max_lifetime_earned:,} Z\n"
            if args.max_lifetime_earned is not None
            else ""
        )
        + f"{'─' * 60}\n"
    )

    accounts = await db.find_purgeable_accounts(
        channel=args.channel,
        inactive_days=args.inactive_days,
        balance_min=args.balance_min,
        balance_max=args.balance_max,
        max_lifetime_earned=args.max_lifetime_earned,
    )

    if not accounts:
        print("No accounts match the given criteria. Nothing to do.")
        return 0

    total_z = sum(a["balance"] for a in accounts)

    # ── Tabular preview ───────────────────────────────────────
    print(f"Found {len(accounts):,} account(s) matching criteria:")
    print(f"  Total balance to be reclaimed: {total_z:,} Z\n")

    col_w = (20, 10, 12, 12, 26)   # username, balance, earned, spent, last_seen
    header = (
        f"{'Username':<{col_w[0]}}  {'Balance':>{col_w[1]}}  "
        f"{'Earned':>{col_w[2]}}  {'Last Seen':<{col_w[4]}}"
    )
    print(header)
    print("─" * (sum(col_w) + 8))
    for a in accounts:
        print(
            f"{a['username']:<{col_w[0]}}  "
            f"{a['balance']:>{col_w[1]},}  "
            f"{a['lifetime_earned']:>{col_w[2]},}  "
            f"{(a['last_seen'] or 'unknown'):<{col_w[4]}}"
        )
    print()

    # ── CSV output (before any deletion) ─────────────────────
    if args.output_csv:
        csv_path = Path(args.output_csv)
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "username", "channel", "balance", "lifetime_earned",
                    "lifetime_spent", "first_seen", "last_seen", "last_active",
                    "welcome_wallet_claimed",
                ],
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(accounts)
        print(f"Matched accounts written to: {csv_path.resolve()}\n")

    # ── Dry-run exit ──────────────────────────────────────────
    if not args.execute:
        print(
            "DRY RUN — no changes made.\n"
            "Re-run with --execute to perform deletions.\n"
            "Tip: use --output-csv to save this list as an audit trail first."
        )
        return 0

    # ── Confirmation prompt ───────────────────────────────────
    if not args.yes:
        try:
            answer = input(
                f"⚠️  About to permanently delete {len(accounts):,} account(s) "
                f"and reclaim {total_z:,} Z.\n"
                "Type 'yes' to confirm, anything else to abort: "
            )
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    # ── Execute deletions ─────────────────────────────────────
    audit_path: Path | None = None
    if args.output_csv:
        # If CSV was already written, append an _audit suffix for the deletion record
        stem = Path(args.output_csv).stem
        audit_path = Path(args.output_csv).with_name(
            f"{stem}_deleted_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.csv"
        )
    else:
        audit_path = Path(
            f"prune_audit_{args.channel}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.csv"
        )

    deleted = 0
    failed = 0
    with audit_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "username", "channel", "balance", "lifetime_earned", "last_seen",
                "deleted_at", "rows_transactions", "rows_daily_activity", "rows_tip_history",
            ],
        )
        writer.writeheader()

        for acct in accounts:
            try:
                counts = await db.delete_account_and_cascade(
                    acct["username"], acct["channel"]
                )
                deleted += 1
                writer.writerow(
                    {
                        "username": acct["username"],
                        "channel": acct["channel"],
                        "balance": acct["balance"],
                        "lifetime_earned": acct["lifetime_earned"],
                        "last_seen": acct["last_seen"],
                        "deleted_at": datetime.now(timezone.utc).isoformat(),
                        "rows_transactions": counts.get("transactions", 0),
                        "rows_daily_activity": counts.get("daily_activity", 0),
                        "rows_tip_history": counts.get("tip_history", 0),
                    }
                )
            except Exception as exc:
                failed += 1
                print(f"  ERROR deleting {acct['username']}: {exc}")

    print(
        f"\n✅ Done. Deleted {deleted:,} account(s)"
        + (f" ({failed} failed — see output above)" if failed else "")
        + f"\n   Reclaimed approximately {total_z:,} Z from circulation."
        f"\n   Audit log: {audit_path.resolve()}\n"
    )
    return 0 if failed == 0 else 2


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
```

### 4.2 `pyproject.toml` — scripts entry

Add to the `[project.scripts]` table:

```toml
[project.scripts]
kryten-economy = "kryten_economy.__main__:main"
kryten-economy-prune = "kryten_economy.prune_cli:main"   # NEW
```

---

## 5. Output & Reporting

### 5.1 Dry-run example

```
────────────────────────────────────────────────────────────
  kryten-economy-prune  [DRY RUN]
────────────────────────────────────────────────────────────
  Channel          : 420grindhouse
  Inactive ≥        : 30 days
  Balance range    : 0 Z – 100,000 Z
────────────────────────────────────────────────────────────

Found 143 account(s) matching criteria:
  Total balance to be reclaimed: 18,250 Z

Username              Balance        Earned   Last Seen
────────────────────────────────────────────────────────────────────
AnonDrifter7              100           100   2026-03-14 02:17:45
GhostUser99               250           250   2026-02-01 21:05:12
...

DRY RUN — no changes made.
Re-run with --execute to perform deletions.
Tip: use --output-csv to save this list as an audit trail first.
```

### 5.2 Execute example

```
⚠️  About to permanently delete 143 account(s) and reclaim 18,250 Z.
Type 'yes' to confirm, anything else to abort: yes

✅ Done. Deleted 143 account(s)
   Reclaimed approximately 18,250 Z from circulation.
   Audit log: /home/kryten/prune_audit_420grindhouse_20260804T141523.csv
```

### 5.3 Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (dry-run or execute with no errors) |
| `1` | Fatal error (config load failure, aborted by user) |
| `2` | Partial failure (some accounts could not be deleted — see output) |

---

## 6. Test Specifications

> **pytest-asyncio required.** All `async def` test functions require pytest-asyncio.
> Add the following to `pyproject.toml` under `[tool.pytest.ini_options]` (already
> present in kryten-economy — verify it is set):
> ```toml
> asyncio_mode = "auto"
> ```
> Without this setting, async tests will be silently skipped or errored. Do **not**
> add `@pytest.mark.asyncio` decorators individually — rely on `asyncio_mode = "auto"`.

File: `tests/test_prune_accounts.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from kryten_economy.prune_cli import build_parser, _run


# ── Database method tests ─────────────────────────────────────────────


async def test_find_purgeable_excludes_banned(db_with_accounts):
    """economy_banned accounts are never returned."""
    # db_with_accounts is a fixture that creates a temp SQLite DB with
    # a mix of accounts: one banned, one active, one ghost
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test", inactive_days=1,
        balance_min=0, balance_max=None, max_lifetime_earned=None,
    )
    usernames = [r["username"] for r in results]
    assert "banned_user" not in usernames


async def test_find_purgeable_excludes_spenders(db_with_accounts):
    """Accounts with lifetime_spent > 0 are never returned."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test", inactive_days=1,
        balance_min=0, balance_max=None, max_lifetime_earned=None,
    )
    assert all(r["lifetime_spent"] == 0 for r in results)


async def test_find_purgeable_excludes_vanity(db_with_accounts):
    """Accounts with a chat_color (or any vanity column) are never returned."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test", inactive_days=1,
        balance_min=0, balance_max=None, max_lifetime_earned=None,
    )
    assert all((r.get("chat_color") or "") == "" for r in results)


async def test_find_purgeable_balance_range_filter(db_with_accounts):
    """balance_min / balance_max are inclusive bounds."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test", inactive_days=1,
        balance_min=50, balance_max=200, max_lifetime_earned=None,
    )
    for r in results:
        assert 50 <= r["balance"] <= 200


async def test_find_purgeable_max_lifetime_earned(db_with_accounts):
    """max_lifetime_earned filters out high earners."""
    results = await db_with_accounts.find_purgeable_accounts(
        channel="test", inactive_days=1,
        balance_min=0, balance_max=None, max_lifetime_earned=200,
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
    args = parser.parse_args([
        "--channel", "my-channel",
        "--inactive-days", "60",
        "--balance-min", "100",
        "--balance-max", "50000",
        "--max-lifetime-earned", "500",
        "--execute", "--yes",
    ])
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
    mock_db.find_purgeable_accounts = AsyncMock(return_value=[
        {
            "username": "ghost", "channel": "test", "balance": 100,
            "lifetime_earned": 100, "lifetime_spent": 0,
            "first_seen": "2026-01-01", "last_seen": "2026-01-01",
            "last_active": None, "welcome_wallet_claimed": 1,
        }
    ])
    mock_db.delete_account_and_cascade = AsyncMock()

    config_file = tmp_path / "config.yaml"
    config_file.write_text("database:\n  path: test.db\n")

    parser = build_parser()
    args = parser.parse_args([
        "--channel", "test", "--config", str(config_file),
    ])

    with patch("kryten_economy.prune_cli.load_config"), \
         patch("kryten_economy.prune_cli.EconomyDatabase", return_value=mock_db):
        rc = await _run(args)

    mock_db.delete_account_and_cascade.assert_not_called()
    assert rc == 0
```

> **Fixture `db_with_accounts`** — create in `tests/conftest.py` as a `pytest.fixture`
> that builds a `tmp_path`-backed SQLite DB, calls `await db.initialize()`, and inserts
> the following rows (all in `channel='test'`). Use `inactive_days=1` in tests so that
> only `last_seen` values in the past trigger the filter.
>
> | username | balance | lifetime_earned | lifetime_spent | economy_banned | chat_color | last_seen (relative to now) |
> |---|---|---|---|---|---|---|
> | `ghost_user` | 100 | 100 | 0 | 0 | NULL | `2020-01-01` (far past) |
> | `banned_user` | 100 | 100 | 0 | 1 | NULL | `2020-01-01` (far past) |
> | `active_user` | 100 | 100 | 0 | 0 | NULL | today (recent) |
> | `color_user` | 100 | 100 | 0 | 0 | `#ff0000` | `2020-01-01` (far past) |
> | `spender_user` | 50 | 100 | 50 | 0 | NULL | `2020-01-01` (far past) |
>
> All other vanity columns (`custom_greeting`, `custom_title`, `channel_gif_url`,
> `personal_currency_name`) are NULL or empty string for all rows unless explicitly
> testing that column.

---

## 7. Acceptance Criteria

**Safety (non-negotiable)**
- [ ] Economy-banned accounts are never returned by `find_purgeable_accounts`
- [ ] Accounts with `lifetime_spent > 0` are never returned
- [ ] Accounts with any non-empty vanity column are never returned
- [ ] Running without `--execute` makes zero writes to the DB
- [ ] Running with `--execute` but without `--yes` prompts for confirmation and aborts on
      anything other than `"yes"`

**Filtering**
- [ ] `--inactive-days N` excludes accounts with `last_seen` within the last N days
- [ ] `--balance-min` / `--balance-max` are inclusive and can be combined independently
- [ ] `--max-lifetime-earned` filters out accounts above the threshold
- [ ] No filter options are required except `--channel`

**Output**
- [ ] Dry-run prints a human-readable table with username, balance, lifetime_earned, last_seen
- [ ] Dry-run prints the total Z that would be reclaimed
- [ ] `--output-csv` writes a valid CSV before any deletion occurs
- [ ] Execute mode writes a timestamped audit CSV regardless of `--output-csv`
- [ ] Exit code `0` on success, `1` on fatal error, `2` on partial failure

**Cascade**
- [ ] `delete_account_and_cascade` deletes rows from `daily_activity`, `transactions`,
      `tip_history`, `vanity_items` (if present), and `accounts` in a single transaction
- [ ] A deleted account is no longer returned by `get_account()`

**Tooling**
- [ ] `kryten-economy-prune --help` prints usage without error
- [ ] `kryten-economy-prune` is registered in `[project.scripts]` in `pyproject.toml`
- [ ] `uv run black --check .` passes
- [ ] `uv run ruff check .` passes
- [ ] `uv run mypy kryten_economy` passes
- [ ] `asyncio_mode = "auto"` is set under `[tool.pytest.ini_options]` in `pyproject.toml`
- [ ] All existing tests pass unmodified
