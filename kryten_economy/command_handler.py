"""Request-reply command handler on kryten.economy.command.

Provides a NATS request-reply API for inter-service communication
and admin tooling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import load_config
from .css_vanity import harvest_managed_colors, merge_vanity_css

if TYPE_CHECKING:
    from kryten import KrytenClient

    from .main import EconomyApp


# ══════════════════════════════════════════════════════════
#  Module-level helpers for queue spending
# ══════════════════════════════════════════════════════════

def _is_blackout_active(
    windows: list, now_utc: "datetime"
) -> bool:
    """Return True if any blackout window covers now_utc.

    Each window has `cron` (start schedule) and `duration_hours`.
    Uses croniter to find the most recent trigger and checks if
    now_utc falls within [trigger, trigger + duration).
    """
    try:
        from croniter import croniter
    except ImportError:
        return False  # croniter not installed — blackout disabled

    for win in windows:
        cron_expr = getattr(win, "cron", None) or win.get("cron") if isinstance(win, dict) else win.cron
        duration_h = getattr(win, "duration_hours", None) or win.get("duration_hours") if isinstance(win, dict) else win.duration_hours
        if not cron_expr or not duration_h:
            continue
        it = croniter(cron_expr, now_utc)
        prev_fire = it.get_prev(datetime)
        if prev_fire.tzinfo is None:
            prev_fire = prev_fire.replace(tzinfo=timezone.utc)
        if prev_fire <= now_utc < prev_fire + timedelta(hours=duration_h):
            return True
    return False


def _rank_queue_bonus(account: dict | None) -> int:
    """Extra queues per day granted by rank perks.

    Users with elevated rank names get +1 queue/day.
    """
    if not account:
        return 0
    elevated = {"vip", "mod", "admin", "owner", "trusted", "regular"}
    rank_name = str(account.get("rank_name", "")).lower()
    return 1 if rank_name in elevated else 0


_HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _normalize_hex_color(value: str) -> str | None:
    """Normalize a user-supplied colour to ``#RRGGBB`` upper-case, or None."""
    match = _HEX_COLOR_RE.match(value.strip())
    if not match:
        return None
    return "#" + match.group(1).upper()


class CommandHandler:
    """Handles request-reply commands on kryten.economy.command."""

    def __init__(
        self,
        app: EconomyApp,
        client: KrytenClient,
        logger: logging.Logger | None = None,
    ) -> None:
        self._app = app
        self._client = client
        self._logger = logger or logging.getLogger("economy.command")
        # Per-user shoutout cooldowns for the NATS command path
        # (web/API). Keyed by (lowercased username, channel).
        self._shoutout_cooldowns: dict[tuple[str, str], datetime] = {}

    async def connect(self) -> None:
        """Subscribe to request-reply on kryten.economy.command."""
        await self._client.subscribe_request_reply(
            "kryten.economy.command",
            self._handle_command,
        )

    async def _handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route a command request to the appropriate handler."""
        command = request.get("command", "")
        handler = self._HANDLER_MAP.get(command)

        if not handler:
            return {
                "service": "economy",
                "command": command,
                "success": False,
                "error": f"Unknown command: {command}",
            }

        try:
            result = await handler(self, request)
            self._app.commands_processed += 1
            return {
                "service": "economy",
                "command": command,
                "success": True,
                "data": result,
            }
        except Exception as e:
            self._logger.exception("Command handler error for %s", command)
            return {
                "service": "economy",
                "command": command,
                "success": False,
                "error": str(e),
            }

    # ══════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _utc_today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def _channel(request: dict[str, Any]) -> str:
        channel = request.get("channel")
        if not channel:
            raise ValueError("channel is required")
        return str(channel)

    @staticmethod
    def _username(request: dict[str, Any]) -> str:
        username = request.get("username")
        if not username:
            raise ValueError("username is required")
        return str(username)

    # ══════════════════════════════════════════════════════════
    #  Sprint 1 Commands
    # ══════════════════════════════════════════════════════════

    async def _handle_ping(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "version": __version__}

    async def _handle_about(self, request: dict[str, Any]) -> dict[str, Any]:
        uptime = self._app.uptime_seconds
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_human = f"{hours}h {minutes}m {seconds}s"
        return {
            "version": __version__,
            "uptime_seconds": uptime,
            "uptime_human": uptime_human,
        }

    async def _handle_health(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "healthy",
            "database": "connected" if self._app.db else "disconnected",
            "active_sessions": sum(
                self._app.presence_tracker.get_connected_count(ch.channel)
                for ch in self._app.config.channels
            ),
            "uptime_seconds": self._app.uptime_seconds,
        }

    async def _handle_balance_get(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)

        account = await self._app.db.get_account(username, channel)
        if not account:
            return {"found": False}

        return {
            "found": True,
            "username": account["username"],
            "channel": account["channel"],
            "balance": account["balance"],
            "lifetime_earned": account["lifetime_earned"],
            "rank_name": account["rank_name"],
        }

    async def _handle_balance_adjust(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        amount = int(request.get("amount", 0))
        reason = str(request.get("reason", ""))
        admin = str(request.get("admin", "system"))

        if amount == 0:
            raise ValueError("amount must be non-zero")

        await self._app.db.get_or_create_account(username, channel)

        if amount > 0:
            new_balance = await self._app.db.credit(
                username,
                channel,
                amount,
                tx_type="admin_adjust",
                reason=reason or f"Admin credit by {admin}",
                trigger_id="admin.adjust",
            )
        else:
            new_balance = await self._app.db.debit(
                username,
                channel,
                abs(amount),
                tx_type="admin_adjust",
                reason=reason or f"Admin debit by {admin}",
                trigger_id="admin.adjust",
            )
            if new_balance is None:
                raise ValueError("insufficient funds")

        return {
            "username": username,
            "channel": channel,
            "amount": amount,
            "balance": new_balance,
        }

    async def _handle_balance_set(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        amount = int(request.get("amount", 0))
        admin = str(request.get("admin", "system"))

        account = await self._app.db.get_or_create_account(username, channel)
        old_balance = int(account.get("balance", 0))
        delta = amount - old_balance

        await self._app.db.set_balance(username, channel, amount)
        await self._app.db.log_transaction(
            username,
            channel,
            delta,
            tx_type="admin_set_balance",
            trigger_id="admin.set_balance",
            reason=f"Set by {admin}",
        )

        return {
            "username": username,
            "channel": channel,
            "old_balance": old_balance,
            "new_balance": amount,
            "delta": delta,
        }

    async def _handle_balance_search(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        pattern = str(request.get("pattern", "")).strip()
        limit = int(request.get("limit", 50))
        limit = max(1, min(limit, 200))

        loop = asyncio.get_running_loop()

        def _sync() -> list[dict[str, Any]]:
            conn = self._app.db._get_connection()  # noqa: SLF001
            try:
                if pattern:
                    rows = conn.execute(
                        "SELECT username, balance, lifetime_earned, rank_name "
                        "FROM accounts WHERE channel = ? AND username LIKE ? "
                        "ORDER BY balance DESC LIMIT ?",
                        (channel, f"%{pattern}%", limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT username, balance, lifetime_earned, rank_name "
                        "FROM accounts WHERE channel = ? "
                        "ORDER BY balance DESC LIMIT ?",
                        (channel, limit),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        results = await loop.run_in_executor(None, _sync)
        return {
            "channel": channel,
            "pattern": pattern,
            "count": len(results),
            "results": results,
        }

    async def _handle_transactions_list(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        limit = int(request.get("limit", 50))
        offset = int(request.get("offset", 0))
        limit = max(1, min(limit, 500))
        offset = max(0, offset)

        loop = asyncio.get_running_loop()

        def _sync() -> list[dict[str, Any]]:
            conn = self._app.db._get_connection()  # noqa: SLF001
            try:
                rows = conn.execute(
                    "SELECT * FROM transactions "
                    "WHERE username = ? AND channel = ? "
                    "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (username, channel, limit, offset),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        transactions = await loop.run_in_executor(None, _sync)
        return {
            "username": username,
            "channel": channel,
            "limit": limit,
            "offset": offset,
            "transactions": transactions,
        }

    async def _handle_transactions_recent(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        limit = int(request.get("limit", 50))
        limit = max(1, min(limit, 500))

        loop = asyncio.get_running_loop()

        def _sync() -> list[dict[str, Any]]:
            conn = self._app.db._get_connection()  # noqa: SLF001
            try:
                rows = conn.execute(
                    "SELECT * FROM transactions "
                    "WHERE channel = ? ORDER BY id DESC LIMIT ?",
                    (channel, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        transactions = await loop.run_in_executor(None, _sync)
        return {
            "channel": channel,
            "limit": limit,
            "transactions": transactions,
        }

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Queue Spending Commands
    # ══════════════════════════════════════════════════════════

    async def _handle_spending_queue_preview(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Read-only cost estimate. No state changed."""
        username = self._username(request)
        channel = self._channel(request)
        duration_sec = int(request.get("duration_sec", 0))
        if duration_sec <= 0:
            raise ValueError("duration_sec must be positive")

        engine = self._app.spending_engine
        cfg = self._app.config.spending
        db = self._app.db

        # --- Pricing ---
        tier_label, base_cost = engine.get_price_tier(duration_sec)
        account = await db.get_account(username, channel)
        rank_index = engine.get_rank_tier_index(account) if account else 0
        final_cost, discount_frac = engine.apply_discount(base_cost, rank_index)
        discount_pct = round(discount_frac * 100, 1)

        # --- Eligibility checks (in priority order) ---
        error_code = None
        cooldown_remaining_sec = None
        daily_remaining = cfg.max_queues_per_day

        # 1. Blackout
        now_utc = datetime.now(timezone.utc)
        if _is_blackout_active(cfg.blackout_windows, now_utc):
            error_code = "blackout_active"

        # 2. Daily limit
        if error_code is None:
            today = self._utc_today()
            activity = await db.get_or_create_daily_activity(username, channel, today)
            queues_used = activity.get("queues_used", 0)
            max_queues = cfg.max_queues_per_day + _rank_queue_bonus(account)
            daily_remaining = max(0, max_queues - queues_used)
            if queues_used >= max_queues:
                error_code = "daily_limit_reached"

        # 3. Cooldown
        if error_code is None:
            last_queue_time = await db.get_last_queue_time(username, channel)
            if last_queue_time is not None:
                elapsed = (now_utc - last_queue_time).total_seconds()
                cooldown_total = cfg.queue_cooldown_minutes * 60
                if elapsed < cooldown_total:
                    cooldown_remaining_sec = int(cooldown_total - elapsed)
                    error_code = "cooldown_active"

        # 4. Balance
        if error_code is None:
            outcome = await engine.validate_spend(username, channel, final_cost, "queue")
            if outcome is not None:
                error_code = "insufficient_balance"

        result: dict[str, Any] = {
            "available": error_code is None,
            "cost_z": final_cost,
            "base_cost": base_cost,
            "tier_label": tier_label,
            "discount_pct": discount_pct,
            "daily_remaining": daily_remaining,
            "error_code": error_code,
        }
        if cooldown_remaining_sec is not None:
            result["cooldown_remaining_sec"] = cooldown_remaining_sec
        return result

    async def _handle_spending_queue(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Atomic validate + debit. Idempotent via request_id."""
        username = self._username(request)
        channel = self._channel(request)
        duration_sec = int(request.get("duration_sec", 0))
        tier = str(request.get("tier", "queue"))
        request_id = str(request.get("request_id", "")).strip()
        if not request_id:
            raise ValueError("request_id is required")
        if duration_sec <= 0:
            raise ValueError("duration_sec must be positive")

        engine = self._app.spending_engine
        cfg = self._app.config.spending
        db = self._app.db

        # --- Idempotency check ---
        existing = await db.get_queue_spend_request(request_id)
        if existing is not None:
            # Already processed — return stored outcome without re-debiting
            return {
                "success": True,
                "cost_z": existing["cost_z"],
                "tier": existing["tier"],
                "request_id": request_id,
                "idempotent_replay": True,
            }

        # --- Pricing ---
        tier_label, base_cost = engine.get_price_tier(duration_sec)
        account = await db.get_account(username, channel)
        rank_index = engine.get_rank_tier_index(account) if account else 0
        final_cost, _ = engine.apply_discount(base_cost, rank_index)

        # --- Eligibility (same order as preview) ---
        now_utc = datetime.now(timezone.utc)
        if _is_blackout_active(cfg.blackout_windows, now_utc):
            return {"success": False, "cost_z": final_cost, "error_code": "blackout_active"}

        today = self._utc_today()
        activity = await db.get_or_create_daily_activity(username, channel, today)
        queues_used = activity.get("queues_used", 0)
        max_queues = cfg.max_queues_per_day + _rank_queue_bonus(account)
        if queues_used >= max_queues:
            return {"success": False, "cost_z": final_cost, "error_code": "daily_limit_reached"}

        last_queue_time = await db.get_last_queue_time(username, channel)
        if last_queue_time is not None:
            elapsed = (now_utc - last_queue_time).total_seconds()
            cooldown_total = cfg.queue_cooldown_minutes * 60
            if elapsed < cooldown_total:
                return {"success": False, "cost_z": final_cost, "error_code": "cooldown_active"}

        outcome = await engine.validate_spend(username, channel, final_cost, "queue")
        if outcome is not None:
            return {"success": False, "cost_z": final_cost, "error_code": "insufficient_balance"}

        # --- Debit ---
        new_balance = await db.debit(
            username, channel, final_cost,
            tx_type="spend",
            reason=f"Queue spend ({tier_label})",
            trigger_id=f"spend.queue.{request_id}",
        )
        if new_balance is None:
            return {"success": False, "cost_z": final_cost, "error_code": "insufficient_balance"}

        # --- Record idempotency + daily counter ---
        await db.insert_queue_spend_request(
            request_id=request_id,
            username=username,
            channel=channel,
            cost_z=final_cost,
            tier=tier,
        )
        await db.increment_daily_queues_used(username, channel, today)

        return {
            "success": True,
            "cost_z": final_cost,
            "tier": tier,
            "tier_label": tier_label,
            "new_balance": new_balance,
            "request_id": request_id,
        }

    async def _handle_spending_queue_refund(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Compensating credit. Idempotent via request_id."""
        username = self._username(request)
        channel = self._channel(request)
        request_id = str(request.get("request_id", "")).strip()
        reason = str(request.get("reason", "refund"))
        if not request_id:
            raise ValueError("request_id is required")

        db = self._app.db

        # Look up the original spend
        existing = await db.get_queue_spend_request(request_id)
        if existing is None:
            return {"success": False, "error": "unknown_request_id"}

        # Idempotency: already refunded
        if existing.get("refunded"):
            return {
                "success": True,
                "refunded": existing["cost_z"],
                "request_id": request_id,
                "idempotent_replay": True,
            }

        # Credit the user back
        cost = existing["cost_z"]
        new_balance = await db.credit(
            username, channel, cost,
            tx_type="refund",
            reason=f"Queue refund: {reason}",
            trigger_id=f"refund.queue.{request_id}",
        )

        # Mark as refunded
        await db.mark_queue_spend_refunded(request_id)

        return {
            "success": True,
            "refunded": cost,
            "new_balance": new_balance,
            "request_id": request_id,
        }

    async def _handle_stats_float(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        total = await self._app.db.get_total_circulation(channel)
        accounts = await self._app.db.get_account_count(channel)
        return {
            "channel": channel,
            "float": total,
            "accounts": accounts,
        }

    async def _handle_stats_summary(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        today = self._utc_today()

        total_float = await self._app.db.get_total_circulation(channel)
        accounts = await self._app.db.get_account_count(channel)
        daily = await self._app.db.get_daily_totals(channel, today)
        gambling = await self._app.db.get_gambling_summary_global(channel)

        return {
            "channel": channel,
            "date": today,
            "float": total_float,
            "accounts": accounts,
            "totals": daily,
            "gambling": gambling,
            "net_flow_today": int(daily.get("z_earned", 0)) - int(daily.get("z_spent", 0)),
        }

    async def _handle_stats_health(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        today = datetime.now(timezone.utc).date()
        week_start = (today - timedelta(days=6)).isoformat()
        week_end = today.isoformat()

        circulation = await self._app.db.get_total_circulation(channel)
        median = await self._app.db.get_median_balance(channel)
        accounts = await self._app.db.get_account_count(channel)
        active_today = await self._app.db.get_active_economy_users_today(channel, today.isoformat())
        daily = await self._app.db.get_daily_totals(channel, today.isoformat())
        weekly = await self._app.db.get_weekly_totals(channel, week_start, week_end)

        return {
            "channel": channel,
            "date": today.isoformat(),
            "circulation": circulation,
            "median_balance": median,
            "accounts": accounts,
            "active_today": active_today,
            "daily": daily,
            "weekly": weekly,
            "daily_net": int(daily.get("z_earned", 0)) - int(daily.get("z_spent", 0)),
        }

    async def _handle_gambling_stats(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        return await self._app.db.get_gambling_summary_global(channel)

    async def _handle_config_reload(self, request: dict[str, Any]) -> dict[str, Any]:
        config_path = getattr(self._app, "config_path", None)
        if config_path is None:
            raise ValueError("config_path not available")

        new_config = load_config(str(Path(config_path)))
        self._app.config = new_config

        for component_name in (
            "presence_tracker",
            "earning_engine",
            "gambling_engine",
            "spending_engine",
            "achievement_engine",
            "rank_engine",
            "competition_engine",
            "bounty_manager",
            "multiplier_engine",
            "event_announcer",
            "greeting_handler",
        ):
            component = getattr(self._app, component_name, None)
            if component and hasattr(component, "update_config"):
                component.update_config(new_config)

        if hasattr(self._app, "_ignored_users"):
            self._app._ignored_users = {  # noqa: SLF001
                u.lower() for u in (new_config.ignored_users or [])
            }

        return {
            "reloaded": True,
            "channels": [ch.channel for ch in new_config.channels],
        }

    async def _handle_event_start(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        multiplier = float(request.get("multiplier", 1.0))
        minutes = int(request.get("minutes", 0))
        name = str(request.get("name", "Ad-hoc Event"))

        if multiplier <= 1.0:
            raise ValueError("multiplier must be > 1.0")
        if minutes <= 0:
            raise ValueError("minutes must be > 0")

        self._app.multiplier_engine.start_adhoc_event(name, multiplier, minutes)

        if self._app.event_announcer:
            await self._app.event_announcer.announce(
                channel,
                f"🎉 {name} started: x{multiplier:.2f} for {minutes} minute(s)",
            )

        return {
            "channel": channel,
            "name": name,
            "multiplier": multiplier,
            "minutes": minutes,
            "active": True,
        }

    async def _handle_event_stop(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        stopped = self._app.multiplier_engine.stop_adhoc_event()

        if stopped and self._app.event_announcer:
            await self._app.event_announcer.announce(
                channel,
                "⏹️ Ad-hoc multiplier event stopped.",
            )

        return {"channel": channel, "stopped": stopped}

    async def _handle_events_list(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        now = datetime.now(timezone.utc)
        active = []

        sched = self._app.multiplier_engine._scheduled_events.get(channel)  # noqa: SLF001
        if sched and now < sched["end_time"]:
            active.append({
                "type": "scheduled",
                "name": sched["name"],
                "multiplier": sched["multiplier"],
                "ends_at": sched["end_time"].isoformat(),
            })

        adhoc = self._app.multiplier_engine._adhoc_event  # noqa: SLF001
        if adhoc and now < adhoc["end_time"]:
            active.append({
                "type": "adhoc",
                "name": adhoc["name"],
                "multiplier": adhoc["multiplier"],
                "ends_at": adhoc["end_time"].isoformat(),
            })

        return {"channel": channel, "events": active}

    async def _handle_triggers_stats(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        days = int(request.get("days", 7))
        days = max(1, min(days, 30))

        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days - 1)
        rows = await self._app.db.get_trigger_analytics_range(
            channel,
            start.isoformat(),
            end.isoformat(),
        )

        aggregated: dict[str, dict[str, int]] = {}
        for row in rows:
            tid = row["trigger_id"]
            bucket = aggregated.setdefault(tid, {
                "hit_count": 0,
                "unique_users": 0,
                "total_z_awarded": 0,
            })
            bucket["hit_count"] += int(row.get("hit_count", 0))
            bucket["unique_users"] += int(row.get("unique_users", 0))
            bucket["total_z_awarded"] += int(row.get("total_z_awarded", 0))

        return {
            "channel": channel,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "triggers": [
                {
                    "trigger_id": tid,
                    **vals,
                }
                for tid, vals in sorted(
                    aggregated.items(),
                    key=lambda kv: kv[1]["total_z_awarded"],
                    reverse=True,
                )
            ],
        }

    async def _handle_user_detail(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)

        account = await self._app.db.get_account(username, channel)
        if not account:
            return {"found": False}

        achievements = await self._app.db.get_user_achievements(username, channel)
        banned = await self._app.db.is_banned(username, channel)
        gambling = await self._app.db.get_gambling_summary(username, channel)

        return {
            "found": True,
            "username": username,
            "channel": channel,
            "account": account,
            "achievements": achievements,
            "banned": banned,
            "gambling": gambling or {},
        }

    async def _handle_account_summary(self, request: dict[str, Any]) -> dict[str, Any]:
        """User-facing account snapshot: balance, rank progression, perks, vanity.

        Designed for surfaces like the webqueue dashboard. Returns everything
        needed to render rank progress and editable vanity items in one call.
        """
        username = self._username(request)
        channel = self._channel(request)

        account = await self._app.db.get_account(username, channel)
        if not account:
            return {"found": False, "username": username, "channel": channel}

        lifetime = int(account.get("lifetime_earned", 0))
        rank_engine = self._app.rank_engine
        spending = self._app.spending_engine
        config = self._app.config

        rank_block: dict[str, Any]
        next_block: dict[str, Any] | None = None
        perks: list[str] = []
        discount_fraction = 0.0

        if rank_engine is not None:
            tier_index, current_tier = rank_engine.get_rank_for_lifetime(lifetime)
            next_tier = rank_engine.get_next_tier(tier_index)
            tier_count = len(rank_engine._tiers)  # noqa: SLF001
            perks = list(current_tier.perks)
            if spending is not None:
                discount_fraction = spending.get_rank_discount(tier_index)

            rank_block = {
                "name": current_tier.name,
                "index": tier_index,
                "level": tier_index + 1,
                "tier_count": tier_count,
                "min_lifetime_earned": current_tier.min_lifetime_earned,
            }

            if next_tier is not None:
                target = next_tier.min_lifetime_earned
                remaining = max(0, target - lifetime)
                progress = (lifetime / target * 100.0) if target > 0 else 100.0
                next_block = {
                    "name": next_tier.name,
                    "min_lifetime_earned": target,
                    "remaining": remaining,
                    "progress_percent": round(min(100.0, progress), 1),
                }
        else:
            rank_block = {
                "name": account.get("rank_name", "Extra"),
                "index": 0,
                "level": 1,
                "tier_count": 1,
                "min_lifetime_earned": 0,
            }

        vanity = await self._app.db.get_all_vanity_items(username, channel)
        currency_name = (
            vanity.get("personal_currency_name")
            or config.currency.name
        )

        greeting_cfg = config.vanity_shop.custom_greeting
        color_cfg = config.vanity_shop.chat_color
        shoutout_cfg = config.vanity_shop.shoutout

        return {
            "found": True,
            "username": username,
            "channel": channel,
            "balance": int(account.get("balance", 0)),
            "lifetime_earned": lifetime,
            "lifetime_spent": int(account.get("lifetime_spent", 0)),
            "currency_name": currency_name,
            "currency_symbol": config.currency.symbol,
            "rank": rank_block,
            "next_rank": next_block,
            "perks": perks,
            "spend_discount_percent": round(discount_fraction * 100.0, 1),
            "vanity": {
                "custom_greeting": vanity.get("custom_greeting"),
                "custom_color": vanity.get("chat_color"),
            },
            "vanity_costs": {
                "custom_greeting": greeting_cfg.cost,
                "custom_color": color_cfg.cost,
                "shoutout": shoutout_cfg.cost,
            },
            "vanity_enabled": {
                "custom_greeting": bool(greeting_cfg.enabled),
                "custom_color": bool(color_cfg.enabled),
                "shoutout": bool(shoutout_cfg.enabled),
            },
        }

    async def _purchase_vanity(
        self,
        username: str,
        channel: str,
        base_cost: int,
        item_type: str,
        value: str,
        trigger_id: str,
    ) -> dict[str, Any]:
        """Shared debit + persist logic for vanity purchases via the API.

        Raises ValueError (surfaced as a command error) on validation or
        funding failure.
        """
        spending = self._app.spending_engine
        account = await self._app.db.get_or_create_account(username, channel)

        if spending is not None:
            rank_tier = spending.get_rank_tier_index(account)
            final_cost, discount = spending.apply_discount(base_cost, rank_tier)
            validation = await spending.validate_spend(
                username, channel, final_cost, "vanity",
            )
            if validation is not None:
                raise ValueError(validation.message)
        else:
            final_cost, discount = base_cost, 0.0

        new_balance = await self._app.db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id=trigger_id,
            reason=f"Vanity: {item_type}",
        )
        if new_balance is None:
            raise ValueError("Insufficient funds.")

        await self._app.db.set_vanity_item(username, channel, item_type, value)
        if getattr(self._app, "metrics", None):
            self._app.metrics.record_vanity_purchase(final_cost)

        return {
            "username": username,
            "channel": channel,
            "item_type": item_type,
            "value": value,
            "charged": final_cost,
            "discount": discount,
            "new_balance": new_balance,
        }

    async def _handle_vanity_set_greeting(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        value = str(request.get("value", "")).strip()

        cfg = self._app.config.vanity_shop.custom_greeting
        if not cfg.enabled:
            raise ValueError("Custom greetings are not available.")
        if not value:
            raise ValueError("Greeting text is required.")
        if len(value) > 200:
            raise ValueError("Greeting text too long (max 200 characters).")

        return await self._purchase_vanity(
            username, channel, cfg.cost, "custom_greeting", value,
            "spend.vanity.custom_greeting",
        )

    async def _handle_vanity_set_color(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        raw = str(request.get("value", ""))

        cfg = self._app.config.vanity_shop.chat_color
        if not cfg.enabled:
            raise ValueError("Chat colors are not available.")
        hex_value = _normalize_hex_color(raw)
        if hex_value is None:
            raise ValueError("Invalid color. Provide a 6-digit hex like #1A2B3C.")

        # Capture the prior color so a failed CSS apply can be fully rolled back.
        prev_value = await self._app.db.get_vanity_item(username, channel, "chat_color")

        result = await self._purchase_vanity(
            username, channel, cfg.cost, "chat_color", hex_value,
            "spend.vanity.chat_color",
        )

        outcome = await self._apply_chat_color_css(channel, username)
        if outcome in ("error", "unavailable"):
            # The color was charged + stored but could not be pushed to the
            # channel — either the CSS write failed (robot/NATS outage) or the
            # channel's current CSS was unavailable so we refused to overwrite it.
            # Refund and roll the item back so the user isn't billed for a change
            # that didn't take effect, and so it isn't silently applied later.
            await self._refund_failed_vanity(
                username, channel, result["charged"], "chat_color",
                prev_value, "spend.vanity.chat_color",
            )
            raise ValueError(
                "Couldn't update your chat color right now — your Z has been "
                "refunded. Please try again in a moment."
            )
        return result

    # ── Chat-color CSS application ───────────────────────────

    def _domain_for_channel(self, channel: str) -> str | None:
        """Return the configured CyTube domain for ``channel`` (or None)."""
        for ch in self._app.config.channels:
            if ch.channel == channel:
                return ch.domain
        return None

    def _chat_color_protected_users(self) -> set[str]:
        """Lowercased set of users the CSS automation must never touch."""
        cfg = self._app.config.vanity_shop.chat_color
        protected = {u.lower() for u in cfg.protected_users}
        bot_username = getattr(self._app.config.bot, "username", "")
        if bot_username:
            protected.add(bot_username.lower())
        return protected

    async def _import_legacy_chat_colors(
        self, channel: str, existing_css: str, protected: set[str],
    ) -> int:
        """Import per-user colors that live only in the CSS into the database.

        Reads ``.chat-msg-*`` colors from the managed block and legacy rules and,
        for any non-protected user that has no ``chat_color`` row yet, persists
        one. Idempotent and additive: users already in the database, protected
        users, and non-hex values are skipped. Returns the number imported.
        """
        harvested = harvest_managed_colors(
            existing_css,
            begin_marker=self._app.config.vanity_shop.chat_color.css_block_begin,
            end_marker=self._app.config.vanity_shop.chat_color.css_block_end,
            legacy_marker=self._app.config.vanity_shop.chat_color.css_legacy_marker,
        )
        imported = 0
        for lower_user, (display, value) in harvested.items():
            if lower_user in protected:
                continue
            # Existence check must use the canonical (display) casing, since
            # vanity_items is stored case-sensitively. Checking the lowercased
            # name would miss an existing canonical-case row and re-import it.
            existing_db = await self._app.db.get_vanity_item(
                display, channel, "chat_color",
            )
            if existing_db:
                continue
            hex_value = _normalize_hex_color(value)
            if hex_value is None:
                continue
            await self._app.db.set_vanity_item(display, channel, "chat_color", hex_value)
            imported += 1
        if imported:
            self._logger.info(
                "Imported %d pre-existing chat color(s) from CSS for %s",
                imported, channel,
            )
        return imported

    async def _apply_chat_color_css(self, channel: str, buyer: str) -> str:
        """Rebuild and push the channel's auto-managed vanity-color CSS block.

        Reads the current channel CSS, optionally imports any colors that exist
        only in the CSS into the database, rebuilds the managed block (skipping
        protected users, preserving any remaining CSS-only colors), and writes it
        back.

        Returns an outcome string:

        * ``"applied"``     — the managed block was rebuilt and pushed.
        * ``"noop"``        — the rebuilt CSS matched the current CSS; nothing sent.
        * ``"disabled"``    — CSS application is off in config (DB-only mode).
        * ``"unavailable"`` — the channel's current CSS read back empty, so the
          real CSS is unavailable (Kryten-Robot has not seeded it into its state
          KV, or a transient outage). Writing would replace the channel's entire
          hand-maintained CSS with just our managed block, so we refuse. The
          caller refunds and rolls back on this outcome.
        * ``"error"``       — the CSS read or write raised. Caller refunds/rolls back.

        CRITICAL: an empty read is **never** written back. Every read layer
        (``get_state_channel_css`` → ``kv_get`` → low-level ``kv_get``) collapses
        a missing key or NATS error to ``""``, so an empty string does **not**
        mean "the channel has no CSS" — it means we could not read it. Treating
        empty as writable previously clobbered a channel's entire hand-maintained
        CSS (regression fixed in 0.10.2).
        """
        cfg = self._app.config.vanity_shop.chat_color
        if not cfg.apply_css:
            return "disabled"
        try:
            domain = self._domain_for_channel(channel)
            existing = await self._client.get_state_channel_css(channel, domain=domain)
            existing = existing or ""

            # Safety guard: refuse to write when the current CSS is empty/
            # unavailable. See the docstring — an empty read is indistinguishable
            # from "robot CSS not seeded" or a transient outage, and writing a
            # managed-block-only document would wipe all hand-maintained CSS.
            if not existing.strip():
                self._logger.warning(
                    "Chat-color CSS apply for %s skipped: current channel CSS is "
                    "empty/unavailable (refusing to overwrite hand-maintained CSS).",
                    channel,
                )
                return "unavailable"

            protected = self._chat_color_protected_users()

            # Import pre-existing CSS-only colors so they survive the rewrite and
            # become editable in the portal (idempotent; additive).
            if cfg.import_existing_colors:
                await self._import_legacy_chat_colors(channel, existing, protected)

            colors = await self._app.db.get_users_with_chat_colors(channel)

            new_css = merge_vanity_css(
                existing,
                colors,
                display_overrides={buyer.lower(): buyer},
                protected=protected,
                preserve_existing=cfg.import_existing_colors,
                selector_template=cfg.css_selector_template,
                begin_marker=cfg.css_block_begin,
                end_marker=cfg.css_block_end,
                legacy_marker=cfg.css_legacy_marker,
            )
            if new_css.strip() == existing.strip():
                return "noop"
            await self._client.set_channel_css(channel, new_css, domain=domain)
            self._logger.info(
                "Applied chat-color CSS for %s (%d managed user(s))",
                channel, len(colors),
            )
            return "applied"
        except Exception:
            self._logger.exception("Failed to apply chat-color CSS for %s", channel)
            return "error"

    async def _refund_failed_vanity(
        self,
        username: str,
        channel: str,
        amount: int,
        item_type: str,
        prev_value: str | None,
        trigger_id: str,
    ) -> None:
        """Reverse a vanity charge whose effect couldn't be applied.

        Refunds ``amount`` and restores the previous value (or deactivates the
        item when there was none), so a refunded purchase leaves no active trace.
        """
        await self._app.db.refund(
            username, channel, amount,
            trigger_id=f"{trigger_id}.refund",
            reason=f"Refund: {item_type} could not be applied",
        )
        if prev_value is not None:
            await self._app.db.set_vanity_item(username, channel, item_type, prev_value)
        else:
            await self._app.db.deactivate_vanity_item(username, channel, item_type)
        self._logger.warning(
            "Refunded %d Z to %s in %s: %s could not be applied (rolled back).",
            amount, username, channel, item_type,
        )

    async def _handle_vanity_resync_colors(self, request: dict[str, Any]) -> dict[str, Any]:
        """Ops command: import pre-existing CSS colors into the DB and re-apply.

        Lets an operator import the channel's hand-maintained chat colors into
        the economy without waiting for someone to make a new purchase. Reads the
        current CSS, imports any CSS-only colors (skipping protected users), and —
        unless ``apply`` is false — rewrites the managed CSS block. Idempotent.
        """
        channel = self._channel(request)
        cfg = self._app.config.vanity_shop.chat_color

        domain = self._domain_for_channel(channel)
        existing = await self._client.get_state_channel_css(channel, domain=domain)
        if not existing.strip():
            raise ValueError(
                "Channel CSS is empty or unavailable; cannot resync colors."
            )

        protected = self._chat_color_protected_users()
        imported = await self._import_legacy_chat_colors(channel, existing, protected)

        applied = False
        if bool(request.get("apply", True)) and cfg.apply_css:
            outcome = await self._apply_chat_color_css(channel, self._app.config.bot.username)
            applied = outcome in ("applied", "noop")

        total = len(await self._app.db.get_users_with_chat_colors(channel))
        return {
            "channel": channel,
            "imported": imported,
            "total_managed": total,
            "css_reapplied": applied,
        }

    async def _handle_vanity_shoutout(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        value = str(request.get("value", "")).strip()

        cfg = self._app.config.vanity_shop.shoutout
        if not cfg.enabled:
            raise ValueError("Shoutouts are not available.")
        if not value:
            raise ValueError("Shoutout message is required.")
        if len(value) > cfg.max_length:
            raise ValueError(f"Message too long (max {cfg.max_length} characters).")

        now = datetime.now(timezone.utc)
        last = self._shoutout_cooldowns.get((username.lower(), channel))
        if last is not None:
            elapsed = (now - last).total_seconds()
            cooldown = cfg.cooldown_minutes * 60
            if elapsed < cooldown:
                remaining = max(1, math.ceil((cooldown - elapsed) / 60))
                raise ValueError(f"Shoutout cooldown: {remaining} minute(s) remaining.")

        spending = self._app.spending_engine
        account = await self._app.db.get_or_create_account(username, channel)
        if spending is not None:
            rank_tier = spending.get_rank_tier_index(account)
            final_cost, discount = spending.apply_discount(cfg.cost, rank_tier)
            validation = await spending.validate_spend(
                username, channel, final_cost, "vanity",
            )
            if validation is not None:
                raise ValueError(validation.message)
        else:
            final_cost, discount = cfg.cost, 0.0

        new_balance = await self._app.db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id="spend.vanity.shoutout",
            reason="Vanity: Shoutout",
        )
        if new_balance is None:
            raise ValueError("Insufficient funds.")

        if getattr(self._app, "metrics", None):
            self._app.metrics.record_shoutout(final_cost)

        await self._client.send_chat(channel, f"📢 {username}: {value}")
        self._shoutout_cooldowns[(username.lower(), channel)] = now

        return {
            "username": username,
            "channel": channel,
            "message": value,
            "charged": final_cost,
            "discount": discount,
            "new_balance": new_balance,
        }

    async def _handle_rank_set(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        rank_name = str(request.get("rank_name", "")).strip()
        if not rank_name:
            raise ValueError("rank_name is required")

        await self._app.db.get_or_create_account(username, channel)
        await self._app.db.update_account_rank(username, channel, rank_name)
        return {
            "username": username,
            "channel": channel,
            "rank_name": rank_name,
        }

    async def _handle_rain(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        total_amount = int(request.get("total_amount", 0))
        admin = str(request.get("admin", "system"))

        if total_amount <= 0:
            raise ValueError("total_amount must be > 0")

        recipients = sorted(self._app.presence_tracker.get_connected_users(channel))
        if not recipients:
            raise ValueError("no connected users to rain on")

        each = total_amount // len(recipients)
        remainder = total_amount % len(recipients)

        if each <= 0:
            raise ValueError("total_amount too small for current recipient count")

        awarded = 0
        for idx, username in enumerate(recipients):
            amt = each + (1 if idx < remainder else 0)
            await self._app.db.credit(
                username,
                channel,
                amt,
                tx_type="rain",
                trigger_id="admin.rain",
                reason=f"Rain by {admin}",
            )
            awarded += amt

        return {
            "channel": channel,
            "recipients": len(recipients),
            "amount_each": each,
            "total_awarded": awarded,
        }

    async def _handle_user_ban(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        admin = str(request.get("admin", "system"))
        reason = str(request.get("reason", ""))

        created = await self._app.db.ban_user(username, channel, admin, reason)
        return {
            "username": username,
            "channel": channel,
            "banned": True,
            "new_ban": created,
            "reason": reason,
        }

    async def _handle_user_unban(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        removed = await self._app.db.unban_user(username, channel)
        return {
            "username": username,
            "channel": channel,
            "banned": False,
            "was_banned": removed,
        }

    async def _handle_announce(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        message = str(request.get("message", "")).strip()
        if not message:
            raise ValueError("message is required")
        await self._client.send_chat(channel, message)
        return {"channel": channel, "sent": True}

    async def _handle_approval_approve_gif(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        admin = str(request.get("admin", "system"))

        pending = await self._app.db.get_pending_approval(username, channel, "channel_gif")
        if not pending:
            raise ValueError(f"No pending GIF approval for {username}")

        await self._app.db.resolve_approval(int(pending["id"]), admin, True)
        await self._client.send_pm(channel, username, f"✅ Your channel GIF has been approved by {admin}!")
        return {
            "username": username,
            "channel": channel,
            "approved": True,
        }

    async def _handle_approval_reject_gif(self, request: dict[str, Any]) -> dict[str, Any]:
        username = self._username(request)
        channel = self._channel(request)
        admin = str(request.get("admin", "system"))

        pending = await self._app.db.get_pending_approval(username, channel, "channel_gif")
        if not pending:
            raise ValueError(f"No pending GIF approval for {username}")

        await self._app.db.resolve_approval(int(pending["id"]), admin, False)
        refund = int(pending["cost"])
        await self._app.db.credit(
            username,
            channel,
            refund,
            tx_type="refund",
            trigger_id="refund.gif_rejected",
            reason=f"Channel GIF rejected by {admin}",
        )
        await self._client.send_pm(
            channel,
            username,
            f"❌ Your channel GIF was rejected by {admin}. Your {refund:,} Z were refunded.",
        )
        return {
            "username": username,
            "channel": channel,
            "approved": False,
            "refund": refund,
        }

    async def _handle_leaderboard(self, request: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(request)
        category = str(request.get("category", "earners")).lower().strip()
        limit = int(request.get("limit", 10))
        limit = max(1, min(limit, 100))

        if category in {"earners", "today"}:
            rows = await self._app.db.get_top_earners_today(channel, limit=limit)
        elif category in {"rich", "balance", "balances"}:
            rows = await self._app.db.get_richest_users(channel, limit=limit)
        elif category in {"lifetime", "all"}:
            rows = await self._app.db.get_highest_lifetime(channel, limit=limit)
        elif category == "ranks":
            rows = await self._app.db.get_rank_distribution(channel)
            return {"channel": channel, "category": category, "distribution": rows}
        else:
            raise ValueError("unknown leaderboard category")

        return {"channel": channel, "category": category, "rows": rows}

    _HANDLER_MAP: dict[str, Any] = {
        "system.ping": _handle_ping,
        "system.about": _handle_about,
        "system.health": _handle_health,
        "balance.get": _handle_balance_get,
        "balance.adjust": _handle_balance_adjust,
        "balance.set": _handle_balance_set,
        "balance.search": _handle_balance_search,
        "transactions.list": _handle_transactions_list,
        "transactions.recent": _handle_transactions_recent,
        "stats.float": _handle_stats_float,
        "stats.summary": _handle_stats_summary,
        "stats.health": _handle_stats_health,
        "gambling.stats": _handle_gambling_stats,
        "config.reload": _handle_config_reload,
        "event.start": _handle_event_start,
        "event.stop": _handle_event_stop,
        "events.list": _handle_events_list,
        "triggers.stats": _handle_triggers_stats,
        "user.detail": _handle_user_detail,
        "account.summary": _handle_account_summary,
        "vanity.set_greeting": _handle_vanity_set_greeting,
        "vanity.set_color": _handle_vanity_set_color,
        "vanity.shoutout": _handle_vanity_shoutout,
        "vanity.resync_colors": _handle_vanity_resync_colors,
        "rank.set": _handle_rank_set,
        "rain": _handle_rain,
        "user.ban": _handle_user_ban,
        "user.unban": _handle_user_unban,
        "announce": _handle_announce,
        "approval.approve_gif": _handle_approval_approve_gif,
        "approval.reject_gif": _handle_approval_reject_gif,
        "leaderboard": _handle_leaderboard,
        "spending.queue_preview": _handle_spending_queue_preview,
        "spending.queue": _handle_spending_queue,
        "spending.queue_refund": _handle_spending_queue_refund,
    }
