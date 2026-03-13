# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
