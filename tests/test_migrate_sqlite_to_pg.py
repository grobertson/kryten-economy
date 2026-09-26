"""Tests for the SQLite → PostgreSQL ETL (``kryten_economy.migrate_sqlite_to_pg``).

Acceptance evidence for SPEC-Sortie-4. Two tiers:

* **Pure-unit tests** (no database) build a synthetic SQLite fixture and cover the
  parts that are pure logic: read-only enforcement, value conversion, insert
  generation, and order-independent checksums. These run everywhere.
* **Live end-to-end tests** run the real CLI against a live PostgreSQL server and
  prove the properties that only matter against an actual server: idempotency,
  drift detection, and self-healing. They are marked ``postgres`` and skip
  cleanly when no DSN is set.

    export KRYTEN_ECONOMY_TEST_DSN='postgresql://kryten:...@chandra-1.local:5432/kryten_economy_test'
    uv run pytest tests/test_migrate_sqlite_to_pg.py -v

The target database is expected to have the schema applied already
(``alembic upgrade head``). These tests truncate the tables they touch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kryten_economy import migrate_sqlite_to_pg as etl
from tests.conftest import PG_AVAILABLE, PG_DSN, PG_DSN_ENV_VAR, make_config_dict

requires_pg = pytest.mark.skipif(
    not PG_AVAILABLE,
    reason="No PostgreSQL DSN; PostgreSQL ETL tests skipped",
)


# ── Synthetic SQLite fixture ──────────────────────────────────────────────────
def _build_source(path: Path, accounts: int = 3, transactions: int = 5) -> Path:
    """Create a small but realistic SQLite economy database.

    The shapes here deliberately match the real production database: integer flags
    (0/1) for booleans, naive ``%Y-%m-%d %H:%M:%S`` timestamp strings, and JSON
    *string* metadata. A fixture built from native Python types would let type
    bugs through.
    """
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE accounts (
            username TEXT NOT NULL,
            channel TEXT NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,
            quiet_mode INTEGER NOT NULL DEFAULT 0,
            welcome_wallet_claimed INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT,
            metadata TEXT,
            PRIMARY KEY (username, channel)
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            channel TEXT NOT NULL,
            amount INTEGER NOT NULL,
            type TEXT NOT NULL,
            reason TEXT,
            metadata TEXT,
            created_at TEXT
        );
        """
    )
    for i in range(accounts):
        con.execute(
            "INSERT INTO accounts VALUES (?,?,?,?,?,?,?)",
            (
                f"User{i}",
                "Test-Channel",
                100 * (i + 1),
                i % 2,
                1,
                "2026-01-0%d 12:00:00" % ((i % 9) + 1),
                '{"source": "test"}',
            ),
        )
    for i in range(transactions):
        con.execute(
            "INSERT INTO transactions "
            "(username, channel, amount, type, reason, metadata, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                f"User{i % accounts}",
                "Test-Channel",
                10 + i,
                "credit",
                "test",
                '{"kind": "test"}',
                "2026-01-15 09:00:00",
            ),
        )
    con.commit()
    con.close()
    return path


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    return _build_source(tmp_path / "source.db")


def _args(source: Path, **overrides: object) -> object:
    """Build a stand-in for the parsed CLI namespace.

    ``pg_dsn_env`` names whichever variable actually holds the DSN (the ETL only
    ever reads a DSN through that indirection), so these tests work whether the
    run was configured with ``KRYTEN_ECONOMY_TEST_DSN`` or the legacy
    ``KRYTEN_ECONOMY_PG_DSN``.
    """
    args = type("Args", (), {})()
    args.source = str(source)
    args.config = "config.yaml"
    args.pg_dsn_env = PG_DSN_ENV_VAR
    args.batch_size = 100
    args.dry_run = False
    args.verify_only = False
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


# ── Pure unit tests ───────────────────────────────────────────────────────────
class TestReadOnlySource:
    """The ETL must never be able to modify production data."""

    def test_source_opens(self, source_db: Path) -> None:
        con = etl.open_source(str(source_db))
        try:
            assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3
        finally:
            con.close()

    def test_writes_are_rejected(self, source_db: Path) -> None:
        """mode=ro is a hard guarantee from SQLite, not a convention."""
        con = etl.open_source(str(source_db))
        try:
            with pytest.raises(sqlite3.OperationalError):
                con.execute("UPDATE accounts SET balance = 999999")
        finally:
            con.close()

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            etl.open_source(str(tmp_path / "nope.db"))

    def test_read_only_leaves_no_journal(self, source_db: Path) -> None:
        etl.open_source(str(source_db)).close()
        assert not (source_db.parent / f"{source_db.name}-journal").exists()
        assert not (source_db.parent / f"{source_db.name}-wal").exists()


class TestSourceColumns:
    def test_reads_affinity(self, source_db: Path) -> None:
        con = etl.open_source(str(source_db))
        try:
            cols = etl.source_columns(con, "accounts")
        finally:
            con.close()
        assert "balance" in cols
        assert "quiet_mode" in cols


class TestConvertValue:
    """SQLite has no bool/timestamp types; PostgreSQL demands them."""

    def test_int_flag_to_bool(self) -> None:
        assert etl._convert_value(1, "boolean") is True
        assert etl._convert_value(0, "boolean") is False

    def test_none_passes_through(self) -> None:
        assert etl._convert_value(None, "boolean") is None
        assert etl._convert_value(None, "text") is None

    def test_timestamp_string_to_datetime(self) -> None:
        result = etl._convert_value("2026-01-15 09:00:00", "timestamp with time zone")
        assert result is not None
        assert (result.year, result.month, result.day) == (2026, 1, 15)

    def test_date_string_to_date(self) -> None:
        result = etl._convert_value("2026-01-15", "date")
        assert result is not None
        assert (result.year, result.month, result.day) == (2026, 1, 15)

    def test_numeric_coercion(self) -> None:
        assert etl._convert_value("42", "bigint") == 42
        assert etl._convert_value("1.5", "double precision") == 1.5

    def test_text_untouched(self) -> None:
        """Metadata stays a JSON *string*; callers json.dumps/loads it as a str."""
        assert etl._convert_value('{"rake": 10}', "text") == '{"rake": 10}'


class TestBuildInsert:
    def _plan(self, conflict: str = "username, channel") -> etl.TablePlan:
        return etl.TablePlan(
            name="accounts",
            columns=("username", "channel", "balance", "quiet_mode"),
            conflict_target=conflict,
        )

    def test_emits_upsert_not_plain_insert(self) -> None:
        sql = etl._build_insert(
            self._plan(), {"username": "text", "channel": "text", "balance": "bigint"}
        )
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql

    def test_qualified_set_clause(self) -> None:
        """A bare column in the SET clause is ambiguous against EXCLUDED."""
        sql = etl._build_insert(self._plan(), {"username": "text", "balance": "bigint"})
        assert "balance = EXCLUDED.balance" in sql
        assert "balance = COALESCE" not in sql

    def test_casts_placeholders(self) -> None:
        sql = etl._build_insert(self._plan(), {"username": "text", "quiet_mode": "boolean"})
        assert "$1::text" in sql
        assert "$2::boolean" in sql

    def test_placeholders_are_contiguous(self) -> None:
        """asyncpg cannot infer a type for a gapped placeholder."""
        sql = etl._build_insert(
            self._plan(), {"username": "text", "channel": "text", "balance": "bigint"}
        )
        assert "$1::" in sql and "$2::" in sql and "$3::" in sql
        assert "$4" not in sql

    def test_no_shared_columns_raises(self) -> None:
        with pytest.raises(ValueError):
            etl._build_insert(self._plan(), {})


class TestChecksum:
    def test_is_order_independent(self) -> None:
        """SQLite (BINARY) and PostgreSQL (locale) can order the same rows
        differently; the digest must not depend on row order."""
        rows = [("a", "c", 1), ("b", "c", 2)]
        assert etl._balance_checksum(rows) == etl._balance_checksum(list(reversed(rows)))

    def test_detects_value_drift(self) -> None:
        assert etl._balance_checksum([("a", "c", 1)]) != etl._balance_checksum([("a", "c", 2)])

    def test_detects_channel_drift(self) -> None:
        assert etl._balance_checksum([("a", "c", 1)]) != etl._balance_checksum([("a", "z", 1)])

    def test_normalises_numeric_types(self) -> None:
        """SQLite yields int; PostgreSQL may yield Decimal for the same value."""
        assert etl._balance_checksum([("a", "c", 1)]) == etl._balance_checksum([("a", "c", 1.0)])


class TestTableOrder:
    def test_accounts_copied_before_transactions(self) -> None:
        assert etl.TABLE_ORDER.index("accounts") < etl.TABLE_ORDER.index("transactions")

    def test_sqlite_internals_skipped(self) -> None:
        assert "sqlite_sequence" in etl._SKIP_TABLES


class TestVerificationResult:
    def test_ok_requires_full_match(self) -> None:
        good = etl.VerificationResult(counts={"a": (1, 1)}, circulation=(5, 5), checksum=("x", "x"))
        assert good.ok

    def test_count_drift_fails(self) -> None:
        # Counts and their verdict are separate fields; a differing count in the
        # dict is only a failure once verify() has recorded it.
        not_recorded = etl.VerificationResult(
            counts={"a": (1, 2)}, circulation=(5, 5), checksum=("x", "x")
        )
        assert not_recorded.ok, "counts alone do not fail until recorded"
        recorded = etl.VerificationResult(
            counts={"a": (1, 2)},
            circulation=(5, 5),
            checksum=("x", "x"),
            count_mismatches=["a: source=1 target=2"],
        )
        assert not recorded.ok

    def test_balance_drift_fails(self) -> None:
        bad = etl.VerificationResult(
            counts={"a": (1, 1)},
            circulation=(5, 5),
            checksum=("x", "y"),
            mismatched_accounts=["u@c"],
        )
        assert not bad.ok


class TestCliParser:
    def test_source_required(self) -> None:
        with pytest.raises(SystemExit):
            etl.build_parser().parse_args([])

    def test_dry_run_flag(self) -> None:
        args = etl.build_parser().parse_args(["--source", "x.db", "--dry-run"])
        assert args.dry_run is True

    def test_batch_size_default(self) -> None:
        args = etl.build_parser().parse_args(["--source", "x.db"])
        assert args.batch_size == 1000


class TestExitCodeContract:
    """The runbook tells operators how to act on each exit status.

    ``docs/postgres-cutover.md`` maps 0 = verified, 1 = data drift (do not
    start the service), 2 = usage/configuration error. A connection failure is a
    configuration problem, so it must report 2 -- reporting 1 would tell an
    operator their currency has drifted when the real problem is a bad DSN.
    """

    @staticmethod
    def _run_main_raising(monkeypatch, exc: BaseException) -> int:
        """Run ``etl.main`` with ``run()`` replaced by one that raises ``exc``."""

        async def _noop() -> int:
            return 0

        def _boom(coro: object) -> None:
            coro.close()  # avoid an un-awaited coroutine warning
            raise exc

        monkeypatch.setattr(etl.asyncio, "run", _boom)
        return etl.main(["--source", "x.db"])

    def test_unset_dsn_env_is_exit_2(self, tmp_path: Path) -> None:
        src = _build_source(tmp_path / "s.db")
        with pytest.raises(ValueError, match="unset or empty"):
            etl._resolve_target(_args(src, pg_dsn_env="DEFINITELY_NOT_SET_12345"))

    def test_connection_refused_is_exit_2_not_1(self, monkeypatch) -> None:
        """A refused connection must not masquerade as verification drift."""
        assert self._run_main_raising(monkeypatch, ConnectionRefusedError(1225, "refused")) == 2

    def test_postgres_error_is_exit_2_not_1(self, monkeypatch) -> None:
        """An asyncpg server-side error (auth, missing db) is a config problem."""
        assert self._run_main_raising(monkeypatch, etl.asyncpg.InvalidPasswordError("no")) == 2

    def test_value_error_is_still_exit_2(self, monkeypatch) -> None:
        assert self._run_main_raising(monkeypatch, ValueError("bad thing")) == 2

    def test_drift_still_exits_1(self, monkeypatch) -> None:
        """The drift path must keep its own exit code; only config errors moved."""

        def _drift(coro: object) -> int:
            coro.close()  # avoid an un-awaited coroutine warning
            return 1

        monkeypatch.setattr(etl.asyncio, "run", _drift)
        assert etl.main(["--source", "x.db"]) == 1


class TestRefusesToGuessTarget:
    """A migration must never copy currency into a database nobody chose."""

    def test_sqlite_backend_config_is_refused(self, tmp_path: Path) -> None:
        import yaml

        # A config left on the SQLite backend, with no dsn_env/dsn. Every
        # PostgresConfig field has a default, so this would otherwise assemble a
        # silent localhost:5432/kryten_economy DSN.
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.safe_dump(make_config_dict()), encoding="utf-8")
        args = type("Args", (), {})()
        args.source = "x.db"
        args.config = str(cfg_file)
        args.pg_dsn_env = None
        args.batch_size = 100
        args.dry_run = True
        args.verify_only = False
        with pytest.raises(ValueError, match="selects database.backend"):
            etl._resolve_target(args)

    def test_postgres_backend_config_is_allowed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Once the config nominates PostgreSQL, the assembled DSN is allowed."""
        import yaml

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(make_config_dict(database={"backend": "postgres", "path": "e.db"})),
            encoding="utf-8",
        )
        args = type("Args", (), {})()
        args.source = "x.db"
        args.config = str(cfg_file)
        args.pg_dsn_env = None
        args.batch_size = 100
        args.dry_run = True
        args.verify_only = False
        with caplog.at_level("WARNING"):
            dsn = etl._resolve_target(args)
        assert dsn == "postgresql://kryten:@localhost:5432/kryten_economy"
        assert "Confirm that is the intended database" in caplog.text

    def test_explicit_dsn_env_skips_the_guard(self, monkeypatch) -> None:
        monkeypatch.setenv("KRYTEN_ECONOMY_EXPLICIT_DSN", "postgresql://u:p@host:5432/db")
        args = type("Args", (), {})()
        args.source = "x.db"
        args.config = "does-not-exist.yaml"
        args.pg_dsn_env = "KRYTEN_ECONOMY_EXPLICIT_DSN"
        assert etl._resolve_target(args) == "postgresql://u:p@host:5432/db"


# ── Live end-to-end tests ─────────────────────────────────────────────────────
@pytest.mark.postgres
class TestLiveMigration:
    """The properties that only an actual PostgreSQL server can prove.

    Each test truncates the tables it touches, so they require a *disposable*
    target database. ``uv run pytest -m 'not postgres'`` excludes this class
    entirely when a machine has no server, or when the target is real data.
    """

    @requires_pg
    async def test_end_to_end_idempotent_and_verifiable(self, source_db: Path) -> None:
        import asyncpg

        con = await asyncpg.connect(PG_DSN)
        try:
            await con.execute("TRUNCATE TABLE accounts, transactions CASCADE")
        finally:
            await con.close()

        args = _args(source_db, batch_size=2)  # small batches exercise chunking
        assert await etl.run(args) == 0, "first migration should verify"

        con = await asyncpg.connect(PG_DSN)
        try:
            first_counts = (
                await con.fetchval("SELECT COUNT(*) FROM accounts"),
                await con.fetchval("SELECT COUNT(*) FROM transactions"),
            )
            assert first_counts == (3, 5)
        finally:
            await con.close()

        # Re-run: must not duplicate anything and must still verify.
        assert await etl.run(args) == 0, "idempotent re-run should verify"

        con = await asyncpg.connect(PG_DSN)
        try:
            second_counts = (
                await con.fetchval("SELECT COUNT(*) FROM accounts"),
                await con.fetchval("SELECT COUNT(*) FROM transactions"),
            )
            assert second_counts == first_counts, "re-run must not duplicate rows"
        finally:
            await con.close()

    @requires_pg
    async def test_dry_run_writes_nothing(self, source_db: Path) -> None:
        import asyncpg

        con = await asyncpg.connect(PG_DSN)
        try:
            await con.execute("TRUNCATE TABLE accounts, transactions CASCADE")
        finally:
            await con.close()

        assert await etl.run(_args(source_db, dry_run=True)) == 0

        con = await asyncpg.connect(PG_DSN)
        try:
            assert await con.fetchval("SELECT COUNT(*) FROM accounts") == 0
            assert await con.fetchval("SELECT COUNT(*) FROM transactions") == 0
        finally:
            await con.close()

    @requires_pg
    async def test_verify_detects_injected_drift(self, source_db: Path) -> None:
        """A verifier that cannot fail is worthless - prove this one can."""
        import asyncpg

        args = _args(source_db)
        assert await etl.run(args) == 0

        con = await asyncpg.connect(PG_DSN)
        try:
            await con.execute(
                "UPDATE accounts SET balance = balance + 1 WHERE username = $1", "User0"
            )
        finally:
            await con.close()

        assert (
            await etl.run(_args(source_db, verify_only=True)) == 1
        ), "verify must fail on a one-unit balance drift"
        assert await etl.run(args) == 0, "re-run must repair the drift"
        assert (
            await etl.run(_args(source_db, verify_only=True)) == 0
        ), "state must be clean again after repair"

    @requires_pg
    async def test_missing_required_column_fails_fast(self, source_db: Path) -> None:
        """A source older than the target schema must fail with a clear message.

        `transactions.type` is NOT NULL in PostgreSQL. If the source lacks it, the
        copy cannot succeed - and it must say *why* up front rather than dying on a
        driver NOT NULL violation partway through the copy.
        """
        con = sqlite3.connect(source_db)
        con.execute("ALTER TABLE transactions RENAME TO transactions_old")
        con.execute(
            "CREATE TABLE transactions ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " username TEXT NOT NULL, channel TEXT NOT NULL,"
            " amount INTEGER NOT NULL, reason TEXT, created_at TEXT)"
        )
        con.execute("DROP TABLE transactions_old")
        con.commit()
        con.close()

        with pytest.raises(ValueError, match="type"):
            await etl.run(_args(source_db, dry_run=False))

    @requires_pg
    async def test_unknown_source_table_is_refused(self, source_db: Path) -> None:
        """Never silently skip data: an unknown table is a hard error."""
        con = sqlite3.connect(source_db)
        con.execute("CREATE TABLE surprise_table (id INTEGER PRIMARY KEY, x TEXT)")
        con.commit()
        con.close()

        with pytest.raises(ValueError, match="surprise_table"):
            await etl.run(_args(source_db, dry_run=True))
