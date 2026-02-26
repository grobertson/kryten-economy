# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
