"""Request-reply command handler on kryten.economy.command.

Provides a NATS request-reply API for inter-service communication
and admin tooling.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import __version__

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
    #  Sprint 1 Commands
    # ══════════════════════════════════════════════════════════

    async def _handle_ping(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "version": __version__}

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
        username = request.get("username")
        channel = request.get("channel")
        if not username or not channel:
            raise ValueError("username and channel are required")

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

    _HANDLER_MAP: dict[str, Any] = {
        "system.ping": _handle_ping,
        "system.health": _handle_health,
        "balance.get": _handle_balance_get,
    }
