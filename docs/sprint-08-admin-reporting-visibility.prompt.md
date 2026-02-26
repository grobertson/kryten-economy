# Sprint 8 — Admin Tooling, Reporting & Visibility

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 8 of 9  
> **Goal:** Full admin control over the economy via PM commands (gated by CyTube rank), hot-reload config, economy health snapshots, scheduled digests, and comprehensive Prometheus metrics.  
> **Depends on:** Sprint 1 (Core Foundation), Sprint 4 (Gambling stats), Sprint 5 (Pending approvals), Sprint 6 (Ranks/achievements), Sprint 7 (Events/bounties/admin gate)  
> **Enables:** Sprint 9 (Announcement polish, error hardening, integration tests)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Admin Gate Refinement](#3-admin-gate-refinement)
4. [Admin PM Commands — Economy Control](#4-admin-pm-commands--economy-control)
5. [Admin PM Commands — Inspection](#5-admin-pm-commands--inspection)
6. [Admin PM Commands — Content Approval](#6-admin-pm-commands--content-approval)
7. [Admin PM Commands — User Management](#7-admin-pm-commands--user-management)
8. [Config Hot-Reload](#8-config-hot-reload)
9. [Economy Snapshots](#9-economy-snapshots)
10. [Trigger Analytics](#10-trigger-analytics)
11. [Weekly Admin Digest](#11-weekly-admin-digest)
12. [User Daily Digest](#12-user-daily-digest)
13. [Prometheus Metrics Expansion](#13-prometheus-metrics-expansion)
14. [Request-Reply Command Extensions](#14-request-reply-command-extensions)
15. [PM Command Registrations](#15-pm-command-registrations)
16. [Test Specifications](#16-test-specifications)
17. [Acceptance Criteria](#17-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint:

- **16 admin PM commands** — `grant`, `deduct`, `rain`, `set_balance`, `set_rank`, `reload`, `econ:stats`, `econ:user`, `econ:health`, `econ:triggers`, `econ:gambling`, `approve_gif`, `reject_gif`, `ban`, `unban`, `announce` — all gated by CyTube rank ≥ `owner_level` (default 4)
- **Config hot-reload** via `reload` command — re-reads `config.yaml`, validates with Pydantic, applies without restart
- **Economy snapshots** — periodic task writes `economy_snapshots` table for trend tracking
- **Trigger analytics** — per-trigger per-day hit counts written to `trigger_analytics` table
- **Weekly admin digest** — scheduled PM to admin-level users with economy health summary
- **User daily digest** — scheduled PM to active economy users with personal summary
- **Prometheus metrics expansion** — full counter/gauge set matching Section 11 of the master plan
- All commands and notifications via kryten-py wrappers — zero raw NATS

> **⚠️ Ecosystem rule:** All NATS interaction goes through kryten-py's `KrytenClient`. Use `client.send_pm()`, `client.send_chat()` — never raw NATS subjects.

---

## 2. New Database Tables

### 2.1 `economy_snapshots` Table

```sql
CREATE TABLE IF NOT EXISTS economy_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    snapshot_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_accounts INTEGER,
    total_z_circulation INTEGER,
    active_economy_users_today INTEGER,
    z_earned_today INTEGER,
    z_spent_today INTEGER,
    z_gambled_net_today INTEGER,
    median_balance INTEGER,
    participation_rate REAL         -- economy users / channel population
);

CREATE INDEX IF NOT EXISTS idx_snapshots_channel ON economy_snapshots(channel, snapshot_time);
```

### 2.2 `trigger_analytics` Table

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

CREATE INDEX IF NOT EXISTS idx_trigger_analytics_date ON trigger_analytics(channel, date);
```

### 2.3 `banned_users` Table

```sql
CREATE TABLE IF NOT EXISTS banned_users (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    banned_by TEXT NOT NULL,
    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT,
    UNIQUE(username, channel)
);
```

### 2.4 Database Methods to Add

```python
# ── Snapshot Methods ─────────────────────────────────────────

async def write_snapshot(self, channel: str, data: dict) -> None:
    """Insert an economy snapshot row."""

async def get_latest_snapshot(self, channel: str) -> dict | None:
    """Get the most recent snapshot for a channel."""

async def get_snapshot_history(self, channel: str, days: int = 7) -> list[dict]:
    """Get recent snapshots for trend analysis."""

# ── Trigger Analytics Methods ────────────────────────────────

async def increment_trigger_analytics(
    self, channel: str, trigger_id: str, date: str, z_awarded: int,
) -> None:
    """Upsert trigger analytics: increment hit_count and total_z_awarded."""

async def get_trigger_analytics(self, channel: str, date: str) -> list[dict]:
    """Get all trigger analytics for a date."""

async def get_trigger_analytics_range(
    self, channel: str, start_date: str, end_date: str,
) -> list[dict]:
    """Get trigger analytics across a date range."""

# ── Ban Methods ──────────────────────────────────────────────

async def ban_user(self, username: str, channel: str, banned_by: str, reason: str = "") -> bool:
    """Ban a user from the economy. Returns True if newly banned."""

async def unban_user(self, username: str, channel: str) -> bool:
    """Remove economy ban. Returns True if was banned."""

async def is_banned(self, username: str, channel: str) -> bool:
    """Check if a user is banned from the economy."""

# ── Aggregate Queries for Reporting ──────────────────────────

async def get_total_circulation(self, channel: str) -> int:
    """Sum of all balances in channel."""

async def get_median_balance(self, channel: str) -> int:
    """Median balance across all accounts."""

async def get_active_economy_users_today(self, channel: str, date: str) -> int:
    """Count users who earned or spent today."""

async def get_daily_totals(self, channel: str, date: str) -> dict:
    """Get {z_earned, z_spent, z_gambled_in, z_gambled_out} for a date."""

async def get_weekly_totals(self, channel: str, start_date: str, end_date: str) -> dict:
    """Aggregate totals across a week for admin digest."""

async def get_top_earners_range(self, channel: str, start_date: str, end_date: str, limit: int = 5) -> list[dict]:
    """Top earners over a date range."""

async def get_top_spenders_range(self, channel: str, start_date: str, end_date: str, limit: int = 5) -> list[dict]:
    """Top spenders over a date range."""

async def get_gambling_summary_global(self, channel: str) -> dict:
    """Global gambling stats: total_in, total_out, active_gamblers, actual_house_edge."""

async def get_all_accounts_count(self, channel: str) -> int:
    """Total number of accounts."""

async def get_participation_rate(self, channel: str, total_channel_users: int) -> float:
    """Percentage of channel users who have economy accounts."""
```

---

## 3. Admin Gate Refinement

### 3.1 Centralized Admin Check

Sprint 7 introduced the admin gate concept. Sprint 8 formalizes it:

```python
class PmHandler:
    """Handles PM commands."""

    def _is_admin(self, rank: int) -> bool:
        """Check if user's CyTube rank meets admin threshold."""
        return rank >= self._config.admin.owner_level  # default 4

    async def _dispatch_command(
        self, username: str, channel: str, command: str, args: list[str], rank: int,
    ) -> str:
        """Route a command to its handler."""
        # Check economy ban (except for admin commands from admins)
        if not self._is_admin(rank):
            if await self._db.is_banned(username, channel):
                return "⛔ Your economy access has been suspended."

        # Admin commands
        if command in self._admin_command_map:
            if not self._is_admin(rank):
                return "⛔ This command requires admin privileges."
            handler = self._admin_command_map[command]
            return await handler(username, channel, args)

        # User commands
        if command in self._command_map:
            handler = self._command_map[command]
            return await handler(username, channel, args)

        return None  # Unknown command
```

### 3.2 CyTube Rank Source

The `rank` field comes from `ChatMessageEvent.rank` (kryten-py delivers this from the CyTube PM event). CyTube ranks: 0=Guest, 1=User, 2=Moderator, 3=Admin, 4=Owner, 5=Founder.

---

## 4. Admin PM Commands — Economy Control

### 4.1 `grant @user <amount> [reason]`

```python
async def _cmd_grant(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Credit Z to a user."""
    if len(args) < 2:
        return "Usage: grant @user <amount> [reason]"
    target = args[0].lstrip("@")
    try:
        amount = int(args[1])
    except ValueError:
        return "Amount must be a number."
    if amount <= 0:
        return "Amount must be positive."

    reason = " ".join(args[2:]) if len(args) > 2 else f"Admin grant by {username}"

    await self._db.credit(
        target, channel, amount,
        tx_type="admin_grant",
        trigger_id="admin.grant",
        reason=reason,
    )
    balance = (await self._db.get_account(target, channel))["balance"]

    await self._client.send_pm(
        channel, target,
        f"💰 You received {amount:,} Z from an admin. Reason: {reason}"
    )

    return f"Granted {amount:,} Z to {target}. New balance: {balance:,} Z"
```

### 4.2 `deduct @user <amount> [reason]`

```python
async def _cmd_deduct(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Debit Z from a user."""
    if len(args) < 2:
        return "Usage: deduct @user <amount> [reason]"
    target = args[0].lstrip("@")
    try:
        amount = int(args[1])
    except ValueError:
        return "Amount must be a number."
    if amount <= 0:
        return "Amount must be positive."

    reason = " ".join(args[2:]) if len(args) > 2 else f"Admin deduction by {username}"

    success = await self._db.atomic_debit(
        target, channel, amount,
        tx_type="admin_deduct",
        trigger_id="admin.deduct",
        reason=reason,
    )
    if not success:
        return f"Failed: {target} has insufficient balance."

    balance = (await self._db.get_account(target, channel))["balance"]

    await self._client.send_pm(
        channel, target,
        f"💸 {amount:,} Z deducted by an admin. Reason: {reason}"
    )

    return f"Deducted {amount:,} Z from {target}. New balance: {balance:,} Z"
```

### 4.3 `rain <amount>`

```python
async def _cmd_rain(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Distribute Z equally among all present users."""
    if not args:
        return "Usage: rain <amount>"
    try:
        total = int(args[0])
    except ValueError:
        return "Amount must be a number."
    if total <= 0:
        return "Amount must be positive."

    present = self._presence.get_present_users(channel)
    if not present:
        return "No users present."

    per_user = max(1, total // len(present))
    actual_total = per_user * len(present)

    for user in present:
        await self._db.credit(
            user, channel, per_user,
            tx_type="admin_rain",
            trigger_id="admin.rain",
            reason=f"Admin rain by {username}",
        )
        await self._client.send_pm(
            channel, user,
            f"☔ Admin rain! +{per_user:,} Z from {username}"
        )

    template = self._config.announcements.templates.get(
        "rain", "☔ Rain! {count} users just got free {currency}."
    )
    await self._client.send_chat(
        channel,
        template.format(count=len(present), currency=self._config.currency.name)
    )

    return f"Rained {actual_total:,} Z ({per_user:,} each) to {len(present)} users."
```

### 4.4 `set_balance @user <amount>`

```python
async def _cmd_set_balance(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Hard-set a user's balance."""
    if len(args) < 2:
        return "Usage: set_balance @user <amount>"
    target = args[0].lstrip("@")
    try:
        amount = int(args[1])
    except ValueError:
        return "Amount must be a number."
    if amount < 0:
        return "Balance cannot be negative."

    account = await self._db.get_or_create_account(target, channel)
    old_balance = account["balance"]
    diff = amount - old_balance

    await self._db.set_balance(target, channel, amount)

    # Log the adjustment as a transaction
    await self._db.log_transaction(
        target, channel,
        amount=diff,
        tx_type="admin_set_balance",
        trigger_id="admin.set_balance",
        reason=f"Balance set to {amount:,} by {username} (was {old_balance:,})",
    )

    return f"Set {target}'s balance to {amount:,} Z (was {old_balance:,} Z)."
```

### 4.5 `set_rank @user <rank_name>`

```python
async def _cmd_set_rank(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Override a user's economy rank."""
    if len(args) < 2:
        return "Usage: set_rank @user <rank_name>"
    target = args[0].lstrip("@")
    rank_name = " ".join(args[1:])

    # Validate rank name exists in config
    valid_names = [t.name for t in self._config.ranks.tiers]
    if rank_name not in valid_names:
        return f"Unknown rank. Valid: {', '.join(valid_names)}"

    await self._db.update_account_rank(target, channel, rank_name)

    await self._client.send_pm(
        channel, target,
        f"⭐ Your rank has been set to **{rank_name}** by an admin."
    )

    return f"Set {target}'s rank to {rank_name}."
```

### 4.6 `announce <message>`

```python
async def _cmd_announce(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Post a message in public chat via the bot."""
    if not args:
        return "Usage: announce <message>"
    message = " ".join(args)
    await self._client.send_chat(channel, message)
    return f"Announced: {message}"
```

---

## 5. Admin PM Commands — Inspection

### 5.1 `econ:stats`

```python
async def _cmd_econ_stats(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Economy overview."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = await self._db.get_daily_totals(channel, today)
    accounts = await self._db.get_all_accounts_count(channel)
    circulation = await self._db.get_total_circulation(channel)
    active = await self._db.get_active_economy_users_today(channel, today)
    present = len(self._presence.get_present_users(channel))

    return (
        f"📊 Economy Overview:\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Accounts: {accounts:,}\n"
        f"Currently present: {present}\n"
        f"Active today: {active}\n"
        f"Total circulation: {circulation:,} Z\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Today's activity:\n"
        f"  Earned: {totals.get('z_earned', 0):,} Z\n"
        f"  Spent: {totals.get('z_spent', 0):,} Z\n"
        f"  Gambled in: {totals.get('z_gambled_in', 0):,} Z\n"
        f"  Gambled out: {totals.get('z_gambled_out', 0):,} Z\n"
        f"  Net gamble: {totals.get('z_gambled_out', 0) - totals.get('z_gambled_in', 0):+,} Z"
    )
```

### 5.2 `econ:user <name>`

```python
async def _cmd_econ_user(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Full user inspection."""
    if not args:
        return "Usage: econ:user <username>"
    target = args[0].lstrip("@")

    account = await self._db.get_account(target, channel)
    if not account:
        return f"No account for '{target}'."

    banned = await self._db.is_banned(target, channel)
    achievements = await self._db.get_achievement_count(target, channel)
    gambling = await self._db.get_gambling_summary(target, channel)

    lines = [
        f"👤 Admin Inspection: {target}",
        "━" * 30,
        f"Balance: {account['balance']:,} Z",
        f"Lifetime earned: {account.get('lifetime_earned', 0):,} Z",
        f"Lifetime spent: {account.get('lifetime_spent', 0):,} Z",
        f"Rank: {account.get('rank_name', 'Extra')}",
        f"Streak: {account.get('current_streak', 0)} days",
        f"Achievements: {achievements}",
        f"Banned: {'⛔ YES' if banned else 'No'}",
        f"Created: {account.get('created_at', 'unknown')}",
        f"Last seen: {account.get('last_seen', 'unknown')}",
    ]

    if gambling and gambling.get("total_games", 0) > 0:
        lines.append(f"Gambling: {gambling['total_games']} games, net {gambling['net_profit']:+,} Z")

    return "\n".join(lines)
```

### 5.3 `econ:health`

```python
async def _cmd_econ_health(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Inflation indicators and economy health."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    circulation = await self._db.get_total_circulation(channel)
    median = await self._db.get_median_balance(channel)
    totals = await self._db.get_daily_totals(channel, today)
    accounts = await self._db.get_all_accounts_count(channel)
    present = len(self._presence.get_present_users(channel))

    earned = totals.get("z_earned", 0)
    spent = totals.get("z_spent", 0)
    gamble_net = totals.get("z_gambled_out", 0) - totals.get("z_gambled_in", 0)
    net_flow = earned - spent + gamble_net

    participation = (accounts / present * 100) if present > 0 else 0

    # Compare to yesterday's snapshot
    latest = await self._db.get_latest_snapshot(channel)
    prev_circ = latest.get("total_z_circulation", circulation) if latest else circulation
    circ_change = circulation - prev_circ

    return (
        f"🏥 Economy Health:\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Circulation: {circulation:,} Z ({circ_change:+,} since last snapshot)\n"
        f"Median balance: {median:,} Z\n"
        f"Participation: {participation:.1f}% ({accounts}/{present})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Today's Net Flow:\n"
        f"  +Earned: {earned:,}\n"
        f"  −Spent: {spent:,}\n"
        f"  ±Gambling: {gamble_net:+,}\n"
        f"  = Net: {net_flow:+,} Z {'(inflationary)' if net_flow > 0 else '(deflationary)'}"
    )
```

### 5.4 `econ:triggers`

```python
async def _cmd_econ_triggers(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Trigger hit rates — identify hot and dead triggers."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    analytics = await self._db.get_trigger_analytics(channel, today)

    if not analytics:
        return "No trigger data for today."

    # Sort by hit count descending
    sorted_triggers = sorted(analytics, key=lambda t: t["hit_count"], reverse=True)

    lines = ["📊 Trigger Analytics (Today):"]
    lines.append(f"{'Trigger':<30} {'Hits':>6} {'Users':>6} {'Z':>8}")
    lines.append("─" * 55)

    for t in sorted_triggers:
        lines.append(
            f"{t['trigger_id']:<30} {t['hit_count']:>6} "
            f"{t['unique_users']:>6} {t['total_z_awarded']:>8,}"
        )

    # Identify dead triggers (configured but 0 hits today)
    all_configured = self._get_all_trigger_ids()
    active_ids = {t["trigger_id"] for t in analytics}
    dead = all_configured - active_ids
    if dead:
        lines.append(f"\n⚠️ Dead triggers (0 hits today): {', '.join(sorted(dead))}")

    return "\n".join(lines)

def _get_all_trigger_ids(self) -> set[str]:
    """Collect all configured trigger IDs for dead-trigger detection."""
    ids = set()
    # Presence triggers
    ids.add("presence.base")
    ids.add("presence.night_watch")
    # Chat triggers
    for name in ("long_message", "laugh_received", "kudos_received",
                 "first_message_of_day", "conversation_starter", "first_after_media_change"):
        if getattr(self._config.chat_triggers, name, None):
            ids.add(f"chat.{name}")
    # Content triggers
    for name in ("comment_during_media", "like_current", "survived_full_media",
                 "present_at_event_start"):
        if getattr(self._config.content_triggers, name, None):
            ids.add(f"content.{name}")
    # Social triggers
    for name in ("greeted_newcomer", "mentioned_by_other", "bot_interaction"):
        if getattr(self._config.social_triggers, name, None):
            ids.add(f"social.{name}")
    return ids
```

### 5.5 `econ:gambling`

```python
async def _cmd_econ_gambling(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Gambling statistics — house edge, totals, active gamblers."""
    stats = await self._db.get_gambling_summary_global(channel)

    if not stats or stats.get("total_games", 0) == 0:
        return "No gambling activity recorded."

    total_in = stats.get("total_in", 0)
    total_out = stats.get("total_out", 0)
    actual_edge = ((total_in - total_out) / total_in * 100) if total_in > 0 else 0

    # Configured house edge (slot machine)
    configured_ev = 0
    for p in self._config.gambling.spin.payouts:
        configured_ev += p.multiplier * p.probability
    configured_edge = (1 - configured_ev) * 100

    return (
        f"🎰 Gambling Report:\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total wagered: {total_in:,} Z\n"
        f"Total paid out: {total_out:,} Z\n"
        f"House profit: {total_in - total_out:,} Z\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Actual house edge: {actual_edge:.1f}%\n"
        f"Configured house edge (slots): {configured_edge:.1f}%\n"
        f"Active gamblers: {stats.get('active_gamblers', 0)}\n"
        f"Total games: {stats.get('total_games', 0):,}"
    )
```

---

## 6. Admin PM Commands — Content Approval

### 6.1 `approve_gif @user` / `reject_gif @user`

These commands manage pending GIF purchase approvals (from Sprint 5 vanity shop):

```python
async def _cmd_approve_gif(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Approve a pending channel GIF purchase."""
    if not args:
        return "Usage: approve_gif @user"
    target = args[0].lstrip("@")

    pending = await self._db.get_pending_approval(target, channel, "channel_gif")
    if not pending:
        return f"No pending GIF approval for {target}."

    await self._db.resolve_approval(pending["id"], "approved", username)

    await self._client.send_pm(
        channel, target,
        f"✅ Your channel GIF has been approved by {username}!"
    )

    return f"Approved GIF for {target}."

async def _cmd_reject_gif(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Reject a pending channel GIF purchase and refund."""
    if not args:
        return "Usage: reject_gif @user"
    target = args[0].lstrip("@")

    pending = await self._db.get_pending_approval(target, channel, "channel_gif")
    if not pending:
        return f"No pending GIF approval for {target}."

    await self._db.resolve_approval(pending["id"], "rejected", username)

    # Refund
    await self._db.credit(
        target, channel, pending["cost"],
        tx_type="refund",
        trigger_id="refund.gif_rejected",
        reason=f"Channel GIF rejected by {username}",
    )

    await self._client.send_pm(
        channel, target,
        f"❌ Your channel GIF was rejected by {username}. "
        f"Your {pending['cost']:,} Z have been refunded."
    )

    return f"Rejected GIF for {target}. {pending['cost']:,} Z refunded."
```

---

## 7. Admin PM Commands — User Management

### 7.1 `ban @user` / `unban @user`

Economy banning keeps the user's balance intact but blocks all earning and spending:

```python
async def _cmd_ban(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Ban a user from the economy."""
    if not args:
        return "Usage: ban @user [reason]"
    target = args[0].lstrip("@")
    reason = " ".join(args[1:]) if len(args) > 1 else ""

    if await self._db.is_banned(target, channel):
        return f"{target} is already banned."

    await self._db.ban_user(target, channel, username, reason)

    await self._client.send_pm(
        channel, target,
        f"⛔ Your economy access has been suspended."
        + (f" Reason: {reason}" if reason else "")
    )

    return f"Banned {target} from the economy." + (f" Reason: {reason}" if reason else "")

async def _cmd_unban(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Restore a user's economy access."""
    if not args:
        return "Usage: unban @user"
    target = args[0].lstrip("@")

    if not await self._db.is_banned(target, channel):
        return f"{target} is not banned."

    await self._db.unban_user(target, channel)

    await self._client.send_pm(
        channel, target,
        "✅ Your economy access has been restored."
    )

    return f"Unbanned {target}."
```

### 7.2 Ban Enforcement Points

The economy ban is enforced at the top of the command dispatch (Section 3.1) and in the earning path:

```python
# In the earning credit path:
async def _credit_with_multiplier(self, username, channel, ...):
    if await self._db.is_banned(username, channel):
        return 0  # Silently skip earning for banned users
    # ... proceed with credit
```

---

## 8. Config Hot-Reload

### 8.1 `reload` Command

```python
async def _cmd_reload(self, username: str, channel: str, args: list[str]) -> str:
    """Admin: Hot-reload config.yaml without restart."""
    try:
        new_config = self._load_and_validate_config()
        self._apply_config(new_config)
        self._logger.info("Config reloaded by %s", username)
        return "✅ Config reloaded successfully."
    except Exception as e:
        self._logger.error("Config reload failed: %s", e)
        return f"❌ Config reload failed: {e}"

def _load_and_validate_config(self) -> EconomyConfig:
    """Re-read config.yaml and validate via Pydantic."""
    import yaml
    config_path = self._config_path  # Stored at startup
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return EconomyConfig(**raw)

def _apply_config(self, new_config: EconomyConfig) -> None:
    """Apply a validated config to all components.
    
    Some settings require component reinitialization.
    Others are hot-swappable references.
    """
    old_config = self._config
    self._config = new_config

    # Update components that hold config references
    self._presence.update_config(new_config)
    self._earning_engine.update_config(new_config)
    self._spending_engine.update_config(new_config)
    self._gambling_engine.update_config(new_config)
    self._achievement_engine.update_config(new_config)
    self._rank_engine.update_config(new_config)
    self._multiplier_engine.update_config(new_config)
    self._competition_engine.update_config(new_config)
    self._bounty_manager.update_config(new_config)

    # Log significant changes
    if new_config.presence.base_rate_per_minute != old_config.presence.base_rate_per_minute:
        self._logger.info(
            "Presence rate changed: %s → %s",
            old_config.presence.base_rate_per_minute,
            new_config.presence.base_rate_per_minute,
        )
```

### 8.2 Component `update_config` Pattern

Each engine/manager exposes:

```python
def update_config(self, new_config: EconomyConfig) -> None:
    """Hot-swap the config reference. Re-index if needed."""
    self._config = new_config
    # Re-index anything derived from config (e.g., achievement condition map)
```

---

## 9. Economy Snapshots

### 9.1 Periodic Snapshot Task

Runs every 6 hours (configurable) to capture economy state:

```python
async def _schedule_snapshots(self) -> None:
    """Capture economy snapshots periodically."""
    interval = 6 * 3600  # 6 hours
    while True:
        await asyncio.sleep(interval)
        for channel in self._active_channels():
            try:
                await self._capture_snapshot(channel)
            except Exception as e:
                self._logger.error("Snapshot error for %s: %s", channel, e)

async def _capture_snapshot(self, channel: str) -> None:
    """Capture a single economy snapshot."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals = await self._db.get_daily_totals(channel, today)
    present_count = len(self._presence.get_present_users(channel))

    data = {
        "total_accounts": await self._db.get_all_accounts_count(channel),
        "total_z_circulation": await self._db.get_total_circulation(channel),
        "active_economy_users_today": await self._db.get_active_economy_users_today(channel, today),
        "z_earned_today": totals.get("z_earned", 0),
        "z_spent_today": totals.get("z_spent", 0),
        "z_gambled_net_today": totals.get("z_gambled_out", 0) - totals.get("z_gambled_in", 0),
        "median_balance": await self._db.get_median_balance(channel),
        "participation_rate": await self._db.get_participation_rate(channel, present_count),
    }

    await self._db.write_snapshot(channel, data)
    self._logger.debug("Snapshot captured for %s", channel)
```

---

## 10. Trigger Analytics

### 10.1 Recording Trigger Hits

In the centralized earning path, after each credit:

```python
# After crediting Z via any trigger:
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
await self._db.increment_trigger_analytics(channel, trigger_id, today, amount)
```

### 10.2 Unique User Tracking

The `increment_trigger_analytics` method uses upsert logic:

```python
async def increment_trigger_analytics(
    self, channel: str, trigger_id: str, date: str, z_awarded: int,
) -> None:
    def _sync():
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO trigger_analytics (channel, trigger_id, date, hit_count, unique_users, total_z_awarded)
                VALUES (?, ?, ?, 1, 1, ?)
                ON CONFLICT(channel, trigger_id, date)
                DO UPDATE SET
                    hit_count = hit_count + 1,
                    total_z_awarded = total_z_awarded + ?
            """, (channel, trigger_id, date, z_awarded, z_awarded))
            conn.commit()
        finally:
            conn.close()
    await asyncio.get_running_loop().run_in_executor(None, _sync)
```

**Note:** The `unique_users` count is approximate in this simple upsert. For exact counts, track in-memory per trigger per day or use a more complex query. For Sprint 8 this is acceptable — exact unique counts can be derived from the transactions table if needed.

---

## 11. Weekly Admin Digest

### 11.1 Scheduler

```python
async def _schedule_admin_digest(self) -> None:
    """Send weekly admin digest at configured hour."""
    send_hour = self._config.digest.admin_digest.send_hour_utc  # default 5
    while True:
        now = datetime.now(timezone.utc)
        # Next Monday at send_hour
        # weekday(): Mon=0. (7-0)%7 = 0 means "today is Monday".
        # If it's Monday and the hour hasn't passed, target today.
        days_until_monday = (7 - now.weekday()) % 7
        target = now.replace(
            hour=send_hour, minute=0, second=0, microsecond=0,
        ) + timedelta(days=days_until_monday)
        if target <= now:
            target += timedelta(weeks=1)
        
        delay = (target - now).total_seconds()
        await asyncio.sleep(delay)
        
        for channel in self._active_channels():
            try:
                await self._send_admin_digest(channel)
            except Exception as e:
                self._logger.error("Admin digest error for %s: %s", channel, e)
```

### 11.2 Digest Content

```python
async def _send_admin_digest(self, channel: str) -> None:
    """Generate and send weekly admin digest."""
    now = datetime.now(timezone.utc)
    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    weekly = await self._db.get_weekly_totals(channel, start, end)
    top_earners = await self._db.get_top_earners_range(channel, start, end, limit=5)
    top_spenders = await self._db.get_top_spenders_range(channel, start, end, limit=5)
    gambling = await self._db.get_gambling_summary_global(channel)
    circulation = await self._db.get_total_circulation(channel)
    snapshots = await self._db.get_snapshot_history(channel, days=7)

    # Calculate circulation change
    if snapshots and len(snapshots) >= 2:
        circ_change = snapshots[-1].get("total_z_circulation", 0) - snapshots[0].get("total_z_circulation", 0)
    else:
        circ_change = 0

    lines = [
        f"📊 Weekly Economy Digest ({start} → {end})",
        "━" * 40,
        f"Total Z minted: {weekly.get('z_earned', 0):,}",
        f"Total Z spent: {weekly.get('z_spent', 0):,}",
        f"Total Z gambled: {weekly.get('z_gambled_in', 0):,}",
        f"Net circulation change: {circ_change:+,} Z",
        f"Current circulation: {circulation:,} Z",
        "",
        "🏆 Top 5 Earners:",
    ]
    for i, e in enumerate(top_earners, 1):
        lines.append(f"  {i}. {e['username']} — {e['earned']:,} Z")

    lines.append("\n💸 Top 5 Spenders:")
    for i, s in enumerate(top_spenders, 1):
        lines.append(f"  {i}. {s['username']} — {s['spent']:,} Z")

    if gambling and gambling.get("total_games", 0) > 0:
        total_in = gambling.get("total_in", 0)
        total_out = gambling.get("total_out", 0)
        edge = ((total_in - total_out) / total_in * 100) if total_in > 0 else 0
        lines.append(f"\n🎰 Gambling: {gambling['total_games']:,} games, actual edge: {edge:.1f}%")

    digest_msg = "\n".join(lines)

    # Send to all admin-level users who are in the channel
    # We identify admins by querying accounts with high CyTube rank
    # Since we don't store CyTube rank persistently, we send to users
    # who have rank_name >= a configurable threshold, or simply to
    # users who have used admin commands recently.
    # For simplicity: send to all present users with CyTube rank >= owner_level
    # The presence tracker can optionally store rank.
    # Fallback: send to a configured admin list or anyone currently present.
    admins = self._presence.get_admin_users(channel, self._config.admin.owner_level)
    for admin in admins:
        await self._client.send_pm(channel, admin, digest_msg)

    self._logger.info("Admin digest sent to %d admins in %s", len(admins), channel)
```

### 11.3 Presence Tracker Admin Tracking

The presence tracker stores the CyTube rank seen on user events:

```python
# In PresenceTracker, when processing adduser events:
def update_user_rank(self, channel: str, username: str, rank: int) -> None:
    """Track the latest known CyTube rank for a user."""
    key = (channel, username)
    self._user_ranks[key] = rank

def get_admin_users(self, channel: str, min_rank: int) -> list[str]:
    """Get present users with CyTube rank >= min_rank."""
    present = self.get_present_users(channel)
    return [
        u for u in present
        if self._user_ranks.get((channel, u), 0) >= min_rank
    ]
```

---

## 12. User Daily Digest

### 12.1 Scheduler

```python
async def _schedule_user_digest(self) -> None:
    """Send daily user digest at configured hour."""
    if not self._config.digest.user_digest.enabled:
        return
    
    send_hour = self._config.digest.user_digest.send_hour_utc  # default 4
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=send_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        
        delay = (target - now).total_seconds()
        await asyncio.sleep(delay)
        
        for channel in self._active_channels():
            try:
                await self._send_user_digests(channel)
            except Exception as e:
                self._logger.error("User digest error for %s: %s", channel, e)
```

### 12.2 Digest Content

```python
async def _send_user_digests(self, channel: str) -> None:
    """Send personalized daily digest to active economy users."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Get users who were active yesterday
    activities = await self._db.get_daily_activity_all(channel, yesterday)
    
    template = self._config.digest.user_digest.message
    
    for activity in activities:
        username = activity["username"]
        account = await self._db.get_account(username, channel)
        if not account:
            continue
        
        tier_index, tier = self._rank_engine.get_rank_for_lifetime(
            account.get("lifetime_earned", 0)
        )
        next_tier = self._rank_engine.get_next_tier(tier_index)
        
        # Calculate days to next rank
        if next_tier:
            remaining = next_tier.min_lifetime_earned - account.get("lifetime_earned", 0)
            daily_avg = activity.get("z_earned", 1) or 1  # Avoid div by zero
            days_away = max(1, remaining // daily_avg)
            next_goal = f"{next_tier.name} ({remaining:,} Z away, ~{days_away} days)"
        else:
            next_goal = "Maximum rank achieved! 🏆"
            days_away = 0
        
        msg = template.format(
            earned=activity.get("z_earned", 0),
            spent=activity.get("z_spent", 0),
            balance=account["balance"],
            rank=tier.name,
            streak=account.get("current_streak", 0),
            currency=self._config.currency.symbol,
            next_goal_description=next_goal,
            days_away=days_away,
        )
        
        await self._client.send_pm(channel, username, msg)
    
    self._logger.info("User digests sent to %d users in %s", len(activities), channel)
```

---

## 13. Prometheus Metrics Expansion

### 13.1 Full Counter/Gauge Set

```python
async def _generate_metrics(self) -> str:
    """Generate Prometheus metrics text."""
    lines = []
    
    # ── Counters ──────────────────────────────────────────────
    # By trigger_id
    for trigger_id, count in self._app.trigger_totals.items():
        lines.append(f'economy_z_earned_total{{trigger="{trigger_id}"}} {count}')
    
    # By spend type
    for spend_type, count in self._app.spend_totals.items():
        lines.append(f'economy_z_spent_total{{type="{spend_type}"}} {count}')
    
    lines.append(f'economy_z_gambled_in_total {self._app.z_gambled_in_total}')
    lines.append(f'economy_z_gambled_out_total {self._app.z_gambled_out_total}')
    
    # By event type
    for event_type, count in self._app.events_processed.items():
        lines.append(f'economy_events_processed_total{{type="{event_type}"}} {count}')
    
    # By command
    for cmd, count in self._app.commands_processed.items():
        lines.append(f'economy_commands_processed_total{{command="{cmd}"}} {count}')
    
    # By trigger
    for trigger_id, count in self._app.trigger_hits.items():
        lines.append(f'economy_trigger_hits_total{{trigger="{trigger_id}"}} {count}')
    
    # Sprint 6 counters
    lines.append(f'economy_achievements_awarded_total {self._app.achievements_awarded_total}')
    lines.append(f'economy_rank_promotions_total {self._app.rank_promotions_total}')
    lines.append(f'economy_cytube_promotions_total {self._app.cytube_promotions_total}')
    
    # Sprint 7 counters
    lines.append(f'economy_competition_awards_total {self._app.competition_awards_total}')
    lines.append(f'economy_bounties_created_total {self._app.bounties_created_total}')
    lines.append(f'economy_bounties_claimed_total {self._app.bounties_claimed_total}')
    
    # ── Gauges ────────────────────────────────────────────────
    for channel in self._app.active_channels():
        present = len(self._app.presence.get_present_users(channel))
        circulation = await self._app.db.get_total_circulation(channel)
        median = await self._app.db.get_median_balance(channel)
        accounts = await self._app.db.get_all_accounts_count(channel)
        participation = (accounts / present * 100) if present > 0 else 0
        
        combined, active_mults = self._app.multiplier_engine.get_combined_multiplier(channel)
        
        tag = f'channel="{channel}"'
        lines.append(f'economy_active_users{{{tag}}} {present}')
        lines.append(f'economy_total_circulation{{{tag}}} {circulation}')
        lines.append(f'economy_median_balance{{{tag}}} {median}')
        lines.append(f'economy_participation_rate{{{tag}}} {participation:.2f}')
        lines.append(f'economy_active_multiplier{{{tag}}} {combined:.2f}')
        
        # Rank distribution
        rank_dist = await self._app.db.get_rank_distribution(channel)
        for rank_name, count in rank_dist.items():
            lines.append(f'economy_rank_distribution{{{tag},rank="{rank_name}"}} {count}')
    
    return "\n".join(lines)
```

---

## 14. Request-Reply Command Extensions

### 14.1 New Admin Commands on `kryten.economy.command`

```python
_HANDLER_MAP = {
    # ... existing commands ...
    # Sprint 8:
    "admin.grant": _handle_admin_grant,
    "admin.deduct": _handle_admin_deduct,
    "admin.set_balance": _handle_admin_set_balance,
    "admin.set_rank": _handle_admin_set_rank,
    "admin.ban": _handle_admin_ban,
    "admin.unban": _handle_admin_unban,
    "admin.reload": _handle_admin_reload,
    "economy.stats": _handle_economy_stats,
    "economy.health": _handle_economy_health,
    "economy.snapshot": _handle_economy_snapshot,
}
```

These request-reply handlers expose the same functionality as the PM admin commands, enabling other kryten services to interact with the economy programmatically.

---

## 15. PM Command Registrations

### 15.1 Sprint 8 Admin Command Map

```python
self._admin_command_map.update({
    # Sprint 7 (already registered):
    "event": self._cmd_event,
    "claim_bounty": self._cmd_claim_bounty,
    # Sprint 8:
    "grant": self._cmd_grant,
    "deduct": self._cmd_deduct,
    "rain": self._cmd_rain,
    "set_balance": self._cmd_set_balance,
    "set_rank": self._cmd_set_rank,
    "reload": self._cmd_reload,
    "econ:stats": self._cmd_econ_stats,
    "econ:user": self._cmd_econ_user,
    "econ:health": self._cmd_econ_health,
    "econ:triggers": self._cmd_econ_triggers,
    "econ:gambling": self._cmd_econ_gambling,
    "approve_gif": self._cmd_approve_gif,
    "reject_gif": self._cmd_reject_gif,
    "ban": self._cmd_ban,
    "unban": self._cmd_unban,
    "announce": self._cmd_announce,
})
```

---

## 16. Test Specifications

### 16.1 Admin Command Tests (`tests/test_admin_commands.py`)

| Test | Description |
|---|---|
| `test_grant_success` | Credits target, logs transaction, PMs target |
| `test_grant_non_admin` | CyTube rank < 4 → rejected |
| `test_deduct_success` | Debits target, logs transaction |
| `test_deduct_insufficient` | Target has less → "insufficient balance" |
| `test_rain_distributes` | Splits among present users, PMs each, announces |
| `test_rain_no_users` | No present → "No users present" |
| `test_set_balance` | Hard-sets balance, logs diff as transaction |
| `test_set_rank_valid` | Updates rank_name, PMs target |
| `test_set_rank_invalid` | Unknown rank name → "Valid: ..." |
| `test_announce_sends_chat` | Sends message via `client.send_chat()` |
| `test_ban_user` | Inserts ban, PMs target |
| `test_ban_already_banned` | Already banned → "already banned" |
| `test_unban_user` | Removes ban, PMs target |
| `test_unban_not_banned` | Not banned → "not banned" |
| `test_banned_user_cannot_earn` | Earning silently skipped |
| `test_banned_user_cannot_command` | User commands return "suspended" |
| `test_banned_user_admin_commands_work` | Admin can still run admin commands even if banned |

### 16.2 Inspection Command Tests (`tests/test_admin_inspection.py`)

| Test | Description |
|---|---|
| `test_econ_stats_format` | Returns formatted stats with all fields |
| `test_econ_user_found` | Full user inspection output |
| `test_econ_user_not_found` | Unknown user → error |
| `test_econ_health_inflation` | Reports inflationary when earned > spent |
| `test_econ_health_deflation` | Reports deflationary |
| `test_econ_triggers_hot_and_dead` | Shows active triggers, flags dead ones |
| `test_econ_gambling_stats` | Reports actual vs. configured house edge |
| `test_econ_gambling_no_data` | No gambling → "No gambling activity" |

### 16.3 Content Approval Tests (`tests/test_gif_approval.py`)

| Test | Description |
|---|---|
| `test_approve_gif` | Resolves pending, PMs user |
| `test_approve_no_pending` | No pending → error |
| `test_reject_gif_refund` | Resolves rejected, refunds cost, PMs user |

### 16.4 Config Reload Tests (`tests/test_config_reload.py`)

| Test | Description |
|---|---|
| `test_reload_valid` | Reads new config, applies, returns success |
| `test_reload_invalid_yaml` | Malformed YAML → error, old config retained |
| `test_reload_invalid_values` | Pydantic validation fails → error |
| `test_reload_updates_components` | Each engine's `update_config` called |
| `test_reload_logs_changes` | Significant changes logged |

### 16.5 Snapshot Tests (`tests/test_snapshots.py`)

| Test | Description |
|---|---|
| `test_snapshot_capture` | All fields written correctly |
| `test_snapshot_history` | Returns chronological snapshots |
| `test_latest_snapshot` | Most recent snapshot returned |

### 16.6 Trigger Analytics Tests (`tests/test_trigger_analytics.py`)

| Test | Description |
|---|---|
| `test_increment_new_trigger` | Creates row with hit_count=1 |
| `test_increment_existing` | Updates hit_count and total_z_awarded |
| `test_analytics_by_date` | Returns all triggers for a date |

### 16.7 Digest Tests (`tests/test_digests.py`)

| Test | Description |
|---|---|
| `test_admin_digest_format` | Contains all required sections |
| `test_admin_digest_sent_to_admins` | Only admin-rank users receive it |
| `test_user_digest_format` | Contains personal earnings, rank, next goal |
| `test_user_digest_sent_to_active` | Only users with yesterday's activity receive it |
| `test_user_digest_disabled` | Config disabled → no sends |

### 16.8 Metrics Tests (`tests/test_metrics_full.py`)

| Test | Description |
|---|---|
| `test_metrics_counters_present` | All counter metrics in output |
| `test_metrics_gauges_present` | All gauge metrics in output |
| `test_metrics_by_channel` | Channel label on per-channel gauges |
| `test_metrics_rank_distribution` | Rank labels on distribution gauge |

---

## 17. Acceptance Criteria

### Must Pass

- [ ] All 16 admin commands functional and gated by CyTube rank ≥ `owner_level`
- [ ] `grant` credits target, logs transaction, sends PM
- [ ] `deduct` debits target with insufficient-funds check
- [ ] `rain` distributes equally among present users with public announcement
- [ ] `set_balance` hard-sets with transaction log of diff
- [ ] `set_rank` validates against configured rank names
- [ ] `reload` re-reads config, validates, applies without restart
- [ ] `reload` with invalid config retains old config and reports error
- [ ] `econ:stats` shows accounts, circulation, daily activity
- [ ] `econ:user` shows full user inspection including ban status
- [ ] `econ:health` reports inflation/deflation indicators
- [ ] `econ:triggers` identifies hot and dead triggers
- [ ] `econ:gambling` reports actual vs. configured house edge
- [ ] `approve_gif` / `reject_gif` resolve pending approvals with refund on reject
- [ ] `ban` / `unban` toggles economy access; banned users cannot earn or run user commands
- [ ] `announce` posts via `client.send_chat()`
- [ ] Economy snapshots captured every 6 hours
- [ ] Trigger analytics increment on every earning event
- [ ] Weekly admin digest sent to admin-rank users at configured hour
- [ ] User daily digest sent to active users at configured hour
- [ ] Prometheus metrics include all counters and gauges from Section 11 of master plan
- [ ] All PMs via `client.send_pm()` — zero raw NATS
- [ ] All chat via `client.send_chat()` — zero raw NATS
- [ ] All tests pass (~70 test cases)

### Stretch

- [ ] Admin command audit log (who ran what, when)
- [ ] Snapshot trend visualization via request-reply API
- [ ] Per-trigger unique user counts (exact, not approximate)

---

## Appendix: kryten-py Methods Used in This Sprint

| Method | Usage |
|---|---|
| `client.send_pm(channel, username, message)` | Admin responses, user notifications, digests |
| `client.send_chat(channel, message)` | Admin rain announcements, `announce` command |
| `client.subscribe_request_reply(subject, handler)` | Extended admin command handler |

> No direct NATS imports, no raw subject construction, no `client.publish()` with manual subjects.
