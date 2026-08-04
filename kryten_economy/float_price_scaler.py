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
