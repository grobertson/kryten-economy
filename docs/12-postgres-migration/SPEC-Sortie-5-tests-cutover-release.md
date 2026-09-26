# SPEC — Sortie 5: Test-suite migration, cutover runbook, release

**Sprint**: 12 — `12-postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sorties 1–4
**Estimated**: 3–5 h

---

## 1. Overview

Finish the migration: make the test suite run against Postgres (with a graceful skip when no
PG is available), write the production cutover runbook, bump the version, and update the
CHANGELOG and config docs. After this sortie the service is production-ready on Postgres with a
one-flip rollback to SQLite.

## 2. Scope and Non-Goals

**In scope**
- CI/test wiring for a Postgres fixture; keep sqlite parity tests.
- `docs/postgres-cutover.md` runbook (stop → migrate → verify → start → validate → rollback).
- `pyproject.toml` version → `0.16.0`; `CHANGELOG.md`; `config.example.yaml`, `README.md`,
  `docs/admin-guide.md` updates.

**Non-goals**
- Backup cron wiring (separate sprint). Dropping the SQLite backend (future).

## 3. Requirements

- `uv run pytest` passes with no PG present (postgres-marked tests skip cleanly) **and** passes
  fully when PG is present (CI service container or local `kryten-pg`).
- Runbook is copy-pasteable and includes the exact verification command and rollback steps.
- CHANGELOG flags: new Postgres backend, config-schema addition, transactional credit/debit,
  and the one-time ETL. SemVer minor bump (pre-1.0 breaking).

## 4. Design

### 4.1 Test wiring
- `conftest.py`: a `pg_dsn` fixture reading `KRYTEN_ECONOMY_TEST_DSN`; if unset, `pytest.skip`
  postgres-marked tests. A `@pytest.mark.postgres` marker registered in `pyproject.toml`.
- CI: add a Postgres service (GitHub Actions `services: postgres:` or the shared `kryten-pg`)
  and export the test DSN; run the full suite including postgres-marked tests.

### 4.2 Cutover runbook (`docs/postgres-cutover.md`)
```
1. Create DB + role:   CREATE DATABASE kryten_economy; CREATE ROLE kryten … ; GRANT …
2. Deploy 0.16.0 with backend=postgres pointing at the EMPTY kryten_economy DB.
3. systemctl stop kryten-economy
4. python -m kryten_economy.migrate_sqlite_to_pg --source /var/lib/.../economy.db --verify
5. Confirm --verify exits 0 (counts + total circulation + per-account balances match).
6. systemctl start kryten-economy ; watch logs + a 'balance' PM smoke test.
7. Keep economy.db untouched for one release (rollback = set backend=sqlite, restart).
```

## 5. Implementation Plan

- **Modify** `tests/conftest.py`: add `pg_dsn`/`pg_database` fixtures + skip logic.
- **Register** the `postgres` marker in `pyproject.toml` `[tool.pytest.ini_options]`.
- **Add** CI Postgres service + test DSN env in the workflow.
- **Create** `docs/postgres-cutover.md`.
- **Bump** `pyproject.toml` version to `0.16.0`; **update** `CHANGELOG.md`.
- **Update** `config.example.yaml`, `README.md`, `docs/admin-guide.md`.

## 6. Testing Strategy

- Verify the full suite skips cleanly with no PG and passes fully with PG.
- Dry-run the runbook end-to-end against a scratch DB using a copy of a real `economy.db`.

## 7. Acceptance Criteria

- [ ] `uv run pytest` green both with and without PG available.
- [ ] Cutover runbook validated against a real DB copy; `--verify` exits 0.
- [ ] Version `0.16.0`; CHANGELOG entry present and flags the high-stakes changes.
- [ ] `config.example.yaml`/README/admin-guide document the `database` block.
- [ ] `black`, `ruff`, `mypy`, `pytest` green.

## 8. Rollout

- Follow the runbook in a maintenance window. Announce the config change. Hold the SQLite file
  and the `sqlite` backend for one release as the rollback path.

## 9. Documentation

- Runbook + CHANGELOG + config docs as above. Add a follow-on note: **backup cron wiring is a
  separate sprint** (add `kryten_economy` to the `pg_dump` job).
