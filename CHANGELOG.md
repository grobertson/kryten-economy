# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.14] - 2026-06-14

### Added

- **`account.summary` command** — User-facing account snapshot returning balance, lifetime earned, current rank (name, level, tier count), next-rank progress (remaining + progress percent), active perks, spend discount, currency name/symbol, and editable vanity items (`custom_greeting`, `custom_color`) with their costs and enabled flags. Purpose-built for surfaces like the webqueue dashboard so a single round-trip renders the full progression panel.
- **`vanity.set_greeting` command** — Validates (≤200 chars), applies rank discount, debits, and persists the user's `custom_greeting`. Returns `{charged, discount, new_balance, value}`.
- **`vanity.set_color` command** — Accepts an arbitrary 6-digit hex (normalized to `#RRGGBB`), applies rank discount, debits, and persists it as the `chat_color` vanity item. Replaces the palette-only restriction for API-driven purchases (the `buy color` PM command is unchanged). Returns `{charged, discount, new_balance, value}`.

[0.8.14]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.14

## [0.8.13] - 2026-06-05

### Added

- **`base_cost` in queue-preview response** — `spending.queue_preview` now returns the pre-discount `base_cost` alongside the discounted `cost_z`, allowing clients to render an exact receipt (price, discount amount, total) without deriving the base from the discount percentage.

[0.8.13]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.13

## [0.8.12] - 2026-06-04

### Changed

- **Version bump** — No code changes; released to align deployed version with confirmed-working queue spending integration (kryten-webqueue v0.4.4)

[0.8.12]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.12

## [0.8.11] - 2026-05-30

### Added

- **Queue spending commands** — Three new NATS command handlers: `spending.queue_preview` (read-only cost estimate with eligibility checks), `spending.queue` (atomic validate + debit with idempotency via `request_id`), and `spending.queue_refund` (compensating credit, also idempotent)
- **`queue_spend_requests` table** — Idempotency ledger for queue spend/refund operations; prevents double-debits and double-credits
- **DB helpers** — `insert_queue_spend_request`, `get_queue_spend_request`, `mark_queue_spend_refunded`, `increment_daily_queues_used`
- **Blackout window support** — `_is_blackout_active` helper uses croniter to check if current time falls within a configured blackout window
- **Rank queue bonus** — Elevated ranks (vip, mod, admin, owner, trusted, regular) receive +1 queue/day

[0.8.11]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.11

## [0.8.10] - 2026-03-13

### Fixed

- **Lifecycle registration version mismatch** - Service lifecycle metadata is now injected during config load so `service.name` is always `economy` and `service.version` always matches the installed package version (instead of drifting to `1.0.0` defaults)

### Changed

- **Config example cleanup** - `config.example.yaml` no longer asks users to set service name/version manually; lifecycle toggles remain configurable
- **Retention realism** - Removed inactive-user nudge example from `config.example.yaml` (no reliable contact path for absent/offline users)
- **Bounties docs sync** - Restored `bounties` section in `config.example.yaml` with schema defaults

[0.8.10]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.10

## [0.8.9] - 2026-03-13

### Fixed

- **Chat message handler crash** - Removed invalid `event.uid` access from `handle_chatmsg` in `kryten_economy/main.py`; `ChatMessageEvent` does not define `uid`, which could raise `AttributeError` and skip chat-trigger processing for that message

[0.8.9]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.9

## [0.8.7] - 2026-03-13

### Added

- **User guide** — New end-user documentation at `docs/user-guide.md` with PM command quick start, queue/search flow, event window behavior, and troubleshooting notes; written to render cleanly on GitHub and Reddit

### Changed

- **Admin guide refresh** — Updated `docs/admin-guide.md` to reflect 0.8.6/0.8.7 behavior, including `status`/`eventstatus`, queue/search event lockout semantics, now-playing queue credit announcement, and corrected ad-hoc event command syntax
- **Repo hygiene** — Added `uv.lock` to `.gitignore` and documented the user guide in README

[0.8.7]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.7

## [0.7.4] - 2026-03-03

### Fixed

- **Silent heist join failure** — When a user says "join" in chat but lacks funds (or hits another error), the bot now PMs them an in-character explanation instead of failing silently
- **Heist announcement missing buy-in** — The crew-forming announcement now shows the wager amount so users know the cost before joining

[0.7.4]: https://github.com/grobertson/kryten-economy/releases/tag/v0.7.4

## [0.7.3] - 2026-03-03

### Added

- **Heist Narrator** — `HeistNarrator` and `heist_narratives` modules with 160+ built-in narrative templates; supports static, LLM, and hybrid generation modes
- **Metrics Collector** — Centralised `MetricsCollector` replacing per-attribute counters; SQLite-backed counter persistence (replaces NATS KV)
- **Chat heist join** — Users can now type "join" in chat to join an active heist (in addition to PMs); debug logging added to `handle_chat_heist_join`
- **Grafana dashboard** — JSON dashboard definition for economy metrics
- **Releasing guide** — `docs/releasing.md` with release workflow documentation
- **Start script** — `start-economy.ps1` convenience launcher for Windows

### Changed

- **Config hardening** — `config.yaml` removed from version control and added to `.gitignore` (contains secrets)
- **Scheduler / presence / database** — Various robustness improvements and metrics integration

### Fixed

- **Heist join unreachable on production** — Chat-based heist join hook in `handle_chatmsg` was never committed; production heists timed out because nobody could join via chat

[0.7.3]: https://github.com/grobertson/kryten-economy/releases/tag/v0.7.3

## [0.7.2] - 2026-03-03

### Fixed

- **Missing dependency** — `croniter` added to `[project.dependencies]` in `pyproject.toml`; was required at runtime but omitted from the package manifest

[0.7.2]: https://github.com/grobertson/kryten-economy/releases/tag/v0.7.2

## [0.7.1] - 2026-03-03

### Added

- **`about` command** — New `system.about` NATS command and `about` PM command; reports current version (from package metadata — single source of truth) and formatted uptime (`Xh Ym Zs`)

[0.7.1]: https://github.com/grobertson/kryten-economy/releases/tag/v0.7.1

## [0.1.0] - 2025-07-12

### Added

- **Core foundation** — SQLite WAL database (12 tables), Pydantic config validation, Prometheus metrics server, CLI with `--config`, `--log-level`, `--validate-config`
- **Streaks, milestones & dwell** — Presence tracking with join debounce, streak calculation with gap tolerance, dwell-time milestone rewards, consecutive-day bonuses
- **Chat earning triggers** — Message, emote, poll-vote, playlist-add, and first-of-day triggers with per-trigger cooldowns, earning caps, and configurable payouts
- **Gambling** — Slots (configurable reels, symbol weights, jackpot), coin flip (PvE/PvP), challenge (user-vs-user wagers), heist (cooperative scaling risk/reward)
- **Spending, queue tips & shop** — Queue position tipping, vanity shop (titles, badges, colors, GIFs), gift system, rank-based discounts, transaction history
- **Achievements, ranks & progression** — One-time achievement badges with chat announcements, B-movie themed rank ladder, rank perks (discounts, multipliers, exclusive items)
- **Events, multipliers & bounties** — Scheduled events (happy hour, double-XP), multiplier stacking with priority and decay, daily competitions, user-created bounties with admin approval
- **Admin, reporting & visibility** — Admin PM commands (grant, revoke, set, reset, snapshot, digest, reload, freeze, event management), daily snapshots, digest reports, audit logging
- **Polish & hardening** — EventAnnouncer with dedup/batching, GreetingHandler, PM rate limiter, error isolation on all handlers, integration test suite, systemd service unit, example config

[0.1.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.1.0
