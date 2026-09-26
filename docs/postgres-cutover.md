# PostgreSQL Cutover Runbook — kryten-economy

**Applies to:** kryten-economy 0.16.0 and later
**Written:** 2026-09-26
**Status:** **EXECUTED on chandra-1 / zcoinbank-economy, 2026-09-26 03:43–03:52 PDT**

This is the procedure for moving the running economy service from SQLite to PostgreSQL. It is
written to be copy-pasteable. Every command is a real command; none are illustrative.

> **EXECUTED AND VERIFIED.** Production now runs on PostgreSQL. See §10 for what actually
> happened, including the two failures that did not appear in this document and had to be
> discovered live. Read §10 before reusing this runbook for another service.

> **The rollback is one config line.** `database.backend: "sqlite"` plus a restart. The SQLite
> file is never written to after the cutover, so it stays valid for the whole of the following
> release. Read §6 before you start.

---

## 0. Before you start

You need:

- A maintenance window. Users are asleep; the economy accrues slowly, so a slow migration is
  low-risk, but balance *reads* will fail while the service is stopped. Announce it.
- Root/sudo on the PostgreSQL host.
- The version of the service you are deploying (`0.16.0` or later).
- A **known-good backup of `economy.db`**. The ETL never writes to the source, but you are
  about to stop the only service that writes it.

**Deployment shape assumed here** (yours may differ; substitute paths):

| Thing | Value |
|---|---|
| Service unit | `kryten-economy.service` |
| Install dir | `/opt/kryten/economy` |
| SQLite file | `/opt/kryten/economy/economy.db` |
| Config | `/opt/kryten/economy/config.yaml` |
| PostgreSQL host | `chandra-1.local` |
| PostgreSQL DB | `kryten_economy` |
| PostgreSQL role | `kryten` |

Confirm them before starting:

```bash
systemctl show kryten-economy.service -p WorkingDirectory -p ExecStart
grep -nE '^\s*path:' /opt/kryten/economy/config.yaml
```

---

## 1. Create the role and database (once)

The role and database should already exist from the setup runbook. To check:

```bash
sudo -u postgres psql -tAc \
  "SELECT datname FROM pg_database WHERE datname='kryten_economy'"
```

If it prints `kryten_economy`, skip to §2. If not:

```bash
sudo -u postgres psql -c "CREATE ROLE kryten LOGIN PASSWORD '...';"
sudo -u postgres createdb -O kryten kryten_economy
sudo -u postgres psql -d kryten_economy -c "GRANT ALL ON SCHEMA public TO kryten;"
```

> **`pg_hba.conf` note.** This server authenticates **per database**, not per role. A row for
> one `kryten_*` database does not grant access to another. If your new database is rejected
> with `no pg_hba.conf entry`, add a row for *that* database and **reload** (never restart —
> a restart drops the live connections the other kryten services are holding):
>
> ```bash
> echo 'host kryten_economy kryten 192.168.0.0/24 scram-sha-256' \
>   | sudo tee -a /etc/postgresql/16/main/pg_hba.conf
> sudo systemctl reload postgresql@16-main.service
> ```

## 2. Apply the schema to the empty database

The target must be **empty** — `alembic upgrade head` only creates what is missing, and the ETL
never prunes (see §5).

```bash
cd /opt/kryten/economy
export KRYTEN_ECONOMY_ALEMBIC_URL='postgresql://kryten@chandra-1.local:5432/kryten_economy'
uv run alembic upgrade head
```

That should report `Running upgrade  -> 0001, Initial economy schema for PostgreSQL.`

To confirm the shape, 22 tables and 42 indexes are expected:

```bash
PGPASSWORD="$KRYTEN_ECONOMY_PG_PASSWORD" psql -h chandra-1.local -U kryten \
  -d kryten_economy -tAc \
  "SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='public'),
          (SELECT count(*) FROM pg_indexes WHERE schemaname='public');"
```

## 3. Take the backup and stop the service

Stop the service **before** copying. Copying a live SQLite file can capture a torn page.

```bash
systemctl stop kryten-economy.service
systemctl is-active kryten-economy.service   # expect: inactive

# Now the file is quiescent. Back it up.
cp -a /opt/kryten/economy/economy.db \
      /opt/kryten/economy/backups/economy.db.$(date +%Y%m%dT%H%M%S)
```

Record the expected totals **before** migrating, so you can compare them yourself:

```bash
sqlite3 /opt/kryten/economy/economy.db \
  "SELECT 'accounts', count(*) FROM accounts
   UNION ALL SELECT 'transactions', count(*) FROM transactions
   UNION ALL SELECT 'circulation', coalesce(sum(balance),0) FROM accounts;"
```

Keep this output. §5 does the same check on the far side, and you want to see the numbers
match, not just trust the tool.

## 4. Migrate

```bash
cd /opt/kryten/economy
export KRYTEN_ECONOMY_TARGET_DSN='postgresql://kryten@chandra-1.local:5432/kryten_economy'

uv run python -m kryten_economy.migrate_sqlite_to_pg \
    --source /opt/kryten/economy/economy.db \
    --pg-dsn-env KRYTEN_ECONOMY_TARGET_DSN
```

> **Note the two separate variables.** `KRYTEN_ECONOMY_ALEMBIC_URL` is read by *Alembic*;
> `KRYTEN_ECONOMY_TARGET_DSN` is read by the *ETL*. They happen to hold the same value here,
> but they are different tools and neither one falls back to the other. Setting only
> `KRYTEN_ECONOMY_DSN` does nothing for either.
>
> The ETL deliberately refuses to guess a destination. If you omit `--pg-dsn-env` it resolves
> the DSN from `config.yaml`, and if that file has no usable `database.postgres` block it
> **stops with exit 2** rather than assembling a default `localhost` DSN. That is on purpose:
> copying currency into a default local database is not a recoverable mistake.

Watch for the verification report at the end. **The exit status is the contract:**

| Exit | Meaning | Do this |
|---|---|---|
| `0` | Counts, circulation, and per-account balances all match | Go to §5 |
| `1` | Drift detected — read the report, it names the mismatched rows | **Do not start the service.** Fix and re-run |
| `2` | Usage or configuration error (bad DSN, missing file) | Fix the command and re-run |

The migration is safe to re-run and safe to resume. If it dies halfway, run the same command
again; each table is copied in committed batches and the upsert converges on the same state.

Dry run first if you want to see the plan without writing anything:

```bash
uv run python -m kryten_economy.migrate_sqlite_to_pg \
    --source /opt/kryten/economy/economy.db \
    --pg-dsn-env KRYTEN_ECONOMY_TARGET_DSN --dry-run
```

## 5. Verify independently

Do not rely on the migration's own report alone. Ask the database directly:

```bash
PGPASSWORD="$KRYTEN_ECONOMY_PG_PASSWORD" psql -h chandra-1.local -U kryten \
  -d kryten_economy -tAc \
  "SELECT 'accounts', count(*) FROM accounts
   UNION ALL SELECT 'transactions', count(*) FROM transactions
   UNION ALL SELECT 'circulation', coalesce(sum(balance),0) FROM accounts;"
```

Compare against the §3 numbers. They must be identical.

Then re-run the tool's own check, which is the stricter one — it also compares every
individual account balance and a checksum:

```bash
uv run python -m kryten_economy.migrate_sqlite_to_pg \
    --source /opt/kryten/economy/economy.db \
    --pg-dsn-env KRYTEN_ECONOMY_TARGET_DSN --verify-only
echo "verify exit: $?"   # must be 0
```

## 6. Cut over

Flip the backend and start:

```bash
cd /opt/kryten/economy
# Edit config.yaml:  database.backend: "sqlite"  ->  "postgres"
systemctl start kryten-economy.service
sleep 5
systemctl is-active kryten-economy.service    # expect: active
journalctl -u kryten-economy.service -n 100 --no-pager
```

You are looking for the service connecting to PostgreSQL, not any trace of `sqlite3`. If it
restarts in a loop, the most common cause is the `pg_hba.conf` row from §1.

### Smoke test

Do these in order. They are ordered by blast radius — cheapest first.

```bash
# 1. Does it read?
systemctl show kryten-economy.service -p NRestarts   # should be 0

journalctl -u kryten-economy.service -f              # leave this running
```

Then, in the bot PM as a channel owner:

1. `!balance` — returns a number that matches what the channel had before the cutover.
2. `!balance @someuser` — same for a specific, long-standing user. This is the real test: a
   balance that is silently `0` means the account row did not come across.
3. `!rain` (or another small credit) — confirm currency can be **written** and that the
   balance increases by the expected amount.
4. Confirm it persists: `SELECT sum(balance) FROM accounts;` before and after a spend.

If step 2 or 3 fails, go to rollback. Do not "fix it in the database" — you want a clean
rehearsal, not a hand-patched one.

## 7. Rollback

Available for the whole of the release **after** this one, because the SQLite file is left
untouched on disk.

```bash
systemctl stop kryten-economy.service
# Edit config.yaml:  database.backend: "postgres"  ->  "sqlite"
systemctl start kryten-economy.service
journalctl -u kryten-economy.service -n 50 --no-pager
```

The service resumes from `economy.db` exactly where it left off.

> **Rollback loses any writes made on PostgreSQL after the cutover.** Currency earned or spent
> in the window between cutover and rollback exists only in PostgreSQL. If that window
> contained real activity, take a `pg_dump` of `kryten_economy` *before* rolling back, and
> reconcile afterwards. For this reason, roll back quickly if you roll back at all.

To dump first:

```bash
PGPASSWORD="$KRYTEN_ECONOMY_PG_PASSWORD" pg_dump -h chandra-1.local -U kryten \
  -d kryten_economy -Fc -f /opt/kryten/economy/backups/kryten_economy_pg_$(date +%Y%m%dT%H%M%S).dump
```

---

## 8. After a successful cutover

- **Do not delete `economy.db`.** Keep it for one full release; it is the rollback path.
- **Do add `kryten_economy` to the `pg_dump` backup job.** This is a separate piece of work
  and is *not* covered by this runbook. Until it is done, PostgreSQL has no scheduled backups
  while SQLite did. Do that next.
- Watch the logs for a few hours. Concurrent debits are the thing most likely to behave
  differently, and `ECONSOLE`/`channel` reporting queries are the thing most likely to be
  quietly slow on a cold cache.

---

## 9. Rehearsing without touching production

The whole procedure is safe to rehearse, and you should before the real window:

```bash
# A scratch database you are willing to lose.
sudo -u postgres createdb -O kryten kryten_economy_rehearsal
export KRYTEN_ECONOMY_ALEMBIC_URL='postgresql://kryten@chandra-1.local:5432/kryten_economy_rehearsal'
export KRYTEN_ECONOMY_TARGET_DSN="$KRYTEN_ECONOMY_ALEMBIC_URL"
DSN_ENV=KRYTEN_ECONOMY_TARGET_DSN

uv run alembic upgrade head
cp /opt/kryten/economy/economy.db /tmp/economy_rehearsal.db   # a COPY
uv run python -m kryten_economy.migrate_sqlite_to_pg --source /tmp/economy_rehearsal.db --pg-dsn-env $DSN_ENV
uv run python -m kryten_economy.migrate_sqlite_to_pg --source /tmp/economy_rehearsal.db --pg-dsn-env $DSN_ENV --verify-only

sudo -u postgres dropdb kryten_economy_rehearsal
rm /tmp/economy_rehearsal.db
```

The rehearsal does not prove the *config flip* — that needs the real service, stopped, against
the real config. Do at least that much once on a non-production channel.

---

## 10. What actually happened (2026-09-26)

The cutover ran against production data at real scale. Recorded here because two of the failure
modes were not predictable from the procedure above.

### Numbers

| | |
| --- | --- |
| Accounts | 2,436 |
| Transactions | 5,725,962 |
| Float | 917,950,286 |
| ETL duration | 350 s (upserting ~5.7 M rows into an already-populated target) |
| Total outage | ~9 min (03:43 → 03:52) |
| Verification | `VERIFIED: source and target match`, checksum `43e9895c5c84a8ce` |
| Float immediately after | 917,950,286 — exact, per-account checksum identical |

### Two failures that this document did not predict

**1. The container cannot resolve the PostgreSQL host by name.**

The config used `host: chandra-1.local`. The service runs as a podman Quadlet container on the
`podman-zcoinbank` bridge network, where the host's own hostname does not resolve. The service
crash-looped with `socket.gaierror: Temporary failure in name resolution`.

*Fix:* use the host IP (`192.168.0.116`), which `pg_hba.conf` already permits.

*Lesson:* **verify name resolution from inside the container's network before relying on a
hostname.** The ETL was unaffected because it reads the SQLite file and connects from the host,
so data safety never depended on this — but the service did.

**2. A pre-window abort left the service down for far longer than intended.**

The first run aborted correctly on a precondition, but the abort happened *after* the service had
been stopped in an earlier attempt, and a hung `sqlite3` from a malformed diagnostic query kept
a pipeline alive. The economy was down for longer than the planned window.

*Lesson:* **every abort path in a cutover script must restart the service.** A guard clause that
exits before the service is running is fine; one that exits after it has been stopped is not. The
second run of the script had an explicit `rollback()` on every failure path for this reason.

### Also worth knowing

- **`alembic` is not in the runtime image.** It is not a service dependency, so the schema was
  applied from a host virtualenv (`alembic` + `psycopg2-binary`). If you need to migrate the
  schema from the container, add it.
- **A failed `pg_dump` leaves a 0-byte file that looks like a backup.** The installed backup job
  (`/usr/local/bin/kryten-pg-backup.sh`, nightly 04:17) deletes partial dumps and verifies every
  archive with `pg_restore --list` before reporting success.
- **The metrics/health endpoint answers inside the container on port 28290**, published to the
  host as 30290. Probe it from inside; the host-side port mapping does not accept connections
  from `127.0.0.1` in this deployment.
