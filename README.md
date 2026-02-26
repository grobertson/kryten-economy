# kryten-economy

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-574%20passing-brightgreen.svg)](#testing)

Channel engagement currency microservice for [CyTube](https://github.com/calzoneman/sync), built on the [kryten](https://github.com/grobertson) ecosystem.

## What It Does

kryten-economy turns passive viewers into active participants by rewarding channel presence, chat activity, and social interaction with **Z-Coins** — a fully configurable virtual currency. Users spend Z on queueing media, vanity items, tipping, and gambling. A B-movie-themed rank system (Extra → Stunt Double → … → Director) grants real perks like queue discounts and extra queue slots.

### Feature Highlights

- **Presence earning** — per-minute Z rewards with night-watch bonuses and hourly milestones
- **Chat triggers** — 12+ earning triggers for long messages, kudos, laughs, conversation starters, and more
- **Daily streaks** — consecutive-day bonuses with milestone multipliers and bridge recovery
- **Gambling** — slot machine, coin flip, player duels, and cooperative channel heists
- **Media queue** — search and queue content from a MediaCMS instance, paid with Z
- **Tipping** — alias-aware peer-to-peer transfers with self-tip prevention
- **Vanity shop** — custom greetings, chat colors, titles, channel GIFs (with admin approval)
- **Achievements** — one-time badges with Z rewards for milestones
- **Rank progression** — automatic rank-ups with CyTube rank promotions
- **Multiplier events** — scheduled and ad-hoc earning multipliers, daily competitions, user bounties
- **Admin tooling** — 16 admin commands for grants, bans, economy health, trigger analytics, config hot-reload
- **Monitoring** — Prometheus metrics endpoint with per-trigger, per-command, and per-channel gauges

Everything is configurable via a single YAML file — every rate, threshold, reward, cost, and message template.

## Requirements

- **Python 3.11+**
- **NATS server** (via kryten infrastructure)
- **[kryten-py](https://github.com/grobertson/kryten-py) >= 0.11.5** — CyTube ↔ NATS client library
- **kryten-robot** — event source bridging CyTube websocket events to NATS
- **kryten-userstats** — alias resolution for tip self-transfer prevention
- **Optional:** MediaCMS instance for content queue features

## Installation

### From PyPI

```bash
pip install kryten-economy
```

### From GitHub

```bash
pip install git+https://github.com/grobertson/kryten-economy.git
```

### Development Install

```bash
git clone https://github.com/grobertson/kryten-economy.git
cd kryten-economy
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows
pip install -e ".[dev]"
```

## Quick Start

```bash
# 1. Copy the example config
cp config.example.yaml config.yaml

# 2. Edit with your NATS and channel settings
$EDITOR config.yaml

# 3. Run
kryten-economy --config config.yaml

# Or as a module
python -m kryten_economy --config config.yaml
```

### systemd Deployment

```bash
sudo cp systemd/kryten-economy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kryten-economy
```

The included service file has resource limits (`MemoryMax=512M`, `CPUQuota=50%`) and security hardening (`NoNewPrivileges`, `ProtectSystem=strict`).

## Configuration

See [`config.example.yaml`](config.example.yaml) for the complete reference with inline documentation. Key sections:

| Section | Controls |
|---|---|
| `currency` | Name, symbol, starting balance, daily cap |
| `presence` | Base rate, night watch, milestones, greeting absence |
| `streaks` | Streak bonuses, milestones, bridge recovery |
| `chat_triggers` | 12 chat-based earning triggers with cooldowns |
| `content_triggers` | Media interaction rewards |
| `social_triggers` | Kudos, laughs, greetings, mentions |
| `gambling` | Slot payouts, flip odds, challenge/heist rules |
| `spending` | Queue costs, tip limits, vanity shop items |
| `ranks` | Tier names, thresholds, perks, CyTube promotions |
| `achievements` | Badge conditions and rewards |
| `events` | Scheduled multipliers, competitions, bounties |
| `announcements` | Public chat templates and toggle gates |
| `admin` | Owner level, digest schedules |
| `commands` | PM rate limit |
| `metrics` | Prometheus endpoint port |

Environment variable substitution is supported: `${NATS_URL}`, `${VAR:-default}`.

## PM Commands

### User Commands

| Command | Description |
|---|---|
| `help` | Brief command overview |
| `balance` / `bal` | Current balance, rank, and streak |
| `rank` | Rank progress and perks |
| `profile` | Full user summary |
| `achievements` | Earned badges and progress |
| `top` / `leaderboard` | Leaderboards |
| `history` | Recent transaction history |
| `search <query>` | Search MediaCMS catalog |
| `queue <id>` | Queue content (costs Z) |
| `tip @user <amount>` | Transfer Z to another user |
| `shop` | Browse vanity items |
| `buy <item>` | Purchase a vanity item |
| `spin [wager]` | Slot machine |
| `flip <wager>` | Coin flip |
| `challenge @user <wager>` | Player duel |
| `bounty <amount> "<desc>"` | Create a bounty |
| `bounties` | List open bounties |
| `events` | View active multipliers |

### Admin Commands (CyTube Rank ≥ 4)

| Command | Description |
|---|---|
| `grant @user <amount> [reason]` | Credit Z to a user |
| `deduct @user <amount> [reason]` | Debit Z from a user |
| `rain <amount>` | Distribute Z equally to all present users |
| `set_balance @user <amount>` | Hard-set a user's balance |
| `set_rank @user <rank>` | Override a user's economy rank |
| `ban @user [reason]` / `unban @user` | Economy access control |
| `reload` | Hot-reload config without restart |
| `announce <message>` | Post a message in public chat |
| `econ:stats` | Economy overview dashboard |
| `econ:user <name>` | Full user inspection |
| `econ:health` | Inflation/deflation indicators |
| `econ:triggers` | Trigger hit analytics with dead-trigger detection |
| `econ:gambling` | Actual vs. configured house edge |
| `event start/stop <name>` | Ad-hoc multiplier events |
| `claim_bounty <id> @user` | Award a bounty to winner |
| `approve_gif @user` / `reject_gif @user` | Manage GIF purchase approvals |

## Testing

```bash
# Run all 574 tests
pytest

# With coverage
pytest --cov=kryten_economy --cov-report=term-missing

# Specific sprint area
pytest tests/test_gambling_engine.py tests/test_slots.py tests/test_flip.py -v
```

All tests use mocks — no NATS server, database, or external services required.

## Monitoring

Prometheus metrics served at `http://localhost:28286/metrics` (port configurable).

**Counters:** Z earned by trigger, Z spent by type, gamble in/out, events processed, commands processed, trigger hits, achievements awarded, rank promotions, competitions, bounties.

**Gauges:** Active users, total circulation, median balance, participation rate, active multiplier, rank distribution — all per-channel.

## Architecture

```
kryten-economy/
├── kryten_economy/
│   ├── __init__.py              # Version from metadata
│   ├── __main__.py              # CLI entry, signal handling
│   ├── main.py                  # EconomyApp orchestrator
│   ├── config.py                # Pydantic config models
│   ├── database.py              # SQLite WAL, 12 tables
│   ├── presence_tracker.py      # Dwell tracking, join debounce
│   ├── earning_engine.py        # Centralized earning + multipliers
│   ├── spending_engine.py       # Spending validation + rank discounts
│   ├── gambling_engine.py       # Slots, flip, challenge, heist
│   ├── pm_handler.py            # 34 PM commands + rate limiting
│   ├── command_handler.py       # NATS request-reply API
│   ├── achievement_engine.py    # One-time badges
│   ├── rank_engine.py           # B-movie rank progression
│   ├── multiplier_engine.py     # Multiplier stack
│   ├── competition_engine.py    # Daily competitions
│   ├── bounty_manager.py        # User bounties
│   ├── event_announcer.py       # Chat announcements (dedup + batching)
│   ├── greeting_handler.py      # Custom greetings on join
│   ├── admin_scheduler.py       # Snapshots + digests
│   ├── scheduled_event_manager.py  # Cron-based events
│   ├── metrics_server.py        # Prometheus endpoint
│   ├── media_client.py          # MediaCMS API client
│   └── utils.py                 # Shared utilities
├── tests/                       # 65 test files, 574 tests
├── config.example.yaml          # Full reference config (~570 lines)
├── systemd/
│   └── kryten-economy.service   # Production systemd unit
├── pyproject.toml
├── LICENSE
└── CHANGELOG.md
```

### Design Principles

- **Zero raw NATS** — all messaging through [kryten-py](https://github.com/grobertson/kryten-py) wrappers
- **Atomic debits** — single-transaction debit-or-fail prevents negative balances
- **SQLite WAL** — write-ahead logging with `busy_timeout=30s` for concurrent access
- **Error isolation** — every event handler wrapped in try/except; one bad event never crashes the service
- **Hot-reloadable config** — admin `reload` command re-validates via Pydantic and applies without restart

## Publishing to PyPI

```bash
# Build
python -m build

# Upload to TestPyPI first
python -m twine upload --repository testpypi dist/*

# Upload to PyPI
python -m twine upload dist/*
```

## License

[MIT](LICENSE)

## Contributing

This is part of the kryten ecosystem. Issues and PRs welcome at [github.com/grobertson/kryten-economy](https://github.com/grobertson/kryten-economy).
