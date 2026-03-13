"""Request-reply command handler on kryten.economy.command.

Provides a NATS request-reply API for inter-service communication
and admin tooling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import load_config

if TYPE_CHECKING:
    from kryten import KrytenClient

    from .main import EconomyApp


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
        "rank.set": _handle_rank_set,
        "rain": _handle_rain,
        "user.ban": _handle_user_ban,
        "user.unban": _handle_user_unban,
        "announce": _handle_announce,
        "approval.approve_gif": _handle_approval_approve_gif,
        "approval.reject_gif": _handle_approval_reject_gif,
        "leaderboard": _handle_leaderboard,
    }
