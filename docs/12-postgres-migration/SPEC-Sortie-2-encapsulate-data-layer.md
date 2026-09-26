# SPEC — Sortie 2: Encapsulate the data layer (remove raw-connection leaks)

**Sprint**: 12 — `12-postgres-migration`
**PRD**: [PRD-postgres-migration.md](PRD-postgres-migration.md)
**Depends on**: Sortie 1 (config), but is otherwise SQLite-only and independently shippable
**Estimated**: 3–6 h

---

## 1. Overview

`command_handler.py` reaches into the data layer via `self._app.db._get_connection()` and
executes raw `sqlite3` in several places. That leak makes the engine swap in Sortie 3
impossible without rewriting handler code too. This sortie **eliminates every raw-connection
call site by promoting each to a proper `EconomyDatabase` method** — done entirely on SQLite
so the existing test suite stays green and the refactor is verifiable in isolation.

This is the single most important de-risking step: after it, Sortie 3 only touches
`database.py`.

## 2. Scope and Non-Goals

**In scope**
- Find every `db._get_connection()` (and any other raw-SQL) use outside `database.py`.
- Add a public, well-named `EconomyDatabase` method for each, preserving exact behavior.
- Replace the call sites; keep results identical.

**Non-goals**
- No Postgres yet. No behavior changes. No new features.

## 3. Requirements

- Zero `_get_connection()` references outside `database.py` after this sortie.
- Every new method is `async`, typed (mypy strict-clean), and returns plain
  dicts/values (not `sqlite3.Row`), matching the existing method style.
- Behavior parity proven by existing tests (extend where a call site was previously untested).

## 4. Design

Enumerate the current leaks (from `command_handler.py`): the `_get_connection()` uses around
lines ~287, ~326, ~355, plus any ad-hoc queries in reporting/admin commands. For each, define a
method on `EconomyDatabase` that encapsulates the query, e.g.:

```python
async def get_top_balances(self, channel: str, limit: int) -> list[dict[str, Any]]: ...
async def get_recent_transactions(self, username: str, channel: str, limit: int) -> list[dict]: ...
# …one method per current inline query, named for intent, not SQL.
```

Call sites become `await self._app.db.get_top_balances(channel, limit)`.

## 5. Implementation Plan

- **Audit** (grep) `_get_connection|\.execute\(|sqlite3` under `kryten_economy/` outside
  `database.py`; list every site.
- **Add** one method per site to `database.py` (still using the current `run_in_executor`
  `_sync` pattern — do not change the engine here).
- **Rewrite** `command_handler.py` (and any metrics/report modules) to call the new methods.
- **Remove** the `# noqa: SLF001` private-access suppressions once the leaks are gone.

## 6. Testing Strategy

- Existing `test_command_handler.py`, `test_metrics_*`, `test_spending_commands.py`, etc. must
  pass unchanged.
- Add targeted unit tests for any newly-extracted method that had no direct coverage.
- Confirm no `SLF001` suppressions remain (ruff).

## 7. Acceptance Criteria

- [ ] Zero raw SQLite access outside `database.py`: `grep -rnE "_get_connection|sqlite3|\.execute\(" kryten_economy` returns hits only inside `database.py`.
- [ ] All new methods typed and mypy-clean; no `SLF001` noqa remains.
- [ ] `black`, `ruff`, `mypy`, `pytest` green; behavior identical.

## 8. Rollout

- Pure refactor; no config or runtime change. Safe to ship on its own ahead of the engine swap.

## 9. Documentation

- CHANGELOG `refactor:` entry (internal, no behavior change).
