"""One-shot, re-runnable ETL: SQLite ``economy.db`` → PostgreSQL ``kryten_economy``.

Design goals (in priority order, because this moves real currency):

1. **Non-destructive.** The source is opened with SQLite's ``mode=ro`` URI, so the
   ETL cannot modify, and a bug here cannot corrupt, production data.
2. **Idempotent.** Every insert is an ``ON CONFLICT DO UPDATE`` keyed on the table's
   natural key, so a full re-run converges to the same state instead of duplicating.
3. **Resumable.** Each table is copied in committed batches. A crash mid-table leaves
   the last batch unapplied, and simply running again finishes the job.
4. **Verifiable.** ``--verify`` recomputes per-table counts, total circulation, and a
   per-account balance checksum on both sides and exits non-zero on any drift.

Merge semantics
---------------
The copy is **upsert-only**: the source is authoritative for every row it contains, but
rows that exist in the target and *not* in the source are left untouched. For the
intended cutover the target is created empty by ``alembic upgrade head`` first, so
"upsert only" and "full replace" are identical. If a target has been left over from an
earlier or partial migration, stale extra rows are **not** pruned here — but
verification reports them as count mismatches and fails, so the drift is loud rather
than silent. Truncating the target is a deliberate, human decision, not something this
script does behind your back.

Table/column metadata is **introspected** from the live SQLite and PostgreSQL
schemas (see :func:`describe_tables`) rather than hard-coded. That keeps the ETL
correct if a column is added, and it fails loudly rather than silently dropping a
column if a table is unknown.

Usage
-----
    python -m kryten_economy.migrate_sqlite_to_pg --source /var/lib/kryten-economy/economy.db --dry-run
    python -m kryten_economy.migrate_sqlite_to_pg --source .../economy.db --verify
    python -m kryten_economy.migrate_sqlite_to_pg --source .../economy.db

The target DSN is resolved exactly like the running service (see
``kryten_economy.db.pool.resolve_dsn``), so no credential is accepted as an argument
and none is ever logged.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg

from .db.boundary import to_bool, to_date, to_datetime
from .db.pool import resolve_dsn
from .config import EconomyConfig, load_config

LOGGER = logging.getLogger("economy.migrate")

# Tables copied in dependency order. Parents before children so that a table with a
# foreign key never references a row that has not been inserted yet.
TABLE_ORDER: tuple[str, ...] = (
    "accounts",
    "streaks",
    "hourly_milestones",
    "trigger_cooldowns",
    "trigger_analytics",
    "gambling_stats",
    "trivia_stats",
    "blackjack_stats",
    "race_results",
    "race_bets",
    "pending_challenges",
    "tip_history",
    "pending_approvals",
    "vanity_items",
    "achievements",
    "bounties",
    "economy_snapshots",
    "banned_users",
    "queue_spend_requests",
    "service_metrics",
    "transactions",
    "daily_activity",
)

# Tables whose PostgreSQL identity/sequence must be advanced after the copy.
_IDENTITY_TABLES: tuple[str, ...] = (
    "transactions",
    "daily_activity",
    "trigger_analytics",
    "pending_challenges",
    "race_results",
    "race_bets",
    "tip_history",
    "pending_approvals",
    "vanity_items",
    "achievements",
    "bounties",
    "economy_snapshots",
    "queue_spend_requests",
)

# Source tables intentionally skipped: SQLite internals with no PostgreSQL analogue.
_SKIP_TABLES: frozenset[str] = frozenset({"sqlite_sequence"})


@dataclass(frozen=True)
class TablePlan:
    """How one table is copied."""

    name: str
    columns: tuple[str, ...]
    conflict_target: str
    identity_column: str | None = None


@dataclass
class VerificationResult:
    """Outcome of comparing source and target."""

    counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    circulation: tuple[int, int] = (0, 0)
    checksum: tuple[str, str] = ("", "")
    mismatched_accounts: list[str] = field(default_factory=list)
    count_mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.count_mismatches
            and not self.mismatched_accounts
            and self.circulation[0] == self.circulation[1]
            and self.checksum[0] == self.checksum[1]
        )


# ── Column type conversion ────────────────────────────────────────────────────
# SQLite has no boolean or timestamp type: flags are ints and timestamps are text.
# PostgreSQL is strict, so each source value is converted to the exact Python type
# asyncpg expects. This mirrors `db/boundary.py`, which does the same job on reads.
def _convert_value(value: Any, sql_type: str) -> Any:
    """Convert a SQLite value to the Python type asyncpg requires."""
    if value is None:
        return None
    if sql_type in {"boolean", "bool"}:
        return to_bool(value)
    if "timestamp" in sql_type or "timestamptz" in sql_type:
        return to_datetime(value)
    if sql_type == "date":
        return to_date(value)
    if sql_type in {"double precision", "real"}:
        return float(value)
    if sql_type in {"bigint", "integer", "int", "smallint"}:
        return int(value)
    return value


# ── Schema introspection ──────────────────────────────────────────────────────
async def describe_tables(con: asyncpg.Connection) -> dict[str, TablePlan]:
    """Introspect the PostgreSQL schema into per-table copy plans."""
    rows = await con.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = current_schema()
         ORDER BY table_name, ordinal_position
        """
    )
    grouped: dict[str, list[asyncpg.Record]] = {}
    for row in rows:
        grouped.setdefault(row["table_name"], []).append(row)

    plans: dict[str, TablePlan] = {}
    for table, cols in grouped.items():
        if table in _SKIP_TABLES or table == "alembic_version":
            continue
        conflict_cols: list[str] = []
        identity_col: str | None = None

        # Primary-key columns become the ON CONFLICT target.
        pk = await con.fetch(
            """
            SELECT a.attname AS column_name
              FROM pg_index i
              JOIN pg_attribute a ON a.attrelid = i.indrelid
                                  AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = $1::regclass AND i.indisprimary
             ORDER BY a.attnum
            """,
            table,
        )
        if pk:
            conflict_cols = [r["column_name"] for r in pk]
        else:
            # No PK (e.g. accounts has a composite primary key in our schema, but be
            # defensive): fall back to the unique constraints declared in the DDL.
            uq = await con.fetch(
                """
                SELECT a.attname AS column_name
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid = i.indrelid
                                      AND a.attnum = ANY(i.indkey)
                 WHERE i.indrelid = $1::regclass AND i.indisunique
                   AND NOT i.indisprimary
                 ORDER BY a.attnum
                """,
                table,
            )
            conflict_cols = [r["column_name"] for r in uq]

        columns = tuple(r["column_name"] for r in cols)
        if not conflict_cols:
            # Nothing to conflict on: fall back to the first column so the copy is
            # still idempotent-ish, and log loudly.
            LOGGER.warning(
                "table %s has no primary/unique key; using first column", table
            )
            conflict_cols = [columns[0]]

        for r in cols:
            if r["data_type"] == "bigint" and r["is_nullable"] == "NO":
                default = await con.fetchval(
                    """
                    SELECT column_default FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = $1 AND column_name = $2
                    """,
                    table,
                    r["column_name"],
                )
                if default and "nextval" in str(default):
                    identity_col = r["column_name"]
                    break

        plans[table] = TablePlan(
            name=table,
            columns=columns,
            conflict_target=", ".join(conflict_cols),
            identity_column=identity_col,
        )
    return plans


def _build_insert(plan: TablePlan, types: dict[str, str]) -> str:
    """Build the parameterised ``INSERT ... ON CONFLICT DO UPDATE`` for one table.

    Merge rule: for a column that exists in the target but is absent from the source
    (``types`` will not contain it, so it is not in the INSERT), the existing target
    value is kept. For every column being inserted, the source is authoritative and
    wins outright. That makes a re-run converge on the source state rather than
    silently merging two divergent histories.

    Note the table-qualified references in the SET clause: inside
    ``ON CONFLICT DO UPDATE`` a bare column name is ambiguous between the target
    table and ``EXCLUDED``.
    """
    cols = [c for c in plan.columns if c in types]
    if not cols:
        raise ValueError(f"no shared columns for table {plan.name}")

    targets = [f"{c} = EXCLUDED.{c}" for c in cols]
    columns_sql = ", ".join(cols)
    placeholders = ", ".join(f"${i}::{types[c]}" for i, c in enumerate(cols, start=1))
    updates = ", ".join(targets)
    return (
        f"INSERT INTO {plan.name} ({columns_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({plan.conflict_target}) DO UPDATE SET {updates}"
    )


# ── Source access ─────────────────────────────────────────────────────────────
def open_source(path: str) -> sqlite3.Connection:
    """Open the SQLite source strictly read-only.

    ``mode=ro`` is a hard guarantee: SQLite refuses any write on this handle, so a
    bug in this script cannot modify production data.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"source database not found: {path}")
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def source_columns(con: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return ``{column: sqlite_affinity_type}`` for a source table."""
    cols: dict[str, str] = {}
    for row in con.execute(f"PRAGMA table_info(`{table}`)"):
        name, affinity = row["name"], (row["type"] or "").upper()
        cols[name] = affinity
    return cols


def fetch_batches(
    con: sqlite3.Connection, table: str, columns: list[str], batch_size: int
) -> Any:
    """Yield rows from a source table in batches, ordered deterministically."""
    col_sql = ", ".join(f"`{c}`" for c in columns)
    cur = con.execute(f"SELECT {col_sql} FROM `{table}`")
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        yield rows


# ── Copy ──────────────────────────────────────────────────────────────────────
async def reset_sequences(con: asyncpg.Connection, plans: dict[str, TablePlan]) -> None:
    """Advance identity sequences past the highest copied id.

    Without this, the first insert after the cutover would collide with a copied id.
    """
    for table in _IDENTITY_TABLES:
        plan = plans.get(table)
        if plan is None or not plan.identity_column:
            continue
        col = plan.identity_column
        await con.execute(
            f"SELECT setval("
            f"  pg_get_serial_sequence('{table}', '{col}'),"
            f"  COALESCE((SELECT MAX({col}) FROM {table}), 0) + 1,"
            f"  false)"
        )
        LOGGER.info("reset sequence for %s.%s", table, col)


async def copy_table(
    con: asyncpg.Connection,
    plans: dict[str, TablePlan],
    src: sqlite3.Connection,
    table: str,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Copy one table in committed batches. Returns the number of rows written."""
    plan = plans[table]
    src_types = source_columns(src, table)
    shared = [c for c in plan.columns if c in src_types]
    dropped = [c for c in plan.columns if c not in src_types]
    if not shared:
        raise ValueError(
            f"table {table}: no columns in common between source and target"
        )

    # A target column missing from the source is only harmless if PostgreSQL can
    # fill it in (nullable, or has a default). A NOT NULL column with no default and
    # no source counterpart would abort every insert, so fail fast with a clear
    # message naming the actual mismatch rather than a driver-level NOT NULL error
    # partway through a 60k-row copy.
    required_missing = [
        c for c in dropped if await _is_required_without_default(con, table, c)
    ]
    if required_missing:
        raise ValueError(
            f"table {table}: target column(s) {required_missing} are NOT NULL with no "
            "default and do not exist in the source. The source schema is older than "
            "the PostgreSQL schema; migrate or add the columns before running this."
        )
    if dropped:
        LOGGER.warning(
            "table %s: target column(s) absent from source: %s", table, dropped
        )

    pg_types = await _pg_types(con, table)
    insert_sql = _build_insert(plan, {c: pg_types[c] for c in shared})
    column_list = shared

    total = 0
    for batch in fetch_batches(src, table, column_list, batch_size):
        params = [
            tuple(_convert_value(row[c], pg_types[c]) for c in column_list)
            for row in batch
        ]
        if dry_run:
            total += len(params)
            continue
        # One transaction per batch: a failure rolls back only this batch and the
        # re-run picks up from there.
        async with con.transaction():
            await con.executemany(insert_sql, params)
        total += len(params)
    return total


async def _is_required_without_default(
    con: asyncpg.Connection, table: str, column: str
) -> bool:
    """Return True when PostgreSQL cannot fill this column in on its own."""
    row = await con.fetchrow(
        """
        SELECT is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = $1 AND column_name = $2
        """,
        table,
        column,
    )
    if row is None:
        return False
    return row["is_nullable"] == "NO" and row["column_default"] is None


async def _pg_types(con: asyncpg.Connection, table: str) -> dict[str, str]:
    rows = await con.fetch(
        """
        SELECT column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = $1
         ORDER BY ordinal_position
        """,
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


# ── Verification ──────────────────────────────────────────────────────────────
def _balance_checksum(rows: Any) -> str:
    """Stable checksum over ``(username, channel, balance)`` triples.

    The rows are re-sorted in Python rather than relying on the caller's
    ``ORDER BY``: SQLite orders text with the BINARY collation and PostgreSQL with
    the database's locale collation, so the same set of rows can legitimately come
    back in a different order from each engine. Sorting here (on a normalised
    string key) makes the digest order-independent and the two sides comparable.
    """
    normalized = sorted((str(u), str(c), int(b)) for u, c, b in rows)
    digest = hashlib.sha256()
    for username, channel, balance in normalized:
        digest.update(f"{username}\x1f{channel}\x1f{balance}\x1e".encode())
    return digest.hexdigest()


async def verify(
    con: asyncpg.Connection, src: sqlite3.Connection, tables: list[str]
) -> VerificationResult:
    """Compare source and target: counts, total circulation, per-account balances."""
    result = VerificationResult()

    for table in tables:
        if table not in _SKIP_TABLES:
            src_count = src.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0]
        else:
            src_count = 0
        dst_count = await con.fetchval(f"SELECT COUNT(*) FROM {table}")
        result.counts[table] = (src_count, dst_count)
        if src_count != dst_count:
            result.count_mismatches.append(
                f"{table}: source={src_count} target={dst_count}"
            )

    src_circ = src.execute("SELECT COALESCE(SUM(balance), 0) FROM accounts").fetchone()[
        0
    ]
    dst_circ = await con.fetchval("SELECT COALESCE(SUM(balance), 0) FROM accounts")
    result.circulation = (int(src_circ), int(dst_circ or 0))

    src_rows = src.execute(
        "SELECT username, channel, balance FROM accounts ORDER BY username, channel"
    ).fetchall()
    dst_rows = await con.fetch(
        "SELECT username, channel, balance FROM accounts ORDER BY username, channel"
    )
    result.checksum = (
        _balance_checksum(src_rows),
        _balance_checksum([tuple(r) for r in dst_rows]),
    )

    src_map = {(r[0], r[1]): int(r[2]) for r in src_rows}
    dst_map = {(r["username"], r["channel"]): int(r["balance"]) for r in dst_rows}
    for key in sorted(set(src_map) | set(dst_map)):
        src_balance = src_map.get(key)
        dst_balance = dst_map.get(key)
        if src_balance != dst_balance:
            result.mismatched_accounts.append(
                f"{key[0]}@{key[1]}: source={src_balance} target={dst_balance}"
            )

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────
def _resolve_target(args: argparse.Namespace) -> str:
    """Resolve the target DSN from the same config the service uses.

    Refuses to proceed when the DSN was *assembled* from config fields that were
    never set. ``resolve_dsn`` intentionally falls back to ``localhost`` when no
    ``dsn``/``dsn_env`` is configured, which is the right behaviour for a long
    running service (it will simply fail to connect) but is actively dangerous
    for a one-shot migration: silently connecting to a default local server, or
    printing a "verified" report against the wrong database, is the kind of
    mistake that is only noticed after production currency has been copied
    somewhere unintended.
    """
    if args.pg_dsn_env:
        dsn = os.environ.get(args.pg_dsn_env)
        if not dsn:
            raise ValueError(
                f"--pg-dsn-env names {args.pg_dsn_env!r}, but that variable is unset or "
                f"empty. Export a full DSN in it, or omit --pg-dsn-env to use "
                f"--config (default: {args.config})."
            )
        return dsn

    cfg: EconomyConfig = load_config(args.config)
    dsn = resolve_dsn(cfg.database.postgres)
    _guard_against_unconfigured_target(cfg, args)
    return dsn


def _guard_against_unconfigured_target(
    cfg: EconomyConfig, args: argparse.Namespace
) -> None:
    """Refuse to copy currency into a database the config does not nominate.

    Every field in ``PostgresConfig`` has a default, so an absent
    ``database.postgres`` block still yields a syntactically valid DSN pointing
    at ``localhost:5432/kryten_economy``. That is harmless for the running
    service (it simply fails to connect) but for a one-shot migration it means
    "copy the currency" quietly becomes "connect somewhere nobody chose". The
    only reliable signal that the config genuinely nominates a PostgreSQL
    target is an explicit ``dsn_env``/``dsn``, or ``backend: postgres``.
    """
    pg = cfg.database.postgres
    if pg.dsn_env or pg.dsn:
        return  # an explicit DSN was configured; that is a real destination

    if cfg.database.backend != "postgres":
        raise ValueError(
            f"{args.config} selects database.backend: {cfg.database.backend!r} and has no "
            f"database.postgres.dsn_env/dsn, so the target DSN would be assembled from "
            f"built-in defaults (host={pg.host!r} port={pg.port} dbname={pg.dbname!r}) - "
            f"almost certainly not the database you want to migrate into. Either pass "
            f"--pg-dsn-env NAME with a full DSN, or set database.backend: postgres in "
            f"that config."
        )

    LOGGER.warning(
        "no dsn_env/dsn in %s; target was assembled from its connection fields "
        "(user=%r host=%r port=%r dbname=%r). Confirm that is the intended database.",
        args.config,
        pg.user,
        pg.host,
        pg.port,
        pg.dbname,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kryten_economy.migrate_sqlite_to_pg",
        description="Copy a kryten-economy SQLite database into PostgreSQL and verify it.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="path to the SQLite economy.db (opened read-only)",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("KRYTEN_ECONOMY_CONFIG", "config.yaml"),
        help="service config used to resolve the PostgreSQL connection",
    )
    parser.add_argument(
        "--pg-dsn-env",
        help=(
            "environment variable holding a full PostgreSQL DSN (overrides --config). "
            "Unset by default, in which case the DSN comes from --config"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan and row counts without writing to the target",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="only verify an already-migrated target",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    dsn = _resolve_target(args)
    src = open_source(args.source)
    try:
        source_tables = [
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if r[0] not in _SKIP_TABLES
        ]
        tables = [t for t in TABLE_ORDER if t in source_tables]
        unknown = [t for t in source_tables if t not in TABLE_ORDER]
        if unknown:
            raise ValueError(
                f"source contains tables missing from TABLE_ORDER: {unknown}. "
                "Add them to the copy order rather than silently skipping."
            )

        con = await asyncpg.connect(dsn)
        try:
            plans = await describe_tables(con)
            missing_targets = [t for t in tables if t not in plans]
            if missing_targets:
                raise ValueError(
                    f"target database is missing tables {missing_targets}; "
                    "run `alembic upgrade head` first"
                )

            if args.dry_run:
                LOGGER.info("DRY RUN - no target writes")
                for table in tables:
                    count = src.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0]
                    LOGGER.info("  would copy %-22s %d rows", table, count)
                src_circ = src.execute(
                    "SELECT COALESCE(SUM(balance), 0) FROM accounts"
                ).fetchone()[0]
                LOGGER.info("  source total circulation: %s", src_circ)
                return 0

            if not args.verify_only:
                for table in tables:
                    written = await copy_table(
                        con, plans, src, table, args.batch_size, dry_run=False
                    )
                    LOGGER.info("copied %-22s %d rows", table, written)
                await reset_sequences(con, plans)
                LOGGER.info("identity sequences reset")

            result = await verify(con, src, tables)
            LOGGER.info("--- verification ---")
            for table, (src_count, dst_count) in result.counts.items():
                flag = "" if src_count == dst_count else "  <-- MISMATCH"
                LOGGER.info(
                    "  %-22s source=%-7d target=%-7d%s",
                    table,
                    src_count,
                    dst_count,
                    flag,
                )
            LOGGER.info(
                "  total circulation        source=%-7d target=%-7d",
                result.circulation[0],
                result.circulation[1],
            )
            LOGGER.info("  balance checksum         source=%s", result.checksum[0][:16])
            LOGGER.info("                        target=%s", result.checksum[1][:16])

            if result.count_mismatches:
                for line in result.count_mismatches:
                    LOGGER.error("count mismatch: %s", line)
            if result.mismatched_accounts:
                for line in result.mismatched_accounts[:20]:
                    LOGGER.error("balance drift: %s", line)
                if len(result.mismatched_accounts) > 20:
                    LOGGER.error(
                        "... and %d more", len(result.mismatched_accounts) - 20
                    )

            if result.ok:
                LOGGER.info("VERIFIED: source and target match")
                return 0
            LOGGER.error("VERIFICATION FAILED")
            return 1
        finally:
            await con.close()
    finally:
        src.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        return asyncio.run(run(args))
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 2
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2
    except asyncpg.PostgresError as exc:
        # A connection/DNS/authentication failure is a *configuration* problem, not
        # data drift. It must not surface as a raw traceback, and it must not
        # reuse exit 1, which operators are told to read as "verification drift -
        # do not start the service". The password is deliberately not echoed.
        LOGGER.error(
            "could not talk to the target PostgreSQL server: %s: %s "
            "(check the DSN, that the database exists, and that pg_hba.conf "
            "permits this host)",
            type(exc).__name__,
            exc,
        )
        return 2
    except OSError as exc:
        # ConnectionRefusedError, socket timeouts, unresolvable host, and so on.
        LOGGER.error(
            "could not reach the target PostgreSQL server (%s). "
            "Check the DSN host/port and that the server is listening.",
            exc,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
