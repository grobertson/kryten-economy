# Kryten-Economy — Project Guidelines

Kryten-Economy is a channel-engagement **currency microservice** in the Kryten ecosystem. It tracks per-channel virtual currency and runs economy/gambling mechanics (blackjack, heist, race, trivia, dailies, rewards) driven by CyTube activity over NATS.

## Architecture
- Event-driven microservice on a **NATS message bus**. Never call other services over direct HTTP — the only HTTP surface in the ecosystem is `kryten-api-gate`.
- Use the shared **`kryten-py`** library (`KrytenClient`) for all NATS, lifecycle, health, and KV state — do not use raw `nats-py`.
- Subscribe to chat/activity events on `kryten.events.{domain}.{channel}.{event_type}` (normalized: lowercase, dots stripped). Handle commands on the single subject `kryten.economy.command`, dispatching on the `command` field and replying `{"service","command","success",...}`.
- **Stateful service:** player accounts, balances, and game state persist. Own your KV buckets (`kryten_{channel|economy}_{type}`) via `get_or_create_kv_store`; bind others read-only with `get_kv_store`. Treat currency mutations as high-stakes — guard against double-spend and race conditions.
- Ecosystem contracts: [../KRYTEN_ARCHITECTURE.md](../KRYTEN_ARCHITECTURE.md), [../kryten-py/COMMAND_PROTOCOL.md](../kryten-py/COMMAND_PROTOCOL.md), [../kryten-py/STATE_MANAGEMENT.md](../kryten-py/STATE_MANAGEMENT.md).

## Build and Test
Run from the repo root (uv-managed):
- Install deps: `uv sync`
- Format: `uv run black .`
- Lint (autofix): `uv run ruff check --fix .`
- Types: `uv run mypy kryten_economy`
- Tests: `uv run pytest` (add `--cov=kryten_economy --cov-report=term-missing` for coverage)

Run these before committing. Do not bypass checks (`--no-verify`).

## Conventions
- Python 3.12+, 100% `async`/`await`, Pydantic v2. black/ruff `line-length = 100` (E501 ignored). pytest `asyncio_mode = "auto"`.
- **Config is YAML** (`config.yaml`), not JSON. Auto-discovery resolves to `/etc/kryten/kryten-economy/config.yaml` → `./config.yaml`. Keep `config.example.yaml` in sync.
  - **Planned migration:** config is slated to move from YAML to JSON to match the rest of the ecosystem (the YAML has grown unwieldy). Not yet scheduled — when it happens it's a config-schema change: version it, ship a migration path, and update `config.example.*` and `docs/config-migration.md`.
- **Event handlers must catch and log exceptions — never raise into the event loop.** Rely on `kryten-py` auto-reconnect; don't hand-roll reconnect logic.
- Version lives only in `pyproject.toml [project] version`. Update `CHANGELOG.md` (Keep-a-Changelog + SemVer, ISO dates) for versioned changes.
- Currency/game rule changes and command-contract changes are high-stakes: flag them, keep backward compatibility, and version/document any break.
- Commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`. Branches: `feature/…`, `fix/…`.
