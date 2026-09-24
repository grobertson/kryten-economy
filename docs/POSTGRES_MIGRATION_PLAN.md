# PostgreSQL Migration & Chandra-1 Deployment Plan: Kryten-Economy

**Sprint**: `12-postgres-migration`  
**Status**: Approved Architecture & Deployment Plan  
**Target System**: `kryten-economy`  
**Target Platform**: PostgreSQL on `chandra-1` (Podman Container)  
**Target Version**: `0.16.0`  
**Workflow**: [../AGENT-WORKFLOW-GUIDE.md](../AGENT-WORKFLOW-GUIDE.md)  
**Sprint Specs**: [12-postgres-migration/](12-postgres-migration/)  

---

## Executive Summary

`kryten-economy` is the central engagement and currency microservice of the Kryten ecosystem. It manages player accounts, balances, transactions, gambling engines (slots, flip, challenges, heist, race, trivia, blackjack), daily presence rewards, streaks, vanity purchases, and pay-to-play queue integration.

Currently, `kryten-economy` persists state to a local SQLite database (`economy.db`) via synchronous SQLite operations dispatched on thread executors (`asyncio.run_in_executor`). While sufficient for low-traffic channels, SQLite's single-writer limitation introduces write serialization and prevents true row-level locking during high-concurrency presence ticks and simultaneous gambling events.

This plan details the migration of `kryten-economy` to a dedicated **PostgreSQL** database (`kryten_economy` or `zcoinbank_economy`) on **`chandra-1`**, running containerized via **Podman** within the `zcoinbank` pod network.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                ARCHITECTURAL EVOLUTION                                 │
├────────────────────────────────────────┬───────────────────────────────────────────────┤
│ CURRENT (SQLite on Host)               │ TARGET (PostgreSQL on chandra-1 Podman)       │
├────────────────────────────────────────┼───────────────────────────────────────────────┤
│ • Local SQLite database file.          │ • Dedicated PostgreSQL DB on chandra-1.       │
│ • Synchronous `sqlite3` wrapped in     │ • Native asynchronous `asyncpg` pool.         │
│   `run_in_executor(None, _sync)`.      │ • True ACID row-level locking                 │
│ • Single-writer lock contention during │   (`SELECT ... FOR UPDATE`, atomic decrement).│
│   high-volume presence ticks / games.  │ • Multi-client concurrent execution without   │
│ • Leaked raw connection access in      │   lock starvation.                            │
│   command handlers (`_get_connection`).│ • Strict data encapsulation in `EconomyDB`.   │
│ • Schema migrations via dynamic        │ • Tracked, versioned DDL migration scripts    │
│   `ALTER TABLE ... except Error`.      │   (`schema_version` table).                   │
└────────────────────────────────────────┴───────────────────────────────────────────────┘
```

---

## 1. System Goals & Core Principles

1. **Uncompromising Financial Integrity**:
   - Currency is the ecosystem's crown jewels. Every balance mutation (`credit`, `debit`, `transfer`, `refund`) must execute within an atomic transaction alongside an immutable `transactions` audit log entry.
   - Concurrency protection: Row-level locks (`SELECT ... FOR UPDATE` or atomic `UPDATE accounts SET balance = balance - $3 ... WHERE balance >= $3 RETURNING balance`) guarantee zero possibility of double-spending or negative balances.
2. **Strict Data Retention Policy (Zero-Pruning Constraint)**:
   - **Financial and transaction logs are permanent.**
   - `accounts`, `transactions`, `queue_spend_requests`, `tip_history`, `achievements`, and `vanity_items` are **NEVER pruned or truncated automatically**.
   - Account cleanup is strictly restricted to the offline admin CLI tool (`kryten-economy-prune`) with hard safety barriers (never deletes accounts with lifetime spending > 0, active vanity items, or economy bans).
3. **Encapsulated Data Layer**:
   - Completely eliminate all raw connection leaks (`self._app.db._get_connection()`) from `command_handler.py` and other modules. All database interactions must route through typed, public async methods on `EconomyDatabase`.
4. **Zero Upstream Contract Breakage**:
   - The NATS request-reply interface (`kryten.economy.command`) and event subscriptions remain 100% byte-compatible.
5. **Lossless, Self-Verifying ETL Migration**:
   - A one-shot CLI migration script (`migrate_sqlite_to_pg.py`) transfers all historical data from SQLite to PostgreSQL and executes an exact verification: total circulation (`SUM(balance)`), per-account balances, and transaction counts must match to the exact coin.

---

## 2. Technical Architecture on Chandra-1

### 2.1 Chandra-1 Infrastructure & Topology

- **Host**: `chandra-1`
- **Database Server**: PostgreSQL 16+ on `chandra-1` (port 5432)
- **Database Name**: `kryten_economy` (or `zcoinbank_economy`)
- **Database Role**: `kryten` (owns the database; granted least privilege)
- **Deployment Container**: Podman Quadlet container `zcoinbank-economy.container` attached to `podman-zcoinbank` network.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 chandra-1 Host System                                  │
│                                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │                     Podman Network: podman-zcoinbank                             │  │
│  │                                                                                  │  │
│  │  ┌─────────────────────────────┐           ┌──────────────────────────────────┐  │  │
│  │  │      zcoinbank-robot        │           │       zcoinbank-economy          │  │  │
│  │  │     (CyTube / NATS)         │◄─────────►│     (KrytenClient / NATS)        │  │  │
│  │  └──────────────┬──────────────┘   NATS    └────────────────┬─────────────────┘  │  │
│  │                 │                kryten.economy.command     │                    │  │
│  │                 ▼                                           │                    │  │
│  │  ┌─────────────────────────────┐                            │                    │  │
│  │  │       zcoinbank-nats        │                            │                    │  │
│  │  │        (NATS Server)        │                            │                    │  │
│  │  └─────────────────────────────┘                            │                    │  │
│  └─────────────────────────────────────────────────────────────┼────────────────────┘  │
│                                                                │                       │
│                                                                ▼                       │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │                            PostgreSQL (chandra-1:5432)                           │  │
│  │                            Database: kryten_economy                              │  │
│  │                            Owner: kryten                                         │  │
│  └──────────────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 2.2 Configuration & DSN Secret Resolution

In `kryten_economy/config.py`, the configuration model is updated with a `database` block following the ecosystem-wide DSN resolution pattern:

```yaml
# config.yaml (or config.json)
database:
  backend: postgres # "sqlite" | "postgres" (default: "sqlite" during transition)
  db_path: /var/lib/kryten/zcoinbank/kryten-economy/economy.db
  postgres:
    dsn_env: KRYTEN_ECONOMY_DSN
    host: localhost
    port: 5432
    user: kryten
    dbname: kryten_economy
    password_env: KRYTEN_ECONOMY_PG_PASSWORD
    pool_min_size: 2
    pool_max_size: 10
```

**Resolution Order**:
1. `dsn_env` (Environment variable containing full URI)
2. `dsn` (Literal URI if provided)
3. Assembled URI `postgresql://{user}:{password}@{host}:{port}/{dbname}` where password is read from `password_env`.
*Zero secrets are committed to version control or hardcoded in configuration files.*

---

### 2.3 PostgreSQL Schema & Type Mapping

The 22 tables of `kryten-economy` are translated into clean PostgreSQL DDL:

| Table | Primary Role | Key PostgreSQL Enhancements |
|---|---|---|
| `accounts` | Player balances, ranks, vanity items, bans | `UNIQUE(username, channel)`, `balance bigint NOT NULL DEFAULT 0` with `CHECK (balance >= 0)`. |
| `transactions` | Immutable audit ledger for every currency mutation | `id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY`, `created_at timestamptz DEFAULT clock_timestamp()`, `metadata jsonb`. |
| `daily_activity` | Daily engagement metrics, streaks, daily free spin | `UNIQUE(username, channel, date)`, `date date NOT NULL`. |
| `streaks` | Consecutive daily / weekend dwell streaks | `UNIQUE(username, channel)`, `current_daily_streak integer DEFAULT 0`. |
| `hourly_milestones` | Dwell milestones (1h, 3h, 6h, 12h, 24h) | `UNIQUE(username, channel, date)`, `boolean` flags. |
| `trigger_cooldowns` | Per-user rate-limiting for earning triggers | `UNIQUE(username, channel, trigger_id)`, `window_start timestamptz`. |
| `trigger_analytics` | System-wide trigger hits & payout tracking | `UNIQUE(channel, trigger_id, date)`. |
| `gambling_stats` | Career win/loss and spin counts per game | `UNIQUE(username, channel)`. |
| `pending_challenges` | User-vs-user coin wagers | `id bigint GENERATED ALWAYS AS IDENTITY`, `expires_at timestamptz`. |
| `race_results` | Precomputed race results & spectator state | `race_id text PRIMARY KEY`. |
| `race_bets` | Wagers placed on race outcomes | `FOREIGN KEY (race_id) REFERENCES race_results(race_id) ON DELETE CASCADE`. |
| `trivia_stats` | Trivia accuracy, streaks, wagers | `UNIQUE(username, channel)`. |
| `blackjack_stats` | Solo blackjack hand outcomes & wagers | `UNIQUE(username, channel)`. |
| `tip_history` | Record of peer-to-peer coin transfers | `id bigint GENERATED ALWAYS AS IDENTITY`, `created_at timestamptz`. |
| `pending_approvals` | Vanity / GIF approval queue for admins | `id bigint GENERATED ALWAYS AS IDENTITY`, `status text DEFAULT 'pending'`. |
| `vanity_items` | Active chat colors, greetings, titles | `UNIQUE(username, channel, item_type)`, case-preserving username with case-insensitive index `LOWER(username)`. |
| `achievements` | Unlocked achievement badges | `UNIQUE(username, channel, achievement_id)`. |
| `bounties` | User-created bounties & rewards | `id bigint GENERATED ALWAYS AS IDENTITY`. |
| `economy_snapshots` | System-wide float & circulation snapshots | `id bigint GENERATED ALWAYS AS IDENTITY`, `snapshot_time timestamptz`. |
| `banned_users` | Moderation ban registry | `UNIQUE(username, channel)`. |
| `queue_spend_requests` | Idempotency registry for pay-to-play queue | `request_id text PRIMARY KEY`, `created_at timestamptz`. |
| `service_metrics` | Lifetime service performance counters | `key text PRIMARY KEY, value bigint NOT NULL`. |

---

### 2.4 Transactional Mutation Patterns

All mutations use atomic async transactions.

#### Atomic Single-Account Debit (Debit-or-Fail)
```python
async def debit(
    self,
    username: str,
    channel: str,
    amount: int,
    *,
    type: str,
    reason: str | None = None,
    trigger_id: str | None = None,
    related_user: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Atomically debit user balance and record transaction."""
    if amount <= 0:
        raise ValueError("Debit amount must be positive")

    async with self._pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE accounts
                   SET balance = balance - $3,
                       lifetime_spent = lifetime_spent + $3,
                       last_active = clock_timestamp()
                 WHERE username = $1 
                   AND channel = $2 
                   AND balance >= $3
             RETURNING balance
                """,
                username, channel, amount,
            )
            if not row:
                raise InsufficientFunds(f"User {username} has insufficient balance for debit of {amount}")

            new_balance = row["balance"]
            await conn.execute(
                """
                INSERT INTO transactions (username, channel, amount, type, reason, trigger_id, related_user, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                username, channel, -amount, type, reason, trigger_id, related_user,
                json.dumps(metadata) if metadata else None,
            )
            return new_balance
```

#### Atomic Multi-Account Transfer (Tips / Heist Payouts)
To prevent deadlocks when transferring coins between two accounts, row locks are acquired in consistent alphabetical order by username:

```python
async def transfer(
    self,
    sender: str,
    receiver: str,
    channel: str,
    amount: int,
    *,
    reason: str = "tip",
) -> tuple[int, int]:
    """Atomically transfer currency between two accounts with deadlock prevention."""
    first, second = sorted([sender, receiver])

    async with self._pool.acquire() as conn:
        async with conn.transaction():
            # Lock both accounts in deterministic order
            await conn.execute(
                "SELECT username FROM accounts WHERE username IN ($1, $2) AND channel = $3 FOR UPDATE",
                first, second, channel,
            )
            # Execute debit and credit
            # Insert matching transaction rows
            ...
```

---

## 3. Sprint Execution Roadmap (5 Sorties)

The migration is broken down into 5 sequential, independently verifiable sorties:

```
Sortie 1: Config & Connection Pool
    │
    ▼
Sortie 2: Data Layer Encapsulation (Eliminate Raw SQL Leaks)
    │
    ▼
Sortie 3: PostgreSQL Schema & Async Engine Implementation
    │
    ▼
Sortie 4: One-Shot ETL Migration CLI & Parity Verification
    │
    ▼
Sortie 5: Tests, Chandra-1 Podman Unit & Production Cutover
```

### [Sortie 1: Database Config & Pool Plumbing](12-postgres-migration/SPEC-Sortie-1-db-config-and-pool.md)
- Add `PostgresConfig` and `DatabaseConfig` to `kryten_economy/config.py`.
- Add DSN resolution logic supporting `KRYTEN_ECONOMY_PG_PASSWORD` and `KRYTEN_ECONOMY_DSN`.
- Add `asyncpg>=0.29.0` to `pyproject.toml`.
- Update `config.example.yaml` with documented `database:` block.

### [Sortie 2: Data Layer Encapsulation](12-postgres-migration/SPEC-Sortie-2-encapsulate-data-layer.md)
- Audit codebase for raw connection leaks (`self._app.db._get_connection()`).
- Promote all inline queries in `command_handler.py`, `pm_handler.py`, and `metrics_collector.py` to official methods on `EconomyDatabase`.
- Remove `# noqa: SLF001` suppressions across all handler files.
- Verify 100% test pass rate on SQLite.

### [Sortie 3: Schema Translation & AsyncPG Layer](12-postgres-migration/SPEC-Sortie-3-schema-and-asyncpg-layer.md)
- Create `sql/001_schema.sql` with PostgreSQL DDL covering all 22 tables, check constraints, and indexes.
- Build `kryten_economy/database_pg.py` with native `asyncpg` queries and atomic transaction blocks.
- Implement `schema_version` migration tracker.
- Add backend dispatch in `main.py` allowing instant toggle between `sqlite` and `postgres`.

### [Sortie 4: One-Shot ETL Migration Script](12-postgres-migration/SPEC-Sortie-4-etl-migration.md)
- Build `kryten_economy/migrate_sqlite_to_pg.py` (`--source`, `--dry-run`, `--verify`).
- Stream data in dependency-safe order: `accounts` $\to$ `transactions` $\to$ `daily_activity` $\to$ `streaks` $\to$ `gambling_stats` $\to$ `vanity_items` $\to$ `queue_spend_requests` $\to$ `economy_snapshots`.
- Implement `--verify` validation:
  1. Row count match per table.
  2. Total float match: `SUM(balance)` in PostgreSQL $==$ `SUM(balance)` in SQLite.
  3. Per-account balance diff: `0` discrepancies.
  4. Sequence synchronization for identity columns (`setval()`).

### [Sortie 5: Testing, Chandra-1 Podman Deployment & Cutover](12-postgres-migration/SPEC-Sortie-5-tests-cutover-release.md)
- Configure test suite with `@pytest.mark.postgres` test fixtures.
- Concurrency stress tests: 100 concurrent debits verifying zero lost updates and no negative balances.
- Create Podman deployment unit `deploy/podman/zcoinbank/zcoinbank-economy.container`.
- Write production cutover runbook `docs/postgres-cutover.md`.
- Bump package version to `0.16.0` and update `CHANGELOG.md`.

---

## 4. Production Cutover Runbook

```bash
# -----------------------------------------------------------------------------
# STEP 1: Provision Database on chandra-1
# -----------------------------------------------------------------------------
sudo -u postgres psql -c "CREATE DATABASE kryten_economy OWNER kryten;"
sudo -u postgres psql -d kryten_economy -c "GRANT ALL ON SCHEMA public TO kryten;"

# -----------------------------------------------------------------------------
# STEP 2: Stop Service on Host
# -----------------------------------------------------------------------------
ssh kryten@chandra-1.local "systemctl --user stop zcoinbank-economy"

# -----------------------------------------------------------------------------
# STEP 3: Run Lossless ETL Migration & Parity Verification
# -----------------------------------------------------------------------------
python -m kryten_economy.migrate_sqlite_to_pg \
    --source /var/lib/kryten/zcoinbank/kryten-economy/economy.db \
    --verify

# Ensure script reports: "VERIFICATION SUCCESS: Exact coin parity verified. 0 discrepancies."

# -----------------------------------------------------------------------------
# STEP 4: Update Container Configuration & Start Service
# -----------------------------------------------------------------------------
# Update config.yaml to set `database.backend: postgres`
ssh kryten@chandra-1.local "systemctl --user start zcoinbank-economy"

# -----------------------------------------------------------------------------
# STEP 5: Smoke Testing & Verification
# -----------------------------------------------------------------------------
# Test balance check and earning triggers over NATS
# Verify Prometheus metrics endpoint: http://chandra-1:30290/metrics
```

### Instant Rollback Strategy
If any unexpected issue occurs during cutover:
1. Revert `database.backend: sqlite` in `config.yaml`.
2. Restart the service: `systemctl --user restart zcoinbank-economy`.
3. The original SQLite database remains untouched and byte-identical.
