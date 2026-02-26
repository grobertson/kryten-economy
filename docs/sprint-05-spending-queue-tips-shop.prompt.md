# Sprint 5 — Spending: Queue, Tips & Vanity Shop

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 5 of 9  
> **Goal:** All ways to spend Z-Coins — content queuing via MediaCMS, tipping other users, and a vanity shop with cosmetic perks.  
> **Depends on:** Sprint 1 (Core Foundation)  
> **Enables:** Sprint 6 (Achievements can reference spend milestones), Sprint 7 (Admin tools for approvals)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Config Activation](#3-config-activation)
4. [MediaCMS Client](#4-mediacms-client)
5. [Spending Engine](#5-spending-engine)
6. [Queue Commands](#6-queue-commands)
7. [Blackout Windows](#7-blackout-windows)
8. [Tipping System](#8-tipping-system)
9. [Vanity Shop](#9-vanity-shop)
10. [Custom Greeting Integration](#10-custom-greeting-integration)
11. [Shoutout Delivery](#11-shoutout-delivery)
12. [Daily Fortune](#12-daily-fortune)
13. [Personal Currency Rename](#13-personal-currency-rename)
14. [History Command](#14-history-command)
15. [PM Command Registrations](#15-pm-command-registrations)
16. [Public Announcements](#16-public-announcements)
17. [Request-Reply Command Extensions](#17-request-reply-command-extensions)
18. [Metrics Extensions](#18-metrics-extensions)
19. [Test Specifications](#19-test-specifications)
20. [Acceptance Criteria](#20-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint:

- Users can **search** the MediaCMS catalog via PM and **queue** content by spending Z
- Content cost scales by duration tier, with configurable rank discounts applied automatically
- **Blackout windows** (cron-defined) reject queue commands during scheduled programming
- Users can **tip** other users with alias-aware self-tip blocking, daily cap, and PM confirmations to both parties
- A **vanity shop** offers cosmetic perks: custom greeting, custom title, chat color, channel GIF (pending admin approval), shoutout, daily fortune, personal currency rename
- **Custom greetings** fire on genuine joins (respecting `greeting_absence_minutes` debounce)
- **Shoutouts** post to public chat via `client.send_chat()` with cooldown enforcement
- A **`history`** PM command shows recent transactions
- Queue purchases and other spend events are **announced** in public chat (configurable)
- All spending goes through a centralized **spending engine** that validates balance → daily limits → blackout → price tier → rank discount → atomic debit → execute
- All interaction with CyTube (queuing media, posting announcements) uses **kryten-py wrapper methods** — no direct NATS access

> **⚠️ Ecosystem rule:** All NATS interaction goes through kryten-py's `KrytenClient`. Prefer purpose-built wrappers (`client.send_pm()`, `client.send_chat()`, `client.add_media()`) over raw `client.publish()` where a wrapper exists. `client.publish()` is acceptable when no wrapper covers the use case.

---

## 2. New Database Tables

### 2.1 `tip_history` Table

Supplements the `transactions` table with a dedicated social analytics view for tips:

```sql
CREATE TABLE IF NOT EXISTS tip_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tip_sender ON tip_history(sender, channel);
CREATE INDEX IF NOT EXISTS idx_tip_receiver ON tip_history(receiver, channel);
CREATE INDEX IF NOT EXISTS idx_tip_date ON tip_history(created_at);
```

### 2.2 `pending_approvals` Table

For vanity items that require admin review (channel GIFs, force-play requests):

```sql
CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    type TEXT NOT NULL,             -- 'channel_gif', 'force_play'
    data TEXT NOT NULL,             -- JSON blob: {"gif_url": "...", "video_id": "...", etc.}
    cost INTEGER NOT NULL,          -- Z charged (refunded if rejected)
    status TEXT DEFAULT 'pending',  -- 'pending', 'approved', 'rejected'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_by TEXT,
    resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_approval_status ON pending_approvals(status, channel);
CREATE INDEX IF NOT EXISTS idx_approval_user ON pending_approvals(username, channel);
```

### 2.3 `vanity_items` Table

Tracks which vanity items a user has purchased and their current settings:

```sql
CREATE TABLE IF NOT EXISTS vanity_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    item_type TEXT NOT NULL,        -- 'custom_greeting', 'custom_title', 'chat_color', etc.
    value TEXT NOT NULL,            -- The greeting text, title text, color hex, gif URL, currency name
    active BOOLEAN DEFAULT 1,
    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(username, channel, item_type)
);

CREATE INDEX IF NOT EXISTS idx_vanity_user ON vanity_items(username, channel);
```

### 2.4 Database Methods to Add

```python
# ── Tip Methods ──────────────────────────────────────────────

async def record_tip(
    self, sender: str, receiver: str, channel: str, amount: int,
) -> None:
    """Record a tip in tip_history. The actual balance transfer uses credit/debit."""

async def get_tips_sent_today(self, username: str, channel: str) -> int:
    """Sum of tips sent by username today (for daily cap check)."""

async def get_tip_count_today(self, username: str, channel: str) -> int:
    """Number of distinct tips sent today."""

# ── Approval Methods ─────────────────────────────────────────

async def create_pending_approval(
    self, username: str, channel: str, approval_type: str,
    data: dict, cost: int,
) -> int:
    """Insert a pending approval. Returns the approval ID."""

async def get_pending_approvals(
    self, channel: str, approval_type: str | None = None,
) -> list[dict]:
    """List pending approvals, optionally filtered by type."""

async def resolve_approval(
    self, approval_id: int, resolved_by: str, approved: bool,
) -> dict | None:
    """Resolve an approval. Returns the approval record (for refund logic) or None."""

# ── Vanity Methods ───────────────────────────────────────────

async def set_vanity_item(
    self, username: str, channel: str, item_type: str, value: str,
) -> None:
    """Upsert a vanity item (INSERT OR REPLACE)."""

async def get_vanity_item(
    self, username: str, channel: str, item_type: str,
) -> str | None:
    """Get active vanity value, or None."""

async def get_all_vanity_items(
    self, username: str, channel: str,
) -> dict[str, str]:
    """Return all active vanity items as {item_type: value}."""

async def get_custom_greeting(self, username: str, channel: str) -> str | None:
    """Shortcut: get custom_greeting vanity value."""

async def get_users_with_custom_greetings(self, channel: str) -> dict[str, str]:
    """Return {username: greeting_text} for all users with active greetings in a channel."""

# ── Queue Tracking ───────────────────────────────────────────

async def get_queues_today(self, username: str, channel: str) -> int:
    """Count queue transactions today (for daily limit)."""

async def get_last_queue_time(self, username: str, channel: str) -> datetime | None:
    """Last queue transaction timestamp (for cooldown)."""

# ── Transaction History ──────────────────────────────────────

async def get_recent_transactions(
    self, username: str, channel: str, limit: int = 10,
) -> list[dict]:
    """Return last N transactions for a user, newest first."""
```

---

## 3. Config Activation

This sprint activates these config sections from `kryten-economy-plan.md`:

- `spending.*` — queue tiers, interrupt/force costs, daily limits, cooldown, blackout windows
- `mediacms.*` — base URL, API token, search limit
- `vanity_shop.*` — all vanity item definitions (cost, enabled, palette, etc.)
- `tipping.*` — min, max_per_day, min_account_age, self_tip_blocked
- `announcements.queue_purchase` — public queue announcements

### 3.1 Pydantic Config Extensions

```python
from pydantic import BaseModel, Field
from typing import Optional

class QueueTier(BaseModel):
    max_minutes: int
    label: str
    cost: int

class BlackoutWindow(BaseModel):
    name: str
    cron: str                          # Cron expression for window start
    duration_hours: int | float

class SpendingConfig(BaseModel):
    queue_tiers: list[QueueTier] = [
        QueueTier(max_minutes=15, label="Short / Music Video", cost=250),
        QueueTier(max_minutes=35, label="30-min Episode", cost=500),
        QueueTier(max_minutes=65, label="60-min Episode", cost=750),
        QueueTier(max_minutes=999, label="Movie", cost=1000),
    ]
    interrupt_play_next: int = 10000
    force_play_now: int = 100000
    force_play_requires_admin: bool = True
    max_queues_per_day: int = 3
    queue_cooldown_minutes: int = 30
    blackout_windows: list[BlackoutWindow] = []

class MediaCMSConfig(BaseModel):
    base_url: str = ""
    api_token: str = ""
    search_results_limit: int = 10
    request_timeout_seconds: float = 10.0
    cache_ttl_seconds: int = 300       # Brief result caching

class ChatColorOption(BaseModel):
    name: str
    hex: str

class VanityItemConfig(BaseModel):
    enabled: bool = True
    cost: int
    description: str = ""

class ChatColorConfig(VanityItemConfig):
    palette: list[ChatColorOption] = []

class ChannelGifConfig(VanityItemConfig):
    requires_admin_approval: bool = True

class ShoutoutConfig(VanityItemConfig):
    max_length: int = 200
    cooldown_minutes: int = 60

class VanityShopConfig(BaseModel):
    custom_greeting: VanityItemConfig = VanityItemConfig(cost=500)
    custom_title: VanityItemConfig = VanityItemConfig(cost=1000)
    chat_color: ChatColorConfig = ChatColorConfig(cost=750)
    channel_gif: ChannelGifConfig = ChannelGifConfig(cost=5000)
    shoutout: ShoutoutConfig = ShoutoutConfig(cost=50)
    daily_fortune: VanityItemConfig = VanityItemConfig(cost=10)
    rename_currency_personal: VanityItemConfig = VanityItemConfig(cost=2500)

class TippingConfig(BaseModel):
    enabled: bool = True
    min_amount: int = 1
    max_per_day: int = 5000
    min_account_age_minutes: int = 30
    self_tip_blocked: bool = True
```

### 3.2 EconomyConfig Extensions

Add these fields to the existing `EconomyConfig`:

```python
class EconomyConfig(KrytenConfig):
    # ... existing Sprint 1-4 fields ...
    spending: SpendingConfig = SpendingConfig()
    mediacms: MediaCMSConfig = MediaCMSConfig()
    vanity_shop: VanityShopConfig = VanityShopConfig()
    tipping: TippingConfig = TippingConfig()
```

---

## 4. MediaCMS Client

### 4.1 File: `kryten_economy/media_client.py`

Async HTTP client for querying the MediaCMS catalog. Uses `aiohttp` for non-blocking requests.

### 4.2 Class: `MediaCMSClient`

```python
import aiohttp
import time
import logging
from typing import Optional

class MediaCMSClient:
    """Async client for MediaCMS catalog API."""

    def __init__(self, config: MediaCMSConfig, logger: logging.Logger):
        self._config = config
        self._logger = logger
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, Any]] = {}  # {key: (expiry_ts, data)}

    async def start(self) -> None:
        """Create the HTTP session."""
        headers = {}
        if self._config.api_token:
            headers["Authorization"] = f"Token {self._config.api_token}"
        self._session = aiohttp.ClientSession(
            base_url=self._config.base_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self._config.request_timeout_seconds),
        )

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def search(self, query: str) -> list[dict]:
        """Search the MediaCMS catalog.
        
        Returns list of results with fields:
            - id: str (short unique ID for queue command)
            - title: str
            - duration: int (seconds)
            - media_type: str ("yt", "vm", etc.)
            - media_id: str (external video ID)
        """
        cache_key = f"search:{query.lower().strip()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            async with self._session.get(
                "/api/v1/media",
                params={"search": query, "page_size": self._config.search_results_limit},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                results = self._parse_search_results(data)
                self._set_cached(cache_key, results)
                return results
        except Exception as e:
            self._logger.error("MediaCMS search failed for '%s': %s", query, e)
            return []

    async def get_by_id(self, media_id: str) -> dict | None:
        """Fetch a single media item by its ID.
        
        Returns dict with: id, title, duration, media_type, media_id
        Or None if not found.
        """
        cache_key = f"item:{media_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            async with self._session.get(f"/api/v1/media/{media_id}") as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json()
                result = self._parse_media_item(data)
                self._set_cached(cache_key, result)
                return result
        except Exception as e:
            self._logger.error("MediaCMS get_by_id failed for '%s': %s", media_id, e)
            return None

    async def get_duration(self, media_id: str) -> int | None:
        """Get the duration of a media item in seconds. Returns None if unavailable."""
        item = await self.get_by_id(media_id)
        return item["duration"] if item else None

    def _parse_search_results(self, data: dict) -> list[dict]:
        """Parse the API response into a normalized result list.
        
        Implementation note: Adapt field names to actual MediaCMS API response format.
        Common structure: {"results": [{"friendly_token": "...", "title": "...", "duration": 1234, ...}]}
        """
        results = data.get("results", data if isinstance(data, list) else [])
        return [self._parse_media_item(item) for item in results]

    def _parse_media_item(self, item: dict) -> dict:
        """Parse a single media item from API response.
        
        Implementation note: Adapt field names to actual MediaCMS API format.
        """
        return {
            "id": item.get("friendly_token", item.get("id", "")),
            "title": item.get("title", "Unknown"),
            "duration": item.get("duration", 0),  # seconds
            "media_type": item.get("media_type", "yt"),
            "media_id": item.get("media_id", item.get("friendly_token", "")),
        }

    def _get_cached(self, key: str) -> Any | None:
        """Return cached value if not expired, else None."""
        if key in self._cache:
            expiry, data = self._cache[key]
            if time.time() < expiry:
                return data
            del self._cache[key]
        return None

    def _set_cached(self, key: str, data: Any) -> None:
        """Cache a result with configured TTL."""
        self._cache[key] = (time.time() + self._config.cache_ttl_seconds, data)
```

### 4.3 Error Handling

- Network errors → return empty list / None (never crash the service)
- Log all errors at WARNING level
- Cache both successful results and "not found" to avoid hammering the API
- Honor the `request_timeout_seconds` config

### 4.4 Testing Approach

All tests mock `aiohttp.ClientSession` — never call a real MediaCMS instance. Test:
- Successful search returns parsed results
- 404 returns None
- Network error returns empty/None without raising
- Cache hit avoids HTTP call
- Cache expiry triggers fresh request

---

## 5. Spending Engine

### 5.1 File: `kryten_economy/spending_engine.py`

Centralized validation and execution for all spend actions.

### 5.2 Class: `SpendingEngine`

```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SpendResult(Enum):
    SUCCESS = "success"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    DAILY_LIMIT = "daily_limit"
    COOLDOWN = "cooldown"
    BLACKOUT = "blackout"
    NOT_FOUND = "not_found"
    DISABLED = "disabled"
    REQUIRES_APPROVAL = "requires_approval"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ARGS = "invalid_args"


@dataclass(frozen=True)
class SpendOutcome:
    result: SpendResult
    message: str                       # Human-readable PM response
    amount_charged: int = 0            # Actual Z charged (after discount)
    original_amount: int = 0           # Pre-discount price
    discount_percent: float = 0.0      # Applied rank discount


class SpendingEngine:
    def __init__(self, config, database, media_client, logger):
        self._config = config
        self._db = database
        self._media = media_client
        self._logger = logger

    def get_rank_discount(self, rank_tier_index: int) -> float:
        """Calculate the rank discount percentage.
        
        Formula: spend_discount_per_rank × rank_tier_index
        E.g., tier 5 with 0.02 discount_per_rank = 10% off
        """
        return self._config.ranks.spend_discount_per_rank * rank_tier_index

    def apply_discount(self, base_cost: int, rank_tier_index: int) -> tuple[int, float]:
        """Apply rank discount to a base cost.
        
        Returns (discounted_cost, discount_percent).
        Cost is always at least 1 Z (never free via discount).
        """
        discount = self.get_rank_discount(rank_tier_index)
        discounted = max(1, int(base_cost * (1 - discount)))
        return discounted, discount

    def get_price_tier(self, duration_seconds: int) -> tuple[str, int]:
        """Find the price tier for a given duration.
        
        Returns (tier_label, base_cost).
        Tiers are checked in order; first match wins (max_minutes threshold).
        """
        duration_minutes = duration_seconds / 60
        for tier in self._config.spending.queue_tiers:
            if duration_minutes <= tier.max_minutes:
                return tier.label, tier.cost
        # Fallback to last tier
        last = self._config.spending.queue_tiers[-1]
        return last.label, last.cost

    async def validate_spend(
        self,
        username: str,
        channel: str,
        amount: int,
        spend_type: str,
    ) -> SpendOutcome | None:
        """Common validation for all spends.
        
        Returns SpendOutcome with error if validation fails, or None if OK.
        Checks: account exists, sufficient balance, not economy-banned.
        """
        account = await self._db.get_account(username, channel)
        if not account:
            return SpendOutcome(
                result=SpendResult.INSUFFICIENT_FUNDS,
                message="You don't have an account yet. Stick around to earn some Z!",
            )
        if account.get("economy_banned"):
            return SpendOutcome(
                result=SpendResult.PERMISSION_DENIED,
                message="Your economy access has been suspended.",
            )
        if account["balance"] < amount:
            return SpendOutcome(
                result=SpendResult.INSUFFICIENT_FUNDS,
                message=f"Insufficient funds. You have {account['balance']:,} Z but need {amount:,} Z.",
            )
        return None  # All checks passed
```

### 5.3 Spend Flow (All Types)

```
User sends PM command (e.g., "queue 8Fn")
  → PmHandler parses → dispatches to spending_engine method
  → Validate: account exists, not banned, balance >= cost
  → Type-specific checks (daily limit, cooldown, blackout, etc.)
  → Calculate rank discount
  → Atomic debit via database.atomic_debit()
  → Record transaction (trigger_id = "spend.queue", "spend.tip", etc.)
  → Execute action (add_media, set_vanity, etc.)
  → PM response with cost, discount, new balance
  → Optional: public announcement
```

---

## 6. Queue Commands

### 6.1 `search <query>`

```python
async def _cmd_search(self, username: str, channel: str, args: list[str]) -> str:
    """Search the MediaCMS catalog."""
    if not self._config.mediacms.base_url:
        return "📽️ Content queuing is not configured for this channel."
    
    if not args:
        return "Usage: search <query>"
    
    query = " ".join(args)
    results = await self._media.search(query)
    
    if not results:
        return f"No results found for '{query}'."
    
    account = await self._db.get_account(username, channel)
    rank_tier = self._get_rank_tier_index(account) if account else 0
    
    lines = [f"🔍 Found {len(results)} result(s) for '{query}':"]
    for i, item in enumerate(results, 1):
        duration_str = self._format_duration(item["duration"])
        tier_label, base_cost = self._spending.get_price_tier(item["duration"])
        final_cost, discount = self._spending.apply_discount(base_cost, rank_tier)
        
        cost_str = f"{final_cost:,} Z"
        if discount > 0:
            cost_str += f" ({int(discount * 100)}% off!)"
        
        lines.append(
            f"  {i}. \"{item['title']}\" ({duration_str}) — ID: {item['id']} · {cost_str}"
        )
    
    return "\n".join(lines)
```

### 6.2 `queue <id>`

```python
async def _cmd_queue(self, username: str, channel: str, args: list[str]) -> str:
    """Queue a MediaCMS item for the configured cost."""
    if not args:
        return "Usage: queue <id>"
    
    media_id = args[0]
    
    # Blackout check
    if self._is_blackout_active(channel):
        window_name = self._get_active_blackout_name(channel)
        return f"🚫 Queue is locked during {window_name}. Try again later!"
    
    # Daily limit check
    queues_today = await self._db.get_queues_today(username, channel)
    max_queues = self._config.spending.max_queues_per_day
    if queues_today >= max_queues:
        return f"Daily queue limit reached ({max_queues}/{max_queues}). Try again tomorrow!"
    
    # Cooldown check
    last_queue = await self._db.get_last_queue_time(username, channel)
    if last_queue:
        cooldown = self._config.spending.queue_cooldown_minutes * 60
        elapsed = (datetime.now(timezone.utc) - last_queue).total_seconds()
        if elapsed < cooldown:
            remaining = int((cooldown - elapsed) / 60) + 1
            return f"⏳ Queue cooldown: {remaining} minute(s) remaining."
    
    # Fetch media info from MediaCMS
    item = await self._media.get_by_id(media_id)
    if not item:
        return f"Media '{media_id}' not found in the catalog."
    
    # Calculate cost
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    tier_label, base_cost = self._spending.get_price_tier(item["duration"])
    final_cost, discount = self._spending.apply_discount(base_cost, rank_tier)
    
    # Validate and debit
    validation = await self._spending.validate_spend(username, channel, final_cost, "queue")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend",
        trigger_id="spend.queue",
        reason=f"Queue: \"{item['title']}\"",
    )
    if not success:
        return "Insufficient funds."
    
    # Queue the media via kryten-py
    await self._client.add_media(channel, item["media_type"], item["media_id"])
    
    # Response
    duration_str = self._format_duration(item["duration"])
    new_balance = (await self._db.get_account(username, channel))["balance"]
    
    discount_str = ""
    if discount > 0:
        discount_str = f" ({int(discount * 100)}% {account['rank_name']} discount)"
    
    # Public announcement (if configured)
    if self._config.announcements.queue_purchase:
        announce_msg = self._config.announcements.templates.get(
            "queue",
            "🎬 {user} just queued \"{title}\"! ({cost} {currency})"
        ).format(
            user=username,
            title=item["title"],
            cost=final_cost,
            currency=self._config.currency.name,
        )
        await self._client.send_chat(channel, announce_msg)
    
    return (
        f"🎬 Queued \"{item['title']}\" ({duration_str}).\n"
        f"Charged: {final_cost:,} Z{discount_str} · Balance: {new_balance:,} Z"
    )
```

### 6.3 `playnext <id>`

Same flow as `queue`, but:
- Cost: `config.spending.interrupt_play_next` (default 10,000 Z)
- Uses `await self._client.add_media(channel, media_type, media_id, position="next")`
- Transaction trigger_id: `"spend.playnext"`

```python
async def _cmd_playnext(self, username: str, channel: str, args: list[str]) -> str:
    """Queue a MediaCMS item to play next (premium cost)."""
    if not args:
        return "Usage: playnext <id>"
    
    media_id = args[0]
    
    # Same blackout/cooldown/limit checks as queue...
    # (Factor into a shared _pre_queue_checks() method)
    
    item = await self._media.get_by_id(media_id)
    if not item:
        return f"Media '{media_id}' not found in the catalog."
    
    base_cost = self._config.spending.interrupt_play_next
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(base_cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "playnext")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.playnext",
        reason=f"Play Next: \"{item['title']}\"",
    )
    if not success:
        return "Insufficient funds."
    
    # Queue at front via kryten-py
    await self._client.add_media(channel, item["media_type"], item["media_id"], position="next")
    
    new_balance = (await self._db.get_account(username, channel))["balance"]
    return (
        f"⏭️ \"{item['title']}\" queued to play next!\n"
        f"Charged: {final_cost:,} Z · Balance: {new_balance:,} Z"
    )
```

### 6.4 `forcenow <id>`

Same flow, but:
- Cost: `config.spending.force_play_now` (default 100,000 Z)
- If `force_play_requires_admin` is true, creates a `pending_approval` instead of executing immediately
- Transaction trigger_id: `"spend.forcenow"`
- Uses `await self._client.add_media(channel, media_type, media_id, position="next")` to insert at the top of the queue. **Note:** `client.jump_to()` does not exist in kryten-py. Force-play is implemented by adding to the "next" position. For immediate playback, a NATS request to the robot may be needed — check kryten-py for a `playlist_jump` or similar wrapper, or use `client.publish("kryten.command.playlist.jump", {"channel": channel, "uid": uid})` if available.

```python
async def _cmd_forcenow(self, username: str, channel: str, args: list[str]) -> str:
    """Force-play a MediaCMS item immediately (highest cost, may require approval)."""
    if not args:
        return "Usage: forcenow <id>"
    
    media_id = args[0]
    item = await self._media.get_by_id(media_id)
    if not item:
        return f"Media '{media_id}' not found in the catalog."
    
    base_cost = self._config.spending.force_play_now
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(base_cost, rank_tier)
    
    if self._config.spending.force_play_requires_admin:
        # Charge and create approval request
        validation = await self._spending.validate_spend(username, channel, final_cost, "forcenow")
        if validation:
            return validation.message
        
        success = await self._db.atomic_debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id="spend.forcenow",
            reason=f"Force-Play (pending approval): \"{item['title']}\"",
        )
        if not success:
            return "Insufficient funds."
        
        approval_id = await self._db.create_pending_approval(
            username, channel, "force_play",
            data={"media_id": media_id, "title": item["title"],
                   "media_type": item["media_type"], "media_ext_id": item["media_id"]},
            cost=final_cost,
        )
        return (
            f"📝 Force-play request submitted for \"{item['title']}\".\n"
            f"Charged: {final_cost:,} Z (refunded if rejected) · Approval ID: {approval_id}"
        )
    else:
        # Direct execution (no approval gate)
        validation = await self._spending.validate_spend(username, channel, final_cost, "forcenow")
        if validation:
            return validation.message
        
        success = await self._db.atomic_debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id="spend.forcenow",
            reason=f"Force-Play: \"{item['title']}\"",
        )
        if not success:
            return "Insufficient funds."
        
        await self._client.add_media(channel, item["media_type"], item["media_id"], position="next")
        # NOTE: add_media with position="next" places it at the top of the queue.
        # For truly immediate playback (interrupting current), a playlist jump
        # command may be needed. Check kryten-py for a `playlist_jump` wrapper.
        
        new_balance = (await self._db.get_account(username, channel))["balance"]
        return (
            f"🎬💥 Force-playing \"{item['title']}\" NOW!\n"
            f"Charged: {final_cost:,} Z · Balance: {new_balance:,} Z"
        )
```

---

## 7. Blackout Windows

### 7.1 Blackout Logic

Blackout windows reject queue commands during scheduled programming (e.g., "Weird Wednesday", "Weekend Marathon").

```python
from croniter import croniter
from datetime import datetime, timedelta


class BlackoutChecker:
    """Check whether a blackout window is currently active."""

    def __init__(self, windows: list[BlackoutWindow]):
        self._windows = windows

    def is_active(self) -> bool:
        """Return True if any blackout window is currently active."""
        return self.get_active_window() is not None

    def get_active_window(self) -> BlackoutWindow | None:
        """Return the currently active blackout window, or None."""
        now = datetime.now(timezone.utc)
        for window in self._windows:
            if self._is_window_active(window, now):
                return window
        return None

    def _is_window_active(self, window: BlackoutWindow, now: datetime) -> bool:
        """Check if a single window is active at the given time."""
        # Find the most recent trigger before now
        cron = croniter(window.cron, now)
        prev_trigger = cron.get_prev(datetime)
        window_end = prev_trigger + timedelta(hours=window.duration_hours)
        return prev_trigger <= now < window_end
```

### 7.2 Dependency

Add `croniter>=2.0` to `pyproject.toml` dependencies.

### 7.3 Blackout Response

When a queue command is rejected during a blackout:

```
🚫 Queue is locked during "Weird Wednesday". Resuming in ~2h 15m.
```

---

## 8. Tipping System

### 8.1 `tip @user <amount>`

```python
async def _cmd_tip(self, username: str, channel: str, args: list[str]) -> str:
    """Transfer Z to another user."""
    if not self._config.tipping.enabled:
        return "Tipping is not enabled."
    
    if len(args) < 2:
        return "Usage: tip @user <amount>"
    
    target = args[0].lstrip("@")
    try:
        amount = int(args[1])
    except ValueError:
        return "Amount must be a whole number."
    
    # Validation
    if amount < self._config.tipping.min_amount:
        return f"Minimum tip: {self._config.tipping.min_amount} Z."
    
    if target.lower() == username.lower():
        return "You can't tip yourself! 🤦"
    
    # Alias-aware self-tip check
    # Query kryten-userstats for alias resolution:
    # response = await self._client.nats_request(
    #     "kryten.userstats.command",
    #     {"service": "userstats", "command": "alias.resolve", "username": target, "channel": channel},
    #     timeout=2.0,
    # )
    # If resolved alias matches sender, block.
    
    if self._is_ignored(target):
        return "That user is not participating in the economy."
    
    # Account age check for sender
    sender_account = await self._db.get_or_create_account(username, channel)
    if sender_account.get("account_age_minutes", 0) < self._config.tipping.min_account_age_minutes:
        return "Your account is too new to send tips. Keep hanging out!"
    
    # Target account must exist
    target_account = await self._db.get_account(target, channel)
    if not target_account:
        return f"User '{target}' doesn't have an economy account yet."
    
    # Daily cap
    tips_today = await self._db.get_tips_sent_today(username, channel)
    if tips_today + amount > self._config.tipping.max_per_day:
        remaining = self._config.tipping.max_per_day - tips_today
        return f"Daily tip limit: {self._config.tipping.max_per_day:,} Z. You have {remaining:,} Z remaining today."
    
    # Debit sender
    success = await self._db.atomic_debit(
        username, channel, amount,
        tx_type="tip_send",
        trigger_id="spend.tip",
        reason=f"Tip to {target}",
    )
    if not success:
        return "Insufficient funds."
    
    # Credit receiver
    await self._db.credit(
        target, channel, amount,
        tx_type="tip_receive",
        trigger_id="earn.tip",
        reason=f"Tip from {username}",
    )
    
    # Record in tip_history
    await self._db.record_tip(username, target, channel, amount)
    
    # PM to receiver (via kryten-py)
    symbol = self._config.currency.symbol
    await self._client.send_pm(
        channel, target,
        f"💸 {username} just tipped you {amount:,} {symbol}!"
    )
    
    sender_balance = (await self._db.get_account(username, channel))["balance"]
    return f"💸 Tipped {target} {amount:,} {symbol}. Your balance: {sender_balance:,} {symbol}"
```

---

## 9. Vanity Shop

### 9.1 `shop` Command

Lists available vanity items and their costs (with rank discounts shown):

```python
async def _cmd_shop(self, username: str, channel: str, args: list[str]) -> str:
    """List vanity shop items and prices."""
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    symbol = self._config.currency.symbol
    
    lines = ["🛒 Vanity Shop", "━" * 30]
    
    items = [
        ("greeting", self._config.vanity_shop.custom_greeting, "buy greeting <text>"),
        ("title", self._config.vanity_shop.custom_title, "buy title <text>"),
        ("color", self._config.vanity_shop.chat_color, "buy color <name>"),
        ("gif", self._config.vanity_shop.channel_gif, "buy gif <url>"),
        ("shoutout", self._config.vanity_shop.shoutout, "buy shoutout <message>"),
        ("fortune", self._config.vanity_shop.daily_fortune, "fortune"),
        ("rename", self._config.vanity_shop.rename_currency_personal, "buy rename <name>"),
    ]
    
    for item_key, item_cfg, usage in items:
        if not item_cfg.enabled:
            continue
        final_cost, discount = self._spending.apply_discount(item_cfg.cost, rank_tier)
        cost_str = f"{final_cost:,} {symbol}"
        if discount > 0:
            cost_str += f" (was {item_cfg.cost:,})"
        lines.append(f"  {item_key:<12} {cost_str:<18} → {usage}")
        if item_cfg.description:
            lines.append(f"  {'':12} {item_cfg.description}")
    
    owned = await self._db.get_all_vanity_items(username, channel)
    if owned:
        lines.append("")
        lines.append("Your items:")
        for item_type, value in owned.items():
            display = value[:30] + "..." if len(value) > 30 else value
            lines.append(f"  ✅ {item_type}: {display}")
    
    return "\n".join(lines)
```

### 9.2 `buy <item> [args]` Command

```python
async def _cmd_buy(self, username: str, channel: str, args: list[str]) -> str:
    """Purchase a vanity item."""
    if not args:
        return "Usage: buy <item> [args]. Try 'shop' to see available items."
    
    item_key = args[0].lower()
    item_args = " ".join(args[1:]) if len(args) > 1 else ""
    
    handlers = {
        "greeting": self._buy_custom_greeting,
        "title": self._buy_custom_title,
        "color": self._buy_chat_color,
        "gif": self._buy_channel_gif,
        "shoutout": self._buy_shoutout,
        "rename": self._buy_rename_currency,
    }
    
    handler = handlers.get(item_key)
    if not handler:
        return f"Unknown item '{item_key}'. Try 'shop' to see available items."
    
    return await handler(username, channel, item_args)
```

### 9.3 Buy Handlers (Pattern)

Each buy handler follows this pattern:

```python
async def _buy_custom_greeting(self, username: str, channel: str, value: str) -> str:
    """Purchase a custom greeting."""
    cfg = self._config.vanity_shop.custom_greeting
    if not cfg.enabled:
        return "Custom greetings are not available."
    if not value:
        return "Usage: buy greeting <your greeting text>"
    
    # Validate content (length, no offensive content, etc.)
    if len(value) > 200:
        return "Greeting text too long (max 200 characters)."
    
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(cfg.cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.vanity.custom_greeting",
        reason=f"Vanity: Custom greeting",
    )
    if not success:
        return "Insufficient funds."
    
    await self._db.set_vanity_item(username, channel, "custom_greeting", value)
    
    symbol = self._config.currency.symbol
    new_balance = (await self._db.get_account(username, channel))["balance"]
    return (
        f"✅ Custom greeting set! You'll be greeted with:\n"
        f"  \"{value}\"\n"
        f"Charged: {final_cost:,} {symbol} · Balance: {new_balance:,} {symbol}"
    )
```

### 9.4 Chat Color Buy Handler

Special: validates against the approved palette.

```python
async def _buy_chat_color(self, username: str, channel: str, value: str) -> str:
    """Purchase a chat color from the approved palette."""
    cfg = self._config.vanity_shop.chat_color
    if not cfg.enabled:
        return "Chat colors are not available."
    if not value:
        palette_list = ", ".join(c.name for c in cfg.palette)
        return f"Usage: buy color <name>\nAvailable: {palette_list}"
    
    # Find the color in the palette (case-insensitive)
    color_match = None
    for option in cfg.palette:
        if option.name.lower() == value.lower():
            color_match = option
            break
    
    if not color_match:
        palette_list = ", ".join(c.name for c in cfg.palette)
        return f"Unknown color '{value}'. Available: {palette_list}"
    
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(cfg.cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.vanity.chat_color",
        reason=f"Vanity: Chat color {color_match.name}",
    )
    if not success:
        return "Insufficient funds."
    
    await self._db.set_vanity_item(username, channel, "chat_color", color_match.hex)
    
    new_balance = (await self._db.get_account(username, channel))["balance"]
    return (
        f"🎨 Chat color set to {color_match.name} ({color_match.hex})!\n"
        f"Charged: {final_cost:,} Z · Balance: {new_balance:,} Z"
    )
```

### 9.5 Channel GIF Buy Handler

Special: requires admin approval. Charge immediately, create pending approval.

```python
async def _buy_channel_gif(self, username: str, channel: str, value: str) -> str:
    """Purchase a channel GIF (requires admin approval)."""
    cfg = self._config.vanity_shop.channel_gif
    if not cfg.enabled:
        return "Channel GIFs are not available."
    if not value:
        return "Usage: buy gif <gif_url>"
    
    # Basic URL validation
    if not value.startswith(("http://", "https://")):
        return "Please provide a valid URL for your GIF."
    
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(cfg.cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.vanity.channel_gif",
        reason=f"Vanity: Channel GIF (pending approval)",
    )
    if not success:
        return "Insufficient funds."
    
    approval_id = await self._db.create_pending_approval(
        username, channel, "channel_gif",
        data={"gif_url": value},
        cost=final_cost,
    )
    
    new_balance = (await self._db.get_account(username, channel))["balance"]
    return (
        f"📝 Channel GIF submitted for admin approval!\n"
        f"URL: {value}\n"
        f"Charged: {final_cost:,} Z (refunded if rejected) · Balance: {new_balance:,} Z\n"
        f"Approval ID: {approval_id}"
    )
```

### 9.6 Remaining Vanity Handlers (Summary)

| Item | Key | Validation | Special |
|---|---|---|---|
| Custom title | `title` | Length ≤ 100 chars | None |
| Shoutout | `shoutout` | Length ≤ `max_length`, cooldown check | Posts to public chat via `client.send_chat()` |
| Daily fortune | `fortune` | Once per day | Returns random fortune text |
| Personal currency rename | `rename` | Length ≤ 30 chars, alphanumeric + spaces | Updates `accounts.personal_currency_name` |

---

## 10. Custom Greeting Integration

### 10.1 Trigger: `adduser` Event

When a user joins and passes the **greeting debounce** (`greeting_absence_minutes` from Sprint 1), check if they have a custom greeting:

```python
# In the adduser handler (presence_tracker or main app):
async def _maybe_send_custom_greeting(self, username: str, channel: str) -> None:
    """Send a custom greeting in public chat if the user has one and passes debounce."""
    if not self._config.announcements.custom_greeting:
        return
    
    greeting = await self._db.get_custom_greeting(username, channel)
    if not greeting:
        return
    
    # greeting_absence_minutes check already passed before this is called
    await self._client.send_chat(channel, greeting)
```

### 10.2 Integration Point

This is called from the `adduser` handler in `main.py`, **after** `presence_tracker.handle_user_join()` confirms a genuine arrival with sufficient absence:

```python
@self.client.on("adduser")
async def handle_join(event):
    self.events_processed += 1
    is_genuine = await self.presence_tracker.handle_user_join(event.username, event.channel)
    if is_genuine:
        await self._maybe_send_welcome_wallet(event.username, event.channel)  # Sprint 2
        await self._maybe_send_custom_greeting(event.username, event.channel)  # Sprint 5
```

---

## 11. Shoutout Delivery

### 11.1 Shoutout Flow

When a user buys a shoutout (`buy shoutout <message>`):

1. Validate: enabled, length ≤ max, cooldown not active
2. Charge the user
3. Post the message in public chat via `client.send_chat()`
4. Record cooldown timestamp

```python
async def _buy_shoutout(self, username: str, channel: str, value: str) -> str:
    """Purchase and immediately deliver a shoutout."""
    cfg = self._config.vanity_shop.shoutout
    if not cfg.enabled:
        return "Shoutouts are not available."
    if not value:
        return "Usage: buy shoutout <your message>"
    if len(value) > cfg.max_length:
        return f"Message too long (max {cfg.max_length} characters)."
    
    # Cooldown check (use in-memory dict, keyed by (username, channel))
    last_shoutout = self._shoutout_cooldowns.get((username.lower(), channel))
    if last_shoutout:
        elapsed = (datetime.now(timezone.utc) - last_shoutout).total_seconds()
        cooldown = cfg.cooldown_minutes * 60
        if elapsed < cooldown:
            remaining = int((cooldown - elapsed) / 60) + 1
            return f"⏳ Shoutout cooldown: {remaining} minute(s) remaining."
    
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(cfg.cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.vanity.shoutout",
        reason=f"Vanity: Shoutout",
    )
    if not success:
        return "Insufficient funds."
    
    # Deliver shoutout to public chat via kryten-py
    await self._client.send_chat(channel, f"📢 {username}: {value}")
    
    # Record cooldown
    self._shoutout_cooldowns[(username.lower(), channel)] = datetime.now(timezone.utc)
    
    new_balance = (await self._db.get_account(username, channel))["balance"]
    return f"📢 Shoutout delivered! Charged: {final_cost:,} Z · Balance: {new_balance:,} Z"
```

---

## 12. Daily Fortune

### 12.1 Fortune Command

A cheap, fun, daily consumable:

```python
FORTUNES = [
    "🔮 The stars say you'll find a rare emote today.",
    "🎱 Signs point to a jackpot in your future.",
    "🌙 Tonight's movie will change your life. Or at least your mood.",
    "🃏 A mysterious stranger will tip you. Or not.",
    "⭐ Your Z-Coins are multiplying... in your dreams.",
    "🎬 Your next queue pick will be legendary.",
    "🌊 A rain of fortune approaches. Stay connected.",
    "🎭 Two paths diverge. Both lead to the slot machine.",
    "🔥 Your chat energy today: unstoppable.",
    "🌈 Something beautiful awaits in the playlist.",
    # ... add 20-50 more fortunes
]

async def _cmd_fortune(self, username: str, channel: str, args: list[str]) -> str:
    """Receive a random daily fortune."""
    cfg = self._config.vanity_shop.daily_fortune
    if not cfg.enabled:
        return "Fortunes are not available."
    
    # Check if already used today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fortune_key = f"fortune:{username.lower()}:{channel}:{today}"
    if fortune_key in self._daily_fortune_used:
        return "🔮 You've already received your fortune today. Come back tomorrow!"
    
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(cfg.cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "fortune")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.vanity.fortune",
        reason="Daily fortune",
    )
    if not success:
        return "Insufficient funds."
    
    # Pick a deterministic-ish fortune (seeded by username + date for consistency)
    import hashlib
    seed = int(hashlib.md5(f"{username}{today}".encode()).hexdigest()[:8], 16)
    fortune = FORTUNES[seed % len(FORTUNES)]
    
    self._daily_fortune_used.add(fortune_key)
    
    return fortune
```

### 12.2 State Management

`_daily_fortune_used` is an in-memory `set[str]`. It resets naturally as the date changes (new keys). Old keys can be pruned periodically by the scheduler (or simply ignored — the set won't grow meaningfully).

---

## 13. Personal Currency Rename

### 13.1 `buy rename <name>`

```python
async def _buy_rename_currency(self, username: str, channel: str, value: str) -> str:
    """Rename your personal currency display name."""
    cfg = self._config.vanity_shop.rename_currency_personal
    if not cfg.enabled:
        return "Personal currency rename is not available."
    if not value:
        return "Usage: buy rename <your currency name>"
    if len(value) > 30:
        return "Currency name too long (max 30 characters)."
    
    # Basic sanitization
    if not all(c.isalnum() or c in " -_'" for c in value):
        return "Currency name can only contain letters, numbers, spaces, hyphens, underscores, and apostrophes."
    
    account = await self._db.get_or_create_account(username, channel)
    rank_tier = self._get_rank_tier_index(account)
    final_cost, discount = self._spending.apply_discount(cfg.cost, rank_tier)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.vanity.rename_currency",
        reason=f"Vanity: Rename currency to '{value}'",
    )
    if not success:
        return "Insufficient funds."
    
    # Store in vanity_items table
    await self._db.set_vanity_item(username, channel, "personal_currency_name", value)
    
    new_balance = (await self._db.get_account(username, channel))["balance"]
    return (
        f"✅ Your currency is now called \"{value}\"!\n"
        f"Charged: {final_cost:,} Z · Balance: {new_balance:,} {value}"
    )
```

### 13.2 Usage in Balance Display

The `balance` command (Sprint 1) should check for a personal currency name:

```python
# In _cmd_balance:
personal_name = await self._db.get_vanity_item(username, channel, "personal_currency_name")
currency_name = personal_name or self._config.currency.name
```

---

## 14. History Command

### 14.1 `history` PM Command

```python
async def _cmd_history(self, username: str, channel: str, args: list[str]) -> str:
    """Show recent transactions."""
    limit = 10
    if args:
        try:
            limit = min(25, max(1, int(args[0])))
        except ValueError:
            pass
    
    transactions = await self._db.get_recent_transactions(username, channel, limit)
    
    if not transactions:
        return "No transaction history yet."
    
    symbol = self._config.currency.symbol
    lines = [f"📜 Last {len(transactions)} transactions:"]
    
    for tx in transactions:
        amount = tx["amount"]
        sign = "+" if tx["type"] in ("earn", "credit", "tip_receive") else "-"
        reason = tx.get("reason", tx.get("trigger_id", ""))
        ts = tx["created_at"]
        if isinstance(ts, str):
            # Format nicely
            ts = ts[:16].replace("T", " ")
        lines.append(f"  {sign}{amount:,} {symbol}  {reason}  ({ts})")
    
    return "\n".join(lines)
```

---

## 15. PM Command Registrations

### 15.1 Sprint 5 Additions to Command Map

Add these to the `PmHandler._command_map`:

```python
# In PmHandler.__init__:
self._command_map.update({
    # Queue commands
    "search": self._cmd_search,
    "queue": self._cmd_queue,
    "playnext": self._cmd_playnext,
    "forcenow": self._cmd_forcenow,
    # Tipping
    "tip": self._cmd_tip,
    # Shop
    "shop": self._cmd_shop,
    "buy": self._cmd_buy,
    # Fortune (shortcut, no "buy" prefix needed)
    "fortune": self._cmd_fortune,
    # History
    "history": self._cmd_history,
})
```

### 15.2 Updated Help Text

Update the `help` command response to include Sprint 5 commands:

```
🎬 Economy Bot — Your Pocket Studio
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
balance     Check your Z balance
search      Browse the catalog
queue       Add something to the playlist
tip         Share the love (and the Z)
shop        Browse vanity items
fortune     What do the stars say?
history     Recent transactions
help        You're looking at it!

Discover more as you go. 🍿
```

---

## 16. Public Announcements

### 16.1 Announcement Helper

All public announcements go through a centralized helper that uses kryten-py's `send_chat()`:

```python
async def _announce(self, channel: str, template_key: str, **kwargs) -> None:
    """Send a public announcement using a configured template.
    
    All chat output goes through client.send_chat() — never raw NATS publish.
    """
    template = self._config.announcements.templates.get(template_key)
    if not template:
        return
    
    try:
        message = template.format(**kwargs)
    except KeyError as e:
        self._logger.warning("Announcement template '%s' missing key: %s", template_key, e)
        return
    
    await self._client.send_chat(channel, message)
```

### 16.2 Sprint 5 Announcement Triggers

| Event | Config Gate | Template Key | Template Variables |
|---|---|---|---|
| Queue purchase | `announcements.queue_purchase` | `queue` | `user`, `title`, `cost`, `currency` |
| Playnext purchase | `announcements.queue_purchase` | `queue` | Same |
| Force-play approval | `announcements.queue_purchase` | `queue` | Same |

---

## 17. Request-Reply Command Extensions

### 17.1 New Commands on `kryten.economy.command`

Add to the `CommandHandler._HANDLER_MAP`:

```python
_HANDLER_MAP = {
    # ... Sprint 1 commands ...
    "balance.get": _handle_balance_get,
    # Sprint 5:
    "spend.tip": _handle_tip,
    "spend.queue": _handle_queue,
    "vanity.list": _handle_vanity_list,
    "vanity.get": _handle_vanity_get,
    "approval.list": _handle_approval_list,
    "approval.resolve": _handle_approval_resolve,
}
```

### 17.2 Approval Commands (for admin tools)

```python
async def _handle_approval_list(self, request: dict) -> dict:
    """List pending approvals for a channel."""
    channel = request.get("channel")
    approval_type = request.get("type")  # optional filter
    approvals = await self._app.db.get_pending_approvals(channel, approval_type)
    return {"approvals": approvals}

async def _handle_approval_resolve(self, request: dict) -> dict:
    """Approve or reject a pending approval."""
    approval_id = request.get("approval_id")
    approved = request.get("approved", False)
    resolved_by = request.get("resolved_by")
    
    record = await self._app.db.resolve_approval(approval_id, resolved_by, approved)
    if not record:
        raise ValueError(f"Approval {approval_id} not found or already resolved")
    
    if not approved:
        # Refund the cost
        await self._app.db.credit(
            record["username"], record["channel"], record["cost"],
            tx_type="refund",
            trigger_id="refund.approval_rejected",
            reason=f"Refund: {record['type']} rejected by {resolved_by}",
        )
        # Notify user
        await self._app.client.send_pm(
            record["channel"], record["username"],
            f"❌ Your {record['type']} request was rejected. {record['cost']:,} Z refunded."
        )
    else:
        # Execute the approved action
        await self._execute_approved_action(record)
        # Notify user
        await self._app.client.send_pm(
            record["channel"], record["username"],
            f"✅ Your {record['type']} request was approved!"
        )
    
    return {"approval_id": approval_id, "approved": approved}
```

---

## 18. Metrics Extensions

### 18.1 New Counters

```python
# In _collect_custom_metrics():
lines.append(f'economy_z_spent_total {self._app.z_spent_total}')
lines.append(f'economy_tips_total {self._app.tips_total}')
lines.append(f'economy_queues_total {self._app.queues_total}')
lines.append(f'economy_vanity_purchases_total {self._app.vanity_purchases_total}')
```

### 18.2 App-Level Counters

Add to `EconomyApp`:

```python
self.z_spent_total: int = 0
self.tips_total: int = 0
self.queues_total: int = 0
self.vanity_purchases_total: int = 0
```

Increment these in the respective command handlers.

---

## 19. Test Specifications

### 19.1 MediaCMS Client Tests (`tests/test_media_client.py`)

| Test | Description |
|---|---|
| `test_search_returns_results` | Mock API returns results → parsed correctly |
| `test_search_empty_result` | Mock API returns empty → returns `[]` |
| `test_search_network_error` | Mock network error → returns `[]`, no exception |
| `test_get_by_id_found` | Mock API returns item → parsed correctly |
| `test_get_by_id_not_found` | Mock 404 → returns `None` |
| `test_get_duration` | Returns duration from get_by_id |
| `test_cache_hit` | Second search with same query uses cache, no HTTP |
| `test_cache_expiry` | After TTL, cache miss triggers fresh request |
| `test_request_timeout` | Timeout → returns empty/None |

### 19.2 Spending Engine Tests (`tests/test_spending_engine.py`)

| Test | Description |
|---|---|
| `test_rank_discount_tier_0` | No discount for base tier |
| `test_rank_discount_tier_5` | 10% discount (5 × 0.02) |
| `test_discount_never_free` | Even 100% discount results in 1 Z minimum |
| `test_price_tier_short` | ≤15 min → 250 Z |
| `test_price_tier_episode` | ≤35 min → 500 Z |
| `test_price_tier_movie` | >65 min → 1000 Z |
| `test_validate_spend_no_account` | Returns insufficient funds |
| `test_validate_spend_banned` | Returns permission denied |
| `test_validate_spend_low_balance` | Returns insufficient funds with amounts |
| `test_validate_spend_ok` | Returns None (all checks pass) |

### 19.3 Queue Command Tests (`tests/test_queue_commands.py`)

| Test | Description |
|---|---|
| `test_search_no_mediacms` | Config has no MediaCMS → returns "not configured" |
| `test_search_returns_results` | Returns formatted results with costs |
| `test_search_shows_discount` | Ranked user sees discounted prices |
| `test_queue_success` | Debits, calls `client.add_media()`, sends announcement |
| `test_queue_not_found` | Invalid media ID → error message |
| `test_queue_insufficient_funds` | Low balance → error message |
| `test_queue_daily_limit` | Exceeds max_queues_per_day → error |
| `test_queue_cooldown` | Within cooldown → error with remaining time |
| `test_queue_blackout` | During blackout → error with window name |
| `test_playnext_uses_next_position` | Calls `add_media(..., position="next")` |
| `test_playnext_higher_cost` | Uses interrupt cost instead of tier cost |
| `test_forcenow_requires_approval` | Creates pending_approval when admin-gated |
| `test_forcenow_direct_when_ungated` | Executes immediately when not admin-gated |

### 19.4 Tipping Tests (`tests/test_tipping.py`)

| Test | Description |
|---|---|
| `test_tip_success` | Debits sender, credits receiver, records tip, PMs receiver |
| `test_tip_self_blocked` | Self-tip → error |
| `test_tip_ignored_user` | Tip to ignored user → error |
| `test_tip_insufficient_funds` | Low balance → error |
| `test_tip_below_minimum` | Below min_amount → error |
| `test_tip_daily_cap` | Exceeds max_per_day → error with remaining |
| `test_tip_new_account_blocked` | Account too new → error |
| `test_tip_target_no_account` | Target doesn't exist → error |
| `test_tip_disabled` | Tipping disabled in config → error |

### 19.5 Vanity Shop Tests (`tests/test_vanity_shop.py`)

| Test | Description |
|---|---|
| `test_shop_lists_enabled_items` | Only shows enabled items |
| `test_shop_shows_discount` | Ranked user sees discounted prices |
| `test_shop_shows_owned` | Shows purchased items at bottom |
| `test_buy_greeting_success` | Charges, stores in vanity_items |
| `test_buy_greeting_too_long` | Rejects long text |
| `test_buy_color_valid` | Accepts palette color, stores hex |
| `test_buy_color_invalid` | Rejects non-palette color |
| `test_buy_gif_creates_approval` | Charges, creates pending approval |
| `test_buy_shoutout_sends_chat` | Charges, calls `client.send_chat()` |
| `test_buy_shoutout_cooldown` | Within cooldown → error |
| `test_fortune_once_per_day` | Second fortune same day → error |
| `test_fortune_different_per_user` | Different users get different fortunes |
| `test_rename_currency_success` | Stores personal currency name |
| `test_rename_currency_too_long` | Rejects >30 chars |
| `test_disabled_item_rejected` | Item disabled in config → error |

### 19.6 Blackout Tests (`tests/test_blackout.py`)

| Test | Description |
|---|---|
| `test_no_blackout_returns_false` | No windows configured → not active |
| `test_during_blackout_returns_true` | Inside window → active |
| `test_outside_blackout_returns_false` | After window ends → not active |
| `test_multiple_windows` | Overlapping windows both detected |

### 19.7 History Tests (`tests/test_history.py`)

| Test | Description |
|---|---|
| `test_history_empty` | No transactions → "No transaction history" |
| `test_history_shows_recent` | Returns last N transactions |
| `test_history_custom_limit` | `history 5` returns 5 items |
| `test_history_max_cap` | `history 100` capped at 25 |

### 19.8 Approval Tests (`tests/test_approvals.py`)

| Test | Description |
|---|---|
| `test_create_pending_approval` | Inserts record, returns ID |
| `test_resolve_approval_approved` | Status → 'approved', executes action |
| `test_resolve_approval_rejected` | Status → 'rejected', refund issued, user notified |
| `test_resolve_already_resolved` | Returns None |
| `test_list_pending_approvals` | Filters by status and type |

---

## 20. Acceptance Criteria

### Must Pass

- [ ] `search <query>` returns formatted MediaCMS results with duration-tier pricing
- [ ] `queue <id>` debits correct amount, calls `client.add_media()`, posts public announcement
- [ ] `playnext <id>` uses `position="next"` and charges interrupt price
- [ ] `forcenow <id>` creates pending approval when `force_play_requires_admin` is true
- [ ] Queue commands rejected during blackout windows with descriptive message
- [ ] Queue daily limit and cooldown enforced
- [ ] `tip @user <amount>` transfers Z between users, sends PM to receiver
- [ ] Self-tip blocked (direct and alias-aware)
- [ ] Tip daily cap enforced
- [ ] `shop` lists enabled items with rank-discounted prices
- [ ] `buy greeting <text>` stores custom greeting, debits correctly
- [ ] `buy color <name>` validates against palette, stores hex
- [ ] `buy gif <url>` creates pending approval, debits with refund-on-reject
- [ ] `buy shoutout <msg>` posts to public chat via `client.send_chat()`, enforces cooldown
- [ ] `fortune` works once per day per user
- [ ] `buy rename <name>` changes personal currency display name
- [ ] `history` shows recent transactions with correct signs
- [ ] Rank discounts applied correctly to all spend types
- [ ] All CyTube interaction uses kryten-py wrappers (`send_pm`, `send_chat`, `add_media`) — zero raw NATS
- [ ] All tests pass (~70 test cases)
- [ ] Prometheus metrics include spend counters

### Stretch

- [ ] MediaCMS result caching reduces API calls by >50% in repeated searches
- [ ] Approval workflow: reject refund arrives within 1 second
- [ ] Force-play approval auto-executes the queue action on admin approval

---

## Appendix A: Utility Helpers

### Duration Formatter

```python
def _format_duration(self, seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"
```

### Rank Tier Index Lookup

```python
def _get_rank_tier_index(self, account: dict) -> int:
    """Get the 0-based tier index for a user's rank.
    
    Checks lifetime_earned against rank tier thresholds.
    """
    lifetime = account.get("lifetime_earned", 0)
    tier_index = 0
    for i, tier in enumerate(self._config.ranks.tiers):
        if lifetime >= tier.min_lifetime_earned:
            tier_index = i
    return tier_index
```

---

## Appendix B: kryten-py Methods Used in This Sprint

| Method | Usage |
|---|---|
| `client.send_pm(channel, username, message)` | PM responses to users, tip notifications |
| `client.send_chat(channel, message)` | Public announcements, shoutouts, custom greetings |
| `client.add_media(channel, media_type, media_id, position=...)` | Queue and playnext |
| `client.nats_request(subject, request, timeout)` | Alias resolution via kryten-userstats |
| `client.subscribe_request_reply(subject, handler)` | Extended command handler |
| `@client.on("pm")` | PM command ingestion (Sprint 1) |
| `@client.on("adduser")` | Custom greeting trigger (Sprint 1 + 5 integration) |

> No direct NATS imports, no raw subject construction. Prefer kryten-py wrappers (`send_pm`, `send_chat`, `add_media`) over `client.publish()` where a wrapper exists.
