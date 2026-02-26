# `kryten-economy` — Implementation Plan

> **Version:** 1.0 · **Date:** 2026-02-24 · **Status:** Approved for Sprint Spec Generation
>
> This is the authoritative implementation plan for the `kryten-economy` microservice. It is structured as sequential sprints, each producing a deployable increment. Each sprint section is written to be consumed by an AI coding agent to generate actionable implementation specs. This document supersedes the original pre-requirements prompt.

---

## Table of Contents

1. [Module Goals](#1-module-goals)
2. [Architecture Overview](#2-architecture-overview)
3. [Database Schema](#3-database-schema)
4. [Configuration Schema (YAML)](#4-configuration-schema-yaml)
5. [Currency, Ranks & Progression](#5-currency-ranks--progression)
6. [Earning Mechanisms](#6-earning-mechanisms)
7. [Spending Mechanisms](#7-spending-mechanisms)
8. [Gambling](#8-gambling)
9. [Anti-Abuse & Economy Health](#9-anti-abuse--economy-health)
10. [Bot Interface](#10-bot-interface)
11. [Admin Tooling & Reporting](#11-admin-tooling--reporting)
12. [Sprint Plan](#12-sprint-plan)
13. [Sprint Dependency Graph](#13-sprint-dependency-graph)
14. [Open Questions & Pre-Sprint-1 Decisions](#14-open-questions--pre-sprint-1-decisions)

---

## 1. Module Goals

1. **Maximize dwell time** — reward users for staying connected. AFK is valuable. Every connected body improves channel-list positioning. The economy should never punish idle presence.
2. **Democratize the queue** — engaged users can add content from the MediaCMS catalog via PM, within rules we control.
3. **Drive user pick-up** — make the economy visible enough that non-participants become curious, through public announcements and social proof.
4. **Retain and reward** — streaks, achievements, named ranks, and visible progression create identity and reasons to return.
5. **Create joy** — gambling, vanity perks, social moments (challenges, tips, bounties), and discovery-based hidden triggers make the system fun and optionally obsessive.
6. **Keep it playful and mysterious** — no public rulebook; users discover what earns and what costs through experimentation. A partial `rewards` command reveals basics without spoiling hidden triggers.
7. **Full configurability** — every rate, threshold, cooldown, reward, cost, trigger, rank name, and announcement template is config-driven in YAML with extensive inline comments. No code deployments to tune the economy.
8. **Zero upstream risk** — no changes to CyTube, Kryten-Robot, or kryten-py. Pure microservice. All interaction via kryten-py's abstraction layer (`KrytenClient` event handlers, helper methods, KV accessors). **No direct NATS access.**

---

## 2. Architecture Overview

### Service Identity

| Property | Value |
|---|---|
| Service name | `economy` (normalized per kryten-py conventions) |
| NATS command subject | `kryten.economy.command` |
| Metrics port | `28286` (next available after userstats:28282, moderator:28284) |
| Persistence | SQLite (following kryten-userstats pattern) |
| Config format | **YAML** with extensive inline comments (`config.yaml`) |
| Bot account | Configurable username; interacts via PM only for user commands |
| Python version | 3.11+ |
| Framework | `kryten-py >= 0.11.5` |

### Package Structure

```
kryten-economy/
├── kryten_economy/
│   ├── __init__.py                # Version, exports
│   ├── __main__.py                # CLI entry point, signal handling
│   ├── config.py                  # EconomyConfig (Pydantic, extends KrytenConfig pattern)
│   ├── main.py                    # EconomyApp orchestrator
│   ├── database.py                # EconomyDatabase (SQLite, async via run_in_executor)
│   ├── command_handler.py         # Request-reply on kryten.economy.command (via client.subscribe_request_reply)
│   ├── pm_handler.py              # PM command parser and dispatcher
│   ├── earning_engine.py          # All earning trigger evaluation
│   ├── spending_engine.py         # All spend action processing
│   ├── gambling_engine.py         # Slots, flip, challenge, heist
│   ├── achievement_engine.py      # Milestone tracking and badge awards
│   ├── rank_engine.py             # Named ranks, CyTube tier sync, discount calculation
│   ├── presence_tracker.py        # Dwell time, AFK, hourly milestones, streaks, join debounce
│   ├── event_announcer.py         # Public chat announcements (configurable)
│   ├── media_client.py            # MediaCMS HTTP API wrapper
│   ├── scheduler.py               # Periodic tasks: rain drops, digests, daily resets, competitions
│   ├── metrics_server.py          # Prometheus HTTP endpoint
│   └── utils.py                   # Shared helpers (alias resolution, time windows, etc.)
├── tests/
│   ├── conftest.py
│   ├── test_database.py
│   ├── test_earning_engine.py
│   ├── test_spending_engine.py
│   ├── test_gambling_engine.py
│   ├── test_achievement_engine.py
│   ├── test_rank_engine.py
│   ├── test_presence_tracker.py
│   ├── test_pm_handler.py
│   └── test_scheduler.py
├── config.example.yaml            # Fully commented reference config
├── pyproject.toml
├── README.md
└── systemd/
    └── kryten-economy.service
```

### Event Subscriptions (via `@client.on()` decorators)

| Event | Source | Used For |
|---|---|---|
| `chatmsg` | kryten-robot | Chat earning triggers, content engagement, kudos, laughs, emotes, GIF detection |
| `adduser` | kryten-robot | Presence tracking, join debounce, newcomer greeting detection, welcome wallet |
| `userleave` | kryten-robot | Session end, activity time calculation, departure timestamp for debounce |
| `changemedia` | kryten-robot | Media completion detection, first-to-comment tracking, event start detection |
| `pm` | kryten-robot | User commands (balance, search, queue, spin, tip, etc.) |
| `setafk` | kryten-robot | AFK status updates (supplement to chat-recency detection) |

### Outbound Interaction (via kryten-py wrappers)

| Action | kryten-py Method | Purpose |
|---|---|---|
| Expose API | `client.subscribe_request_reply("kryten.economy.command", handler)` | API for other services and admin tools |
| Public chat | `await client.send_chat(channel, message, *, domain=None) -> str` | Public channel announcements |
| PM responses | `await client.send_pm(channel, username, message, *, domain=None) -> str` | PM responses to users |
| Queue media | `await client.add_media(channel, media_type, media_id, *, position="end", temp=True, domain=None) -> str` | Queue content from MediaCMS |
| Lifecycle | Automatic via `service` config | Startup/shutdown/heartbeat |
| KV read | `await client.kv_get(bucket_name, key)` | Read robot state (userlist, playlist, emotes) |
| KV write | `await client.kv_put(bucket_name, key, value)` | Persist economy state |
| NATS subscribe | `client.subscribe(subject, handler)` | Lifecycle events, inter-service signals |
| NATS request | `await client.nats_request(subject, request, timeout)` | Inter-service queries (e.g., alias resolution) |
| Group restart | `client.on_group_restart(callback)` | Coordinated restart handling |

> **⚠️ Ecosystem rule:** All NATS interaction MUST go through kryten-py's `KrytenClient` methods. Never import `nats` directly, never construct NATS subjects manually, never call `nats_client.publish/subscribe` directly. kryten-py owns the connection lifecycle, subscription tracking, JSON serialization, and error handling.

### Service Startup Sequence

Follows the canonical kryten-py microservice pattern (see kryten-moderator for reference):

1. Load and validate YAML config → Pydantic `EconomyConfig`
2. Initialize SQLite database, create tables
3. Initialize domain components (presence tracker, earning engine, etc.)
4. Create `KrytenClient` with extracted base config
5. Register all event handlers (`@client.on(...)`) before connect
6. Connect to NATS (lifecycle auto-managed by kryten-py)
7. Subscribe to `kryten.lifecycle.robot.startup` via `client.subscribe()` for re-initialization
8. Load initial state from KV stores (userlist, emotes, playlist)
9. Start Prometheus metrics server
10. Initialize NATS request-reply command handler
11. Start periodic scheduler tasks (rain, snapshots, digests, competition eval)
12. Block on `client.run()` until shutdown signal

### CyTube Rank Model

| Level | CyTube Name | Current Use | Economy Use |
|---|---|---|---|
| 0 | Guest | Non-authenticated | No economy participation |
| 1 | User | Standard registered user | Base earn/spend rates |
| 2 | Moderator | **Currently unused** | **Vanity "promoted" tier**: cosmetic rank, configurable earn multiplier, spend discounts, CSS-targetable class (`.userlist_mod`) |
| 3 | Admin | Reserved for future non-board staff | Staff rates (configurable) |
| 4 | Owner | Board members, channel operators | **Full admin commands**: grant, rain, config reload, economy stats, user inspection |
| 5 | Founder | System (never logged in) | N/A |

Promoting a user to CyTube level 2 is a **spend action or achievement reward** within the economy. The channel should be configured to limit actual moderation powers at level 2, making it purely cosmetic. The CSS class `.userlist_mod` enables visual distinction in the channel UI.

---

## 3. Database Schema

All tables shown below. Each sprint implements only the tables it needs.

```sql
-- ═══════════════════════════════════════════
-- Sprint 1: Core Foundation
-- ═══════════════════════════════════════════

-- Primary user account and balance storage
CREATE TABLE accounts (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    balance INTEGER DEFAULT 0,
    lifetime_earned INTEGER DEFAULT 0,
    lifetime_spent INTEGER DEFAULT 0,
    lifetime_gambled_in INTEGER DEFAULT 0,
    lifetime_gambled_out INTEGER DEFAULT 0,
    rank_name TEXT DEFAULT 'Extra',
    cytube_level INTEGER DEFAULT 1,
    chat_color TEXT,               -- Hex from approved palette, NULL = default
    custom_greeting TEXT,          -- NULL = none
    custom_title TEXT,             -- NULL = none
    channel_gif_url TEXT,          -- NULL = none, requires admin approval
    channel_gif_approved BOOLEAN DEFAULT 0,
    personal_currency_name TEXT,   -- NULL = use global name
    welcome_wallet_claimed BOOLEAN DEFAULT 0,
    economy_banned BOOLEAN DEFAULT 0,  -- Excluded from earning/spending, keeps balance
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP,         -- Last chat message timestamp
    UNIQUE(username, channel)
);

-- All balance-changing events (immutable audit log)
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount INTEGER NOT NULL,        -- Positive = credit, negative = debit
    type TEXT NOT NULL,             -- earn, spend, gamble_win, gamble_loss, tip_in, tip_out,
                                   -- admin_grant, admin_deduct, rain, decay, interest,
                                   -- welcome_wallet, welcome_back, streak_bonus, milestone,
                                   -- achievement, competition, event_bonus
    reason TEXT,                    -- Human-readable trigger name / description
    trigger_id TEXT,                -- Config trigger ID for analytics
    related_user TEXT,              -- For tips, challenges
    metadata TEXT,                  -- JSON blob for extra context (multiplier applied, etc.)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-user per-day activity summary (for daily competitions and digest)
CREATE TABLE daily_activity (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    date TEXT NOT NULL,             -- YYYY-MM-DD
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

-- ═══════════════════════════════════════════
-- Sprint 2: Streaks, Milestones & Dwell
-- ═══════════════════════════════════════════

-- Login streak and weekend-weekday bridge tracking
CREATE TABLE streaks (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    current_daily_streak INTEGER DEFAULT 0,
    longest_daily_streak INTEGER DEFAULT 0,
    last_streak_date TEXT,          -- YYYY-MM-DD of last qualifying day
    weekend_seen_this_week BOOLEAN DEFAULT 0,
    weekday_seen_this_week BOOLEAN DEFAULT 0,
    bridge_claimed_this_week BOOLEAN DEFAULT 0,
    week_number TEXT,               -- ISO week (e.g. "2026-W08") for weekly reset
    UNIQUE(username, channel)
);

-- Hourly dwell milestone tracking (per day)
CREATE TABLE hourly_milestones (
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

-- Per-trigger cooldown tracking (anti-abuse)
CREATE TABLE trigger_cooldowns (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    window_start TIMESTAMP,
    UNIQUE(username, channel, trigger_id)
);

-- ═══════════════════════════════════════════
-- Sprint 4: Gambling
-- ═══════════════════════════════════════════

-- Aggregate gambling statistics per user
CREATE TABLE gambling_stats (
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    total_spins INTEGER DEFAULT 0,
    total_flips INTEGER DEFAULT 0,
    total_challenges INTEGER DEFAULT 0,
    total_heists INTEGER DEFAULT 0,
    biggest_win INTEGER DEFAULT 0,
    biggest_loss INTEGER DEFAULT 0,
    net_gambling INTEGER DEFAULT 0,  -- Lifetime net (can be negative)
    UNIQUE(username, channel)
);

-- Active challenges awaiting acceptance (ephemeral, cleaned on timeout)
CREATE TABLE pending_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenger TEXT NOT NULL,
    target TEXT NOT NULL,
    channel TEXT NOT NULL,
    wager INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'pending'   -- pending, accepted, declined, expired
);

-- ═══════════════════════════════════════════
-- Sprint 5: Spending & Tips
-- ═══════════════════════════════════════════

-- Tip audit trail (supplements transactions table for social analytics)
CREATE TABLE tip_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Pending admin approvals (channel GIFs, force-play requests)
CREATE TABLE pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    type TEXT NOT NULL,             -- channel_gif, force_play
    data TEXT NOT NULL,             -- JSON blob (gif_url, video_id, etc.)
    cost INTEGER NOT NULL,          -- Z charged (refunded if rejected)
    status TEXT DEFAULT 'pending',  -- pending, approved, rejected
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_by TEXT,
    resolved_at TIMESTAMP
);

-- ═══════════════════════════════════════════
-- Sprint 6: Achievements & Ranks
-- ═══════════════════════════════════════════

-- Awarded achievements (one-time per user per achievement)
CREATE TABLE achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    channel TEXT NOT NULL,
    achievement_id TEXT NOT NULL,    -- From config
    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(username, channel, achievement_id)
);

-- ═══════════════════════════════════════════
-- Sprint 7: Bounties & Events
-- ═══════════════════════════════════════════

-- User-created bounties
CREATE TABLE bounties (
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

-- ═══════════════════════════════════════════
-- Sprint 8: Reporting & Analytics
-- ═══════════════════════════════════════════

-- Periodic economy health snapshots
CREATE TABLE economy_snapshots (
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

-- Per-trigger per-day analytics
CREATE TABLE trigger_analytics (
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

---

## 4. Configuration Schema (YAML)

The full `config.example.yaml` will be generated during Sprint 1 with extensive inline comments explaining every field. Below is the complete structural reference.

```yaml
# ══════════════════════════════════════════════════════════════
#  kryten-economy — Configuration Reference
# ══════════════════════════════════════════════════════════════
# All values shown are defaults. Every rate, threshold, reward,
# cost, and behavior is tunable here without code changes.
# ══════════════════════════════════════════════════════════════

# ── NATS Connection (standard kryten-py) ─────────────────────
nats:
  servers: ["nats://localhost:4222"]

# ── Channel(s) to operate in ─────────────────────────────────
channels:
  - domain: cytu.be
    channel: mychannel

# ── Service Identity (standard kryten-py) ────────────────────
service:
  name: economy
  version: "1.0.0"
  enable_lifecycle: true
  enable_heartbeat: true
  heartbeat_interval: 30

# ── Persistence ──────────────────────────────────────────────
database:
  path: economy.db

# ── Currency Identity ────────────────────────────────────────
currency:
  name: "Z-Coin"
  symbol: "Z"
  plural: "Z-Coins"

# ── Bot Account ──────────────────────────────────────────────
bot:
  username: "ZCoinBot"

# ── Ignored Users ────────────────────────────────────────────
# Usernames listed here are completely invisible to the economy.
# They do not earn, do not count as "present" for rain/events,
# do not trigger social rewards (e.g. greeting a newcomer),
# and are excluded from "first message" / "first after media"
# type triggers. Use this for other bots in the channel.
ignored_users:
  - "CyTubeBot"
  - "NepBot"
  # Add any other bot accounts or service accounts here.
  # Matching is case-insensitive.

# ── Onboarding ───────────────────────────────────────────────
onboarding:
  welcome_wallet: 100
  welcome_message: >
    Welcome! You've got {amount} {currency}. 
    Stick around and you'll earn more. Try 'help' to see what you can do.
  min_account_age_minutes: 0
  min_messages_to_earn: 0

# ── Presence Earning ─────────────────────────────────────────
# DESIGN PRINCIPLE: Base presence rate is equal for AFK and active.
# Active chatting earns BONUS Z on top. We never punish idling.
presence:
  base_rate_per_minute: 1       # Everyone earns this just for being connected
  active_bonus_per_minute: 0    # Additional bonus for non-AFK users (0 = equal rates)
  afk_threshold_minutes: 5     # Minutes without chat to be considered AFK

  # Join debounce — protects against CyTube WebSocket instability.
  # CyTube's WS connections are unstable; rapid disconnect/reconnect cycles
  # ("bouncing") should not trigger join-based actions.
  join_debounce_minutes: 5       # Minimum absence before a join counts as a "real" arrival
  greeting_absence_minutes: 30   # Custom greetings require this much absence (session-level)

  # Hourly dwell milestones (cumulative minutes within a calendar day)
  # These reward sustained presence with escalating bonuses.
  hourly_milestones:
    1: 10       # 1 hour  → 10 Z
    3: 30       # 3 hours → 30 Z
    6: 75       # 6 hours → 75 Z
    12: 200     # 12 hours → 200 Z
    24: 1000    # 24 hours → 1,000 Z (aspirational, social flex)

  # Night watch: bonus multiplier during configured off-peak hours.
  # Rewards people who leave a tab open overnight.
  night_watch:
    enabled: true
    hours: [2, 3, 4, 5, 6, 7]   # 24h format, UTC
    multiplier: 1.5

# ── Streaks ──────────────────────────────────────────────────
streaks:
  daily:
    enabled: true
    min_presence_minutes: 15     # Must be present this long to count a day
    rewards:
      2: 10
      3: 20
      4: 30
      5: 50
      6: 75
      7: 100
    milestone_7_bonus: 200       # On top of daily streak rewards
    milestone_30_bonus: 2000

  weekend_weekday_bridge:
    enabled: true
    bonus: 500
    announce_on_weekend: true
    message: "Connect any weekday this week for a {amount} {currency} bridge bonus!"

# ── Chat Earning Triggers ────────────────────────────────────
# Active chat earns BONUS Z on top of passive presence.
# Each trigger has: enabled, reward, cooldown/cap, hidden flag.
chat_triggers:
  long_message:
    enabled: true
    min_chars: 30
    reward: 1
    max_per_hour: 30
    hidden: true

  laugh_received:
    enabled: true
    reward_per_laugher: 2
    max_laughers_per_joke: 10
    self_excluded: true
    hidden: true

  kudos_received:
    enabled: true
    reward: 3
    self_excluded: true          # Alias-aware via kryten-userstats
    hidden: true

  first_message_of_day:
    enabled: true
    reward: 5
    hidden: true

  conversation_starter:
    enabled: true
    min_silence_minutes: 10
    reward: 10
    hidden: true

  first_after_media_change:
    enabled: true
    window_seconds: 30
    reward: 3
    hidden: true

# ── Content Engagement ───────────────────────────────────────
content_triggers:
  comment_during_media:
    enabled: true
    reward_per_message: 0.5
    max_per_item_base: 10        # Base cap; scales with duration if enabled
    scale_with_duration: true    # Cap = base × (duration_minutes / 30), min = base
    hidden: true

  like_current:
    enabled: true
    reward: 2
    hidden: true

  survived_full_media:
    enabled: true
    min_presence_percent: 80
    reward: 5
    hidden: true

  present_at_event_start:
    enabled: true
    default_reward: 100          # Split among present users
    hidden: true

# ── Social Triggers ──────────────────────────────────────────
social_triggers:
  greeted_newcomer:
    enabled: true
    window_seconds: 60
    reward: 3
    bot_joins_excluded: true    # Joins by users in ignored_users list never trigger this
    hidden: true

  mentioned_by_other:
    enabled: true
    reward: 1
    max_per_hour_same_user: 5
    hidden: true

  bot_interaction:
    enabled: true
    reward: 2
    max_per_day: 10
    hidden: true

# ── Achievements (One-Time Milestone Bonuses) ────────────────
achievements:
  - id: messages_100
    description: "Sent 100 messages all-time"
    condition: { type: lifetime_messages, threshold: 100 }
    reward: 50
    hidden: true

  - id: messages_1000
    description: "Sent 1,000 messages all-time"
    condition: { type: lifetime_messages, threshold: 1000 }
    reward: 500
    hidden: true

  - id: presence_24h
    description: "24 hours cumulative presence"
    condition: { type: lifetime_presence_hours, threshold: 24 }
    reward: 100
    hidden: true

  - id: streak_7
    description: "7-day login streak"
    condition: { type: daily_streak, threshold: 7 }
    reward: 200
    hidden: true

  - id: streak_30
    description: "30-day login streak"
    condition: { type: daily_streak, threshold: 30 }
    reward: 2000
    hidden: true

  - id: tipped_10_unique
    description: "Tipped 10 different users"
    condition: { type: unique_tip_recipients, threshold: 10 }
    reward: 100
    hidden: true

  - id: received_tips_10
    description: "Received tips from 10 different users"
    condition: { type: unique_tip_senders, threshold: 10 }
    reward: 100
    hidden: true

  # Additional achievement condition types available:
  # lifetime_earned, lifetime_spent, lifetime_gambled,
  # gambling_biggest_win, rank_reached, unique_emotes_used_lifetime

# ── Competitive Daily Awards ─────────────────────────────────
# Participation-threshold based (NOT winner-take-all).
# Everyone who hits the bar gets rewarded. Optional champion bonus on top.
daily_competitions:
  - id: gif_enthusiast
    description: "Posted 5+ GIFs today"
    condition: { type: daily_threshold, field: gifs_posted, threshold: 5 }
    reward: 15
    hidden: true

  - id: gif_champion
    description: "Most GIFs posted today"
    condition: { type: daily_top, field: gifs_posted }
    reward: 35
    hidden: true

  - id: social_butterfly
    description: "Gave 5+ kudos today"
    condition: { type: daily_threshold, field: kudos_given, threshold: 5 }
    reward: 15
    hidden: true

  - id: top_earner_bonus
    description: "Daily top earner (excluding tips received)"
    condition: { type: daily_top, field: z_earned }
    reward_percent_of_earnings: 25
    hidden: true

  - id: emote_variety
    description: "Used 10+ unique emotes today"
    condition: { type: daily_threshold, field: unique_emotes_used, threshold: 10 }
    reward: 25
    hidden: true

  - id: chatterbox
    description: "Sent 50+ messages today"
    condition: { type: daily_threshold, field: messages_sent, threshold: 50 }
    reward: 20
    hidden: true

# ── Multiplier Events ────────────────────────────────────────
multipliers:
  off_peak:
    enabled: true
    days: [1, 2, 3, 4]          # Mon-Thu (0=Sun)
    hours: [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    multiplier: 2.0
    announce: true

  high_population:
    enabled: true
    min_users: 10
    multiplier: 1.5
    hidden: true

  holidays:
    enabled: true
    dates:
      - { date: "12-25", name: "Christmas", multiplier: 3.0 }
      - { date: "10-31", name: "Halloween", multiplier: 2.0 }
    announce: true

  scheduled_events:
    - name: "Weird Wednesday"
      cron: "0 20 * * 3"
      duration_hours: 4
      multiplier: 2.0
      presence_bonus: 500
      announce: true

# ── Rain Drops (Ambient Random Bonuses) ──────────────────────
# Periodic random Z bonus to all connected users.
# Creates a passive lottery that rewards presence.
rain:
  enabled: true
  interval_minutes: 45           # Average (randomized ±30%)
  min_amount: 5
  max_amount: 25
  pm_notification: true
  message: "☔ Rain drop! You received {amount} {currency} just for being here."

# ── Spending ─────────────────────────────────────────────────
spending:
  queue_tiers:
    - max_minutes: 15
      label: "Short / Music Video"
      cost: 250
    - max_minutes: 35
      label: "30-min Episode"
      cost: 500
    - max_minutes: 65
      label: "60-min Episode"
      cost: 750
    - max_minutes: 999
      label: "Movie"
      cost: 1000

  interrupt_play_next: 10000
  force_play_now: 100000
  force_play_requires_admin: true

  max_queues_per_day: 3
  queue_cooldown_minutes: 30

  blackout_windows:
    - name: "Weird Wednesday"
      cron: "0 20 * * 3"
      duration_hours: 4
    - name: "Weekend Marathon"
      cron: "0 12 * * 6"
      duration_hours: 24

# ── MediaCMS Integration ─────────────────────────────────────
mediacms:
  base_url: "https://media.example.com"
  api_token: "your-token-here"
  search_results_limit: 10

# ── Vanity Shop (Non-Playlist Coin Sinks) ────────────────────
vanity_shop:
  custom_greeting:
    enabled: true
    cost: 500
    description: "Bot greets you by name when you join the channel"

  custom_title:
    enabled: true
    cost: 1000
    description: "Custom title shown in bot announcements"

  chat_color:
    enabled: true
    cost: 750
    description: "Choose a color for your chat messages from the approved palette"
    palette:
      - { name: "Crimson", hex: "#DC143C" }
      - { name: "Gold", hex: "#FFD700" }
      - { name: "Emerald", hex: "#50C878" }
      - { name: "Royal Blue", hex: "#4169E1" }
      - { name: "Orchid", hex: "#DA70D6" }
      - { name: "Coral", hex: "#FF7F50" }
      - { name: "Teal", hex: "#008080" }
      - { name: "Silver Screen", hex: "#C0C0C0" }

  channel_gif:
    enabled: true
    cost: 5000
    description: "Personalized channel GIF (requires admin approval)"
    requires_admin_approval: true

  shoutout:
    enabled: true
    cost: 50
    description: "Bot posts your custom message in public chat"
    max_length: 200
    cooldown_minutes: 60

  daily_fortune:
    enabled: true
    cost: 10
    description: "Receive a random fortune / horoscope"

  rename_currency_personal:
    enabled: true
    cost: 2500
    description: "Your balance displays with a custom currency name (e.g. 'TacoBucks')"

# ── Named Ranks (B-Movie Themed) ────────────────────────────
# Based on LIFETIME EARNED Z, not current balance.
# Users never lose rank. Ranks provide real perks.
ranks:
  earn_multiplier_per_rank: 0.0   # Additional earn multiplier per rank tier (0 = none)
  spend_discount_per_rank: 0.02   # 2% discount per tier (tier 5 = 10% off)

  tiers:
    - name: "Extra"
      min_lifetime_earned: 0

    - name: "Grip"
      min_lifetime_earned: 1000
      perks: ["1 free daily fortune"]

    - name: "Key Grip"
      min_lifetime_earned: 5000
      perks: ["2% spend discount"]

    - name: "Gaffer"
      min_lifetime_earned: 15000
      perks: ["4% discount", "rain drops +20%"]

    - name: "Best Boy"
      min_lifetime_earned: 40000
      perks: ["6% discount", "+1 queue/day"]

    - name: "Associate Producer"
      min_lifetime_earned: 100000
      perks: ["8% discount", "premium vanity items"]

    - name: "Producer"
      min_lifetime_earned: 250000
      perks: ["10% discount", "priority queue position"]

    - name: "Director"
      min_lifetime_earned: 500000
      perks: ["12% discount", "+2 queues/day"]

    - name: "Executive Producer"
      min_lifetime_earned: 1000000
      perks: ["15% discount"]

    - name: "Studio Mogul"
      min_lifetime_earned: 5000000
      perks: ["20% discount", "custom everything", "legendary status"]
      cytube_level_promotion: 2

# ── CyTube Level 2 Promotion (Vanity Moderator) ─────────────
cytube_promotion:
  enabled: true
  purchasable: true
  cost: 50000
  min_rank: "Associate Producer"

# ── Gambling ─────────────────────────────────────────────────
gambling:
  enabled: true
  min_account_age_minutes: 60

  spin:
    enabled: true
    min_wager: 10
    max_wager: 500
    cooldown_seconds: 30
    daily_limit: 50
    payouts:
      - { symbols: "🍒🍒🍒", multiplier: 3, probability: 0.10 }
      - { symbols: "🍋🍋🍋", multiplier: 5, probability: 0.05 }
      - { symbols: "💎💎💎", multiplier: 10, probability: 0.02 }
      - { symbols: "7️⃣7️⃣7️⃣", multiplier: 50, probability: 0.002 }
      - { symbols: "partial", multiplier: 2, probability: 0.15 }
      - { symbols: "loss", multiplier: 0, probability: 0.678 }
    announce_jackpots_public: true
    jackpot_announce_threshold: 500

  flip:
    enabled: true
    min_wager: 10
    max_wager: 1000
    win_chance: 0.45
    cooldown_seconds: 15
    daily_limit: 100

  challenge:
    enabled: true
    min_wager: 50
    max_wager: 5000
    accept_timeout_seconds: 120
    rake_percent: 5
    announce_public: true

  daily_free_spin:
    enabled: true
    equivalent_wager: 50

  heist:
    enabled: false               # Stretch goal
    min_participants: 3
    join_window_seconds: 120
    success_chance: 0.40
    payout_multiplier: 1.5
    announce_public: true

# ── Tipping ──────────────────────────────────────────────────
tipping:
  enabled: true
  min_amount: 1
  max_per_day: 5000
  min_account_age_minutes: 30
  self_tip_blocked: true

# ── Balance Maintenance (Interest or Decay) ──────────────────
balance_maintenance:
  mode: "interest"               # "interest", "decay", or "none"

  interest:
    daily_rate: 0.001
    max_daily_interest: 10
    min_balance_to_earn: 100

  decay:
    enabled: false
    daily_rate: 0.005
    exempt_below: 50000
    label: "Vault maintenance fee"

# ── Retention ────────────────────────────────────────────────
retention:
  welcome_back:
    enabled: true
    days_absent: 7
    bonus: 100
    message: "Welcome back! Here's {amount} {currency}. You've been missed. 💚"

  inactivity_nudge:
    enabled: false
    days_absent: 14
    message: "We miss you! Your balance of {balance} {currency} is waiting."

# ── Public Announcements ─────────────────────────────────────
announcements:
  queue_purchase: true
  gambling_jackpot: true
  jackpot_min_amount: 500
  achievement_milestone: true
  rank_promotion: true
  challenge_result: true
  heist_result: true
  rain_drop: true
  daily_champion: true
  streak_milestone: true
  custom_greeting: true          # Show custom greetings in public chat

  templates:
    queue: "🎬 {user} just queued \"{title}\"! ({cost} {currency})"
    jackpot: "🎰 JACKPOT! {user} just won {amount} {currency}!"
    rank_up: "⭐ {user} is now a {rank}!"
    streak: "🔥 {user} hit a {days}-day streak!"
    greeting: "👋 {greeting}"
    rain: "☔ Rain! {count} users just got free {currency}."
    challenge_win: "⚔️ {winner} defeated {loser} and won {amount} {currency}!"

# ── Admin ────────────────────────────────────────────────────
admin:
  owner_level: 4                 # CyTube rank required for admin PM commands

# ── Metrics (Prometheus) ─────────────────────────────────────
metrics:
  enabled: true
  port: 28286
  path: /metrics

# ── Daily Digest ─────────────────────────────────────────────
digest:
  user_digest:
    enabled: true
    send_hour_utc: 4
    message: |
      📊 Daily Summary:
      Earned: {earned} {currency} | Spent: {spent} | Balance: {balance}
      Rank: {rank} | Streak: {streak} days 🔥
      Next goal: {next_goal_description} ({days_away} days away)

  admin_digest:
    enabled: true
    send_hour_utc: 5
```

---

## 5. Currency, Ranks & Progression

### Currency

- **Name:** Z-Coin (configurable). Symbol: `Z`. Plural: `Z-Coins`.
- **Per-user personal rename** available as vanity purchase ("TacoBucks," "Groins," etc.).
- Balances are integer-only (no fractional Z). Sub-integer earning rates (e.g. 0.5 Z/message) accumulate in a float internally and credit whole Z when they cross the threshold.

### Named Ranks (B-Movie Production Crew Theme)

Ranks are based on **lifetime earned Z**, not current balance. Users never lose rank. Rank names, thresholds, and perks are fully configurable. Default theme:

| Rank | Lifetime Earned | Key Perks |
|---|---|---|
| Extra | 0 | — |
| Grip | 1,000 | 1 free daily fortune |
| Key Grip | 5,000 | 2% spend discount |
| Gaffer | 15,000 | 4% discount, rain bonus +20% |
| Best Boy | 40,000 | 6% discount, +1 queue/day |
| Associate Producer | 100,000 | 8% discount, premium vanity |
| Producer | 250,000 | 10% discount, priority queue |
| Director | 500,000 | 12% discount, +2 queues/day |
| Executive Producer | 1,000,000 | 15% discount |
| Studio Mogul | 5,000,000 | 20% discount, auto CyTube level 2 promotion |

**Leveling discounts**: Each rank tier applies `spend_discount_per_rank × tier_index` to all spending. This is configurable globally.

**CyTube Level 2 promotion**: Reaching "Studio Mogul" (or a configurable rank) auto-promotes the user to CyTube moderator level (purely cosmetic — channel config should limit actual mod powers). Alternatively, level 2 can be purchased at any time for 50,000 Z if the user is at least "Associate Producer" rank.

### Progression Visibility

- `rank` command: current rank, lifetime earned, next threshold, progress bar, active perks
- `profile` command: full view — balance, rank, streak, achievements, vanity items, gambling stats
- `achievements` command: earned achievements with timestamps, progress toward next unearned ones
- Public announcements on rank-up (configurable)

---

## 6. Earning Mechanisms

### Design Principle

**Base presence rate is equal for AFK and active users.** Everyone earns 1 Z/min just for being connected. Active chatting earns *bonus* Z on top. This frames idle presence positively ("I earn just by being here") rather than punitively ("I earn less for not talking"). This directly serves the dwell-time goal.

### Presence (Passive)

| Mechanism | Rate | Hidden? |
|---|---|---|
| Connected to channel (AFK or active) | 1 Z/min (configurable) | No |
| Active bonus (optional, default 0) | Configurable additional Z/min for non-AFK | No |
| Hourly milestones (1h, 3h, 6h, 12h, 24h) | 10/30/75/200/1000 Z | No |
| Night watch (off-peak hours presence) | 1.5× multiplier on presence earning | Yes |
| Daily login streak (day 2–7, escalating) | 10→100 Z per day | No |
| 7-day streak milestone | 200 Z bonus | Yes |
| 30-day streak milestone | 2,000 Z bonus | Yes |
| Weekend→weekday bridge (present on both) | 500 Z weekly bonus | No (announced) |
| Rain drops (random periodic bonus) | 5–25 Z to all connected | No (PM notification) |
| Present at scheduled event start | Configurable bonus split among present | Yes |
| Survived full media (≥80% runtime present) | 5 Z per completion | Yes |
| Welcome wallet (first interaction) | 100 Z one-time | No |
| Welcome back (absent ≥7 days, returned) | 100 Z | No |
| Balance interest (daily, capped) | 0.1%/day, max 10 Z | No |

### Chat (Active Bonus)

| Trigger | Rate | Cap | Hidden? |
|---|---|---|---|
| Long message (≥30 chars) | 1 Z | 30/hr | Yes |
| Laugh received (per distinct laughing user) | 2 Z | 10 laughers/joke | Yes |
| Kudos received (`++`) | 3 Z | — | Yes |
| First message of the day | 5 Z | 1/day | Yes |
| Conversation starter (first after ≥10 min silence) | 10 Z | — | Yes |
| First to comment after media change (within 30s) | 3 Z | — | Yes |
| Comment during media (while content playing) | 0.5 Z/msg | Scales with duration | Yes |
| Liked currently playing content (`like` PM) | 2 Z | 1/item | Yes |
| Greeted a newcomer (name in chat within 60s of join) | 3 Z | 1/join event | Yes |
| Mentioned by another user | 1 Z | 5/hr same user | Yes |
| Bot interaction (triggered LLM response) | 2 Z | 10/day | Yes |

### Competitive Daily Awards (Participation Thresholds)

| Award | Condition | Reward |
|---|---|---|
| GIF Enthusiast | Posted ≥5 GIFs today | 15 Z |
| GIF Champion | Most GIFs today (bonus on top) | 35 Z |
| Social Butterfly | Gave ≥5 kudos today | 15 Z |
| Chatterbox | Sent ≥50 messages today | 20 Z |
| Emote Variety | Used ≥10 unique emotes today | 25 Z |
| Top Earner | Highest Z earned today | +25% of day's earnings |

### Multiplier Events

| Condition | Multiplier | Hidden? |
|---|---|---|
| Weekday off-peak (Mon–Thu, configurable hours) | 2× all earning | No (announced) |
| Scheduled event (Weird Wednesday, etc.) | 2× or custom | No (announced) |
| Channel population ≥ N users | 1.5× | Yes |
| Holiday (configurable date list) | 3× | No (announced) |
| Admin ad-hoc event | Custom multiplier, custom duration | No (announced) |

---

## 7. Spending Mechanisms

All spend actions via PM to the economy bot. Costs config-driven. Rank discounts applied automatically.

### Content Queue (MediaCMS Only)

| Action | Base Cost |
|---|---|
| Short / music video (≤15 min) | 250 Z |
| 30-min episode (≤35 min) | 500 Z |
| 60-min episode (≤65 min) | 750 Z |
| Movie (>65 min) | 1,000 Z |
| Interrupt (play next) | 10,000 Z |
| Force-play (stop current, play now) | 100,000 Z (admin-gated) |

Duration resolved via MediaCMS API metadata. Only content hosted on the MediaCMS instance can be queued. Queue commands rejected during configurable blackout windows (scheduled programming).

### Vanity Purchases

| Item | Cost | Notes |
|---|---|---|
| Custom greeting | 500 Z | Bot greets you in public chat on join (debounced) |
| Custom title | 1,000 Z | Shown in bot announcements |
| Chat color (from palette) | 750 Z | CSS-enforced, approved colors only |
| Channel GIF | 5,000 Z | Admin approval required |
| Shoutout (public chat message) | 50 Z | 200 char max, 60 min cooldown |
| Daily fortune | 10 Z | Random fortune/horoscope PM |
| Personal currency rename | 2,500 Z | Your balance shows as "TacoBucks" etc. |
| CyTube level 2 promotion | 50,000 Z | Requires min rank "Associate Producer" |

### Tipping

`tip @user <amount>` — direct transfer. Min 1 Z, daily cap 5,000 Z. Self-tip blocked (alias-aware). Both parties get PM confirmation.

---

## 8. Gambling

The primary mechanic for making users emotionally invested in their balance. All gambling is a **coin sink** (house edge ensures net Z is removed from circulation).

### Slot Machine (`spin <wager>`)

- Min wager: 10 Z. Max: 500 Z. Cooldown: 30s. Daily limit: 50 spins.
- Configurable payout table (symbols, multipliers, probabilities).
- Default expected value: ~0.75× wager (25% house edge).
- Jackpots (50× wager on `7️⃣7️⃣7️⃣`) announced in public chat.

### Coin Flip (`flip <wager>`)

- Double-or-nothing. 45% win chance (configurable). Fast cooldown (15s).

### Challenge (`challenge @user <wager>`)

- Two users, equal wagers, coin flip decides. 5% house rake.
- Target gets PM to accept/decline. 120s timeout.
- Results announced in public chat.

### Daily Free Spin

- One free spin per day for every user (equivalent to 50 Z wager, costs nothing).
- Drives daily return better than almost any other mechanic.

### Heist (`heist <wager>`) — Stretch Goal

- Group gamble. Initiator starts, others join within 120s window.
- 40% success chance. Success: all get wager × 1.5. Failure: all lose.
- Social coordination moment. Announced in public chat.

### Gambling Stats

`gambling` or `stats` PM command shows personal record: total spins/flips/challenges, biggest win, net P&L.

---

## 9. Anti-Abuse & Economy Health

| Concern | Mitigation |
|---|---|
| Chat spam for coins | Per-trigger hourly caps; long-message bonus capped at 30/hr |
| WebSocket bounce (false joins) | Join debounce: `join_debounce_minutes` (default 5) for actions; `greeting_absence_minutes` (default 30) for custom greetings |
| Alt-account farming | Min account age / message count before earning (configurable) |
| Self-kudos / self-laugh | Alias-aware exclusion (query kryten-userstats at runtime) |
| Tip laundering (alt→main) | Daily tip send cap; min account age to tip; admin-visible transfer log |
| AFK farming bots | AFK rate equals active rate (no incentive to fake activity); value comes from genuine presence |
| Inflation over time | Multiple coin sinks: gambling (house edge), vanity purchases, bounty expiry fees. Admin tools: adjust prices, add sinks, tune earn rates in config |
| Queue flooding | Per-user queue-spend cooldown; max queues/day; rejected during blackout windows |
| Gambling addiction concerns | Daily spin/flip limits; wager caps; gambling stats visibility for self-monitoring |
| PM command spam | Rate limit on PM commands per user (configurable max/minute) |
| Economy banning | Admins can `ban @user` from economy (keeps balance, stops all earning/spending) |
| Other bots in channel | Global `ignored_users` list (config-driven, case-insensitive). Ignored users are filtered at event ingestion — they never earn, never count as present for rain/events/population thresholds, and never satisfy "first to X" triggers. Bot joins do not trigger newcomer-greeting rewards. |

---

## 10. Bot Interface

### PM Commands (User)

All interaction via PM to the configured bot account. Commands need no prefix.

| Command | Sprint | Description |
|---|---|---|
| `help` | 1 | Brief, playful, intentionally non-exhaustive command list |
| `balance` / `bal` | 1 | Current balance, rank, streak info |
| `rewards` | 3 | Shows non-hidden earning triggers (partial reveal) |
| `history` | 5 | Last N transactions |
| `rank` | 6 | Current rank, progress to next, active perks |
| `profile` | 6 | Full user view (balance, rank, streak, achievements, vanity, gambling) |
| `achievements` | 6 | Earned achievements + progress toward next |
| `top` / `leaderboard` | 6 | Daily top earners, richest users, highest ranks |
| `search <query>` | 5 | Search MediaCMS catalog (returns titles, durations, costs) |
| `queue <id>` | 5 | Spend Z to queue a catalog item |
| `playnext <id>` | 5 | Spend 10,000 Z to queue at front |
| `forcenow <id>` | 5 | Spend 100,000 Z to force-play (admin-gated) |
| `like` | 3 | Like currently playing content (earn 2 Z) |
| `tip @user <amount>` | 5 | Transfer Z to another user |
| `shop` | 5 | List vanity items and prices |
| `buy <item> [args]` | 5 | Purchase vanity item (greeting, title, color, gif, fortune, shout, rename) |
| `spin [wager]` | 4 | Slot machine |
| `flip <wager>` | 4 | Coin flip (double-or-nothing) |
| `challenge @user <wager>` | 4 | Challenge another user to a duel |
| `accept` / `decline` | 4 | Respond to a pending challenge |
| `heist [wager]` / `heist join` | 4 | Group gamble (when enabled) |
| `gambling` / `stats` | 4 | Personal gambling statistics |
| `bounty <amount> "<desc>"` | 7 | Create a user bounty |
| `bounties` | 7 | List open bounties |

### Search & Queue Flow (User Perspective)

```
User → PM: search kung fu
Bot  → PM: Found 12 results:
           1. "Five Deadly Venoms" (1h 51m) — ID: 8Fn · 1,000 Z (900 Z with your discount!)
           2. "The 36th Chamber of Shaolin" (1h 55m) — ID: k2P · 1,000 Z
           3. "Kung Fu Hustle" (1h 39m) — ID: mR7 · 1,000 Z
           ...
User → PM: queue 8Fn
Bot  → PM: 🎬 Queued "Five Deadly Venoms" (1h 51m).
           Charged: 900 Z (10% Producer discount) · Balance: 2,340 Z
```

### PM Commands (Admin — CyTube Rank ≥ 4)

| Command | Sprint | Description |
|---|---|---|
| `grant @user <amount> [reason]` | 8 | Credit Z to user |
| `deduct @user <amount> [reason]` | 8 | Debit Z from user |
| `rain <amount>` | 8 | Split Z equally among all present users |
| `set_balance @user <amount>` | 8 | Hard-set balance |
| `set_rank @user <rank_name>` | 8 | Override rank |
| `reload` | 8 | Hot-reload config.yaml |
| `econ:stats` | 8 | Economy overview (active users, circulation, daily earn/spend) |
| `econ:user <name>` | 8 | Full user inspection |
| `econ:health` | 8 | Inflation indicators, earn/spend ratio, median balance |
| `econ:triggers` | 8 | Trigger hit rates (today, this week), dead/hot trigger identification |
| `econ:gambling` | 8 | House edge actual vs. configured, totals, active gamblers |
| `approve_gif @user` / `reject_gif @user` | 8 | Review pending channel GIF purchases |
| `ban @user` / `unban @user` | 8 | Exclude/restore user from economy |
| `announce <message>` | 8 | Bot posts message in public chat |
| `event start <multiplier> <minutes> "<name>"` | 7 | Start ad-hoc multiplier event |
| `event stop` | 7 | End current ad-hoc event |
| `claim_bounty <id> @winner` | 7 | Award an open bounty to a user |

---

## 11. Admin Tooling & Reporting

### Metrics (Prometheus)

Counters:
- `economy_z_earned_total` (by trigger_id)
- `economy_z_spent_total` (by spend_type)
- `economy_z_gambled_in_total`, `economy_z_gambled_out_total`
- `economy_events_processed_total` (by event_type)
- `economy_commands_processed_total` (by command)
- `economy_trigger_hits_total` (by trigger_id)

Gauges:
- `economy_active_users`
- `economy_total_circulation`
- `economy_median_balance`
- `economy_participation_rate`
- `economy_active_multiplier`

### Key Analytics (Tracked in DB)

| Metric | Why It Matters |
|---|---|
| Economy participation rate | % of channel users who have interacted with the economy |
| Daily active economy users | Is engagement growing or declining? |
| Trigger hit distribution | Which earning triggers actually fire? Dead triggers = wasted complexity |
| Spend category breakdown | What are people buying? |
| Gambling win/loss ratio | Is the house edge working as configured? |
| Median balance | Inflation indicator — are most users too poor to spend? |
| Time-to-first-spend | How long after onboarding until a user spends? |
| Weekend→weekday bridge rate | Is the bridge bonus changing behavior? |
| Lapsed user return rate | Are welcome-back bonuses working? |
| Queue-to-presence correlation | Do users stick around after queuing? |

### Weekly Admin Digest

Automated PM to all admin-level users (configurable schedule):
- Total Z minted / spent / gambled this week
- Net inflation (circulation change)
- Participation rate trend
- Top 5 earners, top 5 spenders
- Most popular spend categories
- Trigger hit rates (hot and dead)
- Gambling house edge actual vs. configured
- Notable events (jackpots, rank promotions, streak milestones)

### User Daily Digest

Automated PM to active economy users:
- Today's earnings and spending
- Current rank and streak
- Balance
- Progress toward next goal with estimated days

---

## 12. Sprint Plan

### Sprint 1 — Core Foundation

**Goal:** Deployable service that connects to NATS, tracks presence, awards base Z-Coins for being connected, stores balances in SQLite, and responds to `balance` via PM.

**Scope:**

1. **Project scaffolding**: `pyproject.toml` (dependencies: `kryten-py>=0.11.5`, `pyyaml`, `pydantic>=2.0`, `aiohttp>=3.9`), package structure, `__init__.py`, `__main__.py` with signal handling (mirror kryten-userstats), `README.md`.
2. **Config loading** (`config.py`): Pydantic `EconomyConfig` extending `KrytenConfig` pattern (follow kryten-llm approach). Load from YAML via `yaml.safe_load()` → Pydantic model. Validate all fields with defaults.
3. **Database** (`database.py`): `EconomyDatabase` class with async SQLite via `run_in_executor` (mirror kryten-userstats). Create tables: `accounts`, `transactions`, `daily_activity`. CRUD methods for balance operations (get, credit, debit) with atomic transaction logging.
4. **Main app** (`main.py`): `EconomyApp` following the canonical startup sequence (Section 2).
5. **Presence tracker** (`presence_tracker.py`): Track user sessions via `adduser`/`userleave`. **Ignored users filter**: skip all processing for usernames in the `ignored_users` config list (case-insensitive). Ignored users are never tracked, never earn presence Z, and are excluded from population counts used for rain distribution, high-population multiplier thresholds, and event-start presence bonuses. **Join debounce**: maintain in-memory `last_departure` timestamps. `is_genuine_arrival(username, threshold_minutes) → bool` checks both in-memory dict (fast, handles WS bouncing) and DB `last_seen` (handles service restarts). Flat base rate earning (1 Z/min for everyone connected). Periodic tick (every 60s) credits presence Z. Continuous session preservation: if a user leaves and returns within `join_debounce_minutes`, treat as uninterrupted session.
6. **PM handler** (`pm_handler.py`): Handle `pm` events via `@client.on("pm")` decorator. Parse PM text as commands. Send responses via `client.send_pm()`. Implement `balance` (returns balance, rank name, streak). Implement `help` (brief, playful, non-exhaustive).
7. **Command handler** (`command_handler.py`): NATS request-reply via `client.subscribe_request_reply("kryten.economy.command", handler)`. Implement `system.ping`, `system.health`, `balance.get`. Follow the kryten-moderator `ModeratorCommandHandler` pattern with handler routing map.
8. **Metrics server** (`metrics_server.py`): Prometheus HTTP on port 28286. Initial counters/gauges: `economy_z_earned_total`, `economy_z_spent_total`, `economy_events_processed_total`, `economy_active_users`, `economy_total_circulation`.
9. **`config.example.yaml`**: Full reference config with extensive inline comments.
10. **Tests**: Database CRUD, presence tick, join debounce logic, ignored-user filtering (case-insensitive, no earning, no presence tracking, excluded from population counts), PM parsing, balance operations, config validation.

**Deliverable:** Service connects, users accumulate Z for being present, can check balance via PM. Join debounce protects against WS bounce.

---

### Sprint 2 — Streaks, Milestones & Dwell Incentives

**Goal:** All time-based earning mechanics that reward sustained presence and return visits.

**Scope:**

1. **Daily streak tracking**: On each presence tick, check if user qualifies for today (≥`min_presence_minutes`). Update `streaks` table. Award escalating bonuses. Award 7-day and 30-day milestone bonuses. Reset on missed day.
2. **Hourly dwell milestones**: Track cumulative minutes per day in `hourly_milestones`. Award bonuses at 1h/3h/6h/12h/24h. PM notification on hit.
3. **Weekend→weekday bridge bonus**: Track `weekend_seen_this_week` / `weekday_seen_this_week` in `streaks`. Award 500 Z when both true for first time this week. PM reminder on Saturday.
4. **Night watch multiplier**: During configured off-peak hours, presence earnings get multiplier. Announce window open/close if configured.
5. **Rain drops** (`scheduler.py`): Periodic task. Random small Z bonus to all connected. Randomize interval ±30%. PM each recipient. Public announcement.
6. **Welcome wallet**: On `adduser` (genuine arrival per debounce), check `welcome_wallet_claimed`. If false and first PM or first join, credit starting balance, send welcome PM.
7. **Welcome-back bonus**: On genuine arrival, check `last_seen`. If absent ≥N days, award bonus, PM.
8. **Balance interest/decay**: Daily scheduled task. Interest: credit capped amount. Decay: debit from high balances. Log as transaction.
9. **DB tables**: `streaks`, `hourly_milestones`, `trigger_cooldowns`.
10. **Tests**: Streak math, milestone tracking, bridge logic, rain distribution, welcome wallet idempotency, interest/decay, weekly reset.

**Deliverable:** Rich time-based rewards that make staying connected and coming back feel valuable.

---

### Sprint 3 — Chat Earning Triggers

**Goal:** All chat-message-based earning mechanics.

**Scope:**

1. **Earning engine** (`earning_engine.py`): Evaluates a `ChatMessageEvent` against all configured chat triggers. **First gate: reject if username is in `ignored_users` list (case-insensitive).** Then per-trigger: enabled → cooldown → cap → condition → award. Returns `list[(trigger_id, amount)]`. Ignored users are also excluded from "first to X" candidate pools (first message of day, first after media change, conversation starter) — their messages do not reset silence timers or claim first-to-speak slots.
2. **Trigger implementations**: `long_message`, `first_message_of_day`, `conversation_starter` (track last message timestamp per channel), `laugh_received` (reuse kryten-userstats kudos phrase detection), `kudos_received` (`++` detection, alias-aware self-exclusion), `first_after_media_change`.
3. **Content engagement**: `comment_during_media`, `like_current` (PM command), `survived_full_media` (track join-before/present-at-end, ≥80% runtime).
4. **Social triggers**: `greeted_newcomer` (uses debounce — only genuine arrivals trigger greeting detection), `mentioned_by_other`, `bot_interaction`.
5. **Cooldown tracking**: `trigger_cooldowns` table. Per-user, per-trigger, per-window. Reset on expiry.
6. **Trigger analytics**: On each hit, increment `trigger_analytics` for the day.
7. **GIF/emote tracking**: Count GIFs and unique emotes per day per user for daily competitions.
8. **`rewards` PM command**: Shows non-hidden triggers only.
9. **`like` PM command**: Earn 2 Z for liking current media.
10. **Tests**: Each trigger with edge cases, cooldowns, caps, self-exclusion, alias resolution.

**Deliverable:** Active chatters earn bonus Z. All triggers configurable and independently toggleable.

---

### Sprint 4 — Gambling

**Goal:** Full gambling suite — the primary "make users care about coins" feature and major coin sink.

**Scope:**

1. **Gambling engine** (`gambling_engine.py`): Manage all game types. Validate wagers using `atomic_debit()` (Sprint 1's debit-or-fail method). Record outcomes in `transactions` and `gambling_stats`.
2. **Slots** (`spin`): Configurable payout table. Weighted random. Emoji display. Jackpot announcements.
3. **Coin flip** (`flip`): 45% win, double-or-nothing. Fast cooldown.
4. **Challenge** (`challenge @user <wager>`): PM to target, accept/decline, coin flip, rake. `pending_challenges` table for escrow/timeout. Public announcement.
5. **Daily free spin**: Once/day, free at configured equivalent wager. Track in `daily_activity.free_spin_used`.
6. **Heist** (gated `enabled: false`): Group join window → success/failure roll → announce.
7. **PM commands**: `spin`, `flip`, `challenge`, `accept`/`decline`, `heist`/`heist join`, `gambling`/`stats`.
8. **DB tables**: `gambling_stats`, `pending_challenges`.
9. **Tests**: Probability distribution (statistical), balance validation, escrow, cooldowns, free spin idempotency, rake math.

**Deliverable:** Multiple gambling games creating emotional stakes, social moments, and coin sink.

---

### Sprint 5 — Spending: Queue, Tips & Vanity Shop

**Goal:** All ways to spend Z-Coins.

**Scope:**

1. **MediaCMS client** (`media_client.py`): Async HTTP. `search(query)`, `get_by_id(id)`, `get_duration(id)`. Auth via API token. Brief result caching.
2. **Spending engine** (`spending_engine.py`): Validate balance → daily limit → blackout check → MediaCMS lookup → price tier → rank discount → debit → execute.
3. **Queue commands**: `search`, `queue <id>`, `playnext <id>`, `forcenow <id>`. Queue media via `client.add_media(channel, media_type, media_id)` (kryten-py wrapper).
4. **Blackout windows**: Reject queue commands during scheduled programming.
5. **Tipping**: `tip @user <amount>`. Alias-aware self-tip block. Daily cap. PM to both.
6. **Vanity shop**: `shop` (list), `buy <item>`. All items: custom_greeting, custom_title, chat_color, channel_gif (pending approval), shoutout, daily_fortune, rename_currency_personal.
7. **Rank discounts**: Apply before all spends. Show original and discounted price.
8. **`history` PM command**: Last N transactions.
9. **DB tables**: `tip_history`, `pending_approvals`.
10. **Tests**: MediaCMS mock, price tiers, blackouts, tip validation, rank discount, vanity persistence.

**Deliverable:** Content queuing, tipping, and cosmetic perks provide meaningful coin sinks.

---

### Sprint 6 — Achievements, Named Ranks & CyTube Promotion

**Goal:** Persistent progression system.

**Scope:**

1. **Achievement engine** (`achievement_engine.py`): Evaluate configured achievements on relevant events. One-time award. PM notification. Public announcement for major ones.
2. **Achievement conditions**: `lifetime_messages`, `lifetime_presence_hours`, `daily_streak`, `unique_tip_recipients`, `unique_tip_senders`, `lifetime_earned`, etc.
3. **`achievements` command**: List earned + progress toward next.
4. **Rank engine** (`rank_engine.py`): On each earn event, check rank thresholds. Promote, update DB, PM, announce.
5. **Rank perk enforcement**: Discount in spending engine, extra queue slots, rain bonus multiplier.
6. **`rank`, `profile`, `top`/`leaderboard` commands**.
7. **CyTube level 2 promotion**: Via achievement auto-promote or purchase. Change rank via `client.safe_set_channel_rank(channel, username, 2)` (kryten-py wrapper; includes rank-check and timeout).
8. **DB tables**: `achievements`.
9. **Tests**: Achievement conditions, rank promotion, perk application, CyTube promotion.

**Deliverable:** Visible progression with named ranks, achievements, and tangible perks.

---

### Sprint 7 — Competitive Events, Multipliers & Bounties

**Goal:** Time-limited competitive mechanics and user-generated bounties.

**Scope:**

1. **Daily competition evaluation**: End-of-day scheduled task. Threshold awards for all qualifying. Champion bonuses for top. PM + public announce.
2. **Multiplier engine**: Check active multipliers on each earn. Stack per config. Log in transaction metadata.
3. **Scheduled events**: Cron-based start/end. Presence bonuses. Public announcements.
4. **Admin ad-hoc events**: `event start/stop` commands.
5. **Bounties**: `bounty <amount> "<desc>"`, `bounties`, admin `claim_bounty`. Expiry with partial refund.
6. **DB tables**: `bounties`.
7. **Tests**: Competition eval, multiplier stacking, cron parsing, bounty lifecycle.

**Deliverable:** Dynamic events creating urgency and social coordination.

---

### Sprint 8 — Admin Tooling, Reporting & Visibility

**Goal:** Full admin control and operational analytics.

**Scope:**

1. **Admin PM commands**: All commands from Section 10, gated by CyTube rank ≥ `owner_level`. `grant`, `deduct`, `rain`, `set_balance`, `set_rank`, `reload`, `econ:stats`, `econ:user`, `econ:health`, `econ:triggers`, `econ:gambling`, `approve_gif`/`reject_gif`, `ban`/`unban`, `announce`.
2. **Config hot-reload**: `reload` command re-reads `config.yaml`, validates, applies without restart.
3. **Economy snapshots**: Periodic task → `economy_snapshots` table.
4. **Weekly admin digest**: Scheduled PM to admins.
5. **User daily digest**: Scheduled PM to active users.
6. **Prometheus metrics expansion**: Full counter/gauge set per Section 11.
7. **NATS command handler expansion**: All admin commands available via `kryten.economy.command`.
8. **DB tables**: `economy_snapshots`, `trigger_analytics`.
9. **Tests**: Admin authorization, config reload validation, snapshot generation, digest formatting.

**Deliverable:** Complete admin visibility and control. Self-reporting economy.

---

### Sprint 9 — Public Announcements, Polish & Hardening

**Goal:** Ambient visibility, integration testing, performance, deployment.

**Scope:**

1. **Event announcer** (`event_announcer.py`): Centralized announcement engine. Configurable templates. Send to public chat via `client.send_chat(channel, message)` (kryten-py wrapper).
2. **Custom greeting execution**: On genuine `adduser` (debounced with `greeting_absence_minutes`), check for custom greeting. Post in public chat. Batch brief delay to avoid spam on simultaneous joins.
3. **Error hardening**: NATS reconnection, SQLite contention, MediaCMS timeouts, malformed commands, balance race conditions (atomic debit-or-fail).
4. **Performance**: Profile presence tick at 100+ users. Batch SQLite writes if needed.
5. **PM rate limiting**: Max commands/minute per user.
6. **Full integration tests**: End-to-end with `MockKrytenClient` — join → earn → chat → gamble → queue → tip → rank up.
7. **Deployment**: `systemd/kryten-economy.service`, `config.example.yaml` (comprehensive inline docs), `README.md`.

**Deliverable:** Production-ready service with ambient visibility and robust error handling.

---

## 13. Sprint Dependency Graph

```
Sprint 1 (Foundation)
   │
   ├──→ Sprint 2 (Streaks & Dwell)
   │         │
   ├──→ Sprint 3 (Chat Triggers)
   │         │
   │         └──→ Sprint 5 (Spending & Shop) ─────┐
   │                                               │
   ├──→ Sprint 4 (Gambling)                        │
   │         │                                     │
   │         └──→ Sprint 6 (Achievements & Ranks) ─┤
   │                                               │
   │   Sprints 2–6 ──→ Sprint 7 (Events & Bounties)
   │                           │
   │                           └──→ Sprint 8 (Admin & Reporting)
   │                                       │
   │                                       └──→ Sprint 9 (Polish & Deploy)
   │
   └── Sprints 2, 3, 4 can run in parallel after Sprint 1
       Sprints 5, 6 can run in parallel after their predecessors
       Sprints 7+ are sequential
```

**Estimated timeline** (assuming single developer, full-time):
- Sprint 1: 1 week
- Sprints 2–4 (parallel): 2 weeks
- Sprints 5–6 (parallel): 1.5 weeks
- Sprint 7: 1 week
- Sprint 8: 1 week
- Sprint 9: 1 week
- **Total: ~7.5 weeks**

---

## 14. Open Questions & Pre-Sprint-1 Decisions

These should be resolved before generating the Sprint 1 implementation spec:

1. **Currency name** — "Z-Coin" is the working name. Final name? ("Groins" was mentioned.)
2. **Cross-channel balances** — Shared or per-channel? Schema supports per-channel. Recommend per-channel for v1.
3. **kryten-py YAML support** — Verify if `KrytenConfig.from_yaml()` exists. If not, service uses `yaml.safe_load()` → dict → `KrytenConfig(**dict)`. Dependency: `PyYAML`.
4. **PM event format** — Verify how kryten-py delivers PMs via `@client.on("pm")`. Expected: `ChatMessageEvent` model with `.username`, `.message`, `.channel`, `.domain`, `.rank` fields.
5. **Chat color implementation** — Economy stores color choice. Actual CSS rendering is a channel-side concern. Confirm CyTube supports per-user CSS targeting.
6. **Channel GIF delivery mechanism** — How are personalized GIFs delivered today? Economy stores URL + approval status; delivery mechanism TBD.
7. **CyTube rank change command** — Use `client.safe_set_channel_rank(channel, username, rank, *, domain=None, check_rank=True, timeout=2.0)`. This is the preferred method (includes rank-check and timeout). ✅ **Resolved:** Always use `safe_set_channel_rank`.
8. **Alias resolution** — Recommend querying kryten-userstats at runtime via `kryten.userstats.command` → `alias.resolve`. No duplicate alias storage.
9. **Bot account** — Dedicated CyTube account or shared with kryten-robot? Recommend dedicated.

---

*End of consolidated implementation plan. Ready for individual sprint spec generation.*
