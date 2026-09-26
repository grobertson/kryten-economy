# PRD: SQLite → PostgreSQL Migration

**Sprint**: 12 — `12-postgres-migration`
**Status**: Planned (N+1) — full PRD + 5 sortie specs; not yet implementing
**Builds on**: Sprints 1–11 (the full economy on the SQLite `EconomyDatabase`)
**Target version**: `0.16.0` (pre-1.0 breaking change → minor bump per SemVer)
**Workflow**: [../../AGENT-WORKFLOW-GUIDE.md](../../AGENT-WORKFLOW-GUIDE.md)

---

## 1. Executive Summary

kryten-economy persists every account balance, transaction, daily-activity row, and game
state in a local **SQLite** database (`EconomyDatabase`, `kryten_economy/database.py`). This
sprint migrates that persistence layer to **PostgreSQL** on the shared Kryten Postgres server
(the same box that already backs kryten-llm's `kryten_memory`), into a dedicated
`kryten_economy` database. The public NATS command contract does **not** change; this is an
internal storage swap plus a config-schema change and a one-time data migration.

The near-term payoff is **correctness under concurrency and maintainability**, not raw
single-op latency (see §3). SQLite serialises all writes through a single writer; the economy
mutates currency on every presence tick and every game, and its double-spend guarding is only
safe today because volume is low. Postgres gives real row-level locking, transactional
credit/debit, and the analytical query surface (window functions, `percentile_cont`,
materialised views) that the admin/reporting features want.

## 2. Problem Statement

- **What.** All persistence is SQLite-specific: a new `sqlite3.Connection` per call via
  `run_in_executor`, WAL mode, `AUTOINCREMENT`, `ALTER TABLE … ADD COLUMN` migrations wrapped
  in `try/except sqlite3.OperationalError`, and raw connections leaking out of the data layer
  (`command_handler.py` calls `self._app.db._get_connection()` directly). The SQLite dialect
  and the single-writer model are baked in throughout.
- **Who.** Maintainers (fighting SQLite-specific syntax and a leaky abstraction), operators
  (no concurrency headroom; reporting queries are hand-rolled), and — as the channel grows —
  every player, because atomic debit-or-fail only holds under low write contention.
- **Why now.** kryten-llm already runs Postgres in this ecosystem, so the operational surface
  exists. Standardising economy onto it removes a second storage dialect, unlocks the
  reporting/analytics backlog, and hardens the currency-integrity path before load grows.

## 3. Goals and Success Metrics

- **Functional parity.** Every `EconomyDatabase` method behaves identically from the caller's
  perspective; the `kryten.economy.command` contract is byte-for-byte unchanged.
- **Transactional integrity.** `credit`/`debit`/`set_balance` and every account+transaction
  mutation run inside a single Postgres transaction with row-level locking; no double-spend is
  possible under concurrent debits (proven by a concurrency test).
- **Clean data layer.** No caller reaches a raw connection; `command_handler.py` uses only
  public `EconomyDatabase` methods.
- **Verified data migration.** A one-shot ETL moves all rows from SQLite to Postgres,
  idempotent and resumable, with a post-migration verification that **total circulation and
  per-account balances match to the coin**.
- **Config.** A `database` block selects the backend and carries the Postgres DSN (secret via
  env-var indirection, per the llm pattern). `config.example.*` stays in sync.
- **Success metrics.** `uv run pytest` green against a Postgres fixture; ETL verification
  reports zero balance drift; a 100-concurrent-debit test shows no lost updates or negative
  balances.

**Explicit non-goal — raw speed.** A single `get_balance` may get marginally *slower* (socket
hop vs in-process file). We are buying concurrency correctness, analytics, and one storage
dialect — not latency. This expectation is stated up front.

## 4. User Stories

- *As a maintainer*, I want one SQL dialect across economy and llm, so I stop hand-porting
  SQLite idioms and can reuse the llm asyncpg/DSN pattern.
- *As a maintainer*, I want the data layer fully encapsulated, so no route or handler can
  corrupt state by grabbing a raw connection.
- *As an operator*, I want atomic debit-or-fail to hold under concurrent play, so the ledger
  can never go negative or lose a write during a busy presence tick.
- *As an operator*, I want the migration to be re-runnable and self-verifying, so a failed
  cutover is safe to retry and I can prove no coins were lost.
- *As an operator*, I want the old SQLite file left intact after cutover, so rollback is a
  config flip.

## 5. Technical Architecture

### 5.1 Topology
- Shared Postgres **server**, dedicated **database** `kryten_economy`, owned solely by this
  service. No other service reads or writes it (cross-service reads stay on NATS; a future
  read-model copy is out of scope — see kryten-webqueue's PRD for the CQRS discussion).
- Connection via an **`asyncpg` pool** (`pool_min_size`/`pool_max_size`), replacing the
  per-call `sqlite3.connect` + `run_in_executor` pattern.

### 5.2 DSN & secrets (reuse the llm pattern)
Mirror `PgVectorStore._resolve_dsn` (kryten-llm `components/memory/vector_store.py`):
precedence `dsn_env` (env var holding full DSN) → `dsn` (literal) → assembled
`host`/`port`/`user`/`dbname` with the password from `password_env` (preferred) or `password`.
Secrets never land in `config.yaml`/git; gitleaks stays green.

### 5.3 Data layer
- `EconomyDatabase` keeps its public async method surface; internals move from
  `run_in_executor(_sync)` to native `async` `asyncpg` calls against the pool.
- `_get_connection()` (raw `sqlite3`) is removed. Any operation the command handler performed
  inline gets a real, transactional `EconomyDatabase` method (Sortie 2).
- **Currency mutations** use `async with pool.acquire() as con, con.transaction():` and either
  an atomic `UPDATE accounts SET balance = balance - $n WHERE … AND balance >= $n RETURNING`
  (debit-or-fail in one statement) or `SELECT … FOR UPDATE` where multi-row logic requires it.
  The account update and the `transactions` insert commit together — never one without the
  other.

### 5.4 Schema
- DDL moves to versioned `sql/` files (`001_schema.sql`, …) — the source of truth is today's
  `database.py::_create_tables`. Translations: `INTEGER PRIMARY KEY AUTOINCREMENT` →
  `bigint GENERATED ALWAYS AS IDENTITY`; `BOOLEAN DEFAULT 0` → `boolean DEFAULT false`;
  `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` → `timestamptz DEFAULT now()`; `TEXT` metadata blobs
  that hold JSON → `jsonb`; `UNIQUE(username, channel)` preserved. Migrations become ordered,
  tracked SQL files (a `schema_version` table), retiring the `ALTER … except OperationalError`
  idiom.

### 5.5 Message flow (unchanged)
```
CyTube events ─(NATS)─▶ handlers ─▶ EconomyDatabase (asyncpg pool) ─▶ kryten_economy (PG)
kryten.economy.command ─▶ command_handler ─▶ EconomyDatabase (public methods only)
```

## 6. Dependencies

- New runtime dep: `asyncpg>=0.29` (already used by kryten-llm; pin consistently).
- A reachable Postgres server with a `kryten_economy` database + role. Local dev reuses the
  `kryten-pg` WSL distro that already serves `kryten_memory`.
- Sortie 2 (encapsulation) must land before Sortie 3 (engine swap).

## 7. Security and Privacy

- DSN/password via env-var indirection only; nothing secret in `config.yaml` or `sql/`.
- Least privilege: the economy role owns only `kryten_economy`; no superuser at runtime.
- Currency is the crown jewels — every mutation stays transactional and audit-logged in
  `transactions` (never credit without logging). Input validation on command args is unchanged.
- Migration script reads SQLite read-only and never deletes the source file.

## 8. Rollout Plan

1. Stand up `kryten_economy` DB + role on the shared server (manual, documented in the cutover
   sortie).
2. Deploy `0.16.0` configured for Postgres pointing at an **empty** DB; run the ETL; run
   verification (balance/circulation parity).
3. Flip the service to live on Postgres; keep the SQLite file untouched for rollback.
4. Rollback = config flip back to `sqlite` + restart (both backends remain selectable for one
   release).
5. **Backup wiring is a separate later sprint** (per operator decision) — noted in §9.

## 9. Future Enhancements

- **Backup**: add `kryten_economy` to the cron `pg_dump` job (separate sprint).
- **Analytics**: replace hand-rolled aggregates (median balance, circulation, leaderboards)
  with window functions / materialised views now that the engine supports them.
- **Read model**: if another service needs economy data, expose it over NATS or via an
  occasionally-refreshed copy table — never a live cross-DB JOIN.
- Drop the SQLite backend entirely once Postgres is proven in production for a release or two.

## 10. Open Questions

- Do we keep the `sqlite` backend selectable for one release (recommended, enables instant
  rollback) or hard-cut? Default assumption: keep it one release.
- Config format: economy is still YAML with a *planned* YAML→JSON move. Do we add the
  `database` Postgres block to YAML now and let the JSON migration carry it later, or ride the
  JSON switch in the same sprint? Default assumption: add to YAML now; JSON stays its own
  sprint to avoid coupling two schema changes.
- Exact `pool_max_size` for the presence-tick workload — start at 8 (llm's default), tune after
  a load observation.
```
