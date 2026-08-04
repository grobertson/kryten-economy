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

from .config import load_config
from .database import EconomyDatabase


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
        "--channel",
        required=True,
        help="Channel name to prune (must match the value stored in accounts.channel)",
    )

    # ── Filter options ────────────────────────────────────────
    p.add_argument(
        "--inactive-days",
        type=int,
        default=30,
        metavar="DAYS",
        help="Minimum days since last_seen (default: 30)",
    )
    p.add_argument(
        "--balance-min",
        type=int,
        default=0,
        metavar="Z",
        help="Minimum balance to consider for pruning (default: 0)",
    )
    p.add_argument(
        "--balance-max",
        type=int,
        default=None,
        metavar="Z",
        help="Maximum balance to consider for pruning (default: no upper limit)",
    )
    p.add_argument(
        "--max-lifetime-earned",
        type=int,
        default=None,
        metavar="Z",
        help=(
            "Only prune accounts whose total lifetime_earned is at or below this value. "
            "Useful for targeting accounts that never progressed past the welcome wallet. "
            "(default: no limit)"
        ),
    )

    # ── Execution mode ────────────────────────────────────────
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete matched accounts. Default is dry-run (no changes made).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt in --execute mode.",
    )

    # ── Output ────────────────────────────────────────────────
    p.add_argument(
        "--output-csv",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Write matched accounts to a CSV file before any deletion. "
            "In dry-run mode this is the only output. In execute mode it serves "
            "as the audit trail."
        ),
    )

    # ── Config / infra ────────────────────────────────────────
    p.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    p.add_argument(
        "--log-level",
        default="WARNING",
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
        f"  Inactive \u2265        : {args.inactive_days} days\n"
        f"  Balance range    : {args.balance_min:,} Z"
        + (
            f" \u2013 {args.balance_max:,} Z"
            if args.balance_max is not None
            else " \u2013 (no limit)"
        )
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

    col_w = (20, 10, 12, 12, 26)  # username, balance, earned, spent, last_seen
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
                    "username",
                    "channel",
                    "balance",
                    "lifetime_earned",
                    "lifetime_spent",
                    "first_seen",
                    "last_seen",
                    "last_active",
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
                f"\u26a0\ufe0f  About to permanently delete {len(accounts):,} account(s) "
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
    audit_path: Path
    if args.output_csv:
        # Place audit file alongside the output CSV with a _deleted_TIMESTAMP suffix
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
                "username",
                "channel",
                "balance",
                "lifetime_earned",
                "last_seen",
                "deleted_at",
                "rows_transactions",
                "rows_daily_activity",
                "rows_tip_history",
            ],
        )
        writer.writeheader()

        for acct in accounts:
            try:
                counts = await db.delete_account_and_cascade(acct["username"], acct["channel"])
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
        f"\n\u2705 Done. Deleted {deleted:,} account(s)"
        + (f" ({failed} failed \u2014 see output above)" if failed else "")
        + f"\n   Reclaimed approximately {total_z:,} Z from circulation."
        f"\n   Audit log: {audit_path.resolve()}\n"
    )
    return 0 if failed == 0 else 2


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
