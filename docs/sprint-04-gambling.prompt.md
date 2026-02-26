# Sprint 4 — Gambling

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 4 of 9  
> **Goal:** Full gambling suite — the primary mechanism for making users emotionally invested in their balance, and the economy's major coin sink.  
> **Depends on:** Sprint 1 (Core Foundation)  
> **Enables:** Sprint 6 (Achievements, Named Ranks & CyTube Promotion)

---

## Table of Contents

1. [Deliverable Summary](#1-deliverable-summary)
2. [New Database Tables](#2-new-database-tables)
3. [Gambling Engine Architecture](#3-gambling-engine-architecture)
4. [Slot Machine (`spin`)](#4-slot-machine-spin)
5. [Coin Flip (`flip`)](#5-coin-flip-flip)
6. [Challenge (`challenge`)](#6-challenge-challenge)
7. [Daily Free Spin](#7-daily-free-spin)
8. [Heist (Gated)](#8-heist-gated)
9. [Gambling Stats Command](#9-gambling-stats-command)
10. [PM Command Registrations](#10-pm-command-registrations)
11. [Public Announcements](#11-public-announcements)
12. [Anti-Abuse & Limits](#12-anti-abuse--limits)
13. [Config Sections Activated](#13-config-sections-activated)
14. [Test Specifications](#14-test-specifications)
15. [Acceptance Criteria](#15-acceptance-criteria)

---

## 1. Deliverable Summary

At the end of this sprint, the service additionally:

- Provides a **slot machine** (`spin [wager]`) with a configurable payout table, emoji display, jackpot announcements, and 30-second cooldown
- Provides a **coin flip** (`flip <wager>`) — 45% win chance, double-or-nothing, 15-second cooldown
- Provides a **challenge** system (`challenge @user <wager>`) — two-user duel with 5% house rake, accept/decline flow, 120-second timeout, result announced in public chat
- Awards one **daily free spin** per user per day (equivalent to a 50 Z wager at zero cost)
- Includes a **heist** framework (`heist [wager]` / `heist join`) — group gamble, initially disabled (`enabled: false`) as a stretch goal
- Tracks all gambling outcomes in `gambling_stats` and `transactions` tables
- Responds to `gambling` / `stats` PM commands showing personal gambling record
- All wagers enforce **minimum account age** before gambling is allowed
- All gambling is a **coin sink** — house edge ensures net Z is removed from circulation over time

---

## 2. New Database Tables

### 2.1 `gambling_stats` Table

```sql
CREATE TABLE IF NOT EXISTS gambling_stats (
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
```

### 2.2 `pending_challenges` Table

```sql
CREATE TABLE IF NOT EXISTS pending_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenger TEXT NOT NULL,
    target TEXT NOT NULL,
    channel TEXT NOT NULL,
    wager INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'pending'   -- pending, accepted, declined, expired
);
```

---

## 3. Gambling Engine Architecture

### 3.1 File: `kryten_economy/gambling_engine.py`

Central module for all gambling operations. Every game type validates balance, enforces cooldowns/caps, executes the game, records outcomes, and returns a result to be formatted as a PM response.

### 3.2 Shared Data Types

```python
from dataclasses import dataclass
from enum import Enum

class GambleOutcome(Enum):
    WIN = "win"
    LOSS = "loss"
    JACKPOT = "jackpot"
    PUSH = "push"  # Break-even (e.g. partial match)

@dataclass
class GambleResult:
    """Result of a single gambling action."""
    outcome: GambleOutcome
    wager: int
    payout: int              # Gross payout (0 on loss, wager × multiplier on win)
    net: int                 # Net gain/loss (payout - wager; negative on loss)
    display: str             # Emoji/visual display for PM (e.g. "🍒🍒🍒")
    announce_public: bool    # Whether to announce in public chat
    message: str             # Formatted PM message
```

### 3.3 Constructor

```python
class GamblingEngine:
    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        logger: logging.Logger,
    ):
        self._config = config
        self._db = database
        self._logger = logger
        self._currency = config.currency.name
        self._symbol = config.currency.symbol
        
        # Build payout table for slots (pre-computed cumulative probability)
        self._slot_payouts = self._build_payout_table(config.gambling.spin.payouts)
        
        # In-memory cooldown tracking: (username, game_type) → last_play_time
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        
        # In-memory pending challenges for fast lookup
        # challenger_key → pending_challenge_id
        # target_key → pending_challenge_id
        self._active_challenges: dict[str, int] = {}
        
        # Ignored users (bots) cannot be challenge targets
        self._ignored_users: set[str] = {u.lower() for u in config.ignored_users}
```

### 3.4 Common Validation

All games share pre-validation logic:

```python
async def _validate_gamble(
    self, username: str, channel: str, wager: int,
    game_type: str, min_wager: int, max_wager: int,
    cooldown_seconds: int, daily_limit: int | None = None,
) -> str | None:
    """Validate a gambling action. Returns error message string, or None if valid."""
    
    # 1. Gambling enabled?
    if not self._config.gambling.enabled:
        return "Gambling is currently disabled."
    
    # 2. Account exists?
    account = await self._db.get_account(username, channel)
    if not account:
        return "You need an account first. Stick around a bit!"
    
    # 3. Economy banned?
    if account.get("economy_banned"):
        return "Your economy access is restricted."
    
    # 4. Minimum account age
    min_age = self._config.gambling.min_account_age_minutes
    first_seen = parse_timestamp(account.get("first_seen"))
    if first_seen:
        age_minutes = (datetime.now(timezone.utc) - first_seen).total_seconds() / 60
        if age_minutes < min_age:
            remaining = int(min_age - age_minutes)
            return f"You need to be around for {remaining} more minutes before gambling."
    
    # 5. Wager range
    if wager < min_wager:
        return f"Minimum wager: {min_wager} {self._symbol}."
    if wager > max_wager:
        return f"Maximum wager: {max_wager} {self._symbol}."
    
    # 6. Sufficient balance
    if account.get("balance", 0) < wager:
        return f"Insufficient funds. Balance: {account['balance']} {self._symbol}."
    
    # 7. Cooldown
    cooldown_key = (username.lower(), game_type)
    last_play = self._cooldowns.get(cooldown_key)
    if last_play:
        elapsed = (datetime.now(timezone.utc) - last_play).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            return f"Cooldown: {remaining}s remaining."
    
    # 8. Daily limit
    if daily_limit is not None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        activity = await self._db.get_or_create_daily_activity(username, channel, today)
        game_count_field = f"total_{game_type}s_today"  # Not in daily_activity — use gambling_stats
        # Use a cooldown-based approach: track daily count via trigger_cooldowns table
        count_today = await self._get_daily_game_count(username, channel, game_type)
        if count_today >= daily_limit:
            return f"Daily limit reached ({daily_limit} {game_type}s per day)."
    
    return None  # All checks passed
```

### 3.5 Daily Game Count Tracking

Since `daily_activity` doesn't have per-game-type daily counters, use the `trigger_cooldowns` table with a 24-hour window:

```python
async def _get_daily_game_count(self, username: str, channel: str, game_type: str) -> int:
    """Get the number of times a user has played a game type today."""
    trigger_id = f"gambling.{game_type}.daily"
    row = await self._db.get_trigger_cooldown(username, channel, trigger_id)
    if row is None:
        return 0
    window_start = parse_timestamp(row["window_start"])
    if window_start and window_start.date() == datetime.now(timezone.utc).date():
        return row["count"]
    return 0  # New day

async def _increment_daily_game_count(self, username: str, channel: str, game_type: str) -> None:
    """Increment the daily game count."""
    trigger_id = f"gambling.{game_type}.daily"
    now = datetime.now(timezone.utc)
    row = await self._db.get_trigger_cooldown(username, channel, trigger_id)
    if row is None or parse_timestamp(row["window_start"]).date() != now.date():
        await self._db.set_trigger_cooldown(username, channel, trigger_id, 1, now)
    else:
        await self._db.increment_trigger_cooldown(username, channel, trigger_id)
```

### 3.6 Common Post-Game Recording

```python
async def _record_outcome(
    self, username: str, channel: str, wager: int, payout: int,
    game_type: str, display: str, related_user: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Record gambling outcome in transactions and gambling_stats."""
    net = payout - wager
    now = datetime.now(timezone.utc)
    
    if net > 0:
        # Win: credit net winnings
        await self._db.credit(
            username, channel, net,
            tx_type="gamble_win",
            trigger_id=f"gambling.{game_type}",
            reason=f"{game_type} win: {display}",
            related_user=related_user,
            metadata=json.dumps(metadata) if metadata else None,
        )
    elif net < 0:
        # Loss: debit the wager (payout is 0 or less than wager)
        await self._db.debit(
            username, channel, abs(net),
            tx_type="gamble_loss",
            trigger_id=f"gambling.{game_type}",
            reason=f"{game_type} loss: {display}",
            related_user=related_user,
            metadata=json.dumps(metadata) if metadata else None,
        )
    # Push (net == 0): no balance change, but still log
    
    # Update gambling_stats
    await self._db.update_gambling_stats(
        username, channel, game_type,
        net=net,
        biggest_win=max(0, net),
        biggest_loss=abs(min(0, net)),
    )
    
    # Update gambled_in/out on accounts and daily_activity
    await self._db.increment_lifetime_gambled(username, channel, wager, payout)
    today = now.strftime("%Y-%m-%d")
    await self._db.increment_daily_gambled(username, channel, today, wager, payout)
    
    # Update cooldown
    self._cooldowns[(username.lower(), game_type)] = now
    
    # Increment daily count
    await self._increment_daily_game_count(username, channel, game_type)
```

---

## 4. Slot Machine (`spin`)

### 4.1 Config

```yaml
gambling:
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
```

### 4.2 Payout Table Builder

Pre-compute cumulative probability ranges at init time for O(1) lookups:

```python
@dataclass
class PayoutEntry:
    symbols: str
    multiplier: float
    cumulative_probability: float

def _build_payout_table(self, payouts) -> list[PayoutEntry]:
    """Build cumulative probability table from config.
    
    payouts comes from config.gambling.spin.payouts — a list of Pydantic models.
    Use attribute access (p.probability), not dict access (p["probability"]).
    """
    table = []
    cumulative = 0.0
    for p in payouts:
        cumulative += p.probability
        table.append(PayoutEntry(
            symbols=p.symbols,
            multiplier=p.multiplier,
            cumulative_probability=cumulative,
        ))
    # Verify probabilities sum to ~1.0
    if abs(cumulative - 1.0) > 0.01:
        self._logger.warning(
            "Slot payout probabilities sum to %.4f (expected 1.0)", cumulative
        )
    return table
```

### 4.3 Spin Execution

```python
async def spin(self, username: str, channel: str, wager: int) -> GambleResult:
    """Execute a slot machine spin."""
    cfg = self._config.gambling.spin
    
    # Validate
    error = await self._validate_gamble(
        username, channel, wager, "spin",
        cfg.min_wager, cfg.max_wager, cfg.cooldown_seconds, cfg.daily_limit,
    )
    if error:
        return GambleResult(
            outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
            display="", announce_public=False, message=error,
        )
    
    # Debit wager upfront (atomic debit-or-fail)
    success = await self._db.atomic_debit(username, channel, wager)
    if not success:
        return GambleResult(
            outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
            display="", announce_public=False,
            message=f"Insufficient funds.",
        )
    
    # Roll
    roll = random.random()
    result_entry = self._resolve_payout(roll)
    payout = int(wager * result_entry.multiplier)
    net = payout - wager
    
    # Credit payout (if any)
    if payout > 0:
        await self._db.credit(
            username, channel, payout,
            tx_type="gamble_win" if net > 0 else "gamble_loss",
            trigger_id="gambling.spin",
            reason=f"Spin: {result_entry.symbols}",
            metadata=json.dumps({"multiplier": result_entry.multiplier, "roll": round(roll, 4)}),
        )
    
    # Determine outcome type
    if result_entry.multiplier >= 50:
        outcome = GambleOutcome.JACKPOT
    elif net > 0:
        outcome = GambleOutcome.WIN
    elif net == 0:
        outcome = GambleOutcome.PUSH
    else:
        outcome = GambleOutcome.LOSS
    
    # Build display
    display = result_entry.symbols if result_entry.symbols not in ("partial", "loss") else self._generate_loss_display(result_entry.symbols)
    
    # Announce jackpot?
    announce = (
        cfg.announce_jackpots_public
        and payout >= cfg.jackpot_announce_threshold
    )
    
    # Build PM message
    if net > 0:
        message = f"🎰 {display} — WIN! +{net} {self._symbol} (Payout: {payout}). Balance: {{balance}}"
    elif net == 0:
        message = f"🎰 {display} — Push. Balance: {{balance}}"
    else:
        message = f"🎰 {display} — Loss. -{wager} {self._symbol}. Balance: {{balance}}"
    
    # Record in gambling_stats
    await self._db.update_gambling_stats(
        username, channel, "spin", net=net,
        biggest_win=max(0, net), biggest_loss=abs(min(0, net)),
    )
    await self._db.increment_lifetime_gambled(username, channel, wager, payout)
    self._cooldowns[(username.lower(), "spin")] = datetime.now(timezone.utc)
    await self._increment_daily_game_count(username, channel, "spin")
    
    # Fetch updated balance for message
    account = await self._db.get_account(username, channel)
    balance = account.get("balance", 0) if account else 0
    message = message.format(balance=f"{balance} {self._symbol}")
    
    return GambleResult(
        outcome=outcome, wager=wager, payout=payout, net=net,
        display=display, announce_public=announce, message=message,
    )

def _resolve_payout(self, roll: float) -> PayoutEntry:
    """Resolve a random roll to a payout entry."""
    for entry in self._slot_payouts:
        if roll <= entry.cumulative_probability:
            return entry
    # Fallback to last entry (should be "loss")
    return self._slot_payouts[-1]
```

### 4.4 Loss & Partial Display Generation

When the result is "loss" or "partial", generate a realistic-looking near-miss display:

```python
SLOT_SYMBOLS = ["🍒", "🍋", "💎", "7️⃣", "🍊", "🍇", "⭐", "🔔"]

def _generate_loss_display(self, result_type: str) -> str:
    """Generate a display string for non-matching spins."""
    if result_type == "partial":
        # Two matching + one different
        symbol = random.choice(SLOT_SYMBOLS)
        other = random.choice([s for s in SLOT_SYMBOLS if s != symbol])
        return f"{symbol}{symbol}{other}"
    else:
        # All different (or random mishmash)
        symbols = random.sample(SLOT_SYMBOLS, 3)
        return "".join(symbols)
```

---

## 5. Coin Flip (`flip`)

### 5.1 Config

```yaml
gambling:
  flip:
    enabled: true
    min_wager: 10
    max_wager: 1000
    win_chance: 0.45
    cooldown_seconds: 15
    daily_limit: 100
```

### 5.2 Execution

```python
async def flip(self, username: str, channel: str, wager: int) -> GambleResult:
    """Execute a coin flip — double-or-nothing."""
    cfg = self._config.gambling.flip
    
    error = await self._validate_gamble(
        username, channel, wager, "flip",
        cfg.min_wager, cfg.max_wager, cfg.cooldown_seconds, cfg.daily_limit,
    )
    if error:
        return GambleResult(
            outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
            display="", announce_public=False, message=error,
        )
    
    # Debit wager
    success = await self._db.atomic_debit(username, channel, wager)
    if not success:
        return GambleResult(
            outcome=GambleOutcome.LOSS, wager=wager, payout=0, net=0,
            display="", announce_public=False, message="Insufficient funds.",
        )
    
    # Flip
    won = random.random() < cfg.win_chance
    
    if won:
        payout = wager * 2
        net = wager  # Net gain = wager (doubled)
        display = "🪙 Heads!"
        await self._db.credit(
            username, channel, payout,
            tx_type="gamble_win",
            trigger_id="gambling.flip",
            reason=f"Flip win: {payout}",
        )
        outcome = GambleOutcome.WIN
    else:
        payout = 0
        net = -wager
        display = "🪙 Tails!"
        # Wager already debited
        outcome = GambleOutcome.LOSS
    
    # Record
    await self._db.update_gambling_stats(
        username, channel, "flip", net=net,
        biggest_win=max(0, net), biggest_loss=abs(min(0, net)),
    )
    await self._db.increment_lifetime_gambled(username, channel, wager, payout)
    self._cooldowns[(username.lower(), "flip")] = datetime.now(timezone.utc)
    await self._increment_daily_game_count(username, channel, "flip")
    
    account = await self._db.get_account(username, channel)
    balance = account.get("balance", 0) if account else 0
    
    if won:
        message = f"{display} WIN! +{net} {self._symbol}. Balance: {balance} {self._symbol}"
    else:
        message = f"{display} Loss. -{wager} {self._symbol}. Balance: {balance} {self._symbol}"
    
    return GambleResult(
        outcome=outcome, wager=wager, payout=payout, net=net,
        display=display, announce_public=False, message=message,
    )
```

---

## 6. Challenge (`challenge`)

### 6.1 Config

```yaml
gambling:
  challenge:
    enabled: true
    min_wager: 50
    max_wager: 5000
    accept_timeout_seconds: 120
    rake_percent: 5
    announce_public: true
```

### 6.2 Challenge Flow

```
1. Challenger sends PM: "challenge @bob 200"
2. Validate: both users exist, both have sufficient balance, no pending challenge between them
3. Debit wager from challenger (escrow)
4. Create pending_challenges row (status=pending, expires_at=now+timeout)
5. PM to target: "@alice challenges you to a {wager} Z duel! 'accept' or 'decline' (expires in 2 min)"
6. PM to challenger: "Challenge sent to bob for {wager} Z. Waiting for response..."
7. Target responds:
   a. "accept" → debit wager from target → flip → rake → distribute → announce
   b. "decline" → refund challenger → PM both
   c. timeout → expire → refund challenger → PM both
```

### 6.3 Creating a Challenge

```python
async def create_challenge(
    self, challenger: str, target: str, channel: str, wager: int,
) -> str:
    """Create a new challenge. Returns PM response for the challenger."""
    cfg = self._config.gambling.challenge
    
    if not cfg.enabled:
        return "Challenges are currently disabled."
    
    # Basic validation (challenger side)
    error = await self._validate_gamble(
        challenger, channel, wager, "challenge",
        cfg.min_wager, cfg.max_wager, 0, None,  # No cooldown/daily limit on creation
    )
    if error:
        return error
    
    # Target validation
    if challenger.lower() == target.lower():
        return "You can't challenge yourself."
    
    if target.lower() in self._ignored_users:
        return "That user can't be challenged."
    
    target_account = await self._db.get_account(target, channel)
    if not target_account:
        return f"{target} doesn't have an account."
    if target_account.get("balance", 0) < wager:
        return f"{target} can't afford that wager."
    if target_account.get("economy_banned"):
        return f"{target}'s economy access is restricted."
    
    # Check for existing pending challenge between these users
    existing = await self._db.get_pending_challenge(challenger, target, channel)
    if existing:
        return f"You already have a pending challenge with {target}."
    
    # Escrow: debit from challenger
    success = await self._db.atomic_debit(challenger, channel, wager)
    if not success:
        return "Insufficient funds."
    
    # Create pending challenge
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=cfg.accept_timeout_seconds)
    challenge_id = await self._db.create_pending_challenge(
        challenger, target, channel, wager, expires_at,
    )
    
    # PM the target (via callback — the engine returns the message, the caller sends it)
    return f"challenge_created:{challenge_id}:{target}"  # Sentinel for PM handler
```

> **Note:** The sentinel return format tells the PM handler to send a PM to the target AND a confirmation to the challenger. See Section 10.2 for details.

### 6.4 Accepting a Challenge

```python
async def accept_challenge(
    self, target: str, channel: str,
) -> tuple[str, str | None, str | None]:
    """Accept a pending challenge. Returns (pm_to_target, pm_to_challenger, public_announce)."""
    cfg = self._config.gambling.challenge
    
    # Find pending challenge where this user is the target
    challenge = await self._db.get_pending_challenge_for_target(target, channel)
    if not challenge:
        return ("No pending challenge to accept.", None, None)
    
    challenger = challenge["challenger"]
    wager = challenge["wager"]
    challenge_id = challenge["id"]
    
    # Check expiry
    expires_at = parse_timestamp(challenge["expires_at"])
    if expires_at and datetime.now(timezone.utc) > expires_at:
        await self._expire_challenge(challenge_id, challenger, channel, wager)
        return ("That challenge has expired.", None, None)
    
    # Debit target's wager
    success = await self._db.atomic_debit(target, channel, wager)
    if not success:
        return ("You can't afford the wager anymore.", None, None)
    
    # Execute the duel (coin flip, 50/50)
    challenger_wins = random.random() < 0.5
    
    # Calculate rake
    total_pot = wager * 2
    rake = int(total_pot * (cfg.rake_percent / 100))
    prize = total_pot - rake
    
    if challenger_wins:
        winner, loser = challenger, target
    else:
        winner, loser = target, challenger
    
    # Credit winner
    await self._db.credit(
        winner, channel, prize,
        tx_type="gamble_win",
        trigger_id="gambling.challenge",
        reason=f"Challenge win vs {loser}",
        related_user=loser,
        metadata=json.dumps({"rake": rake, "pot": total_pot}),
    )
    
    # Record for both players
    for player, is_winner in [(winner, True), (loser, False)]:
        player_net = prize - wager if is_winner else -wager
        await self._db.update_gambling_stats(
            player, channel, "challenge",
            net=player_net,
            biggest_win=max(0, player_net),
            biggest_loss=abs(min(0, player_net)),
        )
        await self._db.increment_lifetime_gambled(player, channel, wager, prize if is_winner else 0)
    
    # Mark challenge as accepted
    await self._db.update_challenge_status(challenge_id, "accepted")
    
    # Build messages
    winner_balance = (await self._db.get_account(winner, channel)).get("balance", 0)
    loser_balance = (await self._db.get_account(loser, channel)).get("balance", 0)
    
    target_msg = (
        f"⚔️ {'You win!' if target == winner else 'You lost!'} "
        f"{'+'  if target == winner else '-'}{wager} {self._symbol}. "
        f"{'Rake: ' + str(rake) + ' ' + self._symbol + '. ' if rake > 0 else ''}"
        f"Balance: {(winner_balance if target == winner else loser_balance)} {self._symbol}"
    )
    
    challenger_msg = (
        f"⚔️ {'You win!' if challenger == winner else 'You lost!'} "
        f"{'+'  if challenger == winner else '-'}{wager} {self._symbol}. "
        f"Balance: {(winner_balance if challenger == winner else loser_balance)} {self._symbol}"
    )
    
    # Public announcement
    public_msg = None
    if cfg.announce_public:
        public_msg = (
            f"⚔️ {winner} defeated {loser} in a {wager} {self._symbol} duel! "
            f"(Prize: {prize} {self._symbol}, Rake: {rake} {self._symbol})"
        )
    
    return (target_msg, challenger_msg, public_msg)
```

### 6.5 Declining a Challenge

```python
async def decline_challenge(
    self, target: str, channel: str,
) -> tuple[str, str | None]:
    """Decline a pending challenge. Returns (pm_to_target, pm_to_challenger)."""
    challenge = await self._db.get_pending_challenge_for_target(target, channel)
    if not challenge:
        return ("No pending challenge to decline.", None)
    
    challenger = challenge["challenger"]
    wager = challenge["wager"]
    challenge_id = challenge["id"]
    
    # Refund challenger
    await self._db.credit(
        challenger, channel, wager,
        tx_type="gamble_win",  # Refund, not a real win
        trigger_id="gambling.challenge.refund",
        reason=f"Challenge declined by {target}",
    )
    
    await self._db.update_challenge_status(challenge_id, "declined")
    
    return (
        f"Challenge declined. {challenger} has been refunded.",
        f"{target} declined your challenge. {wager} {self._symbol} refunded.",
    )
```

### 6.6 Challenge Expiry

Challenges that time out are cleaned up by a periodic check (in the scheduler, or on next interaction):

```python
async def _expire_challenge(
    self, challenge_id: int, challenger: str, channel: str, wager: int,
) -> None:
    """Expire a timed-out challenge and refund the challenger."""
    await self._db.credit(
        challenger, channel, wager,
        tx_type="gamble_win",
        trigger_id="gambling.challenge.refund",
        reason="Challenge expired",
    )
    await self._db.update_challenge_status(challenge_id, "expired")

async def cleanup_expired_challenges(self, channel: str) -> list[dict]:
    """Find and expire all timed-out pending challenges. Called periodically.
    Returns list of expired challenge info (for PM notifications)."""
    expired = await self._db.get_expired_challenges(channel)
    results = []
    for challenge in expired:
        await self._expire_challenge(
            challenge["id"], challenge["challenger"],
            channel, challenge["wager"],
        )
        results.append(challenge)
    return results
```

**Scheduler integration:** Add a periodic task to `scheduler.py` (every 60 seconds) that calls `cleanup_expired_challenges()` and notifies affected users:

```python
async def _challenge_expiry_loop(self) -> None:
    """Periodically clean up expired challenges."""
    while True:
        await asyncio.sleep(60)
        for ch_config in self._config.channels:
            channel = ch_config.channel
            expired = await self._gambling_engine.cleanup_expired_challenges(channel)
            for challenge in expired:
                await self._send_pm(
                    channel, challenge["challenger"],
                    f"Your challenge to {challenge['target']} expired. "
                    f"{challenge['wager']} {self._symbol} refunded."
                )
                await self._send_pm(
                    channel, challenge["target"],
                    f"Challenge from {challenge['challenger']} expired."
                )
```

### 6.7 Database Methods to Add

```python
async def create_pending_challenge(
    self, challenger: str, target: str, channel: str,
    wager: int, expires_at: datetime,
) -> int:
    """Create a pending challenge. Returns the challenge ID."""

async def get_pending_challenge(
    self, challenger: str, target: str, channel: str,
) -> dict | None:
    """Get pending challenge between two specific users."""

async def get_pending_challenge_for_target(
    self, target: str, channel: str,
) -> dict | None:
    """Get the oldest pending challenge where this user is the target."""

async def get_expired_challenges(self, channel: str) -> list[dict]:
    """Get all pending challenges past their expires_at."""

async def update_challenge_status(self, challenge_id: int, status: str) -> None:
    """Update challenge status (accepted, declined, expired)."""

async def update_gambling_stats(
    self, username: str, channel: str, game_type: str,
    net: int, biggest_win: int, biggest_loss: int,
) -> None:
    """Upsert gambling stats. Increment the game-type counter,
    update net, and track biggest win/loss."""

async def increment_lifetime_gambled(
    self, username: str, channel: str, wagered: int, payout: int,
) -> None:
    """Update accounts.lifetime_gambled_in += wagered, lifetime_gambled_out += payout."""

async def increment_daily_gambled(
    self, username: str, channel: str, date: str, wagered: int, payout: int,
) -> None:
    """Update daily_activity.z_gambled_in += wagered, z_gambled_out += payout."""

async def atomic_debit(
    self, username: str, channel: str, amount: int,
    tx_type: str | None = None,
    trigger_id: str | None = None,
    reason: str | None = None,
) -> bool:
    """Atomically debit amount from balance. Returns False if insufficient funds.
    Uses: UPDATE accounts SET balance = balance - ? WHERE username=? AND channel=? AND balance >= ?
    
    If tx_type is provided, also logs a transaction row in the same commit.
    This avoids the caller needing a separate record_transaction() call.
    """
```

---

## 7. Daily Free Spin

### 7.1 Config

```yaml
gambling:
  daily_free_spin:
    enabled: true
    equivalent_wager: 50
```

### 7.2 Logic

Every user gets one free spin per calendar day. The spin behaves exactly like a regular spin but costs nothing. The "wager" for payout calculation is the `equivalent_wager`.

```python
async def daily_free_spin(self, username: str, channel: str) -> GambleResult:
    """Execute a daily free spin."""
    cfg = self._config.gambling.daily_free_spin
    
    if not cfg.enabled:
        return GambleResult(
            outcome=GambleOutcome.LOSS, wager=0, payout=0, net=0,
            display="", announce_public=False, message="Free spins are disabled.",
        )
    
    # Check if already used today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    activity = await self._db.get_or_create_daily_activity(username, channel, today)
    if activity.get("free_spin_used"):
        return GambleResult(
            outcome=GambleOutcome.LOSS, wager=0, payout=0, net=0,
            display="", announce_public=False,
            message="You've already used your free spin today. Come back tomorrow!",
        )
    
    # Roll using equivalent wager
    wager = cfg.equivalent_wager
    roll = random.random()
    result_entry = self._resolve_payout(roll)
    payout = int(wager * result_entry.multiplier)
    
    # Mark as used
    await self._db.mark_free_spin_used(username, channel, today)
    
    # Credit payout (no debit — it's free)
    if payout > 0:
        await self._db.credit(
            username, channel, payout,
            tx_type="gamble_win",
            trigger_id="gambling.free_spin",
            reason=f"Free spin: {result_entry.symbols}",
        )
    
    display = result_entry.symbols if result_entry.symbols not in ("partial", "loss") else self._generate_loss_display(result_entry.symbols)
    
    account = await self._db.get_account(username, channel)
    balance = account.get("balance", 0) if account else 0
    
    if payout > 0:
        message = f"🎁🎰 {display} — FREE SPIN WIN! +{payout} {self._symbol}. Balance: {balance} {self._symbol}"
    else:
        message = f"🎁🎰 {display} — No luck on the free spin. Try again tomorrow!"
    
    announce = (
        self._config.gambling.spin.announce_jackpots_public
        and payout >= self._config.gambling.spin.jackpot_announce_threshold
    )
    
    return GambleResult(
        outcome=GambleOutcome.WIN if payout > 0 else GambleOutcome.LOSS,
        wager=0, payout=payout, net=payout,
        display=display, announce_public=announce, message=message,
    )
```

### 7.3 Database Method to Add

```python
async def mark_free_spin_used(self, username: str, channel: str, date: str) -> None:
    """Set free_spin_used = 1 in daily_activity for today."""
```

### 7.4 PM Command

`spin` with no argument triggers the daily free spin (if available). `spin <amount>` triggers a paid spin.

```python
async def _handle_spin(self, username: str, channel: str, args: str) -> str:
    args = args.strip()
    
    if not args:
        # No wager specified — try free spin first
        result = await self._gambling_engine.daily_free_spin(username, channel)
        if "already used" in result.message:
            return f"Usage: spin <wager> (min {self._config.gambling.spin.min_wager}). " + result.message
        return result.message
    
    try:
        wager = int(args)
    except ValueError:
        return f"Usage: spin <wager> (e.g. 'spin 50')"
    
    result = await self._gambling_engine.spin(username, channel, wager)
    
    # Handle public announcement
    if result.announce_public:
        await self._announce_public(channel, result)
    
    return result.message
```

---

## 8. Heist (Gated)

### 8.1 Config

```yaml
gambling:
  heist:
    enabled: false               # Stretch goal — disabled by default
    min_participants: 3
    join_window_seconds: 120
    success_chance: 0.40
    payout_multiplier: 1.5
    announce_public: true
```

### 8.2 Overview

The heist is a group gamble. One user starts it, others join within a time window. After the window closes, a single roll determines success or failure for everyone.

### 8.3 Heist State

```python
@dataclass
class ActiveHeist:
    channel: str
    initiator: str
    participants: dict[str, int]  # username → wager
    started_at: datetime
    expires_at: datetime
```

Stored in-memory on `GamblingEngine`:
```python
self._active_heists: dict[str, ActiveHeist] = {}  # channel → active heist
```

### 8.4 Starting a Heist

```python
async def start_heist(self, username: str, channel: str, wager: int) -> str:
    """Start a new heist. Returns PM response."""
    cfg = self._config.gambling.heist
    
    if not cfg.enabled:
        return "Heists are currently disabled."
    
    if channel in self._active_heists:
        return "A heist is already in progress! Use 'heist join' to join."
    
    # Validate & debit
    error = await self._validate_gamble(
        username, channel, wager, "heist",
        self._config.gambling.spin.min_wager,  # Reuse spin min for now
        self._config.gambling.spin.max_wager,
        0, None,
    )
    if error:
        return error
    
    success = await self._db.atomic_debit(username, channel, wager)
    if not success:
        return "Insufficient funds."
    
    now = datetime.now(timezone.utc)
    self._active_heists[channel] = ActiveHeist(
        channel=channel,
        initiator=username,
        participants={username: wager},
        started_at=now,
        expires_at=now + timedelta(seconds=cfg.join_window_seconds),
    )
    
    return f"heist_started:{channel}"  # Sentinel for PM handler to announce
```

### 8.5 Joining a Heist

```python
async def join_heist(self, username: str, channel: str, wager: int) -> str:
    """Join an active heist."""
    cfg = self._config.gambling.heist
    
    if channel not in self._active_heists:
        return "No active heist. Start one with 'heist <wager>'."
    
    heist = self._active_heists[channel]
    
    if username in heist.participants:
        return "You're already in this heist."
    
    if datetime.now(timezone.utc) > heist.expires_at:
        return "The join window has closed."
    
    success = await self._db.atomic_debit(username, channel, wager)
    if not success:
        return "Insufficient funds."
    
    heist.participants[username] = wager
    return f"You joined the heist! ({len(heist.participants)} participants so far)"
```

### 8.6 Resolving a Heist

Called by the scheduler after the join window expires:

```python
async def resolve_heist(self, channel: str) -> tuple[str, list[str]] | None:
    """Resolve an active heist. Returns (public_message, [participant_usernames]) or None."""
    cfg = self._config.gambling.heist
    
    if channel not in self._active_heists:
        return None
    
    heist = self._active_heists.pop(channel)
    
    if len(heist.participants) < cfg.min_participants:
        # Not enough participants — refund all
        for user, wager in heist.participants.items():
            await self._db.credit(
                user, channel, wager,
                tx_type="gamble_win",
                trigger_id="gambling.heist.refund",
                reason="Heist cancelled — not enough participants",
            )
        return (
            f"🏦 Heist cancelled — only {len(heist.participants)} participants "
            f"(need {cfg.min_participants}). Everyone refunded.",
            list(heist.participants.keys()),
        )
    
    # Roll for success
    success = random.random() < cfg.success_chance
    
    if success:
        for user, wager in heist.participants.items():
            payout = int(wager * cfg.payout_multiplier)
            await self._db.credit(
                user, channel, payout,
                tx_type="gamble_win",
                trigger_id="gambling.heist",
                reason="Heist success!",
            )
            net = payout - wager
            await self._db.update_gambling_stats(
                user, channel, "heist", net=net,
                biggest_win=max(0, net), biggest_loss=0,
            )
        
        total_pot = sum(heist.participants.values())
        return (
            f"🏦💰 HEIST SUCCESS! {len(heist.participants)} participants split "
            f"{int(total_pot * cfg.payout_multiplier)} {self._symbol}!",
            list(heist.participants.keys()),
        )
    else:
        for user, wager in heist.participants.items():
            # Wager already debited — record the loss
            await self._db.update_gambling_stats(
                user, channel, "heist", net=-wager,
                biggest_win=0, biggest_loss=wager,
            )
        
        total_lost = sum(heist.participants.values())
        return (
            f"🏦🚨 HEIST FAILED! {len(heist.participants)} participants lost "
            f"{total_lost} {self._symbol} total!",
            list(heist.participants.keys()),
        )
```

### 8.7 Heist Scheduler Task

```python
async def _heist_check_loop(self) -> None:
    """Check for expired heist join windows and resolve them."""
    while True:
        await asyncio.sleep(10)  # Check every 10 seconds
        now = datetime.now(timezone.utc)
        for ch_config in self._config.channels:
            channel = ch_config.channel
            heist = self._gambling_engine.get_active_heist(channel)
            if heist and now > heist.expires_at:
                result = await self._gambling_engine.resolve_heist(channel)
                if result:
                    public_msg, participants = result
                    if self._config.gambling.heist.announce_public:
                        await self._announce_chat(channel, public_msg)
                    for user in participants:
                        await self._send_pm(channel, user, public_msg)
```

---

## 9. Gambling Stats Command

### 9.1 PM Command: `gambling` / `stats`

```python
async def _handle_gambling_stats(self, username: str, channel: str, args: str) -> str:
    """Show personal gambling statistics."""
    stats = await self._db.get_gambling_stats(username, channel)
    
    if not stats:
        return "You haven't gambled yet. Try 'spin' for a free daily spin!"
    
    net = stats.get("net_gambling", 0)
    net_display = f"+{net}" if net >= 0 else str(net)
    
    lines = [
        f"🎰 Gambling Stats for {username}:",
        f"  Spins: {stats.get('total_spins', 0)}",
        f"  Flips: {stats.get('total_flips', 0)}",
        f"  Challenges: {stats.get('total_challenges', 0)}",
        f"  Heists: {stats.get('total_heists', 0)}",
        f"  Biggest win: {stats.get('biggest_win', 0)} {self._symbol}",
        f"  Biggest loss: {stats.get('biggest_loss', 0)} {self._symbol}",
        f"  Net P&L: {net_display} {self._symbol}",
    ]
    
    return "\n".join(lines)
```

### 9.2 Database Method

```python
async def get_gambling_stats(self, username: str, channel: str) -> dict | None:
    """Return gambling stats row, or None if the user hasn't gambled."""
```

---

## 10. PM Command Registrations

### 10.1 New Commands

Add to `pm_handler.py`'s command map:

```python
"spin": self._handle_spin,
"flip": self._handle_flip,
"challenge": self._handle_challenge,
"accept": self._handle_accept,
"decline": self._handle_decline,
"heist": self._handle_heist,
"gambling": self._handle_gambling_stats,
"stats": self._handle_gambling_stats,
```

### 10.2 PM Handler Implementations

**`flip`:**
```python
async def _handle_flip(self, username: str, channel: str, args: str) -> str:
    args = args.strip()
    if not args:
        return f"Usage: flip <wager> (e.g. 'flip 100')"
    try:
        wager = int(args)
    except ValueError:
        return f"Usage: flip <wager>"
    result = await self._gambling_engine.flip(username, channel, wager)
    return result.message
```

**`challenge`:**
```python
async def _handle_challenge(self, username: str, channel: str, args: str) -> str:
    parts = args.strip().split()
    if len(parts) < 2:
        return "Usage: challenge @user <wager>"
    target = parts[0].lstrip("@")
    try:
        wager = int(parts[1])
    except ValueError:
        return "Usage: challenge @user <wager>"
    
    result = await self._gambling_engine.create_challenge(username, target, channel, wager)
    
    # Handle sentinel response for challenge creation
    if result.startswith("challenge_created:"):
        _, challenge_id, target_name = result.split(":", 2)
        cfg = self._config.gambling.challenge
        await self._send_pm(
            channel, target_name,
            f"⚔️ {username} challenges you to a {wager} {self._symbol} duel! "
            f"Reply 'accept' or 'decline' (expires in {cfg.accept_timeout_seconds}s)",
        )
        return f"Challenge sent to {target_name} for {wager} {self._symbol}. Waiting..."
    
    return result
```

**`accept`:**
```python
async def _handle_accept(self, username: str, channel: str, args: str) -> str:
    # Get challenger name BEFORE accept changes status to "accepted"
    challenge = await self._db.get_pending_challenge_for_target(username, channel)
    challenger_name = challenge["challenger"] if challenge else None
    
    target_msg, challenger_msg, public_msg = await self._gambling_engine.accept_challenge(
        username, channel,
    )
    if challenger_msg and challenger_name:
        await self._send_pm(channel, challenger_name, challenger_msg)
    if public_msg:
        await self._announce_chat(channel, public_msg)
    return target_msg
```

**`decline`:**
```python
async def _handle_decline(self, username: str, channel: str, args: str) -> str:
    # Get challenger name BEFORE decline changes status to "declined"
    challenge = await self._db.get_pending_challenge_for_target(username, channel)
    challenger_name = challenge["challenger"] if challenge else None
    
    target_msg, challenger_msg = await self._gambling_engine.decline_challenge(
        username, channel,
    )
    if challenger_msg and challenger_name:
        await self._send_pm(channel, challenger_name, challenger_msg)
    return target_msg
```

**`heist`:**
```python
async def _handle_heist(self, username: str, channel: str, args: str) -> str:
    args = args.strip().lower()
    
    if args == "join":
        # Join existing heist (use a default wager matching the initiator, or prompt)
        heist = self._gambling_engine.get_active_heist(channel)
        if not heist:
            return "No active heist to join."
        # Default to same wager as initiator
        wager = list(heist.participants.values())[0]
        return await self._gambling_engine.join_heist(username, channel, wager)
    
    if not args:
        return "Usage: heist <wager> or heist join"
    
    try:
        wager = int(args)
    except ValueError:
        return "Usage: heist <wager> or heist join"
    
    result = await self._gambling_engine.start_heist(username, channel, wager)
    
    if result.startswith("heist_started:"):
        cfg = self._config.gambling.heist
        await self._announce_chat(
            channel,
            f"🏦 {username} is planning a heist! "
            f"PM 'heist join' within {cfg.join_window_seconds}s to participate!",
        )
        return f"Heist started! Waiting {cfg.join_window_seconds}s for others to join..."
    
    return result
```

### 10.3 Update `help` Command

Add gambling commands to help text:

```
spin [wager] — Slot machine (no wager = free daily spin)
flip <wager> — Coin flip (double-or-nothing)
challenge @user <wager> — Challenge someone to a duel
accept / decline — Respond to a challenge
gambling — Your gambling stats
```

If heist is enabled:
```
heist <wager> — Start a group heist
heist join — Join an active heist
```

---

## 11. Public Announcements

### 11.1 Jackpot Announcements

When a spin result exceeds `jackpot_announce_threshold`:

```python
if result.announce_public:
    await self._announce_chat(
        channel,
        f"🎰 JACKPOT! {username} just won {result.payout} {self._symbol} on the slots!"
    )
```

### 11.2 Challenge Results

When `challenge.announce_public` is true, the duel result is posted to public chat (see Section 6.4).

### 11.3 Heist Results

When `heist.announce_public` is true, the heist outcome is posted to public chat (see Section 8.6).

### 11.4 Announcement Helper

Use kryten-py's `send_chat()` wrapper for publishing to public chat:

```python
async def _announce_chat(self, channel: str, message: str) -> None:
    """Post a message in public chat via kryten-py."""
    await self._client.send_chat(channel, message)
```

---

## 12. Anti-Abuse & Limits

### 12.1 Wager Escrow for Challenges

The challenger's wager is debited immediately on challenge creation. This prevents the "create challenge then spend balance before it resolves" exploit. The wager is either:
- Transferred to the winner (minus rake) on acceptance
- Refunded on decline or expiry

### 12.2 Atomic Debit Guard

All gambling operations use `atomic_debit()` which performs:
```sql
UPDATE accounts SET balance = balance - ? 
WHERE username = ? AND channel = ? AND balance >= ?
```
If the update affects 0 rows, the debit fails (insufficient funds). This prevents race conditions from concurrent spins.

### 12.3 Cooldowns

| Game | Cooldown |
|---|---|
| Spin | 30 seconds |
| Flip | 15 seconds |
| Challenge | None (creation), but only 1 pending per pair |
| Heist | One active per channel |

Cooldowns are tracked in-memory (`self._cooldowns` dict). On service restart, cooldowns reset — this is acceptable (minor exploit window, and users need to re-establish sessions anyway).

### 12.4 Daily Limits

| Game | Daily Limit |
|---|---|
| Spin | 50 |
| Flip | 100 |
| Free Spin | 1 (flag in daily_activity) |

Tracked via `trigger_cooldowns` table (daily window) for spins and flips; `daily_activity.free_spin_used` flag for free spin.

### 12.5 Minimum Account Age

Configured via `gambling.min_account_age_minutes` (default: 60). Prevents freshly created accounts from gambling immediately (anti-alt-farming).

### 12.6 Wager Caps

All game types have configurable `min_wager` and `max_wager`. These protect against accidental fat-finger wagers and limit maximum exposure.

---

## 13. Config Sections Activated

| Config Path | Sprint 4 Consumer |
|---|---|
| `gambling.enabled` | All gambling operations |
| `gambling.min_account_age_minutes` | `_validate_gamble()` |
| `gambling.spin.*` | `GamblingEngine.spin()` |
| `gambling.flip.*` | `GamblingEngine.flip()` |
| `gambling.challenge.*` | `GamblingEngine.create_challenge()`, `accept_challenge()`, etc. |
| `gambling.daily_free_spin.*` | `GamblingEngine.daily_free_spin()` |
| `gambling.heist.*` | `GamblingEngine.start_heist()`, `join_heist()`, `resolve_heist()` |

---

## 14. Test Specifications

### 14.1 File: `tests/test_gambling_engine.py`

Core engine tests:

| Test | Description |
|---|---|
| `test_gambling_disabled` | All games return error when `gambling.enabled = false` |
| `test_min_account_age_enforced` | New account (< 60 min old) → rejected |
| `test_min_account_age_satisfied` | Account ≥ 60 min old → allowed |
| `test_economy_banned_rejected` | Banned user → rejected |
| `test_insufficient_balance` | Balance < wager → rejected |
| `test_min_wager_enforced` | Wager below minimum → rejected |
| `test_max_wager_enforced` | Wager above maximum → rejected |
| `test_atomic_debit_prevents_overdraft` | Two concurrent spins, only balance for one → one succeeds, one fails |

### 14.2 File: `tests/test_slots.py`

| Test | Description |
|---|---|
| `test_spin_win` | Forced roll in win range → payout > wager, balance increased |
| `test_spin_loss` | Forced roll in loss range → payout = 0, balance decreased |
| `test_spin_jackpot` | Forced roll on 7️⃣7️⃣7️⃣ → 50× multiplier, announce_public = true |
| `test_spin_partial_match` | Forced roll on partial → 2× multiplier |
| `test_spin_cooldown_enforced` | Second spin within 30s → rejected |
| `test_spin_cooldown_expired` | Second spin after 30s → allowed |
| `test_spin_daily_limit` | 51st spin in a day → rejected |
| `test_spin_daily_limit_resets` | New calendar day → limit resets |
| `test_spin_payout_table_valid` | Probabilities sum to 1.0 (within tolerance) |
| `test_spin_transaction_logged_win` | Win → transaction with type="gamble_win" |
| `test_spin_transaction_logged_loss` | Loss → transaction with type="gamble_loss" |
| `test_spin_gambling_stats_updated` | total_spins incremented, net_gambling updated |
| `test_spin_display_jackpot_symbols` | Jackpot → display shows "7️⃣7️⃣7️⃣" |
| `test_spin_display_loss_random` | Loss → display shows random mixed symbols |
| `test_jackpot_announce_threshold` | Payout < threshold → no announcement |

### 14.3 File: `tests/test_flip.py`

| Test | Description |
|---|---|
| `test_flip_win` | Forced random < 0.45 → doubled wager |
| `test_flip_loss` | Forced random ≥ 0.45 → lost wager |
| `test_flip_cooldown_enforced` | Second flip within 15s → rejected |
| `test_flip_daily_limit` | 101st flip → rejected |
| `test_flip_balance_updates` | Win: +wager. Loss: -wager |
| `test_flip_gambling_stats_updated` | total_flips incremented |

### 14.4 File: `tests/test_challenge.py`

| Test | Description |
|---|---|
| `test_create_challenge_success` | Valid challenge → pending row created, challenger debited |
| `test_challenge_self_rejected` | Challenge self → error |
| `test_challenge_ignored_user_rejected` | Challenge bot → error |
| `test_challenge_target_insufficient_balance` | Target can't afford → error |
| `test_challenge_duplicate_rejected` | Existing pending → error |
| `test_accept_challenge_success` | Accept → both debited, winner credited (minus rake), loser gets nothing |
| `test_accept_challenge_expired` | Accept after timeout → "expired" + refund |
| `test_decline_challenge_refund` | Decline → challenger refunded, both notified |
| `test_challenge_rake_calculated` | 5% of (wager × 2) removed from pool |
| `test_challenge_result_announced` | announce_public = true → public message returned |
| `test_challenge_expiry_cleanup` | Expired challenges auto-refund and notify |
| `test_challenge_no_pending` | Accept with no pending → error |
| `test_accept_target_insufficient_now` | Target could afford at creation but not now → error |

### 14.5 File: `tests/test_free_spin.py`

| Test | Description |
|---|---|
| `test_free_spin_win` | Free spin in win range → payout credited, no debit |
| `test_free_spin_loss` | Free spin in loss range → no debit, no credit |
| `test_free_spin_once_per_day` | Second free spin same day → rejected |
| `test_free_spin_resets_daily` | New day → eligible again |
| `test_free_spin_disabled` | Config disabled → error |
| `test_free_spin_via_spin_command` | `spin` with no args → free spin (if available) |
| `test_spin_without_args_after_free_used` | `spin` no args, free used → prompt for wager |

### 14.6 File: `tests/test_heist.py`

| Test | Description |
|---|---|
| `test_heist_disabled` | Config disabled → error |
| `test_start_heist` | Valid → heist created, initiator debited, announcement returned |
| `test_join_heist` | Join active heist → participant added, debited |
| `test_join_heist_already_in` | Already participating → error |
| `test_join_heist_expired_window` | Join after window → error |
| `test_heist_success` | Random < 0.4 → all participants get wager × 1.5 |
| `test_heist_failure` | Random ≥ 0.4 → all participants lose wager |
| `test_heist_insufficient_participants` | < 3 participants → cancelled, all refunded |
| `test_heist_one_per_channel` | Start second heist while one active → error |
| `test_heist_stats_recorded` | total_heists incremented for all participants |

### 14.7 File: `tests/test_gambling_stats.py`

| Test | Description |
|---|---|
| `test_stats_no_gambling` | No prior gambling → friendly message |
| `test_stats_after_spin` | After a spin → total_spins = 1 |
| `test_stats_net_positive` | Wins > losses → positive net displayed |
| `test_stats_net_negative` | Losses > wins → negative net displayed |
| `test_biggest_win_tracked` | Largest win recorded |
| `test_biggest_loss_tracked` | Largest loss recorded |
| `test_stats_combines_all_games` | Spins + flips + challenges → all totals shown |

### 14.8 File: `tests/conftest.py` Additions

```python
@pytest.fixture
def gambling_engine(config, test_db):
    """GamblingEngine with test config and database."""
    return GamblingEngine(config, test_db, logging.getLogger("test"))

@pytest.fixture
def mock_random():
    """Fixture to control random.random() output for deterministic testing."""
    with patch("random.random") as mock:
        yield mock
```

> **Testing strategy for randomness:** Use `unittest.mock.patch("random.random")` to force specific outcomes. For spin tests, force the roll value to land in known probability ranges. For flip tests, force < 0.45 (win) or ≥ 0.45 (loss).

---

## 15. Acceptance Criteria

### Slot Machine

- [ ] `spin <wager>` deducts wager, rolls against payout table, credits payout on win
- [ ] Jackpot results (≥ threshold) trigger public chat announcement
- [ ] 30-second cooldown enforced between spins
- [ ] 50 spin daily limit enforced
- [ ] Loss display shows random symbols; partial shows 2 matching + 1 different
- [ ] Transaction logged with `gamble_win` or `gamble_loss` type

### Coin Flip

- [ ] `flip <wager>` — 45% win chance, double-or-nothing
- [ ] 15-second cooldown enforced
- [ ] 100 flip daily limit enforced

### Challenge

- [ ] `challenge @user <wager>` creates pending challenge, debits challenger (escrow)
- [ ] Target receives PM invitation with accept/decline options
- [ ] `accept` triggers duel, distributes pot (minus 5% rake)
- [ ] `decline` refunds challenger
- [ ] Expired challenges auto-refund (within 60 seconds of expiry)
- [ ] Public announcement on challenge result (when configured)
- [ ] Self-challenge rejected; ignored-user challenge rejected

### Daily Free Spin

- [ ] `spin` with no argument triggers free spin (once per day)
- [ ] Free spin uses equivalent_wager for payout calculation but costs 0 Z
- [ ] Second free spin same day → rejected with friendly message
- [ ] After free spin used, `spin` (no args) prompts for wager

### Heist (Gated)

- [ ] Heist disabled by default (`enabled: false`)
- [ ] When enabled: start → join window → resolve → announce
- [ ] Insufficient participants → cancel and refund
- [ ] Success → all participants get wager × multiplier
- [ ] Failure → all participants lose wager

### Stats & Recording

- [ ] `gambling` / `stats` command shows personal record
- [ ] `gambling_stats` table updated after every gambling action
- [ ] `accounts.lifetime_gambled_in` and `lifetime_gambled_out` updated
- [ ] `daily_activity.z_gambled_in` and `z_gambled_out` updated
- [ ] `daily_activity.free_spin_used` flag set correctly

### Anti-Abuse

- [ ] Minimum account age enforced before any gambling
- [ ] Wager min/max enforced per game type
- [ ] Atomic debit prevents overdraft on concurrent plays
- [ ] Challenge escrow prevents spend-after-challenge exploit

### PM Commands

- [ ] `spin`, `flip`, `challenge`, `accept`, `decline`, `heist`, `gambling`/`stats` all registered
- [ ] `help` updated with gambling commands
- [ ] All PM responses include current balance

### Tests

- [ ] All new test files pass (`pytest` exits 0)
- [ ] At least 60 test cases across the 7 test files

---

*End of Sprint 4 specification. This document is self-contained and sufficient for an AI coding agent to implement the full sprint given a completed Sprint 1 codebase.*
