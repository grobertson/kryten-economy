"""PM command handler — processes user commands sent via PM.

Subscribes to 'pm' events via @client.on("pm"). Parses incoming PM text
as commands, dispatches to handlers, and sends responses via client.send_pm().
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from .database import EconomyDatabase
from .gambling_engine import GambleOutcome
from .presence_tracker import PresenceTracker

if TYPE_CHECKING:
    from kryten import ChatMessageEvent, KrytenClient

    from .achievement_engine import AchievementEngine
    from .bounty_manager import BountyManager
    from .channel_state import ChannelStateTracker
    from .config import EconomyConfig
    from .earning_engine import EarningEngine
    from .gambling_engine import GamblingEngine, GambleOutcome
    from .media_client import MediaCMSClient
    from .multiplier_engine import MultiplierEngine
    from .rank_engine import RankEngine
    from .spending_engine import SpendingEngine


# ══════════════════════════════════════════════════════════
#  Sprint 9: PM Rate Limiter
# ══════════════════════════════════════════════════════════

class PmRateLimiter:
    """Sliding-window rate limiter for PM commands per user."""

    def __init__(self, max_per_minute: int = 10) -> None:
        self._max = max_per_minute
        self._counters: dict[str, list[float]] = {}

    def check(self, username: str) -> bool:
        """Return True if the command should be allowed."""
        now = datetime.now(timezone.utc).timestamp()
        window = self._counters.get(username, [])

        # Prune old entries
        cutoff = now - 60
        window = [t for t in window if t > cutoff]

        if len(window) >= self._max:
            self._counters[username] = window
            return False

        window.append(now)
        self._counters[username] = window
        return True

    def cleanup(self) -> None:
        """Remove stale entries (call periodically)."""
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - 120
        stale = [k for k, v in self._counters.items() if all(t < cutoff for t in v)]
        for k in stale:
            del self._counters[k]


class PmHandler:
    """Handles PM commands from users."""

    def __init__(
        self,
        config: EconomyConfig,
        database: EconomyDatabase,
        client: KrytenClient | None,
        presence_tracker: PresenceTracker,
        logger: logging.Logger | None = None,
        earning_engine: EarningEngine | None = None,
        channel_state: ChannelStateTracker | None = None,
        gambling_engine: GamblingEngine | None = None,
        spending_engine: SpendingEngine | None = None,
        media_client: MediaCMSClient | None = None,
        achievement_engine: AchievementEngine | None = None,
        rank_engine: RankEngine | None = None,
        multiplier_engine: MultiplierEngine | None = None,
        bounty_manager: BountyManager | None = None,
    ) -> None:
        self._config = config
        self._db = database
        self._client = client
        self._presence_tracker = presence_tracker
        self._earning_engine = earning_engine
        self._channel_state = channel_state
        self._gambling_engine = gambling_engine
        self._spending = spending_engine
        self._media = media_client
        self._achievement_engine = achievement_engine
        self._rank_engine = rank_engine
        self._multiplier_engine = multiplier_engine
        self._bounty_manager = bounty_manager
        self._logger = logger or logging.getLogger("economy.pm")

        self._ignored_users: set[str] = {u.lower() for u in config.ignored_users}
        self._bot_username_lower = config.bot.username.lower()
        self._symbol = config.currency.symbol
        self._currency_name = config.currency.name

        # In-memory cooldown / once-per-day trackers
        self._shoutout_cooldowns: dict[tuple[str, str], datetime] = {}
        self._daily_fortune_used: set[str] = set()

        # Win-announcement throttle: per-channel tracker
        # key = channel, value = (last_announce_utc, biggest_payout_today, today_date_str)
        self._win_announce_tracker: dict[str, tuple[datetime, int, str]] = {}

        # Sprint 9: PM rate limiter
        self._rate_limiter = PmRateLimiter(
            max_per_minute=config.commands.rate_limit_per_minute,
        )

        # PM delivery queue — throttled to 1 message per _PM_SEND_INTERVAL
        self._pm_queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()
        self._pm_worker_task: asyncio.Task | None = None

        # Command dispatch map
        self._command_map: dict[str, Callable[..., Awaitable[str]]] = {
            "help": self._cmd_help,
            "balance": self._cmd_balance,
            "bal": self._cmd_balance,
            "rewards": self._cmd_rewards,
            "like": self._cmd_like,
            "spin": self._cmd_spin,
            "flip": self._cmd_flip,
            "challenge": self._cmd_challenge,
            "accept": self._cmd_accept,
            "decline": self._cmd_decline,
            "heist": self._cmd_heist,
            "gambling": self._cmd_gambling_stats,
            "stats": self._cmd_gambling_stats,
            # Sprint 5 — Spending
            "search": self._cmd_search,
            "queue": self._cmd_queue,
            "playnext": self._cmd_playnext,
            "forcenow": self._cmd_forcenow,
            "tip": self._cmd_tip,
            "shop": self._cmd_shop,
            "buy": self._cmd_buy,
            "fortune": self._cmd_fortune,
            "history": self._cmd_history,
            # Sprint 6 — Ranks & Achievements
            "rank": self._cmd_rank,
            "profile": self._cmd_profile,
            "achievements": self._cmd_achievements,
            "top": self._cmd_top,
            "leaderboard": self._cmd_top,
            "lb": self._cmd_top,
            "shoutout": self._cmd_shoutout,
            # Sprint 7 — Events, Multipliers & Bounties
            "bounty": self._cmd_bounty,
            "bounties": self._cmd_bounties,
            "events": self._cmd_events,
            "multipliers": self._cmd_events,
        }

        # Admin commands (CyTube rank >= owner_level)
        self._admin_command_map: dict[str, Callable[..., Awaitable[str]]] = {
            "event": self._cmd_event,
            "claim_bounty": self._cmd_claim_bounty,
            # Sprint 8 — Admin commands
            "grant": self._cmd_grant,
            "deduct": self._cmd_deduct,
            "rain": self._cmd_rain,
            "set_balance": self._cmd_set_balance,
            "set_rank": self._cmd_set_rank,
            "reload": self._cmd_reload,
            "econ:stats": self._cmd_econ_stats,
            "econ:user": self._cmd_econ_user,
            "econ:health": self._cmd_econ_health,
            "econ:triggers": self._cmd_econ_triggers,
            "econ:gambling": self._cmd_econ_gambling,
            "approve_gif": self._cmd_approve_gif,
            "reject_gif": self._cmd_reject_gif,
            "ban": self._cmd_ban,
            "unban": self._cmd_unban,
            "announce": self._cmd_announce,
        }

    async def handle_pm(self, event: ChatMessageEvent) -> None:
        """Process an incoming PM event."""
        username = event.username
        channel = event.channel

        # Ignore messages from ignored users and self
        if username.lower() in self._ignored_users:
            return
        if username.lower() == self._bot_username_lower:
            return

        text = event.message.strip()
        if not text:
            return

        # Sprint 9: Rate limiting
        if not self._rate_limiter.check(username):
            await self._send_pm(channel, username, "⏳ Slow down! Try again in a moment.")
            return

        parts = text.split(None, 1)
        command = parts[0].lower()
        args = parts[1].split() if len(parts) > 1 else []

        try:
            # Admin command dispatch (CyTube rank gate)
            admin_handler = self._admin_command_map.get(command)
            if admin_handler:
                cytube_rank = await self._resolve_cytube_rank(event, channel, username)
                admin_level = self._config.admin.owner_level
                if cytube_rank < admin_level:
                    response = "⛔ This command requires admin privileges."
                else:
                    response = await admin_handler(username, channel, args)
                await self._send_pm(channel, username, response)
                return

            # Ban check for non-admin commands
            if await self._db.is_banned(username, channel):
                await self._send_pm(channel, username, "⛔ Your economy access has been suspended.")
                return

            handler = self._command_map.get(command)
            if handler:
                response = await handler(username, channel, args)
            else:
                response = "❓ Unknown command. Try 'help'."

            await self._send_pm(channel, username, response)
        except Exception:
            self._logger.exception(
                "Command handler error for %s/%s", username, command,
            )
            await self._send_pm(
                channel, username,
                "❌ Something went wrong processing your command. Please try again.",
            )

    # ══════════════════════════════════════════════════════════
    #  Commands
    # ══════════════════════════════════════════════════════════

    async def _cmd_help(self, username: str, channel: str, args: list[str]) -> str:
        s = self._symbol
        lines = [
            "Economy Bot",
            "━" * 15,
            "",
            "💰 Basics",
            "  balance · rewards",
            "  like · history",
            "",
            "🎰 Gambling",
            "  spin · spin <wager>",
            "  flip <wager>",
            "  challenge @user <amt>",
            "  accept · decline",
            "  gambling",
        ]
        if self._config.gambling.heist.enabled:
            lines.append("  heist <wager>")
            lines.append("  heist join")
        lines.extend([
            "",
            "🎬 Media",
            "  search · queue",
            "",
            "🛒 Shop & Social",
            "  shop · tip",
            "  fortune · shoutout",
            "",
            "📊 Progress",
            "  rank · profile",
            "  achievements · top",
            "",
            "📌 Bounties & Events",
            "  bounty · bounties",
            "  events",
            "",
            "━" * 15,
            "Discover more as you go 🍿",
        ])
        return "\n".join(lines)

    async def _cmd_balance(self, username: str, channel: str, args: list[str]) -> str:
        account = await self._db.get_or_create_account(username, channel)
        balance = account["balance"]
        rank = account["rank_name"]
        symbol = self._config.currency.symbol
        currency_name = account.get("personal_currency_name") or self._config.currency.name

        return f"💰 Balance: {balance:,} {symbol} ({currency_name})\n⭐ Rank: {rank}"

    async def _cmd_rewards(self, username: str, channel: str, args: list[str]) -> str:
        """Show non-hidden earning triggers."""
        lines = [
            f"💰 How to earn {self._currency_name}:",
            "━" * 15,
            "",
            "📍 Passive",
            f"  Stay connected: {self._config.presence.base_rate_per_minute} {self._symbol}/min",
        ]

        milestones = self._config.presence.hourly_milestones
        if milestones:
            for h, r in sorted(milestones.items()):
                lines.append(f"  {h}h dwell bonus: {r} {self._symbol}")

        if self._config.rain.enabled:
            lines.append("  ☔ Random rain drops")

        # Streaks section
        streak_lines: list[str] = []
        if self._config.streaks.daily.enabled:
            streak_lines.append(f"  Day 2+ earns bonus {self._symbol}")
        if self._config.streaks.weekend_weekday_bridge.enabled:
            streak_lines.append(
                f"  🌉 Bridge bonus: "
                f"{self._config.streaks.weekend_weekday_bridge.bonus} {self._symbol}/week"
            )
        if streak_lines:
            lines.append("")
            lines.append("🔥 Streaks")
            lines.extend(streak_lines)

        # Non-hidden chat/content/social triggers
        all_triggers = [
            ("chat_triggers", self._config.chat_triggers),
            ("content_triggers", self._config.content_triggers),
            ("social_triggers", self._config.social_triggers),
        ]

        trigger_lines: list[str] = []
        for _section_name, section in all_triggers:
            for trigger_name, trigger_cfg in self._iter_trigger_configs(section):
                if hasattr(trigger_cfg, "hidden") and trigger_cfg.hidden:
                    continue
                if not getattr(trigger_cfg, "enabled", True):
                    continue
                reward = self._get_trigger_reward_text(trigger_cfg)
                desc = self._get_trigger_description(trigger_name)
                trigger_lines.append(f"  • {desc}: {reward}")

        if trigger_lines:
            lines.append("")
            lines.append("💬 Activity")
            lines.extend(trigger_lines)

        lines.append("")
        lines.append("🔮 Hidden triggers exist too!")

        return "\n".join(lines)

    async def _cmd_like(self, username: str, channel: str, args: list[str]) -> str:
        """Like the currently playing media."""
        if self._earning_engine is None:
            return "Likes are currently disabled."

        result = await self._earning_engine.evaluate_like_current(username, channel)

        if result.amount > 0:
            media = (
                self._channel_state.get_current_media(channel)
                if self._channel_state
                else None
            )
            title = media.title if media else "current media"
            return f"👍 Liked \"{title}\"! +{result.amount} {self._symbol}"

        if result.blocked_by == "cap":
            return "You've already liked this one!"
        if result.blocked_by == "disabled":
            return "Likes are currently disabled."

        return "Nothing playing right now."

    # ══════════════════════════════════════════════════════════
    #  Gambling Win Announcement Throttle
    # ══════════════════════════════════════════════════════════

    def _should_announce_gambling_win(self, channel: str, payout: int) -> bool:
        """Decide whether a gambling win deserves a public announcement.

        Rules:
        - Always announce jackpots (handled separately, bypass this).
        - At most one win announcement per hour per channel.
        - OR if the payout beats today's highest announced win.
        - Resets daily.
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        tracker = self._win_announce_tracker.get(channel)

        if tracker is None or tracker[2] != today:
            # First win of the day — always announce, seed tracker
            self._win_announce_tracker[channel] = (now, payout, today)
            return True

        last_time, biggest_today, _ = tracker
        elapsed = (now - last_time).total_seconds()

        # New daily high score — always announce
        if payout > biggest_today:
            self._win_announce_tracker[channel] = (now, payout, today)
            return True

        # Cooldown: at most once per hour
        if elapsed >= 3600:
            self._win_announce_tracker[channel] = (now, max(biggest_today, payout), today)
            return True

        return False

    # ══════════════════════════════════════════════════════════
    #  Gambling Commands
    # ══════════════════════════════════════════════════════════

    async def _cmd_spin(self, username: str, channel: str, args: list[str]) -> str:
        """Slot machine spin — no args = free daily spin."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        if not args:
            result = await self._gambling_engine.daily_free_spin(username, channel)
            if result.payout > 0 and self._should_announce_gambling_win(channel, result.payout):
                template = getattr(self._config.announcements.templates, "free_spin_win", None)
                if template:
                    msg = template.format(
                        user=username, amount=f"{result.payout:,}",
                        currency=self._currency_name,
                    )
                else:
                    msg = f"🎁 {username} won {result.payout:,} {self._currency_name} on a FREE spin!"
                await self._announce_chat(channel, msg)
            return result.message

        try:
            wager = int(args[0])
        except ValueError:
            return "Usage: spin [wager] (no wager = free daily spin)"

        result = await self._gambling_engine.spin(username, channel, wager)
        if result.announce_public:
            # Jackpots always get announced — bypass throttle
            await self._announce_chat(
                channel,
                f"🎰 JACKPOT! {username} just won {result.payout:,} {self._symbol} on the slots!",
            )
        elif result.outcome == GambleOutcome.WIN and self._should_announce_gambling_win(channel, result.payout):
            await self._announce_chat(
                channel,
                f"🎰 {username} won {result.payout:,} {self._symbol} on the slots!",
            )
        return result.message

    async def _cmd_flip(self, username: str, channel: str, args: list[str]) -> str:
        """Coin flip — double-or-nothing."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        if not args:
            return "Usage: flip <wager> (e.g. 'flip 100')"

        try:
            wager = int(args[0])
        except ValueError:
            return "Usage: flip <wager>"

        result = await self._gambling_engine.flip(username, channel, wager)

        # Throttled public announcement for wins
        if result.outcome == GambleOutcome.WIN and self._should_announce_gambling_win(channel, result.payout):
            template = getattr(self._config.announcements.templates, "flip_win", None)
            if template:
                msg = template.format(
                    user=username, amount=f"{result.payout:,}",
                    currency=self._currency_name, wager=f"{result.wager:,}",
                )
            else:
                msg = f"🪙 {username} flipped and won {result.payout:,} {self._currency_name}!"
            await self._announce_chat(channel, msg)

        return result.message

    async def _cmd_challenge(self, username: str, channel: str, args: list[str]) -> str:
        """Challenge another user to a duel."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        if len(args) < 2:
            return "Usage: challenge @user <wager>"

        target = args[0].lstrip("@")
        try:
            wager = int(args[1])
        except ValueError:
            return "Usage: challenge @user <wager>"

        result = await self._gambling_engine.create_challenge(
            username, target, channel, wager,
        )

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

    async def _cmd_accept(self, username: str, channel: str, args: list[str]) -> str:
        """Accept a pending challenge."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        challenge = await self._db.get_pending_challenge_for_target(username, channel)
        challenger_name = challenge["challenger"] if challenge else None

        target_msg, challenger_msg, public_msg = (
            await self._gambling_engine.accept_challenge(username, channel)
        )
        if challenger_msg and challenger_name:
            await self._send_pm(channel, challenger_name, challenger_msg)
        if public_msg:
            await self._announce_chat(channel, public_msg)
        return target_msg

    async def _cmd_decline(self, username: str, channel: str, args: list[str]) -> str:
        """Decline a pending challenge."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        challenge = await self._db.get_pending_challenge_for_target(username, channel)
        challenger_name = challenge["challenger"] if challenge else None

        target_msg, challenger_msg = await self._gambling_engine.decline_challenge(
            username, channel,
        )
        if challenger_msg and challenger_name:
            await self._send_pm(channel, challenger_name, challenger_msg)
        return target_msg

    async def _cmd_heist(self, username: str, channel: str, args: list[str]) -> str:
        """Start or join a heist."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        arg_text = " ".join(args).strip().lower()

        if arg_text == "join":
            heist = self._gambling_engine.get_active_heist(channel)
            if not heist:
                return "No active heist to join."
            wager = list(heist.participants.values())[0]
            return await self._gambling_engine.join_heist(username, channel, wager)

        if not arg_text:
            return "Usage: heist <wager> or heist join"

        try:
            wager = int(arg_text)
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

    async def _cmd_gambling_stats(
        self, username: str, channel: str, args: list[str],
    ) -> str:
        """Show personal gambling statistics."""
        if self._gambling_engine is None:
            return "Gambling is currently disabled."

        return await self._gambling_engine.get_stats_message(username, channel)

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Queue / Search Commands
    # ══════════════════════════════════════════════════════════

    async def _cmd_search(self, username: str, channel: str, args: list[str]) -> str:
        """Search the MediaCMS catalog."""
        if not self._media or not self._config.mediacms.base_url:
            return "📽️ Content queuing is not configured for this channel."
        if not args:
            return "Usage: search <query>"

        query = " ".join(args)
        results = await self._media.search(query)
        if not results:
            return f"No results found for '{query}'."

        account = await self._db.get_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account) if account and self._spending else 0

        lines = [f"🔍 Found {len(results)} result(s) for '{query}':"]
        for i, item in enumerate(results, 1):
            duration_str = self._format_duration(item["duration"])
            tier_label, base_cost = self._spending.get_price_tier(item["duration"]) if self._spending else ("", 0)
            final_cost, discount = self._spending.apply_discount(base_cost, rank_tier) if self._spending else (base_cost, 0)

            cost_str = f"{final_cost:,} Z"
            if discount > 0:
                cost_str += f" ({int(discount * 100)}% off!)"

            lines.append(
                f"  {i}. \"{item['title']}\" ({duration_str}) — ID: {item['id']} · {cost_str}"
            )
        return "\n".join(lines)

    async def _cmd_queue(self, username: str, channel: str, args: list[str]) -> str:
        """Queue a MediaCMS item for the configured cost."""
        if not self._media or not self._spending:
            return "📽️ Content queuing is not configured for this channel."
        if not args:
            return "Usage: queue <id>"

        media_id = args[0]
        return await self._queue_media(username, channel, media_id, "queue")

    async def _cmd_playnext(self, username: str, channel: str, args: list[str]) -> str:
        """Queue a MediaCMS item to play next (premium cost)."""
        if not self._media or not self._spending:
            return "📽️ Content queuing is not configured for this channel."
        if not args:
            return "Usage: playnext <id>"

        media_id = args[0]
        return await self._queue_media(username, channel, media_id, "playnext")

    async def _cmd_forcenow(self, username: str, channel: str, args: list[str]) -> str:
        """Force-play a MediaCMS item immediately (highest cost)."""
        if not self._media or not self._spending:
            return "📽️ Content queuing is not configured for this channel."
        if not args:
            return "Usage: forcenow <id>"

        media_id = args[0]
        return await self._queue_media(username, channel, media_id, "forcenow")

    async def _queue_media(
        self, username: str, channel: str, media_id: str, queue_type: str,
    ) -> str:
        """Shared queue/playnext/forcenow logic."""
        assert self._media is not None
        assert self._spending is not None

        # Blackout check (not for forcenow)
        if queue_type != "forcenow" and self._config.spending.blackout_windows:
            # Simple: skip for now if no croniter; tested via mock
            pass

        # Daily limit
        queues_today = await self._db.get_queues_today(username, channel)
        max_queues = self._config.spending.max_queues_per_day
        if queues_today >= max_queues:
            return f"Daily queue limit reached ({max_queues}/{max_queues}). Try again tomorrow!"

        # Cooldown (not for forcenow)
        if queue_type != "forcenow":
            last_queue = await self._db.get_last_queue_time(username, channel)
            if last_queue:
                cooldown = self._config.spending.queue_cooldown_minutes * 60
                elapsed = (datetime.now(timezone.utc) - last_queue).total_seconds()
                if elapsed < cooldown:
                    remaining = int((cooldown - elapsed) / 60) + 1
                    return f"⏳ Queue cooldown: {remaining} minute(s) remaining."

        # Fetch media info
        item = await self._media.get_by_id(media_id)
        if not item:
            return f"Media '{media_id}' not found in the catalog."

        # Calculate cost
        account = await self._db.get_or_create_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account)

        if queue_type == "playnext":
            base_cost = self._config.spending.interrupt_play_next
        elif queue_type == "forcenow":
            base_cost = self._config.spending.force_play_now
        else:
            _tier_label, base_cost = self._spending.get_price_tier(item["duration"])

        final_cost, discount = self._spending.apply_discount(base_cost, rank_tier)

        # Forcenow with admin gate → create approval
        if queue_type == "forcenow" and self._config.spending.force_play_requires_admin:
            validation = await self._spending.validate_spend(username, channel, final_cost, "forcenow")
            if validation:
                return validation.message

            new_balance = await self._db.debit(
                username, channel, final_cost,
                tx_type="spend", trigger_id="spend.forcenow",
                reason=f"Force-Play (pending approval): \"{item['title']}\"",
            )
            if new_balance is None:
                return "Insufficient funds."

            approval_id = await self._db.create_pending_approval(
                username, channel, "force_play",
                data={"media_id": media_id, "title": item["title"],
                       "media_type": item["media_type"], "media_ext_id": item["media_id"]},
                cost=final_cost,
            )
            return (
                f"📝 Force-play request submitted for \"{item['title']}\".\n"
                f"Charged: {final_cost:,} Z (refunded if rejected) · Approval ID: {approval_id}"
            )

        # Standard queue / playnext / ungated forcenow
        validation = await self._spending.validate_spend(username, channel, final_cost, queue_type)
        if validation:
            return validation.message

        trigger_id = f"spend.{queue_type}"
        new_balance = await self._db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id=trigger_id,
            reason=f"Queue: \"{item['title']}\"",
        )
        if new_balance is None:
            return "Insufficient funds."

        # Queue the media via kryten-py
        position = "next" if queue_type in ("playnext", "forcenow") else None
        if self._client:
            if position:
                await self._client.add_media(channel, item["media_type"], item["media_id"], position=position)
            else:
                await self._client.add_media(channel, item["media_type"], item["media_id"])

        duration_str = self._format_duration(item["duration"])
        discount_str = ""
        if discount > 0:
            discount_str = f" ({int(discount * 100)}% off)"

        # Public announcement
        if self._config.announcements.queue_purchase and self._client:
            template = self._config.announcements.templates.queue
            announce_msg = template.format(
                user=username, title=item["title"],
                cost=final_cost, currency=self._currency_name,
            )
            await self._announce_chat(channel, announce_msg)

        emoji = {"queue": "🎬", "playnext": "⏭️", "forcenow": "🎬💥"}.get(queue_type, "🎬")
        return (
            f"{emoji} Queued \"{item['title']}\" ({duration_str}).\n"
            f"Charged: {final_cost:,} Z{discount_str} · Balance: {new_balance:,} Z"
        )

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Tipping
    # ══════════════════════════════════════════════════════════

    async def _cmd_tip(self, username: str, channel: str, args: list[str]) -> str:
        """Tip another user Z coins."""
        if not self._config.tipping.enabled:
            return "Tipping is not enabled."
        if len(args) < 2:
            return "Usage: tip @user <amount>"

        target = args[0].lstrip("@")
        try:
            amount = int(args[1])
        except ValueError:
            return "Amount must be a whole number."

        if amount < self._config.tipping.min_amount:
            return f"Minimum tip: {self._config.tipping.min_amount} Z."

        if target.lower() == username.lower():
            return "You can't tip yourself! 🤦"

        if target.lower() in self._ignored_users:
            return "That user is not participating in the economy."

        # Account age check for sender
        sender_account = await self._db.get_or_create_account(username, channel)
        first_seen = sender_account.get("first_seen")
        if first_seen:
            try:
                if isinstance(first_seen, str):
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                        try:
                            fs_dt = datetime.strptime(first_seen, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
                    else:
                        fs_dt = datetime.fromisoformat(first_seen).replace(tzinfo=timezone.utc)
                else:
                    fs_dt = first_seen
                age_minutes = (datetime.now(timezone.utc) - fs_dt).total_seconds() / 60
                if age_minutes < self._config.tipping.min_account_age_minutes:
                    return "Your account is too new to send tips. Keep hanging out!"
            except (ValueError, TypeError):
                pass

        # Target must exist
        target_account = await self._db.get_account(target, channel)
        if not target_account:
            return f"User '{target}' doesn't have an economy account yet."

        # Daily cap
        tips_today = await self._db.get_tips_sent_today(username, channel)
        if tips_today + amount > self._config.tipping.max_per_day:
            remaining = self._config.tipping.max_per_day - tips_today
            return f"Daily tip limit: {self._config.tipping.max_per_day:,} Z. You have {remaining:,} Z remaining today."

        # Debit sender
        new_balance = await self._db.debit(
            username, channel, amount,
            tx_type="tip_send", trigger_id="spend.tip",
            reason=f"Tip to {target}",
        )
        if new_balance is None:
            return "Insufficient funds."

        # Credit receiver
        await self._db.credit(
            target, channel, amount,
            tx_type="tip_receive", trigger_id="earn.tip",
            reason=f"Tip from {username}",
        )

        # Record in tip_history
        await self._db.record_tip(username, target, channel, amount)

        # PM to receiver
        if self._client:
            await self._client.send_pm(
                channel, target,
                f"💸 {username} just tipped you {amount:,} {self._symbol}!",
            )

        return f"💸 Tipped {target} {amount:,} {self._symbol}. Your balance: {new_balance:,} {self._symbol}"

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Vanity Shop
    # ══════════════════════════════════════════════════════════

    async def _cmd_shop(self, username: str, channel: str, args: list[str]) -> str:
        """List vanity shop items and prices."""
        if not self._spending:
            return "Shop is not available."

        account = await self._db.get_or_create_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account)
        symbol = self._symbol

        lines = ["🛒 Vanity Shop", "━" * 15]

        shop_items: list[tuple[str, Any, str]] = [
            ("greeting", self._config.vanity_shop.custom_greeting, "buy greeting <text>"),
            ("title", self._config.vanity_shop.custom_title, "buy title <text>"),
            ("color", self._config.vanity_shop.chat_color, "buy color <name>"),
            ("gif", self._config.vanity_shop.channel_gif, "buy gif <url>"),
            ("shoutout", self._config.vanity_shop.shoutout, "buy shoutout <message>"),
            ("fortune", self._config.vanity_shop.daily_fortune, "fortune"),
            ("rename", self._config.vanity_shop.rename_currency_personal, "buy rename <name>"),
        ]

        for item_key, item_cfg, usage in shop_items:
            if not getattr(item_cfg, "enabled", True):
                continue
            cost = getattr(item_cfg, "cost", 0)
            final_cost, discount = self._spending.apply_discount(cost, rank_tier)
            cost_str = f"{final_cost:,} {symbol}"
            if discount > 0:
                cost_str += f" (was {cost:,})"
            lines.append("")
            lines.append(f"  {item_key} — {cost_str}")
            lines.append(f"    → {usage}")

        owned = await self._db.get_all_vanity_items(username, channel)
        if owned:
            lines.append("")
            lines.append("━" * 15)
            lines.append("Your items:")
            for item_type, value in owned.items():
                display = value[:30] + "..." if len(value) > 30 else value
                lines.append(f"  ✅ {item_type}: {display}")

        return "\n".join(lines)

    async def _cmd_buy(self, username: str, channel: str, args: list[str]) -> str:
        """Purchase a vanity item."""
        if not args:
            return "Usage: buy <item> [args]. Try 'shop' to see available items."
        if not self._spending:
            return "Shop is not available."

        item_key = args[0].lower()
        item_args = " ".join(args[1:]) if len(args) > 1 else ""

        handlers: dict[str, Callable[..., Awaitable[str]]] = {
            "greeting": self._buy_custom_greeting,
            "title": self._buy_custom_title,
            "color": self._buy_chat_color,
            "gif": self._buy_channel_gif,
            "shoutout": self._buy_shoutout,
            "rename": self._buy_rename_currency,
            "cytube2": self._buy_cytube2,
            "level2": self._buy_cytube2,
        }

        handler = handlers.get(item_key)
        if not handler:
            return f"Unknown item '{item_key}'. Try 'shop' to see available items."

        return await handler(username, channel, item_args)

    async def _buy_custom_greeting(self, username: str, channel: str, value: str) -> str:
        """Purchase a custom greeting."""
        cfg = self._config.vanity_shop.custom_greeting
        if not cfg.enabled:
            return "Custom greetings are not available."
        if not value:
            return "Usage: buy greeting <your greeting text>"
        if len(value) > 200:
            return "Greeting text too long (max 200 characters)."

        return await self._complete_vanity_purchase(
            username, channel, cfg.cost, "custom_greeting", value,
            "spend.vanity.custom_greeting",
            f"✅ Custom greeting set! You'll be greeted with:\n  \"{value}\"",
        )

    async def _buy_custom_title(self, username: str, channel: str, value: str) -> str:
        """Purchase a custom title."""
        cfg = self._config.vanity_shop.custom_title
        if not cfg.enabled:
            return "Custom titles are not available."
        if not value:
            return "Usage: buy title <your title text>"
        if len(value) > 100:
            return "Title text too long (max 100 characters)."

        return await self._complete_vanity_purchase(
            username, channel, cfg.cost, "custom_title", value,
            "spend.vanity.custom_title",
            f"✅ Custom title set to: \"{value}\"",
        )

    async def _buy_chat_color(self, username: str, channel: str, value: str) -> str:
        """Purchase a chat colour from the approved palette."""
        cfg = self._config.vanity_shop.chat_color
        if not cfg.enabled:
            return "Chat colors are not available."
        if not value:
            palette_list = ", ".join(c.name for c in cfg.palette)
            return f"Usage: buy color <name>\nAvailable: {palette_list}"

        # Find in palette (case-insensitive)
        color_match = None
        for option in cfg.palette:
            if option.name.lower() == value.lower():
                color_match = option
                break
        if not color_match:
            palette_list = ", ".join(c.name for c in cfg.palette)
            return f"Unknown color '{value}'. Available: {palette_list}"

        return await self._complete_vanity_purchase(
            username, channel, cfg.cost, "chat_color", color_match.hex,
            "spend.vanity.chat_color",
            f"🎨 Chat color set to {color_match.name} ({color_match.hex})!",
        )

    async def _buy_channel_gif(self, username: str, channel: str, value: str) -> str:
        """Purchase a channel GIF (requires admin approval)."""
        cfg = self._config.vanity_shop.channel_gif
        if not cfg.enabled:
            return "Channel GIFs are not available."
        if not value:
            return "Usage: buy gif <gif_url>"
        if not value.startswith(("http://", "https://")):
            return "Please provide a valid URL for your GIF."

        assert self._spending is not None
        account = await self._db.get_or_create_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account)
        final_cost, _discount = self._spending.apply_discount(cfg.cost, rank_tier)

        validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
        if validation:
            return validation.message

        new_balance = await self._db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id="spend.vanity.channel_gif",
            reason="Vanity: Channel GIF (pending approval)",
        )
        if new_balance is None:
            return "Insufficient funds."

        approval_id = await self._db.create_pending_approval(
            username, channel, "channel_gif",
            data={"gif_url": value},
            cost=final_cost,
        )

        return (
            f"📝 Channel GIF submitted for admin approval!\n"
            f"URL: {value}\n"
            f"Charged: {final_cost:,} Z (refunded if rejected) · Balance: {new_balance:,} Z\n"
            f"Approval ID: {approval_id}"
        )

    async def _cmd_shoutout(self, username: str, channel: str, args: list[str]) -> str:
        """Direct shoutout command — forwards to buy shoutout."""
        if not args:
            return "Usage: shoutout <your message>"
        return await self._buy_shoutout(username, channel, " ".join(args))

    async def _buy_shoutout(self, username: str, channel: str, value: str) -> str:
        """Purchase and deliver a shoutout."""
        cfg = self._config.vanity_shop.shoutout
        if not cfg.enabled:
            return "Shoutouts are not available."
        if not value:
            return "Usage: buy shoutout <your message>"
        if len(value) > cfg.max_length:
            return f"Message too long (max {cfg.max_length} characters)."

        # Cooldown check
        last_shoutout = self._shoutout_cooldowns.get((username.lower(), channel))
        if last_shoutout:
            elapsed = (datetime.now(timezone.utc) - last_shoutout).total_seconds()
            cooldown = cfg.cooldown_minutes * 60
            if elapsed < cooldown:
                remaining = int((cooldown - elapsed) / 60) + 1
                return f"⏳ Shoutout cooldown: {remaining} minute(s) remaining."

        assert self._spending is not None
        account = await self._db.get_or_create_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account)
        final_cost, _discount = self._spending.apply_discount(cfg.cost, rank_tier)

        validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
        if validation:
            return validation.message

        new_balance = await self._db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id="spend.vanity.shoutout",
            reason="Vanity: Shoutout",
        )
        if new_balance is None:
            return "Insufficient funds."

        # Deliver to public chat
        await self._announce_chat(channel, f"📢 {username}: {value}")

        # Record cooldown
        self._shoutout_cooldowns[(username.lower(), channel)] = datetime.now(timezone.utc)

        return f"📢 Shoutout delivered! Charged: {final_cost:,} Z · Balance: {new_balance:,} Z"

    async def _buy_rename_currency(self, username: str, channel: str, value: str) -> str:
        """Rename personal currency display name."""
        cfg = self._config.vanity_shop.rename_currency_personal
        if not cfg.enabled:
            return "Personal currency rename is not available."
        if not value:
            return "Usage: buy rename <your currency name>"
        if len(value) > 30:
            return "Currency name too long (max 30 characters)."
        if not all(c.isalnum() or c in " -_'" for c in value):
            return "Currency name can only contain letters, numbers, spaces, hyphens, underscores, and apostrophes."

        return await self._complete_vanity_purchase(
            username, channel, cfg.cost, "personal_currency_name", value,
            "spend.vanity.rename_currency",
            f"✅ Your currency is now called \"{value}\"!",
        )

    async def _complete_vanity_purchase(
        self,
        username: str,
        channel: str,
        base_cost: int,
        item_type: str,
        value: str,
        trigger_id: str,
        success_message: str,
    ) -> str:
        """Shared debit+store logic for simple vanity purchases."""
        assert self._spending is not None
        account = await self._db.get_or_create_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account)
        final_cost, _discount = self._spending.apply_discount(base_cost, rank_tier)

        validation = await self._spending.validate_spend(username, channel, final_cost, "vanity")
        if validation:
            return validation.message

        new_balance = await self._db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id=trigger_id,
            reason=f"Vanity: {item_type}",
        )
        if new_balance is None:
            return "Insufficient funds."

        await self._db.set_vanity_item(username, channel, item_type, value)

        return f"{success_message}\nCharged: {final_cost:,} {self._symbol} · Balance: {new_balance:,} {self._symbol}"

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: Fortune
    # ══════════════════════════════════════════════════════════

    FORTUNES = [
        "🔮 The stars say you'll find a rare emote today.",
        "🎱 Signs point to a jackpot in your future.",
        "🌙 Tonight's movie will change your life. Or at least your mood.",
        "🃏 A mysterious stranger will tip you. Or not.",
        "⭐ Your Z-Coins are multiplying... in your dreams.",
        "🎬 Your next queue pick will be legendary.",
        "🌊 A rain of fortune approaches. Stay connected.",
        "🎭 Two paths diverge. Both lead to the slot machine.",
        "🔥 Your chat energy today: unstoppable.",
        "🌈 Something beautiful awaits in the playlist.",
        "🎲 Fortune favours the bold (and the broke).",
        "👁️ You will binge something unexpected today.",
        "🍀 Today's lucky number: however much Z you currently have.",
        "💫 The universe is buffering your destiny. Please hold.",
        "🎪 A wild shoutout appears in your future.",
    ]

    async def _cmd_fortune(self, username: str, channel: str, args: list[str]) -> str:
        """Receive a random daily fortune."""
        cfg = self._config.vanity_shop.daily_fortune
        if not cfg.enabled:
            return "Fortunes are not available."

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fortune_key = f"fortune:{username.lower()}:{channel}:{today}"
        if fortune_key in self._daily_fortune_used:
            return "🔮 You've already received your fortune today. Come back tomorrow!"

        if not self._spending:
            return "Shop is not available."

        account = await self._db.get_or_create_account(username, channel)
        rank_tier = self._spending.get_rank_tier_index(account)
        final_cost, _discount = self._spending.apply_discount(cfg.cost, rank_tier)

        validation = await self._spending.validate_spend(username, channel, final_cost, "fortune")
        if validation:
            return validation.message

        new_balance = await self._db.debit(
            username, channel, final_cost,
            tx_type="spend", trigger_id="spend.vanity.fortune",
            reason="Daily fortune",
        )
        if new_balance is None:
            return "Insufficient funds."

        # Deterministic fortune per user+date
        seed = int(hashlib.md5(f"{username}{today}".encode()).hexdigest()[:8], 16)
        fortune = self.FORTUNES[seed % len(self.FORTUNES)]

        self._daily_fortune_used.add(fortune_key)
        return fortune

    # ══════════════════════════════════════════════════════════
    #  Sprint 5: History
    # ══════════════════════════════════════════════════════════

    async def _cmd_history(self, username: str, channel: str, args: list[str]) -> str:
        """Show recent transactions."""
        limit = 10
        if args:
            try:
                limit = min(25, max(1, int(args[0])))
            except ValueError:
                pass

        transactions = await self._db.get_recent_transactions(username, channel, limit)
        if not transactions:
            return "No transaction history yet."

        symbol = self._symbol
        lines = [
            f"📜 Last {len(transactions)} transactions:",
            "━" * 15,
        ]

        for tx in transactions:
            amount = tx["amount"]
            sign = "+" if amount > 0 else ""
            reason = tx.get("reason") or tx.get("trigger_id") or ""
            ts = tx["created_at"]
            if isinstance(ts, str):
                ts = ts[:16].replace("T", " ")
            lines.append(f"{sign}{amount:,} {symbol}  {reason}")
            lines.append(f"  {ts}")

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds as human-readable duration."""
        if seconds < 60:
            return f"{seconds}s"
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        return f"{minutes}m {secs:02d}s"

    @staticmethod
    def _iter_trigger_configs(section: Any) -> list[tuple[str, Any]]:
        """Iterate over trigger config attributes in a Pydantic section."""
        result: list[tuple[str, Any]] = []
        for field_name in section.model_fields:
            result.append((field_name, getattr(section, field_name)))
        return result

    @staticmethod
    def _get_trigger_description(trigger_name: str) -> str:
        descriptions = {
            "long_message": "Long messages (30+ chars)",
            "first_message_of_day": "First message of the day",
            "conversation_starter": "Break the silence",
            "laugh_received": "Make someone laugh",
            "kudos_received": "Receive kudos (++)",
            "first_after_media_change": "First to comment on new media",
            "comment_during_media": "Chat during media",
            "like_current": "Like current media (PM: 'like')",
            "survived_full_media": "Watch full media",
            "greeted_newcomer": "Greet newcomers",
            "mentioned_by_other": "Get mentioned",
            "bot_interaction": "Interact with the bot",
        }
        return descriptions.get(trigger_name, trigger_name)

    def _get_trigger_reward_text(self, cfg: Any) -> str:
        if hasattr(cfg, "reward"):
            return f"{cfg.reward} {self._symbol}"
        if hasattr(cfg, "reward_per_laugher"):
            return f"{cfg.reward_per_laugher} {self._symbol}/laugh"
        if hasattr(cfg, "reward_per_message"):
            return f"{cfg.reward_per_message} {self._symbol}/msg"
        return f"? {self._symbol}"

    # ══════════════════════════════════════════════════════════
    #  Sprint 6: Rank Commands
    # ══════════════════════════════════════════════════════════

    async def _cmd_rank(self, username: str, channel: str, args: list[str]) -> str:
        """Show current rank, progress, and active perks."""
        if not self._rank_engine:
            return "Rank system is not available."

        account = await self._db.get_or_create_account(username, channel)
        lifetime = account.get("lifetime_earned", 0)

        tier_index, current_tier = self._rank_engine.get_rank_for_lifetime(lifetime)
        next_tier = self._rank_engine.get_next_tier(tier_index)

        lines = [
            f"⭐ Rank: {current_tier.name}",
            "━" * 15,
            "",
            f"💰 Lifetime: {lifetime:,} {self._symbol}",
        ]

        if next_tier:
            remaining = next_tier.min_lifetime_earned - lifetime
            progress = lifetime / next_tier.min_lifetime_earned * 100 if next_tier.min_lifetime_earned > 0 else 100
            bar = self._progress_bar(progress)
            lines.append("")
            lines.append(f"Next: {next_tier.name}")
            lines.append(f"  {remaining:,} {self._symbol} to go")
            lines.append(f"  {bar} {progress:.1f}%")
        else:
            lines.append("")
            lines.append("🏆 Maximum rank achieved!")

        if current_tier.perks:
            lines.append("")
            lines.append("Active perks:")
            for perk in current_tier.perks:
                lines.append(f"  ✓ {perk}")

        if self._spending:
            discount = self._spending.get_rank_discount(tier_index)
            if discount > 0:
                lines.append(f"  ✓ {int(discount * 100)}% spend discount")

        return "\n".join(lines)

    async def _cmd_profile(self, username: str, channel: str, args: list[str]) -> str:
        """Full user profile view."""
        target = args[0].lstrip("@") if args else username

        account = await self._db.get_account(target, channel)
        if not account:
            return f"No account found for '{target}'."

        personal_name = await self._db.get_vanity_item(target, channel, "personal_currency_name")
        currency = personal_name or self._config.currency.name

        tier_index, tier = self._rank_engine.get_rank_for_lifetime(
            account.get("lifetime_earned", 0),
        ) if self._rank_engine else (0, None)
        rank_name = tier.name if tier else account.get("rank_name", "Extra")

        streak = await self._db.get_or_create_streak(target, channel)
        streak_days = streak.get("current_daily_streak", 0)

        lines = [
            f"👤 {target}'s Profile",
            "━" * 15,
            "",
            f"⭐ Rank: {rank_name}",
            f"💰 Balance: {account['balance']:,} {self._symbol}",
            f"  ({currency})",
            f"📈 Lifetime: {account.get('lifetime_earned', 0):,} {self._symbol}",
            f"🔥 Streak: {streak_days} days",
        ]

        # Achievements
        achievements = await self._db.get_user_achievements(target, channel)
        if achievements:
            lines.append("")
            lines.append(f"🏆 Achievements: {len(achievements)}")

        # Vanity items
        vanity = await self._db.get_all_vanity_items(target, channel)
        if vanity:
            vanity_list = ", ".join(vanity.keys())
            lines.append(f"✨ Vanity: {vanity_list}")

        # Gambling stats
        gambling_stats = await self._db.get_gambling_summary(target, channel)
        if gambling_stats and gambling_stats.get("total_games", 0) > 0:
            lines.append("")
            lines.append(
                f"🎰 {gambling_stats['total_games']} games, "
                f"net {gambling_stats['net_profit']:+,} {self._symbol}"
            )

        return "\n".join(lines)

    async def _cmd_achievements(self, username: str, channel: str, args: list[str]) -> str:
        """List earned achievements and progress toward next."""
        earned = await self._db.get_user_achievements(username, channel)
        earned_ids = {a["achievement_id"] for a in earned}

        lines = ["🏆 Achievements"]

        # Show earned achievements
        if earned:
            lines.append("━" * 15)
            lines.append("Earned:")
            for a in earned:
                ach_config = self._find_achievement_config(a["achievement_id"])
                desc = ach_config.description if ach_config else a["achievement_id"]
                ts = a["awarded_at"]
                if isinstance(ts, str):
                    ts = ts[:10]
                lines.append(f"  ✅ {desc} ({ts})")

        # Show progress toward unearned non-hidden achievements
        progress_lines: list[str] = []
        for ach in self._config.achievements:
            if ach.id in earned_ids:
                continue
            if ach.hidden:
                continue
            current = await self._get_condition_progress(username, channel, ach.condition)
            if current is not None and ach.condition.threshold and ach.condition.threshold > 0:
                pct = min(100, current / ach.condition.threshold * 100)
                bar = self._progress_bar(pct, width=10)
                progress_lines.append(
                    f"  {bar} {ach.description} ({current}/{ach.condition.threshold})"
                )

        if progress_lines:
            lines.append("")
            lines.append("In progress:")
            lines.extend(progress_lines)

        # Hint about hidden achievements
        hidden_count = sum(
            1 for a in self._config.achievements
            if a.hidden and a.id not in earned_ids
        )
        if hidden_count > 0:
            lines.append(f"\n🔒 {hidden_count} hidden achievement(s) remaining...")

        return "\n".join(lines)

    async def _cmd_top(self, username: str, channel: str, args: list[str]) -> str:
        """Show leaderboards."""
        subcmd = args[0].lower() if args else "earners"

        match subcmd:
            case "earners" | "today":
                return await self._top_earners_today(channel)
            case "rich" | "balance" | "balances":
                return await self._top_richest(channel)
            case "lifetime" | "all":
                return await self._top_lifetime(channel)
            case "ranks":
                return await self._rank_distribution(channel)
            case _:
                return (
                    "Usage: top <category>\n"
                    "Categories: earners, rich, lifetime, ranks"
                )

    # ── Top sub-commands ─────────────────────────────────────

    async def _top_earners_today(self, channel: str) -> str:
        earners = await self._db.get_top_earners_today(channel, limit=10)
        if not earners:
            return "No earnings recorded today."
        lines = ["🏆 Today's Top Earners", "━" * 15]
        for i, e in enumerate(earners, 1):
            medal = "🥇🥈🥉"[i - 1] if i <= 3 else f"{i}."
            lines.append(f"  {medal} {e['username']} — {e['earned_today']:,} {self._symbol}")
        return "\n".join(lines)

    async def _top_richest(self, channel: str) -> str:
        rich = await self._db.get_richest_users(channel, limit=10)
        if not rich:
            return "No accounts yet."
        lines = ["💰 Richest Users", "━" * 15]
        for i, r in enumerate(rich, 1):
            medal = "🥇🥈🥉"[i - 1] if i <= 3 else f"{i}."
            lines.append(
                f"  {medal} {r['username']} — {r['balance']:,} {self._symbol} ({r['rank_name']})"
            )
        return "\n".join(lines)

    async def _top_lifetime(self, channel: str) -> str:
        top = await self._db.get_highest_lifetime(channel, limit=10)
        if not top:
            return "No accounts yet."
        lines = ["📈 Highest Lifetime Earned", "━" * 15]
        for i, t in enumerate(top, 1):
            medal = "🥇🥈🥉"[i - 1] if i <= 3 else f"{i}."
            lines.append(
                f"  {medal} {t['username']} — {t['lifetime_earned']:,} {self._symbol} ({t['rank_name']})"
            )
        return "\n".join(lines)

    async def _rank_distribution(self, channel: str) -> str:
        dist = await self._db.get_rank_distribution(channel)
        if not dist:
            return "No accounts yet."
        lines = ["⭐ Rank Distribution", "━" * 15]
        for tier in self._config.ranks.tiers:
            count = dist.get(tier.name, 0)
            if count > 0:
                lines.append(f"  {tier.name}: {count}")
        return "\n".join(lines)

    # ── CyTube Level 2 Purchase ──────────────────────────────

    async def _buy_cytube2(self, username: str, channel: str, _value: str) -> str:
        """Purchase CyTube level 2 promotion."""
        cfg = self._config.cytube_promotion
        if not cfg.enabled or not cfg.purchasable:
            return "CyTube level 2 promotion is not available."

        account = await self._db.get_or_create_account(username, channel)
        current_rank = account.get("rank_name", "Extra")

        # Check minimum rank
        min_tier_index = self._get_tier_index_by_name(cfg.min_rank)
        current_tier_index = self._spending.get_rank_tier_index(account) if self._spending else 0
        if current_tier_index < min_tier_index:
            return (
                f"You need to be at least {cfg.min_rank} rank to purchase CyTube level 2. "
                f"You're currently {current_rank}."
            )

        # Apply rank discount
        if self._spending:
            final_cost, discount = self._spending.apply_discount(cfg.cost, current_tier_index)
        else:
            final_cost = cfg.cost

        validation = await self._spending.validate_spend(
            username, channel, final_cost, "cytube_promotion",
        ) if self._spending else None
        if validation:
            return validation.message

        new_balance = await self._db.debit(
            username, channel, final_cost,
            tx_type="spend",
            trigger_id="spend.cytube_promotion",
            reason="CyTube Level 2 promotion",
        )
        if new_balance is None:
            return "Insufficient funds."

        # Execute CyTube rank change via kryten-py
        result = await self._client.safe_set_channel_rank(channel, username, 2)

        if result.get("success"):
            new_balance = (await self._db.get_account(username, channel))["balance"]
            return (
                f"🎬 Congratulations! You're now CyTube Level 2!\n"
                f"Charged: {final_cost:,} {self._symbol} · Balance: {new_balance:,} {self._symbol}"
            )
        else:
            # Refund on failure
            await self._db.credit(
                username, channel, final_cost,
                tx_type="refund",
                trigger_id="refund.cytube_promotion_failed",
                reason="CyTube promotion failed — refund",
            )
            return (
                f"❌ CyTube rank change failed: {result.get('error', 'unknown error')}. "
                f"Your {final_cost:,} {self._symbol} have been refunded."
            )

    # ── Sprint 6 Helpers ─────────────────────────────────────

    def _progress_bar(self, percent: float, width: int = 20) -> str:
        """Generate a text-based progress bar."""
        filled = int(width * min(percent, 100) / 100)
        return "█" * filled + "░" * (width - filled)

    def _find_achievement_config(self, achievement_id: str):
        """Look up achievement config by ID."""
        for ach in self._config.achievements:
            if ach.id == achievement_id:
                return ach
        return None

    async def _get_condition_progress(self, username, channel, condition) -> int | None:
        """Get current progress value for a condition. Returns None if unknown type."""
        match condition.type:
            case "lifetime_messages":
                return await self._db.get_lifetime_messages(username, channel)
            case "daily_streak":
                streak = await self._db.get_or_create_streak(username, channel)
                return streak.get("current_daily_streak", 0)
            case "lifetime_earned":
                return await self._db.get_lifetime_earned(username, channel)
            case "unique_tip_recipients":
                return await self._db.get_unique_tip_recipients(username, channel)
            case "unique_tip_senders":
                return await self._db.get_unique_tip_senders(username, channel)
            case _:
                return None

    def _get_tier_index_by_name(self, rank_name: str) -> int:
        """Get tier index for a rank name. Returns 0 if not found."""
        for i, tier in enumerate(self._config.ranks.tiers):
            if tier.name == rank_name:
                return i
        return 0

    def _get_max_queues_for_user(self, account: dict) -> int:
        """Calculate max queues per day including rank perk bonuses."""
        base = self._config.spending.max_queues_per_day
        tier_index = self._spending.get_rank_tier_index(account) if self._spending else 0
        tiers = self._config.ranks.tiers
        if tier_index < len(tiers):
            for perk in tiers[tier_index].perks:
                if "queue" in perk.lower():
                    match = re.search(r"\+(\d+)", perk)
                    if match:
                        base += int(match.group(1))
        return base

    # ══════════════════════════════════════════════════════════
    #  Sprint 7 — Bounties
    # ══════════════════════════════════════════════════════════

    async def _cmd_bounty(self, username: str, channel: str, args: list[str]) -> str:
        """Create a user bounty.

        Usage: bounty <amount> "<description>"
        Example: bounty 500 "First person to find a VHS copy of Manos"
        """
        if self._bounty_manager is None:
            return "Bounties are currently disabled."

        if len(args) < 2:
            return 'Usage: bounty <amount> "<description>"'

        try:
            amount = int(args[0])
        except ValueError:
            return "Amount must be a number."

        # Reconstruct description from remaining args
        description = " ".join(args[1:]).strip('"').strip("'")
        if not description:
            return "Description is required."

        result = await self._bounty_manager.create_bounty(
            username, channel, amount, description,
        )

        if result["success"]:
            await self._announce_chat(
                channel,
                f'📌 New bounty by {username}: "{description}" ({amount:,} {self._symbol})',
            )

        return result["message"]

    async def _cmd_bounties(self, username: str, channel: str, args: list[str]) -> str:
        """List open bounties."""
        bounties = await self._db.get_open_bounties(channel, limit=15)

        if not bounties:
            return "No open bounties."

        lines = [
            "📌 Open Bounties:",
            "━" * 15,
        ]
        for b in bounties:
            age = self._format_age(b["created_at"])
            lines.append("")
            lines.append(f"  #{b['id']} — {b['amount']:,} {self._symbol}")
            lines.append(f"  {b['description'][:60]}")
            lines.append(f"  by {b['creator']}, {age} ago")

        lines.append("")
        lines.append(
            f"{len(bounties)} open. "
            f"Admin: claim_bounty <id> @winner"
        )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    #  Sprint 7 — Events & Multipliers
    # ══════════════════════════════════════════════════════════

    async def _cmd_events(self, username: str, channel: str, args: list[str]) -> str:
        """Show currently active multipliers and events."""
        if self._multiplier_engine is None:
            return "No active multipliers right now. Earning at 1× base rate."

        combined, active = self._multiplier_engine.get_combined_multiplier(channel)

        if not active:
            return "No active multipliers right now. Earning at 1× base rate."

        lines = ["⚡ Active Multipliers:"]
        for m in active:
            if m.hidden:
                continue  # Don't reveal hidden multipliers
            source_display = self._format_multiplier_source(m.source)
            lines.append(f"  {source_display}: {m.multiplier}×")

        lines.append(f"\n💫 Combined: {combined:.1f}× earning rate")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    #  Sprint 7 — Admin Commands
    # ══════════════════════════════════════════════════════════

    async def _cmd_event(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Manage ad-hoc events. Sub-commands: start, stop."""
        if self._multiplier_engine is None:
            return "Multiplier engine not available."

        if not args:
            return 'Usage: event start <multiplier> <minutes> "<name>" | event stop'

        subcmd = args[0].lower()
        remaining = args[1:]

        match subcmd:
            case "start":
                return await self._cmd_event_start(username, channel, remaining)
            case "stop":
                return await self._cmd_event_stop(username, channel, remaining)
            case _:
                return 'Usage: event start <multiplier> <minutes> "<name>" | event stop'

    async def _cmd_event_start(
        self, username: str, channel: str, args: list[str],
    ) -> str:
        """Admin: Start an ad-hoc multiplier event."""
        if len(args) < 3:
            return 'Usage: event start <multiplier> <minutes> "<name>"'

        try:
            multiplier = float(args[0])
            minutes = int(args[1])
            name = " ".join(args[2:]).strip('"').strip("'")
        except (ValueError, IndexError):
            return 'Usage: event start <multiplier> <minutes> "<name>"'

        if not (1.0 < multiplier <= 10.0):
            return "Multiplier must be between 1.0 and 10.0"
        if not (1 <= minutes <= 1440):
            return "Duration must be between 1 and 1440 minutes (24 hours)"
        if not name:
            return "Event name is required."

        self._multiplier_engine.start_adhoc_event(name, multiplier, minutes)

        await self._announce_chat(
            channel,
            f"🎉 **{name}** activated by {username}! "
            f"{multiplier}× earning for {minutes} minutes!",
        )

        return f"Event '{name}' started: {multiplier}× for {minutes} min."

    async def _cmd_event_stop(
        self, username: str, channel: str, args: list[str],
    ) -> str:
        """Admin: Stop the current ad-hoc event."""
        stopped = self._multiplier_engine.stop_adhoc_event()
        if stopped:
            await self._announce_chat(
                channel, "⏰ The current event has been stopped.",
            )
            return "Ad-hoc event stopped."
        return "No ad-hoc event is currently active."

    async def _cmd_claim_bounty(
        self, username: str, channel: str, args: list[str],
    ) -> str:
        """Admin: Award an open bounty to a user.

        Usage: claim_bounty <id> @winner
        """
        if self._bounty_manager is None:
            return "Bounties are currently disabled."

        if len(args) < 2:
            return "Usage: claim_bounty <id> @winner"

        try:
            bounty_id = int(args[0])
        except ValueError:
            return "Bounty ID must be a number."

        winner = args[1].lstrip("@")
        if not winner:
            return "Winner username is required."

        return await self._bounty_manager.claim_bounty(
            bounty_id, channel, winner, username,
        )

    # ── Sprint 7 Helpers ─────────────────────────────────────

    def _format_age(self, timestamp_str: str) -> str:
        """Format a timestamp as a human-readable age string."""
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - ts
            hours = int(delta.total_seconds() / 3600)
            if hours < 1:
                return f"{int(delta.total_seconds() / 60)} min"
            if hours < 24:
                return f"{hours}h"
            return f"{hours // 24}d"
        except Exception:
            return "?"

    def _format_multiplier_source(self, source: str) -> str:
        """Format a multiplier source for display."""
        if source == "off_peak":
            return "📅 Off-Peak Bonus"
        if source == "population":
            return "👥 Crowd Bonus"
        if source.startswith("holiday:"):
            return f"🎄 {source.split(':', 1)[1]}"
        if source.startswith("scheduled:"):
            return f"🎉 {source.split(':', 1)[1]}"
        if source.startswith("adhoc:"):
            return f"⚡ {source.split(':', 1)[1]}"
        return source

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Admin Commands — Economy Control
    # ══════════════════════════════════════════════════════════

    async def _cmd_grant(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Credit Z to a user."""
        if len(args) < 2:
            return "Usage: grant @user <amount> [reason]"
        target = args[0].lstrip("@")
        try:
            amount = int(args[1])
        except ValueError:
            return "Amount must be a number."
        if amount <= 0:
            return "Amount must be positive."
        reason = " ".join(args[2:]) if len(args) > 2 else f"Admin grant by {username}"

        await self._db.get_or_create_account(target, channel)
        await self._db.credit(
            target, channel, amount,
            tx_type="admin_grant",
            trigger_id="admin.grant",
            reason=reason,
        )
        balance = (await self._db.get_account(target, channel))["balance"]

        await self._send_pm(
            channel, target,
            f"💰 You received {amount:,} Z from an admin. Reason: {reason}",
        )
        return f"Granted {amount:,} Z to {target}. New balance: {balance:,} Z"

    async def _cmd_deduct(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Debit Z from a user."""
        if len(args) < 2:
            return "Usage: deduct @user <amount> [reason]"
        target = args[0].lstrip("@")
        try:
            amount = int(args[1])
        except ValueError:
            return "Amount must be a number."
        if amount <= 0:
            return "Amount must be positive."
        reason = " ".join(args[2:]) if len(args) > 2 else f"Admin deduction by {username}"

        success = await self._db.debit(
            target, channel, amount,
            tx_type="admin_deduct",
            trigger_id="admin.deduct",
            reason=reason,
        )
        if success is None:
            return f"Failed: {target} has insufficient balance."

        balance = (await self._db.get_account(target, channel))["balance"]
        await self._send_pm(
            channel, target,
            f"💸 {amount:,} Z deducted by an admin. Reason: {reason}",
        )
        return f"Deducted {amount:,} Z from {target}. New balance: {balance:,} Z"

    async def _cmd_rain(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Distribute Z equally among all present users."""
        if not args:
            return "Usage: rain <amount>"
        try:
            total = int(args[0])
        except ValueError:
            return "Amount must be a number."
        if total <= 0:
            return "Amount must be positive."

        present = self._presence_tracker.get_present_users(channel)
        if not present:
            return "No users present."

        per_user = max(1, total // len(present))
        actual_total = per_user * len(present)

        # Announce publicly FIRST — sending PMs to many users can
        # trigger CyTube rate-limiting that silently drops the chat msg.
        template = self._config.announcements.templates.rain
        await self._announce_chat(
            channel,
            template.format(
                count=len(present),
                currency=self._currency_name,
                amount=f"{actual_total:,}",
                per_user=f"{per_user:,}",
                sender=username,
            ),
        )

        for user in present:
            await self._db.credit(
                user, channel, per_user,
                tx_type="admin_rain",
                trigger_id="admin.rain",
                reason=f"Admin rain by {username}",
            )

        return f"Rained {actual_total:,} Z ({per_user:,} each) to {len(present)} users."

    async def _cmd_set_balance(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Hard-set a user's balance."""
        if len(args) < 2:
            return "Usage: set_balance @user <amount>"
        target = args[0].lstrip("@")
        try:
            amount = int(args[1])
        except ValueError:
            return "Amount must be a number."
        if amount < 0:
            return "Balance cannot be negative."

        account = await self._db.get_or_create_account(target, channel)
        old_balance = account["balance"]
        diff = amount - old_balance
        await self._db.set_balance(target, channel, amount)
        await self._db.log_transaction(
            target, channel, amount=diff,
            tx_type="admin_set_balance",
            trigger_id="admin.set_balance",
            reason=f"Balance set to {amount:,} by {username} (was {old_balance:,})",
        )
        return f"Set {target}'s balance to {amount:,} Z (was {old_balance:,} Z)."

    async def _cmd_set_rank(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Override a user's economy rank."""
        if len(args) < 2:
            return "Usage: set_rank @user <rank_name>"
        target = args[0].lstrip("@")
        rank_name = " ".join(args[1:])

        valid_names = [t.name for t in self._config.ranks.tiers]
        if rank_name not in valid_names:
            return f"Unknown rank. Valid: {', '.join(valid_names)}"

        await self._db.get_or_create_account(target, channel)
        await self._db.update_account_rank(target, channel, rank_name)
        await self._send_pm(
            channel, target,
            f"⭐ Your rank has been set to **{rank_name}** by an admin.",
        )
        return f"Set {target}'s rank to {rank_name}."

    async def _cmd_announce(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Post a message in public chat via the bot."""
        if not args:
            return "Usage: announce <message>"
        message = " ".join(args)
        await self._announce_chat(channel, message)
        return f"Announced: {message}"

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Admin Commands — Inspection
    # ══════════════════════════════════════════════════════════

    async def _cmd_econ_stats(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Economy overview."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        totals = await self._db.get_daily_totals(channel, today)
        accounts = await self._db.get_all_accounts_count(channel)
        circulation = await self._db.get_total_circulation(channel)
        active = await self._db.get_active_economy_users_today(channel, today)
        present = len(self._presence_tracker.get_present_users(channel))

        return (
            f"📊 Economy Overview:\n"
            f"{'━' * 15}\n"
            f"Accounts: {accounts:,}\n"
            f"Present: {present}\n"
            f"Active today: {active}\n"
            f"Circulation: {circulation:,} Z\n"
            f"{'━' * 15}\n"
            f"Today:\n"
            f"  +{totals.get('z_earned', 0):,} earned\n"
            f"  −{totals.get('z_spent', 0):,} spent\n"
            f"  Gamble in: {totals.get('z_gambled_in', 0):,}\n"
            f"  Gamble out: {totals.get('z_gambled_out', 0):,}\n"
            f"  Net: {totals.get('z_gambled_out', 0) - totals.get('z_gambled_in', 0):+,} Z"
        )

    async def _cmd_econ_user(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Full user inspection."""
        if not args:
            return "Usage: econ:user <username>"
        target = args[0].lstrip("@")

        account = await self._db.get_account(target, channel)
        if not account:
            return f"No account for '{target}'."

        banned = await self._db.is_banned(target, channel)
        achievements = await self._db.get_achievement_count(target, channel)
        gambling = await self._db.get_gambling_summary(target, channel)

        lines = [
            f"👤 {target}",
            "━" * 15,
            "",
            f"Balance: {account['balance']:,} Z",
            f"Lifetime earned: {account.get('lifetime_earned', 0):,} Z",
            f"Lifetime spent: {account.get('lifetime_spent', 0):,} Z",
            "",
            f"Rank: {account.get('rank_name', 'Extra')}",
            f"Achievements: {achievements}",
            f"Banned: {'⛔ YES' if banned else 'No'}",
            "",
            f"Created: {account.get('first_seen', 'unknown')}",
            f"Last seen: {account.get('last_seen', 'unknown')}",
        ]

        if gambling and gambling.get("total_games", 0) > 0:
            lines.append(f"Gambling: {gambling['total_games']} games, net {gambling['net_profit']:+,} Z")

        return "\n".join(lines)

    async def _cmd_econ_health(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Inflation indicators and economy health."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        circulation = await self._db.get_total_circulation(channel)
        median = await self._db.get_median_balance(channel)
        totals = await self._db.get_daily_totals(channel, today)
        accounts = await self._db.get_all_accounts_count(channel)
        present = len(self._presence_tracker.get_present_users(channel))

        earned = totals.get("z_earned", 0)
        spent = totals.get("z_spent", 0)
        gamble_net = totals.get("z_gambled_out", 0) - totals.get("z_gambled_in", 0)
        net_flow = earned - spent + gamble_net

        participation = (accounts / present * 100) if present > 0 else 0

        latest = await self._db.get_latest_snapshot(channel)
        prev_circ = latest.get("total_z_circulation", circulation) if latest else circulation
        circ_change = circulation - prev_circ

        return (
            f"🏥 Economy Health:\n"
            f"{'━' * 15}\n"
            f"Circ: {circulation:,} Z\n"
            f"  ({circ_change:+,} since snap)\n"
            f"Median: {median:,} Z\n"
            f"Participation: {participation:.1f}%\n"
            f"  ({accounts}/{present})\n"
            f"{'━' * 15}\n"
            f"Net Flow Today:\n"
            f"  +{earned:,} earned\n"
            f"  −{spent:,} spent\n"
            f"  ±{gamble_net:+,} gamble\n"
            f"  = {net_flow:+,} Z"
        )

    async def _cmd_econ_triggers(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Trigger hit rates — identify hot and dead triggers."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        analytics = await self._db.get_trigger_analytics(channel, today)

        if not analytics:
            return "No trigger data for today."

        sorted_triggers = sorted(analytics, key=lambda t: t["hit_count"], reverse=True)

        lines = ["📊 Triggers (Today):"]
        lines.append("━" * 15)

        for t in sorted_triggers:
            tid = t['trigger_id']
            # Shorten common prefixes
            for pfx in ('presence.', 'chat.', 'content.', 'social.'):
                if tid.startswith(pfx):
                    tid = tid[len(pfx):]
                    break
            lines.append(
                f"{tid}\n"
                f"  {t['hit_count']} hits · "
                f"{t['unique_users']} users · "
                f"{t['total_z_awarded']:,} Z"
            )

        all_configured = self._get_all_trigger_ids()
        active_ids = {t["trigger_id"] for t in analytics}
        dead = all_configured - active_ids
        if dead:
            lines.append(f"\n⚠️ Dead triggers (0 hits today): {', '.join(sorted(dead))}")

        return "\n".join(lines)

    def _get_all_trigger_ids(self) -> set[str]:
        """Collect all configured trigger IDs for dead-trigger detection."""
        ids = set()
        ids.add("presence.base")
        nw = self._config.presence.night_watch
        if nw.enabled:
            ids.add("presence.night_watch")
        for name in ("long_message", "laugh_received", "kudos_received",
                     "first_message_of_day", "conversation_starter", "first_after_media_change"):
            if getattr(self._config.chat_triggers, name, None):
                ids.add(f"chat.{name}")
        for name in ("comment_during_media", "like_current", "survived_full_media",
                     "present_at_event_start"):
            if getattr(self._config.content_triggers, name, None):
                ids.add(f"content.{name}")
        for name in ("greeted_newcomer", "mentioned_by_other", "bot_interaction"):
            if getattr(self._config.social_triggers, name, None):
                ids.add(f"social.{name}")
        return ids

    async def _cmd_econ_gambling(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Gambling statistics."""
        stats = await self._db.get_gambling_summary_global(channel)

        if not stats or stats.get("total_games", 0) == 0:
            return "No gambling activity recorded."

        total_in = stats.get("total_in", 0)
        total_out = stats.get("total_out", 0)
        actual_edge = ((total_in - total_out) / total_in * 100) if total_in > 0 else 0

        configured_ev = 0
        for p in self._config.gambling.spin.payouts:
            configured_ev += p.multiplier * p.probability
        configured_edge = (1 - configured_ev) * 100

        return (
            f"🎰 Gambling Report:\n"
            f"{'━' * 15}\n"
            f"Wagered: {total_in:,} Z\n"
            f"Paid out: {total_out:,} Z\n"
            f"House: {total_in - total_out:,} Z\n"
            f"{'━' * 15}\n"
            f"Edge: {actual_edge:.1f}%\n"
            f"Cfg edge: {configured_edge:.1f}%\n"
            f"Gamblers: {stats.get('active_gamblers', 0)}\n"
            f"Games: {stats.get('total_games', 0):,}"
        )

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Admin Commands — Content Approval
    # ══════════════════════════════════════════════════════════

    async def _cmd_approve_gif(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Approve a pending channel GIF purchase."""
        if not args:
            return "Usage: approve_gif @user"
        target = args[0].lstrip("@")

        pending = await self._db.get_pending_approval(target, channel, "channel_gif")
        if not pending:
            return f"No pending GIF approval for {target}."

        await self._db.resolve_approval(pending["id"], username, True)
        await self._send_pm(
            channel, target,
            f"✅ Your channel GIF has been approved by {username}!",
        )
        return f"Approved GIF for {target}."

    async def _cmd_reject_gif(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Reject a pending channel GIF purchase and refund."""
        if not args:
            return "Usage: reject_gif @user"
        target = args[0].lstrip("@")

        pending = await self._db.get_pending_approval(target, channel, "channel_gif")
        if not pending:
            return f"No pending GIF approval for {target}."

        await self._db.resolve_approval(pending["id"], username, False)
        await self._db.credit(
            target, channel, pending["cost"],
            tx_type="refund",
            trigger_id="refund.gif_rejected",
            reason=f"Channel GIF rejected by {username}",
        )
        await self._send_pm(
            channel, target,
            f"❌ Your channel GIF was rejected by {username}. "
            f"Your {pending['cost']:,} Z have been refunded.",
        )
        return f"Rejected GIF for {target}. {pending['cost']:,} Z refunded."

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Admin Commands — User Management
    # ══════════════════════════════════════════════════════════

    async def _cmd_ban(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Ban a user from the economy."""
        if not args:
            return "Usage: ban @user [reason]"
        target = args[0].lstrip("@")
        reason = " ".join(args[1:]) if len(args) > 1 else ""

        if await self._db.is_banned(target, channel):
            return f"{target} is already banned."

        await self._db.ban_user(target, channel, username, reason)
        msg = "⛔ Your economy access has been suspended."
        if reason:
            msg += f" Reason: {reason}"
        await self._send_pm(channel, target, msg)

        result = f"Banned {target} from the economy."
        if reason:
            result += f" Reason: {reason}"
        return result

    async def _cmd_unban(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Restore a user's economy access."""
        if not args:
            return "Usage: unban @user"
        target = args[0].lstrip("@")

        if not await self._db.is_banned(target, channel):
            return f"{target} is not banned."

        await self._db.unban_user(target, channel)
        await self._send_pm(channel, target, "✅ Your economy access has been restored.")
        return f"Unbanned {target}."

    # ══════════════════════════════════════════════════════════
    #  Sprint 8: Config Hot-Reload
    # ══════════════════════════════════════════════════════════

    async def _cmd_reload(self, username: str, channel: str, args: list[str]) -> str:
        """Admin: Hot-reload config.yaml without restart."""
        try:
            new_config = self._load_and_validate_config()
            self._apply_config(new_config)
            self._logger.info("Config reloaded by %s", username)
            return "✅ Config reloaded successfully."
        except Exception as e:
            self._logger.error("Config reload failed: %s", e)
            return f"❌ Config reload failed: {e}"

    def _load_and_validate_config(self):
        """Re-read config.yaml and validate via Pydantic."""
        import yaml
        from .config import EconomyConfig as ConfigModel
        config_path = getattr(self, "_config_path", None)
        if not config_path:
            raise RuntimeError("No config_path set for hot-reload.")
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        return ConfigModel(**raw)

    def _apply_config(self, new_config) -> None:
        """Apply a validated config to all components."""
        old_config = self._config
        self._config = new_config
        self._symbol = new_config.currency.symbol
        self._currency_name = new_config.currency.name
        self._ignored_users = {u.lower() for u in new_config.ignored_users}

        # Update each component
        if self._presence_tracker:
            self._presence_tracker.update_config(new_config)
        if self._earning_engine:
            self._earning_engine.update_config(new_config)
        if self._spending:
            self._spending.update_config(new_config)
        if self._gambling_engine:
            self._gambling_engine.update_config(new_config)
        if self._achievement_engine:
            self._achievement_engine.update_config(new_config)
        if self._rank_engine:
            self._rank_engine.update_config(new_config)
        if self._multiplier_engine:
            self._multiplier_engine.update_config(new_config)
        if hasattr(self, "_competition_engine") and self._competition_engine:
            self._competition_engine.update_config(new_config)
        if self._bounty_manager:
            self._bounty_manager.update_config(new_config)

        if new_config.presence.base_rate_per_minute != old_config.presence.base_rate_per_minute:
            self._logger.info(
                "Presence rate changed: %s → %s",
                old_config.presence.base_rate_per_minute,
                new_config.presence.base_rate_per_minute,
            )

    # ══════════════════════════════════════════════════════════
    #  PM Sending
    # ══════════════════════════════════════════════════════════

    async def _resolve_cytube_rank(self, event: Any, channel: str, username: str) -> int:
        """Resolve the user's CyTube rank for admin gating.

        CyTube PM events may not carry the sender's rank reliably (often 0).
        If the event rank is missing or 0, fall back to querying the robot's
        live channel state via ``client.get_user()``.
        """
        rank = getattr(event, "rank", 0) or 0
        if rank > 0:
            return rank

        # Fallback: ask kryten-robot for the user's rank via the channel userlist
        if self._client is not None:
            try:
                user_info = await self._client.get_user(channel, username)
                if user_info:
                    return user_info.get("rank", 0) if isinstance(user_info, dict) else getattr(user_info, "rank", 0)
            except Exception:
                self._logger.debug(
                    "Could not resolve CyTube rank for %s via get_user, "
                    "falling back to event rank (%d)",
                    username,
                    rank,
                )
        return rank

    # ──────────────────────────────────────────────────────────
    #  PM delivery with auto-split
    # ──────────────────────────────────────────────────────────

    _PM_MAX_LEN: int = 240  # CyTube single-message character limit
    _PM_SEND_INTERVAL: float = 3.0  # seconds between outbound PMs

    # ── PM queue lifecycle ────────────────────────────────────

    def start_pm_worker(self) -> None:
        """Start the background PM delivery worker."""
        if self._pm_worker_task is None or self._pm_worker_task.done():
            self._pm_worker_task = asyncio.create_task(self._pm_worker())

    async def stop_pm_worker(self) -> None:
        """Drain remaining PMs and stop the worker."""
        if self._pm_worker_task and not self._pm_worker_task.done():
            self._pm_worker_task.cancel()
            try:
                await self._pm_worker_task
            except asyncio.CancelledError:
                pass
            self._pm_worker_task = None

    async def _pm_worker(self) -> None:
        """Background loop: send queued PMs with a pause between each."""
        try:
            while True:
                channel, username, chunk = await self._pm_queue.get()
                try:
                    if self._client is not None:
                        await self._client.send_pm(channel, username, chunk)
                except Exception:
                    self._logger.exception("PM worker failed to send to %s", username)
                finally:
                    self._pm_queue.task_done()
                await asyncio.sleep(self._PM_SEND_INTERVAL)
        except asyncio.CancelledError:
            # Drain remaining items on shutdown
            while not self._pm_queue.empty():
                channel, username, chunk = self._pm_queue.get_nowait()
                try:
                    if self._client is not None:
                        await self._client.send_pm(channel, username, chunk)
                except Exception:
                    self._logger.exception("PM worker (drain) failed for %s", username)
                self._pm_queue.task_done()

    def _split_message(self, message: str) -> list[str]:
        """Split a long PM into chunks that fit within CyTube's limit.

        Splits at ``\\n`` boundaries, keeping each chunk ≤ _PM_MAX_LEN chars.
        A single line longer than the limit is forced through unsplit.
        """
        limit = self._PM_MAX_LEN
        if len(message) <= limit:
            return [message]

        lines = message.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            # +1 accounts for the '\n' join character
            added_len = len(line) + (1 if current else 0)
            if current and current_len + added_len > limit:
                chunks.append("\n".join(current))
                current = [line]
                current_len = len(line)
            else:
                current.append(line)
                current_len += added_len

        if current:
            chunks.append("\n".join(current))

        return chunks

    async def _send_pm(self, channel: str, username: str, message: str) -> None:
        """Enqueue a PM for throttled delivery, auto-splitting long messages.

        If the PM worker is not running (e.g. in tests), sends directly.
        """
        if self._client is None:
            return
        chunks = self._split_message(message)
        # If worker is active, enqueue for throttled delivery
        if self._pm_worker_task and not self._pm_worker_task.done():
            for chunk in chunks:
                await self._pm_queue.put((channel, username, chunk))
        else:
            # Direct send (no throttle) — used in tests or before worker starts
            for chunk in chunks:
                try:
                    await self._client.send_pm(channel, username, chunk)
                except Exception:
                    self._logger.exception("Failed to send PM to %s", username)

    async def _announce_chat(self, channel: str, message: str) -> None:
        """Post a message in public chat via kryten-py."""
        if self._client is None:
            self._logger.warning("_announce_chat: client is None, skipping")
            return
        try:
            self._logger.debug("_announce_chat → channel=%s msg=%s", channel, message[:80])
            cid = await self._client.send_chat(channel, message)
            self._logger.debug("_announce_chat sent OK, cid=%s", cid)
        except Exception:
            self._logger.exception("Failed to send chat to %s", channel)
