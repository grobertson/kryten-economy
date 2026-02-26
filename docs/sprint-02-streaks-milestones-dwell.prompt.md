# Sprint 2 — Streaks, Milestones & Dwell Incentives

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 2 of 9  
> **Goal:** All time-based earning mechanics that reward sustained presence and return visits.  
> **Depends on:** Sprint 1 (Core Foundation)  
> **Enables:** Sprint 7 (Events & Bounties)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Daily Streak Tracking](#3-daily-streak-tracking)
4. [Hourly Dwell Milestones](#4-hourly-dwell-milestones)
5. [Weekend→Weekday Bridge Bonus](#5-weekendweekday-bridge-bonus)
6. [Night Watch Multiplier](#6-night-watch-multiplier)
7. [Rain Drops](#7-rain-drops)
8. [Welcome Wallet](#8-welcome-wallet)
9. [Welcome-Back Bonus](#9-welcome-back-bonus)
10. [Balance Interest & Decay](#10-balance-interest--decay)
11. [Scheduler Module](#11-scheduler-module)
12. [Presence Tracker Modifications](#12-presence-tracker-modifications)
13. [Config Sections Activated](#13-config-sections-activated)
14. [Test Specifications](#14-test-specifications)
15. [Acceptance Criteria](#15-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint, the service additionally:

- Tracks daily login **streaks** with escalating bonuses (day 2–7), milestone bonuses at 7 and 30 days, and streak reset on missed days
- Awards **hourly dwell milestones** (1h/3h/6h/12h/24h) for cumulative presence within a calendar day
- Awards a **weekend→weekday bridge bonus** (500 Z) when a user is present on both a weekend day and a weekday in the same ISO week
- Applies a **night watch multiplier** (1.5×) to presence earnings during configured off-peak hours
- Distributes **rain drops** — periodic random Z bonuses to all connected (non-ignored) users
- Credits a **welcome wallet** (100 Z) to first-time users on their first genuine arrival
- Credits a **welcome-back bonus** (100 Z) to returning users who have been absent for ≥7 days
- Applies daily **balance interest** (or optional decay) via scheduled task
- Introduces a `scheduler.py` module for all periodic/scheduled tasks

All values remain config-driven.

---

## 2. New Database Tables

Add these tables during database initialization. Sprint 1's `initialize()` method should be extended (or a `migrate()` pattern introduced) to add new tables idempotently.

### 2.1 `streaks` Table

```sql
CREATE TABLE IF NOT EXISTS streaks (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    current_daily_streak INTEGER DEFAULT 0,
    longest_daily_streak INTEGER DEFAULT 0,
    last_streak_date TEXT,          -- YYYY-MM-DD of last qualifying day
    weekend_seen_this_week BOOLEAN DEFAULT 0,
    weekday_seen_this_week BOOLEAN DEFAULT 0,
    bridge_claimed_this_week BOOLEAN DEFAULT 0,
    week_number TEXT,               -- ISO week string (e.g. "2026-W09") for weekly reset
    UNIQUE(username, channel)
);
```

### 2.2 `hourly_milestones` Table

```sql
CREATE TABLE IF NOT EXISTS hourly_milestones (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    date TEXT NOT NULL,             -- YYYY-MM-DD
    hours_1 BOOLEAN DEFAULT 0,
    hours_3 BOOLEAN DEFAULT 0,
    hours_6 BOOLEAN DEFAULT 0,
    hours_12 BOOLEAN DEFAULT 0,
    hours_24 BOOLEAN DEFAULT 0,
    UNIQUE(username, channel, date)
);
```

### 2.3 `trigger_cooldowns` Table

```sql
CREATE TABLE IF NOT EXISTS trigger_cooldowns (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    window_start TIMESTAMP,
    UNIQUE(username, channel, trigger_id)
);
```

> **Note:** `trigger_cooldowns` is defined here but will be primarily used by Sprint 3 (Chat Earning Triggers). Create the table now so it's available.

---

## 3. Daily Streak Tracking

### 3.1 Overview

A user earns a "streak day" when they accumulate ≥ `streaks.daily.min_presence_minutes` (default: 15) in a single calendar day (UTC). On each qualifying day beyond day 1, they receive an escalating bonus. Streaks reset when a day is missed entirely.

### 3.2 Streak State Machine

```
Day N qualifies (≥15 min present):
  ├─ last_streak_date == yesterday → current_daily_streak += 1
  ├─ last_streak_date == today     → already counted (no-op)
  └─ last_streak_date is older/null → current_daily_streak = 1 (reset)
  
After increment:
  ├─ Update last_streak_date = today
  ├─ Update longest_daily_streak = max(current, longest)
  ├─ Award streak bonus if current_daily_streak >= 2 (see reward table)
  ├─ Award milestone bonus if current_daily_streak == 7 or == 30
  └─ PM notification on award
```

### 3.3 Reward Table (Config-Driven)

From `config.streaks.daily.rewards`:

| Streak Day | Default Reward |
|---|---|
| 2 | 10 Z |
| 3 | 20 Z |
| 4 | 30 Z |
| 5 | 50 Z |
| 6 | 75 Z |
| 7 | 100 Z |

Additional milestones (on top of daily reward):

| Milestone | Bonus |
|---|---|
| 7-day streak | 200 Z (`milestone_7_bonus`) |
| 30-day streak | 2,000 Z (`milestone_30_bonus`) |

For streak days > 7, repeat the day-7 reward (100 Z) unless the config defines higher tiers.

### 3.4 Integration Point

Streak evaluation runs as part of the **presence tick** (every 60 seconds). On each tick, after crediting presence Z:

```
1. Check if user's cumulative_minutes_today >= min_presence_minutes
2. If yes, call streak_engine.evaluate_daily_streak(username, channel)
3. evaluate_daily_streak:
   a. Get or create streaks row
   b. If last_streak_date == today → return (already counted)
   c. Determine if this extends the streak or resets
   d. Update DB
   e. Award bonuses, log transactions
   f. Send PM notification for streak bonus
```

### 3.5 Database Methods to Add

```python
async def get_or_create_streak(self, username: str, channel: str) -> dict:
    """Return streak row, creating with defaults if not exists."""

async def update_streak(
    self, username: str, channel: str,
    current_streak: int, longest_streak: int, last_date: str
) -> None:
    """Update streak counters."""

async def update_bridge_fields(
    self, username: str, channel: str,
    weekend_seen: bool | None = None,
    weekday_seen: bool | None = None,
    bridge_claimed: bool | None = None,
    week_number: str | None = None,
) -> None:
    """Update weekend/weekday bridge tracking fields."""
```

---

## 4. Hourly Dwell Milestones

### 4.1 Overview

Users earn bonus Z for cumulative presence within a single calendar day (UTC). Milestones are one-time per day — once earned, they don't reset until the next calendar day.

### 4.2 Milestone Thresholds (Config-Driven)

From `config.presence.hourly_milestones`:

| Cumulative Minutes | Default Reward |
|---|---|
| 60 (1 hour) | 10 Z |
| 180 (3 hours) | 30 Z |
| 360 (6 hours) | 75 Z |
| 720 (12 hours) | 200 Z |
| 1440 (24 hours) | 1,000 Z |

### 4.3 Tracking Logic

On each presence tick, after incrementing `session.cumulative_minutes_today`:

```
1. Get or create hourly_milestones row for (username, channel, today)
2. For each milestone threshold (sorted ascending):
   a. If cumulative_minutes >= threshold AND milestone not yet claimed:
      - Award bonus Z
      - Mark milestone claimed in DB
      - PM notification:
        "⏰ {hours}-hour milestone! +{reward} {currency}. Keep it up!"
      - Log transaction (type="milestone", trigger_id="dwell.{hours}h")
```

### 4.4 Calendar Day Reset

The `cumulative_minutes_today` counter on `UserSession` resets when the date (UTC) changes. On each presence tick:

```python
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
if session._current_date != today:
    session.cumulative_minutes_today = 0
    session._current_date = today
```

> **Note:** `_current_date` must be added to the `UserSession` dataclass (Sprint 1 §5.3):
> `_current_date: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))`
```

### 4.5 Database Methods to Add

```python
async def get_or_create_hourly_milestones(
    self, username: str, channel: str, date: str
) -> dict:
    """Return milestones row for today, creating if needed."""

async def mark_hourly_milestone(
    self, username: str, channel: str, date: str, hours: int
) -> None:
    """Set the hours_N column to 1.
    Column name derived from hours: hours_{hours}."""
```

---

## 5. Weekend→Weekday Bridge Bonus

### 5.1 Overview

If a user is present on at least one **weekend** day (Saturday or Sunday) AND at least one **weekday** (Monday–Friday) within the same ISO week, they receive a one-time bridge bonus (default: 500 Z).

This encourages users to return on off-days, bridging the weekend-to-weekday gap.

### 5.2 Logic

On each qualifying streak day (user has ≥15 min present today):

```
1. Get current ISO week (e.g. "2026-W09")
2. Get or create streaks row
3. If streaks.week_number != current_week:
   - Reset: weekend_seen=0, weekday_seen=0, bridge_claimed=0, week_number=current_week
4. today = weekday? → weekday_seen = 1 : weekend_seen = 1
5. If weekend_seen AND weekday_seen AND NOT bridge_claimed:
   - Award bridge bonus
   - bridge_claimed = 1
   - Log transaction (type="earn", trigger_id="bridge.weekly")
   - PM: "🌉 Weekend-weekday bridge bonus! +{amount} {currency}!"
```

### 5.3 Weekend Announcement

If `config.streaks.weekend_weekday_bridge.announce_on_weekend` is true, and it's Saturday, and the user has `weekday_seen_this_week == 0`:

```
PM on Saturday: "Connect any weekday this week for a {amount} {currency} bridge bonus!"
```

Send this once per week per user (track via a flag or cooldown).

---

## 6. Night Watch Multiplier

### 6.1 Overview

During configured off-peak hours, presence earnings receive a multiplier. This rewards users who leave a tab open overnight — genuine presence during low-traffic hours.

### 6.2 Configuration

```yaml
presence:
  night_watch:
    enabled: true
    hours: [2, 3, 4, 5, 6, 7]   # UTC hours (24h format)
    multiplier: 1.5
```

### 6.3 Logic

In the presence tick, when calculating the Z to credit:

```python
base_amount = config.presence.base_rate_per_minute

# Night watch multiplier
if config.presence.night_watch.enabled:
    current_hour = datetime.now(timezone.utc).hour
    if current_hour in config.presence.night_watch.hours:
        base_amount = int(base_amount * config.presence.night_watch.multiplier)

# (Sprint 7 adds additional multiplier stacking here)
```

### 6.4 Transaction Metadata

When night watch is active, include in the transaction metadata:

```json
{"multiplier": "night_watch", "factor": 1.5}
```

This aids analytics in Sprint 8 (understanding how much Z is minted by night watch).

---

## 7. Rain Drops

### 7.1 Overview

A periodic "rain" event distributes a random small amount of Z to every connected (non-ignored) user. This creates ambient reward moments that reinforce "being here pays off."

### 7.2 Configuration

```yaml
rain:
  enabled: true
  interval_minutes: 45        # Average interval (randomized ±30%)
  min_amount: 5
  max_amount: 25
  pm_notification: true
  message: "☔ Rain drop! You received {amount} {currency} just for being here."
```

### 7.3 Logic

Implemented as a periodic task in `scheduler.py`:

```
1. Calculate next rain time:
   base = interval_minutes
   jitter = random.uniform(-0.3 * base, 0.3 * base)
   wait = (base + jitter) * 60  # seconds
2. Sleep for `wait` seconds
3. Get list of all connected users (from presence_tracker, excluding ignored)
4. If no users connected → skip, schedule next
5. Roll a random amount: random.randint(min_amount, max_amount)
6. For each connected user:
   a. Credit `amount` Z
   b. Log transaction (type="rain", trigger_id="rain.ambient")
   c. If pm_notification: PM the user with the rain message
7. Optionally announce in public chat (Sprint 9 announcer)
8. Schedule next rain
```

### 7.4 Rain and Ignored Users

The connected user list comes from `presence_tracker.get_connected_users(channel)`. Ignored users are already excluded from this set (Sprint 1 guarantee). No additional filtering needed here.

### 7.5 Rain and Rank Bonus (Sprint 6 Forward)

In Sprint 6, certain ranks get a rain bonus multiplier (e.g. Gaffer gets +20%). For now, just credit the flat amount. Leave a comment in the code:

```python
# TODO (Sprint 6): Apply rank-based rain bonus multiplier
rain_amount = base_amount
```

---

## 8. Welcome Wallet

### 8.1 Overview

The first time a new user joins (genuine arrival), they receive a starting balance of `onboarding.welcome_wallet` Z (default: 100). This gives immediate purchasing power and a reason to explore the economy.

### 8.2 Logic

Trigger point: `presence_tracker.handle_user_join()`, after determining the arrival is **genuine** (per debounce).

```
1. If NOT genuine arrival → skip
2. account = await db.get_or_create_account(username, channel)
3. If account.welcome_wallet_claimed → skip (already received)
4. Credit welcome_wallet amount
5. Set welcome_wallet_claimed = 1
6. Log transaction (type="welcome_wallet", trigger_id="onboarding.wallet")
7. PM the welcome message:
   config.onboarding.welcome_message with {amount} and {currency} substituted
```

### 8.3 Idempotency

The `welcome_wallet_claimed` flag on the `accounts` table ensures this is strictly one-time per user per channel, even across service restarts or race conditions. Use a self-guarding UPDATE to eliminate TOCTOU races:

```python
def _sync():
    conn = self._get_connection()
    try:
        # Atomic: UPDATE only if not yet claimed (self-guarding)
        conn.execute(
            "UPDATE accounts SET balance = balance + ?, lifetime_earned = lifetime_earned + ?, "
            "welcome_wallet_claimed = 1 WHERE username=? AND channel=? AND welcome_wallet_claimed = 0",
            (amount, amount, username, channel)
        )
        if conn.total_changes == 0:
            conn.rollback()
            return False  # already claimed (or account doesn't exist)
        conn.execute(
            "INSERT INTO transactions (username, channel, amount, type, trigger_id) "
            "VALUES (?, ?, ?, 'welcome_wallet', 'onboarding.wallet')",
            (username, channel, amount)
        )
        conn.commit()
        return True
    finally:
        conn.close()
```

> **Key:** The `WHERE welcome_wallet_claimed = 0` guard eliminates the SELECT→UPDATE TOCTOU race. If two tasks race, only one UPDATE will match. Check `conn.total_changes` (or `cursor.rowcount`) to detect the no-op.

### 8.4 Database Method to Add

```python
async def claim_welcome_wallet(self, username: str, channel: str, amount: int) -> bool:
    """Atomically credit welcome wallet if not already claimed.
    Returns True if credited, False if already claimed."""
```

---

## 9. Welcome-Back Bonus

### 9.1 Overview

When a user who has been absent for ≥ `retention.welcome_back.days_absent` (default: 7) days returns (genuine arrival), they receive a bonus.

### 9.2 Configuration

```yaml
retention:
  welcome_back:
    enabled: true
    days_absent: 7
    bonus: 100
    message: "Welcome back! Here's {amount} {currency}. You've been missed. 💚"
```

### 9.3 Logic

Trigger point: `presence_tracker.handle_user_join()`, after determining the arrival is **genuine**.

```
1. If NOT genuine arrival → skip
2. If NOT config.retention.welcome_back.enabled → skip
3. account = await db.get_account(username, channel)
4. If no account → skip (new user, handled by welcome wallet instead)
5. Parse account.last_seen → last_seen_dt
6. If last_seen_dt is None → skip
7. days_absent = (now - last_seen_dt).days
8. If days_absent >= config.retention.welcome_back.days_absent:
   a. Credit bonus
   b. Log transaction (type="welcome_back", trigger_id="retention.welcome_back")
   c. PM the welcome-back message with {amount} and {currency} substituted
```

### 9.4 Interaction with Welcome Wallet

A brand-new user (no prior account) gets the **welcome wallet** only. A returning user who has been gone ≥7 days gets the **welcome-back bonus** only. These are mutually exclusive because:
- Welcome wallet fires only if `welcome_wallet_claimed == 0` (new users)
- Welcome-back fires only if account exists with a `last_seen` in the past

---

## 10. Balance Interest & Decay

### 10.1 Overview

A daily scheduled task that either credits interest to all balances (reward for holding) or debits decay from high balances (inflation control). Only one mode is active, controlled by `config.balance_maintenance.mode`.

### 10.2 Configuration

```yaml
balance_maintenance:
  mode: "interest"    # "interest", "decay", or "none"
  
  interest:
    daily_rate: 0.001          # 0.1% per day
    max_daily_interest: 10     # Cap at 10 Z/day per user
    min_balance_to_earn: 100   # Must have at least 100 Z to earn interest
  
  decay:
    enabled: false
    daily_rate: 0.005          # 0.5% per day
    exempt_below: 50000        # Balances below this are exempt
    label: "Vault maintenance fee"
```

### 10.3 Interest Logic (Daily Task)

```
1. Query all accounts where balance >= min_balance_to_earn
2. For each account:
   a. interest = floor(balance * daily_rate)
   b. interest = min(interest, max_daily_interest)
   c. If interest > 0:
      - Credit interest
      - Log transaction (type="interest", trigger_id="maintenance.interest")
```

### 10.4 Decay Logic (Daily Task)

```
1. Query all accounts where balance >= exempt_below
2. For each account:
   a. decay_amount = floor(balance * daily_rate)
   b. If decay_amount > 0:
      - Debit decay_amount (always succeeds — balance is high enough)
      - Log transaction (type="decay", trigger_id="maintenance.decay",
                         reason=config.balance_maintenance.decay.label)
```

### 10.5 Scheduling

Runs once per day at a fixed UTC hour (recommend: 3:00 AM UTC, before the daily digest at 4:00 AM). Implemented via `scheduler.py`.

### 10.6 Database Method to Add

```python
async def get_accounts_with_min_balance(self, channel: str, min_balance: int) -> list[dict]:
    """Return all accounts in channel with balance >= min_balance."""

async def apply_interest_batch(self, channel: str, rate: float, cap: int, min_balance: int) -> int:
    """Apply interest to all qualifying accounts. Returns total interest paid."""

async def apply_decay_batch(self, channel: str, rate: float, exempt_below: int) -> int:
    """Apply decay to all qualifying accounts. Returns total decay collected."""
```

---

## 11. Scheduler Module

### 11.1 File: `kryten_economy/scheduler.py`

Central module for all periodic and scheduled tasks. Uses `asyncio.Task` for each scheduled operation.

### 11.2 Sprint 2 Scheduled Tasks

| Task | Interval / Schedule | Description |
|---|---|---|
| Rain drops | Every ~45 min (±30% jitter) | Random Z to all connected users |
| Balance maintenance | Daily at 03:00 UTC | Interest or decay |
| Weekend bridge reminder | Saturday presence tick | PM bridge-eligible users |

### 11.3 Class Structure

```python
class Scheduler:
    def __init__(self, config: EconomyConfig, database: EconomyDatabase,
                 presence_tracker: PresenceTracker, client: KrytenClient,
                 logger: logging.Logger):
        self._config = config
        self._db = database
        self._presence_tracker = presence_tracker
        self._client = client
        self._logger = logger
        self._tasks: list[asyncio.Task] = []

    async def _send_pm(self, channel: str, username: str, message: str) -> str:
        """Send PM via kryten-py client. Returns correlation ID."""
        return await self._client.send_pm(channel, username, message)
    
    async def start(self) -> None:
        """Start all scheduled tasks."""
        if self._config.rain.enabled:
            self._tasks.append(asyncio.create_task(self._rain_loop()))
        
        if self._config.balance_maintenance.mode != "none":
            self._tasks.append(asyncio.create_task(self._daily_maintenance_loop()))
    
    async def stop(self) -> None:
        """Cancel all tasks."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
    
    async def _rain_loop(self) -> None:
        """Periodic rain drop distribution."""
        while True:
            interval = self._config.rain.interval_minutes
            jitter = random.uniform(-0.3, 0.3) * interval
            await asyncio.sleep((interval + jitter) * 60)
            try:
                await self._execute_rain()
            except Exception:
                self._logger.exception("Rain execution failed")
    
    async def _daily_maintenance_loop(self) -> None:
        """Runs once per day at 03:00 UTC."""
        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self._execute_balance_maintenance()
            except Exception:
                self._logger.exception("Balance maintenance failed")
    
    async def _execute_rain(self) -> None:
        """Distribute rain to all connected users across all channels."""
        for ch_config in self._config.channels:
            channel = ch_config.channel
            users = self._presence_tracker.get_connected_users(channel)
            if not users:
                continue
            amount = random.randint(
                self._config.rain.min_amount,
                self._config.rain.max_amount,
            )
            for username in users:
                await self._db.credit(
                    username, channel, amount,
                    tx_type="rain",
                    trigger_id="rain.ambient",
                    reason=f"Rain drop: {amount}",
                )
                if self._config.rain.pm_notification:
                    msg = self._config.rain.message.format(
                        amount=amount,
                        currency=self._config.currency.name,
                    )
                    await self._send_pm(channel, username, msg)
            self._logger.info("Rain: %d Z to %d users in %s", amount, len(users), channel)
    
    async def _execute_balance_maintenance(self) -> None:
        """Apply interest or decay to all accounts."""
        mode = self._config.balance_maintenance.mode
        for ch_config in self._config.channels:
            channel = ch_config.channel
            if mode == "interest":
                cfg = self._config.balance_maintenance.interest
                total = await self._db.apply_interest_batch(
                    channel, cfg.daily_rate, cfg.max_daily_interest, cfg.min_balance_to_earn
                )
                self._logger.info("Interest: %d Z total in %s", total, channel)
            elif mode == "decay":
                cfg = self._config.balance_maintenance.decay
                total = await self._db.apply_decay_batch(
                    channel, cfg.daily_rate, cfg.exempt_below
                )
                self._logger.info("Decay: %d Z total in %s", total, channel)
```

### 11.4 Integration with `EconomyApp`

In `main.py`, after starting presence tracker:

```python
# Start scheduler
self.scheduler = Scheduler(
    config=self.config,
    database=self.db,
    presence_tracker=self.presence_tracker,
    client=self.client,
    logger=self.logger,
)
await self.scheduler.start()
```

In `stop()`:

```python
if self.scheduler:
    await self.scheduler.stop()
```

---

## 12. Presence Tracker Modifications

Sprint 1's presence tracker needs these additions for Sprint 2:

### 12.1 Enhanced Tick Logic

The presence tick (`_presence_tick`) is expanded:

```python
async def _presence_tick(self) -> None:
    while self._running:
        await asyncio.sleep(60)
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        current_hour = now.hour
        
        for key, session in list(self._sessions.items()):
            username, channel = session.username, session.channel
            
            # ── 0. Calendar day reset (Sprint 2) ───────────
            if getattr(session, '_current_date', None) != today:
                session.cumulative_minutes_today = 0
                session._current_date = today
            
            # ── 1. Base presence earning (Sprint 1) ─────────
            amount = self._config.base_rate_per_minute
            
            # ── 2. Night watch multiplier (Sprint 2) ────────
            metadata = {}
            nw = self._config.night_watch
            if nw.enabled and current_hour in nw.hours:
                amount = int(amount * nw.multiplier)
                metadata["multiplier"] = "night_watch"
                metadata["factor"] = nw.multiplier
            
            # ── 3. Credit presence Z ────────────────────────
            if amount > 0:
                await self._db.credit(
                    username, channel, amount,
                    tx_type="earn", reason="Presence",
                    trigger_id="presence.base",
                    metadata=json.dumps(metadata) if metadata else None,
                )
                await self._db.increment_daily_minutes_present(username, channel, today)
                await self._db.increment_daily_z_earned(username, channel, today, amount)
            
            # ── 4. Update session tracking ──────────────────
            session.cumulative_minutes_today += 1
            session.last_tick_at = now
            await self._db.update_last_seen(username, channel)
            
            # ── 5. Hourly dwell milestones (Sprint 2) ───────
            await self._check_hourly_milestones(username, channel, today,
                                                 session.cumulative_minutes_today)
            
            # ── 6. Daily streak check (Sprint 2) ────────────
            if session.cumulative_minutes_today == self._streak_config.daily.min_presence_minutes:
                # Exact threshold crossing — evaluate streak once
                await self._evaluate_daily_streak(username, channel, today)
                await self._evaluate_bridge(username, channel, today)
```

### 12.2 `_check_hourly_milestones()`

```python
async def _check_hourly_milestones(
    self, username: str, channel: str, date: str, cumulative_minutes: int
) -> None:
    """Award hourly milestones that haven't been claimed today."""
    milestones = self._config.hourly_milestones  # {hours: reward}
    for hours, reward in sorted(milestones.items()):
        threshold_minutes = hours * 60
        if cumulative_minutes >= threshold_minutes:
            # Check if already claimed
            row = await self._db.get_or_create_hourly_milestones(username, channel, date)
            col = f"hours_{hours}"
            if not row.get(col):
                # Award
                await self._db.credit(
                    username, channel, reward,
                    tx_type="milestone",
                    trigger_id=f"dwell.{hours}h",
                    reason=f"{hours}-hour dwell milestone",
                )
                await self._db.mark_hourly_milestone(username, channel, date, hours)
                await self._send_pm(
                    channel, username,
                    f"⏰ {hours}-hour milestone! +{reward} {self._currency_symbol}. Keep it up!"
                )
```

### 12.3 `_evaluate_daily_streak()`

```python
async def _evaluate_daily_streak(self, username: str, channel: str, today: str) -> None:
    """Called once per user per day when they hit min_presence_minutes."""
    streak = await self._db.get_or_create_streak(username, channel)
    last_date = streak.get("last_streak_date")
    current = streak.get("current_daily_streak", 0)
    longest = streak.get("longest_daily_streak", 0)
    
    if last_date == today:
        return  # Already counted today
    
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    
    if last_date == yesterday:
        current += 1  # Streak continues
    else:
        current = 1   # Streak resets
    
    longest = max(current, longest)
    await self._db.update_streak(username, channel, current, longest, today)
    
    # Award streak bonus (day 2+)
    if current >= 2:
        rewards = self._streak_config.daily.rewards  # {day_number: amount}
        reward = rewards.get(current, rewards.get(7, 100))  # Day 7+ repeats day-7 reward
        if reward > 0:
            await self._db.credit(
                username, channel, reward,
                tx_type="streak_bonus",
                trigger_id=f"streak.day{current}",
                reason=f"Day {current} streak bonus",
            )
            await self._send_pm(
                channel, username,
                f"🔥 Day {current} streak! +{reward} {self._currency_symbol}!"
            )
    
    # Milestone bonuses
    if current == 7:
        bonus = self._streak_config.daily.milestone_7_bonus
        if bonus > 0:
            await self._db.credit(
                username, channel, bonus,
                tx_type="streak_bonus",
                trigger_id="streak.milestone.7",
                reason="7-day streak milestone",
            )
            await self._send_pm(
                channel, username,
                f"🔥🔥 7-DAY STREAK! +{bonus} {self._currency_symbol}! You're on fire!"
            )
    
    if current == 30:
        bonus = self._streak_config.daily.milestone_30_bonus
        if bonus > 0:
            await self._db.credit(
                username, channel, bonus,
                tx_type="streak_bonus",
                trigger_id="streak.milestone.30",
                reason="30-day streak milestone",
            )
            await self._send_pm(
                channel, username,
                f"🔥🔥🔥 30-DAY STREAK! +{bonus} {self._currency_symbol}! LEGENDARY!"
            )
```

### 12.4 `_evaluate_bridge()`

```python
async def _evaluate_bridge(self, username: str, channel: str, today: str) -> None:
    """Check and award weekend→weekday bridge bonus."""
    if not self._config.streaks.weekend_weekday_bridge.enabled:
        return
    
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    iso_week = today_dt.strftime("%G-W%V")  # e.g. "2026-W09"
    is_weekend = today_dt.weekday() >= 5  # Sat=5, Sun=6
    
    streak = await self._db.get_or_create_streak(username, channel)
    
    # Reset if new week
    if streak.get("week_number") != iso_week:
        await self._db.update_bridge_fields(
            username, channel,
            weekend_seen=False, weekday_seen=False,
            bridge_claimed=False, week_number=iso_week,
        )
        streak["weekend_seen_this_week"] = False
        streak["weekday_seen_this_week"] = False
        streak["bridge_claimed_this_week"] = False
    
    # Update seen flags
    if is_weekend and not streak.get("weekend_seen_this_week"):
        await self._db.update_bridge_fields(username, channel, weekend_seen=True)
        streak["weekend_seen_this_week"] = True
    elif not is_weekend and not streak.get("weekday_seen_this_week"):
        await self._db.update_bridge_fields(username, channel, weekday_seen=True)
        streak["weekday_seen_this_week"] = True
    
    # Check for bridge
    if (streak.get("weekend_seen_this_week") and
        streak.get("weekday_seen_this_week") and
        not streak.get("bridge_claimed_this_week")):
        
        bonus = self._config.streaks.weekend_weekday_bridge.bonus
        await self._db.credit(
            username, channel, bonus,
            tx_type="earn",
            trigger_id="bridge.weekly",
            reason="Weekend-weekday bridge bonus",
        )
        await self._db.update_bridge_fields(username, channel, bridge_claimed=True)
        await self._send_pm(
            channel, username,
            f"🌉 Weekend→weekday bridge bonus! +{bonus} {self._currency_symbol}!"
        )
```

### 12.5 Welcome Wallet & Welcome-Back in `handle_user_join()`

Add to the genuine-arrival branch of `handle_user_join()`:

```python
if genuine:
    account = await self._db.get_or_create_account(username, channel)
    await self._db.update_last_seen(username, channel)
    
    # Welcome wallet (new users)
    if not account.get("welcome_wallet_claimed"):
        wallet_amount = self._onboarding_config.welcome_wallet
        if wallet_amount > 0:
            claimed = await self._db.claim_welcome_wallet(username, channel, wallet_amount)
            if claimed:
                msg = self._onboarding_config.welcome_message.format(
                    amount=wallet_amount,
                    currency=self._currency_name,
                )
                await self._send_pm(channel, username, msg)
    
    # Welcome-back bonus (returning users)
    elif self._retention_config.welcome_back.enabled:
        last_seen = parse_timestamp(account.get("last_seen"))  # from utils.py (Sprint 1)
        if last_seen:
            days_absent = (datetime.now(timezone.utc) - last_seen).days
            if days_absent >= self._retention_config.welcome_back.days_absent:
                bonus = self._retention_config.welcome_back.bonus
                await self._db.credit(
                    username, channel, bonus,
                    tx_type="welcome_back",
                    trigger_id="retention.welcome_back",
                    reason=f"Welcome back ({days_absent} days absent)",
                )
                msg = self._retention_config.welcome_back.message.format(
                    amount=bonus,
                    currency=self._currency_name,
                )
                await self._send_pm(channel, username, msg)
```

---

## 13. Config Sections Activated

These config sections become operational in Sprint 2 (were defined with defaults in Sprint 1 but not consumed):

| Config Path | Sprint 2 Consumer |
|---|---|
| `presence.hourly_milestones` | `_check_hourly_milestones()` |
| `presence.night_watch` | Presence tick multiplier |
| `streaks.daily` | `_evaluate_daily_streak()` |
| `streaks.weekend_weekday_bridge` | `_evaluate_bridge()` |
| `rain` | `scheduler._rain_loop()` |
| `onboarding.welcome_wallet` | `handle_user_join()` |
| `onboarding.welcome_message` | `handle_user_join()` |
| `retention.welcome_back` | `handle_user_join()` |
| `balance_maintenance` | `scheduler._daily_maintenance_loop()` |

---

## 14. Test Specifications

### 14.1 File: `tests/test_streaks.py`

| Test | Description |
|---|---|
| `test_first_day_streak_is_1` | First qualifying day → current_daily_streak = 1, no bonus awarded |
| `test_second_day_streak_awards_bonus` | Day 2 → streak = 2, 10 Z bonus |
| `test_consecutive_days_escalate` | Days 2–7 award escalating rewards per config |
| `test_missed_day_resets_streak` | Miss a day → streak resets to 1 on next qualifying day |
| `test_same_day_no_double_count` | Hitting threshold twice in same day doesn't double-count |
| `test_7_day_milestone_bonus` | Day 7 → extra 200 Z milestone bonus on top of daily |
| `test_30_day_milestone_bonus` | Day 30 → extra 2,000 Z milestone bonus |
| `test_streak_beyond_7_repeats_day7_reward` | Day 8, 9, 10... get the day-7 reward (100 Z) |
| `test_longest_streak_tracked` | After reset, longest_daily_streak retains peak |
| `test_streak_persists_across_restart` | Streak state loaded from DB, not just in-memory |
| `test_min_presence_minutes_threshold` | 14 min present → no streak. 15 min → streak qualifies |

### 14.2 File: `tests/test_hourly_milestones.py`

| Test | Description |
|---|---|
| `test_1h_milestone_awarded` | 60 cumulative minutes → 10 Z milestone |
| `test_3h_milestone_awarded` | 180 minutes → 30 Z |
| `test_6h_milestone_awarded` | 360 minutes → 75 Z |
| `test_12h_milestone_awarded` | 720 minutes → 200 Z |
| `test_24h_milestone_awarded` | 1440 minutes → 1,000 Z |
| `test_milestone_not_awarded_twice` | Hitting 60 min twice same day → only 1 award |
| `test_milestones_reset_on_new_day` | Day changes → milestones can be earned again |
| `test_all_milestones_cumulative` | 360 min earns 1h, 3h, AND 6h milestones |
| `test_milestone_pm_sent` | PM notification sent on each milestone |

### 14.3 File: `tests/test_bridge.py`

| Test | Description |
|---|---|
| `test_weekend_only_no_bonus` | Present Saturday only → no bridge bonus |
| `test_weekday_only_no_bonus` | Present Tuesday only → no bridge bonus |
| `test_weekend_and_weekday_awards_bonus` | Present Sat + Tue → 500 Z bridge bonus |
| `test_bridge_one_per_week` | Second qualifying combo in same week → no second bonus |
| `test_bridge_resets_on_new_week` | New ISO week → bridge can be earned again |
| `test_bridge_sunday_and_monday` | Sunday + Monday same week → bridge bonus |
| `test_bridge_disabled` | Config `enabled: false` → no bonus |

### 14.4 File: `tests/test_night_watch.py`

| Test | Description |
|---|---|
| `test_night_watch_multiplier_applied` | During configured hours, presence Z = base × multiplier |
| `test_night_watch_off_hours_normal` | Outside configured hours, presence Z = base rate |
| `test_night_watch_disabled` | Config `enabled: false` → no multiplier |
| `test_night_watch_metadata_logged` | Transaction metadata contains multiplier info |

### 14.5 File: `tests/test_rain.py`

| Test | Description |
|---|---|
| `test_rain_credits_all_connected` | Rain credits Z to every connected user |
| `test_rain_amount_in_range` | Amount is between min_amount and max_amount |
| `test_rain_skips_empty_channel` | No users connected → no rain distributed |
| `test_rain_excludes_ignored_users` | Ignored users don't receive rain |
| `test_rain_pm_notification` | If pm_notification enabled, PM sent to each user |
| `test_rain_transaction_logged` | Each credit logged with type="rain" |
| `test_rain_disabled` | Config `enabled: false` → no rain task started |

### 14.6 File: `tests/test_welcome.py`

| Test | Description |
|---|---|
| `test_welcome_wallet_new_user` | First genuine arrival → 100 Z credited |
| `test_welcome_wallet_idempotent` | Second arrival → no duplicate wallet |
| `test_welcome_wallet_pm_sent` | Welcome PM sent with correct amount |
| `test_welcome_wallet_zero` | Config `welcome_wallet: 0` → no credit |
| `test_welcome_back_after_7_days` | Absent 7 days, return → 100 Z bonus |
| `test_welcome_back_not_for_new_users` | Brand new user gets wallet, not welcome-back |
| `test_welcome_back_absent_6_days_no_bonus` | Absent 6 days → no bonus (threshold is 7) |
| `test_welcome_back_disabled` | Config `enabled: false` → no bonus |
| `test_welcome_wallet_and_back_mutually_exclusive` | New user never gets both |

### 14.7 File: `tests/test_balance_maintenance.py`

| Test | Description |
|---|---|
| `test_interest_credited` | Balance 10,000, rate 0.001 → 10 Z interest |
| `test_interest_capped` | Balance 100,000, rate 0.001 → capped at 10 Z (not 100) |
| `test_interest_min_balance` | Balance 50 (below min 100) → no interest |
| `test_interest_transaction_logged` | Interest logged with type="interest" |
| `test_decay_deducted` | Balance 100,000, rate 0.005 → 500 Z deducted |
| `test_decay_exempt_below` | Balance 40,000 (below 50,000 exempt) → no decay |
| `test_decay_transaction_logged` | Decay logged with type="decay" |
| `test_mode_none` | Config `mode: "none"` → no interest or decay |

---

## 15. Acceptance Criteria

- [ ] `streaks`, `hourly_milestones`, and `trigger_cooldowns` tables created on startup
- [ ] Daily streak increments when user is present ≥ min_presence_minutes
- [ ] Streak bonuses award correct amounts per config (days 2–7+)
- [ ] Streak resets on missed day
- [ ] 7-day and 30-day milestone bonuses award on top of daily
- [ ] Hourly milestones award at 1h, 3h, 6h, 12h, 24h thresholds (one-time per day)
- [ ] Milestones reset on calendar day change (UTC)
- [ ] Weekend→weekday bridge bonus awards once per ISO week
- [ ] Bridge fields reset on new ISO week
- [ ] Night watch multiplier applies during configured hours
- [ ] Night watch multiplier does NOT apply outside configured hours
- [ ] Rain distributes random Z to all connected (non-ignored) users
- [ ] Rain interval is randomized ±30% around configured base
- [ ] Welcome wallet credits one-time to new users on genuine arrival
- [ ] Welcome wallet is idempotent (no double credit)
- [ ] Welcome-back bonus credits to returning users absent ≥ N days
- [ ] Welcome wallet and welcome-back are mutually exclusive
- [ ] Balance interest/decay runs daily, applies correct rates and caps
- [ ] All bonuses logged as transactions with appropriate types and trigger_ids
- [ ] PM notifications sent for streaks, milestones, rain, welcome, and welcome-back
- [ ] All new tests pass (`pytest` exits 0)

---

*End of Sprint 2 specification. This document is self-contained and sufficient for an AI coding agent to implement the full sprint given a completed Sprint 1 codebase.*
