# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.1] - 2026-09-26

**Behavioural change — read before upgrading:** gambling wagers are now recorded in the
transaction ledger and count toward `lifetime_spent`. Historical wagers are **not** backfilled,
so the ledger remains incomplete before the deployment date of this release.

### Fixed

- **Wagers no longer move currency without leaving a ledger entry.** `atomic_debit` (used by
  every gambling, blackjack, trivia, and race wager) debited `balance` but wrote no `transactions`
  row and never touched `lifetime_spent`. The consequence was that `SUM(transactions.amount)`
  overstated currency in existence by roughly 6.8 billion against a float of ~918 million, and
  every wager was invisible in a user's `!history`. Recorded in
  `docs/KNOWN-ISSUES-wager-ledger-gap.md`.
  - `atomic_debit` is now a complete accounting operation on **both** backends: the balance
    decrement, the `lifetime_spent` increment, and the `transactions` insert all happen in a
    single transaction, so a wager can never move the balance without a ledger row, nor the
    reverse.
  - The invariant `balance == lifetime_earned - lifetime_spent` now holds for every account, and
    `SUM(accounts.balance) == SUM(transactions.amount)` per channel. Both were broken for the
    lifetime of the feature and were never asserted by any test.
  - Added a `postgres`-marked test module plus a shared `pg_store` fixture, so the invariant is
    checked against the asyncpg store as well as SQLite.

### Changed

- **Wagers now count as spent.** `lifetime_spent` is incremented by a wager, which is what the
  `lifetime_spent` achievement and the account pruner (`lifetime_spent = 0` means "never spent")
  have always implied they meant. Refunds decrement it again.
- **Wagers carry a specific `type` in the ledger** — `wager_spin`, `wager_flip`,
  `wager_challenge`, `wager_heist`, `wager_blackjack`, `wager_blackjack_double`, `wager_trivia`,
  `wager_race` — instead of leaving no row at all. Any report that groups by `type` will see
  these new values; `wager` is the default when a caller omits one.
- **Refunded wagers are no longer logged as `gamble_win`.** Challenge declines/expiries and
  heist cancellations previously credited the balance as a win, which inflated apparent
  gambling winnings and counted never-won money as `lifetime_earned`. They now use `refund()`,
  so `lifetime_earned` means "actually earned". On a heist push only the returned portion is
  refunded, leaving the fee counted as spent.

**Not changed:** historical wager volume. It cannot be reconstructed from `gambling_stats`
(counts and net results only, no per-wager amounts or timestamps), so no backfill was written.
For reports spanning the boundary, filter on `transactions.id`.

## [0.16.0] - 2026-09-26

**High-stakes changes in this release — read before upgrading:**

1. **New PostgreSQL backend.** `database.backend: "postgres"` runs the economy on
   PostgreSQL 16 via asyncpg. `sqlite` remains the **default and is still what production
   runs**; this release does not move any live data.
2. **Config-schema addition.** The `database` block gains `backend` and a nested `postgres`
   sub-block (`host`, `port`, `user`, `dbname`, `password_env`, `dsn_env`, `dsn`,
   `pool_min_size`, `pool_max_size`). Every field has a default, so existing `config.yaml`
   files load unchanged and keep working on SQLite. Selecting the backend requires a restart;
   a config reload will not switch it.
3. **Transactional credit/debit.** `credit`, `debit`, and `refund` now apply the balance change
   and write the ledger row inside a single transaction, so a balance can never move without a
   matching transaction row or vice versa. `debit` is a conditional `UPDATE ... WHERE
   balance >= $` and therefore safe under concurrency: the row lock serialises simultaneous
   spends, and a debit that cannot be covered updates zero rows instead of going negative.
   This is a behaviour change on the SQLite backend too, which previously relied on
   `INSERT OR IGNORE` + a separate balance write.
4. **One-time data migration.** `python -m kryten_economy.migrate_sqlite_to_pg` copies an
   existing `economy.db` into PostgreSQL and verifies it. It is a separate, operator-run step
   — upgrading the package does **not** migrate anything. See `docs/postgres-cutover.md`.

**Cutover and rollback.** Follow `docs/postgres-cutover.md`. Rollback is
`database.backend: "sqlite"` plus a restart; the SQLite file is left untouched and is the
rollback path for one full release. Note that rolling back discards any currency earned or
spent on PostgreSQL after the cutover — `pg_dump` first if that window had real activity.

### Added

- **Sprint 12 Sortie 5 — test wiring, cutover runbook, and release.**
  - `docs/postgres-cutover.md`: the full cutover procedure (create role/database, apply the
    schema, stop, migrate, verify independently, flip the backend, smoke test, roll back),
    including the `pg_hba.conf` per-database gotcha, the `pg_dump`-before-rollback warning,
    and a rehearsal procedure that uses a scratch database.
  - A `postgres` pytest marker plus shared `pg_dsn` / `pg_pool` fixtures in
    `tests/conftest.py`. PostgreSQL tests skip cleanly with a clear reason when no DSN is
    configured, so a developer machine without a database still gets a green suite.
    `KRYTEN_ECONOMY_TEST_DSN` is the preferred variable; the Sorties 3–4
    `KRYTEN_ECONOMY_PG_DSN` is still honoured.
  - `.github/workflows/ci.yml`: a lint job, a test job with a real `postgres:16-alpine`
    service running the PostgreSQL-marked tests, and a no-PostgreSQL job that proves those
    tests skip rather than error.
  - **The ETL now refuses to guess its target.** If no `database.postgres.dsn_env`/`dsn` is
    configured and `database.backend` is not `postgres`, it exits 2 with an explanatory
    message rather than assembling a default `localhost` DSN. Every `PostgresConfig` field
    has a default, so the previous behaviour could point a currency migration at a database
    nobody had nominated.
  - **Connection failures now exit 2, not 1.** The runbook maps 1 to "verification drift — do
    not start the service" and 2 to "usage or configuration error". A refused connection, a
    DNS failure, or an authentication error is a configuration problem, so reporting it as
    drift would have told an operator their currency was inconsistent when the real fault
    was a bad DSN. The password is never echoed.

- **Sprint 12 Sortie 4 — SQLite → PostgreSQL data-migration ETL.**
  `kryten_economy/migrate_sqlite_to_pg.py` is a standalone, re-runnable operator tool that
  copies the legacy `economy.db` into PostgreSQL and verifies the result. It is never
  imported by the running service; it is an entry point invoked as
  `python -m kryten_economy.migrate_sqlite_to_pg`.
  - **Non-destructive by construction.** The source is opened through the SQLite
    `file:...?mode=ro` URI, so the tool cannot write to production data even if a bug
    tries to. Verified by byte-comparing the source file across a full run.
  - **Idempotent.** Every insert is an `ON CONFLICT DO UPDATE` keyed on each table's
    primary key, so a full re-run converges instead of duplicating. Verified by running
    the migration twice against real data and asserting row counts and balances are
    unchanged.
  - **Resumable.** Each table is copied in committed batches (`--batch-size`, default
    1000), so an interrupted run leaves the last batch unapplied and a re-run finishes.
    Verified by migrating a deliberately truncated source, then re-running to a
    fully-verified state.
  - **Verifiable.** `--verify-only` (or the automatic post-run check) compares per-table
    row counts, total circulation, and an order-independent per-account balance checksum,
    exiting non-zero on any drift. Verified to detect a single-unit balance change and to
    be repaired by a subsequent re-run. The checksum sorts in Python because SQLite
    (`BINARY`) and PostgreSQL (locale) collations order identically-matching rows
    differently, which would otherwise produce false failures.
  - **Refuses to guess.** An unknown source table is a hard error rather than a silent
    skip, and a target column that is `NOT NULL` with no default but missing from the
    source aborts before copying with a message naming the mismatch, instead of failing
    mid-way on a driver error.
  - **Schema-introspecting.** Columns, primary keys, and types are read from the live
    SQLite and PostgreSQL catalogs rather than hard-coded, so a schema change does not
    silently mis-copy. Merge semantics are upsert-only (source wins for rows it has);
    stale extra target rows are reported by verification rather than silently truncated.
  - The target DSN is resolved exactly as the service resolves it (env-var indirection or
    `database.postgres` config), so no credential is accepted as an argument or logged.
    `--dry-run` reports the plan and row counts without writing.

- **Sprint 12 Sortie 3 — PostgreSQL backend (Alembic schema + asyncpg store).**
  `database.backend: postgres` now runs the economy on PostgreSQL; `sqlite` remains the
  default and the only backend approved for production until the migration is cut over.
  - **Alembic is the single schema authority** (decided for this sprint). There is no
    hand-rolled `sql/` directory and no custom `schema_version` table —
    `alembic_version` tracks applied revisions. Revision `0001` creates 22 tables and
    42 indexes. Apply with `uv run alembic upgrade head`; the DSN is read from
    `KRYTEN_ECONOMY_ALEMBIC_URL` or the service's own `database.postgres` config, so no
    secret is stored in the repository.
  - `EconomyStore` protocol (`kryten_economy/db/protocol.py`) defines the persistence
    surface that both backends implement, and `EconomyDatabasePg`
    (`kryten_economy/db/database_pg.py`) is a full asyncpg implementation of all 132
    public `EconomyDatabase` methods. mypy checks the PostgreSQL store clean.
  - **Currency-integrity semantics (high-stakes).** `credit`/`debit`/`refund` now update
    the account row and write the ledger row inside a single transaction. `debit` uses a
    conditional `UPDATE ... WHERE balance >= $n RETURNING balance`, so the balance check
    and the write are one atomic statement: concurrent debits serialise on the row lock
    and can never overdraw an account or lose an update. A refused debit writes nothing.
  - **Value-boundary layer** (`kryten_economy/db/boundary.py`) keeps the store
    interchangeable with SQLite at the Python-type level. PostgreSQL storage is native
    (`timestamptz`, `boolean`, `date`, `NUMERIC` aggregates), but rows cross the store
    edge as ISO-8601 timestamp strings, `0`/`1` integer flags, and `YYYY-MM-DD` dates,
    because callers such as `pm_handler` call `datetime.fromisoformat()` on account rows
    and `Decimal` from `SUM()` is not JSON-serialisable.

### Fixed

- **Vanity-item username casing is no longer clobbered by a differently-cased purchase.**
  `set_vanity_item` now stores the canonical casing held on the `accounts` table instead
  of whatever casing the caller used. Previously a later lowercase purchase overwrote the
  stored casing, which would break the case-sensitive CyTube CSS selector
  `.chat-msg-<User>`. This affected SQLite and PostgreSQL identically and is fixed in both.
- **`asyncpg` is now type-checked rather than skipped.** The `asyncpg` import previously
  raised `import-untyped` under mypy (the package ships no `py.typed` marker), so the
  PostgreSQL layer was effectively unchecked. `asyncpg-stubs` is now a dev dependency,
  which surfaced and fixed an inaccurate annotation: `_ensure_account` and `_log_tx`
  declared `asyncpg.Connection`, but every caller passes a `PoolConnectionProxy` from
  `pool.acquire()`. Both helpers are now typed against the union that actually describes
  the call sites. `mypy kryten_economy/db` and the ETL are now clean with no suppressions.

### Changed

- **`test_raw_sqlite_access_is_confined_to_database_module` now also exempts
  `migrate_sqlite_to_pg.py`.** The Sortie 4 ETL is the one offline module whose purpose is
  to read the legacy SQLite file, and it is never imported by the running service. The
  exemption is keyed on that single filename, so any new module touching `sqlite3` or
  `.execute(` still fails the check; this was confirmed by temporarily adding an offending
  module and observing the guard fail.

- **Sprint 12 Sortie 2 — Encapsulated SQLite access at the data boundary.** `CommandHandler`
  no longer opens private database connections or executes raw SQLite. Account search, user
  transaction pagination, and channel-wide recent transactions now use typed public
  `EconomyDatabase` methods that return plain dictionaries with unchanged command response
  shapes. Added public read-only accessors for active multiplier events, rank tier count, and
  normalized ignored users so command handling no longer relies on private state or `SLF001`
  suppressions. No configuration, NATS command/event, or persistence behavior changes.

- **Sprint 12 Sortie 1 — PostgreSQL configuration and connection plumbing.** Added
  `PostgresConfig`/`DatabaseConfig.backend`, DSN resolution with env-var indirection, an
  `asyncpg` pool factory, and application-owned pool lifecycle (opened at startup, closed
  on normal and partial-startup shutdown). `SQLite` remains the default.

## [0.15.4] - 2026-08-30

### Fixed

- **Pay-to-play queue locking is no longer enforced by economy.**
  The `spending.queue_preview` and `spending.queue` commands no longer check
  `spending.blackout_windows` and never return the `blackout_active` error code.
  Event/pre-fire locking for the web queue is owned entirely by kryten-webqueue
  (its `active_schedule` + pre-fire locks), so the economy-side blackout check was
  a vestigial second lock: it kept the web queue closed past the intended window
  and made the queue price-preview path fail (HTTP 500 via kryten-api-gate) whenever
  the economy service was stopped. The unused `_is_blackout_active` helper was removed.
  - The chat `!queue` flow (`pm_handler`) still honors `spending.blackout_windows`;
    that surface is unchanged.
  - No config change required. `spending.blackout_windows` remains a valid key
    (still consumed by the chat flow) and can be left as-is or cleared.

## [0.15.3] - 2026-08-14

### Fixed

- **Queue refunds now restore cooldown and daily limit.**
  When a queue attempt fails (e.g., trying to queue deleted media), the user is no longer
  penalized with a cooldown or a reduction in their daily queue limit. The refund operation
  now decrements the daily queue counter and excludes refunded spends from cooldown checks.
  - `EconomyDatabase.get_last_queue_time()` now joins with `queue_spend_requests` to
    exclude refunded transactions from cooldown calculation.
  - New `EconomyDatabase.decrement_daily_queues_used()` method to roll back the daily
    queue counter when refunding.
  - `CommandHandler._handle_spending_queue_refund()` now calls
    `decrement_daily_queues_used()` to restore the user's daily queue allowance.

## [0.15.2] - 2026-08-04

### Added

- **Sprint 10 — Inflation Governor: Float-Tied Pricing.**
  All spend-sink prices now self-regulate by tying them to the total coin float.
  When the float grows above the `anchor_float`, spend-sink prices rise proportionally;
  when it shrinks back, prices soften — creating a closed-loop regulator requiring no
  manual intervention.
  - New `InflationConfig` Pydantic model (`enabled`, `anchor_float`, `min_multiplier`,
    `max_multiplier`, `update_interval_seconds`). Default `enabled: false` — fully opt-in.
  - New `FloatPriceScaler` component (`kryten_economy/float_price_scaler.py`): holds the
    live multiplier, refreshes periodically from the DB, exposes `scale(base_cost) → int`.
  - `SpendingEngine` extended with `get_inflated_price`, `get_effective_price_tier`,
    `get_interrupt_play_next_price`, `get_force_play_now_price`, `get_vanity_item_price`.
    `price_scaler` is an optional constructor parameter for backward compatibility.
  - `PmHandler._cmd_shop` annotates prices with `(base: N Z, ×X.XX)` when inflation is
    active and multiplier differs meaningfully from 1.0.
  - `PmHandler._start_queue_confirm` shows inflation-adjusted cost in the queue
    confirmation prompt.
  - New admin PM command `inflation` — shows current multiplier, anchor, live float, and
    sample spend-sink prices at the current inflation rate.
  - New NATS command `stats.inflation` — returns `enabled`, `multiplier`, `anchor_float`,
    `current_float`, `min_multiplier`, `max_multiplier`.
  - `economy_inflation_multiplier{channel=...}` Prometheus gauge emitted by the metrics
    server (always 1.0 when governor is disabled).
  - Economy snapshots now include an `inflation_multiplier` column. A safe `ALTER TABLE`
    migration runs on startup for existing databases.
  - `EconomyDatabase.write_snapshot` updated to accept and persist `inflation_multiplier`.
  - `EconomyApp` wires scalers: constructed after DB init, started after NATS connect,
    stopped on shutdown, hot-reloaded on `reload` command. `price_scaler_for(channel)`
    helper exposed for command and PM handler use.
  - `config.example.yaml` updated with a fully-documented `inflation:` section.

- **Sprint 11 — Account Pruner CLI.**
  Standalone offline tool (`kryten-economy-prune`) for identifying and removing ghost
  accounts — users who received the welcome wallet but never engaged — to keep the DB
  and float lean.
  - New CLI `kryten_economy/prune_cli.py` registered as `kryten-economy-prune` script.
  - Dry-run by default; `--execute` required to commit deletions.
  - Safety rules enforced unconditionally: never deletes economy-banned accounts, accounts
    with `lifetime_spent > 0`, or accounts with any non-empty vanity column.
  - Interactive confirmation prompt in execute mode (bypassable with `--yes`).
  - Writes a timestamped audit CSV for every execute run.
  - Filters: `--inactive-days`, `--balance-min`, `--balance-max`, `--max-lifetime-earned`.
  - New `EconomyDatabase.find_purgeable_accounts` method applies all safety filters.
  - New `EconomyDatabase.delete_account_and_cascade` atomically removes an account and all
    child rows from `daily_activity`, `transactions`, `tip_history`, `vanity_items`.
  - `EconomyDatabase` logger parameter is now optional (defaults to `economy.database`),
    enabling CLI usage without a running service.

### Changed

- `EconomyDatabase.__init__` logger parameter is now optional (`logger: logging.Logger | None = None`).
  Backward-compatible — all existing callers that pass a logger continue to work.

[0.15.2]: https://github.com/grobertson/kryten-economy/releases/tag/v0.15.2

### Changed

- **Secret scanning gate added.** `.gitleaks.toml` (default ruleset), a `gitleaks` pre-commit
  hook, and a GitHub Actions CI workflow (`.github/workflows/gitleaks.yml`) have been added
  so secrets are caught before they reach the repository. An allowlist entry covers the
  MediaCMS API token that is intentionally checked in to `config.example.yaml`.
- **Linting enforced in pre-commit.** `ruff` and `black` (line-length 100, matching the
  ecosystem standard) are now run as pre-commit hooks. `config.json` (live secrets) has been
  added to `.gitignore`.
- Bumped minimum `kryten-py` dependency to `>=0.17.3`.

[0.14.2]: https://github.com/grobertson/kryten-economy/releases/tag/v0.14.2

## [0.14.1] - 2026-07-25

### Fixed

- **Heist now participates in the shared spectacle cooldown.** Previously, heist ran its
  own private cooldown (`GamblingEngine._heist_cooldowns`) that was completely separate from
  `SpectacleManager`, meaning a heist and a race could start concurrently and each observed
  its own independent cooldown. Heist now goes through `SpectacleManager.try_acquire()` /
  `release()` exactly like race and trivia, so all three games share a single mutex and a
  single cooldown window. The private `_heist_cooldowns` dict in `GamblingEngine` is now
  dormant (never populated); `get_heist_cooldown_remaining()` always returns 0 and will be
  removed in a future major release.

## [0.14.0] - 2026-07-24

### Added
- **Shadow-mute filtering in chat and PM.** Messages from CyTube shadow-muted users
  (`event.shadow = True`) are now silently discarded in both the `chatmsg` and `pm`
  handlers — they no longer trigger earning, game actions, or PM command dispatch.
  Requires `kryten-py>=0.17.2`, which also propagates `meta.shadow` for PM events.

### Changed

- **Minimum Python is now 3.12** (was 3.11). This unblocks PEP 701 f-string syntax already present in the codebase and aligns `requires-python`, tool `target-version`, and trove classifiers.
- **Tooling aligned to the ecosystem standard.** Formatting is now black/ruff at `line-length = 100` (was ruff-only at 120, with no `[tool.black]` config); added a `[tool.black]` section and `ignore = ["E501"]` under `[tool.ruff.lint]`. The codebase was reformatted accordingly.
- **Eight racers per race.** The grid grew from 6 cars to **8** (added Brown 🟤 and White ⚪) across all built-in odds profiles, with re-tuned win chances/speeds (each profile's chances still sum to 1.0).

### Fixed

- **Lint clean-up.** Removed dead assignments and unused locals flagged by ruff (`F841`) and renamed ambiguous loop variables (`E741`); no behavior change.
- **Winner missing from the race finish announcement.** The chat finish line (and the web commentary winner call) now always names the winning car, even when commentary is LLM-authored or a custom/misconfigured template omits the `{racer}` placeholder — previously such a template could leave the winner's name blank. The announcement falls back to a deterministic `«🏁 <emoji> <Colour> wins the race!»` headline whenever the resolved line doesn't mention the winner.

[0.14.1]: https://github.com/grobertson/kryten-economy/releases/tag/v0.14.1
[0.14.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.14.0

## [0.13.0] - 2026-06-22

### Added

- **Precomputed race timeline (smooth web playback).** At betting close the *entire* race is now simulated up front into a position timeline (one row of per-racer percentages every `frame_interval_seconds`, default 0.3s) plus timed commentary and the winner. `race.state`'s racing frame carries this timeline (and the server-clock `elapsed`), so the web race view animates the whole race smoothly client-side and re-syncs to the server clock instead of lurching between coarse polls. New `RaceConfig` knobs: `target_duration_seconds` (race length, default 32s), `frame_interval_seconds` (timeline resolution), `closeness` (0–1, how tightly the pack finishes).
- **Punny driver names.** Each car is assigned a Car Talk-style pun driver name per race (e.g. *Pikup Andropov*, *Manuel Transmission*) from a built-in pool, surfaced on the web race view and in the winner call. Configurable via `gambling.race.racer_names` (`enabled`, `extra_names`).
- **Two more racers.** The grid grew from 4 cars to **6** (added Purple and Orange) across all odds profiles, with re-tuned win chances/speeds.
- **Driver-aware web commentary track.** The timeline includes timed commentary (start, lead changes naming the driver, a close-finish flourish, and the winner call) for a live feed on the web view, independent of the terse chat beats.

### Changed

- **Races are now scripted for drama, not emergent physics.** The winner is drawn weighted by each car's win chance (so displayed odds are exactly meaningful), and the field is shaped by trait-flavoured pace curves and a `closeness` control so also-rans finish near the leader — races feel close instead of blowing out. The scheduler plays back the precomputed timeline (advancing positions to the wall-clock moment and resolving at the end) rather than stepping per-tick physics; nothing is posted to chat per tick (unchanged from 0.11.1).

[0.13.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.13.0

## [0.12.0] - 2026-06-22

### Added

- **`race.state` command — a live web race-view feed.** A new read-only request-reply command returns a JSON snapshot of the current race for a channel: `{"active": bool, "frame": {...}|None}`. The frame carries everything a browser needs to animate the race — phase (betting/racing/finished), every racer's position/progress/percent/odds/emoji/trait, the betting countdown, a per-colour bet summary (pool + bettor counts), and, once the race resolves, the winner and top payouts. The live frame is served from the in-memory race state (always current, no persistence), and the final result frame is retained for a short window (`FINISHED_FRAME_TTL_SECONDS`, 20s) after the race ends so the web view can show the outcome before going idle. This is the economy half of moving the race play-by-play off public chat (see 0.11.1) and onto a visual web view; it has no side effects and is safe to poll.

[0.12.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.12.0

## [0.11.1] - 2026-06-22

### Fixed

- **Race play-by-play no longer floods public chat.** Every simulation tick used to post a progress-bar block (and each random event) to the channel — for a ~20s race that's a burst of a dozen-plus messages, which buried other chatter and tripped the bot's antiflood (swallowed PMs, occasional disconnect/reconnect). The per-tick play-by-play is now **silent in chat**; the tick still advances the simulation and detects the finish. The channel sees only the high-signal beats.
- **Race commentary placeholders like `{name}` are now resolved.** In `llm`/`hybrid` commentary mode the model occasionally emits an undocumented placeholder (`{name}`, `{color}`, `{winner}`, …) instead of the documented `{racer}`/`{emoji}`. The old formatter raised `KeyError` and fell back to the **raw** template, so literal `{name}` showed up in chat. Commentary formatting now maps common aliases onto the racer/emoji values and renders any genuinely unknown placeholder as empty — never leaving literal braces in chat. Static and custom commentary lines go through the same tolerant path.

### Changed

- **The four channel race beats are terser.** To save chat real-estate the betting announcement is now a two-line headline (all racers + odds inline, plus the bet instruction) instead of a multi-line block, and the finish announcement is a headline finish line plus a single combined summary (winners + pool + bettor count) rather than several separate lines. The remaining beats — race declared/betting open, bets placed, race start, race end & payouts — are unchanged in intent.

[0.11.1]: https://github.com/grobertson/kryten-economy/releases/tag/v0.11.1

## [0.11.0] - 2026-06-21

### Added

- **Chat-color readability guard.** Chat colors render as light text on a near-black chat background, where two different things make a color hard to read: very dark colors (maroon, navy) and harsh near-monochromatic reds (pure red reads badly despite decent lightness). A new `kryten_economy.contrast` module scores a candidate color by **combining APCA perceptual lightness contrast with a chroma penalty** for red-dominant colors, against the configurable chat background. `vanity.set_color` now **refuses** colors below `min_contrast_lc` (no charge) and a new read-only **`vanity.check_color`** command returns the verdict (`lc`, combined `score`, `level` of ok/warn/reject, and a user-facing message) so the dashboard can preview/validate before purchase. Configurable under `vanity_shop.chat_color`: `enforce_contrast`, `contrast_bg` (default `#111111`), `min_contrast_lc` (default 30 — blocks all dark reds, pure red, red-orange, navy, pure blue), `warn_contrast_lc` (default 40 — flags borderline colors). WCAG ratio is intentionally not used (it passes pure red on black).

### Removed

- **Curated chat-color palette and the `buy color` PM command.** Chat color is now set only as an arbitrary 6-digit hex via the web dashboard / `vanity.set_color`, which is where the readability guard lives. The fixed `ChatColorConfig.palette` and `ChatColorPaletteEntry` are gone, and `buy color <name>` is no longer a PM command. (Pydantic ignores the now-unused `palette:` key, so existing config files load unchanged.)

[0.11.0]: https://github.com/grobertson/kryten-economy/releases/tag/v0.11.0

## [0.10.3] - 2026-06-21

### Fixed

- **Chat-color changes silently reverted while still charging the user (showstopper).** Confirmed on live data: a user could end up with two active `vanity_items` rows for the same name differing only in case — a stale lowercased row (e.g. `teenagedraculerx` → old green) left behind by the 0.10.2 migration, plus the canonical-cased row (`TeenageDraculerX`) that new purchases update. The 0.10.2 recasing migration used `UPDATE OR IGNORE`, which silently *skipped* such collisions instead of merging them, and `set_vanity_item` upserted on the case-sensitive `UNIQUE(username, ...)` index, so a differently-cased purchase created/kept a second row. The CSS rebuild lowercases both selectors into one, the stale row wins the merge, the rebuilt CSS equals the current CSS → no `setChannelCSS` push (color never changes) and a no-op isn't refunded (user still charged). Two-part fix: (1) `set_vanity_item` now upserts **case-insensitively** — it updates the existing row in place (refreshing value *and* canonical casing) and only inserts when the user has no row yet, so a second case-variant row can never be created; (2) the migration now **dedupes** each `(lower(username), channel, item_type)` collision — keeping the most recently purchased row — *before* recasing survivors from the `accounts` table (portable correlated-subquery form; idempotent). Existing affected rows heal automatically on startup.

[0.10.3]: https://github.com/grobertson/kryten-economy/releases/tag/v0.10.3

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
