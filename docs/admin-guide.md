# Kryten Economy — Channel Administrator Guide

> **Audience:** Channel owners and moderators responsible for configuring and managing the economy bot.  
> **Version:** kryten-economy 1.x  
> **Updated:** 2026-02-26

---

## Table of Contents

1. [Overview](#1-overview)
2. [Admin Access — How It Works](#2-admin-access--how-it-works)
3. [Admin PM Commands — Quick Reference](#3-admin-pm-commands--quick-reference)
4. [Economy Control Commands](#4-economy-control-commands)
5. [Inspection & Reporting Commands](#5-inspection--reporting-commands)
6. [User Management Commands](#6-user-management-commands)
7. [Content Approval Commands](#7-content-approval-commands)
8. [Automated Reports](#8-automated-reports)
9. [Configuration Guide](#9-configuration-guide)
10. [Multiplier Events](#10-multiplier-events)
11. [Ranks & Progression](#11-ranks--progression)
12. [Economy Health Monitoring](#12-economy-health-monitoring)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Overview

The economy bot runs as a background service and participates in your CyTube channel by:

- **Rewarding presence** — users earn Z-Coins passively just for being connected.
- **Rewarding engagement** — chat activity, media reactions, and social interactions earn bonus coins.
- **Providing gambling** — slot spins, coin flips, PvP challenges, and group heists.
- **Running a shop** — users spend coins on queue slots, vanity items, tips, and more.
- **Tracking progression** — ranks, achievements, streaks, and daily competitions.
- **Running events** — scheduled multiplier events, bounties, and holiday bonuses.

All interaction with the bot happens via **PM** (private message). Users type a command, the bot replies privately.

Everything is configurable from `config.yaml`. Many settings can be applied **live** without restarting the service via the `reload` command.

---

## 2. Admin Access — How It Works

Admin commands are gated by **CyTube channel rank**. The threshold is set in `config.yaml`:

```yaml
admin:
  owner_level: 4   # CyTube rank required for admin commands (default: 4 = Owner)
```

Any user whose CyTube rank in the current channel is **greater than or equal to** `owner_level` can issue admin PM commands to the bot.

**CyTube rank levels for reference:**

| Level | Label       |
|-------|-------------|
| 0     | Anon        |
| 1     | Registered  |
| 2     | Trusted     |
| 3     | Moderator   |
| 4     | Owner       |
| 5+    | Admin/Root  |

With the default `owner_level: 4`, Owners and above can run admin commands. Set it to `3` to extend admin access to Moderators as well.

If a non-admin user attempts an admin command, they receive:
> ⛔ This command requires admin privileges.

---

## 3. Admin PM Commands — Quick Reference

Send all commands as a **PM to the bot** (the bot's username is set in `config.yaml` under `bot.username`).

| Command | Purpose |
|---------|---------|
| `grant @user <amount> [reason]` | Credit Z to a user |
| `deduct @user <amount> [reason]` | Remove Z from a user |
| `rain <amount>` | Distribute Z to all present users |
| `set_balance @user <amount>` | Hard-set a user's balance |
| `set_rank @user <rank_name>` | Override a user's economy rank |
| `announce <message>` | Post a message in public chat via the bot |
| `reload` | Hot-reload config.yaml without restarting |
| `econ:stats` | Economy overview — accounts, circulation, daily totals |
| `econ:health` | Inflation indicators, median balance, net flow |
| `econ:user <username>` | Full profile inspection for any user |
| `econ:triggers` | Trigger hit rates — active vs. dead triggers |
| `econ:gambling` | Global gambling statistics and house edge |
| `ban @user [reason]` | Suspend a user's economy access |
| `unban @user` | Restore a user's economy access |
| `approve_gif @user` | Approve a pending channel GIF purchase |
| `reject_gif @user` | Reject a pending GIF and refund the user |

---

## 4. Economy Control Commands

### `grant`

Credit Z-Coins to any user. Creates an account for the user if they don't have one yet.

```
grant @username <amount>
grant @username <amount> <reason>
```

**Examples:**

```
grant @MovieFan 500
grant @MovieFan 1000 prize for trivia night
```

The recipient is notified via PM:
> 💰 You received 500 Z from an admin. Reason: prize for trivia night

---

### `deduct`

Remove Z-Coins from a user's balance. Fails if the user would go below zero.

```
deduct @username <amount>
deduct @username <amount> <reason>
```

**Examples:**

```
deduct @TroubleUser 200
deduct @TroubleUser 200 spam penalty
```

The user is notified via PM:
> 💸 200 Z deducted by an admin. Reason: spam penalty

---

### `rain`

Distribute Z equally among **all users currently present** in the channel. Amount is split evenly; any remainder is discarded. A public chat announcement is made immediately.

```
rain <total_amount>
```

**Example:**

```
rain 1000
```

With 10 users present, each gets 100 Z. Public announcement:
> ☔ Rain! 10 users just got free Z-Coins.

**Tip:** Use `rain` for movie nights, event rewards, or just to generate buzz.

---

### `set_balance`

Hard-set a user's balance to an exact value. Use sparingly — this bypasses the normal earning/spending ledger and the adjustment is logged as `admin_set_balance`.

```
set_balance @username <amount>
```

**Example:**

```
set_balance @MovieFan 5000
```

---

### `set_rank`

Override a user's economy rank, bypassing the normal lifetime-earnings threshold. The user is notified.

```
set_rank @username <rank_name>
```

Rank names must match exactly (case-sensitive) as configured under `ranks.tiers`. Default rank names:

```
Extra · Grip · Key Grip · Gaffer · Best Boy ·
Associate Producer · Producer · Director ·
Executive Producer · Studio Mogul
```

**Example:**

```
set_rank @MovieFan Producer
```

> ⭐ Your rank has been set to **Producer** by an admin.

---

### `announce`

Post a message in **public channel chat** attributed to the bot. Useful for economy announcements, event starts, or reminders.

```
announce <message>
```

**Example:**

```
announce Tonight's movie night starts in 30 minutes — queue your picks now!
```

---

### `reload`

Hot-reload `config.yaml` without restarting the service. Validates the new config with Pydantic first — if validation fails, the old config stays active and an error is returned.

```
reload
```

**What gets updated immediately:**

- Currency name and symbol
- Earning rates and trigger rewards
- Gambling limits
- Announcement templates
- Rain settings
- Spending costs
- Presence earning rates

**Requires restart:** NATS connection settings, `channels` list, `database.path`, `metrics.port`.

---

## 5. Inspection & Reporting Commands

### `econ:stats`

Daily economy overview with totals since midnight UTC.

```
econ:stats
```

**Sample output:**

```
📊 Economy Overview:
━━━━━━━━━━━━━━━
Accounts: 142
Present: 23
Active today: 67
Circulation: 892,450 Z
━━━━━━━━━━━━━━━
Today:
  +14,320 earned
  −8,750 spent
  Gamble in: 22,000
  Gamble out: 19,800
  Net: −2,200 Z
```

**Fields explained:**

| Field | Meaning |
|-------|---------|
| Accounts | Total registered accounts in this channel |
| Present | Users currently connected |
| Active today | Users who earned or spent at least once today |
| Circulation | Sum of all current balances |
| Gamble net | Negative = house won more than it paid out (good for economy health) |

---

### `econ:health`

Inflation health check — tracks whether coins are accumulating faster than they're being spent.

```
econ:health
```

**Sample output:**

```
🏥 Economy Health:
━━━━━━━━━━━━━━━
Circ: 892,450 Z
  (+12,340 since snap)
Median: 4,820 Z
Participation: 47.1%
  (142/301)
━━━━━━━━━━━━━━━
Net Flow Today:
  +14,320 earned
  −8,750 spent
  ±−2,200 gamble
  = +3,370 Z
```

A persistently positive **Net Flow** means coins are entering the economy faster than they leave. Consider:
- Increasing spending costs
- Enabling balance `decay` mode
- Reducing passive earning rates

A persistently negative Net Flow can discourage participation. Consider:
- Adding a promotion or rain event
- Lowering queue costs
- Running a multiplier event

---

### `econ:user`

Full account inspection for any user.

```
econ:user <username>
econ:user @username
```

**Sample output:**

```
👤 MovieFan
━━━━━━━━━━━━━━━

Balance: 12,450 Z
Lifetime earned: 48,320 Z
Lifetime spent: 35,870 Z

Rank: Key Grip
Achievements: 7
Banned: No

Created: 2025-11-03
Last seen: 2026-02-25
Gambling: 412 games, net −1,240 Z
```

---

### `econ:triggers`

Shows trigger hit rates for today — useful for identifying which earning mechanics are working and which are dead.

```
econ:triggers
```

**Sample output:**

```
📊 Triggers (Today):
━━━━━━━━━━━━━━━
long_message
  247 hits · 34 users · 247 Z
first_message_of_day
  102 hits · 102 users · 510 Z
comment_during_media
  88 hits · 21 users · 44 Z
...
⚠️ Dead triggers (0 hits today): conversation_starter, night_watch
```

Dead triggers may indicate the trigger is misconfigured, too hard to achieve, or legitimately never fired (e.g. `night_watch` during a daytime session).

---

### `econ:gambling`

Global gambling statistics including actual vs. configured house edge.

```
econ:gambling
```

**Sample output:**

```
🎰 Gambling Report:
━━━━━━━━━━━━━━━
Wagered: 1,240,000 Z
Paid out: 1,178,000 Z
House: 62,000 Z
━━━━━━━━━━━━━━━
Edge: 5.0%
Cfg edge: 4.8%
Gamblers: 89
Games: 8,420
```

If **actual edge** is significantly higher than **configured edge**, gamblers are running unlucky (variance). Over time these should converge. A sustained large gap may indicate a configuration problem.

---

## 6. User Management Commands

### `ban`

Suspends a user's access to all economy features — they cannot earn, spend, gamble, or tip. They are notified via PM.

```
ban @username
ban @username <reason>
```

**Example:**

```
ban @TroubleUser exploiting a loop
```

Banned users receive a PM:
> ⛔ Your economy access has been suspended. Reason: exploiting a loop

If a banned user sends any non-admin command, the bot silently returns the suspension message. Their balance and history are preserved.

---

### `unban`

Restores a banned user's economy access. They are notified via PM.

```
unban @username
```

> ✅ Your economy access has been restored.

---

## 7. Content Approval Commands

When a user purchases a **channel GIF** (a personalised animated GIF attached to their account), the purchase is held pending admin approval before it takes effect. You receive no automatic notification — use `econ:user` to check for pending items or look for PMs from users asking for approval.

### `approve_gif`

Approve a pending GIF purchase. The user is notified.

```
approve_gif @username
```

> ✅ Your channel GIF has been approved by {admin}!

### `reject_gif`

Reject a pending GIF and **automatically refund** the full purchase amount to the user.

```
reject_gif @username
```

> ❌ Your channel GIF was rejected by {admin}. Your 5,000 Z have been refunded.

**Configuration for channel GIF:**

```yaml
vanity_shop:
  channel_gif:
    enabled: true
    cost: 5000
    requires_admin_approval: true   # Set false to auto-approve
```

---

## 8. Automated Reports

The bot sends two scheduled digests every day.

### User Daily Digest

Sent via PM to each **active economy user** at the configured UTC hour. Shows their personal daily summary.

```yaml
digest:
  user_digest:
    enabled: true
    send_hour_utc: 4
    message: |
      📊 Daily Summary:
      Earned: {earned} {currency} | Spent: {spent} | Balance: {balance}
      Rank: {rank} | Streak: {streak} days 🔥
      Next goal: {next_goal_description} ({days_away} days away)
```

### Admin Daily Digest

Sent via PM to all users with CyTube rank ≥ `owner_level` at the configured UTC hour. Contains an economy-wide health snapshot — the same data as `econ:health` plus weekly totals.

```yaml
digest:
  admin_digest:
    enabled: true
    send_hour_utc: 5
```

To disable either digest:

```yaml
digest:
  user_digest:
    enabled: false
  admin_digest:
    enabled: false
```

---

## 9. Configuration Guide

All settings live in `config.yaml`. After editing, send `reload` to the bot to apply most changes without a restart.

### Currency Identity

```yaml
currency:
  name: "Z-Coin"
  symbol: "Z"
  plural: "Z-Coins"
```

Users see `name` and `plural` in messages. `symbol` is used for compact display (e.g. `500 Z`). Change all three together for consistency.

---

### Onboarding

Controls what new users receive when they first interact.

```yaml
onboarding:
  welcome_wallet: 100            # Starting balance for new accounts
  welcome_message: >
    Welcome! You've got {amount} {currency}. ...
  min_account_age_minutes: 0    # Require account age before earning starts
  min_messages_to_earn: 0       # Require N messages before earning starts
```

Setting `min_account_age_minutes: 60` prevents new accounts from immediately gambling.

---

### Presence Earning

```yaml
presence:
  base_rate_per_minute: 1        # Everyone earns this just for being connected
  active_bonus_per_minute: 0     # Extra bonus for non-AFK users (0 = no bonus for activity)
  afk_threshold_minutes: 5       # Minutes of chat silence before marked AFK
  join_debounce_minutes: 5       # CyTube bounces don't trigger re-join actions
  greeting_absence_minutes: 30   # Minimum absence for a custom greeting to fire
```

**Hourly dwell milestones** — reward sustained presence:

```yaml
presence:
  hourly_milestones:
    1: 10     # 1 hour  → 10 Z bonus
    3: 30     # 3 hours → 30 Z bonus
    6: 75
    12: 200
    24: 1000
```

**Night watch** — bonus multiplier during off-peak hours (rewards the tab-leavers):

```yaml
presence:
  night_watch:
    enabled: true
    hours: [2, 3, 4, 5, 6, 7]   # UTC hours
    multiplier: 1.5
```

---

### Rain Drops

Periodic random Z bonus distributed to all connected users:

```yaml
rain:
  enabled: true
  interval_minutes: 45     # Average interval (randomised ±30%)
  min_amount: 5
  max_amount: 25
  pm_notification: true    # Whether each user gets a PM
```

Admins can also trigger rain on-demand with the `rain` command. The scheduled rain is separate from admin rain — both work independently.

---

### Gambling

```yaml
gambling:
  enabled: true
  min_account_age_minutes: 60   # Prevent brand-new accounts from gambling

  spin:
    min_wager: 10
    max_wager: 500
    cooldown_seconds: 30
    daily_limit: 50             # Max spins per user per day
    announce_jackpots_public: true
    jackpot_announce_threshold: 500   # Only announce jackpots ≥ this amount

  flip:
    min_wager: 10
    max_wager: 1000
    win_chance: 0.45            # House has 55% to win
    cooldown_seconds: 15
    daily_limit: 100

  challenge:
    min_wager: 50
    max_wager: 5000
    rake_percent: 5             # House takes 5% of the pot
    announce_public: true

  heist:
    enabled: false              # Off by default — enable for events
    min_participants: 3
    success_chance: 0.40
    payout_multiplier: 1.5
```

**Slot machine payouts** — probabilities must sum to ≤ 1.0 (the deficit is the base house edge):

```yaml
spin:
  payouts:
    - { symbols: "🍒🍒🍒", multiplier: 3,  probability: 0.10 }
    - { symbols: "🍋🍋🍋", multiplier: 5,  probability: 0.05 }
    - { symbols: "💎💎💎", multiplier: 10, probability: 0.02 }
    - { symbols: "7️⃣7️⃣7️⃣",  multiplier: 50, probability: 0.002 }
    - { symbols: "partial",  multiplier: 2,  probability: 0.15 }
    - { symbols: "loss",     multiplier: 0,  probability: 0.678 }
```

To calculate expected value: $EV = \sum (multiplier \times probability)$. With the defaults, $EV \approx 0.952$ — a ~4.8% house edge.

---

### Queue & Spending

Control what users can spend coins on:

```yaml
spending:
  queue_tiers:
    - { max_minutes: 15,  label: "Short / Music Video", cost: 250  }
    - { max_minutes: 35,  label: "30-min Episode",      cost: 500  }
    - { max_minutes: 65,  label: "60-min Episode",      cost: 750  }
    - { max_minutes: 999, label: "Movie",               cost: 1000 }

  interrupt_play_next:  10000    # Cost to skip to top of queue
  force_play_now:       100000   # Cost to immediately play (very expensive)
  force_play_requires_admin: true  # Admin must also approve force plays

  max_queues_per_day: 3          # Queue slots per user per day
  queue_cooldown_minutes: 30     # Must wait this long between queues

  blackout_windows: []           # Time windows when queuing is disabled
```

**Blackout window example** (disable queuing during curated movie nights):

```yaml
spending:
  blackout_windows:
    - name: "Movie Night"
      cron: "0 20 * * 5"    # Every Friday at 20:00 UTC
      duration_hours: 4
```

---

### Balance Maintenance

Choose between interest (inflation, rewards savers) or decay (deflation, penalises hoarders):

```yaml
balance_maintenance:
  mode: "interest"    # "interest" | "decay" | "none"

  interest:
    daily_rate: 0.001          # 0.1% per day
    max_daily_interest: 10     # Cap per user per day
    min_balance_to_earn: 100   # Must have at least this to earn interest

  decay:
    enabled: false
    daily_rate: 0.005          # 0.5% per day
    exempt_below: 50000        # Don't decay balances below this
    label: "Vault maintenance fee"
```

---

### Announcements

Control which economy events are announced in public chat, and customise the message templates:

```yaml
announcements:
  queue_purchase: true
  gambling_jackpot: true
  jackpot_min_amount: 500      # Only announce jackpots ≥ this
  achievement_milestone: true
  rank_promotion: true
  challenge_result: true
  heist_result: true
  rain_drop: true
  daily_champion: true
  streak_milestone: true

  templates:
    queue:         '🎬 {user} just queued "{title}"! ({cost} {currency})'
    jackpot:       "🎰 JACKPOT! {user} just won {amount} {currency}!"
    rank_up:       "⭐ {user} is now a {rank}!"
    streak:        "🔥 {user} hit a {days}-day streak!"
    rain:          "☔ Rain! {count} users just got free {currency}."
    challenge_win: "⚔️ {winner} defeated {loser} and won {amount} {currency}!"
```

---

### Ignored Users

Other bots and service accounts should be excluded from all economy activity:

```yaml
ignored_users:
  - "CyTubeBot"
  - "NepBot"
```

Matching is case-insensitive. Ignored users do not earn, are excluded from rain, and do not count as "present" for population-based events.

---

### PM Rate Limiting

Prevents users from spamming the bot:

```yaml
commands:
  rate_limit_per_minute: 10
```

A user who exceeds this receives:
> ⏳ Slow down! Try again in a moment.

---

## 10. Multiplier Events

Multipliers increase earning rates for all users during a window. They stack additively when multiple are active.

### Off-Peak Multiplier

Rewards engagement during quiet periods:

```yaml
multipliers:
  off_peak:
    enabled: true
    days: [1, 2, 3, 4]                                   # Mon-Thu (0=Sun)
    hours: [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]          # UTC
    multiplier: 2.0
    announce: true
```

### High-Population Multiplier

Automatically activates when the channel has many active users:

```yaml
multipliers:
  high_population:
    enabled: true
    min_users: 10
    multiplier: 1.5
    hidden: true    # true = activates silently
```

### Holiday Multipliers

```yaml
multipliers:
  holidays:
    enabled: true
    dates:
      - { date: "12-25", name: "Christmas", multiplier: 3.0 }
      - { date: "10-31", name: "Halloween", multiplier: 2.0 }
    announce: true
```

### Scheduled Events

Recurring time-boxed events with a cron schedule:

```yaml
multipliers:
  scheduled_events:
    - name: "Weird Wednesday"
      cron: "0 20 * * 3"       # Every Wednesday at 20:00 UTC
      duration_hours: 4
      multiplier: 2.0
      presence_bonus: 500       # Flat Z bonus for being present at start
      announce: true
```

Admins can also trigger one-off events via the `event` PM command:

```
event start "Double Coins" 2 2.0
```

*(name, duration_hours, multiplier)*

---

## 11. Ranks & Progression

Ranks are based on **lifetime earned** (total Z ever credited, not current balance). Users cannot lose rank by spending.

```yaml
ranks:
  earn_multiplier_per_rank: 0.0    # Extra earn % per rank tier (0 = disabled)
  spend_discount_per_rank: 0.02    # 2% spending discount per rank tier

  tiers:
    - name: "Extra"
      min_lifetime_earned: 0

    - name: "Grip"
      min_lifetime_earned: 1000
      perks: ["1 free daily fortune"]

    - name: "Studio Mogul"
      min_lifetime_earned: 5000000
      perks: ["20% discount", "legendary status"]
      cytube_level_promotion: 2    # Auto-promotes to CyTube rank 2
```

The `cytube_level_promotion` field triggers an automatic CyTube rank promotion when a user reaches that economy tier — use for granting Trusted status to top community members.

Users can also **purchase** a CyTube promotion:

```yaml
cytube_promotion:
  enabled: true
  purchasable: true
  cost: 50000
  min_rank: "Associate Producer"   # Economy rank gate
```

---

## 12. Economy Health Monitoring

### Prometheus Metrics

The service exposes Prometheus metrics at `http://localhost:<port>/metrics` (port configured under `metrics.port`, default `28286`).

Key metrics to watch:

| Metric | What it tells you |
|--------|-------------------|
| `economy_z_earned_total` | Total Z created |
| `economy_z_spent_total` | Total Z destroyed via spending |
| `economy_gambling_wagered_total` | Total Z wagered |
| `economy_gambling_payout_total` | Total Z paid out by gambling |
| `economy_active_users` | Current session active users |
| `economy_events_processed_total` | Total NATS events handled |
| `economy_uptime_seconds` | Service uptime |
| `economy_nats_connected` | 1 if NATS is connected, 0 if not |

### Snapshot History

The bot takes periodic economy snapshots stored in the database (`economy_snapshots` table). The `econ:health` command shows you the delta since the last snapshot:

```
Circ: 892,450 Z
  (+12,340 since snap)
```

A rapidly increasing "since snap" delta means coins are accumulating faster than snaps are being taken, or faster than they're leaving the economy.

### Trigger Analytics

The `trigger_analytics` table records per-trigger hit counts, unique users, and total Z awarded — per day. Query this directly for trend analysis:

```sql
SELECT trigger_id, SUM(hit_count), SUM(total_z_awarded)
FROM trigger_analytics
WHERE channel = 'mychannel'
  AND date >= date('now', '-7 days')
GROUP BY trigger_id
ORDER BY SUM(total_z_awarded) DESC;
```

---

## 13. Troubleshooting

### Bot is not responding to PMs

1. Confirm the service is running: `systemctl status kryten-economy` (or equivalent)
2. Check the Prometheus health endpoint: `curl http://localhost:28286/health`
3. Check logs for NATS connection errors
4. Verify `bot.username` in config matches the actual bot account in CyTube

### My admin commands aren't working

1. Check your CyTube rank in the channel — it must be ≥ `admin.owner_level` (default 4)
2. The rank check uses the rank at the time the PM is sent; if you just changed your rank, wait for it to propagate
3. Try `econ:stats` first — if it works, your rank is fine

### Config reload failed

The `reload` command validates the entire config file before applying it. If validation fails the old config remains active and the error is returned in the PM. Common causes:
- YAML syntax error (indentation, missing quotes)
- Invalid value type (e.g. string where an int is expected)
- Unknown field name

Run `python -m kryten_economy --validate-config --config config.yaml` locally to check before reloading on a live service.

### A user got suspended and shouldn't have

Use `unban @username` to restore access immediately. Bans are stored in the `banned_users` database table — they persist across restarts.

### Gambling house edge is way off

Run `econ:gambling`. If actual edge diverges significantly from configured edge, it's usually variance (especially under 5,000 games). Only investigate a config problem if the divergence persists above ~50,000 games. Also verify your `spin.payouts` probabilities sum to ≤ 1.0.

### Rain distributed nothing / wrong amount

`rain <total>` splits the total evenly across present users. If 0 users are present the command returns an error. The per-user amount is floored (`total // users`), so small rains with many users may result in 1 Z each or nothing for remainder users.

### Digests aren't being sent

1. Confirm `digest.user_digest.enabled: true` and `digest.admin_digest.enabled: true`
2. Verify `send_hour_utc` matches an hour that actually passes (check timezone)
3. The admin digest only goes to users with rank ≥ `owner_level` who have economy accounts — you need an account in the channel for the bot to PM you

---

*For service deployment, NATS configuration, and systemd setup, see the README.*  
*For Prometheus metric definitions, see `kryten_economy/metrics_server.py`.*  
*For database schema, see `kryten_economy/database.py`.*
