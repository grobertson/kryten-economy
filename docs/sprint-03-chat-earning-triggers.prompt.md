# Sprint 3 — Chat Earning Triggers

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 3 of 9  
> **Goal:** All chat-message-based earning mechanics. Active chatting earns bonus Z on top of passive presence.  
> **Depends on:** Sprint 1 (Core Foundation)  
> **Enables:** Sprint 5 (Spending: Queue, Tips & Vanity Shop)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Earning Engine Architecture](#3-earning-engine-architecture)
4. [Channel State Tracker](#4-channel-state-tracker)
5. [Chat Triggers](#5-chat-triggers)
6. [Content Engagement Triggers](#6-content-engagement-triggers)
7. [Social Triggers](#7-social-triggers)
8. [Cooldown & Cap System](#8-cooldown--cap-system)
9. [Fractional Earning Accumulator](#9-fractional-earning-accumulator)
10. [Trigger Analytics](#10-trigger-analytics)
11. [Daily Activity Tracking](#11-daily-activity-tracking)
12. [PM Commands: `rewards` and `like`](#12-pm-commands-rewards-and-like)
13. [Event Handler Registrations](#13-event-handler-registrations)
14. [Config Sections Activated](#14-config-sections-activated)
15. [Test Specifications](#15-test-specifications)
16. [Acceptance Criteria](#16-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint, the service additionally:

- Evaluates every `chatmsg` event against **12 configurable earning triggers** across three categories (chat, content engagement, social)
- Awards bonus Z for active participation on top of Sprint 1's passive presence earnings
- Enforces per-trigger **cooldowns and caps** via the `trigger_cooldowns` table (created in Sprint 2)
- Tracks **channel-level state** needed for context-aware triggers: last message timestamps, current media info, recent joins, silence timers
- Accumulates **fractional earnings** (e.g. 0.5 Z/message) and credits whole Z when the threshold is crossed
- Records every trigger hit in the **`trigger_analytics`** table for future admin reporting (Sprint 8)
- Tracks **GIF posts, unique emotes, kudos given/received, and message counts** in `daily_activity` for daily competitions (Sprint 7)
- Responds to **`rewards`** PM command — shows non-hidden earning triggers as a partial guide
- Responds to **`like`** PM command — earn 2 Z for liking the currently playing media
- All triggers are independently **enabled/disabled, configurable, and hidden/visible** via YAML config
- Ignored users are rejected at the first gate and excluded from all "first to X" candidate pools

---

## 2. New Database Tables

### 2.1 `trigger_analytics` Table

Add this table during database initialization (idempotent `CREATE TABLE IF NOT EXISTS`).

```sql
CREATE TABLE IF NOT EXISTS trigger_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    date TEXT NOT NULL,             -- YYYY-MM-DD
    hit_count INTEGER DEFAULT 0,
    unique_users INTEGER DEFAULT 0,
    total_z_awarded INTEGER DEFAULT 0,
    UNIQUE(channel, trigger_id, date)
);
```

> **Note:** The `trigger_cooldowns` table was created in Sprint 2. This sprint is its primary consumer.

---

## 3. Earning Engine Architecture

### 3.1 File: `kryten_economy/earning_engine.py`

This is a new module — the central evaluation pipeline for all chat-based earning triggers.

### 3.2 Class: `EarningEngine`

```python
from dataclasses import dataclass, field

@dataclass
class TriggerResult:
    """Outcome of a single trigger evaluation."""
    trigger_id: str
    amount: int          # Whole Z awarded (0 if cooldown/cap blocked)
    blocked_by: str | None = None  # "cooldown", "cap", "disabled", "condition", None

@dataclass
class EarningOutcome:
    """Result of evaluating all triggers for a single chat message."""
    username: str
    channel: str
    results: list[TriggerResult] = field(default_factory=list)
    
    @property
    def total_earned(self) -> int:
        return sum(r.amount for r in self.results)
    
    @property
    def awarded_triggers(self) -> list[TriggerResult]:
        return [r for r in self.results if r.amount > 0]
```

### 3.3 Constructor

```python
class EarningEngine:
    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        channel_state: ChannelStateTracker,
        logger: logging.Logger,
    ):
        self._config = config
        self._db = database
        self._channel_state = channel_state
        self._logger = logger
        
        # Fractional earning accumulators: (username, channel, trigger_id) → float
        self._fractional: dict[tuple[str, str, str], float] = {}
        
        # Build the set of ignored usernames (lowercase) for fast lookup
        self._ignored_users: set[str] = {
            u.lower() for u in (config.ignored_users or [])
        }
```

### 3.4 Main Evaluation Method

```python
async def evaluate_chat_message(
    self, username: str, channel: str, message: str, timestamp: datetime
) -> EarningOutcome:
    """Evaluate a chat message against all configured triggers.
    
    First gate: reject if username is in ignored_users (case-insensitive).
    Then per-trigger: enabled → cooldown → cap → condition → award.
    Returns an EarningOutcome with results for every evaluated trigger.
    """
    outcome = EarningOutcome(username=username, channel=channel)
    
    # ── Gate: Ignored users earn nothing ────────────────────
    if username.lower() in self._ignored_users:
        return outcome
    
    # ── Evaluate chat triggers ──────────────────────────────
    chat_cfg = self._config.chat_triggers
    
    if chat_cfg.long_message.enabled:
        outcome.results.append(
            await self._eval_long_message(username, channel, message, timestamp)
        )
    
    if chat_cfg.first_message_of_day.enabled:
        outcome.results.append(
            await self._eval_first_message_of_day(username, channel, timestamp)
        )
    
    if chat_cfg.conversation_starter.enabled:
        outcome.results.append(
            await self._eval_conversation_starter(username, channel, timestamp)
        )
    
    # first_after_media_change
    if self._config.content_triggers.first_after_media_change.enabled:
        outcome.results.append(
            await self._eval_first_after_media_change(username, channel, timestamp)
        )
    
    # ── Evaluate content engagement triggers ────────────────
    content_cfg = self._config.content_triggers
    
    if content_cfg.comment_during_media.enabled:
        outcome.results.append(
            await self._eval_comment_during_media(username, channel, message, timestamp)
        )
    
    # survived_full_media is NOT evaluated per-message — see Section 6.3
    
    # ── Evaluate social triggers ────────────────────────────
    social_cfg = self._config.social_triggers
    
    if social_cfg.greeted_newcomer.enabled:
        outcome.results.append(
            await self._eval_greeted_newcomer(username, channel, message, timestamp)
        )
    
    if social_cfg.mentioned_by_other.enabled:
        await self._eval_mentioned_by_other(username, channel, message, timestamp, outcome)
    
    if social_cfg.bot_interaction.enabled:
        # bot_interaction is evaluated externally (see Section 7.3)
        pass
    
    # ── Track daily activity ────────────────────────────────
    await self._update_daily_activity(username, channel, message, timestamp)
    
    # ── Credit earned Z ─────────────────────────────────────
    for result in outcome.awarded_triggers:
        await self._db.credit(
            username, channel, result.amount,
            tx_type="earn",
            trigger_id=result.trigger_id,
            reason=f"Chat trigger: {result.trigger_id}",
        )
        await self._record_analytics(channel, result.trigger_id, result.amount, timestamp)
    
    # ── Update last message time (for conversation_starter) ─
    self._channel_state.record_message(channel, username, timestamp)
    
    return outcome
```

### 3.5 Reactive Triggers (Called Externally)

Some triggers cannot be evaluated inside `evaluate_chat_message` because they respond to other users' messages about the original user. These are called by separate event handlers or by the chat pipeline itself:

| Trigger | Invocation |
|---|---|
| `laugh_received` | Called by a secondary scan when a laugh phrase is detected in a message, attributing it to the recent joke-teller |
| `kudos_received` | Called when a `++` pattern is detected in a message, crediting the target user |
| `bot_interaction` | Called when the LLM event (`chatmsg` from the bot in response to a user) is detected |
| `survived_full_media` | Called on `changemedia` when the previous media ends |
| `like_current` | Called from PM handler when user sends `like` |

These all share the same cooldown/cap infrastructure but are invoked from different call sites (see Sections 5–7 for details).

---

## 4. Channel State Tracker

### 4.1 File: `kryten_economy/channel_state.py`

A new module that holds volatile, channel-scoped state needed by the earning engine. This state is in-memory only — it does not persist across restarts. On restart, triggers that depend on this state simply start fresh (no historical credit, no harm).

### 4.2 Data Structures

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class MediaInfo:
    """Currently playing media item."""
    title: str
    media_id: str           # CyTube media UID
    duration_seconds: float # 0 if unknown
    started_at: datetime
    users_present_at_start: set[str] = field(default_factory=set)

@dataclass
class ChannelState:
    """Volatile state for a single channel."""
    # Media tracking
    current_media: MediaInfo | None = None
    
    # Conversation starter
    last_message_time: datetime | None = None
    last_message_user: str | None = None
    
    # First-after-media-change
    first_comment_after_media: str | None = None  # Username who claimed it, or None
    media_change_time: datetime | None = None
    
    # Newcomer greeting detection
    # {username: join_time} — populated from presence_tracker genuine arrivals
    recent_joins: dict[str, datetime] = field(default_factory=dict)
    
    # Per-media-item state for comment_during_media
    # {username: messages_counted}
    comment_counts_this_media: dict[str, int] = field(default_factory=dict)
    
    # Per-media-item state for like_current
    # Set of usernames who have liked the current media
    users_liked_current: set[str] = field(default_factory=set)
    
    # Per-media-item state for survived_full_media
    # {username: joined_before_time} — users present when media started
    users_at_media_start: set[str] = field(default_factory=set)
```

### 4.3 Class: `ChannelStateTracker`

```python
class ChannelStateTracker:
    """Manages volatile per-channel state for the earning engine."""
    
    def __init__(self, config: EconomyConfig, logger: logging.Logger):
        self._config = config
        self._logger = logger
        self._states: dict[str, ChannelState] = {}  # channel → state
        self._ignored_users: set[str] = {
            u.lower() for u in (config.ignored_users or [])
        }
    
    def _get(self, channel: str) -> ChannelState:
        if channel not in self._states:
            self._states[channel] = ChannelState()
        return self._states[channel]
    
    # ── Message tracking ────────────────────────────────────
    
    def record_message(self, channel: str, username: str, timestamp: datetime) -> None:
        """Record that a message was sent. Updates conversation-starter tracking."""
        state = self._get(channel)
        state.last_message_time = timestamp
        state.last_message_user = username
    
    def get_silence_seconds(self, channel: str, now: datetime) -> float | None:
        """Seconds since last message in channel. None if no messages recorded."""
        state = self._get(channel)
        if state.last_message_time is None:
            return None
        return (now - state.last_message_time).total_seconds()
    
    # ── Media tracking ──────────────────────────────────────
    
    def handle_media_change(
        self, channel: str, title: str, media_id: str,
        duration_seconds: float, connected_users: set[str], timestamp: datetime,
    ) -> MediaInfo | None:
        """Process a media change event. Returns the PREVIOUS media info
        (for survived_full_media evaluation), or None if there was none."""
        state = self._get(channel)
        previous = state.current_media
        
        # Filter ignored users from "present at start"
        non_ignored = {u for u in connected_users if u.lower() not in self._ignored_users}
        
        state.current_media = MediaInfo(
            title=title,
            media_id=media_id,
            duration_seconds=duration_seconds,
            started_at=timestamp,
            users_present_at_start=non_ignored,
        )
        
        # Reset per-media counters
        state.first_comment_after_media = None
        state.media_change_time = timestamp
        state.comment_counts_this_media.clear()
        state.users_liked_current.clear()
        state.users_at_media_start = non_ignored.copy()
        
        return previous
    
    def get_current_media(self, channel: str) -> MediaInfo | None:
        return self._get(channel).current_media
    
    # ── First-after-media-change ────────────────────────────
    
    def try_claim_first_after_media(
        self, channel: str, username: str, now: datetime,
    ) -> bool:
        """Attempt to claim 'first comment after media change'.
        Returns True if this user is the first (and within the window)."""
        state = self._get(channel)
        if state.first_comment_after_media is not None:
            return False  # Already claimed
        if state.media_change_time is None:
            return False  # No media change recorded
        
        window = self._config.content_triggers.first_after_media_change.window_seconds
        elapsed = (now - state.media_change_time).total_seconds()
        if elapsed > window:
            return False  # Outside window
        
        state.first_comment_after_media = username
        return True
    
    # ── Comment-during-media tracking ───────────────────────
    
    def increment_media_comments(self, channel: str, username: str) -> int:
        """Increment and return the user's comment count for the current media."""
        state = self._get(channel)
        count = state.comment_counts_this_media.get(username, 0) + 1
        state.comment_counts_this_media[username] = count
        return count
    
    def get_media_comment_cap(self, channel: str) -> int:
        """Calculate the comment cap for the current media.
        If scale_with_duration is enabled: base × (duration_min / 30), min = base.
        Otherwise: base cap."""
        cfg = self._config.content_triggers.comment_during_media
        base_cap = cfg.max_per_item_base
        state = self._get(channel)
        
        if cfg.scale_with_duration and state.current_media:
            duration_min = state.current_media.duration_seconds / 60
            scaled = int(base_cap * (duration_min / 30))
            return max(scaled, base_cap)
        
        return base_cap
    
    # ── Like tracking ───────────────────────────────────────
    
    def try_like_current(self, channel: str, username: str) -> bool:
        """Attempt to like the current media. Returns True if this is a new like."""
        state = self._get(channel)
        if state.current_media is None:
            return False
        if username in state.users_liked_current:
            return False
        state.users_liked_current.add(username)
        return True
    
    # ── Newcomer tracking ───────────────────────────────────
    
    def record_genuine_join(self, channel: str, username: str, timestamp: datetime) -> None:
        """Called by presence_tracker when a genuine (debounced) arrival occurs."""
        if username.lower() in self._ignored_users:
            return  # Bot joins never trigger greeting detection
        state = self._get(channel)
        state.recent_joins[username.lower()] = timestamp
    
    def get_recent_joiners(self, channel: str, now: datetime, window_seconds: int) -> dict[str, datetime]:
        """Return {username: join_time} for users who joined within window_seconds.
        Prunes expired entries."""
        state = self._get(channel)
        active = {}
        expired = []
        for uname, join_time in state.recent_joins.items():
            if (now - join_time).total_seconds() <= window_seconds:
                active[uname] = join_time
            else:
                expired.append(uname)
        for uname in expired:
            del state.recent_joins[uname]
        return active
    
    # ── Survived-full-media helpers ─────────────────────────
    
    def get_users_at_media_start(self, channel: str) -> set[str]:
        return self._get(channel).users_at_media_start
```

### 4.4 Integration with Presence Tracker

The `PresenceTracker` must call `channel_state.record_genuine_join()` inside its `handle_user_join()` method when a genuine arrival is confirmed. This populates the recent-joins dict for greeting detection.

```python
# In presence_tracker.py, handle_user_join(), genuine arrival branch:
if genuine:
    # ... existing welcome wallet / welcome-back logic ...
    self._channel_state.record_genuine_join(channel, username, now)
```

---

## 5. Chat Triggers

### 5.1 `long_message`

**Trigger ID:** `chat.long_message`

**Config:**
```yaml
chat_triggers:
  long_message:
    enabled: true
    min_chars: 30
    reward: 1
    max_per_hour: 30
    hidden: true
```

**Condition:** `len(message) >= min_chars`

**Cooldown:** Hourly cap — max `max_per_hour` hits per rolling 60-minute window.

**Implementation:**
```python
async def _eval_long_message(
    self, username: str, channel: str, message: str, timestamp: datetime,
) -> TriggerResult:
    trigger_id = "chat.long_message"
    cfg = self._config.chat_triggers.long_message
    
    if len(message) < cfg.min_chars:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    if not await self._check_cooldown(username, channel, trigger_id, cfg.max_per_hour, 3600, timestamp):
        return TriggerResult(trigger_id, 0, blocked_by="cap")
    
    return TriggerResult(trigger_id, cfg.reward)
```

---

### 5.2 `first_message_of_day`

**Trigger ID:** `chat.first_message_of_day`

**Config:**
```yaml
chat_triggers:
  first_message_of_day:
    enabled: true
    reward: 5
    hidden: true
```

**Condition:** User has not sent any message today (checked via `daily_activity.first_message_claimed`).

**Cooldown:** Implicit (one per day, enforced by DB flag).

**Implementation:**
```python
async def _eval_first_message_of_day(
    self, username: str, channel: str, timestamp: datetime,
) -> TriggerResult:
    trigger_id = "chat.first_message_of_day"
    cfg = self._config.chat_triggers.first_message_of_day
    today = timestamp.strftime("%Y-%m-%d")
    
    activity = await self._db.get_or_create_daily_activity(username, channel, today)
    if activity.get("first_message_claimed"):
        return TriggerResult(trigger_id, 0, blocked_by="cap")
    
    await self._db.mark_first_message_claimed(username, channel, today)
    return TriggerResult(trigger_id, cfg.reward)
```

**Database method to add:**
```python
async def mark_first_message_claimed(self, username: str, channel: str, date: str) -> None:
    """Set first_message_claimed = 1 for the given day."""
```

---

### 5.3 `conversation_starter`

**Trigger ID:** `chat.conversation_starter`

**Config:**
```yaml
chat_triggers:
  conversation_starter:
    enabled: true
    min_silence_minutes: 10
    reward: 10
    hidden: true
```

**Condition:** No message in the channel for ≥ `min_silence_minutes`. The user who breaks the silence earns the bonus. Ignored users' messages do NOT reset the silence timer (they are rejected before `record_message()` is called).

**Cooldown:** None beyond the natural silence gap.

**Implementation:**
```python
async def _eval_conversation_starter(
    self, username: str, channel: str, timestamp: datetime,
) -> TriggerResult:
    trigger_id = "chat.conversation_starter"
    cfg = self._config.chat_triggers.conversation_starter
    
    silence = self._channel_state.get_silence_seconds(channel, timestamp)
    
    # None means no messages recorded yet (fresh start) — treat as qualifying
    if silence is not None and silence < cfg.min_silence_minutes * 60:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    return TriggerResult(trigger_id, cfg.reward)
```

> **Important ordering:** `_eval_conversation_starter()` must be called BEFORE `channel_state.record_message()` updates the last-message timestamp. The evaluation pipeline in `evaluate_chat_message()` evaluates all triggers first, then calls `record_message()` at the end.

---

### 5.4 `laugh_received`

**Trigger ID:** `chat.laugh_received`

**Config:**
```yaml
chat_triggers:
  laugh_received:
    enabled: true
    reward_per_laugher: 2
    max_laughers_per_joke: 10
    self_excluded: true
    hidden: true
```

**Condition:** Another user's message contains a laugh phrase (see detection patterns below). The reward goes to the **joke-teller** — the user who sent the most recent non-laugh message before this laugh reaction.

**Self-exclusion:** If the laughing user is the same as the joke-teller (including aliases), no reward.

**Detection patterns** (case-insensitive, partial match):

```python
LAUGH_PATTERNS: list[re.Pattern] = [
    re.compile(r'\b(?:lol|lmao|lmfao|rofl|haha|hahaha+|hehe+|kek|dead|💀|😂|🤣)\b', re.IGNORECASE),
    re.compile(r'\b(?:ha){2,}\b', re.IGNORECASE),         # "haha", "hahaha", etc.
    re.compile(r'^(?:lol|lmao|lmfao|rofl)$', re.IGNORECASE),  # Standalone laugh
]
```

**Architecture:** Laugh detection runs inside `evaluate_chat_message()` but credits the **previous message sender**, not the current one. This requires a two-step process:

```python
async def _eval_laugh_received(
    self, laughing_user: str, channel: str, message: str, timestamp: datetime,
) -> TriggerResult | None:
    """Evaluate if this message is a laugh reaction.
    Returns a TriggerResult for the JOKE-TELLER (not the laughing user), or None."""
    trigger_id = "chat.laugh_received"
    cfg = self._config.chat_triggers.laugh_received
    
    if not self._is_laugh(message):
        return None
    
    # Who told the joke? The last message sender in this channel (who isn't the laugher)
    joke_teller = self._channel_state.get_last_non_self_message_user(channel, laughing_user)
    if joke_teller is None:
        return None
    
    # Self-exclusion (including alias check)
    if cfg.self_excluded and laughing_user.lower() == joke_teller.lower():
        return None
    # TODO (Sprint 6+): Query kryten-userstats for alias resolution
    
    # Cap: max laughers per joke (per joke-teller, rolling)
    if not await self._check_cooldown(
        joke_teller, channel, trigger_id, cfg.max_laughers_per_joke, 300, timestamp
    ):
        return None  # Too many laughs in window
    
    return TriggerResult(trigger_id, cfg.reward_per_laugher)
```

**Credit routing:** The `evaluate_chat_message()` pipeline calls `_eval_laugh_received()`. If it returns a result, it credits the **joke_teller** (not `username`):

```python
# In evaluate_chat_message(), after standard trigger evaluation:
if chat_cfg.laugh_received.enabled:
    laugh_result = await self._eval_laugh_received(username, channel, message, timestamp)
    if laugh_result and laugh_result.amount > 0:
        joke_teller = self._channel_state.get_last_non_self_message_user(channel, username)
        if joke_teller:
            await self._db.credit(
                joke_teller, channel, laugh_result.amount,
                tx_type="earn",
                trigger_id=laugh_result.trigger_id,
                reason=f"Laugh from {username}",
                related_user=username,
            )
            await self._record_analytics(
                channel, laugh_result.trigger_id, laugh_result.amount, timestamp
            )
            # Update daily_activity for joke_teller
            today = timestamp.strftime("%Y-%m-%d")
            await self._db.increment_daily_laughs_received(joke_teller, channel, today)
```

**Channel state addition:** `ChannelStateTracker` needs a method to retrieve the last message sender who isn't the current user:

```python
def get_last_non_self_message_user(self, channel: str, current_user: str) -> str | None:
    """Return the username who sent the last message before this one,
    excluding current_user. Returns None if no qualifying message found."""
    state = self._get(channel)
    if state.last_message_user and state.last_message_user.lower() != current_user.lower():
        return state.last_message_user
    return None
```

> **Note:** This is a simplified "last sender" approach. A more sophisticated implementation could track the last N messages to handle interleaved conversation. For v1, the single last-sender model is sufficient and easy to reason about.

---

### 5.5 `kudos_received`

**Trigger ID:** `chat.kudos_received`

**Config:**
```yaml
chat_triggers:
  kudos_received:
    enabled: true
    reward: 3
    self_excluded: true          # Alias-aware via kryten-userstats
    hidden: true
```

**Condition:** A message contains the pattern `@username++` or `username++` (case-insensitive, word boundary). Credits the **target** user, not the sender.

**Detection pattern:**
```python
KUDOS_PATTERN = re.compile(r'(?:^|\s)@?(\S+)\+\+', re.IGNORECASE)
```

**Self-exclusion:** Sender cannot kudos themselves (or their aliases).

**Architecture:** Like `laugh_received`, this credits a different user than the message sender. It runs inside `evaluate_chat_message()`:

```python
async def _eval_kudos_received(
    self, sender: str, channel: str, message: str, timestamp: datetime,
) -> list[tuple[str, TriggerResult]]:
    """Detect kudos targets in message. Returns list of (target_username, TriggerResult).
    A single message may kudos multiple users."""
    trigger_id = "chat.kudos_received"
    cfg = self._config.chat_triggers.kudos_received
    results: list[tuple[str, TriggerResult]] = []
    
    matches = KUDOS_PATTERN.findall(message)
    seen_targets: set[str] = set()
    
    for target_raw in matches:
        target = target_raw.strip().lower()
        
        if target in seen_targets:
            continue
        seen_targets.add(target)
        
        # Self-exclusion
        if cfg.self_excluded and target == sender.lower():
            continue
        # TODO (Sprint 6+): Query kryten-userstats alias.resolve for alias-aware exclusion
        
        # Ignored users cannot receive kudos
        if target in self._ignored_users:
            continue
        
        results.append((
            target_raw,  # Preserve original casing for DB
            TriggerResult(trigger_id, cfg.reward),
        ))
    
    return results
```

**Credit routing:** In `evaluate_chat_message()`:

```python
if chat_cfg.kudos_received.enabled:
    kudos_results = await self._eval_kudos_received(username, channel, message, timestamp)
    for target, result in kudos_results:
        if result.amount > 0:
            await self._db.credit(
                target, channel, result.amount,
                tx_type="earn",
                trigger_id=result.trigger_id,
                reason=f"Kudos from {username}",
                related_user=username,
            )
            await self._record_analytics(channel, result.trigger_id, result.amount, timestamp)
            today = timestamp.strftime("%Y-%m-%d")
            await self._db.increment_daily_kudos_received(target, channel, today)
            await self._db.increment_daily_kudos_given(username, channel, today)
```

---

### 5.6 `first_after_media_change`

**Trigger ID:** `content.first_after_media_change`

**Config:**
```yaml
content_triggers:
  first_after_media_change:
    enabled: true
    window_seconds: 30
    reward: 3
    hidden: true
```

**Condition:** First message in the channel within `window_seconds` after a `changemedia` event. Only one user can claim this per media change. Ignored users cannot claim it — their messages do not count.

**Implementation:**
```python
async def _eval_first_after_media_change(
    self, username: str, channel: str, timestamp: datetime,
) -> TriggerResult:
    trigger_id = "content.first_after_media_change"
    cfg = self._config.content_triggers.first_after_media_change
    
    claimed = self._channel_state.try_claim_first_after_media(channel, username, timestamp)
    if not claimed:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    return TriggerResult(trigger_id, cfg.reward)
```

---

## 6. Content Engagement Triggers

### 6.1 `comment_during_media`

**Trigger ID:** `content.comment_during_media`

**Config:**
```yaml
content_triggers:
  comment_during_media:
    enabled: true
    reward_per_message: 0.5
    max_per_item_base: 10        # Base cap; scales with duration if enabled
    scale_with_duration: true    # Cap = base × (duration_minutes / 30), min = base
    hidden: true
```

**Condition:** A message sent while media is currently playing. Every qualifying message earns `reward_per_message` Z (which may be fractional — see Section 9).

**Cap:** Per-user, per-media-item cap. If `scale_with_duration` is true, the cap scales with the media's duration (longer content → higher cap → more rewarded engagement).

**Implementation:**
```python
async def _eval_comment_during_media(
    self, username: str, channel: str, message: str, timestamp: datetime,
) -> TriggerResult:
    trigger_id = "content.comment_during_media"
    cfg = self._config.content_triggers.comment_during_media
    
    # Must have active media
    media = self._channel_state.get_current_media(channel)
    if media is None:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    # Check per-item cap
    comment_count = self._channel_state.increment_media_comments(channel, username)
    cap = self._channel_state.get_media_comment_cap(channel)
    if comment_count > cap:
        return TriggerResult(trigger_id, 0, blocked_by="cap")
    
    # Fractional earning
    amount = self._accumulate_fractional(username, channel, trigger_id, cfg.reward_per_message)
    return TriggerResult(trigger_id, amount)
```

---

### 6.2 `like_current`

**Trigger ID:** `content.like_current`

**Config:**
```yaml
content_triggers:
  like_current:
    enabled: true
    reward: 2
    hidden: true
```

**Condition:** User sends `like` as a PM command. They must be connected to the channel with media currently playing. One like per user per media item.

**Implementation:** This is a PM command, not a chat trigger. It is handled by the PM handler, not the earning engine. However, the earning engine provides the evaluation method:

```python
async def evaluate_like_current(self, username: str, channel: str) -> TriggerResult:
    """Called by PM handler when user sends 'like'."""
    trigger_id = "content.like_current"
    cfg = self._config.content_triggers.like_current
    
    if not cfg.enabled:
        return TriggerResult(trigger_id, 0, blocked_by="disabled")
    
    if username.lower() in self._ignored_users:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    if not self._channel_state.try_like_current(channel, username):
        return TriggerResult(trigger_id, 0, blocked_by="cap")
    
    # Credit directly
    await self._db.credit(
        username, channel, cfg.reward,
        tx_type="earn",
        trigger_id=trigger_id,
        reason="Liked current media",
    )
    await self._record_analytics(channel, trigger_id, cfg.reward, datetime.now(timezone.utc))
    
    return TriggerResult(trigger_id, cfg.reward)
```

---

### 6.3 `survived_full_media`

**Trigger ID:** `content.survived_full_media`

**Config:**
```yaml
content_triggers:
  survived_full_media:
    enabled: true
    min_presence_percent: 80
    reward: 5
    hidden: true
```

**Condition:** When media changes (`changemedia` event), evaluate the **previous** media. Users who were present at the start AND still connected now, with the media having run for ≥ `min_presence_percent`% of its duration, earn the reward.

**Implementation:** Called from the `changemedia` event handler, NOT from `evaluate_chat_message()`.

```python
async def evaluate_survived_full_media(
    self, channel: str, previous_media: MediaInfo,
    currently_connected: set[str], now: datetime,
) -> list[str]:
    """Evaluate survived_full_media for the media that just ended.
    Returns list of usernames who earned the reward."""
    trigger_id = "content.survived_full_media"
    cfg = self._config.content_triggers.survived_full_media
    
    if not cfg.enabled:
        return []
    
    if previous_media.duration_seconds <= 0:
        return []  # Unknown duration — can't evaluate
    
    # How long did the media actually play?
    actual_seconds = (now - previous_media.started_at).total_seconds()
    presence_ratio = actual_seconds / previous_media.duration_seconds
    
    # If the media was skipped early (< min_presence_percent of duration played)
    # no one qualifies
    if presence_ratio < (cfg.min_presence_percent / 100):
        return []
    
    # Users who were present at media start AND still connected now
    survivors = previous_media.users_present_at_start & currently_connected
    
    # Filter ignored users
    survivors = {u for u in survivors if u.lower() not in self._ignored_users}
    
    rewarded = []
    today = now.strftime("%Y-%m-%d")
    for username in survivors:
        await self._db.credit(
            username, channel, cfg.reward,
            tx_type="earn",
            trigger_id=trigger_id,
            reason=f"Survived: {previous_media.title}",
        )
        await self._record_analytics(channel, trigger_id, cfg.reward, now)
        rewarded.append(username)
    
    return rewarded
```

---

## 7. Social Triggers

### 7.1 `greeted_newcomer`

**Trigger ID:** `social.greeted_newcomer`

**Config:**
```yaml
social_triggers:
  greeted_newcomer:
    enabled: true
    window_seconds: 60
    reward: 3
    bot_joins_excluded: true    # Joins by users in ignored_users list never trigger this
    hidden: true
```

**Condition:** A user's chat message contains the **username** (case-insensitive) of someone who genuinely joined within the last `window_seconds`. The greeting user earns the reward. Only the **first** greeter for each newcomer earns (one reward per join event).

**Bot exclusion:** Joins by users in the `ignored_users` list are never recorded in `recent_joins` (handled in `channel_state.record_genuine_join()`), so they can never trigger greeting rewards.

**Implementation:**
```python
async def _eval_greeted_newcomer(
    self, username: str, channel: str, message: str, timestamp: datetime,
) -> TriggerResult:
    trigger_id = "social.greeted_newcomer"
    cfg = self._config.social_triggers.greeted_newcomer
    
    # Get recent joiners within window
    recent = self._channel_state.get_recent_joiners(channel, timestamp, cfg.window_seconds)
    
    if not recent:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    message_lower = message.lower()
    
    for joiner_name, join_time in recent.items():
        # Can't greet yourself
        if joiner_name == username.lower():
            continue
        
        # Check if the joiner's name appears in the message
        if joiner_name in message_lower:
            # Remove from recent_joins to prevent double-reward
            self._channel_state.consume_greeting(channel, joiner_name)
            return TriggerResult(trigger_id, cfg.reward)
    
    return TriggerResult(trigger_id, 0, blocked_by="condition")
```

**Channel state addition:**
```python
def consume_greeting(self, channel: str, joiner_name_lower: str) -> None:
    """Remove a joiner from recent_joins after they've been greeted."""
    state = self._get(channel)
    state.recent_joins.pop(joiner_name_lower, None)
```

---

### 7.2 `mentioned_by_other`

**Trigger ID:** `social.mentioned_by_other`

**Config:**
```yaml
social_triggers:
  mentioned_by_other:
    enabled: true
    reward: 1
    max_per_hour_same_user: 5
    hidden: true
```

**Condition:** A user's message contains another user's **username** (case-insensitive, word boundary). The **mentioned** user earns the reward (not the sender). Caps: max 5 mentions per hour from the same sender to the same target.

**Self-exclusion:** Mentioning yourself earns nothing.

**Implementation:** This trigger may credit **multiple** users from a single message (if multiple names are mentioned). It appends results directly to the `EarningOutcome`:

```python
async def _eval_mentioned_by_other(
    self, sender: str, channel: str, message: str,
    timestamp: datetime, outcome: EarningOutcome,
) -> None:
    trigger_id = "social.mentioned_by_other"
    cfg = self._config.social_triggers.mentioned_by_other
    message_lower = message.lower()
    
    # Get all connected users in the channel (from presence_tracker)
    connected = self._presence_tracker.get_connected_users(channel)
    
    for target in connected:
        if target.lower() == sender.lower():
            continue  # Can't earn from mentioning yourself
        if target.lower() in self._ignored_users:
            continue  # Ignored users don't earn
        
        if target.lower() in message_lower:
            # Per-pair cooldown: sender → target, max N per hour
            cooldown_key = f"{trigger_id}.{sender.lower()}.{target.lower()}"
            if not await self._check_cooldown(
                target, channel, cooldown_key, cfg.max_per_hour_same_user, 3600, timestamp
            ):
                continue  # Cap reached for this pair
            
            await self._db.credit(
                target, channel, cfg.reward,
                tx_type="earn",
                trigger_id=trigger_id,
                reason=f"Mentioned by {sender}",
                related_user=sender,
            )
            await self._record_analytics(channel, trigger_id, cfg.reward, timestamp)
```

> **Note:** The `_eval_mentioned_by_other()` method handles its own crediting because it may credit multiple users per message. It does NOT return a `TriggerResult` to the standard pipeline.

### 7.3 `bot_interaction`

**Trigger ID:** `social.bot_interaction`

**Config:**
```yaml
social_triggers:
  bot_interaction:
    enabled: true
    reward: 2
    max_per_day: 10
    hidden: true
```

**Condition:** The user's message triggered an LLM response from the bot. This is detected by observing the bot's response messages on the `chatmsg` event stream — when the bot account (from `config.bot.username`) sends a message that appears to be a reply, the previous human message sender gets credited.

**Alternative detection approach:** If kryten-llm publishes a custom event when generating a response, subscribe via `client.subscribe("kryten.llm.events.response", handler)` for more reliable detection. **Do NOT use `@client.on("llm_response")` — `@client.on()` is exclusively for CyTube socket events and `"llm_response"` is not a CyTube event.** Check kryten-llm's documentation for its event contract.

**Implementation:** This trigger is evaluated **outside** the main `evaluate_chat_message()` pipeline. It is called from the `chatmsg` handler when the message sender is the bot account:

```python
async def evaluate_bot_interaction(
    self, responding_to_user: str, channel: str, timestamp: datetime,
) -> TriggerResult:
    """Called when the bot responds to a user. Credits the user who triggered the response."""
    trigger_id = "social.bot_interaction"
    cfg = self._config.social_triggers.bot_interaction
    
    if not cfg.enabled:
        return TriggerResult(trigger_id, 0, blocked_by="disabled")
    
    if responding_to_user.lower() in self._ignored_users:
        return TriggerResult(trigger_id, 0, blocked_by="condition")
    
    # Daily cap
    today = timestamp.strftime("%Y-%m-%d")
    activity = await self._db.get_or_create_daily_activity(responding_to_user, channel, today)
    if activity.get("bot_interactions", 0) >= cfg.max_per_day:
        return TriggerResult(trigger_id, 0, blocked_by="cap")
    
    await self._db.increment_daily_bot_interactions(responding_to_user, channel, today)
    
    await self._db.credit(
        responding_to_user, channel, cfg.reward,
        tx_type="earn",
        trigger_id=trigger_id,
        reason="Bot interaction",
    )
    await self._record_analytics(channel, trigger_id, cfg.reward, timestamp)
    
    return TriggerResult(trigger_id, cfg.reward)
```

---

## 8. Cooldown & Cap System

### 8.1 Overview

Per-trigger cooldowns and caps are enforced via the `trigger_cooldowns` table (created in Sprint 2). Each row tracks a (username, channel, trigger_id) combination with a count and window start time.

### 8.2 Core Cooldown Method

```python
async def _check_cooldown(
    self, username: str, channel: str, trigger_id: str,
    max_count: int, window_seconds: int, now: datetime,
) -> bool:
    """Check if the user is within the cooldown/cap for this trigger.
    Returns True if the action is ALLOWED (within cap).
    Returns False if the cap has been reached.
    Automatically increments the count if allowed."""
    
    row = await self._db.get_trigger_cooldown(username, channel, trigger_id)
    
    if row is None:
        # First ever hit — create entry
        await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
        return True
    
    window_start = parse_timestamp(row["window_start"])
    elapsed = (now - window_start).total_seconds()
    
    if elapsed >= window_seconds:
        # Window expired — reset
        await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
        return True
    
    if row["count"] >= max_count:
        return False  # Cap reached
    
    # Within window, under cap — increment
    await self._db.increment_trigger_cooldown(username, channel, trigger_id)
    return True
```

### 8.3 Database Methods to Add

```python
async def get_trigger_cooldown(
    self, username: str, channel: str, trigger_id: str,
) -> dict | None:
    """Return the cooldown row, or None if not exists."""

async def set_trigger_cooldown(
    self, username: str, channel: str, trigger_id: str,
    count: int, window_start: datetime,
) -> None:
    """Insert or replace cooldown entry."""

async def increment_trigger_cooldown(
    self, username: str, channel: str, trigger_id: str,
) -> None:
    """Increment count by 1 for an existing cooldown entry."""
```

### 8.4 Cooldown Key Conventions

Standard triggers use the trigger_id as the cooldown key:

| Trigger | Cooldown Key | Window | Max |
|---|---|---|---|
| `long_message` | `chat.long_message` | 3600s (1 hour) | 30 |
| `mentioned_by_other` | `social.mentioned_by_other.{sender}.{target}` | 3600s (1 hour) | 5 |
| `bot_interaction` | Uses `daily_activity.bot_interactions` instead | Daily | 10 |

Triggers without explicit cooldown config (e.g. `first_message_of_day`, `conversation_starter`, `first_after_media_change`) use their natural limiting mechanisms (daily flag, silence gap, per-media claim slot) rather than the cooldown table.

---

## 9. Fractional Earning Accumulator

### 9.1 Overview

Some triggers award sub-integer Z (e.g. `comment_during_media` at 0.5 Z/message). Since balances are integer-only, fractional earnings are accumulated in memory and credited as whole Z when they cross the threshold.

### 9.2 Implementation

```python
def _accumulate_fractional(
    self, username: str, channel: str, trigger_id: str, amount: float,
) -> int:
    """Add fractional amount to the accumulator. Returns whole Z to credit (may be 0)."""
    key = (username, channel, trigger_id)
    current = self._fractional.get(key, 0.0)
    current += amount
    whole = int(current)
    self._fractional[key] = current - whole
    return whole
```

### 9.3 Example

```
Message 1: 0.5 → accumulator = 0.5, credit 0 Z
Message 2: 0.5 → accumulator = 1.0, credit 1 Z, accumulator resets to 0.0
Message 3: 0.5 → accumulator = 0.5, credit 0 Z
...
```

### 9.4 Volatility

The fractional accumulator is in-memory only. On service restart, partial fractions are lost. This is acceptable — the maximum loss per user per trigger is < 1 Z. No persistence needed.

---

## 10. Trigger Analytics

### 10.1 Overview

Every trigger hit (successful or not) is optionally tracked in the `trigger_analytics` table for admin visibility (Sprint 8) and economy health monitoring. For Sprint 3, we only record **successful** hits (amount > 0).

### 10.2 Implementation

```python
async def _record_analytics(
    self, channel: str, trigger_id: str, amount: int, timestamp: datetime,
) -> None:
    """Record a trigger hit in the analytics table."""
    date = timestamp.strftime("%Y-%m-%d")
    await self._db.record_trigger_analytics(channel, trigger_id, date, amount)
```

### 10.3 Database Method to Add

```python
async def record_trigger_analytics(
    self, channel: str, trigger_id: str, date: str, z_awarded: int,
) -> None:
    """Upsert trigger analytics: increment hit_count, add to total_z_awarded.
    For unique_users: use INSERT OR IGNORE with a separate tracking approach,
    or accept an approximate count for v1."""
    def _sync():
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO trigger_analytics (channel, trigger_id, date, hit_count, unique_users, total_z_awarded)
                VALUES (?, ?, ?, 1, 1, ?)
                ON CONFLICT(channel, trigger_id, date) DO UPDATE SET
                    hit_count = hit_count + 1,
                    total_z_awarded = total_z_awarded + ?
            """, (channel, trigger_id, date, z_awarded, z_awarded))
            # NOTE: unique_users is approximate (always increments by 1).
            # Exact unique counting requires a separate user-tracking table or set.
            # For v1, this is acceptable. Sprint 8 can refine.
            conn.commit()
        finally:
            conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _sync)
```

> **Note on `unique_users`:** Accurately tracking unique users per trigger per day requires either a secondary table or in-memory set. For Sprint 3, `unique_users` is an approximate upper bound (it counts hits, not distinct users). Sprint 8 can refine this with a `trigger_analytics_users` helper table if needed. The column exists now for forward compatibility.

---

## 11. Daily Activity Tracking

### 11.1 Overview

Sprint 1 created the `daily_activity` table. Sprint 3 actively populates it on every chat message. This data feeds Sprint 7's daily competitions.

### 11.2 Fields Updated per Message

On each `evaluate_chat_message()` call, after trigger evaluation:

```python
async def _update_daily_activity(
    self, username: str, channel: str, message: str, timestamp: datetime,
) -> None:
    """Update daily_activity counters for this message."""
    today = timestamp.strftime("%Y-%m-%d")
    
    # Always increment message count
    await self._db.increment_daily_messages_sent(username, channel, today)
    
    # Long message tracking (reuses the min_chars threshold)
    if len(message) >= self._config.chat_triggers.long_message.min_chars:
        await self._db.increment_daily_long_messages(username, channel, today)
    
    # GIF detection (simple URL pattern + common GIF hosts)
    if self._is_gif(message):
        await self._db.increment_daily_gifs_posted(username, channel, today)
    
    # Unique emote tracking
    emotes_in_message = self._extract_emotes(message)
    if emotes_in_message:
        await self._db.add_unique_emotes(username, channel, today, emotes_in_message)
```

### 11.3 GIF Detection

```python
GIF_PATTERN = re.compile(
    r'https?://\S+\.gif(?:\?\S*)?'          # Direct .gif links
    r'|https?://(?:media\.)?giphy\.com/\S+'  # Giphy
    r'|https?://tenor\.com/\S+'              # Tenor
    r'|https?://i\.imgur\.com/\S+\.gif'      # Imgur GIFs
    , re.IGNORECASE
)

def _is_gif(self, message: str) -> bool:
    """Detect if a message contains a GIF link."""
    return bool(GIF_PATTERN.search(message))
```

### 11.4 Emote Extraction

CyTube channels have custom emotes. The emote list is available from kryten-py's KV store helpers (e.g. `await client.kv_get("kryten_{channel}_emotes", "list", default=[], parse_json=True)` or the appropriate robot state helper).

```python
def _extract_emotes(self, message: str) -> set[str]:
    """Extract emote names from the message.
    Uses the channel emote list loaded from the KV store."""
    found = set()
    for emote_name in self._known_emotes:
        if emote_name in message:
            found.add(emote_name)
    return found
```

**Emote list loading:** The `EconomyApp` subscribes to the emote list KV store on startup and updates `earning_engine._known_emotes` (a `set[str]`). If the emote list is unavailable, emote tracking is silently skipped (no error, just zero counts).

### 11.5 Database Methods to Add

```python
async def increment_daily_messages_sent(self, username: str, channel: str, date: str) -> None:
    """Increment messages_sent in daily_activity (upsert)."""

async def increment_daily_long_messages(self, username: str, channel: str, date: str) -> None:
    """Increment long_messages in daily_activity."""

async def increment_daily_gifs_posted(self, username: str, channel: str, date: str) -> None:
    """Increment gifs_posted in daily_activity."""

async def increment_daily_kudos_given(self, username: str, channel: str, date: str) -> None:
    """Increment kudos_given in daily_activity."""

async def increment_daily_kudos_received(self, username: str, channel: str, date: str) -> None:
    """Increment kudos_received in daily_activity."""

async def increment_daily_laughs_received(self, username: str, channel: str, date: str) -> None:
    """Increment laughs_received in daily_activity."""

async def increment_daily_bot_interactions(self, username: str, channel: str, date: str) -> None:
    """Increment bot_interactions in daily_activity."""

async def add_unique_emotes(
    self, username: str, channel: str, date: str, emotes: set[str],
) -> None:
    """Update unique_emotes_used count. Since we don't store individual emote names,
    this maintains a running count. Use an in-memory set per user per day for
    accurate unique counting, and persist just the total count."""
```

### 11.6 Unique Emote Counting Strategy

The `daily_activity.unique_emotes_used` column stores an **integer count** of unique emotes used today. Tracking *which* emotes have been used requires an in-memory set (per user per day) to avoid double-counting:

```python
# In EarningEngine.__init__:
self._emote_sets: dict[tuple[str, str, str], set[str]] = {}  # (user, channel, date) → set

# In _update_daily_activity:
key = (username, channel, today)
if key not in self._emote_sets:
    self._emote_sets[key] = set()
new_emotes = emotes_in_message - self._emote_sets[key]
if new_emotes:
    self._emote_sets[key] |= new_emotes
    await self._db.set_daily_unique_emotes(username, channel, today, len(self._emote_sets[key]))
```

On date change, prune old date entries from `_emote_sets` to avoid unbounded memory growth:

```python
def _prune_emote_sets(self, current_date: str) -> None:
    """Remove emote sets for past dates."""
    expired = [k for k in self._emote_sets if k[2] != current_date]
    for k in expired:
        del self._emote_sets[k]
```

---

## 12. PM Commands: `rewards` and `like`

### 12.1 `rewards` Command

**PM syntax:** `rewards`

**Response:** A list of **non-hidden** earning triggers with their reward amounts. This gives users a partial guide to the economy without revealing hidden mechanics.

**Implementation:** Add to `pm_handler.py`'s command map:

```python
"rewards": self._handle_rewards,
```

```python
async def _handle_rewards(self, username: str, channel: str, args: str) -> str:
    """Show non-hidden earning triggers."""
    lines = [
        f"💰 How to earn {self._currency_name}:",
        "",
        f"📍 Be connected: {self._config.presence.base_rate_per_minute} {self._symbol}/min",
    ]
    
    # Presence milestones (never hidden)
    milestones = self._config.presence.hourly_milestones
    if milestones:
        ms_text = ", ".join(f"{h}h={r}{self._symbol}" for h, r in sorted(milestones.items()))
        lines.append(f"⏰ Dwell milestones: {ms_text}")
    
    # Streaks (never hidden)
    if self._config.streaks.daily.enabled:
        lines.append(f"🔥 Daily streaks: day 2+ earns bonus {self._symbol}")
    
    # Bridge (never hidden)
    if self._config.streaks.weekend_weekday_bridge.enabled:
        lines.append(
            f"🌉 Weekend+weekday bridge: {self._config.streaks.weekend_weekday_bridge.bonus} {self._symbol}/week"
        )
    
    # Rain (never hidden)
    if self._config.rain.enabled:
        lines.append(f"☔ Random rain drops to connected users")
    
    # Non-hidden chat/content/social triggers
    all_triggers = [
        ("chat_triggers", self._config.chat_triggers),
        ("content_triggers", self._config.content_triggers),
        ("social_triggers", self._config.social_triggers),
    ]
    
    for section_name, section in all_triggers:
        for trigger_name, trigger_cfg in self._iter_trigger_configs(section):
            if hasattr(trigger_cfg, 'hidden') and trigger_cfg.hidden:
                continue
            if not trigger_cfg.enabled:
                continue
            reward = self._get_trigger_reward_text(trigger_cfg)
            desc = self._get_trigger_description(trigger_name)
            lines.append(f"  • {desc}: {reward}")
    
    # Note about hidden triggers
    lines.append("")
    lines.append("🔮 Some triggers are hidden. Experiment to find them!")
    
    return "\n".join(lines)
```

**Helper methods:**

```python
def _iter_trigger_configs(self, section) -> list[tuple[str, Any]]:
    """Iterate over trigger config attributes in a section.
    Each section is a Pydantic model with trigger sub-models as fields."""
    for field_name in section.model_fields:
        yield field_name, getattr(section, field_name)

def _get_trigger_description(self, trigger_name: str) -> str:
    """Human-readable description for a trigger name."""
    descriptions = {
        "long_message": "Long messages (30+ chars)",
        "first_message_of_day": "First message of the day",
        "conversation_starter": "Break the silence",
        "laugh_received": "Make someone laugh",
        "kudos_received": "Receive kudos (++)",
        "first_after_media_change": "First to comment on new media",
        "comment_during_media": "Chat during media",
        "like_current": "Like current media (PM: 'like')",
        "survived_full_media": "Watch full media",
        "greeted_newcomer": "Greet newcomers",
        "mentioned_by_other": "Get mentioned",
        "bot_interaction": "Interact with the bot",
    }
    return descriptions.get(trigger_name, trigger_name)

def _get_trigger_reward_text(self, cfg) -> str:
    """Format the reward amount from a trigger config."""
    if hasattr(cfg, 'reward'):
        return f"{cfg.reward} {self._symbol}"
    if hasattr(cfg, 'reward_per_laugher'):
        return f"{cfg.reward_per_laugher} {self._symbol}/laugh"
    if hasattr(cfg, 'reward_per_message'):
        return f"{cfg.reward_per_message} {self._symbol}/msg"
    return f"? {self._symbol}"
```

### 12.2 `like` Command

**PM syntax:** `like`

**Response:** Confirm the like was registered (with Z earned), or error if no media is playing / already liked.

**Implementation:** Add to `pm_handler.py`'s command map:

```python
"like": self._handle_like,
```

```python
async def _handle_like(self, username: str, channel: str, args: str) -> str:
    """Like the currently playing media."""
    result = await self._earning_engine.evaluate_like_current(username, channel)
    
    if result.amount > 0:
        media = self._channel_state.get_current_media(channel)
        title = media.title if media else "current media"
        return f"👍 Liked \"{title}\"! +{result.amount} {self._symbol}"
    
    if result.blocked_by == "cap":
        return "You've already liked this one!"
    if result.blocked_by == "disabled":
        return "Likes are currently disabled."
    
    return "Nothing playing right now."
```

### 12.3 Update `help` Command

Add `rewards` and `like` to the help text:

```
rewards — See ways to earn {currency}
like — Like the current media (earn {reward} {currency})
```

---

## 13. Event Handler Registrations

### 13.1 `chatmsg` Handler (Primary)

Register in `EconomyApp.start()`:

```python
@self.client.on("chatmsg")
async def on_chatmsg(event: ChatMessageEvent) -> None:
    username = event.username
    channel = event.channel
    message = event.message
    timestamp = event.timestamp  # Use authoritative event timestamp
    
    # Ignored user gate (fast reject before any processing)
    if username.lower() in self._ignored_users:
        return
    
    # Bot's own messages — detect bot_interaction
    if username.lower() == self._config.bot.username.lower():
        # The bot just responded. Credit the previous speaker.
        last_human = self._channel_state.get_last_non_self_message_user(
            channel, username
        )
        if last_human and self._config.social_triggers.bot_interaction.enabled:
            await self._earning_engine.evaluate_bot_interaction(
                last_human, channel, timestamp
            )
        return  # Bot messages don't earn and don't trigger other triggers
    
    # Main earning pipeline
    outcome = await self._earning_engine.evaluate_chat_message(
        username, channel, message, timestamp
    )
    
    if outcome.total_earned > 0:
        self._logger.debug(
            "Chat triggers for %s in %s: %d Z from %d triggers",
            username, channel, outcome.total_earned,
            len(outcome.awarded_triggers),
        )
```

### 13.2 `changemedia` Handler

Register in `EconomyApp.start()`:

```python
@self.client.on("changemedia")
async def on_changemedia(event: ChangeMediaEvent) -> None:
    channel = event.channel
    title = event.title
    media_id = event.media_id
    duration = event.duration
    uid = event.uid  # CyTube playlist entry UID
    timestamp = event.timestamp  # Use authoritative event timestamp
    
    # Get currently connected users for "present at start" snapshot
    connected = self._presence_tracker.get_connected_users(channel)
    
    # Process media change — returns previous media info
    previous = self._channel_state.handle_media_change(
        channel, title, media_id, float(duration), connected, timestamp,
    )
    
    # Evaluate survived_full_media for previous media
    if previous is not None:
        rewarded = await self._earning_engine.evaluate_survived_full_media(
            channel, previous, connected, timestamp,
        )
        if rewarded:
            self._logger.info(
                "survived_full_media: %d users rewarded for '%s' in %s",
                len(rewarded), previous.title, channel,
            )
```

### 13.3 Presence Tracker Integration

In `presence_tracker.py`, the existing `handle_user_join()` for genuine arrivals must call:

```python
self._channel_state.record_genuine_join(channel, username, now)
```

This is described in Section 4.4.

---

## 14. Config Sections Activated

These config sections become operational in Sprint 3 (were defined with defaults in Sprint 1 but not consumed):

| Config Path | Sprint 3 Consumer |
|---|---|
| `chat_triggers.long_message` | `EarningEngine._eval_long_message()` |
| `chat_triggers.laugh_received` | `EarningEngine._eval_laugh_received()` |
| `chat_triggers.kudos_received` | `EarningEngine._eval_kudos_received()` |
| `chat_triggers.first_message_of_day` | `EarningEngine._eval_first_message_of_day()` |
| `chat_triggers.conversation_starter` | `EarningEngine._eval_conversation_starter()` |
| `content_triggers.first_after_media_change` | `EarningEngine._eval_first_after_media_change()` |
| `content_triggers.comment_during_media` | `EarningEngine._eval_comment_during_media()` |
| `content_triggers.like_current` | `EarningEngine.evaluate_like_current()` |
| `content_triggers.survived_full_media` | `EarningEngine.evaluate_survived_full_media()` |
| `social_triggers.greeted_newcomer` | `EarningEngine._eval_greeted_newcomer()` |
| `social_triggers.mentioned_by_other` | `EarningEngine._eval_mentioned_by_other()` |
| `social_triggers.bot_interaction` | `EarningEngine.evaluate_bot_interaction()` |

---

## 15. Test Specifications

### 15.1 File: `tests/test_earning_engine.py`

Core pipeline tests:

| Test | Description |
|---|---|
| `test_ignored_user_earns_nothing` | Message from ignored user → empty outcome, no DB writes |
| `test_ignored_user_case_insensitive` | "CyTubeBot" in config matches "cytubebot" message sender |
| `test_multiple_triggers_fire` | Single message triggers long_message + first_message_of_day → total is sum |
| `test_disabled_trigger_skipped` | Trigger with `enabled: false` → not evaluated |
| `test_transactions_logged_per_trigger` | Each awarded trigger creates a separate transaction |
| `test_trigger_analytics_updated` | Successful trigger → analytics table incremented |
| `test_empty_message_no_triggers` | Empty string message → no trigger fires |

### 15.2 File: `tests/test_chat_triggers.py`

| Test | Description |
|---|---|
| `test_long_message_qualifies` | 30-char message → 1 Z |
| `test_short_message_rejected` | 29-char message → 0 Z |
| `test_long_message_hourly_cap` | 31st long message in hour → blocked |
| `test_long_message_cap_resets_after_hour` | After 1 hour, cap resets |
| `test_first_message_of_day_awarded` | First message → 5 Z, flag set |
| `test_first_message_of_day_no_double` | Second message same day → 0 Z |
| `test_first_message_of_day_resets_next_day` | New calendar day → eligible again |
| `test_conversation_starter_after_silence` | 10 min silence → 10 Z |
| `test_conversation_starter_no_silence` | Message 5 min after last → 0 Z |
| `test_conversation_starter_first_ever_message` | No prior messages (None silence) → qualifies |
| `test_conversation_starter_ignored_user_no_silence_reset` | Ignored user's message doesn't update last_message_time |

### 15.3 File: `tests/test_laugh_received.py`

| Test | Description |
|---|---|
| `test_lol_detected_as_laugh` | "lol" → laugh detected |
| `test_haha_detected_as_laugh` | "hahaha" → laugh detected |
| `test_emoji_laugh_detected` | "😂" → laugh detected |
| `test_normal_message_not_laugh` | "hello there" → not detected |
| `test_laugh_credits_joke_teller` | Laugher says "lol" → previous sender gets 2 Z |
| `test_laugh_self_excluded` | User laughs at own message → no credit |
| `test_laugh_max_laughers_cap` | 11th laugher at same joke → blocked |
| `test_laugh_no_previous_sender` | Laugh with no prior message → no credit |
| `test_laugh_ignored_user_no_joke_credit` | If joke-teller is ignored user → no credit |

### 15.4 File: `tests/test_kudos.py`

| Test | Description |
|---|---|
| `test_username_plus_plus_detected` | "alice++" → alice gets 3 Z |
| `test_at_username_plus_plus_detected` | "@alice++" → alice gets 3 Z |
| `test_self_kudos_blocked` | "alice++" sent by alice → 0 Z |
| `test_multiple_kudos_in_one_message` | "alice++ bob++" → both credited |
| `test_duplicate_kudos_same_message` | "alice++ alice++" → only 1 credit |
| `test_kudos_to_ignored_user` | "CyTubeBot++" → no credit |
| `test_kudos_case_insensitive` | "Alice++" matches user "alice" |
| `test_kudos_daily_activity_updated` | Sender's kudos_given incremented, target's kudos_received incremented |

### 15.5 File: `tests/test_content_triggers.py`

| Test | Description |
|---|---|
| `test_first_after_media_change_within_window` | Message within 30s → 3 Z |
| `test_first_after_media_change_too_late` | Message at 31s → 0 Z |
| `test_first_after_media_change_second_user` | Second message within window → 0 Z (already claimed) |
| `test_first_after_media_change_no_media` | No media change recorded → 0 Z |
| `test_comment_during_media_earns` | Message during media → 0.5 Z accumulated |
| `test_comment_during_media_fractional` | Two messages → 1 Z credited |
| `test_comment_during_media_cap` | Exceeding cap → blocked |
| `test_comment_during_media_cap_scales` | 60-min media: cap = 10 × (60/30) = 20 |
| `test_comment_during_media_no_media` | No media playing → 0 Z |
| `test_like_current_earns` | PM "like" with media → 2 Z |
| `test_like_current_double_blocked` | Second "like" same media → 0 Z |
| `test_like_current_no_media` | PM "like" with nothing playing → 0 Z |
| `test_like_current_resets_on_media_change` | New media → can like again |
| `test_survived_full_media_qualifies` | Present at start + still connected + ≥80% played → 5 Z |
| `test_survived_full_media_left_early` | User left before end → 0 Z |
| `test_survived_full_media_joined_late` | User joined after media start → 0 Z |
| `test_survived_full_media_skipped` | Media skipped at 50% → nobody qualifies |
| `test_survived_full_media_zero_duration` | Duration 0 (unknown) → skipped |
| `test_survived_full_media_ignored_user` | Ignored user present throughout → 0 Z |

### 15.6 File: `tests/test_social_triggers.py`

| Test | Description |
|---|---|
| `test_greeted_newcomer_within_window` | Message contains joiner's name within 60s → 3 Z |
| `test_greeted_newcomer_after_window` | Message 61s after join → 0 Z |
| `test_greeted_newcomer_only_first_greeter` | Second person greeting same newcomer → 0 Z |
| `test_greeted_newcomer_self_greet` | Newcomer's own message containing own name → 0 Z |
| `test_greeted_newcomer_bot_join_excluded` | Bot joins don't appear in recent_joins → no greeting reward |
| `test_greeted_newcomer_bounced_join_excluded` | Non-genuine (debounced) join → no greeting reward |
| `test_mentioned_by_other_earns` | "hey alice" when alice is connected → alice gets 1 Z |
| `test_mentioned_self_no_earn` | "hey alice" sent by alice → 0 Z |
| `test_mentioned_by_other_hourly_cap` | 6th mention from same sender → blocked |
| `test_mentioned_multiple_users` | "hey alice and bob" → both credited |
| `test_mentioned_ignored_user` | Mentioning ignored user → 0 Z |
| `test_bot_interaction_earns` | Bot response after user message → user gets 2 Z |
| `test_bot_interaction_daily_cap` | 11th bot interaction → blocked |
| `test_bot_interaction_disabled` | Config disabled → no credit |

### 15.7 File: `tests/test_channel_state.py`

| Test | Description |
|---|---|
| `test_media_change_returns_previous` | `handle_media_change()` returns old `MediaInfo` |
| `test_media_change_resets_counters` | Comment counts, likes, first-claim all reset |
| `test_first_claim_once_per_media` | `try_claim_first_after_media()` returns True once, then False |
| `test_comment_count_increments` | `increment_media_comments()` returns sequential counts |
| `test_like_once_per_media` | `try_like_current()` returns True once per user per media |
| `test_genuine_join_recorded` | `record_genuine_join()` adds to recent_joins |
| `test_ignored_user_join_not_recorded` | Ignored user join → not in recent_joins |
| `test_recent_joiners_pruned` | Old joins (outside window) removed on query |
| `test_silence_tracking` | `get_silence_seconds()` returns correct duration |
| `test_media_comment_cap_scales` | 60-min media with scale=true → scaled cap |
| `test_media_comment_cap_no_scale` | scale=false → base cap |

### 15.8 File: `tests/test_cooldowns.py`

| Test | Description |
|---|---|
| `test_first_hit_allowed` | No prior cooldown → allowed, count = 1 |
| `test_within_cap_allowed` | count < max → allowed, count incremented |
| `test_at_cap_blocked` | count == max → blocked |
| `test_window_expired_resets` | After window_seconds → reset, allowed |
| `test_different_triggers_independent` | Cooldown for trigger A doesn't affect trigger B |
| `test_different_users_independent` | User A's cooldown doesn't affect user B |
| `test_compound_cooldown_key` | `mentioned_by_other.alice.bob` keyed per pair |

### 15.9 File: `tests/test_daily_activity.py`

| Test | Description |
|---|---|
| `test_messages_sent_incremented` | Each message → messages_sent += 1 |
| `test_long_messages_counted` | 30+ char message → long_messages += 1 |
| `test_gif_detected` | Message with .gif URL → gifs_posted += 1 |
| `test_non_gif_url_ignored` | Regular URL → gifs_posted unchanged |
| `test_giphy_detected` | giphy.com link → gifs_posted += 1 |
| `test_tenor_detected` | tenor.com link → gifs_posted += 1 |
| `test_unique_emotes_counted` | 3 different emotes → unique_emotes_used = 3 |
| `test_duplicate_emote_not_double_counted` | Same emote twice → unique_emotes_used = 1 |
| `test_emote_set_resets_on_new_day` | New date → emote tracking starts fresh |

### 15.10 File: `tests/test_fractional.py`

| Test | Description |
|---|---|
| `test_half_z_no_credit` | 0.5 → accumulator = 0.5, credit = 0 |
| `test_two_halves_credit_one` | 0.5 + 0.5 → credit = 1, accumulator = 0 |
| `test_three_thirds_credit_one` | 0.33 + 0.33 + 0.34 → credit = 1 |
| `test_different_triggers_independent` | Accumulators for different trigger_ids are separate |
| `test_different_users_independent` | Accumulators for different usernames are separate |
| `test_whole_number_credits_immediately` | 1.0 → credit = 1 immediately |

### 15.11 File: `tests/conftest.py` Additions

Add shared fixtures for Sprint 3:

```python
@pytest.fixture
def channel_state(config):
    """ChannelStateTracker with test config."""
    return ChannelStateTracker(config, logging.getLogger("test"))

@pytest.fixture
def earning_engine(config, test_db, channel_state):
    """EarningEngine with test dependencies."""
    return EarningEngine(config, test_db, channel_state, logging.getLogger("test"))

@pytest.fixture
def sample_media_info():
    """A MediaInfo for a 30-minute video."""
    return MediaInfo(
        title="Test Video",
        media_id="test123",
        duration_seconds=1800,
        started_at=datetime(2026, 3, 1, 12, 0, 0),
        users_present_at_start={"alice", "bob"},
    )
```

---

## 16. Acceptance Criteria

### Earning Engine Core

- [ ] `earning_engine.py` and `channel_state.py` created as new modules
- [ ] `trigger_analytics` table created on startup (idempotent)
- [ ] Ignored users produce empty `EarningOutcome` with no DB writes
- [ ] Each trigger independently toggleable via `enabled` config flag
- [ ] All earning triggers log transactions with correct `type`, `trigger_id`, and `reason`
- [ ] Trigger analytics recorded for every successful hit

### Chat Triggers

- [ ] `long_message`: awards for messages ≥ min_chars, capped per hour
- [ ] `first_message_of_day`: one-time per calendar day per user, flag persisted in DB
- [ ] `conversation_starter`: awards after configured silence gap; ignored users' messages don't reset silence timer
- [ ] `laugh_received`: detects laugh patterns, credits the joke-teller (not laugher), self-excluded
- [ ] `kudos_received`: detects `username++` pattern, credits target, self-excluded, multi-kudos per message
- [ ] `first_after_media_change`: first message within window claims the slot

### Content Engagement

- [ ] `comment_during_media`: fractional earning (0.5 Z) accumulates correctly; per-media cap enforced; cap scales with duration when enabled
- [ ] `like_current`: PM command awards once per media item; resets on media change
- [ ] `survived_full_media`: evaluated on `changemedia`; rewards users present at start + still connected + ≥80% duration played

### Social Triggers

- [ ] `greeted_newcomer`: detects newcomer's name in message within window; only genuine (debounced) joins qualify; bot joins excluded; first greeter only
- [ ] `mentioned_by_other`: credits mentioned user; per-pair hourly cap; self-mention excluded
- [ ] `bot_interaction`: credits user who triggered bot response; daily cap; detected via bot's `chatmsg` events

### Cooldowns & Caps

- [ ] Cooldown system tracks per-user, per-trigger count within rolling window
- [ ] Expired windows reset automatically
- [ ] Compound cooldown keys (e.g. mention pairs) work correctly

### Daily Activity

- [ ] `messages_sent`, `long_messages`, `gifs_posted`, `kudos_given`, `kudos_received`, `laughs_received`, `bot_interactions` updated per message
- [ ] GIF detection recognizes `.gif` URLs, Giphy, Tenor, Imgur
- [ ] Unique emote count tracked accurately (no double-counting within a day)

### PM Commands

- [ ] `rewards` returns non-hidden triggers with reward amounts
- [ ] `rewards` includes the hidden-trigger teaser line
- [ ] `like` awards Z and confirms the media title
- [ ] `like` rejects duplicate likes and missing media with appropriate messages
- [ ] `help` updated to include `rewards` and `like`

### Fractional Accumulator

- [ ] Sub-integer earnings accumulate and credit whole Z when threshold crossed
- [ ] Accumulators are independent per (user, channel, trigger)
- [ ] Maximum loss on restart is < 1 Z per user per trigger

### Event Handlers

- [ ] `chatmsg` handler registered and routes through earning engine
- [ ] `changemedia` handler registered and triggers `survived_full_media` evaluation + state reset
- [ ] Bot's own `chatmsg` events trigger `bot_interaction` for the previous human speaker

### Tests

- [ ] All new test files pass (`pytest` exits 0)
- [ ] At least 70 test cases across the 10 test files

---

*End of Sprint 3 specification. This document is self-contained and sufficient for an AI coding agent to implement the full sprint given a completed Sprint 1 + Sprint 2 codebase.*
