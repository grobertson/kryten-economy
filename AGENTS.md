# Kryten-Economy — Project Guidelines

Kryten-Economy is a channel-engagement **currency microservice** in the Kryten ecosystem. It tracks per-channel virtual currency and runs economy/gambling mechanics (blackjack, heist, race, trivia, dailies, rewards) driven by CyTube activity over NATS.

## Architecture
- Event-driven microservice on a **NATS message bus**. Never call other services over direct HTTP — the only HTTP surface in the ecosystem is `kryten-api-gate`.
- Use the shared **`kryten-py`** library (`KrytenClient`) for all NATS, lifecycle, health, and KV state — do not use raw `nats-py`.
- Subscribe to chat/activity events on `kryten.events.{domain}.{channel}.{event_type}` (normalized: lowercase, dots stripped). Handle commands on the single subject `kryten.economy.command`, dispatching on the `command` field and replying `{"service","command","success",...}`.
- **Stateful service:** player accounts, balances, and game state persist. Own your KV buckets (`kryten_{channel|economy}_{type}`) via `get_or_create_kv_store`; bind others read-only with `get_kv_store`. Treat currency mutations as high-stakes — guard against double-spend and race conditions.
- Ecosystem contracts: [../KRYTEN_ARCHITECTURE.md](../KRYTEN_ARCHITECTURE.md), [../kryten-py/COMMAND_PROTOCOL.md](../kryten-py/COMMAND_PROTOCOL.md), [../kryten-py/STATE_MANAGEMENT.md](../kryten-py/STATE_MANAGEMENT.md).

## Build, Test & Conventions
Shared ecosystem rules (uv build/test, versioning, commit style, NATS/KV patterns,
contract-change policy): see [../KRYTEN_CONVENTIONS.md](../KRYTEN_CONVENTIONS.md).
Repo specifics:
- **Python 3.12+**; mypy target `uv run mypy kryten_economy`.
- **Config is YAML** (`config.yaml`), not JSON — auto-discovery resolves to
  `/etc/kryten/kryten-economy/config.yaml` → `./config.yaml`; keep
  `config.example.yaml` in sync. **Planned migration to JSON** (YAML has grown
  unwieldy); when it happens it's a config-schema change — version it, ship a
  migration path, update `config.example.*` and `docs/config-migration.md`.
- **Stateful, high-stakes currency service**: guard against double-spend and race
  conditions on balance/game-state mutations.
- Currency/game-rule changes and `kryten.economy.command` contract changes are
  high-stakes — keep backward compatible and version/document any break.
