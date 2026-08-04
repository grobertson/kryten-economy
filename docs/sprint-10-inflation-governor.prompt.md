# Sprint 10 — Inflation Governor: Float-Tied Pricing

> **Parent plan:** `kryten-economy-plan.md` · **Sprint:** 10 (post-launch feature)
> **Goal:** Make all spend-sink prices self-regulating by tying them to the total coin float.
> As the economy grows, purchasing power naturally scales with it; as players spend
> (destroying coins), prices soften — creating a closed-loop regulator with no manual
> intervention required.
> **Depends on:** All prior sprints (1–9) — spending engine, DB snapshots, metrics
> **Enables:** Long-term float stability without periodic manual redenominations

---

## Table of Contents

1. [Background & Design Rationale](#1-background--design-rationale)
2. [Deliverable Summary](#2-deliverable-summary)
3. [Config Activation](#3-config-activation)
4. [FloatPriceScaler Component](#4-floatpricescaler-component)
5. [SpendingEngine Changes](#5-spendingengine-changes)
6. [PM Display Changes](#6-pm-display-changes)
7. [Admin PM Command: `inflation`](#7-admin-pm-command-inflation)
8. [NATS Command Extension: `stats.inflation`](#8-nats-command-extension-statsinflation)
9. [Metrics Extension](#9-metrics-extension)
10. [Snapshot Extension](#10-snapshot-extension)
11. [EconomyApp Wiring](#11-economyapp-wiring)
12. [config.example.yaml Documentation](#12-configexampleyaml-documentation)
13. [Test Specifications](#13-test-specifications)
14. [Acceptance Criteria](#14-acceptance-criteria)

---

## 1. Background & Design Rationale

### The Problem

The channel's coin supply grows monotonically over time. Presence payouts, gambling wins,
daily streaks, and chat rewards inject new coins continuously. Spending sinks (queue
purchases, vanity shop items) burn coins, but the net flow has historically been inflationary.
At 800M+ Z in circulation, nominal prices feel small and the numbers are getting unwieldy.

### Why Float-Tied Pricing (Option A)

| Property | Float-Tied Pricing | Manual Redenomination |
|---|---|---|
| Requires maintenance | No — self-regulates | Yes — periodic admin action |
| Preserves relative wealth | Yes | Yes |
| Affects new earners fairly | Yes — same proportional cost | Depends on timing |
| UX clarity | Good with `inflation` command | Brief but disorienting |
| Covers future growth | Permanently | Until next devaluation |

### Mechanism

Every spend-sink price in the economy is expressed as a **base cost** (what the operator
configured). At runtime, the **inflation multiplier** is layered on top:

```
inflation_multiplier = clamp(current_float / anchor_float, min_multiplier, max_multiplier)
effective_price      = round(base_cost × inflation_multiplier)
final_price          = apply_rank_discount(effective_price, rank_tier)
```

- **`anchor_float`** is the operator-chosen "healthy" float level (e.g. 80M Z after a
  one-time redenomination, or wherever the operator wants prices to feel "normal").
- When the float equals the anchor, the multiplier is exactly 1.0 — prices are at their
  configured base values.
- When the float is 4× the anchor, prices are 4× more expensive, making spend-sinks drain
  4× as much coin per transaction — accelerating deflation automatically.
- When the float shrinks below the anchor (over-deflation), `min_multiplier` floors the
  multiplier so prices don't collapse to near-zero.
- `max_multiplier` caps the multiplier to prevent absurdly unaffordable prices if the float
  balloons unexpectedly (e.g. due to a bug).

### What Is Affected

Only **spend-sinks** (coin destruction events) scale with inflation:
- Queue tier costs (movie/episode/short queuing)
- `interrupt_play_next` / `force_play_now` premium actions
- All vanity shop items (chat color, custom greeting, channel GIF, shoutout, daily fortune,
  custom title, personal currency rename)

What is **NOT** affected by inflation:
- Tipping (coin redistribution, not destruction)
- Gambling wagers / payouts (these are governed by their own balance mechanics)
- Earning rates (presence, chat, streaks, bounties, competition rewards)

### Self-Regulation Loop

```
float rises → multiplier rises → spend-sinks cost more Z → more Z destroyed per tx
float falls → multiplier falls → spend-sinks cost fewer Z → less Z destroyed per tx
```

The loop dampens extremes in both directions without manual intervention.

> **⚠️ Ecosystem rule:** All NATS interaction goes through kryten-py's `KrytenClient`.
> No direct NATS access. No raw nats-py.

---

## 2. Deliverable Summary

At the end of this sprint:

- A new **`FloatPriceScaler`** component holds the current inflation multiplier, refreshes
  it periodically from `db.get_total_circulation()`, and exposes `scale(base_cost) → int`
- **`SpendingEngine`** is updated so `get_price_tier()`, `interrupt_play_next`,
  `force_play_now`, and all vanity shop prices go through the scaler before rank discount
- **`PmHandler`** `shop` and `queue` confirmation flows display the live effective price
  when inflation is active; the multiplier is shown when it differs meaningfully from 1.0
- A new **`inflation`** admin PM command shows the current multiplier, anchor, live float,
  and sample prices at current rate
- A new **`stats.inflation`** NATS command returns the same data for the API gate
- A **`economy_inflation_multiplier`** Prometheus gauge is emitted
- The 6-hourly economy snapshot records the current multiplier
- The feature is **opt-in** — `enabled: false` by default; all behaviour is identical to
  pre-Sprint-10 when disabled
- All existing tests continue to pass; new tests cover the scaler, spending integration,
  and command outputs

---

## 3. Config Activation

### 3.1 New Pydantic Models in `kryten_economy/config.py`

Add after `BalanceMaintenanceConfig` (which already handles interest/decay), before the
Sprint 3 section:

```python
# ═══════════════════════════════════════════════════════════════
#  Sprint 10 — Inflation Governor
# ═══════════════════════════════════════════════════════════════


class InflationConfig(BaseModel):
    """Float-tied pricing: effective_price = base × clamp(float/anchor, min, max)."""

    enabled: bool = Field(
        default=False,
        description="Enable float-tied pricing. When False, all prices are at base values.",
    )
    anchor_float: int = Field(
        default=100_000_000,
        ge=1,
        description=(
            "The 'healthy' float level at which inflation_multiplier == 1.0. "
            "Set this to the target total circulation you consider normal."
        ),
    )
    min_multiplier: float = Field(
        default=0.25,
        ge=0.01,
        le=1.0,
        description="Floor multiplier. Prices never fall below this fraction of base cost.",
    )
    max_multiplier: float = Field(
        default=8.0,
        ge=1.0,
        description="Ceiling multiplier. Prices never exceed this multiple of base cost.",
    )
    update_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often FloatPriceScaler refreshes the live float from the DB.",
    )
```

### 3.2 Wire into `EconomyConfig`

`EconomyConfig` already has a `balance_maintenance` field. Add `inflation` alongside it:

```python
class EconomyConfig(BaseModel):
    ...
    balance_maintenance: BalanceMaintenanceConfig = Field(
        default_factory=BalanceMaintenanceConfig
    )
    inflation: InflationConfig = Field(default_factory=InflationConfig)
    ...
```

> **Placement:** add the `inflation` field immediately after `balance_maintenance` in
> `EconomyConfig`. Alphabetical ordering is not required; proximity to related fields is.

### 3.3 Config Validation

`InflationConfig.min_multiplier` must be ≤ 1.0 and `max_multiplier` must be ≥ 1.0. The
Pydantic `ge`/`le` annotations above enforce this. Additionally add a model validator:

```python
from pydantic import model_validator

class InflationConfig(BaseModel):
    ...

    @model_validator(mode="after")
    def _validate_bounds(self) -> "InflationConfig":
        if self.min_multiplier > self.max_multiplier:
            raise ValueError(
                f"inflation.min_multiplier ({self.min_multiplier}) "
                f"must not exceed max_multiplier ({self.max_multiplier})"
            )
        return self
```

---

## 4. FloatPriceScaler Component

### 4.1 File: `kryten_economy/float_price_scaler.py`

```python
"""Float-Tied Price Scaler — Sprint 10.

Holds the live inflation multiplier and exposes ``scale(base_cost) → int``.
The multiplier is refreshed periodically from the DB rather than computed on
every spend call so that spending remains O(1) without a DB hit per transaction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import EconomyConfig, InflationConfig
    from .database import EconomyDatabase


class FloatPriceScaler:
    """Maintains the current inflation multiplier for a single channel.

    Usage::

        scaler = FloatPriceScaler(config, db, channel, logger)
        await scaler.start()           # begins the refresh loop
        price = scaler.scale(100_000)  # returns inflated price as int
        await scaler.stop()
    """

    def __init__(
        self,
        config: EconomyConfig,
        db: EconomyDatabase,
        channel: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._channel = channel
        self._log = logger or logging.getLogger("economy.inflation")

        # Live state — refreshed by _refresh_loop
        self._multiplier: float = 1.0
        self._current_float: int = 0

        self._refresh_task: asyncio.Task | None = None

    # ──────────────────────────────────────────────────────────
    #  Lifecycle
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Perform an immediate refresh then start the periodic refresh loop."""
        await self._refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._log.info(
            "FloatPriceScaler started: channel=%s multiplier=%.3f float=%d anchor=%d",
            self._channel,
            self._multiplier,
            self._current_float,
            self._cfg.anchor_float,
        )

    async def stop(self) -> None:
        """Cancel the refresh loop."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    def update_config(self, config: EconomyConfig) -> None:
        """Hot-swap config (e.g. after SIGHUP reload). Triggers immediate recalc."""
        self._config = config
        self._recalculate()

    # ──────────────────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def multiplier(self) -> float:
        """Current inflation multiplier (1.0 when disabled)."""
        return self._multiplier if self._cfg.enabled else 1.0

    @property
    def current_float(self) -> int:
        """Most recent total-circulation reading from the DB."""
        return self._current_float

    def scale(self, base_cost: int) -> int:
        """Return ``base_cost`` scaled by the current multiplier.

        Always returns at least 1. When the governor is disabled, returns
        ``base_cost`` unchanged.
        """
        if not self._cfg.enabled:
            return base_cost
        return max(1, round(base_cost * self._multiplier))

    # ──────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────

    @property
    def _cfg(self) -> InflationConfig:
        return self._config.inflation

    async def _refresh(self) -> None:
        """Read the live float from the DB and recalculate the multiplier."""
        try:
            self._current_float = await self._db.get_total_circulation(self._channel)
            self._recalculate()
        except Exception as exc:
            self._log.warning("FloatPriceScaler refresh failed: %s", exc)

    def _recalculate(self) -> None:
        """Recompute the multiplier from the cached float and current config."""
        cfg = self._cfg
        if not cfg.enabled or cfg.anchor_float <= 0:
            self._multiplier = 1.0
            return
        raw = self._current_float / cfg.anchor_float
        self._multiplier = max(cfg.min_multiplier, min(cfg.max_multiplier, raw))
        self._log.debug(
            "Inflation recalc: float=%d anchor=%d raw=%.4f multiplier=%.4f",
            self._current_float,
            cfg.anchor_float,
            raw,
            self._multiplier,
        )

    async def _refresh_loop(self) -> None:
        """Periodic refresh at config.inflation.update_interval_seconds."""
        while True:
            try:
                await asyncio.sleep(self._cfg.update_interval_seconds)
                await self._refresh()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("FloatPriceScaler loop error: %s", exc)
```

### 4.2 Design Notes

- **Stateful per channel.** `EconomyApp` constructs one scaler per channel. The channel
  set is stable after startup; scalers do not need to be created/destroyed dynamically.
- **No DB hit per transaction.** The scaler is a simple property read at spend time.
  The DB is hit only by the background refresh and the initial `start()` call.
- **Failure-safe.** If the DB refresh fails, the last-known multiplier is retained.
  A WARNING is logged but the service continues operating.
- **Config hot-reload.** `update_config()` recalculates in-place using the cached float;
  the next scheduled refresh will re-read the DB anyway.

---

## 5. SpendingEngine Changes

### 5.1 Constructor Signature

Add `price_scaler` as an optional argument (default `None` for backward compatibility
and test isolation):

```python
class SpendingEngine:
    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        media_client: MediaCMSClient | None,
        logger: logging.Logger,
        price_scaler: FloatPriceScaler | None = None,   # NEW
    ) -> None:
        ...
        self._scaler = price_scaler
```

Add `update_config` body update:

```python
def update_config(self, new_config, price_scaler: FloatPriceScaler | None = None) -> None:
    self._config = new_config
    if price_scaler is not None:
        self._scaler = price_scaler
```

### 5.2 New Helper: `get_inflated_price`

```python
def get_inflated_price(self, base_cost: int) -> int:
    """Apply inflation multiplier to a base cost.

    Returns ``base_cost`` unchanged if the governor is disabled or not wired.
    """
    if self._scaler is None:
        return base_cost
    return self._scaler.scale(base_cost)
```

### 5.3 Update `get_price_tier`

The existing method returns `(label, base_cost)` — do not change its return type. Instead,
add a companion method:

```python
def get_effective_price_tier(self, duration_seconds: int) -> tuple[str, int, int]:
    """Return (label, base_cost, effective_cost) for a video duration.

    ``base_cost`` is the configured value; ``effective_cost`` has inflation applied.
    """
    label, base_cost = self.get_price_tier(duration_seconds)
    return label, base_cost, self.get_inflated_price(base_cost)
```

### 5.4 Update `apply_discount`

No signature change needed. All callers must apply inflation **before** rank discount:

```
effective_price = get_inflated_price(base_cost)
final_price, discount = apply_discount(effective_price, rank_tier_index)
```

This means a tier-5 rank user with a 10% rank discount and a 2× inflation multiplier pays:
`base × 2.0 × 0.90` — inflation is applied first, discount reduces from the inflated price.

> **Rationale:** rank discounts are a reward for loyalty, not a hedge against inflation.
> Applying discount after inflation keeps the discount meaningful at any float level.

### 5.5 Update Premium Action Prices

Add two new methods (do not modify the existing attributes):

```python
def get_interrupt_play_next_price(self) -> int:
    """Effective price for interrupt_play_next (inflation-adjusted)."""
    return self.get_inflated_price(self._config.spending.interrupt_play_next)

def get_force_play_now_price(self) -> int:
    """Effective price for force_play_now (inflation-adjusted)."""
    return self.get_inflated_price(self._config.spending.force_play_now)
```

### 5.6 Update Vanity Shop Price Helper

Add:

```python
def get_vanity_item_price(self, base_cost: int) -> int:
    """Effective price for a vanity shop item (inflation-adjusted)."""
    return self.get_inflated_price(base_cost)
```

---

## 6. PM Display Changes

All changes are in `kryten_economy/pm_handler.py`.

### 6.1 `_cmd_shop` — Vanity Shop Listing

When `inflation.enabled` and `multiplier != 1.0`, display both the base and effective price:

```
🛒 Vanity Shop
──────────────────────────────
custom_greeting   5,000 Z  (base: 5,000 Z, inflation: 1.0×)
chat_color       15,000 Z  (base: 7,500 Z, inflation: 2.0×)
...
```

When `inflation.enabled` is `False` or multiplier is effectively 1.0 (within ±0.01),
display the price as before — no `(base:…)` suffix. This keeps the common case clean.

Implementation: replace direct `cfg.cost` lookups in `_cmd_shop` with
`self._spending.get_vanity_item_price(cfg.cost)` and conditionally append the inflation
annotation using a helper:

```python
def _format_inflated_price(
    self, base_cost: int, effective_cost: int
) -> str:
    """Return 'N Z' or 'N Z  (base: M Z, ×X.XX)' depending on inflation state."""
    if not self._config.inflation.enabled:
        return f"{effective_cost:,} Z"
    multiplier = self._app.price_scaler_for(self._channel).multiplier
    if abs(multiplier - 1.0) < 0.01:
        return f"{effective_cost:,} Z"
    return (
        f"{effective_cost:,} Z  "
        f"(base: {base_cost:,} Z, ×{multiplier:.2f})"
    )
```

### 6.2 `_cmd_queue` Confirmation Flow

The queue confirmation already calls `self._spending.get_price_tier()` +
`apply_discount()`. Replace with `get_effective_price_tier()` so the confirmation shows
the inflation-adjusted price:

```
🎬 "Blade Runner 2049" (3h 0m) — Movie
Cost: 800,000 Z  (base: 100,000 Z, inflation: 8.0×)
Your balance: 12,400,000 Z
Reply YES to confirm, NO to cancel.
```

When inflation multiplier ≈ 1.0, show `Cost: 100,000 Z` as before (no annotation).

### 6.3 No changes to earning commands

Earning-path PM commands (`balance`, `rewards`, `leaderboard`, etc.) are unaffected.

---

## 7. Admin PM Command: `inflation`

### 7.1 Registration

Add to the admin command table in `PmHandler`:

```python
"inflation": self._cmd_inflation,   # admin only
```

The command is restricted to CyTube rank ≥ 4 (admin) — same gate as existing admin
commands.

### 7.2 Implementation

```python
async def _cmd_inflation(self, username: str, channel: str, args: list[str]) -> str:
    if not self._config.inflation.enabled:
        return "📊 Inflation governor is disabled. Set inflation.enabled: true to activate."

    scaler = self._app.price_scaler_for(channel)
    cfg = self._config.inflation
    m = scaler.multiplier

    lines = [
        "📊 Inflation Governor Status",
        f"  Current float : {scaler.current_float:,} Z",
        f"  Anchor float  : {cfg.anchor_float:,} Z",
        f"  Multiplier    : {m:.3f}×  (min {cfg.min_multiplier}×, max {cfg.max_multiplier}×)",
        f"  Refresh every : {cfg.update_interval_seconds // 60} min",
        "",
        "Sample prices at current inflation:",
    ]
    # Show a representative spread of spend-sink prices
    spending = self._config.spending
    samples = [
        ("Queue (short video)", spending.queue_tiers[0].cost if spending.queue_tiers else 2500),
        ("Queue (movie)", spending.queue_tiers[-1].cost if spending.queue_tiers else 10000),
        ("Skip to next", spending.interrupt_play_next),
        ("Chat color", self._config.vanity_shop.chat_color.cost),
        ("Shoutout", self._config.vanity_shop.shoutout.cost),
    ]
    for label, base in samples:
        effective = scaler.scale(base)
        lines.append(f"  {label:<22} {base:>10,} Z  →  {effective:>10,} Z")

    return "\n".join(lines)
```

### 7.3 Example Output

```
📊 Inflation Governor Status
  Current float : 847,231,540 Z
  Anchor float  : 100,000,000 Z
  Multiplier    : 8.000×  (min 0.25×, max 8.0×)
  Refresh every : 60 min

Sample prices at current inflation:
  Queue (short video)    25,000 Z  →     200,000 Z
  Queue (movie)         100,000 Z  →     800,000 Z
  Skip to next        1,000,000 Z  →   8,000,000 Z
  Chat color              7,500 Z  →      60,000 Z
  Shoutout                  500 Z  →       4,000 Z
```

---

## 8. NATS Command Extension: `stats.inflation`

### 8.1 Handler in `CommandHandler`

```python
async def _handle_stats_inflation(self, request: dict[str, Any]) -> dict[str, Any]:
    channel = request.get("channel", "")
    cfg = self._app.config.inflation
    if not cfg.enabled:
        return {
            "enabled": False,
            "multiplier": 1.0,
            "anchor_float": cfg.anchor_float,
            "current_float": 0,
            "min_multiplier": cfg.min_multiplier,
            "max_multiplier": cfg.max_multiplier,
        }
    scaler = self._app.price_scaler_for(channel)
    return {
        "enabled": True,
        "multiplier": scaler.multiplier,
        "anchor_float": cfg.anchor_float,
        "current_float": scaler.current_float,
        "min_multiplier": cfg.min_multiplier,
        "max_multiplier": cfg.max_multiplier,
    }
```

### 8.2 Register in dispatch table

```python
"stats.inflation": _handle_stats_inflation,
```

---

## 9. Metrics Extension

In `kryten_economy/metrics_collector.py`, add a new gauge to the existing Prometheus output:

```python
economy_inflation_multiplier{channel="..."} <float>
```

The metric is emitted regardless of whether `enabled` is `True` — when disabled, it always
emits `1.0`. This ensures the Prometheus scrape config doesn't need to be conditioned on the
feature flag, and provides a flat baseline for alert rule authoring.

Implementation: `MetricsCollector` receives the scaler (or a callable that returns the
multiplier) from `EconomyApp`. Add a `_inflation_multiplier` attribute populated in the
existing `collect()` / `update_state()` path, then emit it in `generate_metrics()`.

If `MetricsCollector` currently does not hold a reference to the scaler, add it as an
optional constructor parameter (`price_scaler: FloatPriceScaler | None = None`).

---

## 10. Snapshot Extension

In `AdminScheduler._capture_snapshot()`, include the current multiplier:

```python
data = {
    ...
    "inflation_multiplier": self._app.price_scaler_for(channel).multiplier
    if self._config.inflation.enabled
    else 1.0,
}
```

Add a `inflation_multiplier REAL DEFAULT 1.0` column to the `economy_snapshots` table.

### 10.1 Database Migration

Because `economy_snapshots` already exists in production, add the column via `ALTER TABLE`
in the schema initialisation block using the idiomatic safe pattern already used elsewhere
in `database.py`:

```python
# Sprint 10 — add inflation_multiplier column to economy_snapshots
try:
    conn.execute(
        "ALTER TABLE economy_snapshots ADD COLUMN inflation_multiplier REAL DEFAULT 1.0"
    )
except Exception:
    pass  # column already exists
```

---

## 11. EconomyApp Wiring

### 11.1 Imports in `main.py`

```python
from .float_price_scaler import FloatPriceScaler
```

### 11.2 Add to `EconomyApp.__init__`

```python
self.price_scalers: dict[str, FloatPriceScaler] = {}
```

### 11.3 Add helper method

```python
def price_scaler_for(self, channel: str) -> FloatPriceScaler:
    """Return the FloatPriceScaler for a channel, creating a no-op scaler if missing."""
    if channel not in self.price_scalers:
        # Defensive: return a disabled-effective scaler rather than raising
        scaler = FloatPriceScaler(self.config, self.db, channel, self.logger)
        self.price_scalers[channel] = scaler
    return self.price_scalers[channel]
```

### 11.4 Construction in `start()`

Construct scalers after `EconomyDatabase` is initialized and before `SpendingEngine`
is constructed. The channel list is read from `self.config.channels`:

```python
# Sprint 10: Inflation governor
if self.config.inflation.enabled:
    for ch in self.config.channels:
        scaler = FloatPriceScaler(
            config=self.config,
            db=self.db,
            channel=ch,
            logger=self.logger.getChild("inflation"),
        )
        self.price_scalers[ch] = scaler
    self.logger.info(
        "FloatPriceScaler created for %d channel(s)", len(self.price_scalers)
    )
```

### 11.5 `start()` lifecycle

After NATS connection is established (so the DB is reachable), start each scaler:

```python
for scaler in self.price_scalers.values():
    await scaler.start()
```

Inject into `SpendingEngine` and `MetricsCollector` using the per-channel scaler. Since
`SpendingEngine` is currently channel-agnostic, pass the scaler for the primary channel
(when only one channel is configured) or accept that the scaler is looked up dynamically
via `EconomyApp.price_scaler_for(channel)` in PM handler paths.

> **Implementation note:** If `EconomyApp` currently instantiates one `SpendingEngine`
> shared across all channels, inject the scaler at the PM-handler level via
> `self._app.price_scaler_for(channel)` rather than baking a channel-specific scaler into
> `SpendingEngine`. The `SpendingEngine` helper methods (`get_inflated_price`,
> `get_effective_price_tier`, etc.) should delegate to whichever scaler is passed in or
> cached at construction time — choose the simpler path given the existing architecture.

### 11.6 `stop()` lifecycle

```python
for scaler in self.price_scalers.values():
    await scaler.stop()
```

### 11.7 Config hot-reload

In the existing config-reload path (where `update_config()` is called on engines), add:

```python
for ch, scaler in self.price_scalers.items():
    scaler.update_config(new_config)
```

---

## 12. config.example.yaml Documentation

Add the following section immediately after `balance_maintenance` (retain existing
indentation style — top-level keys are flush left):

```yaml
# ── Inflation Governor ────────────────────────────────────────────────
# Ties all spend-sink prices to the total coin float. When the float
# exceeds the anchor, prices rise proportionally; when it falls back,
# prices soften. This prevents the economy from needing periodic manual
# redenominations.
#
# Formula: effective_price = base_price × clamp(float/anchor, min, max)
# Rank discounts apply AFTER inflation: final = effective × (1 - discount)
#
# NOTE: Set anchor_float to your current "healthy" target float level.
# If you've just done a redenomination (e.g. 800M → 80M), set anchor
# to 80000000 so prices start at 1.0× and scale up as the float grows.
inflation:
  enabled: false                  # Set true to activate float-tied pricing
  anchor_float: 100000000         # Target float (100M Z) — multiplier = 1.0 here
  min_multiplier: 0.25            # Floor: prices never drop below 25% of base
  max_multiplier: 8.0             # Ceiling: prices never exceed 8× base
  update_interval_seconds: 3600   # Refresh the live float from DB every hour
```

---

## 13. Test Specifications

All test files are in `tests/`. The existing `asyncio_mode = "auto"` pytest configuration
applies. Use `pytest-asyncio` async test functions and `MockKrytenClient` from the existing
test infrastructure where NATS interaction is involved.

### 13.1 `tests/test_float_price_scaler.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from kryten_economy.float_price_scaler import FloatPriceScaler
from kryten_economy.config import EconomyConfig, InflationConfig


def make_config(enabled=True, anchor=100_000_000, min_m=0.25, max_m=8.0) -> EconomyConfig:
    cfg = EconomyConfig()
    cfg.inflation = InflationConfig(
        enabled=enabled,
        anchor_float=anchor,
        min_multiplier=min_m,
        max_multiplier=max_m,
        update_interval_seconds=3600,
    )
    return cfg


def make_db(circulation: int) -> MagicMock:
    db = MagicMock()
    db.get_total_circulation = AsyncMock(return_value=circulation)
    return db


# ── scale() ──────────────────────────────────────────────────


async def test_scale_at_anchor_returns_base():
    """float == anchor → multiplier 1.0 → scale returns base cost."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(100_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(10_000) == 10_000


async def test_scale_above_anchor_inflates():
    """float 4× anchor → multiplier 4.0 → scale returns 4× base."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(400_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(100_000) == 400_000


async def test_scale_below_anchor_deflates():
    """float 0.5× anchor → multiplier 0.5 → scale returns 50% of base."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(50_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(10_000) == 5_000


async def test_max_multiplier_cap():
    """float 100× anchor → raw=100.0 → clamped to max_multiplier=8.0."""
    cfg = make_config(anchor=100_000_000, max_m=8.0)
    db = make_db(10_000_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.multiplier == 8.0
    assert scaler.scale(100_000) == 800_000


async def test_min_multiplier_floor():
    """float 0.01× anchor → raw=0.01 → clamped to min_multiplier=0.25."""
    cfg = make_config(anchor=100_000_000, min_m=0.25)
    db = make_db(1_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.multiplier == 0.25
    assert scaler.scale(10_000) == 2_500


async def test_disabled_returns_base():
    """When enabled=False, scale() is a no-op."""
    cfg = make_config(enabled=False)
    db = make_db(999_999_999)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.multiplier == 1.0
    assert scaler.scale(100_000) == 100_000


async def test_scale_always_returns_at_least_one():
    """scale(1) with min_multiplier fractional still returns 1."""
    cfg = make_config(anchor=100_000_000, min_m=0.001)
    db = make_db(50_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()
    assert scaler.scale(1) >= 1


async def test_db_failure_retains_last_multiplier():
    """If the DB refresh throws, the previous multiplier is kept."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(200_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()                    # multiplier = 2.0
    assert scaler.multiplier == 2.0

    db.get_total_circulation = AsyncMock(side_effect=RuntimeError("DB down"))
    await scaler._refresh()                    # should not raise; multiplier unchanged
    assert scaler.multiplier == 2.0


async def test_update_config_recalculates():
    """update_config() with a new anchor recalculates without a new DB read."""
    cfg = make_config(anchor=100_000_000)
    db = make_db(200_000_000)
    scaler = FloatPriceScaler(cfg, db, "test")
    await scaler._refresh()                    # float=200M, anchor=100M → 2.0
    assert scaler.multiplier == 2.0

    new_cfg = make_config(anchor=200_000_000)  # anchor matches float → should be 1.0
    scaler.update_config(new_cfg)
    assert scaler.multiplier == 1.0
```

### 13.2 `tests/test_spending_engine_inflation.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from kryten_economy.spending_engine import SpendingEngine
from kryten_economy.float_price_scaler import FloatPriceScaler
from kryten_economy.config import EconomyConfig, InflationConfig, SpendingConfig, QueueTierConfig


def make_scaler(multiplier: float, enabled: bool = True) -> MagicMock:
    scaler = MagicMock(spec=FloatPriceScaler)
    scaler.enabled = enabled
    scaler.multiplier = multiplier
    scaler.scale = MagicMock(side_effect=lambda base: max(1, round(base * multiplier)) if enabled else base)
    return scaler


def make_engine(multiplier: float = 1.0, enabled: bool = True) -> SpendingEngine:
    cfg = EconomyConfig()
    cfg.inflation = InflationConfig(enabled=enabled, anchor_float=100_000_000)
    cfg.spending = SpendingConfig(
        queue_tiers=[
            QueueTierConfig(max_minutes=15, label="Short", cost=25_000),
            QueueTierConfig(max_minutes=999, label="Movie", cost=100_000),
        ],
        interrupt_play_next=1_000_000,
        force_play_now=10_000_000,
    )
    scaler = make_scaler(multiplier, enabled)
    return SpendingEngine(cfg, MagicMock(), None, MagicMock(), price_scaler=scaler)


def test_get_inflated_price_applies_multiplier():
    engine = make_engine(multiplier=2.0)
    assert engine.get_inflated_price(100_000) == 200_000


def test_get_inflated_price_disabled_is_passthrough():
    engine = make_engine(multiplier=5.0, enabled=False)
    assert engine.get_inflated_price(100_000) == 100_000


def test_get_effective_price_tier_returns_three_tuple():
    engine = make_engine(multiplier=3.0)
    label, base, effective = engine.get_effective_price_tier(30 * 60)  # 30 min → Movie tier
    assert label == "Movie"
    assert base == 100_000
    assert effective == 300_000


def test_interrupt_price_inflated():
    engine = make_engine(multiplier=4.0)
    assert engine.get_interrupt_play_next_price() == 4_000_000


def test_force_play_price_inflated():
    engine = make_engine(multiplier=4.0)
    assert engine.get_force_play_now_price() == 40_000_000


def test_rank_discount_applied_after_inflation():
    """Rank discount is applied on the inflated price, not base."""
    engine = make_engine(multiplier=2.0)
    # base=100_000, inflated=200_000, 10% discount → 180_000
    inflated = engine.get_inflated_price(100_000)
    final, discount = engine.apply_discount(inflated, rank_tier_index=5)
    expected_discount = engine._config.ranks.spend_discount_per_rank * 5
    expected_final = max(1, int(200_000 * (1 - expected_discount)))
    assert final == expected_final
```

### 13.3 `tests/test_config_inflation.py`

```python
import pytest
from pydantic import ValidationError
from kryten_economy.config import InflationConfig, EconomyConfig


def test_defaults():
    cfg = InflationConfig()
    assert cfg.enabled is False
    assert cfg.anchor_float == 100_000_000
    assert cfg.min_multiplier == 0.25
    assert cfg.max_multiplier == 8.0
    assert cfg.update_interval_seconds == 3600


def test_min_exceeds_max_raises():
    with pytest.raises(ValidationError):
        InflationConfig(min_multiplier=5.0, max_multiplier=2.0)


def test_economy_config_includes_inflation():
    cfg = EconomyConfig()
    assert hasattr(cfg, "inflation")
    assert isinstance(cfg.inflation, InflationConfig)


def test_inflation_loaded_from_yaml_dict():
    """EconomyConfig can be constructed with an inline inflation dict (simulates YAML load)."""
    cfg = EconomyConfig(
        **{"inflation": {"enabled": True, "anchor_float": 80_000_000, "max_multiplier": 10.0}}
    )
    assert cfg.inflation.enabled is True
    assert cfg.inflation.anchor_float == 80_000_000
    assert cfg.inflation.max_multiplier == 10.0


def test_inflation_none_coercion():
    """YAML `inflation:` with all sub-keys commented out parses as None → default."""
    cfg = EconomyConfig(**{"inflation": None})
    assert isinstance(cfg.inflation, InflationConfig)
    assert cfg.inflation.enabled is False
```

> **Note:** Add a `@field_validator("inflation", mode="before")` to `EconomyConfig` that
> coerces `None` → `InflationConfig()`, following the same pattern used for
> `HeistNarrativeConfig._coerce_narrative_none`.

### 13.4 `tests/test_command_handler_inflation.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


async def test_stats_inflation_disabled(make_app):
    """stats.inflation returns enabled=False when governor is off."""
    make_app.config.inflation.enabled = False
    handler = make_app.command_handler
    result = await handler._handle_stats_inflation({"channel": "test"})
    assert result["enabled"] is False
    assert result["multiplier"] == 1.0


async def test_stats_inflation_enabled(make_app):
    """stats.inflation returns live multiplier when governor is on."""
    make_app.config.inflation.enabled = True
    scaler = MagicMock()
    scaler.multiplier = 4.2
    scaler.current_float = 420_000_000
    make_app.price_scaler_for = MagicMock(return_value=scaler)

    handler = make_app.command_handler
    result = await handler._handle_stats_inflation({"channel": "test"})
    assert result["enabled"] is True
    assert result["multiplier"] == pytest.approx(4.2)
    assert result["current_float"] == 420_000_000
```

> `make_app` should be an existing pytest fixture from the test suite that produces a
> minimal `EconomyApp` with mocked dependencies. If no such fixture exists, create a
> minimal one in `conftest.py`.

---

## 14. Acceptance Criteria

All of the following must be true before this sprint is considered complete.

**Config & parsing**
- [ ] `InflationConfig` with all five fields parses from a YAML dict correctly
- [ ] `EconomyConfig.inflation` is `None`-safe (coerces to defaults)
- [ ] `InflationConfig(min_multiplier=5.0, max_multiplier=2.0)` raises `ValidationError`
- [ ] `EconomyConfig()` (no args) has `inflation.enabled = False`

**FloatPriceScaler**
- [ ] At float == anchor, `scale(x) == x`
- [ ] At float 4× anchor, `scale(x) == 4x`
- [ ] At float 0.1× anchor with `min_multiplier=0.25`, `multiplier == 0.25`
- [ ] At float 100× anchor with `max_multiplier=8.0`, `multiplier == 8.0`
- [ ] DB failure retains last-known multiplier (no crash, WARNING logged)
- [ ] `enabled=False` → `scale(x) == x` always
- [ ] `update_config()` recalculates with new anchor without a DB call

**SpendingEngine**
- [ ] `get_inflated_price()` returns `base × multiplier` (rounded)
- [ ] `get_effective_price_tier()` returns `(label, base, effective)` 3-tuple
- [ ] `get_interrupt_play_next_price()` and `get_force_play_now_price()` reflect inflation
- [ ] Rank discount is applied on the inflation-adjusted price, not base
- [ ] When scaler is `None` or disabled, all methods return base prices unchanged

**PM display**
- [ ] `shop` command shows inflation annotation when multiplier ≠ 1.0
- [ ] `shop` command shows plain price when `enabled=False`
- [ ] Queue confirmation shows inflated cost when governor is active

**Admin command**
- [ ] `inflation` PM command is gated to rank ≥ 4 (admin)
- [ ] Output includes multiplier, float, anchor, and sample spend-sink prices
- [ ] When `enabled=False`, returns a clear "disabled" message

**NATS command**
- [ ] `stats.inflation` is in the dispatch table
- [ ] Returns `enabled`, `multiplier`, `anchor_float`, `current_float`, bounds

**Metrics & observability**
- [ ] `economy_inflation_multiplier{channel=...}` appears in Prometheus scrape output
- [ ] Emits `1.0` when `enabled=False`
- [ ] Economy snapshot includes `inflation_multiplier` column value

**Regression**
- [ ] All 832 existing tests pass unmodified
- [ ] `uv run black --check .` passes
- [ ] `uv run ruff check .` passes
- [ ] `uv run mypy kryten_economy` passes (strict mode — keep annotations complete)
