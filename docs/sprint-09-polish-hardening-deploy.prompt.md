# Sprint 9 — Public Announcements, Polish & Hardening

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 9 of 9  
> **Goal:** Production-ready service — centralized event announcer, custom greeting execution, error hardening, performance profiling, PM rate limiting, integration tests, and deployment artifacts.  
> **Depends on:** All prior sprints (1–8)  
> **Enables:** Production deployment

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [Event Announcer](#2-event-announcer)
3. [Custom Greeting Execution](#3-custom-greeting-execution)
4. [Error Hardening](#4-error-hardening)
5. [Performance Profiling & Optimization](#5-performance-profiling--optimization)
6. [PM Rate Limiting](#6-pm-rate-limiting)
7. [Integration Tests](#7-integration-tests)
8. [Deployment Artifacts](#8-deployment-artifacts)
9. [Final Audit Checklist](#9-final-audit-checklist)
10. [Test Specifications](#10-test-specifications)
11. [Acceptance Criteria](#11-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint:

- **Event announcer** — centralized announcement engine with configurable templates, deduplication, and batching to avoid chat spam
- **Custom greeting execution** — on genuine `adduser` (debounced with `greeting_absence_minutes`), posts custom greetings in public chat with batch delay to avoid spam on simultaneous joins
- **Error hardening** — graceful handling of NATS reconnection (kryten-py handles this), SQLite contention, MediaCMS timeouts, malformed commands, and balance race conditions (atomic debit-or-fail)
- **Performance** — profiled presence tick at 100+ users, batch SQLite writes if needed, connection pooling review
- **PM rate limiting** — configurable max commands/minute per user, abuse-resistant
- **Integration tests** — full end-to-end with `MockKrytenClient`: join → earn → chat → gamble → queue → tip → rank up → achievement → event → admin
- **Deployment artifacts** — `systemd/kryten-economy.service`, polished `config.example.yaml` with comprehensive inline docs, `README.md` with setup/operation guide
- Service is production-ready with all 9 sprints integrated and hardened

> **⚠️ Ecosystem rule:** All NATS interaction goes through kryten-py's `KrytenClient`. Use `client.send_pm()`, `client.send_chat()` — never raw NATS subjects.

---

## 2. Event Announcer

### 2.1 File: `kryten_economy/event_announcer.py`

### 2.2 Class: `EventAnnouncer`

Centralized public chat announcements. Prior sprints called `client.send_chat()` directly — Sprint 9 introduces a centralized layer with deduplication, batching, and template rendering.

```python
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any


class EventAnnouncer:
    """Centralized announcement engine for public chat messages.
    
    Features:
    - Configurable templates with variable substitution
    - Deduplication (suppress identical messages within a window)
    - Batch delay (group rapid-fire announcements)
    - Rate limiting (max messages/minute to chat)
    """

    def __init__(self, config, client, logger: logging.Logger):
        self._config = config
        self._client = client
        self._logger = logger
        self._recent: deque[tuple[str, float]] = deque(maxlen=100)  # (hash, timestamp)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()  # (channel, message)
        self._flush_task: asyncio.Task | None = None
        self._max_per_minute = 10  # Hard cap on public messages/minute
        self._batch_delay_seconds = 2.0  # Brief delay to batch rapid announcements
        self._dedup_window_seconds = 30.0  # Suppress identical messages within window

    async def start(self) -> None:
        """Start the announcement flush loop."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop the flush loop."""
        if self._flush_task:
            self._flush_task.cancel()

    async def announce(
        self, channel: str, template_key: str, variables: dict[str, Any],
        fallback: str | None = None,
    ) -> None:
        """Queue a public announcement.
        
        Args:
            channel: Target channel
            template_key: Key in config.announcements.templates
            variables: Template variables for .format()
            fallback: Fallback message if template not found
        """
        # Check if this announcement type is enabled
        gate_attr = template_key  # e.g., "queue_purchase", "gambling_jackpot"
        if hasattr(self._config.announcements, gate_attr):
            if not getattr(self._config.announcements, gate_attr):
                return  # This announcement type is disabled

        # Render template
        template = self._config.announcements.templates.get(template_key, fallback or "")
        if not template:
            return
        
        try:
            message = template.format(**variables)
        except (KeyError, IndexError) as e:
            self._logger.warning("Template render failed for '%s': %s", template_key, e)
            return

        # Deduplication check
        msg_hash = hash((channel, message))
        now = datetime.now(timezone.utc).timestamp()
        if any(h == msg_hash and now - t < self._dedup_window_seconds for h, t in self._recent):
            self._logger.debug("Deduped announcement: %s", message[:50])
            return
        self._recent.append((msg_hash, now))

        # Queue for batched delivery
        await self._queue.put((channel, message))

    async def announce_raw(self, channel: str, message: str) -> None:
        """Queue a raw announcement (no template, still subject to dedup/batching)."""
        msg_hash = hash((channel, message))
        now = datetime.now(timezone.utc).timestamp()
        if any(h == msg_hash and now - t < self._dedup_window_seconds for h, t in self._recent):
            return
        self._recent.append((msg_hash, now))
        await self._queue.put((channel, message))

    async def _flush_loop(self) -> None:
        """Drain the announcement queue with rate limiting."""
        sent_this_minute = 0
        minute_start = datetime.now(timezone.utc).timestamp()

        while True:
            try:
                channel, message = await asyncio.wait_for(
                    self._queue.get(), timeout=self._batch_delay_seconds,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            now = datetime.now(timezone.utc).timestamp()

            # Reset minute counter
            if now - minute_start >= 60:
                sent_this_minute = 0
                minute_start = now

            # Rate limit
            if sent_this_minute >= self._max_per_minute:
                self._logger.warning("Announcement rate limit reached, dropping: %s", message[:50])
                continue

            # Small batch delay to coalesce rapid-fire announcements
            await asyncio.sleep(self._batch_delay_seconds)

            # Send via kryten-py
            try:
                await self._client.send_chat(channel, message)
                sent_this_minute += 1
            except Exception as e:
                self._logger.error("Announcement send failed: %s", e)
```

### 2.3 Migration from Direct `send_chat` Calls

Prior sprints called `self._client.send_chat()` directly. Sprint 9 refactors these to go through the announcer:

```python
# Before (sprints 4-8):
await self._client.send_chat(channel, f"🎰 JACKPOT! {user} won {amount:,} Z!")

# After (Sprint 9):
await self._announcer.announce(channel, "jackpot", {"user": user, "amount": f"{amount:,}"})
```

This is a **refactor, not a behavior change**. All existing announcement points are updated to use the announcer. Direct `client.send_chat()` is still used for admin `announce` command (bypasses templates/dedup).

---

## 3. Custom Greeting Execution

### 3.1 File: `kryten_economy/greeting_handler.py`

### 3.2 Class: `GreetingHandler`

Handles posting custom greetings in public chat when a user joins:

```python
import asyncio
import logging
from datetime import datetime, timezone


class GreetingHandler:
    """Posts custom greetings on genuine user arrivals."""

    def __init__(self, config, database, presence_tracker, announcer, logger: logging.Logger):
        self._config = config
        self._db = database
        self._presence = presence_tracker
        self._announcer = announcer
        self._logger = logger
        self._pending_greetings: list[tuple[str, str, str]] = []  # [(channel, username, greeting)]
        self._batch_task: asyncio.Task | None = None
        self._batch_delay = 3.0  # Seconds to wait before flushing batch

    async def on_user_join(self, channel: str, username: str) -> None:
        """Called on adduser event AFTER presence_tracker.handle_user_join()
        confirms a genuine arrival (join_debounce_minutes).
        
        Applies the ADDITIONAL greeting_absence_minutes threshold — greetings
        require a longer absence than the debounce window. Sprint 1 defines
        is_genuine_arrival() using join_debounce_minutes (5 min default).
        Greetings use greeting_absence_minutes (30 min default) to avoid
        spamming on frequent but genuine short visits.
        """
        if not self._config.announcements.custom_greeting:
            return

        # Check the GREETING-specific absence threshold (longer than debounce).
        # Sprint 1's presence tracker stores departure times in _last_departure dict.
        # We check that threshold here rather than inventing a new presence method.
        greeting_threshold_minutes = self._config.presence.greeting_absence_minutes
        if not self._presence.was_absent_longer_than(username, channel, greeting_threshold_minutes):
            return

        # Use Sprint 5's dedicated shortcut (not the generic get_vanity_item)
        greeting = await self._db.get_custom_greeting(username, channel)
        if not greeting:
            return

        # Queue greeting for batched delivery
        self._pending_greetings.append((channel, username, greeting))

        # Start or reset batch timer
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()
        self._batch_task = asyncio.create_task(self._flush_greetings())

    async def _flush_greetings(self) -> None:
        """Wait briefly then post all pending greetings."""
        await asyncio.sleep(self._batch_delay)

        if not self._pending_greetings:
            return

        # Group by channel
        by_channel: dict[str, list[tuple[str, str]]] = {}
        for channel, username, greeting in self._pending_greetings:
            by_channel.setdefault(channel, []).append((username, greeting))
        self._pending_greetings.clear()

        for channel, greetings in by_channel.items():
            if len(greetings) == 1:
                username, greeting = greetings[0]
                template = self._config.announcements.templates.get(
                    "greeting", "👋 {greeting}"
                )
                msg = template.format(greeting=greeting, user=username)
                await self._announcer.announce_raw(channel, msg)
            else:
                # Multiple joins within batch window — combine to reduce spam
                msgs = []
                for username, greeting in greetings:
                    msgs.append(f"👋 {greeting}")
                combined = " | ".join(msgs)
                await self._announcer.announce_raw(channel, combined)
```

### 3.3 Required Addition to `PresenceTracker`

Sprint 9 requires one new method on the presence tracker (extending the Sprint 1 class):

```python
def was_absent_longer_than(self, username: str, channel: str, minutes: int) -> bool:
    """Return True if the user was absent for at least `minutes` minutes.
    
    Similar to is_genuine_arrival() but with a caller-specified threshold
    instead of using join_debounce_minutes. Used by GreetingHandler to
    apply the longer greeting_absence_minutes threshold.
    
    Returns True if:
    - No departure record exists (truly new or gone very long)
    - Time since last departure >= minutes
    """
    key = (username.lower(), channel)
    departure_time = self._last_departure.get(key)
    if departure_time is None:
        return True  # No record = long absence
    threshold = timedelta(minutes=minutes)
    return datetime.now(timezone.utc) - departure_time >= threshold
```

### 3.4 Integration Point

In the orchestrator's `@client.on("adduser")` handler (extending Sprint 5's pattern from Section 10.2):

```python
@client.on("adduser")
async def handle_join(event):
    username = event.username
    channel = event.channel
    is_genuine = await presence_tracker.handle_user_join(username, channel)
    if is_genuine:
        await _maybe_send_welcome_wallet(username, channel)   # Sprint 2
        await greeting_handler.on_user_join(channel, username)  # Sprint 9 (replaces Sprint 5's inline _maybe_send_custom_greeting)
```

> **Note:** Sprint 5's Section 10 defined an inline `_maybe_send_custom_greeting()` that called `client.send_chat()` directly. Sprint 9 replaces this with the `GreetingHandler` class which routes through the `EventAnnouncer` for dedup/batching.

---

## 4. Error Hardening

### 4.1 NATS Reconnection

kryten-py handles NATS reconnection **entirely internally** via a private `_on_reconnected` callback registered with the NATS library. There is **no user-facing event** for reconnection — `@client.on()` only handles CyTube events (chatmsg, pm, adduser, etc.), not NATS connection lifecycle.

The economy service needs to:
- **Not crash** on transient NATS disconnects — kryten-py buffers and retries automatically
- **Not re-subscribe** — kryten-py restores all subscriptions on reconnect
- **Not register any reconnection handler** — there is no `@client.on("nats_reconnect")` event

> **⚠️ Do NOT fabricate `@client.on("nats_reconnect")`** — this event does not exist in kryten-py's `on()` decorator system. Any such handler would silently never fire.

Reconnection is observable only through kryten-py's internal logger (`"Reconnected to NATS"`). If the economy service needs to detect prolonged disconnection, use a heartbeat-based approach:

```python
# In the periodic presence tick (runs every 60s):
# If the tick itself fails because kryten-py can't publish, catch and log.
# kryten-py will raise PublishError on send_chat/send_pm if disconnected.
async def _presence_tick(self) -> None:
    try:
        # ... credit presence ...
        pass
    except Exception as e:
        self._logger.warning("Presence tick failed (possible NATS disconnect): %s", e)
        # Next tick will retry. kryten-py reconnects automatically.
```

### 4.2 SQLite Contention

With `run_in_executor` pattern, SQLite writes serialize through the executor. Additional hardening:

```python
import sqlite3

def _get_connection(self) -> sqlite3.Connection:
    """Get a new SQLite connection with hardened settings."""
    conn = sqlite3.connect(self._db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s for locks
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL = good balance
    conn.row_factory = sqlite3.Row
    return conn
```

### 4.3 MediaCMS Timeout Handling

```python
async def _mediacms_request(self, endpoint: str, **kwargs) -> dict | None:
    """Make a MediaCMS API request with timeout and retry."""
    timeout = aiohttp.ClientTimeout(total=10)
    for attempt in range(3):
        try:
            async with self._session.get(
                f"{self._base_url}/{endpoint}",
                headers=self._headers,
                timeout=timeout,
                **kwargs,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                self._logger.warning(
                    "MediaCMS %s returned %d (attempt %d)",
                    endpoint, resp.status, attempt + 1,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._logger.warning(
                "MediaCMS request failed: %s (attempt %d)", e, attempt + 1,
            )
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return None
```

### 4.4 Malformed Command Handling

```python
async def _dispatch_command(self, username, channel, command, args, rank):
    try:
        result = await self._do_dispatch(username, channel, command, args, rank)
        if result:
            await self._client.send_pm(channel, username, result)
    except Exception as e:
        self._logger.error(
            "Command handler error for %s/%s: %s",
            username, command, e, exc_info=True,
        )
        await self._client.send_pm(
            channel, username,
            "❌ Something went wrong processing your command. Please try again."
        )
```

### 4.5 Atomic Debit-or-Fail

Introduced in Sprint 4 alongside the `GamblingEngine`. Sprint 1's `debit()` method returns `int | None` and is used for simple balance operations. `atomic_debit()` returns `bool` and is the canonical pattern for any debit-then-act flow (gambling wagers, spending, bounties). Sprint 9 audits all spending paths to confirm they use this pattern:

```python
async def atomic_debit(self, username, channel, amount, *, tx_type="debit", trigger_id=None, reason=None) -> bool:
    """Debit if and only if balance >= amount. Returns True if successful.
    
    Uses a single SQL transaction to prevent race conditions.
    """
    def _sync():
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "UPDATE accounts SET balance = balance - ? "
                "WHERE username = ? AND channel = ? AND balance >= ?",
                (amount, username, channel, amount),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO transactions (username, channel, amount, type, trigger_id, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, channel, -amount, tx_type, trigger_id, reason),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await asyncio.get_running_loop().run_in_executor(None, _sync)
```

> **⚠️ Modernization note:** Sprint 1 established the `run_in_executor` pattern using `asyncio.get_event_loop()`. This is deprecated in Python 3.10+ and emits `DeprecationWarning` in 3.12+. Sprint 9 migrates **all** executor calls to `asyncio.get_running_loop().run_in_executor()`. Audit every `get_event_loop()` call in the codebase during this sprint.

### 4.6 Event Handler Isolation

Every `@client.on()` handler wraps its body in try/except to prevent one bad event from crashing the service:

```python
@client.on("chatmsg")
async def on_chatmsg(event):
    try:
        # ... process chat message ...
    except Exception as e:
        logger.error("chatmsg handler error: %s", e, exc_info=True)
        # Do NOT re-raise — other events must continue processing
```

---

## 5. Performance Profiling & Optimization

### 5.1 Presence Tick at Scale

Profile the presence tick with 100+ simulated users:

```python
async def _presence_tick(self) -> None:
    """Credit presence Z to all connected users. Runs every 60s."""
    start = time.monotonic()
    
    for channel in self._active_channels():
        users = self._presence.get_present_users(channel)
        
        # Batch credit: collect all credits, write in a single transaction
        credits = []
        for username in users:
            amount = self._calculate_presence_earning(username, channel)
            if amount > 0:
                credits.append((username, channel, amount))
        
        if credits:
            await self._db.batch_credit_presence(credits)
    
    elapsed = time.monotonic() - start
    if elapsed > 5.0:
        self._logger.warning("Presence tick took %.2fs for %d users", elapsed, len(users))
```

### 5.2 Batch SQLite Writes

```python
async def batch_credit_presence(self, credits: list[tuple[str, str, int]]) -> None:
    """Batch-credit presence Z in a single transaction.
    
    Args:
        credits: [(username, channel, amount), ...]
    """
    def _sync():
        conn = self._get_connection()
        try:
            for username, channel, amount in credits:
                conn.execute(
                    "UPDATE accounts SET balance = balance + ?, lifetime_earned = lifetime_earned + ? "
                    "WHERE username = ? AND channel = ?",
                    (amount, amount, username, channel),
                )
                conn.execute(
                    "INSERT INTO transactions (username, channel, amount, type, trigger_id, reason) "
                    "VALUES (?, ?, ?, 'presence', 'presence.base', 'Presence earning')",
                    (username, channel, amount),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _sync)
```

### 5.3 Performance Targets

| Operation | Target | Mitigation if Exceeded |
|---|---|---|
| Presence tick (100 users) | < 2 seconds | Batch SQLite writes |
| Presence tick (500 users) | < 10 seconds | Batch writes + reduce per-user queries |
| PM command response | < 500ms | Cache account lookups |
| Achievement check | < 200ms | Skip already-awarded via indexed query |
| Metrics endpoint | < 1 second | Cache aggregate queries for 30s |

---

## 6. PM Rate Limiting

### 6.1 Implementation

```python
from collections import defaultdict
from datetime import datetime, timezone


class PmRateLimiter:
    """Rate limit PM commands per user."""

    def __init__(self, max_per_minute: int = 10):
        self._max = max_per_minute
        self._counters: dict[str, list[float]] = defaultdict(list)

    def check(self, username: str) -> bool:
        """Returns True if the command should be allowed."""
        now = datetime.now(timezone.utc).timestamp()
        window = self._counters[username]
        
        # Prune old entries
        cutoff = now - 60
        self._counters[username] = [t for t in window if t > cutoff]
        
        if len(self._counters[username]) >= self._max:
            return False
        
        self._counters[username].append(now)
        return True

    def cleanup(self) -> None:
        """Remove stale entries (call periodically)."""
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - 120
        stale = [k for k, v in self._counters.items() if all(t < cutoff for t in v)]
        for k in stale:
            del self._counters[k]
```

### 6.2 Integration

```python
# In PmHandler.__init__():
# Config is a Pydantic model — use attribute access, not dict-style .get()
self._rate_limiter = PmRateLimiter(max_per_minute=config.commands.rate_limit_per_minute)

async def _on_pm(self, event):
    if not self._rate_limiter.check(event.username):
        await self._client.send_pm(
            event.channel, event.username,
            "⏳ Slow down! Try again in a moment."
        )
        return
    # ... dispatch command ...
```

---

## 7. Integration Tests

### 7.1 MockKrytenClient

```python
class MockKrytenClient:
    """Mock kryten-py client for integration testing.
    
    Records all method calls for assertion.
    """

    def __init__(self):
        self.sent_pms: list[tuple[str, str, str]] = []  # (channel, username, message)
        self.sent_chats: list[tuple[str, str]] = []      # (channel, message)
        self.rank_changes: list[tuple[str, str, int]] = []  # (channel, username, rank)
        self.media_adds: list[dict] = []
        self._handlers: dict[str, list] = {}
        self._request_reply_handlers: dict[str, Any] = {}
        self._kv_store: dict[str, dict[str, Any]] = {}  # bucket → {key: value}

    async def send_pm(self, channel: str, username: str, message: str, *, domain=None) -> str:
        self.sent_pms.append((channel, username, message))
        return "mock-correlation-id"

    async def send_chat(self, channel: str, message: str, *, domain=None) -> str:
        self.sent_chats.append((channel, message))
        return "mock-correlation-id"

    async def safe_set_channel_rank(self, channel, username, rank, *, domain=None, check_rank=True, timeout=2.0) -> dict:
        self.rank_changes.append((channel, username, rank))
        return {"success": True}

    async def add_media(self, channel, media_type, media_id, *, position="end", temp=True, domain=None) -> str:
        self.media_adds.append({"channel": channel, "media_type": media_type, "media_id": media_id, "position": position, "temp": temp})
        return "mock-correlation-id"

    async def kv_get(self, bucket_name, key, default=None, parse_json=False):
        return self._kv_store.get(bucket_name, {}).get(key, default)

    async def kv_put(self, bucket_name, key, value, *, as_json=False):
        self._kv_store.setdefault(bucket_name, {})[key] = value

    async def nats_request(self, subject, request, timeout=5):
        # Stub: return empty response
        return {}

    def on(self, event_name, channel=None, domain=None):
        """Match kryten-py's on() signature: on(event_name, channel=None, domain=None)."""
        def decorator(func):
            self._handlers.setdefault(event_name, []).append(func)
            return func
        return decorator

    async def subscribe_request_reply(self, subject, handler):
        self._request_reply_handlers[subject] = handler

    async def fire_event(self, event_name, event):
        """Test helper: simulate an incoming event."""
        for handler in self._handlers.get(event_name, []):
            await handler(event)
```

### 7.2 End-to-End Scenario Tests

```python
# tests/test_integration.py

class TestFullLifecycle:
    """End-to-end integration tests using MockKrytenClient."""

    async def test_join_earn_check_balance(self):
        """User joins → accumulates presence Z → checks balance."""
        # Simulate adduser event
        # Wait for presence tick
        # Simulate PM: "balance"
        # Assert PM response contains balance > 0

    async def test_chat_triggers_earn(self):
        """User sends long message → earns chat bonus."""
        # Simulate chatmsg with 50+ char message
        # Assert balance increased by trigger amount

    async def test_streak_and_milestone(self):
        """User present for 2 consecutive days → streak bonus."""
        # Day 1: simulate presence, end-of-day processing
        # Day 2: simulate presence, end-of-day processing
        # Assert streak bonus credited

    async def test_gambling_cycle(self):
        """User earns → gambles → wins/loses → checks stats."""
        # Seed balance
        # Simulate PM: "flip 100"
        # Assert balance changed, gambling stats updated

    async def test_queue_media(self):
        """User searches → queues → balance decremented."""
        # Seed balance with enough Z
        # Simulate PM: "search test"
        # Simulate PM: "queue <id>"
        # Assert client.add_media called, balance decremented

    async def test_tip_cycle(self):
        """User A tips User B → both get PMs."""
        # Seed both accounts
        # Simulate PM from A: "tip @B 50"
        # Assert A debited, B credited, both get PMs

    async def test_rank_promotion(self):
        """User earns enough to rank up → PM + announcement."""
        # Set user's lifetime_earned just below Grip threshold
        # Credit enough to cross threshold
        # Assert rank_engine promoted, PM sent, announcement made

    async def test_achievement_award(self):
        """User hits 100 messages → achievement awarded."""
        # Set lifetime_messages to 99
        # Simulate one more chat message
        # Assert achievement awarded, reward credited, PM sent

    async def test_competition_evaluation(self):
        """End-of-day → competitions evaluated → awards distributed."""
        # Seed daily_activity with qualifying data
        # Run competition engine
        # Assert awards credited, PMs sent, announcement made

    async def test_multiplier_application(self):
        """Active multiplier → earnings boosted."""
        # Activate an ad-hoc 2x event
        # Credit base 5 Z
        # Assert 10 Z credited with metadata

    async def test_bounty_lifecycle(self):
        """Create → list → claim → winner gets paid."""
        # Seed balance
        # Create bounty
        # List bounties → see it
        # Admin claims → winner credited, creator notified

    async def test_admin_commands(self):
        """Admin grants/deducts/bans/unbans."""
        # Simulate admin PM: "grant @user 1000"
        # Assert user credited
        # Simulate admin PM: "ban @user"
        # Assert user cannot earn

    async def test_config_reload(self):
        """Admin reloads config → new values applied."""
        # Write modified config file
        # Simulate admin PM: "reload"
        # Assert new config applied

    async def test_rate_limiting(self):
        """Rapid PM commands → rate limited after threshold."""
        # Send 15 commands in rapid succession
        # Assert first 10 processed, rest rate-limited

    async def test_full_flow(self):
        """Complete lifecycle: join → earn → chat → gamble → queue → tip → rank up."""
        # This is the comprehensive flow test combining all the above
```

---

## 8. Deployment Artifacts

### 8.1 `systemd/kryten-economy.service`

```ini
[Unit]
Description=kryten-economy — Channel engagement currency system
After=network-online.target nats-server.service
Wants=network-online.target nats-server.service
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
Type=simple
User=kryten
Group=kryten
WorkingDirectory=/opt/kryten/economy
ExecStart=/opt/kryten/economy/.venv/bin/python -m kryten_economy
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kryten-economy

# Resource limits
MemoryMax=512M
CPUQuota=50%

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/kryten/economy
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 8.2 `README.md`

```markdown
# kryten-economy

Channel engagement currency system for CyTube, built on the kryten ecosystem.

## Overview

kryten-economy rewards users with Z-Coins for channel presence, chat activity,
and social interaction. Users can spend Z on queueing content, vanity items,
and gambling. Named B-movie-themed ranks provide real perks (discounts, extra
queue slots). Fully configurable via YAML.

## Requirements

- Python 3.11+
- NATS server (via kryten infrastructure)
- kryten-py >= 0.11.5
- kryten-robot (event source)
- kryten-userstats (alias resolution)
- Optional: MediaCMS instance (for content queue)

## Setup

1. Clone and install:
   ```bash
   cd /opt/kryten/economy
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

2. Copy and configure:
   ```bash
   cp config.example.yaml config.yaml
   # Edit config.yaml with your settings
   ```

3. Deploy:
   ```bash
   sudo cp systemd/kryten-economy.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now kryten-economy
   ```

## Configuration

See `config.example.yaml` for the complete reference with inline documentation.
Every rate, threshold, reward, cost, and behavior is tunable without code changes.

## PM Commands (User)

| Command | Description |
|---|---|
| `help` | Brief command overview |
| `balance` / `bal` | Current balance, rank, streak |
| `rank` | Rank progress and perks |
| `profile` | Full user view |
| `achievements` | Earned badges and progress |
| `top` / `leaderboard` | Leaderboards |
| `search <query>` | Search MediaCMS catalog |
| `queue <id>` | Queue content |
| `tip @user <amount>` | Transfer Z |
| `shop` | Vanity items |
| `buy <item>` | Purchase vanity item |
| `spin [wager]` | Slot machine |
| `flip <wager>` | Coin flip |
| `challenge @user <wager>` | Duel |
| `bounty <amount> "<desc>"` | Create bounty |
| `bounties` | List open bounties |
| `events` | Active multipliers |

## PM Commands (Admin — CyTube Rank ≥ 4)

| Command | Description |
|---|---|
| `grant @user <amount>` | Credit Z |
| `deduct @user <amount>` | Debit Z |
| `rain <amount>` | Distribute Z to present users |
| `set_balance @user <amount>` | Hard-set balance |
| `set_rank @user <rank>` | Override rank |
| `ban @user` / `unban @user` | Economy access control |
| `reload` | Hot-reload config |
| `econ:stats` | Economy overview |
| `econ:user <name>` | User inspection |
| `econ:health` | Inflation indicators |
| `econ:triggers` | Trigger analytics |
| `econ:gambling` | Gambling stats |
| `event start/stop` | Ad-hoc multiplier events |
| `claim_bounty <id> @user` | Award bounty |
| `announce <message>` | Public chat message |

## Monitoring

Prometheus metrics at `http://localhost:28286/metrics`.

## Architecture

See `kryten-economy-plan.md` for the full implementation plan.
```

### 8.3 `config.example.yaml` Audit

Sprint 9 reviews and polishes the example config created in Sprint 1, ensuring:
- Every field has an inline comment explaining its purpose
- Default values are production-sensible
- All sprint-added config sections are present and documented
- Sensitive fields (API tokens) are clearly marked as placeholders

---

## 9. Final Audit Checklist

### 9.1 kryten-py Conformance

| Check | Status |
|---|---|
| All event handlers use `@client.on()` decorators | ✅ Verify |
| All PMs sent via `client.send_pm()` | ✅ Verify |
| All chat sent via `client.send_chat()` | ✅ Verify |
| All CyTube rank changes via `client.safe_set_channel_rank()` | ✅ Verify |
| All media adds via `client.add_media()` | ✅ Verify |
| All KV access via `client.kv_get()` / `client.kv_put()` | ✅ Verify |
| All request-reply via `client.subscribe_request_reply()` | ✅ Verify |
| All inter-service calls via `client.nats_request()` | ✅ Verify |
| Zero `import nats` statements | ✅ Verify |
| Zero `client.publish()` with raw subjects | ✅ Verify |
| Zero manually constructed NATS subjects | ✅ Verify |

### 9.2 Database Integrity

| Check | Status |
|---|---|
| All tables have proper indexes | ✅ Verify |
| All spending uses `atomic_debit` | ✅ Verify |
| WAL mode enabled on all connections | ✅ Verify |
| `busy_timeout` set on all connections | ✅ Verify |
| No balance can go negative | ✅ Verify |

### 9.3 Error Resilience

| Check | Status |
|---|---|
| Every `@client.on()` handler has try/except | ✅ Verify |
| MediaCMS requests have timeout + retry | ✅ Verify |
| Malformed PM commands return helpful error | ✅ Verify |
| Unknown commands silently ignored | ✅ Verify |
| Config reload failure preserves old config | ✅ Verify |

### 9.4 Anti-Abuse

| Check | Status |
|---|---|
| PM rate limiting enforced | ✅ Verify |
| Join debounce prevents WS bounce exploitation | ✅ Verify |
| Ignored users list filters at event ingestion | ✅ Verify |
| Self-tip blocked (alias-aware) | ✅ Verify |
| Self-kudos/laugh excluded | ✅ Verify |
| All hourly/daily caps enforced per trigger | ✅ Verify |
| Economy ban prevents all earning/spending | ✅ Verify |

---

## 10. Test Specifications

### 10.1 Event Announcer Tests (`tests/test_event_announcer.py`)

| Test | Description |
|---|---|
| `test_template_rendering` | Variables substituted correctly |
| `test_missing_template` | Missing key → no announcement |
| `test_disabled_announcement` | Config gate off → suppressed |
| `test_deduplication` | Same message within 30s → only first sent |
| `test_rate_limiting` | 15 rapid announcements → max 10 sent |
| `test_batch_delay` | Messages delayed by batch window |
| `test_raw_announcement` | `announce_raw` bypasses templates |

### 10.2 Greeting Handler Tests (`tests/test_greeting_handler.py`)

| Test | Description |
|---|---|
| `test_genuine_arrival_with_greeting` | Absent > threshold → greeting posted |
| `test_bounce_no_greeting` | WS bounce (absent < threshold) → no greeting |
| `test_no_custom_greeting` | No vanity item → no greeting |
| `test_disabled_greetings` | Config off → no greeting |
| `test_batch_simultaneous_joins` | 3 joins within 3s → combined greeting |

### 10.3 Error Hardening Tests (`tests/test_error_hardening.py`)

| Test | Description |
|---|---|
| `test_malformed_command_no_crash` | Bad args → error PM, service continues |
| `test_event_handler_isolation` | Exception in chatmsg handler → adduser still works |
| `test_mediacms_timeout_retry` | Timeout → retries up to 3 times |
| `test_mediacms_all_retries_fail` | 3 failures → returns None, no crash |
| `test_atomic_debit_race` | Concurrent debits → only one succeeds |
| `test_sqlite_busy_waits` | Busy connection waits up to timeout |

### 10.4 Performance Tests (`tests/test_performance.py`)

| Test | Description |
|---|---|
| `test_presence_tick_100_users` | Completes in < 2 seconds |
| `test_presence_tick_500_users` | Completes in < 10 seconds |
| `test_batch_credit_efficiency` | Batch write faster than individual writes |
| `test_command_response_latency` | PM command → response in < 500ms |

### 10.5 Rate Limiter Tests (`tests/test_rate_limiter.py`)

| Test | Description |
|---|---|
| `test_within_limit` | 5 commands → all allowed |
| `test_exceeds_limit` | 15 commands → first 10 allowed, rest blocked |
| `test_window_reset` | Wait 60s → limit resets |
| `test_per_user_isolation` | User A rate-limited, User B unaffected |
| `test_cleanup` | Stale entries removed |

### 10.6 Integration Tests (`tests/test_integration.py`)

See Section 7.2 — approximately 15 comprehensive end-to-end scenario tests.

### 10.7 Deployment Tests (`tests/test_deployment.py`)

| Test | Description |
|---|---|
| `test_config_example_valid` | `config.example.yaml` parses without errors |
| `test_config_example_all_sections` | All expected sections present |
| `test_systemd_unit_syntax` | Service file has required sections |

---

## 11. Acceptance Criteria

### Must Pass

- [ ] Event announcer delivers templated messages with dedup and rate limiting
- [ ] Custom greetings post on genuine arrivals (debounced)
- [ ] Multiple simultaneous joins batch greetings to reduce spam
- [ ] All `@client.on()` handlers have try/except isolation
- [ ] MediaCMS requests retry on timeout/error
- [ ] Malformed commands return helpful error messages
- [ ] Atomic debit prevents negative balances under concurrency
- [ ] Presence tick completes in < 2s for 100 users
- [ ] Batch SQLite writes used for presence crediting
- [ ] PM rate limiter enforces configurable max/minute
- [ ] Rate-limited users get "slow down" message
- [ ] All integration tests pass (15+ end-to-end scenarios)
- [ ] `MockKrytenClient` supports all kryten-py methods used across sprints
- [ ] systemd service file deployable
- [ ] `config.example.yaml` valid and comprehensively documented
- [ ] `README.md` covers setup, commands, and monitoring
- [ ] **Zero `import nats` in entire codebase** — verified by grep
- [ ] **Zero raw NATS subject construction** — verified by grep
- [ ] All tests pass (total across all sprints: ~400+ test cases)
- [ ] Service starts, connects, earns, spends, gambles, promotes, announces — full lifecycle

### Stretch

- [ ] Load testing with 500+ simulated users
- [ ] Sentry/error reporting integration
- [ ] Docker deployment alternative
- [ ] Hot migration script from previous data formats

---

## Appendix A: kryten-py Methods Used Across All Sprints

| Method | Sprints |
|---|---|
| `@client.on("chatmsg")` | 1, 3, 5 |
| `@client.on("pm")` | 1 |
| `@client.on("adduser")` | 1, 2, 9 |
| `@client.on("userleave")` | 1 |
| `@client.on("changemedia")` | 3 |
| `@client.on("playlist")` | 3 |
| `@client.on("setafk")` (RawEvent) | 1 |
| `client.send_pm(channel, username, message, *, domain) -> str` | 1–9 |
| `client.send_chat(channel, message, *, domain) -> str` | 2, 4–9 |
| `client.add_media(channel, media_type, media_id, ...)` | 5 |
| `client.safe_set_channel_rank(channel, username, rank, *, domain=None, check_rank=True, timeout=2.0)` | 6 |
| `client.kv_get(bucket_name, key, default, parse_json)` | 3 |
| `client.kv_put(bucket_name, key, value, as_json)` | 3 |
| `client.get_or_create_kv_store(bucket_name, description)` | 1 |
| `client.get_state_current_media(channel)` | 3 |
| `client.subscribe_request_reply(subject, handler)` | 1, 5, 6, 7, 8 |
| `client.nats_request(subject, request, timeout)` | 5 (alias resolution) |
| `client.subscribe(subject, handler)` | 1 (lifecycle startup) |
| `client.on_group_restart(callback)` | 1 |

> **No direct NATS imports, no raw subject construction anywhere in the codebase.** `client.publish()` is a valid kryten-py method but is not currently used by kryten-economy; prefer `subscribe_request_reply` or `nats_request` for inter-service communication.

## Appendix B: Complete File Structure

```
kryten-economy/
├── pyproject.toml
├── README.md
├── config.example.yaml
├── systemd/
│   └── kryten-economy.service
├── kryten_economy/
│   ├── __init__.py
│   ├── __main__.py
│   ├── main.py                      # EconomyApp orchestrator
│   ├── config.py                    # Pydantic config models
│   ├── database.py                  # EconomyDatabase (SQLite WAL)
│   ├── presence_tracker.py          # User session tracking, join debounce
│   ├── pm_handler.py                # PM command dispatch + rate limiting
│   ├── command_handler.py           # NATS request-reply handler
│   ├── earning_engine.py            # Centralized earning with multiplier
│   ├── spending_engine.py           # Spending validation + rank discounts
│   ├── streak_engine.py             # Daily streaks, milestones, bridge bonus
│   ├── chat_trigger_engine.py       # 12 chat earning triggers
│   ├── gambling_engine.py           # Slots, flip, challenge, heist
│   ├── media_queue.py               # MediaCMS search + queue commands
│   ├── tipping.py                   # Tip transfers (alias-aware)
│   ├── vanity_shop.py               # 7 vanity items
│   ├── achievement_engine.py        # One-time achievement badges
│   ├── rank_engine.py               # B-movie rank progression + perks
│   ├── multiplier_engine.py         # Active multiplier stack
│   ├── competition_engine.py        # Daily competition evaluation
│   ├── scheduled_event_manager.py   # Cron-based events
│   ├── bounty_manager.py            # User-created bounties
│   ├── event_announcer.py           # Centralized public announcements
│   ├── greeting_handler.py          # Custom greeting execution
│   ├── metrics_server.py            # Prometheus HTTP endpoint
│   └── utils.py                     # Shared utilities
└── tests/
    ├── conftest.py                  # Fixtures, MockKrytenClient
    ├── test_database.py
    ├── test_presence.py
    ├── test_pm_handler.py
    ├── test_earning.py
    ├── test_spending.py
    ├── test_streaks.py
    ├── test_chat_triggers.py
    ├── test_gambling.py
    ├── test_media_queue.py
    ├── test_tipping.py
    ├── test_vanity_shop.py
    ├── test_achievement_engine.py
    ├── test_rank_engine.py
    ├── test_cytube_promotion.py
    ├── test_rank_commands.py
    ├── test_competition_engine.py
    ├── test_multiplier_engine.py
    ├── test_scheduled_events.py
    ├── test_bounty_manager.py
    ├── test_event_admin.py
    ├── test_multiplied_earning.py
    ├── test_admin_commands.py
    ├── test_admin_inspection.py
    ├── test_gif_approval.py
    ├── test_config_reload.py
    ├── test_snapshots.py
    ├── test_trigger_analytics.py
    ├── test_digests.py
    ├── test_metrics_full.py
    ├── test_event_announcer.py
    ├── test_greeting_handler.py
    ├── test_error_hardening.py
    ├── test_performance.py
    ├── test_rate_limiter.py
    ├── test_integration.py
    └── test_deployment.py
```

---

*Sprint 9 completes the kryten-economy implementation plan. All 9 sprints are now specified.*
