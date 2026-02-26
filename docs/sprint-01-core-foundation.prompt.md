# Sprint 1 — Core Foundation

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 1 of 9  
> **Goal:** Deployable service that connects to NATS, tracks user presence, awards base Z-Coins for being connected, stores balances in SQLite, and responds to `balance` and `help` via PM.  
> **Depends on:** Nothing (foundation sprint)  
> **Enables:** Sprints 2, 3, 4 (all three can start in parallel after this)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [Project Scaffolding](#2-project-scaffolding)
3. [Configuration System](#3-configuration-system)
4. [Database Module](#4-database-module)
5. [Presence Tracker](#5-presence-tracker)
6. [PM Handler](#6-pm-handler)
7. [Request-Reply Command Handler](#7-request-reply-command-handler)
8. [Metrics Server](#8-metrics-server)
9. [Service Orchestrator](#9-service-orchestrator)
10. [Reference Config File](#10-reference-config-file)
11. [Test Specifications](#11-test-specifications)
12. [Acceptance Criteria](#12-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint, the service:

- Connects to NATS via `kryten-py` and subscribes to CyTube events
- Tracks which users are connected via `adduser`/`userleave` events
- Filters out ignored users (other bots) at event ingestion — they never earn, never appear in population counts
- Implements **join debounce** to handle CyTube WebSocket instability — rapid disconnect/reconnect cycles do not create duplicate sessions
- Credits every connected (non-ignored) user **1 Z per minute** via a periodic tick
- Persists all balances and transactions in SQLite
- Responds to `balance` and `help` PM commands
- Exposes a request-reply API on `kryten.economy.command` via `client.subscribe_request_reply()` (`system.ping`, `system.health`, `balance.get`)
- Serves Prometheus metrics on port 28286
- Includes a fully commented `config.example.yaml`
- Includes comprehensive tests for all components

---

## 2. Project Scaffolding

### 2.1 Directory Structure

```
kryten-economy/
├── kryten_economy/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── main.py
│   ├── database.py
│   ├── presence_tracker.py
│   ├── pm_handler.py
│   ├── command_handler.py
│   ├── metrics_server.py
│   └── utils.py
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_database.py
│   ├── test_presence_tracker.py
│   ├── test_pm_handler.py
│   ├── test_command_handler.py
│   └── test_metrics_server.py
├── config.example.yaml
├── pyproject.toml
├── README.md
└── systemd/
    └── kryten-economy.service
```

### 2.2 `pyproject.toml`

Follow the kryten-userstats / kryten-llm convention exactly.

```toml
[project]
name = "kryten-economy"
version = "0.1.0"
description = "Channel engagement currency microservice for CyTube channels"
readme = "README.md"
requires-python = ">=3.11,<4.0.0"
dependencies = [
    "kryten-py>=0.11.5",
    "pyyaml>=6.0,<7.0.0",
    "pydantic>=2.0,<3.0.0",
    "aiohttp>=3.9.0,<4.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = [
    "pytest>=8.0.0,<9.0.0",
    "pytest-asyncio>=0.24.0,<0.25.0",
    "pytest-cov>=4.1.0,<5.0.0",
    "black>=24.0.0,<25.0.0",
    "ruff>=0.4.0,<1.0.0",
    "mypy>=1.10.0,<2.0.0",
]

[project.scripts]
kryten-economy = "kryten_economy.__main__:main"

[tool.hatch.build.targets.wheel]
packages = ["kryten_economy"]

[tool.ruff]
line-length = 120
target-version = "py311"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

### 2.3 `kryten_economy/__init__.py`

```python
"""kryten-economy — Channel engagement currency microservice."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kryten-economy")
except PackageNotFoundError:
    __version__ = "0.0.0"
```

### 2.4 `kryten_economy/__main__.py`

Follow the kryten-userstats pattern: sync `main()` wrapping `asyncio.run(main_async())`.

```python
"""CLI entry point for kryten-economy."""
import argparse
import asyncio
import logging
import signal
import sys

from .main import EconomyApp


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kryten Economy — Channel Currency Service")
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--validate-config", action="store_true",
                        help="Validate config and exit without starting")
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("economy")

    # Config path resolution
    config_path = args.config
    if not config_path:
        from pathlib import Path
        for candidate in [
            "/etc/kryten/kryten-economy/config.yaml",
            "./config.yaml",
        ]:
            if Path(candidate).exists():
                config_path = candidate
                break
    if not config_path:
        logger.error("No config file found. Use --config or place config.yaml in CWD.")
        sys.exit(1)

    if args.validate_config:
        from .config import load_config
        try:
            load_config(config_path)
            logger.info("Config is valid.")
        except Exception as e:
            logger.error("Config validation failed: %s", e)
            sys.exit(1)
        return

    app = EconomyApp(config_path)

    # Signal handling (Unix only; Windows uses KeyboardInterrupt)
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.stop()))

    try:
        await app.start()
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()


def main() -> None:
    """Sync entry point for pyproject.toml [project.scripts]."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
```

---

## 3. Configuration System

### 3.1 File: `kryten_economy/config.py`

**Pattern:** Extend `KrytenConfig` (from `kryten-py`) with economy-specific Pydantic sub-models. Since `KrytenConfig.from_json()` exists but we need YAML, implement a standalone `load_config(path) → EconomyConfig` using `yaml.safe_load()`.

**Key design rules:**

- Every field has a sensible default so that a minimal config (just `nats` + `channels`) works.
- Sub-models are used for logical grouping (mirrors the YAML structure).
- Only fields relevant to Sprint 1 need full implementation. Later sprints extend the same config class by adding new sub-models. Define the full class now but mark later-sprint sections with `# Sprint N` comments.

### 3.2 Pydantic Model Hierarchy

```
EconomyConfig (extends KrytenConfig)
├── database: DatabaseConfig
├── currency: CurrencyConfig
├── bot: BotConfig
├── ignored_users: list[str]
├── onboarding: OnboardingConfig
├── presence: PresenceConfig
│   ├── hourly_milestones: dict[int, int]          # Sprint 2
│   └── night_watch: NightWatchConfig               # Sprint 2
├── streaks: StreaksConfig                           # Sprint 2
├── chat_triggers: ChatTriggersConfig                # Sprint 3
├── content_triggers: ContentTriggersConfig          # Sprint 3
├── social_triggers: SocialTriggersConfig            # Sprint 3
├── achievements: list[AchievementConfig]            # Sprint 6
├── daily_competitions: list[CompetitionConfig]      # Sprint 7
├── multipliers: MultipliersConfig                   # Sprint 7
├── rain: RainConfig                                 # Sprint 2
├── spending: SpendingConfig                         # Sprint 5
├── mediacms: MediaCMSConfig                         # Sprint 5
├── vanity_shop: VanityShopConfig                    # Sprint 5
├── ranks: RanksConfig                               # Sprint 6
├── cytube_promotion: CytubePromotionConfig          # Sprint 6
├── gambling: GamblingConfig                         # Sprint 4
├── tipping: TippingConfig                           # Sprint 5
├── balance_maintenance: BalanceMaintenanceConfig     # Sprint 2
├── retention: RetentionConfig                       # Sprint 2
├── announcements: AnnouncementsConfig               # Sprint 9
├── admin: AdminConfig                               # Sprint 8
├── metrics: MetricsConfig (override)
└── digest: DigestConfig                             # Sprint 8
```

### 3.3 Sprint 1 Sub-Models (Implement Fully)

These are the sub-models that Sprint 1 code actually reads at runtime. All others should be defined as Pydantic models with defaults but are not consumed yet.

```python
class DatabaseConfig(BaseModel):
    path: str = "economy.db"

class CurrencyConfig(BaseModel):
    name: str = "Z-Coin"
    symbol: str = "Z"
    plural: str = "Z-Coins"

class BotConfig(BaseModel):
    username: str = "ZCoinBot"

class OnboardingConfig(BaseModel):
    welcome_wallet: int = 100
    welcome_message: str = (
        "Welcome! You've got {amount} {currency}. "
        "Stick around and you'll earn more. Try 'help' to see what you can do."
    )
    min_account_age_minutes: int = 0
    min_messages_to_earn: int = 0

class NightWatchConfig(BaseModel):
    enabled: bool = False
    start_hour: int = 2     # UTC hour the night-watch window opens
    end_hour: int = 6       # UTC hour the night-watch window closes
    bonus_per_minute: int = 1

class PresenceConfig(BaseModel):
    base_rate_per_minute: int = 1
    active_bonus_per_minute: int = 0
    afk_threshold_minutes: int = 5
    join_debounce_minutes: int = 5
    greeting_absence_minutes: int = 30
    # Sprint 2 fields defined here with defaults but not consumed in Sprint 1
    hourly_milestones: dict[int, int] = {1: 10, 3: 30, 6: 75, 12: 200, 24: 1000}
    night_watch: NightWatchConfig = NightWatchConfig()

# NOTE: If KrytenConfig already has a metrics: MetricsConfig field,
# extend it rather than creating a separate model. If MetricsConfig
# uses field names like 'metrics_port' or 'metrics_path', match those.
# The model below assumes MetricsConfig does NOT exist in the parent.
class EconomyMetricsConfig(BaseModel):
    enabled: bool = True
    port: int = 28286
    path: str = "/metrics"
```

### 3.4 `load_config()` Function

```python
import yaml
from pathlib import Path

def load_config(config_path: str) -> EconomyConfig:
    """Load and validate YAML config file into EconomyConfig."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Config file must contain a YAML mapping at the top level.")

    return EconomyConfig(**raw)
```

**Why a custom `load_config()`?** `KrytenConfig` provides `from_json()` and `from_yaml()` loaders, but our `EconomyConfig` extends `KrytenConfig` with many additional sub-models that the base loaders don't know about. We need `yaml.safe_load()` → `EconomyConfig(**raw)` to get full Pydantic validation of all economy-specific fields.

**Important:** The `from_json()` / `from_yaml()` loaders perform `${ENV_VAR}` substitution before parsing. Our custom `load_config()` must replicate this. Add an `_expand_env_vars()` pre-processor that walks the parsed dict and replaces `${VAR}` / `${VAR:-default}` patterns with `os.environ` lookups before passing to Pydantic:

```python
import os
import re

def _expand_env_vars(obj):
    """Recursively expand ${VAR} and ${VAR:-default} in string values."""
    if isinstance(obj, str):
        return re.sub(
            r'\$\{([^}:]+)(?::-(.*?))?\}',
            lambda m: os.environ.get(m.group(1), m.group(2) or ''),
            obj,
        )
    elif isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    return obj
```

Call `raw = _expand_env_vars(raw)` before `return EconomyConfig(**raw)`.

### 3.5 KrytenClient Integration

`EconomyConfig` extends `KrytenConfig`. `KrytenClient` accepts a `KrytenConfig` (or dict). Pass the config directly:

```python
self.client = KrytenClient(self.config)
```

`KrytenClient.__init__` will read `.nats`, `.channels`, `.service`, etc. from the Pydantic model. `KrytenClient` accepts either a `dict` (converted internally via `KrytenConfig(**config)`) or a `KrytenConfig` instance (stored directly). Since `EconomyConfig` extends `KrytenConfig`, passing the config directly works — `isinstance(config, KrytenConfig)` is `True`.

---

## 4. Database Module

### 4.1 File: `kryten_economy/database.py`

**Pattern:** Mirror `kryten-userstats` `StatsDatabase` exactly:

- `__init__(db_path, logger)` — stores path, does NOT open connection
- `_get_connection() → sqlite3.Connection` — creates a **new connection each call**, WAL mode, 30s busy timeout, `sqlite3.Row` factory
- `initialize()` — async, calls `_create_tables` via `run_in_executor`
- Every public method is `async` and wraps a synchronous `_sync()` inner function via `run_in_executor(None, _sync)`

### 4.2 Sprint 1 Tables

Only create these three tables in Sprint 1. Later sprints add their own via new migration methods.

```sql
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    balance INTEGER DEFAULT 0,
    lifetime_earned INTEGER DEFAULT 0,
    lifetime_spent INTEGER DEFAULT 0,
    lifetime_gambled_in INTEGER DEFAULT 0,
    lifetime_gambled_out INTEGER DEFAULT 0,
    rank_name TEXT DEFAULT 'Extra',
    cytube_level INTEGER DEFAULT 1,
    chat_color TEXT,
    custom_greeting TEXT,
    custom_title TEXT,
    channel_gif_url TEXT,
    channel_gif_approved BOOLEAN DEFAULT 0,
    personal_currency_name TEXT,
    welcome_wallet_claimed BOOLEAN DEFAULT 0,
    economy_banned BOOLEAN DEFAULT 0,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP,
    UNIQUE(username, channel)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount INTEGER NOT NULL,
    type TEXT NOT NULL,
    reason TEXT,
    trigger_id TEXT,
    related_user TEXT,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_activity (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    date TEXT NOT NULL,
    minutes_present INTEGER DEFAULT 0,
    minutes_active INTEGER DEFAULT 0,
    messages_sent INTEGER DEFAULT 0,
    long_messages INTEGER DEFAULT 0,
    gifs_posted INTEGER DEFAULT 0,
    unique_emotes_used INTEGER DEFAULT 0,
    kudos_given INTEGER DEFAULT 0,
    kudos_received INTEGER DEFAULT 0,
    laughs_received INTEGER DEFAULT 0,
    bot_interactions INTEGER DEFAULT 0,
    z_earned INTEGER DEFAULT 0,
    z_spent INTEGER DEFAULT 0,
    z_gambled_in INTEGER DEFAULT 0,
    z_gambled_out INTEGER DEFAULT 0,
    first_message_claimed BOOLEAN DEFAULT 0,
    free_spin_used BOOLEAN DEFAULT 0,
    queues_used INTEGER DEFAULT 0,
    UNIQUE(username, channel, date)
);
```

Create indexes:

```sql
-- NOTE: No index needed on accounts(username, channel) — the UNIQUE constraint already creates one.
CREATE INDEX IF NOT EXISTS idx_transactions_username_channel ON transactions(username, channel);
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(type);
CREATE INDEX IF NOT EXISTS idx_daily_activity_date ON daily_activity(date);
```

### 4.3 Required Public Methods (Sprint 1)

Each method follows the `run_in_executor` pattern. Here is the full public API for this sprint:

```python
class EconomyDatabase:
    def __init__(self, db_path: str, logger: logging.Logger): ...
    def _get_connection(self) -> sqlite3.Connection: ...

    async def initialize(self) -> None:
        """Create tables and indexes. Idempotent."""

    # ── Account Operations ──────────────────────────────────

    async def get_or_create_account(self, username: str, channel: str) -> dict:
        """Return account row as dict. Creates with defaults if not exists.
        Uses INSERT ... ON CONFLICT DO NOTHING then SELECT."""

    async def get_account(self, username: str, channel: str) -> dict | None:
        """Return account row as dict, or None if not exists."""

    async def get_balance(self, username: str, channel: str) -> int:
        """Shorthand: return balance integer, 0 if account doesn't exist."""

    async def update_last_seen(self, username: str, channel: str) -> None:
        """Set last_seen to CURRENT_TIMESTAMP. Called on adduser and periodic tick."""

    async def update_last_active(self, username: str, channel: str) -> None:
        """Set last_active to CURRENT_TIMESTAMP. Called on chat message."""

    # ── Balance Operations ──────────────────────────────────

    async def credit(
        self,
        username: str,
        channel: str,
        amount: int,
        tx_type: str,
        reason: str | None = None,
        trigger_id: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """Atomically credit Z to account and log transaction.
        Updates balance, lifetime_earned.
        Returns new balance.
        Creates account if not exists."""

    async def debit(
        self,
        username: str,
        channel: str,
        amount: int,
        tx_type: str,
        reason: str | None = None,
        trigger_id: str | None = None,
        related_user: str | None = None,
        metadata: str | None = None,
    ) -> int | None:
        """Atomically debit Z from account and log transaction.
        FAILS (returns None) if balance < amount.
        Updates balance, lifetime_spent.
        Returns new balance on success, None on insufficient funds."""

    # ── Daily Activity ──────────────────────────────────────

    async def increment_daily_minutes_present(
        self, username: str, channel: str, date: str, minutes: int = 1
    ) -> None:
        """Add minutes to daily_activity.minutes_present.
        Uses INSERT ... ON CONFLICT DO UPDATE."""

    async def increment_daily_z_earned(
        self, username: str, channel: str, date: str, amount: int
    ) -> None:
        """Add to daily_activity.z_earned."""

    # ── Population Queries ──────────────────────────────────

    async def get_total_circulation(self, channel: str) -> int:
        """SUM(balance) for all accounts in channel."""

    async def get_account_count(self, channel: str) -> int:
        """COUNT of accounts in channel."""
```

### 4.4 Critical Implementation Details

**Atomic credit/debit:** Both `credit()` and `debit()` must update the `accounts` table AND insert into `transactions` within the **same SQLite connection** and **same `conn.commit()`**. Never credit without logging.

**Debit guard:** `debit()` must check `balance >= amount` before decrementing. Use `UPDATE accounts SET balance = balance - ? WHERE username = ? AND channel = ? AND balance >= ?` — if `cursor.rowcount == 0`, the debit failed (insufficient funds). Do NOT insert a transaction row on failure.

**UPSERT for daily_activity:** Use `INSERT INTO daily_activity (...) VALUES (...) ON CONFLICT(username, channel, date) DO UPDATE SET minutes_present = minutes_present + excluded.minutes_present`.

---

## 5. Presence Tracker

### 5.1 File: `kryten_economy/presence_tracker.py`

This is the heart of Sprint 1. It translates CyTube join/leave events into earning.

### 5.2 Responsibilities

1. Maintain in-memory set of currently connected users per channel (excluding ignored users)
2. Track session start times for each user
3. **Join debounce**: determine if a join event is a "genuine arrival" vs. a WebSocket bounce
4. Periodic tick (every 60 seconds): credit presence Z to all connected users
5. Update `daily_activity.minutes_present` on each tick
6. Update `accounts.last_seen` on each tick

### 5.3 Data Structures

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class UserSession:
    """Tracks a single user's current connection state."""
    username: str
    channel: str
    connected_at: datetime          # When this session started (or was resumed)
    last_tick_at: datetime          # Last time presence Z was credited
    is_afk: bool = False
    cumulative_minutes_today: int = 0  # Running total for the calendar day

class PresenceTracker:
    def __init__(self, config: PresenceConfig, ignored_users: list[str],
                 database: EconomyDatabase, logger: logging.Logger):
        # Active sessions: {(username_lower, channel): UserSession}
        self._sessions: dict[tuple[str, str], UserSession] = {}
        # Departure timestamps for debounce: {(username_lower, channel): datetime}
        self._last_departure: dict[tuple[str, str], datetime] = {}
        # Normalized ignored-user set for O(1) lookup
        self._ignored_users: set[str] = {u.lower() for u in ignored_users}
        # Periodic tick task handle
        self._tick_task: asyncio.Task | None = None
```

### 5.4 Ignored Users Filter

**Enforcement point:** The very first check in `handle_user_join()` and `handle_user_leave()`.

```python
def _is_ignored(self, username: str) -> bool:
    return username.lower() in self._ignored_users
```

If `_is_ignored(username)` returns `True`:
- `handle_user_join`: return immediately, do not create session, do not count in population
- `handle_user_leave`: return immediately
- The user will never appear in `self._sessions`
- Population count (`get_connected_count()`) only counts `self._sessions`

### 5.5 Join Debounce

CyTube WebSocket connections are unstable. A user may disconnect and reconnect within seconds. The debounce logic determines if a join is a **genuine arrival** (user was actually gone) or a **bounce** (user's connection blipped).

```python
async def is_genuine_arrival(self, username: str, channel: str) -> bool:
    """Return True if this join represents a user who was genuinely absent.
    
    Checks:
    1. In-memory last_departure dict (handles WS bouncing during runtime)
    2. DB accounts.last_seen (handles service restart — user was present,
       service restarted, user still present, adduser fires on reconnect)
    
    Returns True if:
    - User has no departure record (truly new, or gone a very long time)
    - Time since last departure >= join_debounce_minutes
    """
    key = (username.lower(), channel)
    threshold = timedelta(minutes=self._config.join_debounce_minutes)

    # Check in-memory first (fast path)
    departure_time = self._last_departure.get(key)
    if departure_time is not None:
        if datetime.now(timezone.utc) - departure_time < threshold:
            return False  # bounce
        return True  # genuinely gone

    # Fallback: check DB last_seen (for service restarts)
    account = await self._db.get_account(username, channel)
    if account and account.get("last_seen"):
        last_seen = _parse_timestamp(account["last_seen"])
        if datetime.now(timezone.utc) - last_seen < threshold:
            return False  # likely a bounce around service restart
    
    return True  # no record, treat as genuine
```

### 5.6 Session Handling

#### On `adduser` event:

```
1. If _is_ignored(username) → return
2. If session already exists for (username, channel) → return immediately
   (handles duplicate adduser without prior userleave — do NOT update connected_at)
3. genuine = await is_genuine_arrival(username, channel)
4. Create UserSession, add to self._sessions
5. If genuine:
   a. await db.get_or_create_account(username, channel)
   b. await db.update_last_seen(username, channel)
   c. Remove from self._last_departure (if present)
   d. Mark session as "genuine arrival" (for Sprint 2 welcome wallet, Sprint 9 greeting)
6. If NOT genuine (bounce):
   a. Preserve session continuity — the session's connected_at should use the
      ORIGINAL connection time (from the last_departure record or current),
      not this re-join timestamp
   b. Log at DEBUG level: "Debounced join for {username} (absent {seconds}s)"
```

#### On `userleave` event:

```
1. If _is_ignored(username) → return
2. If no active session → return (already gone)
3. Record departure: self._last_departure[(username.lower(), channel)] = now
4. Do NOT remove the session immediately — keep it for debounce_minutes
   to allow session preservation if they reconnect quickly
5. Schedule a deferred session cleanup:
   loop = asyncio.get_running_loop()
   loop.call_later(
       debounce_minutes * 60,
       lambda u=username, c=channel: asyncio.ensure_future(
           self._finalize_departure(u, c)
       ),
   )
   NOTE: _finalize_departure is async, so we must wrap it with
   asyncio.ensure_future() inside the call_later lambda. The lambda
   captures username/channel via default args to avoid late-binding.
```

#### `_finalize_departure(username, channel)`:

```
1. Check if user has reconnected (session exists and connected_at > departure time)
   If so → do nothing, session was preserved
2. Otherwise → remove session from self._sessions
3. await db.update_last_seen(username, channel)
```

### 5.7 Periodic Tick (Presence Earning)

Start an `asyncio.Task` that runs every 60 seconds:

```python
async def _presence_tick(self) -> None:
    """Award presence Z to all connected users. Runs every 60 seconds."""
    while self._running:
        await asyncio.sleep(60)
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        
        for key, session in list(self._sessions.items()):
            username, channel = session.username, session.channel
            
            # Credit presence Z
            amount = self._config.base_rate_per_minute
            # (Sprint 2+ adds active_bonus, night_watch, multipliers here)
            
            if amount > 0:
                await self._db.credit(
                    username, channel, amount,
                    tx_type="earn",
                    reason="Presence",
                    trigger_id="presence.base",
                )
                
                # Update daily activity
                await self._db.increment_daily_minutes_present(
                    username, channel, today, minutes=1
                )
                await self._db.increment_daily_z_earned(
                    username, channel, today, amount
                )
            
            # Update last_seen
            await self._db.update_last_seen(username, channel)
            session.last_tick_at = now
            session.cumulative_minutes_today += 1
            
            # Update metrics
            self._metrics_z_earned += amount
```

### 5.8 Public API

```python
class PresenceTracker:
    async def handle_user_join(self, username: str, channel: str) -> bool:
        """Process adduser event. Returns True if genuine arrival."""

    async def handle_user_leave(self, username: str, channel: str) -> None:
        """Process userleave event."""

    def get_connected_users(self, channel: str) -> set[str]:
        """Return set of currently connected usernames for channel."""

    def get_connected_count(self, channel: str) -> int:
        """Return count of connected users (excludes ignored)."""

    def is_connected(self, username: str, channel: str) -> bool:
        """Check if a specific user is currently connected."""

    async def start(self) -> None:
        """Start the periodic tick task."""

    async def stop(self) -> None:
        """Cancel the tick task, finalize all sessions."""
```

---

## 6. PM Handler

### 6.1 File: `kryten_economy/pm_handler.py`

Subscribes to `pm` events via `@client.on("pm")`. Parses incoming PM text as commands. Dispatches to command implementations. Sends responses back via `client.send_pm()`.

### 6.2 Event Flow

```
@client.on("pm") handler fires
  → pm_handler.handle_pm(event) called
  → Ignore if sender is in ignored_users
  → Ignore if sender is the bot itself (config.bot.username)
  → Parse first word as command
  → Look up in command map
  → If found: execute handler, get response text
  → If not found: respond with "Unknown command. Try 'help'."
  → Send response via client.send_pm(channel, username, response)
```

### 6.3 PM Response Publishing

The response is sent via kryten-py's `send_pm()` wrapper:

```python
async def _send_pm(self, channel: str, target_username: str, message: str) -> str:
    """Send a PM response via kryten-py. Returns correlation ID."""
    return await self._client.send_pm(channel, target_username, message)
```

### 6.4 Sprint 1 Commands

#### `help`

Response text (intentionally playful and non-exhaustive):

```
🎬 Economy Bot — Your Pocket Studio
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
balance     Check your {currency} balance
help        You're looking at it!

Stick around. Earn {currency}. Discover the rest. 🍿
```

Use `{currency}` placeholder → resolved from `config.currency.name` at runtime.

#### `balance` / `bal`

```python
async def _cmd_balance(self, username: str, channel: str, args: list[str]) -> str:
    account = await self._db.get_or_create_account(username, channel)
    balance = account["balance"]
    rank = account["rank_name"]
    symbol = self._config.currency.symbol
    currency_name = account.get("personal_currency_name") or self._config.currency.name
    
    return (
        f"💰 Balance: {balance:,} {symbol} ({currency_name})\n"
        f"⭐ Rank: {rank}"
    )
```

### 6.5 Command Map Pattern

```python
class PmHandler:
    def __init__(self, config, database, client, presence_tracker, logger):
        self._command_map: dict[str, Callable] = {
            "help": self._cmd_help,
            "balance": self._cmd_balance,
            "bal": self._cmd_balance,
        }
        # Sprint 2+ adds: rewards, like, spin, flip, etc.

    async def handle_pm(self, event) -> None:
        username = event.username  # or however the PM event exposes the sender
        channel = event.channel
        
        if self._is_ignored(username):
            return
        if username.lower() == self._config.bot.username.lower():
            return  # don't respond to self
        
        text = event.message.strip()  # ChatMessageEvent.message field
        if not text:
            return
        
        parts = text.split(None, 1)  # Split into command + args
        command = parts[0].lower()
        args = parts[1].split() if len(parts) > 1 else []
        
        handler = self._command_map.get(command)
        if handler:
            response = await handler(username, channel, args)
        else:
            response = "❓ Unknown command. Try 'help'."
        
        await self._send_pm(channel, username, response)
```

### 6.6 PM Event Format

**Critical prerequisite:** The PM event is a `ChatMessageEvent` model (same model as `chatmsg`). Verify the exact field names:

- `event.username` — sender of the PM
- `event.message` — PM text content
- `event.channel` — channel context
- `event.domain` — CyTube domain
- `event.rank` — sender's CyTube rank

The PM event arrives via `@client.on("pm")` (kryten-py handles the NATS subscription and deserialization internally).

---

## 7. Request-Reply Command Handler

### 7.1 File: `kryten_economy/command_handler.py`

**Pattern:** Follow `ModeratorCommandHandler` from kryten-moderator. Use `client.subscribe_request_reply()` for the unified command subject.

### 7.2 Structure

```python
class CommandHandler:
    def __init__(self, app: "EconomyApp", client: KrytenClient, logger: logging.Logger):
        self._app = app
        self._client = client
        self._logger = logger

    async def connect(self) -> None:
        """Subscribe to request-reply on kryten.economy.command."""
        await self._client.subscribe_request_reply(
            "kryten.economy.command",
            self._handle_command,
        )

    async def _handle_command(self, request: dict) -> dict:
        command = request.get("command", "")
        handler = self._HANDLER_MAP.get(command)
        if not handler:
            return {
                "service": "economy",
                "command": command,
                "success": False,
                "error": f"Unknown command: {command}",
            }
        try:
            result = await handler(self, request)
            return {
                "service": "economy",
                "command": command,
                "success": True,
                "data": result,
            }
        except Exception as e:
            self._logger.exception("Command handler error for %s", command)
            return {
                "service": "economy",
                "command": command,
                "success": False,
                "error": str(e),
            }
```

### 7.3 Sprint 1 Commands

```python
    _HANDLER_MAP = {
        "system.ping": _handle_ping,
        "system.health": _handle_health,
        "balance.get": _handle_balance_get,
    }
```

#### `system.ping`

```python
async def _handle_ping(self, request: dict) -> dict:
    return {"pong": True, "version": __version__}
```

#### `system.health`

```python
async def _handle_health(self, request: dict) -> dict:
    return {
        "status": "healthy",
        "database": "connected" if self._app.db else "disconnected",
        "active_sessions": sum(
            self._app.presence_tracker.get_connected_count(ch.channel)
            for ch in self._app.config.channels
        ),
        "uptime_seconds": self._app.uptime_seconds,
    }
```

#### `balance.get`

```python
async def _handle_balance_get(self, request: dict) -> dict:
    username = request.get("username")
    channel = request.get("channel")
    if not username or not channel:
        raise ValueError("username and channel are required")
    account = await self._app.db.get_account(username, channel)
    if not account:
        return {"found": False}
    return {
        "found": True,
        "username": account["username"],
        "channel": account["channel"],
        "balance": account["balance"],
        "lifetime_earned": account["lifetime_earned"],
        "rank_name": account["rank_name"],
    }
```

---

## 8. Metrics Server

### 8.1 File: `kryten_economy/metrics_server.py`

**Pattern:** Subclass `BaseMetricsServer` from kryten-py.

```python
from kryten import BaseMetricsServer

class EconomyMetricsServer(BaseMetricsServer):
    def __init__(self, app: "EconomyApp", port: int = 28286):
        super().__init__(
            service_name="economy",
            port=port,
            client=app.client,
            logger=app.logger,
        )
        self._app = app
```

### 8.2 Sprint 1 Custom Metrics

```python
async def _collect_custom_metrics(self) -> list[str]:
    lines = []
    
    # Active sessions gauge
    active = sum(
        self._app.presence_tracker.get_connected_count(ch.channel)
        for ch in self._app.config.channels
    )
    lines.append(f'economy_active_users {active}')
    
    # Total circulation gauge
    for ch in self._app.config.channels:
        circ = await self._app.db.get_total_circulation(ch.channel)
        lines.append(
            f'economy_total_circulation{{channel="{ch.channel}"}} {circ}'
        )
    
    # Total accounts gauge
    for ch in self._app.config.channels:
        count = await self._app.db.get_account_count(ch.channel)
        lines.append(
            f'economy_total_accounts{{channel="{ch.channel}"}} {count}'
        )
    
    # Counters (maintained in-memory on the app, incremented by other components)
    lines.append(f'economy_events_processed_total {self._app.events_processed}')
    lines.append(f'economy_commands_processed_total {self._app.commands_processed}')
    lines.append(f'economy_z_earned_total {self._app.z_earned_total}')
    
    return lines

async def _get_health_details(self) -> dict:
    return {
        "database": "connected" if self._app.db else "disconnected",
        "channels_configured": len(self._app.config.channels),
        "active_sessions": sum(
            self._app.presence_tracker.get_connected_count(ch.channel)
            for ch in self._app.config.channels
        ),
    }
```

---

## 9. Service Orchestrator

### 9.1 File: `kryten_economy/main.py`

**Pattern:** Follow the canonical kryten-py microservice pattern (see kryten-moderator for reference).

### 9.2 Class: `EconomyApp`

```python
class EconomyApp:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.logger = logging.getLogger("economy")
        
        # Components (initialized in start())
        self.config: EconomyConfig | None = None
        self.client: KrytenClient | None = None
        self.db: EconomyDatabase | None = None
        self.presence_tracker: PresenceTracker | None = None
        self.pm_handler: PmHandler | None = None
        self.command_handler: CommandHandler | None = None
        self.metrics_server: EconomyMetricsServer | None = None
        
        # State
        self._running = False
        self._start_time: float | None = None
        
        # Counters (for metrics)
        self.events_processed: int = 0
        self.commands_processed: int = 0
        self.z_earned_total: int = 0
    
    @property
    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time
```

### 9.3 `start()` Method — Canonical Sequence

```python
async def start(self) -> None:
    self.logger.info("Starting kryten-economy...")
    self._start_time = time.time()
    
    # 1. Load and validate config
    self.config = load_config(str(self.config_path))
    self.logger.info("Config loaded: %d channel(s)", len(self.config.channels))
    
    # 2. Initialize database
    self.db = EconomyDatabase(self.config.database.path, self.logger)
    await self.db.initialize()
    self.logger.info("Database initialized: %s", self.config.database.path)
    
    # 3. Initialize domain components
    self.presence_tracker = PresenceTracker(
        config=self.config.presence,
        ignored_users=self.config.ignored_users,
        database=self.db,
        logger=self.logger,
    )
    self.pm_handler = PmHandler(
        config=self.config,
        database=self.db,
        client=None,  # set after client creation
        presence_tracker=self.presence_tracker,
        logger=self.logger,
    )
    
    # 4. Create KrytenClient
    self.client = KrytenClient(self.config)
    self.pm_handler._client = self.client  # wire up
    
    # 5. Register event handlers
    @self.client.on("adduser")
    async def handle_join(event):
        self.events_processed += 1
        await self.presence_tracker.handle_user_join(event.username, event.channel)
    
    @self.client.on("userleave")
    async def handle_leave(event):
        self.events_processed += 1
        await self.presence_tracker.handle_user_leave(event.username, event.channel)
    
    @self.client.on("pm")
    async def handle_pm(event):
        self.events_processed += 1
        await self.pm_handler.handle_pm(event)
    
    # 6. Connect to NATS
    await self.client.connect()
    self.logger.info("Connected to NATS")
    
    # 7. Subscribe to robot startup for re-initialization
    await self.client.subscribe(
        "kryten.lifecycle.robot.startup",
        self._handle_robot_startup,
    )
    
    # 8. Start metrics server
    metrics_port = self.config.metrics.port if self.config.metrics else 28286
    self.metrics_server = EconomyMetricsServer(self, port=metrics_port)
    await self.metrics_server.start()
    self.logger.info("Metrics server started on port %d", metrics_port)
    
    # 9. Start command handler
    self.command_handler = CommandHandler(self, self.client, self.logger)
    await self.command_handler.connect()
    self.logger.info("Command handler ready on kryten.economy.command")
    
    # 10. Start presence tracker tick
    await self.presence_tracker.start()
    
    # 11. Mark running
    self._running = True
    self.logger.info("kryten-economy started successfully (v%s)", __version__)
    
    # 12. Block on client event loop
    await self.client.run()
```

### 9.4 `stop()` Method

```python
async def stop(self) -> None:
    if not self._running:
        return
    self.logger.info("Shutting down kryten-economy...")
    self._running = False
    
    # Reverse order
    if self.presence_tracker:
        await self.presence_tracker.stop()
    if self.metrics_server:
        await self.metrics_server.stop()
    if self.client:
        await self.client.stop()
    
    self.logger.info("kryten-economy stopped.")
```

### 9.5 Robot Startup Handler

When kryten-robot restarts, it re-sends `adduser` events for all currently connected users. This is normal and handled by the debounce logic.

```python
async def _handle_robot_startup(self, msg) -> None:
    """Handle kryten-robot restart — sessions will be re-populated via adduser events."""
    self.logger.info("Robot startup detected — awaiting fresh adduser events")
    # Optionally: clear all sessions and let adduser events rebuild.
    # The debounce logic handles duplicates gracefully either way.
```

---

## 10. Reference Config File

### 10.1 File: `config.example.yaml`

Generate a complete `config.example.yaml` with extensive inline comments. It should contain **every** field from the full schema (all sprints), with only the Sprint 1 fields being operationally required. Later-sprint sections should be present with their defaults and comments like `# (Sprint N — no effect until sprint N is deployed)`.

The config from Section 4 of the parent plan (`kryten-economy-plan.md`) is the authoritative reference. Reproduce it in full with these additions:

- A header block explaining the file
- Comments on every section boundary
- Comments on every field explaining its purpose, type, and default
- Notes on which sprint activates each section

This file should be self-documenting enough that a channel operator can configure the economy without reading any other documentation.

---

## 11. Test Specifications

### 11.1 File: `tests/conftest.py`

Shared fixtures:

```python
import logging
import pytest
import asyncio
import tempfile
import os

@pytest.fixture
def tmp_db_path(tmp_path):
    """Temporary SQLite database path."""
    return str(tmp_path / "test_economy.db")

@pytest.fixture
def sample_config_dict():
    """Minimal valid config dict for testing."""
    return {
        "nats": {"servers": ["nats://localhost:4222"]},
        "channels": [{"domain": "cytu.be", "channel": "testchannel"}],
        "service": {"name": "economy"},
        "database": {"path": ":memory:"},
        "currency": {"name": "TestCoin", "symbol": "T", "plural": "TestCoins"},
        "bot": {"username": "TestBot"},
        "ignored_users": ["BotA", "BotB"],
        "presence": {
            "base_rate_per_minute": 1,
            "join_debounce_minutes": 5,
            "greeting_absence_minutes": 30,
        },
    }

@pytest.fixture
async def database(tmp_db_path):
    """Initialized EconomyDatabase instance."""
    from kryten_economy.database import EconomyDatabase
    db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
    await db.initialize()
    return db
```

### 11.2 File: `tests/test_config.py`

| Test | Description |
|---|---|
| `test_load_minimal_config` | Minimal YAML (nats + channels) loads with all defaults |
| `test_load_full_config` | Full config.example.yaml loads without error |
| `test_missing_nats_fails` | Config without `nats` raises ValidationError |
| `test_missing_channels_fails` | Config without `channels` raises ValidationError |
| `test_defaults_applied` | currency.name defaults to "Z-Coin", presence.base_rate_per_minute defaults to 1, etc. |
| `test_ignored_users_default_empty` | Default ignored_users is empty list |
| `test_custom_values_override` | Custom values in YAML override defaults |
| `test_invalid_yaml_raises` | Non-YAML file raises error |
| `test_nonexistent_file_raises` | Missing file raises FileNotFoundError |

### 11.3 File: `tests/test_database.py`

| Test | Description |
|---|---|
| `test_initialize_creates_tables` | After initialize(), all three tables exist |
| `test_get_or_create_account_new` | First call creates account with defaults |
| `test_get_or_create_account_existing` | Second call returns same account, doesn't duplicate |
| `test_get_account_not_found` | Returns None for non-existent user |
| `test_credit_creates_account` | Credit to non-existent user creates account first |
| `test_credit_updates_balance` | Credit 100, check balance is 100 |
| `test_credit_updates_lifetime_earned` | Credit 100, lifetime_earned is 100 |
| `test_credit_logs_transaction` | After credit, transaction row exists with correct type/amount |
| `test_debit_success` | Credit 100, debit 50, balance is 50 |
| `test_debit_insufficient_funds` | Credit 50, debit 100, returns None, balance unchanged |
| `test_debit_updates_lifetime_spent` | Debit 50, lifetime_spent is 50 |
| `test_debit_logs_transaction` | After debit, transaction row exists with negative amount |
| `test_debit_zero_balance_fails` | Debit from 0-balance returns None |
| `test_multiple_credits_accumulate` | Credit 10 five times, balance is 50 |
| `test_daily_minutes_increment` | Increment minutes_present 5 times, value is 5 |
| `test_daily_z_earned_increment` | Increment z_earned, value accumulates |
| `test_daily_activity_upsert` | Multiple increments on same date update (not duplicate) |
| `test_get_total_circulation` | Three accounts with 100, 200, 300 → circulation is 600 |
| `test_get_account_count` | Three accounts → count is 3 |
| `test_update_last_seen` | After update, last_seen is recent |
| `test_concurrent_credits` | 10 concurrent credit operations produce correct total |

### 11.4 File: `tests/test_presence_tracker.py`

| Test | Description |
|---|---|
| `test_join_adds_session` | After handle_user_join, user is in connected set |
| `test_leave_schedules_removal` | After handle_user_leave, user remains briefly (debounce) then removed |
| `test_ignored_user_join_no_session` | Ignored user's join creates no session |
| `test_ignored_user_case_insensitive` | "BOTA" is ignored when config has "BotA" |
| `test_ignored_user_not_counted_in_population` | Connected count excludes ignored users |
| `test_debounce_bounce_detected` | Join, leave, re-join within debounce window → NOT genuine arrival |
| `test_debounce_genuine_arrival` | Join, leave, wait past debounce window, re-join → genuine arrival |
| `test_debounce_no_prior_departure` | First-ever join → genuine arrival |
| `test_session_preserved_on_bounce` | Leave + re-join within debounce → session.connected_at unchanged |
| `test_presence_tick_credits_z` | After tick, all connected users get base_rate Z credited |
| `test_presence_tick_updates_daily_activity` | After tick, minutes_present incremented |
| `test_presence_tick_skips_ignored` | Ignored users never get presence Z |
| `test_multiple_channels_independent` | Sessions in different channels are independent |
| `test_get_connected_users` | Returns correct set of usernames |
| `test_get_connected_count` | Returns correct count |
| `test_start_stop` | Start begins tick task, stop cancels it |

### 11.5 File: `tests/test_pm_handler.py`

| Test | Description |
|---|---|
| `test_help_command` | PM "help" returns help text containing currency name |
| `test_balance_command_new_user` | "balance" for new user creates account, returns 0 balance |
| `test_balance_command_with_funds` | Credit some Z, "balance" returns correct amount |
| `test_bal_alias` | "bal" works same as "balance" |
| `test_unknown_command` | "foobar" returns unknown command message |
| `test_empty_message_ignored` | Empty PM text is ignored (no response) |
| `test_ignored_user_no_response` | PM from ignored user gets no response |
| `test_self_message_ignored` | PM from bot's own username gets no response |
| `test_command_case_insensitive` | "BALANCE", "Balance", "balance" all work |
| `test_personal_currency_name` | User with personal_currency_name sees it in balance response |

### 11.6 File: `tests/test_command_handler.py`

| Test | Description |
|---|---|
| `test_ping` | `system.ping` returns `{pong: true}` |
| `test_health` | `system.health` returns status, database, active_sessions |
| `test_balance_get_existing` | `balance.get` for existing user returns account data |
| `test_balance_get_not_found` | `balance.get` for non-existent user returns `{found: false}` |
| `test_balance_get_missing_params` | `balance.get` without username/channel returns error |
| `test_unknown_command` | Unknown command returns `{success: false}` |

---

## 12. Acceptance Criteria

All of the following must be true before Sprint 1 is complete:

- [ ] `pyproject.toml` is valid; `pip install -e .` succeeds
- [ ] `kryten-economy --validate-config` exits cleanly with `config.example.yaml`
- [ ] Service starts with `kryten-economy --config config.example.yaml`
- [ ] Service connects to NATS and logs "Connected to NATS"
- [ ] `adduser` events create sessions; `userleave` events schedule removal
- [ ] Ignored users (from config) produce no sessions, no DB rows, no earnings
- [ ] Ignored user filtering is case-insensitive
- [ ] A user who disconnects and reconnects within `join_debounce_minutes` is debounced (session preserved, no duplicate accounting)
- [ ] A user who disconnects and reconnects AFTER `join_debounce_minutes` is treated as a genuine new arrival
- [ ] Presence tick runs every 60 seconds and credits `base_rate_per_minute` Z to all connected (non-ignored) users
- [ ] `balance` PM command returns correct balance
- [ ] `help` PM command returns a playful help message
- [ ] `kryten.economy.command` responds to `system.ping`, `system.health`, `balance.get`
- [ ] Prometheus metrics endpoint on port 28286 returns `economy_active_users`, `economy_total_circulation`, `economy_z_earned_total`
- [ ] All transactions are logged in the `transactions` table with correct type and amount
- [ ] `daily_activity.minutes_present` increments correctly
- [ ] `config.example.yaml` contains ALL fields from the full plan (all 9 sprints) with inline comments
- [ ] All tests pass (`pytest` exits 0)
- [ ] No `ruff` lint errors

---

## Appendix A: Utility Helpers

### File: `kryten_economy/utils.py`

Sprint 1 needs minimal helpers. Define these now to avoid duplication:

```python
from datetime import datetime, timezone

def normalize_channel(channel: str) -> str:
    """Normalize channel name for NATS subject use.
    Follow kryten-py convention (lowercase, strip special chars)."""
    return channel.lower().replace(" ", "_")

def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse SQLite TIMESTAMP string to datetime, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None

def today_str() -> str:
    """Return today's date as YYYY-MM-DD string (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
```

---

## Appendix B: systemd Service File

### File: `systemd/kryten-economy.service`

```ini
[Unit]
Description=Kryten Economy — Channel Currency Service
After=network.target nats-server.service
Wants=nats-server.service

[Service]
Type=simple
User=kryten
Group=kryten
WorkingDirectory=/opt/kryten/kryten-economy
ExecStart=/opt/kryten/kryten-economy/.venv/bin/kryten-economy --config /etc/kryten/kryten-economy/config.yaml
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

*End of Sprint 1 specification. This document is self-contained and sufficient for an AI coding agent to implement the full sprint without referencing other documents.*
