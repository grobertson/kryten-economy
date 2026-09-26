# SPEC — Sortie 1: Database config + asyncpg pool scaffolding

**Sprint**: 12 — `12-postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: none (first sortie)
**Estimated**: 2–4 h

---

## 1. Overview

Introduce the Postgres backend *selection* and connection plumbing without yet rewriting the
data layer. After this sortie the service can construct an `asyncpg` pool from config and
resolve a DSN securely, while still running on SQLite. This isolates the config/secret surface
from the (larger) engine rewrite in Sortie 3.

## 2. Scope and Non-Goals

**In scope**
- `DatabaseConfig` gains a backend selector and a Postgres sub-config.
- A `_resolve_dsn` helper (ported from the llm pattern) and a pool factory.
- `config.example.yaml` documents the new block; secrets via env only.

**Non-goals**
- No changes to `EconomyDatabase` query methods (Sortie 3).
- No ETL (Sortie 4). No command-handler refactor (Sortie 2).

## 3. Requirements

- Config must support: `backend: sqlite | postgres` (default `sqlite` this release).
- Postgres sub-block: `dsn_env`, `dsn`, `host`, `port`, `user`, `dbname`, `password_env`,
  `password`, `pool_min_size`, `pool_max_size`.
- DSN resolution precedence identical to `PgVectorStore._resolve_dsn`: `dsn_env` → `dsn` →
  assembled with `password_env` preferred over `password`.
- Connection Pool: Use `asyncpg.create_pool()` for PostgreSQL to manage a persistent connection pool.
- Lifecycle Integration: The pool must be initialized during service startup and gracefully closed during shutdown.
- No secret may be required to live in `config.yaml`.

## 4. Design

```python
# kryten_economy/config.py
class PostgresConfig(BaseModel):
    dsn_env: str | None = None
    dsn: str | None = None
    host: str = "localhost"
    port: int = 5432
    user: str = "kryten"
    dbname: str = "kryten_economy"
    password_env: str | None = None
    password: str | None = None
    pool_min_size: int = 1
    pool_max_size: int = 8

class DatabaseConfig(BaseModel):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    path: str = "economy.db"          # sqlite only
    postgres: PostgresConfig = PostgresConfig()
```

```python
# kryten_economy/db/pool.py  (new)
def resolve_dsn(cfg: PostgresConfig) -> str: ...          # mirrors llm _resolve_dsn
async def create_pool(cfg: PostgresConfig) -> asyncpg.Pool: ...
```

`resolve_dsn` raises a clear `ValueError` if `dsn_env` is set but empty (llm parity).

## 5. Implementation Plan

- **Modify** `kryten_economy/config.py`: add `PostgresConfig`, extend `DatabaseConfig`.
- **Create** `kryten_economy/db/__init__.py`, `kryten_economy/db/pool.py`.
- **Add dep** `asyncpg>=0.29` to `pyproject.toml`; `uv sync`.
- **Modify** `config.example.yaml`: add commented `database.postgres` block with env-var
  placeholders (`KRYTEN_ECONOMY_DSN` / `KRYTEN_ECONOMY_PG_PASSWORD`), never a literal secret.
- **Modify** `_write_config.py` if it emits the database block.

## 6. Testing Strategy

- Unit: `resolve_dsn` precedence (dsn_env / dsn / assembled), empty-env error, password_env vs
  password.
- Unit: `DatabaseConfig` parses both a sqlite-only and a postgres block; default backend is
  `sqlite`.
- No live PG needed in this sortie (pool creation covered in Sortie 3 integration tests).

## 7. Acceptance Criteria

- [ ] `database.backend` selectable; defaults to `sqlite`.
- [ ] `resolve_dsn` matches llm precedence and error behavior (unit-tested).
- [ ] `asyncpg` added; `uv sync` clean.
- [ ] `config.example.yaml` shows the block with env placeholders; gitleaks clean.
- [ ] `black`, `ruff`, `mypy`, `pytest` all green.

## 8. Rollout

- No runtime behavior change (still SQLite). Pure additive config + new module.

## 9. Documentation

- Note the new `database` block in `README.md` config section and `docs/admin-guide.md`.
