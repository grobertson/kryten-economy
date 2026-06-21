# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.2] - 2026-06-21

### Fixed

- **Chat-color apply wiped the channel's hand-maintained CSS (showstopper, regression from 0.10.1).** 0.10.1 removed the "empty CSS read" guard on the theory that an empty read meant "channel has no CSS" and was therefore safe to overwrite. That was wrong: every read layer (`get_state_channel_css` → `kv_get` → low-level `kv_get`) collapses a missing key or NATS error to `""`, so an empty string means *the CSS could not be read*, not that it is empty. Worse, Kryten-Robot never seeds channel CSS into its state KV (see kryten-robot 0.x), so the read is **always** empty — and the rebuild wrote a managed-block-only document, destroying all hand-maintained styling. The guard is restored: an empty/unavailable read now **refuses to write**, returns an `unavailable` outcome, and the purchase is **refunded** (see below) instead of silently no-op'ing.
- **Chat-color usernames now preserve canonical casing (showstopper).** `vanity_items` previously lowercased usernames on write, but CyTube chat-message CSS classes (`.chat-msg-<User>`) are case-sensitive, so the rebuilt block (`.chat-msg-teenagedraculerx`) failed to match for every user with capitals — only the active buyer (whose casing was passed through a display override) worked. `vanity_items` now **stores** usernames with their canonical CyTube casing (matching kryten-webqueue, which never lowercases usernames — its login OTP is PM'd case-sensitively, so authenticated names are always canonical), while username **lookups remain case-insensitive** (`COLLATE NOCASE`) so identity-based reads (greetings, the shop, on-join lookups) still match regardless of the casing a caller happens to have. `merge_vanity_css` now derives selector casing from the database key for **every** managed user, not just the buyer. A one-time, idempotent migration recases existing lowercased `vanity_items` rows from the case-preserving `accounts` table.
- **Failed chat-color changes are refunded.** When the colour can't be applied — the CSS write fails *or* the current CSS is unavailable — the spend is fully refunded (balance restored, `lifetime_spent` reversed) and the `chat_color` item is rolled back to its previous value (or deactivated if there was none). The command returns a clear "your Z has been refunded — try again" message.

### Added

- **`EconomyDatabase.refund` and `EconomyDatabase.deactivate_vanity_item`** — internal helpers backing the refund/rollback path. `refund` reverses a prior spend (credits the balance and decrements `lifetime_spent`, clamped at 0, logging a `refund` transaction) rather than counting as new earnings.

[0.10.2]: https://github.com/grobertson/kryten-economy/releases/tag/v0.10.2

## [0.10.1] - 2026-06-21

### Fixed

- **Chat-color purchases silently failed on channels with no custom CSS, and the buyer was charged with no refund.** The CSS apply step refused to write whenever the channel's current CSS read back empty, logging `Skipping chat-color CSS apply … current channel CSS is empty/unavailable (refusing to overwrite)`. But an empty read is the *normal* state for a channel with no hand-maintained CSS (and every read layer collapses missing keys / NATS errors to `""`, so "empty" and "unavailable" were indistinguishable). The guard therefore made the feature permanently no-op on such channels while still debiting the buyer. Empty CSS is now treated as a writable channel — `merge_vanity_css` on empty input emits only the auto-managed block, so it clobbers nothing — and the colour applies. The hand-maintained-CSS safety is preserved differently: a genuine robot/NATS outage now surfaces when the CSS *write* fails (not from an empty read).
- **Failed chat-color changes are now refunded.** If the colour is charged but can't be pushed to the channel (robot/NATS outage during the write), the spend is fully refunded (balance restored and `lifetime_spent` reversed) and the `chat_color` vanity item is rolled back to its previous value (or deactivated if there was none), so the buyer is never billed for a change that didn't take effect. The command returns a clear "your Z has been refunded — try again" message.

### Added

- **`EconomyDatabase.refund` and `EconomyDatabase.deactivate_vanity_item`** — internal helpers backing the refund/rollback path above. `refund` reverses a prior spend (credits the balance and decrements `lifetime_spent`, logging a `refund` transaction) rather than counting as new earnings.

[0.10.1]: https://github.com/grobertson/kryten-economy/releases/tag/v0.10.1

## [0.10.0] - 2026-06-19

### Added

- **Purchased chat colors are now applied to the channel CSS automatically.** When a user buys/updates a `chat_color` vanity item (via PM or the web dashboard), the economy reads the channel's current CyTube CSS, rebuilds an auto-managed block of `.chat-msg-<user> { color: … }` rules from the database, and pushes it back through Kryten-Robot. The managed block is delimited by sentinel markers so hand-maintained CSS (layout, bot colors) is preserved, and existing `/* ZCoin purchased vanity colors */` rules are absorbed into the block on first apply (no duplicates). Original username casing is harvested from the current CSS so case-sensitive CyTube classes keep matching. Configurable under `vanity_shop.chat_color` (`apply_css`, `css_selector_template`, `css_block_begin`/`css_block_end`, `css_legacy_marker`, `protected_users`).
- **Pre-existing chat colors are preserved and imported on upgrade.** Colors that previously lived only in the channel CSS (added by hand, never recorded in the database) are no longer lost when the managed block is rebuilt: on apply they are carried over and, when `import_existing_colors` is enabled (default), written into the owning account so they become editable in the portal. A new `vanity.resync_colors` command lets an operator trigger this import (and a CSS rewrite) on demand instead of waiting for the next purchase. Both paths are idempotent and skip protected users.
- **"Don't touch" protection list.** `vanity_shop.chat_color.protected_users` lists usernames the automation must never write, modify, or remove (bot accounts and manually-handled colors); the economy bot account is always protected. As a safety guard, an empty/unavailable CSS read is never written back, so the channel's hand-maintained CSS can't be clobbered.
- **`vanity.shoutout` command** — New NATS request-reply command so the API gateway and web dashboard can purchase a shoutout (debits with rank discount, enforces the per-user cooldown and max length, and delivers `📢 <user>: <message>` to public chat). Mirrors the existing `buy shoutout` PM command.

[0.10.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.10.0

## [0.9.2] - 2026-06-18

### Fixed

- **Spectacle games crashed on databases created before v0.9.0.** `gambling_stats` gained `total_races` / `total_trivias` / `total_blackjacks` columns in v0.9.0, but `CREATE TABLE IF NOT EXISTS` cannot add columns to an existing table, so resolving any race, trivia, or blackjack on an upgraded database raised `sqlite3.OperationalError: table gambling_stats has no column named total_blackjacks`. A startup migration now adds the missing columns. This was especially visible in Blackjack: `stand`, `double`, a busting `hit`, a natural blackjack, and the inactivity auto-stand all run through the failing stats write, so a hand could never be completed and timed-out hands produced no output (the game also leaked because cleanup ran after the failing write). The migration restores the full hit/stand/double/resolve/timeout flow.

[0.9.2]: https://github.com/grobertson/kryten-economy/releases/tag/v0.9.2

## [0.9.1] - 2026-06-18

### Fixed

- **`help` now lists the new spectacle games.** The PM `help` output gained a "🎲 Spectacle Games" section covering Race (`race`, `race <amt> <color>`, `race odds`, `race stats`, plus the `!race` chat shortcut), Trivia (`trivia <wager>`, answering A/B/C/D in chat, `trivia stats`), and Blackjack (`blackjack`/`bj <wager>`, `hit`/`stand`/`double`, `blackjack stats`). Each game only appears when it is enabled in config, so the v0.9.0 games are now discoverable instead of being undocumented.

[0.9.1]: https://github.com/grobertson/kryten-economy/releases/tag/v0.9.1

## [0.9.0] - 2026-06-18

### Added

- **Race Betting** (spectacle game) — Weighted race simulation with pari-mutuel (pool) and fixed-odds modes, live in-race betting, racer traits, random mid-race events, and a progress-bar display. Commentary is provided by a new `RaceNarrator` supporting **static / LLM / hybrid** modes: in LLM/hybrid mode a themed commentary set is generated once per race (cached per channel, bound to the race instance) and falls back to the built-in narrative pools on any failure.
- **Trivia Gamble** (spectacle game) — Multi-user wagered Q&A backed by a new async Open Trivia DB client (`TriviaClient`) with session-token handling and a local cache. Difficulty-scaled payouts, chat-answer grading, and a min-account-age gate on both start and join.
- **Blackjack Lite** (PM-only solo game) — Hit/stand/double, dealer hits soft 17, natural pays 3:2, and inactivity auto-stand. Enforces its own `cooldown_seconds` and `daily_limit`.
- **`SpectacleManager`** — Ensures only one spectacle game (heist, race, trivia) runs per channel at a time, with a shared post-game cooldown to prevent chat flooding.
- **`gambling_common.py`** — Shared, single-source pre-wager account validation and daily game-count tracking, now reused by the existing `GamblingEngine` and all three new engines.
- New database schema for race results/bets and trivia/blackjack stats, plus `total_races` / `total_trivias` / `total_blackjacks` gambling-stat columns.

### Changed

- **DRY remediation** — All gambling engines share `validate_gamble_account()` and the daily-count helpers; race payout is computed once as the single source of truth for crediting, PMs, and the public winners line; date helpers from `utils.py` are reused; race balance values are hoisted to named constants.

[0.9.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.9.0

## [0.8.15] - 2026-06-18

### Changed

- **Movie search & queueing moved to the web queue.** The `search`, `queue`, and `playnext` PM commands are now disabled by default and instead point users at the kryten-webqueue instance (`https://queue.dropsugar.co/`). The `help` text Media section links to the same URL. This is controlled by two new `mediacms` config fields: `web_queue_redirect` (default `true`) and `web_queue_url` (default `https://queue.dropsugar.co/`). Set `web_queue_redirect: false` to restore the legacy in-PM search/queue flow. The underlying spend/queue engine and the `forcenow` command are unchanged.

[0.8.15]: https://github.com/grobertson/kryten-economy/releases/tag/v0.8.15

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
