# Sprint 7 — Competitive Events, Multipliers & Bounties

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 7 of 9  
> **Goal:** Time-limited competitive mechanics, earning multipliers, and user-generated bounties that drive urgency, social coordination, and organic coin sinks.  
> **Depends on:** Sprint 1 (Core Foundation), Sprint 3 (daily_activity tracking), Sprint 5 (Spending engine for bounty debit), Sprint 6 (Rank engine — perks interact with multipliers)  
> **Enables:** Sprint 8 (Admin event start/stop, reporting on competition/bounty data), Sprint 9 (Event announcer refinement)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Config Activation](#3-config-activation)
4. [Daily Competition Engine](#4-daily-competition-engine)
5. [Multiplier Engine](#5-multiplier-engine)
6. [Scheduled Event Manager](#6-scheduled-event-manager)
7. [Admin Ad-Hoc Events](#7-admin-ad-hoc-events)
8. [Bounty System](#8-bounty-system)
9. [PM Commands: bounty](#9-pm-commands-bounty)
10. [PM Commands: bounties](#10-pm-commands-bounties)
11. [PM Commands: events / multipliers](#11-pm-commands-events--multipliers)
12. [Admin PM Commands: event start/stop](#12-admin-pm-commands-event-startstop)
13. [Admin PM Commands: claim_bounty](#13-admin-pm-commands-claim_bounty)
14. [PM Command Registrations](#14-pm-command-registrations)
15. [Earning Path Integration](#15-earning-path-integration)
16. [Public Announcements](#16-public-announcements)
17. [Metrics Extensions](#17-metrics-extensions)
18. [Test Specifications](#18-test-specifications)
19. [Acceptance Criteria](#19-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint:

- **Daily competition engine** runs at configurable end-of-day, evaluates threshold and champion awards against `daily_activity` counters, credits rewards, sends PMs and public announcements
- **Multiplier engine** maintains a live stack of active multipliers (off-peak, population, holiday, scheduled, ad-hoc) and applies them to all earning events with metadata logged in transactions
- **Scheduled event manager** uses croniter to start/stop events with cron expressions, fires presence bonuses to all connected users, announces start/end in public chat
- **Admin ad-hoc events** allow CyTube rank ≥ 4 users to start/stop custom multiplier events via PM
- **Bounty system** lets users create Z-funded bounties with descriptions, view open bounties, and have admins claim/cancel them with winner payout and partial-refund expiry
- New PM commands: `bounty`, `bounties`, `events`/`multipliers`
- Admin PM commands: `event start`, `event stop`, `claim_bounty`
- All interaction via kryten-py wrappers — zero raw NATS

> **⚠️ Ecosystem rule:** All NATS interaction goes through kryten-py's `KrytenClient`. Use `client.send_pm()`, `client.send_chat()` — never raw NATS subjects.

---

## 2. New Database Tables

### 2.1 `bounties` Table

```sql
CREATE TABLE IF NOT EXISTS bounties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator TEXT NOT NULL,
    channel TEXT NOT NULL,
    description TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT DEFAULT 'open',     -- open, claimed, expired, cancelled
    winner TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    resolved_by TEXT,               -- Admin who claimed/cancelled
    resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(channel, status);
CREATE INDEX IF NOT EXISTS idx_bounties_creator ON bounties(creator, channel);
```

### 2.2 Database Methods to Add

```python
# ── Bounty Methods ───────────────────────────────────────────

async def create_bounty(
    self, creator: str, channel: str, description: str,
    amount: int, expires_at: str | None = None,
) -> int:
    """Create a bounty. Returns bounty ID."""

async def get_open_bounties(self, channel: str, limit: int = 20) -> list[dict]:
    """List open bounties. Returns [{id, creator, description, amount, created_at, expires_at}, ...]"""

async def get_bounty(self, bounty_id: int, channel: str) -> dict | None:
    """Get a single bounty by ID."""

async def claim_bounty(
    self, bounty_id: int, channel: str, winner: str, resolved_by: str,
) -> bool:
    """Claim a bounty. Sets status='claimed', winner, resolved_by/at. Returns True if updated."""

async def cancel_bounty(
    self, bounty_id: int, channel: str, resolved_by: str,
) -> bool:
    """Cancel a bounty. Sets status='cancelled'. Returns True if updated."""

async def expire_bounties(self, channel: str) -> list[dict]:
    """Find and expire all open bounties past expires_at. Returns list of expired bounties."""

# ── Daily Competition Queries ────────────────────────────────

async def get_daily_activity_all(self, channel: str, date: str) -> list[dict]:
    """Get all daily_activity rows for a channel+date. Returns all columns."""

async def get_daily_top(self, channel: str, date: str, field: str, limit: int = 1) -> list[dict]:
    """Get top users for a specific daily_activity field. Returns [{username, value}, ...]"""

async def get_daily_threshold_qualifiers(
    self, channel: str, date: str, field: str, threshold: int,
) -> list[str]:
    """Get usernames where daily_activity.{field} >= threshold. Returns [username, ...]"""
```

---

## 3. Config Activation

This sprint activates these config sections from `kryten-economy-plan.md`:

- `daily_competitions[]` — competition definitions with threshold and top conditions
- `multipliers.off_peak` — weekday off-peak hours multiplier
- `multipliers.high_population` — population-gated hidden multiplier
- `multipliers.holidays` — date-based holiday multipliers
- `multipliers.scheduled_events[]` — cron-based recurring events with presence bonuses
- Bounty config (new section to add)

### 3.1 Pydantic Config Models

```python
from pydantic import BaseModel, Field
from typing import Literal


class DailyCompetitionCondition(BaseModel):
    type: Literal["daily_threshold", "daily_top"]
    field: str              # daily_activity column name
    threshold: int = 0      # For daily_threshold type

class DailyCompetition(BaseModel):
    id: str
    description: str = ""
    condition: DailyCompetitionCondition
    reward: int = 0
    reward_percent_of_earnings: int = 0  # For top_earner: % of day's z_earned
    hidden: bool = True
    announce_public: bool = True

class OffPeakConfig(BaseModel):
    enabled: bool = True
    days: list[int] = [1, 2, 3, 4]     # Mon-Thu (0=Sun)
    hours: list[int] = list(range(6, 16))
    multiplier: float = 2.0
    announce: bool = True

class HighPopulationConfig(BaseModel):
    enabled: bool = True
    min_users: int = 10
    multiplier: float = 1.5
    hidden: bool = True

class HolidayEntry(BaseModel):
    date: str              # "MM-DD" format
    name: str
    multiplier: float = 3.0

class HolidayConfig(BaseModel):
    enabled: bool = True
    dates: list[HolidayEntry] = []
    announce: bool = True

class ScheduledEvent(BaseModel):
    name: str
    cron: str              # croniter-compatible expression
    duration_hours: float = 4.0
    multiplier: float = 2.0
    presence_bonus: int = 0     # Z split among all present at event start
    announce: bool = True

class MultiplierConfig(BaseModel):
    off_peak: OffPeakConfig = OffPeakConfig()
    high_population: HighPopulationConfig = HighPopulationConfig()
    holidays: HolidayConfig = HolidayConfig()
    scheduled_events: list[ScheduledEvent] = []

class BountyConfig(BaseModel):
    enabled: bool = True
    min_amount: int = 100
    max_amount: int = 50000
    max_open_per_user: int = 3
    default_expiry_hours: int = 168     # 7 days
    expiry_refund_percent: int = 50     # 50% refund on expiry
    description_max_length: int = 200

class EconomyConfig(KrytenConfig):
    # ... existing Sprint 1-6 fields ...
    daily_competitions: list[DailyCompetition] = []
    multipliers: MultiplierConfig = MultiplierConfig()
    bounties: BountyConfig = BountyConfig()
```

---

## 4. Daily Competition Engine

### 4.1 File: `kryten_economy/competition_engine.py`

### 4.2 Class: `CompetitionEngine`

```python
import logging
from datetime import datetime, timedelta, timezone


class CompetitionEngine:
    """Evaluates daily competitions at end-of-day and awards prizes."""

    def __init__(self, config, database, client, logger: logging.Logger):
        self._config = config
        self._db = database
        self._client = client
        self._logger = logger
        self._competitions = config.daily_competitions

    async def evaluate_daily_competitions(self, channel: str, date: str) -> list[dict]:
        """Run all configured daily competitions for a given date.
        
        Called by scheduler at end-of-day (e.g., 23:59 UTC or configurable).
        Returns list of award records: [{competition_id, username, reward, reason}, ...]
        """
        all_awards = []

        for comp in self._competitions:
            try:
                awards = await self._evaluate_one(comp, channel, date)
                all_awards.extend(awards)
            except Exception as e:
                self._logger.error(
                    "Competition %s evaluation failed: %s", comp.id, e
                )

        # Credit all awards
        for award in all_awards:
            await self._db.credit(
                award["username"], channel, award["reward"],
                tx_type="competition",
                trigger_id=f"competition.{award['competition_id']}",
                reason=award["reason"],
            )
            # PM the winner
            symbol = self._config.currency.symbol
            await self._client.send_pm(
                channel, award["username"],
                f"🏅 {award['reason']} — +{award['reward']:,} {symbol}"
            )

        # Public announcement summary
        if all_awards:
            await self._announce_daily_results(channel, all_awards)

        return all_awards

    async def _evaluate_one(
        self, comp: DailyCompetition, channel: str, date: str,
    ) -> list[dict]:
        """Evaluate a single competition. Returns list of awards."""
        awards = []
        ctype = comp.condition.type

        if ctype == "daily_threshold":
            qualifiers = await self._db.get_daily_threshold_qualifiers(
                channel, date, comp.condition.field, comp.condition.threshold,
            )
            for username in qualifiers:
                awards.append({
                    "competition_id": comp.id,
                    "username": username,
                    "reward": comp.reward,
                    "reason": comp.description,
                })

        elif ctype == "daily_top":
            top_users = await self._db.get_daily_top(channel, date, comp.condition.field, limit=1)
            if top_users:
                winner = top_users[0]
                if comp.reward_percent_of_earnings > 0:
                    # Top earner: reward is % of their day's earnings
                    day_earned = winner.get("value", 0)
                    reward = max(1, int(day_earned * comp.reward_percent_of_earnings / 100))
                else:
                    reward = comp.reward
                
                awards.append({
                    "competition_id": comp.id,
                    "username": winner["username"],
                    "reward": reward,
                    "reason": comp.description,
                })

        return awards

    async def _announce_daily_results(self, channel: str, awards: list[dict]) -> None:
        """Public announcement of daily competition results."""
        # Group by competition
        by_comp = {}
        for a in awards:
            by_comp.setdefault(a["competition_id"], []).append(a)

        lines = ["📊 Daily Competition Results:"]
        for comp_id, comp_awards in by_comp.items():
            comp_cfg = next((c for c in self._competitions if c.id == comp_id), None)
            desc = comp_cfg.description if comp_cfg else comp_id
            if len(comp_awards) == 1:
                a = comp_awards[0]
                lines.append(f"  🏅 {desc}: {a['username']} (+{a['reward']:,} Z)")
            else:
                names = ", ".join(a["username"] for a in comp_awards)
                reward = comp_awards[0]["reward"]
                lines.append(f"  🏅 {desc}: {names} (+{reward:,} Z each)")

        await self._client.send_chat(channel, "\n".join(lines))
```

### 4.3 Scheduler Integration

The daily competition evaluation is triggered by a scheduler (asyncio task in the orchestrator):

```python
async def _schedule_daily_competitions(self) -> None:
    """Run once daily at end-of-day (23:59 UTC or config)."""
    while True:
        now = datetime.now(timezone.utc)
        # Calculate seconds until next end-of-day
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        await asyncio.sleep(delay)

        # Recalculate 'today' AFTER the sleep — 'now' was captured before sleeping
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for channel in self._active_channels():
            try:
                awards = await self._competition_engine.evaluate_daily_competitions(
                    channel, today,
                )
                self._logger.info(
                    "Daily competitions for %s: %d awards", channel, len(awards),
                )
            except Exception as e:
                self._logger.error("Daily competition error for %s: %s", channel, e)
```

---

## 5. Multiplier Engine

### 5.1 File: `kryten_economy/multiplier_engine.py`

### 5.2 Class: `MultiplierEngine`

The multiplier engine checks all active multiplier sources and returns a combined multiplier for any earning event. It does NOT apply the multiplier itself — it just reports the active stack. The earning path multiplies the base reward.

```python
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple


class ActiveMultiplier(NamedTuple):
    source: str         # "off_peak", "population", "holiday:Christmas", "scheduled:Weird Wednesday", "adhoc:Admin Event"
    multiplier: float
    hidden: bool


class MultiplierEngine:
    """Calculates the combined active multiplier at any given moment."""

    def __init__(self, config, presence_tracker, logger: logging.Logger):
        self._config = config.multipliers
        self._presence = presence_tracker
        self._logger = logger
        # Ad-hoc event state (set by admin commands)
        self._adhoc_event: dict | None = None  # {name, multiplier, end_time}
        # Scheduled event state (set by ScheduledEventManager)
        self._scheduled_events: dict[str, dict] = {}  # channel → {name, multiplier, end_time}

    def get_active_multipliers(self, channel: str) -> list[ActiveMultiplier]:
        """Return all currently active multipliers for the channel."""
        now = datetime.now(timezone.utc)
        active = []

        # Off-peak
        if self._config.off_peak.enabled:
            if (now.weekday() + 1) % 7 in self._config.off_peak.days:  # Convert to 0=Sun
                if now.hour in self._config.off_peak.hours:
                    active.append(ActiveMultiplier(
                        source="off_peak",
                        multiplier=self._config.off_peak.multiplier,
                        hidden=False,
                    ))

        # High population
        if self._config.high_population.enabled:
            user_count = len(self._presence.get_connected_users(channel))
            if user_count >= self._config.high_population.min_users:
                active.append(ActiveMultiplier(
                    source="population",
                    multiplier=self._config.high_population.multiplier,
                    hidden=self._config.high_population.hidden,
                ))

        # Holidays
        if self._config.holidays.enabled:
            today_mmdd = now.strftime("%m-%d")
            for holiday in self._config.holidays.dates:
                if holiday.date == today_mmdd:
                    active.append(ActiveMultiplier(
                        source=f"holiday:{holiday.name}",
                        multiplier=holiday.multiplier,
                        hidden=False,
                    ))

        # Scheduled events
        sched = self._get_scheduled_multiplier(channel)
        if sched:
            active.append(sched)

        # Ad-hoc event
        if self._adhoc_event:
            if now < self._adhoc_event["end_time"]:
                active.append(ActiveMultiplier(
                    source=f"adhoc:{self._adhoc_event['name']}",
                    multiplier=self._adhoc_event["multiplier"],
                    hidden=False,
                ))
            else:
                # Auto-expire
                self._adhoc_event = None

        return active

    def get_combined_multiplier(self, channel: str) -> tuple[float, list[ActiveMultiplier]]:
        """Return the combined multiplier and the list of active sources.
        
        Multipliers are MULTIPLICATIVE: 2.0 × 1.5 = 3.0× total.
        Returns (combined_multiplier, active_list).
        """
        active = self.get_active_multipliers(channel)
        combined = 1.0
        for m in active:
            combined *= m.multiplier
        return combined, active

    # ── Scheduled event registration ─────────────────────────

    def set_scheduled_event(self, channel: str, name: str, multiplier: float, end_time: datetime) -> None:
        """Register an active scheduled event."""
        self._scheduled_events[channel] = {
            "name": name, "multiplier": multiplier, "end_time": end_time,
        }

    def clear_scheduled_event(self, channel: str) -> None:
        """Deregister the active scheduled event."""
        self._scheduled_events.pop(channel, None)

    def _get_scheduled_multiplier(self, channel: str) -> ActiveMultiplier | None:
        """Check for an active scheduled event."""
        ev = self._scheduled_events.get(channel)
        if ev and datetime.now(timezone.utc) < ev["end_time"]:
            return ActiveMultiplier(
                source=f"scheduled:{ev['name']}",
                multiplier=ev["multiplier"],
                hidden=False,
            )
        elif ev:
            del self._scheduled_events[channel]
        return None

    # ── Ad-hoc event management ──────────────────────────────

    def start_adhoc_event(self, name: str, multiplier: float, duration_minutes: int) -> None:
        """Start an admin-triggered ad-hoc multiplier event."""
        self._adhoc_event = {
            "name": name,
            "multiplier": multiplier,
            "end_time": datetime.now(timezone.utc) + timedelta(minutes=duration_minutes),
        }

    def stop_adhoc_event(self) -> bool:
        """Stop the current ad-hoc event. Returns True if there was one to stop."""
        if self._adhoc_event:
            self._adhoc_event = None
            return True
        return False
```

**Note:** The `get_active_multipliers` method must also call `_get_scheduled_multiplier()` to include scheduled events. Add to the method:

```python
        # Scheduled events
        sched = self._get_scheduled_multiplier(channel)
        if sched:
            active.append(sched)
```

---

## 6. Scheduled Event Manager

### 6.1 File: `kryten_economy/scheduled_event_manager.py`

### 6.2 Class: `ScheduledEventManager`

Uses `croniter` (already a dependency from Sprint 5) to evaluate cron-based scheduled events:

```python
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from croniter import croniter


class ScheduledEventManager:
    """Manages cron-based scheduled events: start/end, presence bonuses, announcements."""

    def __init__(self, config, multiplier_engine, presence_tracker, database, client, logger):
        self._events = config.multipliers.scheduled_events
        self._multiplier = multiplier_engine
        self._presence = presence_tracker
        self._db = database
        self._client = client
        self._logger = logger
        self._active: dict[str, dict] = {}  # channel → {event_name, end_time, task}
        self._check_task: asyncio.Task | None = None

    async def start(self, channels: list[str]) -> None:
        """Start the event monitoring loop."""
        self._channels = channels
        self._check_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the event monitoring loop."""
        if self._check_task:
            self._check_task.cancel()

    async def _monitor_loop(self) -> None:
        """Check every 60 seconds for events that should start or end."""
        while True:
            try:
                now = datetime.now(timezone.utc)
                for event_cfg in self._events:
                    for channel in self._channels:
                        await self._check_event(event_cfg, channel, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.error("Scheduled event monitor error: %s", e)
            await asyncio.sleep(60)

    async def _check_event(
        self, event_cfg: ScheduledEvent, channel: str, now: datetime,
    ) -> None:
        """Check if a specific event should start or has ended."""
        key = f"{channel}:{event_cfg.name}"

        # Is this event currently active?
        if key in self._active:
            active = self._active[key]
            if now >= active["end_time"]:
                # Event ended
                await self._end_event(event_cfg, channel, key)
            return

        # Should this event start now?
        cron = croniter(event_cfg.cron, now - timedelta(minutes=1))
        next_fire = cron.get_next(datetime)
        # If next_fire is within 60 seconds of now, it's "firing now"
        if abs((next_fire - now).total_seconds()) < 90:
            # Don't re-fire if we already started it this cycle
            await self._start_event(event_cfg, channel, key, next_fire)

    async def _start_event(
        self, event_cfg: ScheduledEvent, channel: str, key: str, fire_time: datetime,
    ) -> None:
        """Activate a scheduled event."""
        end_time = fire_time + timedelta(hours=event_cfg.duration_hours)
        self._active[key] = {"event_name": event_cfg.name, "end_time": end_time}
        
        # Register multiplier
        self._multiplier.set_scheduled_event(
            channel, event_cfg.name, event_cfg.multiplier, end_time,
        )
        
        self._logger.info("Scheduled event started: %s in %s", event_cfg.name, channel)

        # Announce start
        if event_cfg.announce:
            await self._client.send_chat(
                channel,
                f"🎉 **{event_cfg.name}** has started! "
                f"{event_cfg.multiplier}× earning for {event_cfg.duration_hours:.0f} hours!"
            )

        # Presence bonus
        if event_cfg.presence_bonus > 0:
            await self._distribute_presence_bonus(event_cfg, channel)

    async def _end_event(
        self, event_cfg: ScheduledEvent, channel: str, key: str,
    ) -> None:
        """Deactivate a scheduled event."""
        del self._active[key]
        self._multiplier.clear_scheduled_event(channel)
        
        self._logger.info("Scheduled event ended: %s in %s", event_cfg.name, channel)
        
        if event_cfg.announce:
            await self._client.send_chat(
                channel,
                f"⏰ **{event_cfg.name}** has ended. Thanks for participating!"
            )

    async def _distribute_presence_bonus(
        self, event_cfg: ScheduledEvent, channel: str,
    ) -> None:
        """Split presence bonus among all connected users at event start."""
        present_users = self._presence.get_present_users(channel)
        if not present_users:
            return
        
        per_user = max(1, event_cfg.presence_bonus // len(present_users))
        
        for username in present_users:
            await self._db.credit(
                username, channel, per_user,
                tx_type="event_bonus",
                trigger_id=f"event.{event_cfg.name}.presence",
                reason=f"Present at {event_cfg.name} start",
            )
            await self._client.send_pm(
                channel, username,
                f"🎁 You were here when **{event_cfg.name}** started! +{per_user:,} Z"
            )
        
        self._logger.info(
            "Presence bonus for %s: %d Z each to %d users",
            event_cfg.name, per_user, len(present_users),
        )
```

---

## 7. Admin Ad-Hoc Events

### 7.1 Start Event

```python
async def _cmd_event_start(
    self, username: str, channel: str, args: list[str],
) -> str:
    """Admin: Start an ad-hoc multiplier event.
    
    Usage: event start <multiplier> <minutes> "<name>"
    Example: event start 3.0 120 "Triple Z Friday"
    """
    if len(args) < 3:
        return 'Usage: event start <multiplier> <minutes> "<name>"'
    
    try:
        multiplier = float(args[0])
        minutes = int(args[1])
        name = " ".join(args[2:]).strip('"').strip("'")
    except (ValueError, IndexError):
        return 'Usage: event start <multiplier> <minutes> "<name>"'
    
    if not (1.0 < multiplier <= 10.0):
        return "Multiplier must be between 1.0 and 10.0"
    if not (1 <= minutes <= 1440):
        return "Duration must be between 1 and 1440 minutes (24 hours)"
    if not name:
        return "Event name is required."
    
    self._multiplier_engine.start_adhoc_event(name, multiplier, minutes)
    
    # Public announcement
    await self._client.send_chat(
        channel,
        f"🎉 **{name}** activated by {username}! "
        f"{multiplier}× earning for {minutes} minutes!"
    )
    
    return f"Event '{name}' started: {multiplier}× for {minutes} min."
```

### 7.2 Stop Event

```python
async def _cmd_event_stop(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Stop the current ad-hoc event."""
    stopped = self._multiplier_engine.stop_adhoc_event()
    if stopped:
        await self._client.send_chat(channel, "⏰ The current event has been stopped.")
        return "Ad-hoc event stopped."
    return "No ad-hoc event is currently active."
```

### 7.3 Admin Gate

These commands are gated by CyTube rank. The PM handler checks rank before dispatch:

```python
# In the PM handler's command dispatch:
_ADMIN_COMMANDS = {"event", "claim_bounty", "grant", "deduct", ...}

async def _dispatch(self, username, channel, command, args, rank):
    if command in self._ADMIN_COMMANDS:
        admin_level = self._config.admin.owner_level  # default 4
        if rank < admin_level:
            return "⛔ This command requires admin privileges."
    # ... dispatch as normal
```

**Note:** The `rank` comes from the `ChatMessageEvent.rank` field (CyTube rank, not economy rank).

---

## 8. Bounty System

### 8.1 File: `kryten_economy/bounty_manager.py`

### 8.2 Class: `BountyManager`

```python
import logging
from datetime import datetime, timedelta, timezone


class BountyManager:
    """Manages user-created bounties."""

    def __init__(self, config, database, client, logger: logging.Logger):
        self._config = config.bounties
        self._db = database
        self._client = client
        self._logger = logger

    async def create_bounty(
        self, creator: str, channel: str, amount: int, description: str,
    ) -> dict:
        """Create a new bounty. Debits creator's balance.
        
        Returns {success: bool, bounty_id: int, message: str}.
        """
        cfg = self._config
        
        if not cfg.enabled:
            return {"success": False, "message": "Bounties are disabled."}
        
        if amount < cfg.min_amount:
            return {"success": False, "message": f"Minimum bounty: {cfg.min_amount:,} Z"}
        if amount > cfg.max_amount:
            return {"success": False, "message": f"Maximum bounty: {cfg.max_amount:,} Z"}
        if len(description) > cfg.description_max_length:
            return {
                "success": False,
                "message": f"Description max {cfg.description_max_length} chars.",
            }
        
        # Check open bounty limit
        open_bounties = await self._db.get_open_bounties(channel)
        user_open = [b for b in open_bounties if b["creator"] == creator]
        if len(user_open) >= cfg.max_open_per_user:
            return {
                "success": False,
                "message": f"You already have {len(user_open)} open bounties (max {cfg.max_open_per_user}).",
            }
        
        # Debit the bounty amount
        success = await self._db.atomic_debit(
            creator, channel, amount,
            tx_type="bounty_create",
            trigger_id="bounty.create",
            reason=f"Bounty: {description[:50]}",
        )
        if not success:
            return {"success": False, "message": "Insufficient funds."}
        
        # Calculate expiry
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=cfg.default_expiry_hours)
        ).isoformat()
        
        bounty_id = await self._db.create_bounty(
            creator, channel, description, amount, expires_at,
        )
        
        self._logger.info(
            "Bounty #%d created by %s: %d Z '%s'",
            bounty_id, creator, amount, description,
        )
        
        return {
            "success": True,
            "bounty_id": bounty_id,
            "message": (
                f"📌 Bounty #{bounty_id} created! {amount:,} Z\n"
                f"   \"{description}\"\n"
                f"   Expires in {cfg.default_expiry_hours} hours."
            ),
        }

    async def claim_bounty(
        self, bounty_id: int, channel: str, winner: str, admin: str,
    ) -> str:
        """Admin claims a bounty for a winner. Credits winner."""
        bounty = await self._db.get_bounty(bounty_id, channel)
        if not bounty:
            return f"Bounty #{bounty_id} not found."
        if bounty["status"] != "open":
            return f"Bounty #{bounty_id} is already {bounty['status']}."
        
        # Claim it
        claimed = await self._db.claim_bounty(bounty_id, channel, winner, admin)
        if not claimed:
            return "Failed to claim bounty."
        
        # Credit the winner
        await self._db.credit(
            winner, channel, bounty["amount"],
            tx_type="bounty_claim",
            trigger_id=f"bounty.claim.{bounty_id}",
            reason=f"Bounty #{bounty_id}: {bounty['description'][:50]}",
        )
        
        # Notify creator
        await self._client.send_pm(
            channel, bounty["creator"],
            f"📌 Your bounty #{bounty_id} was claimed by {winner}! "
            f"({bounty['amount']:,} Z awarded)"
        )
        
        # Notify winner
        await self._client.send_pm(
            channel, winner,
            f"🎯 You earned bounty #{bounty_id}: {bounty['description']}! "
            f"+{bounty['amount']:,} Z"
        )
        
        # Public announcement
        await self._client.send_chat(
            channel,
            f"🎯 {winner} claimed bounty #{bounty_id}: "
            f"\"{bounty['description']}\" (+{bounty['amount']:,} Z)"
        )
        
        return f"Bounty #{bounty_id} claimed by {winner}. {bounty['amount']:,} Z awarded."

    async def process_expired_bounties(self, channel: str) -> int:
        """Expire old bounties and refund creators partially.
        
        Called periodically by scheduler. Returns count of expired bounties.
        """
        expired = await self._db.expire_bounties(channel)
        refund_pct = self._config.expiry_refund_percent
        
        for bounty in expired:
            refund = int(bounty["amount"] * refund_pct / 100)
            if refund > 0:
                await self._db.credit(
                    bounty["creator"], channel, refund,
                    tx_type="bounty_expired_refund",
                    trigger_id=f"bounty.expired.{bounty['id']}",
                    reason=f"Bounty #{bounty['id']} expired — {refund_pct}% refund",
                )
                await self._client.send_pm(
                    channel, bounty["creator"],
                    f"📌 Bounty #{bounty['id']} expired. "
                    f"Refund: {refund:,} Z ({refund_pct}% of {bounty['amount']:,} Z)"
                )
            
            self._logger.info(
                "Bounty #%d expired. Refund %d Z to %s",
                bounty["id"], refund, bounty["creator"],
            )
        
        return len(expired)
```

### 8.3 Expiry Scheduler

```python
async def _schedule_bounty_expiry(self) -> None:
    """Run every hour to expire old bounties."""
    while True:
        await asyncio.sleep(3600)  # 1 hour
        for channel in self._active_channels():
            try:
                count = await self._bounty_manager.process_expired_bounties(channel)
                if count > 0:
                    self._logger.info("Expired %d bounties in %s", count, channel)
            except Exception as e:
                self._logger.error("Bounty expiry error for %s: %s", channel, e)
```

---

## 9. PM Commands: `bounty`

### 9.1 Create a Bounty

```python
async def _cmd_bounty(self, username: str, channel: str, args: list[str]) -> str:
    """Create a user bounty.
    
    Usage: bounty <amount> "<description>"
    Example: bounty 500 "First person to find a VHS copy of Manos: The Hands of Fate"
    """
    if len(args) < 2:
        return 'Usage: bounty <amount> "<description>"'
    
    try:
        amount = int(args[0])
    except ValueError:
        return "Amount must be a number."
    
    # Reconstruct description from remaining args
    description = " ".join(args[1:]).strip('"').strip("'")
    if not description:
        return "Description is required."
    
    result = await self._bounty_manager.create_bounty(username, channel, amount, description)
    
    if result["success"]:
        # Public announcement
        await self._client.send_chat(
            channel,
            f"📌 New bounty by {username}: \"{description}\" ({amount:,} Z)"
        )
    
    return result["message"]
```

---

## 10. PM Commands: `bounties`

### 10.1 List Open Bounties

```python
async def _cmd_bounties(self, username: str, channel: str, args: list[str]) -> str:
    """List open bounties."""
    bounties = await self._db.get_open_bounties(channel, limit=15)
    
    if not bounties:
        return "No open bounties."
    
    lines = ["📌 Open Bounties:"]
    for b in bounties:
        age = self._format_age(b["created_at"])
        lines.append(
            f"  #{b['id']} — {b['amount']:,} Z — {b['description'][:60]}"
            f" (by {b['creator']}, {age} ago)"
        )
    
    lines.append(f"\n{len(bounties)} open bounty/bounties. An admin can claim with: claim_bounty <id> @winner")
    return "\n".join(lines)

def _format_age(self, timestamp_str: str) -> str:
    """Format a timestamp as a human-readable age string."""
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        hours = int(delta.total_seconds() / 3600)
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)} min"
        if hours < 24:
            return f"{hours}h"
        return f"{hours // 24}d"
    except Exception:
        return "?"
```

---

## 11. PM Commands: `events` / `multipliers`

### 11.1 Show Active Events and Multipliers

```python
async def _cmd_events(self, username: str, channel: str, args: list[str]) -> str:
    """Show currently active multipliers and events."""
    combined, active = self._multiplier_engine.get_combined_multiplier(channel)
    
    if not active:
        return "No active multipliers right now. Earning at 1× base rate."
    
    lines = ["⚡ Active Multipliers:"]
    for m in active:
        if m.hidden:
            continue  # Don't reveal hidden multipliers
        source_display = self._format_multiplier_source(m.source)
        lines.append(f"  {source_display}: {m.multiplier}×")
    
    lines.append(f"\n💫 Combined: {combined:.1f}× earning rate")
    return "\n".join(lines)

def _format_multiplier_source(self, source: str) -> str:
    """Format a multiplier source for display."""
    if source == "off_peak":
        return "📅 Off-Peak Bonus"
    if source == "population":
        return "👥 Crowd Bonus"
    if source.startswith("holiday:"):
        return f"🎄 {source.split(':', 1)[1]}"
    if source.startswith("scheduled:"):
        return f"🎉 {source.split(':', 1)[1]}"
    if source.startswith("adhoc:"):
        return f"⚡ {source.split(':', 1)[1]}"
    return source
```

---

## 12. Admin PM Commands: `event start/stop`

See Section 7 (Admin Ad-Hoc Events) for implementation.

The `event` command delegates to sub-commands:

```python
async def _cmd_event(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Manage ad-hoc events. Sub-commands: start, stop."""
    if not args:
        return "Usage: event start <multiplier> <minutes> \"<name>\" | event stop"
    
    subcmd = args[0].lower()
    remaining = args[1:]
    
    match subcmd:
        case "start":
            return await self._cmd_event_start(username, channel, remaining)
        case "stop":
            return await self._cmd_event_stop(username, channel, remaining)
        case _:
            return "Usage: event start <multiplier> <minutes> \"<name>\" | event stop"
```

---

## 13. Admin PM Commands: `claim_bounty`

```python
async def _cmd_claim_bounty(
    self, username: str, channel: str, args: list[str],
) -> str:
    """Admin: Award an open bounty to a user.
    
    Usage: claim_bounty <id> @winner
    """
    if len(args) < 2:
        return "Usage: claim_bounty <id> @winner"
    
    try:
        bounty_id = int(args[0])
    except ValueError:
        return "Bounty ID must be a number."
    
    winner = args[1].lstrip("@")
    if not winner:
        return "Winner username is required."
    
    return await self._bounty_manager.claim_bounty(bounty_id, channel, winner, username)
```

---

## 14. PM Command Registrations

### 14.1 Sprint 7 Additions to Command Map

```python
# User commands
self._command_map.update({
    "bounty": self._cmd_bounty,
    "bounties": self._cmd_bounties,
    "events": self._cmd_events,
    "multipliers": self._cmd_events,  # alias
})

# Admin commands (CyTube rank ≥ owner_level)
self._admin_command_map.update({
    "event": self._cmd_event,
    "claim_bounty": self._cmd_claim_bounty,
})
```

---

## 15. Earning Path Integration

### 15.1 Applying Multipliers to Earnings

In the centralized earning/credit path (established in Sprint 1-3), apply the multiplier before crediting:

```python
async def _credit_with_multiplier(
    self, username: str, channel: str, base_amount: float,
    tx_type: str, trigger_id: str, reason: str,
) -> int:
    """Credit Z with active multiplier applied. Returns final amount credited."""
    combined, active_multipliers = self._multiplier_engine.get_combined_multiplier(channel)
    final_amount = base_amount * combined
    
    # Round through fractional accumulator (Sprint 3 pattern)
    whole_z = self._accumulator.add(username, channel, final_amount)
    if whole_z <= 0:
        return 0
    
    # Build multiplier metadata for transaction log
    multiplier_meta = None
    if combined > 1.0:
        multiplier_meta = {
            "base": base_amount,
            "multiplier": combined,
            "sources": [{"source": m.source, "mult": m.multiplier} for m in active_multipliers],
        }
    
    await self._db.credit(
        username, channel, whole_z,
        tx_type=tx_type,
        trigger_id=trigger_id,
        reason=reason,
        metadata=multiplier_meta,  # Logged in transaction for audit
    )
    
    # Also update daily z_earned counter
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await self._db.increment_daily_z_earned(username, channel, today, whole_z)
    
    return whole_z
```

### 15.2 Metadata in Transactions

The `transactions` table's `reason` or a `metadata` JSON column should record the multiplier stack. If the transaction table doesn't yet have a metadata column, add it:

```sql
ALTER TABLE transactions ADD COLUMN metadata TEXT;  -- JSON blob
```

---

## 16. Public Announcements

### 16.1 Announcement Triggers in This Sprint

| Event | Config Gate | Template Key | Variables |
|---|---|---|---|
| Daily competition results | `announcements.daily_champion` | — | Custom in competition engine |
| Scheduled event start | Event's `announce` flag | — | Event name, multiplier, duration |
| Scheduled event end | Event's `announce` flag | — | Event name |
| Ad-hoc event start | Always announced | — | Name, multiplier, duration, admin |
| Ad-hoc event stop | Always announced | — | — |
| Bounty created | Always | — | Creator, description, amount |
| Bounty claimed | Always | — | Winner, description, amount |

### 16.2 All Announcements via kryten-py

```python
await self._client.send_chat(channel, message)
```

No raw NATS publish.

---

## 17. Metrics Extensions

### 17.1 New Metrics

```python
lines.append(f'economy_competition_awards_total {self._app.competition_awards_total}')
lines.append(f'economy_active_multiplier {combined_multiplier}')
lines.append(f'economy_bounties_created_total {self._app.bounties_created_total}')
lines.append(f'economy_bounties_claimed_total {self._app.bounties_claimed_total}')
lines.append(f'economy_bounties_expired_total {self._app.bounties_expired_total}')
lines.append(f'economy_adhoc_events_started_total {self._app.adhoc_events_total}')

# Active multiplier sources gauge
for m in active_multipliers:
    lines.append(f'economy_multiplier_active{{source="{m.source}"}} {m.multiplier}')
```

---

## 18. Test Specifications

### 18.1 Competition Engine Tests (`tests/test_competition_engine.py`)

| Test | Description |
|---|---|
| `test_threshold_all_qualify` | 3 users with gifs ≥ 5 → all 3 awarded |
| `test_threshold_none_qualify` | No users meet threshold → 0 awards |
| `test_threshold_some_qualify` | 1 of 3 meets threshold → 1 awarded |
| `test_daily_top_single_winner` | Top earner gets champion bonus |
| `test_daily_top_percent_reward` | Top earner reward = 25% of z_earned |
| `test_daily_top_no_activity` | No daily_activity → no awards |
| `test_multiple_competitions` | Multiple competitions evaluated in one call |
| `test_pm_sent_per_award` | Each award → PM to winner |
| `test_public_announcement` | Results announced in public chat |
| `test_competition_error_isolated` | One competition error doesn't stop others |

### 18.2 Multiplier Engine Tests (`tests/test_multiplier_engine.py`)

| Test | Description |
|---|---|
| `test_no_multipliers` | Normal time → combined = 1.0, empty list |
| `test_off_peak_active` | During off-peak hours → 2.0× |
| `test_off_peak_inactive` | Outside off-peak → not in list |
| `test_population_active` | 10+ users → 1.5× |
| `test_population_below` | 5 users → not in list |
| `test_holiday_match` | Dec 25 → 3.0× Christmas |
| `test_holiday_no_match` | Regular day → no holiday |
| `test_scheduled_event_active` | Registered event not expired → in list |
| `test_scheduled_event_expired` | Past end_time → auto-cleared |
| `test_adhoc_event_active` | Admin-started event → in list |
| `test_adhoc_event_expired` | Past end_time → auto-cleared |
| `test_stacking_multiplicative` | off_peak 2.0 × population 1.5 = 3.0 |
| `test_hidden_not_shown_in_events_cmd` | Hidden multiplier excluded from display |

### 18.3 Scheduled Event Manager Tests (`tests/test_scheduled_events.py`)

| Test | Description |
|---|---|
| `test_event_start_on_cron` | Cron fires → event registered, multiplier set |
| `test_event_end_after_duration` | Duration elapsed → event cleared, multiplier removed |
| `test_presence_bonus_distributed` | 500 Z split among 5 users → 100 each |
| `test_presence_bonus_zero_users` | No users → no error |
| `test_announcement_on_start` | Public chat message on start |
| `test_announcement_on_end` | Public chat message on end |
| `test_no_refire_same_cycle` | Event doesn't start twice in same cron window |

### 18.4 Bounty System Tests (`tests/test_bounty_manager.py`)

| Test | Description |
|---|---|
| `test_create_success` | Debits creator, creates row, returns ID |
| `test_create_insufficient_funds` | Low balance → rejected |
| `test_create_below_min` | Amount < min → rejected |
| `test_create_above_max` | Amount > max → rejected |
| `test_create_max_open_reached` | Already 3 open → rejected |
| `test_claim_success` | Status → claimed, winner credited, both notified |
| `test_claim_nonexistent` | Invalid ID → error |
| `test_claim_already_claimed` | Double claim → rejected |
| `test_expire_refund` | Past expiry → status expired, 50% refund |
| `test_expire_no_refund_if_zero_percent` | Config refund 0% → no credit |
| `test_bounty_list_open_only` | Only open bounties returned |
| `test_public_announcement_on_create` | Chat message on creation |
| `test_public_announcement_on_claim` | Chat message on claim |

### 18.5 Admin Command Tests (`tests/test_event_admin.py`)

| Test | Description |
|---|---|
| `test_event_start_valid` | Parses args, starts event, announces |
| `test_event_start_bad_multiplier` | Multiplier 0.5 → rejected |
| `test_event_start_bad_duration` | 9999 minutes → rejected |
| `test_event_stop` | Stops active event |
| `test_event_stop_none_active` | No event → message |
| `test_claim_bounty_valid` | Admin claims bounty for user |
| `test_claim_bounty_non_admin` | Rank < 4 → rejected |

### 18.6 Earning Integration Tests (`tests/test_multiplied_earning.py`)

| Test | Description |
|---|---|
| `test_earn_with_2x_multiplier` | Base 5 × 2.0 = 10 Z credited |
| `test_earn_with_stacked_3x` | Base 5 × 3.0 = 15 Z credited |
| `test_earn_no_multiplier` | Base 5 × 1.0 = 5 Z credited |
| `test_multiplier_metadata_logged` | Transaction metadata contains multiplier sources |
| `test_daily_z_earned_updated` | Multiplied amount reflected in daily_activity |

---

## 19. Acceptance Criteria

### Must Pass

- [ ] Daily competition engine evaluates all configured competitions at end-of-day
- [ ] Threshold competitions award all qualifying users
- [ ] Champion (daily_top) competition awards single winner with correct reward
- [ ] Top earner bonus calculates as percentage of daily z_earned
- [ ] Competition awards credited with PM and public summary
- [ ] Multiplier engine returns combined multiplier (multiplicative stacking)
- [ ] Off-peak, population, holiday, scheduled, and ad-hoc multipliers all functional
- [ ] Hidden multipliers not revealed in `events` command
- [ ] Scheduled events start/stop on cron schedule with announcements
- [ ] Presence bonuses distributed at scheduled event start
- [ ] Admin `event start` creates ad-hoc multiplier with public announcement
- [ ] Admin `event stop` clears ad-hoc event
- [ ] Bounty creation debits creator and records bounty
- [ ] Bounty claiming credits winner, notifies both parties, announces publicly
- [ ] Bounty expiry refunds configured percentage to creator
- [ ] Admin gate enforces CyTube rank ≥ `owner_level` for admin commands
- [ ] Earning path applies multiplier before crediting, logs metadata
- [ ] All PMs via `client.send_pm()` — zero raw NATS
- [ ] All announcements via `client.send_chat()` — zero raw NATS
- [ ] All tests pass (~65 test cases)

### Stretch

- [ ] Bounty auto-cancel command for creators (before expiry, partial refund)
- [ ] Multiplier history log viewable by admins
- [ ] Competition tiebreaker rules for daily_top

---

## Appendix A: kryten-py Methods Used in This Sprint

| Method | Usage |
|---|---|
| `client.send_pm(channel, username, message)` | Competition awards, event bonuses, bounty notifications |
| `client.send_chat(channel, message)` | Competition results, event start/end, bounty announcements |
| `client.subscribe_request_reply(subject, handler)` | Extended command handler |

> No direct NATS imports, no raw subject construction, no `client.publish()` with manual subjects.

## Appendix B: Daily Activity Fields Referenced

The competition engine reads from `daily_activity` (populated by Sprint 3):

| Field | Used By |
|---|---|
| `gifs_posted` | gif_enthusiast, gif_champion |
| `kudos_given` | social_butterfly |
| `messages_sent` | chatterbox |
| `unique_emotes_used` | emote_variety |
| `z_earned` | top_earner_bonus |
