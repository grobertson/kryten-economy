# HANDOFF: PostgreSQL Migration Repository Port Status

**Date:** 2026-09-26
**Scope:** `kryten-economy` Sprint 12, SQLite → PostgreSQL migration
**Status:** **Sorties 1–5 complete — code and runbook ready; production still on SQLite**
**Implementation status:** Config/pool, encapsulated data boundary, Alembic schema, a full
asyncpg store with parity/concurrency tests, a verified SQLite→PostgreSQL ETL, CI wiring, and
a rehearsed cutover runbook are complete. The `0.16.0` release is prepared but not tagged.

> This note is a repository handoff, not a cutover approval. The service must remain configured
> for SQLite until the cutover is executed deliberately, per `docs/postgres-cutover.md`.

---

## 1. Executive summary

The Sprint 12 PostgreSQL migration is **not complete**, but Sorties 1–4 are.

Sortie 1 (config + pool) and Sortie 2 (encapsulated data boundary) are done. Sortie 3 adds an
**Alembic-managed schema** and a **complete `EconomyDatabasePg`** implementing all 132 public
`EconomyDatabase` methods on asyncpg, with `database.backend` dispatching between the two. The
store is verified against a live PostgreSQL 16.15 server, including the concurrency and
currency-integrity tests the spec requires.

Sortie 4 adds the **ETL** (`kryten_economy/migrate_sqlite_to_pg.py`): a read-only, batched,
idempotent, self-verifying copier. It was proven against the real 10.7 MB dev database
(148 accounts, 59,536 transactions, 1,612,831 total circulation) into a disposable PostgreSQL
database, and is covered by 33 tests.

Sortie 5 adds the **test wiring** (a `postgres` pytest marker plus shared `pg_dsn`/`pg_pool`
fixtures, a `postgres:16-alpine` CI service, and a second CI job proving the suite stays green
with no database), the **cutover runbook** (`docs/postgres-cutover.md`), and the **`0.16.0`**
release. The runbook was rehearsed end to end against a scratch database using a copy of the
real 10.7 MB dev database: `alembic upgrade head`, migrate, verify, and rollback path all
behaved as documented, with 148 accounts, 59,536 transactions, and circulation 1,612,831
matching exactly. The scratch database and its `pg_hba.conf` rule were removed afterwards.

Sortie 5 also fixed two operational hazards in the ETL that the rehearsal exposed: a connection
or authentication failure now exits 2 (configuration) rather than 1 (data drift), and the tool
refuses to assemble a default `localhost` DSN when the config does not nominate a PostgreSQL
target. Both mattered because the runbook tells operators that exit 1 means "do not start the
service".

What remains is execution, not implementation: execute the cutover in a maintenance window,
add `kryten_economy` to the `pg_dump` backup job (**not yet done — PostgreSQL currently has no
scheduled backups**), and tag `v0.16.0`. A full application boot against a migrated database
remains untested; that is the single largest unverified risk and is the reason the cutover
should be rehearsed against a real service once more before it touches production.

The migration is therefore **not yet cut over** and should not be enabled with
`database.backend: postgres` in production until the runbook is walked through deliberately.

---

## 2. Evidence and verification limits

### Evidence reviewed

- `docs/12-postgres-migration/PRD-postgres-migration.md`
- `docs/12-postgres-migration/SPEC-Sortie-1-db-config-and-pool.md`
- `docs/12-postgres-migration/SPEC-Sortie-2-encapsulate-data-layer.md`
- `docs/12-postgres-migration/SPEC-Sortie-3-schema-and-asyncpg-layer.md`
- `docs/12-postgres-migration/SPEC-Sortie-4-etl-migration.md`
- `docs/12-postgres-migration/SPEC-Sortie-5-tests-cutover-release.md`
- `docs/POSTGRES_MIGRATION_PLAN.md`
- Current `kryten-economy` configuration, database, orchestrator, tests, and worktree
- `kryten-webqueue` PostgreSQL implementation and handoff documentation as a reference

### Verification performed

- Read the migration specifications and current source files.
- Searched for `_get_connection`, `sqlite3`, asyncpg, pool creation, migration, schema, and
  datetime usage.
- **Measured asyncpg behaviour against the live server** rather than assuming it: a
  `boolean` column rejects an `int` bind, a `timestamptz` column rejects a `str` bind, and
  `SUM()` over `bigint` returns `Decimal`. Each is handled in `db/boundary.py`.
- Verified Sorties 1–3 end to end against a live PostgreSQL 16.15 server: `alembic upgrade
  head` applied 22 tables / 42 indexes; 46 parity tests pass; the full suite is 936 passing.
- **Verified Sortie 4 end to end against real data.** The ETL was run against the actual
  `economy-dev.db` into a disposable PostgreSQL database (`kryten_economy_etl_test`):
  all 18 source tables copied, 59,536 transactions, total circulation matched exactly at
  1,612,831, and the per-account balance diff was empty. The source file was byte-identical
  (SHA-256) before and after every run. A second full run left row counts unchanged
  (idempotent). Injecting a **single-unit** balance error into the target made `--verify`
  exit 1 and name the account; a subsequent re-run repaired it and returned to verified.
  A deliberately truncated source migrated first, then a full re-run converged to a
  verified state (resumable).
- Formatting, lint (`ruff`), typing (`mypy` on `kryten_economy/db` and the ETL), `git diff
  --check`, and a `gitleaks` secret scan are all clean for the touched files. The typing result
  is real rather than vacuous: `asyncpg-stubs` was added as a dev dependency so mypy analyses
  the PostgreSQL layer instead of skipping it, and doing so surfaced an inaccurate annotation
  (`_ensure_account`/`_log_tx` were declared as taking `asyncpg.Connection` while every caller
  passes a `PoolConnectionProxy`), which is fixed. There are no `type: ignore` suppressions
  added for the database layer.
- The full suite is **977 passing** with a live PostgreSQL server, and **926 passing / 51
  skipped** with none. The 51 PostgreSQL tests skip with an actionable reason rather than
  erroring, so a machine without a database still gets a green suite.
- **No full application boot against a PostgreSQL-backed database has been performed.** That
  remains the single largest unverified risk.

### Explicit uncertainty

The §6 `datetime`/JSON findings below are now **largely mitigated at the store edge** by
`db/boundary.py`, which was validated with round-trip tests. They remain unverified at the
**HTTP/NATS response** layer.

The ETL resolves the `metadata` question for itself: `metadata` stays a `TEXT` column holding
a JSON **string** rather than becoming `jsonb`, because the store's string contract must be
preserved (`gambling_engine.py` and `presence_tracker.py` call `json.dumps(...)` and expect a
string back; `jsonb` would make asyncpg return a `dict` and break those callers). The ETL
copies `metadata` verbatim, so this is consistent with Sortie 3 and no spec deviation is
introduced at the data level.

---

## 3. Worktree state observed

Sorties 1–4 remain in an uncommitted worktree alongside pre-existing untracked files.
Preserve and review the complete change set before committing.

| Path | State observed | Notes |
| --- | --- | --- |
| `alembic.ini`, `alembic/` | Untracked | Alembic environment + revision `0001` (the schema authority). |
| `kryten_economy/db/` | Untracked directory | `pool.py`, `boundary.py`, `protocol.py`, `database_pg.py`. |
| `tests/test_store_parity.py` | Untracked | Sortie 3 live-DB parity/concurrency tests. |
| `tests/test_postgres_config.py` | Untracked | Sortie 1 configuration/pool tests. |
| `tests/test_data_layer_encapsulation.py` | Untracked | Sortie 2 parity and boundary tests; the SQLite confinement guard now also exempts the ETL. |
| `kryten_economy/migrate_sqlite_to_pg.py` | Untracked | Sortie 4 ETL (operator entry point; never imported by the service). |
| `tests/test_migrate_sqlite_to_pg.py` | Untracked | Sortie 4 ETL unit + live tests (33). |
| `kryten_economy/config.py` | Modified | Adds `PostgresConfig` and extends `DatabaseConfig`. |
| `kryten_economy/main.py` | Modified | Backend dispatch, pool lifecycle, ignored-user seam. |
| `kryten_economy/database.py` | Modified | Account-search/channel-transaction methods, vanity casing fix. |
| `kryten_economy/command_handler.py` | Modified | Uses only the public data/component APIs. |
| `kryten_economy/multiplier_engine.py` | Modified | Adds copy-returning event accessors. |
| `kryten_economy/rank_engine.py` | Modified | Adds `get_tier_count()`. |
| `pyproject.toml` | Modified | Adds `asyncpg>=0.29`; version remains `0.15.4`. |
| `README.md`, `docs/admin-guide.md`, `config.example.yaml`, `_write_config.py`, `CHANGELOG.md` | Modified | Document Alembic, the backend selector, and the production-is-SQLite constraint. |
| `.github/prompts/`, `kryten_economy/py.typed` | Untracked | Pre-existing unrelated files; not modified as migration work. |

---

## 4. Sortie status and gap matrix

| Sortie | Status | What exists | What remains |
| --- | --- | --- | --- |
| **1 — Config + pool** | **Complete** | `PostgresConfig`, `DatabaseConfig.backend`, pool-size validation, DSN resolution, application-owned pool lifecycle, `asyncpg` dependency, config/operator docs, and unit coverage. | None for Sortie 1. |
| **2 — Encapsulation** | **Complete** | Account search, transaction pagination/recent listing, and private event/rank/app state are accessed through typed public methods. Raw SQLite is confined to `database.py`; no production `SLF001` suppressions remain. | None for Sortie 2. |
| **3 — Schema + asyncpg store** | **Complete** | Alembic revision `0001` (22 tables, 42 indexes) applied to a live server; `EconomyStore` protocol; `EconomyDatabasePg` covering all 132 methods; backend dispatch; live-DB parity, value-shape, concurrency, and atomicity tests. | None for Sortie 3 code. The `datetime`/JSON response boundary from §6 is still unaddressed and is Sortie 3 follow-up work. |
| **4 — ETL** | **Complete** | `kryten_economy/migrate_sqlite_to_pg.py`: read-only (`mode=ro`) batched copy in FK order, natural-key `ON CONFLICT DO UPDATE` idempotency, identity-sequence resets, schema-introspecting column/type handling, `--dry-run`, `--verify-only`, count + circulation + checksum verification, committed batches for resume. Verified against the real dev DB; 41 tests. | None. Rehearsed on a production-sized copy in Sortie 5. |
| **5 — Tests/cutover/release** | **Complete (code)** | `postgres` pytest marker + shared `pg_dsn`/`pg_pool` fixtures; `KRYTEN_ECONOMY_TEST_DSN` (legacy `KRYTEN_ECONOMY_PG_DSN` still honoured); `.github/workflows/ci.yml` with a lint job, a `postgres:16-alpine` service job, and a no-PostgreSQL job; `docs/postgres-cutover.md`; `0.16.0` + changelog. Rehearsed end to end against a scratch DB. | **Execution only:** tag `v0.16.0`, walk the runbook in a maintenance window, add `kryten_economy` to the `pg_dump` job, and boot the real service against a migrated database. |

### Acceptance-criteria snapshot

| Requirement | Current state |
| --- | --- |
| `database.backend` selectable | **Met.** `main.py::_initialize_database_resources` dispatches to `EconomyDatabase` or `EconomyDatabasePg`. |
| Default backend remains SQLite | Met: config default and runtime default. |
| DSN precedence and empty-env error | Unit-tested, including environment failure behavior. |
| Pool lifecycle integration | `EconomyApp` opens the pool at startup and closes it during normal and partial-startup shutdown. |
| Zero raw SQLite access outside `database.py` | **Met**, enforced by a tokenizing regression test. |
| Alembic is the schema authority | **Met.** `alembic_version` tracks revisions; no hand-rolled `sql/` or `schema_version` table. |
| Every store method has parity | **Met.** 132/132 `EconomyDatabase` methods implemented on `EconomyDatabasePg`; a canary test fails if this regresses. |
| Concurrent-debit safety | **Verified live.** 10 concurrent debits of 30 against 100 → exactly 3 succeed, balance 10, never negative, 3 ledger rows. |
| Two-account operations atomic | **Verified live** for batch presence credit (all-or-nothing transaction) and refund/debit. |
| Versioned PostgreSQL schema | **Met** via Alembic revision `0001`, applied to the live server. |
| PostgreSQL `EconomyStore` parity | **Met** (`tests/test_store_parity.py`, 46 live tests). |
| ETL and balance verification | **Met.** Verified live against the real dev database: circulation matched exactly (1,612,831) and the per-account diff was empty; a one-unit injected error was caught (exit 1) and repaired by a re-run. |
| PostgreSQL test fixture/CI | **Met.** `postgres` marker registered in `pyproject.toml`; shared `pg_dsn`/`pg_pool` fixtures in `tests/conftest.py`; CI runs the suite both with a real `postgres:16-alpine` service and with no database at all. Verified locally: **977 passing** with PostgreSQL, **926 passing / 51 skipped** without. |
| Cutover/rollback runbook | **Met.** `docs/postgres-cutover.md`, rehearsed end to end against a scratch database created from a copy of the real dev DB. |
| Version `0.16.0` and migration changelog | **Met.** `pyproject.toml` is `0.16.0`; the changelog flags the new backend, the config-schema addition, transactional credit/debit, and the one-time ETL. Not yet tagged/published. |

---

## 5. Current code facts

### Orchestrator and lifecycle

- `kryten_economy/main.py::_initialize_database_resources` dispatches on
  `config.database.backend`: `sqlite` -> `EconomyDatabase`, `postgres` -> `EconomyDatabasePg`
  over the pool from `create_pool`.
- `EconomyApp.stop()` closes the optional pool during normal and partial-startup shutdown.
- Counter persistence and restore are still described and implemented as SQLite operations.
- The PostgreSQL schema is **not** created at startup; it is applied out-of-band by Alembic.
  `EconomyDatabasePg.initialize()` only verifies connectivity so a misconfigured database
  fails fast.

### Encapsulated command queries

`kryten_economy/command_handler.py` no longer accesses private SQLite connections. Account
search, user transaction pagination, and channel-wide recent transactions are delegated to
typed public `EconomyDatabase` methods. Public accessors also replace the remaining private
multiplier-event, rank-tier, and ignored-user state access; no production `SLF001` suppressions
remain.

### PostgreSQL store (`kryten_economy/db/`)

| Module | Role |
| --- | --- |
| `pool.py` | DSN resolution + `asyncpg` pool factory (Sortie 1). |
| `boundary.py` | Value-boundary conversions and the column-family allowlists. |
| `protocol.py` | `EconomyStore` protocol shared by both backends. |
| `database_pg.py` | The asyncpg store (132/132 methods). |

The value boundary is the load-bearing design decision: PostgreSQL storage is native
(`timestamptz`, `boolean`, `date`, `NUMERIC` aggregates) but rows cross the store edge with
SQLite-compatible Python types, because `pm_handler` calls `datetime.fromisoformat()` on
account rows and `Decimal` from `SUM()` is not JSON-serialisable.

### Current database layer

- `kryten_economy/database.py` is still the SQLite implementation and remains the default.
- `kryten_economy/db/database_pg.py` is the full asyncpg implementation of the same surface.
  Both satisfy `EconomyStore`; mypy checks the PostgreSQL store clean.
- The schema is defined by `alembic/versions/0001_initial_schema.py` (the Alembic authority),
  not by `database.py::_create_tables` and not by a hand-rolled `sql/` directory.
- `EconomyDatabasePg.initialize()` performs no DDL by design.

### Verification infrastructure

- `tests/test_store_parity.py` runs against a real PostgreSQL server and skips cleanly when
  neither `KRYTEN_ECONOMY_TEST_DSN` nor `KRYTEN_ECONOMY_PG_DSN` is set. 46 tests cover
  value-shape parity, currency integrity, concurrency, idempotency, pruner safety, and full
  method-surface parity.
- `test_pg_backend_covers_sqlite_surface` is a deliberate canary: it fails if any public
  `EconomyDatabase` method is missing from `EconomyDatabasePg`. Do not weaken it.
- A `postgres` pytest marker is registered in `pyproject.toml`, and `tests/conftest.py`
  exposes `pg_dsn`/`pg_pool` fixtures plus a module-level `PG_AVAILABLE` flag for tests that
  must decide at collection time. The ETL tests read `PG_DSN_ENV_VAR` so they work with
  either DSN variable name.
- `.github/workflows/ci.yml` has three jobs: `lint` (ruff/black/mypy), `test` (with a
  `postgres:16-alpine` service and `alembic upgrade head`), and `test-no-postgres` (which
  asserts no DSN is present, runs `-m "not postgres"`, then asserts the postgres-marked tests
  still exist so the marker cannot be "fixed" by deleting them).

### Configuration and documentation

- `config.example.yaml` and `_write_config.py` document the backend selector and the
  environment-based PostgreSQL connection block, and state that production stays on SQLite
  until the cutover is performed deliberately.
- `README.md` and `docs/admin-guide.md` document Alembic as the schema authority, the
  `alembic upgrade head` step, DSN precedence, restart requirements, the ETL's exit-code
  contract, and that the ETL must be given `--pg-dsn-env` explicitly.
- `docs/postgres-cutover.md` is the cutover runbook: create role/database, apply the schema,
  stop, back up, migrate, verify independently, flip the backend, smoke test, roll back, plus
  the `pg_hba.conf` per-database gotcha, the `pg_dump`-before-rollback warning, and a
  rehearsal procedure.
- `CHANGELOG.md` has a `0.16.0` entry flagging the new backend, the config-schema addition,
  transactional credit/debit, and the one-time ETL.
- `pyproject.toml` declares version `0.16.0`. **Not yet tagged or published** — the release
  step is deliberately left for a human.

---

## 6. High-risk `datetime` and JSON boundary

This is the most important correctness issue to resolve before PostgreSQL is enabled.

### What changes at the database boundary

| Value | SQLite path | PostgreSQL/`asyncpg` path | Consequence |
| --- | --- | --- | --- |
| `TIMESTAMP` result | Usually a string | Native `datetime` for `timestamptz`/`timestamp` | Code that assumes a string can fail. |
| `timestamptz` input | SQLite often accepts ISO text | `asyncpg` requires a real `datetime` | ETL and application writes need native values. |
| Boolean flags | Integer-like values | Native `bool` | Queries must bind actual booleans. |
| JSON metadata | `TEXT` containing JSON | `jsonb` or native JSON object | Source conversion and response handling must be explicit. |
| JSON/NATS response | Strings are safe | Native `datetime` is not accepted by standard `json.dumps` | A response containing a PostgreSQL row can fail at the transport boundary. |

### Status after Sortie 3

Items 1 (and the store half of 2) are now **implemented in `kryten_economy/db/boundary.py`
and verified by live tests**:

- `normalize_row`/`normalize_rows` convert `timestamptz` -> ISO-8601 `str`, `boolean` -> `int`
  0/1, `date` -> `YYYY-MM-DD`, and integral `Decimal`/`float` aggregates -> `int`, so both
  `pm_handler._cmd_tip`'s `datetime.fromisoformat(first_seen)` and the NATS command JSON
  encoders keep working unchanged.
- `to_datetime`/`to_date`/`to_bool` accept `str`, `datetime`, `date`, and `int` on the way in,
  which asyncpg requires (a `boolean` column rejects an `int` bind; a `timestamptz` column
  rejects a `str` bind). Naive values are treated as UTC.
- Column families are declared as explicit allowlists in `boundary.py`, so a new
  boolean/timestamp column cannot silently leak a native type.

What is **still open**:

- `kryten_economy/utils.py::parse_timestamp` is still typed `str | None` and still returns
  `None` on a bad value. It is safe today *only* because the store hands it strings. Widening
  it to accept `datetime` would remove that coupling.
- No test yet exercises a **real command/event path against a migrated database**; the parity
  tests are store-level. A full application boot against PostgreSQL is still required.
- The ETL copies `metadata` as an opaque JSON **string** (see §2). That is consistent with the
  store contract but means the column is not queryable as JSON in PostgreSQL; if JSON
  querying is wanted later it needs a deliberate, separately-specified change.

### Reference from the successful `kryten-webqueue` work

The corresponding webqueue implementation provides two useful patterns:

- `kryten_webqueue/catalog/db/_pg_base_domain.py::parse_dt` accepts either an ISO string or a
  native `datetime` before binding to `asyncpg`. (The economy port does the mirror image:
  `boundary.to_datetime` accepts either before binding.)
- The WebSocket initial-state response uses `fastapi.encoders.jsonable_encoder` before
  `json.dumps`, because PostgreSQL timestamps are native `datetime` objects. (The economy port
  avoids needing this at the store edge by normalizing on the way out.)
- Its pool uses `server_settings={"search_path": ...}` rather than relying only on an `init`
  callback, because pool reset can clear session-level `SET` state.
- The webqueue migration also demonstrated that an application layer assuming database values
  are strings can crash even when repository-level PostgreSQL tests pass. That is exactly the
  failure the economy boundary layer exists to prevent.

---

## 7. Decisions that were resolved during the migration

1. **DDL strategy — Alembic wins.** The Sortie 3 spec contradicted itself (versioned `sql/`
   files *and* Alembic). Alembic is the single schema authority: `alembic/versions/0001`,
   no hand-rolled `sql/` directory, no custom `schema_version` table.
2. **Store abstraction — separate class behind a Protocol.** `EconomyStore` (runtime-checkable
   `Protocol`) documents the contract; `EconomyDatabase` (SQLite) and `EconomyDatabasePg`
   (asyncpg) both satisfy it, and `database.backend` dispatches between them.
3. **Configuration naming — `database.path` preserved.** The existing key was kept so every
   deployed `config.yaml` loads unchanged.
4. **Target database name — `kryten_economy`.** Provisioned on `chandra-1.local`; the
   disposable `kryten_economy_test` and the (now dropped) `kryten_economy_rehearsal` exist
   alongside it.
5. **JSON metadata — stays `TEXT`, not `jsonb`.** Deliberate deviation from Sortie 4 §4. The
   callers (`gambling_engine.py`, `presence_tracker.py`) `json.dumps()` into this column and
   expect a string back; `jsonb` would hand them a `dict` and change every NATS response
   payload. The column is almost entirely empty in real data, so there is no querying or
   indexing benefit to justify the blast radius. Revisit only as its own Alembic revision.
6. **Rollback support — retained for one release.** `database.backend: "sqlite"` plus a
   restart, with the SQLite file left untouched. The runbook warns that rolling back discards
   PostgreSQL-side activity and to `pg_dump` first if that window was active.

---

## 8. Recommended next sequence (not executed)

All of this is **execution**, not implementation. In order:

1. Review and commit/preserve the completed Sortie 1–5 work.
2. **Perform a real application boot against a PostgreSQL-backed database populated by the
   ETL**, and smoke-test a command/event path with real data. This remains the largest
   unverified risk in the whole migration. The runbook's §6 smoke test is the script for it.
3. Add `kryten_economy` to the existing `pg_dump` backup job. **Until this is done,
   PostgreSQL has no scheduled backups while SQLite had one.** This is the single most
   important remaining action and is a small, self-contained piece of work.
4. Tag `v0.16.0` and publish.
5. Walk `docs/postgres-cutover.md` in a maintenance window. Announce the config change.
6. Only after a successful cutover and one clean release, consider dropping the SQLite backend.

---

## 9. Bottom line

The repository contains the **complete migration**: connection/configuration layer,
encapsulated data boundary, Alembic schema, a verified PostgreSQL store, a verified ETL, CI
wiring, and a rehearsed cutover runbook.

> **SQLite remains the production data layer. `0.16.0` is code-complete and its cutover
> procedure has been rehearsed end to end, but production has not been moved and the real
> service has not yet booted against a migrated database.**

The remaining work is deliberately operational: boot-test, back up, tag, cut over. Do not
enable `database.backend: postgres` in production by editing `config.yaml` — follow
`docs/postgres-cutover.md`, which keeps the SQLite file intact as a one-line rollback.
