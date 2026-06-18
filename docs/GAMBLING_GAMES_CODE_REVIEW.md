# Code Review & Remediation Spec — Spectacle Gambling Games

**Scope:** New code added for Race Betting, Trivia Gamble, Blackjack Lite, and the SpectacleManager.
**Files reviewed:** `race_engine.py`, `race_narrator.py`, `race_narratives.py`, `trivia_engine.py`, `trivia_client.py`, `blackjack_engine.py`, `spectacle_manager.py`, plus new additions in `pm_handler.py`, `scheduler.py`, `main.py`.
**Verdict:** Functionally correct and well-tested (63 new tests pass), but carries measurable "slop" and drift from house conventions. Two functional gaps, several DRY violations, and dead/unimplemented surface area.

> **Status (resolved):** All findings below have been remediated on branch
> `feature/spectacle-gambling-games`. A shared `gambling_common.py` now backs
> account validation and daily-count tracking; Blackjack enforces its cooldown
> and daily limit; Trivia's join path enforces the account-age gate; date
> helpers from `utils.py` are reused; the Race payout is computed once; race
> balance values are named constants; and dead code / unused imports were
> cleaned up. Regression tests were added for H1, H2, and M4.
>
> **Update (M3 → track B implemented):** rather than removing the Race LLM
> surface, the full **static / LLM / hybrid** commentary feature was
> implemented (see *Fix 4* below) — `RaceNarrator` now ports
> `_generate_llm_story` / `prepare_story` / cached-story consumption from
> `HeistNarrator`, stories are generated per-channel and pre-fetched during the
> betting window, and `config` + docstrings + `config.example.yaml` agree.

---

## Part 1 — Findings

### HIGH — Functional gaps

#### H1. Blackjack ignores its own `cooldown_seconds` and `daily_limit`
`BlackjackConfig` declares `cooldown_seconds: 10` and `daily_limit: 50`, but `blackjack_engine.py` never reads or enforces either. Because Blackjack is **PM-only** (deliberately *not* governed by `SpectacleManager`), there is currently **no rate limit at all** — a user can deal unlimited back-to-back hands. This is both dead/misleading config and an abuse vector.
- Evidence: `grep "cooldown_seconds|daily_limit"` in `blackjack_engine.py` → 0 matches.
- Contrast: the existing `GamblingEngine._validate_gamble()` enforces both for spin/flip.

#### H2. Trivia `place_bet()` skips the account-age gate that `start_trivia()` enforces
`start_trivia()` checks `min_account_age_minutes`; `place_bet()` (used when *joining* an in-progress round, including via `!trivia <wager>` in chat) does not. A too-new account cannot start trivia but can join one — inconsistent gating and a minor bypass.
- Evidence: `trivia_engine.start_trivia` has the `first_seen`/`min_age` block; `trivia_engine.place_bet` omits it and also uses a shorter "You need an account first." message.

### MEDIUM — DRY violations & drift

#### M1. Account-validation block duplicated 4× (and divergent)
The same account → `economy_banned` → `min_account_age` → balance sequence is copy-pasted into `race_engine.place_bet`, `trivia_engine.start_trivia`, `trivia_engine.place_bet`, and `blackjack_engine.deal`. Copies have drifted (trivia's join path drops the age check; error strings differ). The existing `GamblingEngine._validate_gamble()` already implements this and is **not reused**.

#### M2. Re-implemented date/`now` helpers that already exist in `utils.py`
`datetime.now(timezone.utc).strftime("%Y-%m-%d")` is repeated ~5× across the new engines, even though `utils.today_str()` already exists and does exactly this. `datetime.now(timezone.utc)` is likewise repeated where `utils.now_utc()` exists.
- Locations: `race_engine.resolve_race` (×3), `trivia_engine.resolve_trivia` (×1, in a loop), `blackjack_engine._resolve` (×1).

#### M3. `race_narrator.py` advertises LLM/hybrid commentary that is never implemented
`RaceCommentaryConfig` exposes `mode: static|llm|hybrid` plus a full `llm:` block (`HeistLLMConfig`), and the class docstring says "Supports three modes (mirroring HeistNarrator)." In reality only static pickers exist: `RaceStory` and `self._cached_story` are declared but never populated; there is no `prepare_story()` / `_generate_llm_story()`. Setting `mode: hybrid` silently produces static-only output with no warning.
- This is scope drift: config + docstring promise a feature the engine doesn't deliver.

#### M4. Payout recomputed purely for the display line in `resolve_race`
Winner payouts are computed when crediting, then **recomputed** in a nested `for wb in winning_bets` loop solely to build the "Winners:" string (with a dead `pm = per_user_pm.get(...)` assignment alongside). Two sources of truth for the same number → divergence risk on any future formula/rounding change.

#### M5. Game-balance magic numbers hardcoded (house style is config-driven)
`race_engine.py` hardcodes values the rest of the system would put in config/constants:
- Event-type weights `weights=[35, 30, 15, 20]`
- Trait multipliers `1.5`, `0.85`, `0.75`, `1.5`, wildcard `random.uniform(0.5, 2.0)`
- Shortcut bonus `cfg.finish_distance * 0.15`
- Buff durations (`speed_buff_ticks = 2`, `frozen_ticks = 1`) and close-finish thresholds (`0.05`, `0.7`)

### LOW — Cleanup

| ID | Issue | Location |
|----|-------|----------|
| L1 | Unused imports `field`, `Any` | `race_engine.py` |
| L2 | Unused imports `json`, `Any`; dead `RaceStory`/`_cached_story` | `race_narrator.py` |
| L3 | Unused import `field` | `trivia_engine.py` |
| L4 | Unused import `timedelta` | `blackjack_engine.py` |
| L5 | Unused imports `field`, `timedelta` | `spectacle_manager.py` |
| L6 | Unused local `racer = race.racers[color_match]` | `race_engine.place_bet` |
| L7 | Dead `elif total_on_winner == 0: pass` branch | `race_engine.resolve_race` |
| L8 | Dataclass field `ActiveRace.race_started_at` set but never read | `race_engine.py` |
| L9 | Silent `except (ValueError, IndexError): pass` — no user feedback on malformed `!race`/`!trivia` | `main.py` chat router |

---

## Part 2 — Remediation Spec

### Fix 1 — Centralize gambling validation (resolves H2, M1; enables H1)

Create `kryten_economy/gambling_common.py` as the single source of truth for pre-wager validation, reused by all engines.

```python
"""Shared pre-wager validation for all gambling engines."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .utils import now_utc, parse_timestamp

if TYPE_CHECKING:
    from .config import GamblingConfig
    from .database import EconomyDatabase

async def validate_gamble_account(
    db: EconomyDatabase,
    gambling_cfg: GamblingConfig,
    symbol: str,
    username: str,
    channel: str,
    wager: int,
    *,
    require_age: bool = True,
) -> str | None:
    """Return an error string, or None if the wager may proceed.

    Centralizes: enabled gate, account existence, economy ban,
    min account age, and balance check. Mirrors GamblingEngine._validate_gamble.
    """
    if not gambling_cfg.enabled:
        return "Gambling is currently disabled."
    account = await db.get_account(username, channel)
    if not account:
        return "You need an account first. Stick around a bit!"
    if account.get("economy_banned"):
        return "Your economy access is restricted."
    if require_age:
        min_age = gambling_cfg.min_account_age_minutes
        first_seen = parse_timestamp(account.get("first_seen"))
        if first_seen:
            age_minutes = (now_utc() - first_seen).total_seconds() / 60
            if age_minutes < min_age:
                remaining = int(min_age - age_minutes)
                return f"You need to be around for {remaining} more minutes before gambling."
    if account.get("balance", 0) < wager:
        return f"Insufficient funds. Balance: {account['balance']} {symbol}."
    return None
```

**Apply:**
- `race_engine.place_bet`, `trivia_engine.start_trivia`, `trivia_engine.place_bet`, `blackjack_engine.deal`: replace the inline block with a call to `validate_gamble_account(...)`. This **adds the missing age check to trivia join** (H2) automatically.
- **Recommended (single source of truth):** refactor `GamblingEngine._validate_gamble` to delegate its account/age/balance portion to this helper, keeping its cooldown/daily-limit logic local. If the blast radius is a concern, at minimum keep the messages identical so the two implementations don't diverge further.

### Fix 2 — Enforce Blackjack cooldown + daily limit (resolves H1)

Blackjack must self-rate-limit since it is not under `SpectacleManager`. Reuse the existing DB trigger-cooldown plumbing (`db.get_trigger_cooldown` / `set_trigger_cooldown` / `increment_trigger_cooldown`) exactly as `GamblingEngine._get_daily_game_count` / `_increment_daily_game_count` do.

In `BlackjackEngine.__init__`:
```python
self._cooldowns: dict[tuple[str, str], datetime] = {}  # (user_lower, channel) -> last deal
```

In `deal()`, after `validate_gamble_account(...)` and before `atomic_debit`:
```python
cfg = self._config.gambling.blackjack
key = (username.lower(), channel)
last = self._cooldowns.get(key)
if last and (now_utc() - last).total_seconds() < cfg.cooldown_seconds:
    remaining = int(cfg.cooldown_seconds - (now_utc() - last).total_seconds())
    return f"Cooldown: {remaining}s remaining."

count_today = await self._get_daily_count(username, channel)
if count_today >= cfg.daily_limit:
    return f"Daily blackjack limit reached ({cfg.daily_limit}/day)."
```

On a successful deal, set `self._cooldowns[key] = now_utc()` and increment the daily counter. Implement `_get_daily_count` / `_increment_daily_count` with `trigger_id = "gambling.blackjack.daily"`, mirroring the existing GamblingEngine helpers.

> **DRY note:** the daily-count helpers are themselves duplicated from `GamblingEngine`. Preferred: hoist `get_daily_game_count` / `increment_daily_game_count` into `gambling_common.py` (or onto `EconomyDatabase`) and have both `GamblingEngine` and `BlackjackEngine` call them.

### Fix 3 — Use existing date helpers (resolves M2)

Import and use the existing helpers everywhere in the new engines:
```python
from .utils import today_str, now_utc
```
- Replace every `datetime.now(timezone.utc).strftime("%Y-%m-%d")` → `today_str()`.
- Replace bare `datetime.now(timezone.utc)` → `now_utc()` where a timestamp is needed.
- In `race_engine.resolve_race`, compute `today = today_str()` **once** at the top of the method rather than three times.

### Fix 4 — Resolve the Race narrator LLM drift (resolves M3)

Pick one:
- **(A) Pragmatic — remove dead surface.** Delete `RaceStory`, `self._cached_story`, the `import json`/`Any`, and the "three modes" docstring claim. In `RaceCommentaryConfig`, drop `llm` and reduce `mode` to a comment that only `static` is supported (keep `max_lines_per_race`). Update `config.example.yaml` to match.
- **(B) Feature-complete — implement it.** Port `HeistNarrator._generate_llm_story` / `prepare_story` / cached-story consumption into `RaceNarrator`, and call `prepare_story()` at race start in the scheduler before the first commentary line.

Recommend **(A)** now; track **(B)** as a follow-up feature. Whichever is chosen, config + docstring + implementation must agree.

> **Resolved via track (B).** `RaceNarrator` now supports static / LLM / hybrid
> modes: `_generate_llm_story()` + `prepare_story()` are ported from
> `HeistNarrator`, with a dedicated `RaceLLMConfig` and a race-specific system
> prompt (JSON keys `start`/`lead_change`/`event`/`finish`). To stay correct
> under concurrency and avoid re-introducing the serial-loop stall flagged in
> review, stories are cached **per channel** and `prepare_story()` is kicked off
> as a background task during the betting window (so the themed story is ready
> before the first commentary line without blocking other channels). Stories are
> consumed when the race resolves or is cancelled; static remains the fallback.
> Docstrings and `config.example.yaml` were updated to match.

### Fix 5 — Single payout source in `resolve_race` (resolves M4, L7)

Refactor to compute each winner's payout once, store it, then reuse for crediting, PM text, and the public "Winners:" line.

```python
# Compute once
payouts: list[tuple[RaceBet, int, int]] = []  # (bet, payout, net)
for bet in winning_bets:
    if cfg.odds_mode == "pool":
        payout = int(distributable * (bet.amount / total_on_winner))
    else:  # fixed
        odds = (1.0 / winner.win_chance) if winner.win_chance > 0 else 1.0
        payout = int(bet.amount * odds * (1.0 - cfg.house_rake_pct))
    payouts.append((bet, payout, payout - bet.amount))

# Credit + stats from `payouts`; build "Winners:" line from `payouts`.
```
Remove the dead `elif total_on_winner == 0: pass` branch (L7) — the loserbets loop already handles the "nobody won" case, and the public message is appended separately.

### Fix 6 — Hoist race-balance constants (resolves M5)

At minimum, move magic numbers to module-level named constants at the top of `race_engine.py`:
```python
EVENT_TYPE_WEIGHTS = (35, 30, 15, 20)  # speed_boost, stumble, mudslide, shortcut
SHORTCUT_BONUS_PCT = 0.15
SPEED_BOOST_TICKS, SPEED_BOOST_MULT = 2, 2.0
MUDSLIDE_MULT = 0.5
CLOSE_FINISH_GAP_PCT, CLOSE_FINISH_MIN_PROGRESS_PCT = 0.05, 0.70
TRAIT_MULT = {  # (early, late) where applicable
    "sprinter": (1.5, 0.85),
    "closer": (0.75, 1.5),
    "wildcard": (0.5, 2.0),
}
```
Ideally promote the most balance-relevant ones (`EVENT_TYPE_WEIGHTS`, `SHORTCUT_BONUS_PCT`) to `RaceConfig` so they're tunable without a code change, consistent with the rest of the system.

### Fix 7 — Cleanup (resolves L1–L6, L8, L9)

- Remove unused imports: `field`/`Any` (`race_engine`), `json`/`Any` (`race_narrator`, also covered by Fix 4), `field` (`trivia_engine`), `timedelta` (`blackjack_engine`), `field`/`timedelta` (`spectacle_manager`).
- Remove the unused `racer = race.racers[color_match]` local in `race_engine.place_bet` (L6).
- Remove `ActiveRace.race_started_at` unless a concrete use (telemetry/logging) is added (L8).
- `main.py` chat router (L9): on `except (ValueError, IndexError)`, send a one-line PM, e.g. `"Usage: !race <amount> <color>"`, instead of silently passing.

---

## Suggested order of work
1. Fix 1 (shared validation) + Fix 2 (blackjack limits) — closes both HIGH gaps.
2. Fix 3 (date helpers) + Fix 5 (single payout) + Fix 7 (cleanup) — mechanical, low risk.
3. Fix 4 (narrator drift) — decide A vs B.
4. Fix 6 (constants/config) — balance-tuning quality of life.

## Verification
- Existing 63 new tests must continue to pass.
- Add tests: blackjack cooldown enforced; blackjack daily limit enforced; trivia **join** rejects a too-new account (H2 regression guard); `resolve_race` winners-line amount equals credited amount (M4 guard).
- `python -m pytest tests/test_race_engine.py tests/test_trivia_engine.py tests/test_blackjack_engine.py tests/test_spectacle_manager.py tests/test_trivia_client.py -v`
