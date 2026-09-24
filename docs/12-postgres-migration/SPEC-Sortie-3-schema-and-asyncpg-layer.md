# SPEC — Sortie 3: Schema translation + asyncpg `EconomyDatabase`

**Sprint**: 12 — `12-postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 1 (pool/config), Sortie 2 (encapsulation)
**Estimated**: 6 h (the core sortie; may split into two commits)

---

## 1. Overview

Reimplement `EconomyDatabase` against the `asyncpg` pool and translate the schema from SQLite
DDL to PostgreSQL. Currency mutations become explicit, row-locked transactions. When
`database.backend == "postgres"`, the service runs entirely on Postgres; `sqlite` remains
selectable for rollback.

## 2. Scope and Non-Goals

**In scope**
- Postgres DDL in `sql/` (idempotent, versioned) covering every current table/index.
- `EconomyDatabase` methods rewritten to `async` asyncpg calls (no `run_in_executor`).
- Transactional `credit`/`debit`/`set_balance` with row-level locking (debit-or-fail).
- Backend dispatch: construct the SQLite or Postgres implementation from `DatabaseConfig`.

**Non-goals**
- No ETL of existing data (Sortie 4). No analytics rewrite (future). No contract changes.

## 3. Requirements

- **Parity.** Every public method returns the same shape as the SQLite version (dicts/values).
- **Integrity.** Account balance update + `transactions` insert commit atomically; a debit that
  would go negative fails without mutating either table.
- **Concurrency-safe.** Concurrent debits on one account never lose an update or go negative.
- **Idempotent schema.** DDL is safe to run repeatedly; a `schema_version` table tracks applied
  ordered migrations (replacing the `ALTER … except OperationalError` idiom).
- **mypy strict-clean**, asyncio-native, no blocking calls in the event loop.

## 4. Design

### 4.1 Schema files
`sql/001_schema.sql` … source of truth = current `database.py::_create_tables`. Type mapping:

| SQLite | PostgreSQL |
|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| `INTEGER` (bool flags `… DEFAULT 0`) | `boolean DEFAULT false` |
| `INTEGER` (counters/amounts) | `bigint` / `integer` as appropriate |
| `TEXT` | `text` |
| `TEXT` holding JSON (`metadata`) | `jsonb` |
| `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` | `timestamptz DEFAULT now()` |
| `UNIQUE(username, channel)` | unchanged |
| `date TEXT` (daily_activity) | `date` (or keep `text` if callers pass ISO strings) |

Add explicit indexes matching current implicit access patterns
(`accounts(username, channel)`, `transactions(username, channel, created_at)`,
`daily_activity(username, channel, date)`).

### 4.2 Transactional credit/debit
```python
async def debit(self, username, channel, amount, *, type, reason, ...) -> int:
    async with self._pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                """UPDATE accounts
                     SET balance = balance - $3,
                         lifetime_spent = lifetime_spent + $3,
                         last_active = now()
                   WHERE username = $1 AND channel = $2 AND balance >= $3
               RETURNING balance""",
                username, channel, amount,
            )
            if row is None:
                raise InsufficientFunds(...)          # existing error type
            await con.execute(
                "INSERT INTO transactions (username, channel, amount, type, reason, ...) "
                "VALUES ($1,$2,$3,$4,$5, ...)",
                username, channel, -amount, type, reason, ...,
            )
            return row["balance"]
```
`credit` is the symmetric positive update + insert. Multi-account operations (tips, rain,
heist payouts) that touch two accounts wrap both updates + both transaction rows in one
`con.transaction()`; use `SELECT … FOR UPDATE` ordered by a stable key to avoid deadlocks.

### 4.3 Backend dispatch
Keep the SQLite class; add `EconomyDatabasePg`. `main.py` selects via
`DatabaseConfig.backend`. Both satisfy a shared `Protocol`/ABC so `main.py`/`command_handler`
are backend-agnostic (mypy enforces the interface).

## 5. Implementation Plan

- **Create** `sql/001_schema.sql` (+ later files as needed) and a tiny `schema_version` applier
  in `db/pool.py` or `db/migrate.py`.
- **Use Alembic** for all schema migrations to ensure versioned, reproducible transitions.
- **Create** `EconomyDatabasePg` (in `database.py` or `database_pg.py`) implementing the shared
  interface with asyncpg.
- **Refactor** `EconomyDatabase` and `EconomyDatabasePg` to share a `Protocol` (`EconomyStore`).
- **Modify** `main.py` to build the pool (Sortie 1 factory) and instantiate the right store;
  call the schema applier on startup when backend is postgres.
- Map `sqlite3.IntegrityError`/`OperationalError` semantics to asyncpg equivalents where the
  data layer raises typed errors.

## 6. Testing Strategy

- **Fixture**: a Postgres test database (via `asyncpg` against a local/CI PG, or `pytest`
  markers that skip when no PG). Provide a `conftest` `pg_database` fixture parallel to the
  existing `tmp_path` sqlite fixture.
- **Parity tests**: run the core `test_database.py` assertions against *both* backends.
- **Concurrency test (new, critical)**: fire N concurrent `debit` calls on one account with
  balance < N×amount; assert exactly `balance // amount` succeed, balance never negative,
  transaction count matches successful debits.
- **Two-account atomicity**: a tip that fails mid-way leaves both accounts unchanged.

## 7. Acceptance Criteria

- [ ] All tables/indexes created by idempotent `sql/` DDL; `schema_version` tracks them.
- [ ] Every `EconomyStore` method has parity across sqlite and postgres backends.
- [ ] Concurrent-debit test passes: no lost updates, no negative balances.
- [ ] Two-account operations are all-or-nothing.
- [ ] `main.py` selects backend from config; sqlite still works.
- [ ] `black`, `ruff`, `mypy` (strict), `pytest` green on both backends.

## 8. Rollout

- Ship configured for `sqlite` still; Postgres exercised in tests/staging. Production cutover
  happens in Sortie 5 after ETL (Sortie 4).

## 9. Documentation

- `docs/` schema note; CHANGELOG `feat:` (adds Postgres backend). Flag the credit/debit
  transaction semantics as a high-stakes change.
