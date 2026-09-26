# SPEC — Sortie 4: One-shot ETL (SQLite → PostgreSQL)

**Sprint**: 12 — `12-postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 3 (Postgres schema must exist)
**Estimated**: 4–6 h

---

## 1. Overview

A standalone, re-runnable migration script that copies every row from the live SQLite
`economy.db` into the `kryten_economy` Postgres database, then **verifies** the copy —
balance-for-balance. Currency is unforgiving, so this sortie prioritises correctness,
resumability, and a proof of parity over speed.

## 2. Scope and Non-Goals

**In scope**
- `python -m kryten_economy.migrate_sqlite_to_pg` (a CLI entry) with `--dry-run`, `--verify`,
  `--source`, and Postgres config resolution reused from the service.
- Ordered table copy respecting FK/logical dependencies (accounts before transactions, etc.).
- Idempotent + resumable: re-running does not duplicate rows; a crash mid-run resumes cleanly.
- Verification pass: row counts per table + **total circulation** + per-account balance
  checksum must match source exactly.

**Non-goals**
- No live dual-write. No schema changes. Not part of the service runtime path.

## 3. Requirements

- **Non-destructive**: opens SQLite read-only (`mode=ro`); never writes or deletes the source.
- **Idempotent**: uses `INSERT … ON CONFLICT DO NOTHING`/`DO UPDATE` keyed on natural keys
  (`accounts(username, channel)`, transaction PKs, `daily_activity(username, channel, date)`).
- **Batched + transactional**: commit per batch; a failed batch rolls back only itself and is
  safely retried.
- **Resumable**: The script must be safe to restart from the beginning using `ON CONFLICT` logic. 
  A full re-run should result in an identical state without duplication or corruption.
- **Verifiable**: `--verify` recomputes and compares:
  - `COUNT(*)` per table (source vs target),
  - `SUM(balance)` over accounts (total circulation),
  - a per-account `(username, channel) → balance` diff, reporting any drift.
- Identity/serial columns: reset sequences after copy so new inserts don't collide.

## 4. Design

```
open sqlite (ro)  ──▶  for table in ORDER:
                          stream rows in batches of N (e.g. 1000)
                          COPY / executemany INSERT … ON CONFLICT into PG (per-batch txn)
                        reset identity sequences
--verify:   compare counts, SUM(balance), per-account balances → exit non-zero on any drift
--dry-run:  run against a scratch schema/db; report what would change, touch nothing real
```

- Prefer `asyncpg.copy_records_to_table` for large tables (transactions, daily_activity);
  fall back to batched `executemany` where `ON CONFLICT` upsert semantics are needed.
- JSON `metadata` TEXT → `jsonb`: parse-and-bind (validate JSON; on parse failure, log an error and store the raw string as a fallback to prevent data loss).
- Timestamp strings → `timestamptz`: reuse the service's existing `utils.py` timestamp parser
  so format handling matches runtime.

## 5. Implementation Plan

- **Create** `kryten_economy/migrate_sqlite_to_pg.py` (argparse CLI, async main).
- Reuse `db/pool.py` (Sortie 1) for target connection; open SQLite with `sqlite3`/`aiosqlite`
  read-only.
- Define the table copy order + natural-key upsert per table.
- Implement `--verify` and `--dry-run`.

## 6. Testing Strategy

- Build a fixture SQLite DB with known rows (a few accounts, transactions, daily activity,
  gambling stats); migrate to a PG test DB; assert:
  - counts match, `SUM(balance)` matches, per-account balances match,
  - **re-running is a no-op** (idempotency),
  - a simulated mid-run crash (kill after batch 1) resumes to a correct final state.
- `--verify` exits non-zero when a balance is deliberately perturbed in target.

## 7. Acceptance Criteria

- [ ] `--dry-run` touches nothing real and reports the plan.
- [ ] Full run copies all tables; sequences reset; second run is a no-op.
- [ ] `--verify` proves count + total-circulation + per-account balance parity (exit 0).
- [ ] Source SQLite file is byte-identical after the run (read-only proven).
- [ ] `black`, `ruff`, `mypy`, `pytest` green.

## 8. Rollout

- Run during the cutover window (Sortie 5): stop service → migrate → verify → start on PG.

## 9. Documentation

- A `docs/postgres-cutover.md` runbook section (authored in Sortie 5) references this CLI.
