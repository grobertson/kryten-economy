# Sprint 6 — Achievements, Named Ranks & CyTube Promotion

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 6 of 9  
> **Goal:** Persistent progression system — one-time achievement badges, named B-movie ranks with real perks, CyTube level 2 vanity promotion, and user-facing commands (`rank`, `profile`, `achievements`, `top`/`leaderboard`).  
> **Depends on:** Sprint 1 (Core Foundation), Sprint 4 (Gambling stats), Sprint 5 (Spending — for tip/spend milestones)  
> **Enables:** Sprint 7 (Events reference rank perks), Sprint 8 (Admin tools for rank/achievement management)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Config Activation](#3-config-activation)
4. [Achievement Engine](#4-achievement-engine)
5. [Achievement Conditions](#5-achievement-conditions)
6. [Rank Engine](#6-rank-engine)
7. [CyTube Level 2 Promotion](#7-cytube-level-2-promotion)
8. [Rank Perk Enforcement](#8-rank-perk-enforcement)
9. [PM Commands: rank](#9-pm-commands-rank)
10. [PM Commands: profile](#10-pm-commands-profile)
11. [PM Commands: achievements](#11-pm-commands-achievements)
12. [PM Commands: top / leaderboard](#12-pm-commands-top--leaderboard)
13. [PM Command Registrations](#13-pm-command-registrations)
14. [Public Announcements](#14-public-announcements)
15. [Request-Reply Command Extensions](#15-request-reply-command-extensions)
16. [Metrics Extensions](#16-metrics-extensions)
17. [Integration Points](#17-integration-points)
18. [Test Specifications](#18-test-specifications)
19. [Acceptance Criteria](#19-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint:

- **Achievement engine** evaluates configurable conditions on relevant events and awards one-time badges with Z rewards
- Achievement conditions include: `lifetime_messages`, `lifetime_presence_hours`, `daily_streak`, `unique_tip_recipients`, `unique_tip_senders`, `lifetime_earned`, `lifetime_spent`, `lifetime_gambled`, `gambling_biggest_win`, `rank_reached`, `unique_emotes_used_lifetime`
- **Rank engine** checks lifetime earnings against configurable B-movie-themed tier thresholds and promotes users with PM notification + public announcement
- Ranks provide **real perks**: spend discounts, extra queue slots, rain bonus multiplier
- **CyTube level 2 promotion** — via top rank auto-promotion or purchasable at 50,000 Z (min rank gated) — uses `client.safe_set_channel_rank()` (kryten-py wrapper)
- `rank` command shows current rank, progress bar, and active perks
- `profile` command shows comprehensive user view
- `achievements` command lists earned badges and progress toward next
- `top` / `leaderboard` command shows daily leaders, richest users, and rank distribution
- Public announcements for rank-ups and major achievements
- All CyTube interaction via kryten-py wrappers — no direct NATS access

> **⚠️ Ecosystem rule:** All NATS interaction goes through kryten-py's `KrytenClient`. Use `client.send_pm()`, `client.send_chat()`, `client.safe_set_channel_rank()` — never raw NATS subjects.

---

## 2. New Database Tables

### 2.1 `achievements` Table

```sql
CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    achievement_id TEXT NOT NULL,    -- From config, e.g. "messages_100"
    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(username, channel, achievement_id)
);

CREATE INDEX IF NOT EXISTS idx_achievements_user ON achievements(username, channel);
CREATE INDEX IF NOT EXISTS idx_achievements_id ON achievements(achievement_id, channel);
```

### 2.2 Database Methods to Add

```python
# ── Achievement Methods ──────────────────────────────────────

async def has_achievement(self, username: str, channel: str, achievement_id: str) -> bool:
    """Check if a user already has a specific achievement."""

async def award_achievement(self, username: str, channel: str, achievement_id: str) -> bool:
    """Award an achievement. Returns True if newly awarded, False if already held."""

async def get_user_achievements(self, username: str, channel: str) -> list[dict]:
    """List all achievements for a user. Returns [{achievement_id, awarded_at}, ...]"""

async def get_achievement_count(self, username: str, channel: str) -> int:
    """Count achievements earned by a user."""

# ── Rank/Progression Queries ─────────────────────────────────

async def get_lifetime_earned(self, username: str, channel: str) -> int:
    """Get lifetime_earned from accounts table."""

async def get_lifetime_presence_hours(self, username: str, channel: str) -> float:
    """Calculate cumulative presence hours from session data."""

async def get_unique_tip_recipients(self, username: str, channel: str) -> int:
    """Count distinct receivers in tip_history for this sender."""

async def get_unique_tip_senders(self, username: str, channel: str) -> int:
    """Count distinct senders in tip_history for this receiver."""

async def get_lifetime_gambled(self, username: str, channel: str) -> int:
    """Sum of all wagers from gambling_stats."""

async def get_biggest_gambling_win(self, username: str, channel: str) -> int:
    """Max single win from gambling_stats."""

# ── Leaderboard Queries ──────────────────────────────────────

async def get_top_earners_today(self, channel: str, limit: int = 10) -> list[dict]:
    """Top Z earned today (excluding tip receipts). Returns [{username, earned_today}, ...]"""

async def get_richest_users(self, channel: str, limit: int = 10) -> list[dict]:
    """Highest current balances. Returns [{username, balance, rank_name}, ...]"""

async def get_highest_lifetime(self, channel: str, limit: int = 10) -> list[dict]:
    """Highest lifetime earned. Returns [{username, lifetime_earned, rank_name}, ...]"""

async def get_rank_distribution(self, channel: str) -> dict[str, int]:
    """Count users at each rank tier. Returns {rank_name: count}."""
```

---

## 3. Config Activation

This sprint activates these config sections from `kryten-economy-plan.md`:

- `achievements` — list of achievement definitions with conditions, rewards, hidden flags
- `ranks.*` — tier definitions with perks, `earn_multiplier_per_rank`, `spend_discount_per_rank`
- `cytube_promotion.*` — enabled, purchasable, cost, min_rank
- `announcements.rank_promotion`, `announcements.achievement_milestone`

### 3.1 Pydantic Config Models

```python
from pydantic import BaseModel, Field
from typing import Any


class AchievementCondition(BaseModel):
    type: str                          # "lifetime_messages", "daily_streak", etc.
    threshold: int | float = 0         # Numeric threshold for the condition
    field: str = ""                    # For daily_threshold/daily_top conditions

class AchievementConfig(BaseModel):
    id: str
    description: str = ""
    condition: AchievementCondition
    reward: int = 0                    # Z reward on achievement
    hidden: bool = True                # Hidden from achievements list until earned
    announce_public: bool = False      # Announce in public chat on award

class RankTier(BaseModel):
    name: str
    min_lifetime_earned: int
    perks: list[str] = []
    cytube_level_promotion: int | None = None  # Auto-promote to this CyTube level

class RanksConfig(BaseModel):
    earn_multiplier_per_rank: float = 0.0
    spend_discount_per_rank: float = 0.02
    tiers: list[RankTier] = []         # Ordered by min_lifetime_earned ascending

class CytubePromotionConfig(BaseModel):
    enabled: bool = True
    purchasable: bool = True
    cost: int = 50000
    min_rank: str = "Associate Producer"  # Minimum economy rank to purchase
```

### 3.2 EconomyConfig Extensions

```python
class EconomyConfig(KrytenConfig):
    # ... existing Sprint 1-5 fields ...
    achievements: list[AchievementConfig] = []
    ranks: RanksConfig = RanksConfig()
    cytube_promotion: CytubePromotionConfig = CytubePromotionConfig()
```

---

## 4. Achievement Engine

### 4.1 File: `kryten_economy/achievement_engine.py`

### 4.2 Class: `AchievementEngine`

```python
import logging
from datetime import datetime


class AchievementEngine:
    """Evaluates achievement conditions and awards one-time badges."""

    def __init__(self, config, database, client, logger: logging.Logger):
        self._config = config
        self._db = database
        self._client = client
        self._logger = logger
        # Pre-index achievements by condition type for efficient lookup
        self._by_condition_type: dict[str, list[AchievementConfig]] = {}
        for ach in config.achievements:
            ctype = ach.condition.type
            self._by_condition_type.setdefault(ctype, []).append(ach)

    async def check_achievements(
        self, username: str, channel: str, relevant_types: list[str] | None = None,
    ) -> list[AchievementConfig]:
        """Check all achievements (or those matching relevant_types) for a user.
        
        Returns list of newly awarded achievements.
        """
        awarded = []
        types_to_check = relevant_types or list(self._by_condition_type.keys())
        
        for ctype in types_to_check:
            for ach in self._by_condition_type.get(ctype, []):
                # Skip if already earned
                if await self._db.has_achievement(username, channel, ach.id):
                    continue
                
                # Evaluate condition
                if await self._evaluate_condition(username, channel, ach.condition):
                    # Award it
                    newly = await self._db.award_achievement(username, channel, ach.id)
                    if newly:
                        # Credit reward
                        if ach.reward > 0:
                            await self._db.credit(
                                username, channel, ach.reward,
                                tx_type="achievement",
                                trigger_id=f"achievement.{ach.id}",
                                reason=f"Achievement: {ach.description}",
                            )
                        awarded.append(ach)
                        self._logger.info(
                            "Achievement awarded: %s → %s (+%d Z) in %s",
                            username, ach.id, ach.reward, channel,
                        )
        
        # Notify for each awarded achievement
        for ach in awarded:
            await self._notify_achievement(username, channel, ach)
        
        return awarded

    async def _notify_achievement(
        self, username: str, channel: str, achievement: AchievementConfig,
    ) -> None:
        """Send PM and optional public announcement for an achievement."""
        # PM to user
        symbol = self._config.currency.symbol
        await self._client.send_pm(
            channel, username,
            f"🏆 Achievement Unlocked: {achievement.description}! +{achievement.reward:,} {symbol}"
        )
        
        # Public announcement if configured
        if (achievement.announce_public
                or self._config.announcements.achievement_milestone):
            template = self._config.announcements.templates.get(
                "achievement",
                "🏆 {user} unlocked: {achievement}!"
            )
            msg = template.format(user=username, achievement=achievement.description)
            await self._client.send_chat(channel, msg)
```

### 4.3 Condition Evaluation Dispatch

```python
    async def _evaluate_condition(
        self, username: str, channel: str, condition: AchievementCondition,
    ) -> bool:
        """Evaluate a single achievement condition."""
        evaluator = self._CONDITION_MAP.get(condition.type)
        if not evaluator:
            self._logger.warning("Unknown achievement condition type: %s", condition.type)
            return False
        return await evaluator(username, channel, condition)
    
    _CONDITION_MAP = {
        "lifetime_messages": "_eval_lifetime_messages",
        "lifetime_presence_hours": "_eval_lifetime_presence_hours",
        "daily_streak": "_eval_daily_streak",
        "unique_tip_recipients": "_eval_unique_tip_recipients",
        "unique_tip_senders": "_eval_unique_tip_senders",
        "lifetime_earned": "_eval_lifetime_earned",
        "lifetime_spent": "_eval_lifetime_spent",
        "lifetime_gambled": "_eval_lifetime_gambled",
        "gambling_biggest_win": "_eval_gambling_biggest_win",
        "rank_reached": "_eval_rank_reached",
        "unique_emotes_used_lifetime": "_eval_unique_emotes",
    }
```

> **IMPORTANT:** `_CONDITION_MAP` values are **string method names**, not callable references. The dispatch must use `getattr(self, evaluator)` to resolve the string to a bound method: `return await getattr(self, evaluator)(username, channel, condition)`. Do NOT call `evaluator(self, ...)` directly — that attempts to call a `str` as a function and raises `TypeError`.

---

## 5. Achievement Conditions

### 5.1 Condition Evaluators

Each evaluator queries the database and compares against the condition's `threshold`:

```python
async def _eval_lifetime_messages(self, username, channel, condition) -> bool:
    account = await self._db.get_account(username, channel)
    if not account:
        return False
    return account.get("lifetime_messages", 0) >= condition.threshold

async def _eval_lifetime_presence_hours(self, username, channel, condition) -> bool:
    hours = await self._db.get_lifetime_presence_hours(username, channel)
    return hours >= condition.threshold

async def _eval_daily_streak(self, username, channel, condition) -> bool:
    account = await self._db.get_account(username, channel)
    if not account:
        return False
    return account.get("current_streak", 0) >= condition.threshold

async def _eval_unique_tip_recipients(self, username, channel, condition) -> bool:
    count = await self._db.get_unique_tip_recipients(username, channel)
    return count >= condition.threshold

async def _eval_unique_tip_senders(self, username, channel, condition) -> bool:
    count = await self._db.get_unique_tip_senders(username, channel)
    return count >= condition.threshold

async def _eval_lifetime_earned(self, username, channel, condition) -> bool:
    earned = await self._db.get_lifetime_earned(username, channel)
    return earned >= condition.threshold

async def _eval_lifetime_spent(self, username, channel, condition) -> bool:
    account = await self._db.get_account(username, channel)
    if not account:
        return False
    return account.get("lifetime_spent", 0) >= condition.threshold

async def _eval_lifetime_gambled(self, username, channel, condition) -> bool:
    total = await self._db.get_lifetime_gambled(username, channel)
    return total >= condition.threshold

async def _eval_gambling_biggest_win(self, username, channel, condition) -> bool:
    biggest = await self._db.get_biggest_gambling_win(username, channel)
    return biggest >= condition.threshold

async def _eval_rank_reached(self, username, channel, condition) -> bool:
    """Check if user has reached a specific named rank."""
    account = await self._db.get_account(username, channel)
    if not account:
        return False
    # Compare rank tier index against threshold (tier index, not name)
    current_tier = self._get_rank_tier_index(account)
    return current_tier >= condition.threshold

async def _eval_unique_emotes(self, username, channel, condition) -> bool:
    account = await self._db.get_account(username, channel)
    if not account:
        return False
    return account.get("unique_emotes_used", 0) >= condition.threshold
```

### 5.2 Trigger Points

Achievement checks are invoked at strategic points — not on every single event. The engine is called with `relevant_types` to narrow the check:

| Event / Action | Relevant Achievement Types |
|---|---|
| Chat message processed (Sprint 3) | `lifetime_messages`, `unique_emotes_used_lifetime` |
| Presence tick (Sprint 1) | `lifetime_presence_hours` |
| Daily streak updated (Sprint 2) | `daily_streak` |
| Tip sent (Sprint 5) | `unique_tip_recipients` |
| Tip received (Sprint 5) | `unique_tip_senders` |
| Any earn event | `lifetime_earned`, `rank_reached` |
| Any spend event (Sprint 5) | `lifetime_spent` |
| Gambling outcome (Sprint 4) | `lifetime_gambled`, `gambling_biggest_win` |

**Integration example** (in the earn/credit path):

```python
# After crediting Z in any earn handler:
await self._achievement_engine.check_achievements(
    username, channel,
    relevant_types=["lifetime_earned", "rank_reached"],
)
```

---

## 6. Rank Engine

### 6.1 File: `kryten_economy/rank_engine.py`

### 6.2 Class: `RankEngine`

```python
class RankEngine:
    """Manages named rank progression based on lifetime earnings."""

    def __init__(self, config, database, client, logger):
        self._config = config
        self._db = database
        self._client = client
        self._logger = logger
        # Pre-sort tiers by min_lifetime_earned ascending
        self._tiers = sorted(
            config.ranks.tiers,
            key=lambda t: t.min_lifetime_earned,
        )

    def get_rank_for_lifetime(self, lifetime_earned: int) -> tuple[int, RankTier]:
        """Determine rank tier for a given lifetime earned amount.
        
        Returns (tier_index, RankTier).
        """
        tier_index = 0
        for i, tier in enumerate(self._tiers):
            if lifetime_earned >= tier.min_lifetime_earned:
                tier_index = i
        return tier_index, self._tiers[tier_index]

    def get_next_tier(self, current_index: int) -> RankTier | None:
        """Get the next rank tier, or None if at max."""
        if current_index + 1 < len(self._tiers):
            return self._tiers[current_index + 1]
        return None

    async def check_rank_promotion(self, username: str, channel: str) -> RankTier | None:
        """Check if a user should be promoted. Returns new tier or None.
        
        Call this after any earn event.
        """
        account = await self._db.get_account(username, channel)
        if not account:
            return None
        
        lifetime = account.get("lifetime_earned", 0)
        current_rank = account.get("rank_name", "")
        
        new_index, new_tier = self.get_rank_for_lifetime(lifetime)
        
        if new_tier.name != current_rank:
            # Promotion!
            await self._db.update_account_rank(username, channel, new_tier.name)
            await self._notify_rank_promotion(username, channel, new_tier)
            
            # Auto CyTube level promotion if configured
            if new_tier.cytube_level_promotion is not None:
                await self._promote_cytube_level(
                    username, channel, new_tier.cytube_level_promotion,
                )
            
            return new_tier
        
        return None

    async def _notify_rank_promotion(
        self, username: str, channel: str, tier: RankTier,
    ) -> None:
        """PM user and optionally announce publicly."""
        perks_str = ", ".join(tier.perks) if tier.perks else "No additional perks"
        await self._client.send_pm(
            channel, username,
            f"⭐ Rank Up! You are now a **{tier.name}**!\n"
            f"Perks: {perks_str}"
        )
        
        if self._config.announcements.rank_promotion:
            template = self._config.announcements.templates.get(
                "rank_up",
                "⭐ {user} is now a {rank}!"
            )
            msg = template.format(user=username, rank=tier.name)
            await self._client.send_chat(channel, msg)

    async def _promote_cytube_level(
        self, username: str, channel: str, level: int,
    ) -> None:
        """Promote user to a CyTube level via kryten-py wrapper."""
        try:
            result = await self._client.safe_set_channel_rank(channel, username, level)
            if result.get("success"):
                self._logger.info(
                    "CyTube level %d granted to %s in %s", level, username, channel,
                )
                await self._client.send_pm(
                    channel, username,
                    f"🎬 You've been promoted to CyTube Level {level}! "
                    f"Look at that shiny name in the user list!"
                )
            else:
                self._logger.warning(
                    "CyTube rank change failed for %s: %s",
                    username, result.get("error"),
                )
        except Exception as e:
            self._logger.error("CyTube rank promotion error: %s", e)
```

### 6.3 Database Method for Rank Update

```python
async def update_account_rank(self, username: str, channel: str, rank_name: str) -> None:
    """Update the rank_name field on an account."""
    def _sync():
        conn = self._get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET rank_name = ? WHERE username = ? AND channel = ?",
                (rank_name, username, channel),
            )
            conn.commit()
        finally:
            conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _sync)
```

---

## 7. CyTube Level 2 Promotion

### 7.1 Purchase Path

Users can purchase CyTube level 2 at any time via `buy cytube2` (or `buy level2`), subject to:
- `cytube_promotion.enabled` is true
- `cytube_promotion.purchasable` is true
- User's economy rank meets `cytube_promotion.min_rank`
- User has sufficient balance (default 50,000 Z)

```python
async def _cmd_buy_cytube2(self, username: str, channel: str) -> str:
    """Purchase CyTube level 2 promotion."""
    cfg = self._config.cytube_promotion
    if not cfg.enabled or not cfg.purchasable:
        return "CyTube level 2 promotion is not available."
    
    account = await self._db.get_or_create_account(username, channel)
    current_rank = account.get("rank_name", "Extra")
    
    # Check minimum rank
    min_tier_index = self._get_tier_index_by_name(cfg.min_rank)
    current_tier_index = self._get_rank_tier_index(account)
    if current_tier_index < min_tier_index:
        return (
            f"You need to be at least {cfg.min_rank} rank to purchase CyTube level 2. "
            f"You're currently {current_rank}."
        )
    
    # Apply rank discount
    final_cost, discount = self._spending.apply_discount(cfg.cost, current_tier_index)
    
    validation = await self._spending.validate_spend(username, channel, final_cost, "cytube_promotion")
    if validation:
        return validation.message
    
    success = await self._db.atomic_debit(
        username, channel, final_cost,
        tx_type="spend", trigger_id="spend.cytube_promotion",
        reason="CyTube Level 2 promotion",
    )
    if not success:
        return "Insufficient funds."
    
    # Execute CyTube rank change via kryten-py
    result = await self._client.safe_set_channel_rank(channel, username, 2)
    
    if result.get("success"):
        new_balance = (await self._db.get_account(username, channel))["balance"]
        return (
            f"🎬 Congratulations! You're now CyTube Level 2!\n"
            f"Charged: {final_cost:,} Z · Balance: {new_balance:,} Z"
        )
    else:
        # Refund on failure
        await self._db.credit(
            username, channel, final_cost,
            tx_type="refund", trigger_id="refund.cytube_promotion_failed",
            reason="CyTube promotion failed — refund",
        )
        return (
            f"❌ CyTube rank change failed: {result.get('error', 'unknown error')}. "
            f"Your {final_cost:,} Z have been refunded."
        )
```

### 7.2 Auto-Promotion Path

When a user reaches a rank tier that has `cytube_level_promotion` set (e.g., Studio Mogul → level 2), the `RankEngine._promote_cytube_level()` fires automatically. No charge — it's a rank perk.

---

## 8. Rank Perk Enforcement

### 8.1 Perk Application Points

| Perk | Where Applied | Mechanism |
|---|---|---|
| Spend discount | `SpendingEngine.apply_discount()` | `spend_discount_per_rank × tier_index` |
| Extra queue slots | Queue daily limit check | Base `max_queues_per_day` + perk bonus |
| Rain bonus multiplier | Rain distribution (Sprint 2 scheduler) | Multiply rain drop by `1 + rain_bonus_percent` |
| Earn multiplier | Presence tick / earning engine | `earn_multiplier_per_rank × tier_index` (default 0) |

### 8.2 Extra Queue Slots Implementation

Parse the perks list for queue bonuses:

```python
def _get_max_queues_for_user(self, account: dict) -> int:
    """Calculate max queues per day including rank perk bonuses."""
    base = self._config.spending.max_queues_per_day
    tier_index = self._get_rank_tier_index(account)
    tier = self._config.ranks.tiers[tier_index] if tier_index < len(self._config.ranks.tiers) else None
    if tier:
        for perk in tier.perks:
            # Parse "+1 queue/day" or "+2 queues/day" style perks
            if "queue" in perk.lower():
                import re
                match = re.search(r'\+(\d+)', perk)
                if match:
                    base += int(match.group(1))
    return base
```

### 8.3 Rain Bonus Implementation

In the rain distribution logic (Sprint 2 `_execute_rain`):

```python
def _get_rain_multiplier(self, account: dict) -> float:
    """Get rain bonus multiplier for a user based on rank perks."""
    tier_index = self._get_rank_tier_index(account)
    tier = self._config.ranks.tiers[tier_index] if tier_index < len(self._config.ranks.tiers) else None
    if tier:
        for perk in tier.perks:
            if "rain" in perk.lower():
                import re
                match = re.search(r'\+(\d+)%', perk)
                if match:
                    return 1 + int(match.group(1)) / 100
    return 1.0
```

---

## 9. PM Commands: `rank`

### 9.1 Response Format

```python
async def _cmd_rank(self, username: str, channel: str, args: list[str]) -> str:
    """Show current rank, progress, and active perks."""
    account = await self._db.get_or_create_account(username, channel)
    lifetime = account.get("lifetime_earned", 0)
    
    tier_index, current_tier = self._rank_engine.get_rank_for_lifetime(lifetime)
    next_tier = self._rank_engine.get_next_tier(tier_index)
    
    lines = [
        f"⭐ Rank: {current_tier.name}",
        f"💰 Lifetime earned: {lifetime:,} Z",
    ]
    
    if next_tier:
        remaining = next_tier.min_lifetime_earned - lifetime
        progress = lifetime / next_tier.min_lifetime_earned * 100
        bar = self._progress_bar(progress)
        lines.append(f"Next: {next_tier.name} ({remaining:,} Z to go)")
        lines.append(f"  {bar} {progress:.1f}%")
    else:
        lines.append("🏆 Maximum rank achieved!")
    
    if current_tier.perks:
        lines.append("")
        lines.append("Active perks:")
        for perk in current_tier.perks:
            lines.append(f"  ✓ {perk}")
    
    discount = self._spending.get_rank_discount(tier_index)
    if discount > 0:
        lines.append(f"  ✓ {int(discount * 100)}% spend discount")
    
    return "\n".join(lines)

def _progress_bar(self, percent: float, width: int = 20) -> str:
    """Generate a text-based progress bar."""
    filled = int(width * min(percent, 100) / 100)
    return "█" * filled + "░" * (width - filled)
```

---

## 10. PM Commands: `profile`

### 10.1 Response Format

Comprehensive user view combining data from all sprints:

```python
async def _cmd_profile(self, username: str, channel: str, args: list[str]) -> str:
    """Full user profile view."""
    # Allow looking up another user: profile @alice
    target = args[0].lstrip("@") if args else username
    
    account = await self._db.get_account(target, channel)
    if not account:
        return f"No account found for '{target}'."
    
    personal_name = await self._db.get_vanity_item(target, channel, "personal_currency_name")
    currency = personal_name or self._config.currency.name
    symbol = self._config.currency.symbol
    
    tier_index, tier = self._rank_engine.get_rank_for_lifetime(account.get("lifetime_earned", 0))
    
    lines = [
        f"👤 {target}'s Profile",
        "━" * 30,
        f"⭐ Rank: {tier.name}",
        f"💰 Balance: {account['balance']:,} {symbol} ({currency})",
        f"📈 Lifetime earned: {account.get('lifetime_earned', 0):,} {symbol}",
        f"🔥 Streak: {account.get('current_streak', 0)} days",
    ]
    
    # Achievements
    achievements = await self._db.get_user_achievements(target, channel)
    if achievements:
        lines.append(f"🏆 Achievements: {len(achievements)} earned")
    
    # Vanity items
    vanity = await self._db.get_all_vanity_items(target, channel)
    if vanity:
        vanity_list = ", ".join(vanity.keys())
        lines.append(f"✨ Vanity: {vanity_list}")
    
    # Gambling stats (if user has gambled)
    gambling_stats = await self._db.get_gambling_summary(target, channel)
    if gambling_stats and gambling_stats.get("total_games", 0) > 0:
        lines.append(
            f"🎰 Gambling: {gambling_stats['total_games']} games, "
            f"net {gambling_stats['net_profit']:+,} {symbol}"
        )
    
    return "\n".join(lines)
```

---

## 11. PM Commands: `achievements`

### 11.1 Response Format

```python
async def _cmd_achievements(self, username: str, channel: str, args: list[str]) -> str:
    """List earned achievements and progress toward next."""
    earned = await self._db.get_user_achievements(username, channel)
    earned_ids = {a["achievement_id"] for a in earned}
    
    lines = ["🏆 Achievements"]
    
    # Show earned achievements
    if earned:
        lines.append("━" * 30)
        lines.append("Earned:")
        for a in earned:
            ach_config = self._find_achievement_config(a["achievement_id"])
            desc = ach_config.description if ach_config else a["achievement_id"]
            ts = a["awarded_at"]
            if isinstance(ts, str):
                ts = ts[:10]
            lines.append(f"  ✅ {desc} ({ts})")
    
    # Show progress toward unearned non-hidden achievements
    progress_lines = []
    for ach in self._config.achievements:
        if ach.id in earned_ids:
            continue
        if ach.hidden:
            continue  # Don't reveal hidden achievements
        # Get current progress
        current = await self._get_condition_progress(username, channel, ach.condition)
        if current is not None:
            pct = min(100, current / ach.condition.threshold * 100) if ach.condition.threshold > 0 else 0
            bar = self._progress_bar(pct, width=10)
            progress_lines.append(
                f"  {bar} {ach.description} ({current}/{ach.condition.threshold})"
            )
    
    if progress_lines:
        lines.append("")
        lines.append("In progress:")
        lines.extend(progress_lines)
    
    # Hint about hidden achievements
    hidden_count = sum(
        1 for a in self._config.achievements
        if a.hidden and a.id not in earned_ids
    )
    if hidden_count > 0:
        lines.append(f"\n🔒 {hidden_count} hidden achievement(s) remaining...")
    
    return "\n".join(lines)

def _find_achievement_config(self, achievement_id: str) -> AchievementConfig | None:
    """Look up achievement config by ID."""
    for ach in self._config.achievements:
        if ach.id == achievement_id:
            return ach
    return None

async def _get_condition_progress(self, username, channel, condition) -> int | None:
    """Get current progress value for a condition. Returns None if unknown type."""
    match condition.type:
        case "lifetime_messages":
            acc = await self._db.get_account(username, channel)
            return acc.get("lifetime_messages", 0) if acc else 0
        case "daily_streak":
            acc = await self._db.get_account(username, channel)
            return acc.get("current_streak", 0) if acc else 0
        case "lifetime_earned":
            return await self._db.get_lifetime_earned(username, channel)
        case "unique_tip_recipients":
            return await self._db.get_unique_tip_recipients(username, channel)
        case "unique_tip_senders":
            return await self._db.get_unique_tip_senders(username, channel)
        case _:
            return None
```

---

## 12. PM Commands: `top` / `leaderboard`

### 12.1 Response Format

```python
async def _cmd_top(self, username: str, channel: str, args: list[str]) -> str:
    """Show leaderboards."""
    # Sub-commands: "top earners", "top rich", "top lifetime", "top ranks"
    subcmd = args[0].lower() if args else "earners"
    
    match subcmd:
        case "earners" | "today":
            return await self._top_earners_today(channel)
        case "rich" | "balance" | "balances":
            return await self._top_richest(channel)
        case "lifetime" | "all":
            return await self._top_lifetime(channel)
        case "ranks":
            return await self._rank_distribution(channel)
        case _:
            return (
                "Usage: top <category>\n"
                "Categories: earners, rich, lifetime, ranks"
            )

async def _top_earners_today(self, channel: str) -> str:
    earners = await self._db.get_top_earners_today(channel, limit=10)
    if not earners:
        return "No earnings recorded today."
    lines = ["🏆 Today's Top Earners"]
    for i, e in enumerate(earners, 1):
        medal = "🥇🥈🥉"[i-1] if i <= 3 else f"{i}."
        lines.append(f"  {medal} {e['username']} — {e['earned_today']:,} Z")
    return "\n".join(lines)

async def _top_richest(self, channel: str) -> str:
    rich = await self._db.get_richest_users(channel, limit=10)
    if not rich:
        return "No accounts yet."
    lines = ["💰 Richest Users"]
    for i, r in enumerate(rich, 1):
        medal = "🥇🥈🥉"[i-1] if i <= 3 else f"{i}."
        lines.append(f"  {medal} {r['username']} — {r['balance']:,} Z ({r['rank_name']})")
    return "\n".join(lines)

async def _top_lifetime(self, channel: str) -> str:
    top = await self._db.get_highest_lifetime(channel, limit=10)
    if not top:
        return "No accounts yet."
    lines = ["📈 Highest Lifetime Earned"]
    for i, t in enumerate(top, 1):
        medal = "🥇🥈🥉"[i-1] if i <= 3 else f"{i}."
        lines.append(f"  {medal} {t['username']} — {t['lifetime_earned']:,} Z ({t['rank_name']})")
    return "\n".join(lines)

async def _rank_distribution(self, channel: str) -> str:
    dist = await self._db.get_rank_distribution(channel)
    if not dist:
        return "No accounts yet."
    lines = ["⭐ Rank Distribution"]
    for tier in self._config.ranks.tiers:
        count = dist.get(tier.name, 0)
        if count > 0:
            lines.append(f"  {tier.name}: {count}")
    return "\n".join(lines)
```

---

## 13. PM Command Registrations

### 13.1 Sprint 6 Additions to Command Map

```python
# In PmHandler.__init__:
self._command_map.update({
    "rank": self._cmd_rank,
    "profile": self._cmd_profile,
    "achievements": self._cmd_achievements,
    "top": self._cmd_top,
    "leaderboard": self._cmd_top,   # alias
    "lb": self._cmd_top,            # short alias
})
```

### 13.2 CyTube Promotion in Buy Map

```python
# In PmHandler._cmd_buy handler map:
"cytube2": self._cmd_buy_cytube2,
"level2": self._cmd_buy_cytube2,
```

---

## 14. Public Announcements

### 14.1 Announcement Triggers

| Event | Config Gate | Template Key | Variables |
|---|---|---|---|
| Rank promotion | `announcements.rank_promotion` | `rank_up` | `user`, `rank` |
| Achievement (major) | `announcements.achievement_milestone` | `achievement` | `user`, `achievement` |
| CyTube level 2 | `announcements.rank_promotion` | `rank_up` | `user`, `rank="CyTube Level 2"` |

### 14.2 All Announcements via kryten-py

```python
await self._client.send_chat(channel, announcement_message)
```

No raw NATS publish. No `cytube.commands.{channel}.chat` subjects.

---

## 15. Request-Reply Command Extensions

### 15.1 New Commands on `kryten.economy.command`

```python
_HANDLER_MAP = {
    # ... existing commands ...
    # Sprint 6:
    "rank.get": _handle_rank_get,
    "rank.set": _handle_rank_set,          # Admin override
    "achievement.list": _handle_achievement_list,
    "achievement.grant": _handle_achievement_grant,  # Admin grant
    "leaderboard.top": _handle_leaderboard_top,
    "profile.get": _handle_profile_get,
}
```

---

## 16. Metrics Extensions

### 16.1 New Metrics

```python
lines.append(f'economy_achievements_awarded_total {self._app.achievements_awarded_total}')
lines.append(f'economy_rank_promotions_total {self._app.rank_promotions_total}')
lines.append(f'economy_cytube_promotions_total {self._app.cytube_promotions_total}')

# Rank distribution gauge
for tier_name, count in rank_dist.items():
    lines.append(f'economy_rank_distribution{{rank="{tier_name}"}} {count}')
```

---

## 17. Integration Points

### 17.1 Earn Path Integration

After **any** Z credit (presence tick, chat triggers, gambling wins, tips received):

```python
# 1. Check rank promotion
new_rank = await self._rank_engine.check_rank_promotion(username, channel)

# 2. Check relevant achievements
await self._achievement_engine.check_achievements(
    username, channel,
    relevant_types=["lifetime_earned", "rank_reached"],
)
```

### 17.2 Spend Path Integration

After **any** Z debit:

```python
await self._achievement_engine.check_achievements(
    username, channel,
    relevant_types=["lifetime_spent"],
)
```

### 17.3 Gambling Integration

After gambling outcomes (Sprint 4):

```python
await self._achievement_engine.check_achievements(
    username, channel,
    relevant_types=["lifetime_gambled", "gambling_biggest_win"],
)
```

### 17.4 Chat Message Integration

After chat trigger evaluation (Sprint 3):

```python
await self._achievement_engine.check_achievements(
    username, channel,
    relevant_types=["lifetime_messages", "unique_emotes_used_lifetime"],
)
```

### 17.5 Tip Integration

After a tip is recorded (Sprint 5):

```python
# For sender
await self._achievement_engine.check_achievements(
    sender, channel, relevant_types=["unique_tip_recipients"],
)
# For receiver
await self._achievement_engine.check_achievements(
    receiver, channel, relevant_types=["unique_tip_senders"],
)
```

---

## 18. Test Specifications

### 18.1 Achievement Engine Tests (`tests/test_achievement_engine.py`)

| Test | Description |
|---|---|
| `test_award_first_time` | Achievement awarded, reward credited, PM sent |
| `test_already_awarded_skipped` | Duplicate achievement not re-awarded |
| `test_condition_lifetime_messages` | Threshold met → awarded |
| `test_condition_lifetime_messages_below` | Below threshold → not awarded |
| `test_condition_daily_streak` | Streak threshold met → awarded |
| `test_condition_unique_tip_recipients` | Tip count meets threshold |
| `test_condition_rank_reached` | Tier index meets threshold |
| `test_hidden_achievement_not_shown` | Hidden achievements excluded from unearned list |
| `test_public_announcement` | Achievement with `announce_public` sends chat |
| `test_multiple_achievements_same_event` | Multiple achievements can trigger in one check |
| `test_unknown_condition_type` | Unknown type logged, not awarded |

### 18.2 Rank Engine Tests (`tests/test_rank_engine.py`)

| Test | Description |
|---|---|
| `test_initial_rank` | 0 lifetime → "Extra" |
| `test_rank_at_threshold` | Exactly 1000 → "Grip" |
| `test_rank_promotion` | Lifetime crosses threshold → promote, PM, announce |
| `test_no_promotion_same_rank` | Already at correct rank → no action |
| `test_max_rank` | "Studio Mogul" → no next tier |
| `test_cytube_auto_promotion` | Reaching tier with `cytube_level_promotion` → calls `safe_set_channel_rank()` |
| `test_cytube_promotion_failure_logged` | Rank change fails → logged, no crash |
| `test_rank_discount_calculation` | tier 5 × 0.02 = 10% |
| `test_rank_perks_parsed` | Extra queue slots parsed from perk strings |
| `test_rain_multiplier_parsed` | Rain bonus parsed from perk strings |

### 18.3 CyTube Promotion Tests (`tests/test_cytube_promotion.py`)

| Test | Description |
|---|---|
| `test_purchase_success` | Debits, calls `safe_set_channel_rank(channel, user, 2)` |
| `test_purchase_min_rank_gate` | Below min rank → rejected |
| `test_purchase_insufficient_funds` | Low balance → rejected |
| `test_purchase_failure_refund` | `safe_set_channel_rank` fails → refund |
| `test_purchase_disabled` | Config disabled → rejected |

### 18.4 PM Command Tests (`tests/test_rank_commands.py`)

| Test | Description |
|---|---|
| `test_rank_shows_progress` | Progress bar, next tier, perks |
| `test_rank_max_tier` | Shows "Maximum rank achieved" |
| `test_profile_self` | Own profile with all sections |
| `test_profile_other_user` | `profile @alice` shows Alice's profile |
| `test_profile_not_found` | Unknown user → error |
| `test_achievements_earned_and_progress` | Shows earned + in-progress |
| `test_achievements_hidden_count` | Shows hidden count hint |
| `test_top_earners` | Formatted leaderboard |
| `test_top_richest` | Formatted by balance |
| `test_top_lifetime` | Formatted by lifetime earned |
| `test_rank_distribution` | Count per rank tier |
| `test_top_unknown_subcmd` | Shows usage help |

---

## 19. Acceptance Criteria

### Must Pass

- [ ] Achievement engine awards one-time achievements when conditions are met
- [ ] Achievement rewards credited to balance with transaction log
- [ ] Hidden achievements not shown in `achievements` until earned
- [ ] Rank promotion fires on lifetime earned threshold crossing
- [ ] Rank promotion sends PM and public announcement (if configured)
- [ ] Rank perks applied: spend discount, extra queue slots, rain bonus
- [ ] CyTube level 2 purchasable via `buy cytube2` with min rank gate
- [ ] CyTube level 2 auto-grants on reaching configured top rank
- [ ] CyTube rank change uses `client.safe_set_channel_rank()` — zero raw NATS
- [ ] Failed CyTube promotion refunds the cost
- [ ] `rank` command shows progress bar and active perks
- [ ] `profile` command shows comprehensive user data
- [ ] `achievements` command shows earned + progress + hidden count
- [ ] `top` command shows daily earners, richest, lifetime, rank distribution
- [ ] Achievement checks integrated at earn/spend/gamble/chat/tip event points
- [ ] All announcements via `client.send_chat()` — zero raw NATS
- [ ] All PMs via `client.send_pm()` — zero raw NATS
- [ ] All tests pass (~50 test cases)

### Stretch

- [ ] Achievement progress bars in `achievements` command for all condition types
- [ ] `profile @user` works for looking up other users
- [ ] Rank distribution shown as histogram in `top ranks`

---

## Appendix: kryten-py Methods Used in This Sprint

| Method | Usage |
|---|---|
| `client.send_pm(channel, username, message)` | Achievement notifications, rank promotions, CyTube promotion |
| `client.send_chat(channel, message)` | Public rank-up and achievement announcements |
| `client.safe_set_channel_rank(channel, username, level)` | CyTube level 2 promotion (purchase and auto) |
| `client.nats_request(subject, request, timeout)` | Alias resolution via kryten-userstats (for self-tip detection) |
| `client.subscribe_request_reply(subject, handler)` | Extended command handler |

> No direct NATS imports, no raw subject construction, no `client.publish()` with manual subjects.
