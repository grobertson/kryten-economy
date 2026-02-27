"""Service orchestrator — EconomyApp.

Follows the canonical kryten-py microservice pattern:
config → DB init → register handlers → connect → subscribe → metrics → run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from kryten import KrytenClient

from . import __version__
from .achievement_engine import AchievementEngine
from .bounty_manager import BountyManager
from .channel_state import ChannelStateTracker
from .command_handler import CommandHandler
from .competition_engine import CompetitionEngine
from .config import EconomyConfig, load_config
from .database import EconomyDatabase
from .earning_engine import EarningEngine
from .event_announcer import EventAnnouncer
from .gambling_engine import GamblingEngine
from .greeting_handler import GreetingHandler
from .media_client import MediaCMSClient
from .metrics_server import EconomyMetricsServer
from .multiplier_engine import MultiplierEngine
from .pm_handler import PmHandler
from .presence_tracker import PresenceTracker
from .rank_engine import RankEngine
from .admin_scheduler import AdminScheduler
from .scheduled_event_manager import ScheduledEventManager
from .scheduler import Scheduler
from .spending_engine import SpendingEngine


class EconomyApp:
    """Top-level application orchestrator."""

    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self.logger = logging.getLogger("economy")

        # Components (initialized in start())
        self.config: EconomyConfig | None = None
        self.client: KrytenClient | None = None
        self.db: EconomyDatabase | None = None
        self.channel_state: ChannelStateTracker | None = None
        self.earning_engine: EarningEngine | None = None
        self.gambling_engine: GamblingEngine | None = None
        self.media_client: MediaCMSClient | None = None
        self.spending_engine: SpendingEngine | None = None
        self.achievement_engine: AchievementEngine | None = None
        self.rank_engine: RankEngine | None = None
        self.competition_engine: CompetitionEngine | None = None
        self.multiplier_engine: MultiplierEngine | None = None
        self.bounty_manager: BountyManager | None = None
        self.scheduled_event_manager: ScheduledEventManager | None = None
        self.presence_tracker: PresenceTracker | None = None
        self.pm_handler: PmHandler | None = None
        self.command_handler: CommandHandler | None = None
        self.metrics_server: EconomyMetricsServer | None = None
        self.scheduler: Scheduler | None = None
        self.admin_scheduler: AdminScheduler | None = None
        self.event_announcer: EventAnnouncer | None = None
        self.greeting_handler: GreetingHandler | None = None

        # State
        self._running = False
        self._start_time: float | None = None
        self._counter_persistence_task: asyncio.Task | None = None

        # Counters (for metrics)
        self.events_processed: int = 0
        self.commands_processed: int = 0
        self.z_spent_total: int = 0
        self.tips_total: int = 0
        self.queues_total: int = 0
        self.vanity_purchases_total: int = 0
        self.achievements_awarded_total: int = 0
        self.rank_promotions_total: int = 0
        self.competition_awards_total: int = 0
        self.bounties_created_total: int = 0
        self.bounties_claimed_total: int = 0

    @property
    def z_earned_total(self) -> int:
        """Aggregate Z earned counter from presence tracker."""
        if self.presence_tracker:
            return self.presence_tracker.metrics_z_earned
        return 0

    @property
    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    # ------------------------------------------------------------------
    # Metrics counter persistence (NATS KV)
    # ------------------------------------------------------------------
    _COUNTERS_KV_BUCKET = "kryten_economy_state"
    _COUNTERS_KV_KEY = "counters"
    _COUNTERS_SAVE_INTERVAL = 300  # seconds (5 minutes)
    _COUNTER_NAMES = [
        "events_processed",
        "commands_processed",
        "z_spent_total",
        "tips_total",
        "queues_total",
        "vanity_purchases_total",
        "achievements_awarded_total",
        "rank_promotions_total",
        "competition_awards_total",
        "bounties_created_total",
        "bounties_claimed_total",
    ]

    async def _save_counters(self) -> None:
        """Persist volatile metrics counters to NATS KV."""
        data = {name: getattr(self, name) for name in self._COUNTER_NAMES}
        data["z_earned_total"] = (
            self.presence_tracker.metrics_z_earned if self.presence_tracker else 0
        )
        try:
            await self.client.kv_put(
                self._COUNTERS_KV_BUCKET,
                self._COUNTERS_KV_KEY,
                data,
                as_json=True,
            )
            self.logger.debug("Persisted metrics counters to KV")
        except Exception:
            self.logger.exception("Failed to persist metrics counters")

    async def _restore_counters(self) -> None:
        """Restore volatile metrics counters from NATS KV on startup."""
        try:
            data = await self.client.kv_get(
                self._COUNTERS_KV_BUCKET,
                self._COUNTERS_KV_KEY,
                default={},
                parse_json=True,
            )
            if not data:
                self.logger.info("No persisted counters found — starting fresh")
                return
            for name in self._COUNTER_NAMES:
                if name in data:
                    setattr(self, name, int(data[name]))
            if self.presence_tracker and "z_earned_total" in data:
                self.presence_tracker.metrics_z_earned = int(data["z_earned_total"])
            self.logger.info("Restored metrics counters from KV: %s", data)
        except Exception:
            self.logger.exception("Failed to restore metrics counters from KV")

    async def _counter_persistence_loop(self) -> None:
        """Periodically save counters to KV."""
        try:
            while True:
                await asyncio.sleep(self._COUNTERS_SAVE_INTERVAL)
                await self._save_counters()
        except asyncio.CancelledError:
            pass

    async def start(self) -> None:
        """Start the economy service — canonical kryten-py sequence."""
        self.logger.info("Starting kryten-economy...")
        self._start_time = time.time()

        # 1. Load and validate config
        self.config = load_config(str(self.config_path))
        self.logger.info("Config loaded: %d channel(s)", len(self.config.channels))

        # 2. Initialize database
        self.db = EconomyDatabase(self.config.database.path, self.logger)
        await self.db.initialize()
        self.logger.info("Database initialized: %s", self.config.database.path)

        # 3. Initialize domain components
        self.channel_state = ChannelStateTracker(
            config=self.config,
            logger=self.logger,
        )
        self.presence_tracker = PresenceTracker(
            config=self.config,
            database=self.db,
            client=None,  # Set after client creation
            logger=self.logger,
            channel_state=self.channel_state,
        )
        self.earning_engine = EarningEngine(
            config=self.config,
            database=self.db,
            channel_state=self.channel_state,
            logger=self.logger,
            presence_tracker=self.presence_tracker,
        )
        self.gambling_engine = GamblingEngine(
            config=self.config,
            database=self.db,
            logger=self.logger,
        )
        self.media_client = MediaCMSClient(
            config=self.config.mediacms,
            logger=self.logger,
        )
        self.spending_engine = SpendingEngine(
            config=self.config,
            database=self.db,
            media_client=self.media_client,
            logger=self.logger,
        )
        self.achievement_engine = AchievementEngine(
            config=self.config,
            database=self.db,
            client=None,  # Set after client creation
            logger=self.logger,
        )
        self.rank_engine = RankEngine(
            config=self.config,
            database=self.db,
            client=None,  # Set after client creation
            logger=self.logger,
        )
        self.multiplier_engine = MultiplierEngine(
            config=self.config,
            presence_tracker=self.presence_tracker,
            logger=self.logger,
        )
        self.competition_engine = CompetitionEngine(
            config=self.config,
            database=self.db,
            client=None,  # Set after client creation
            logger=self.logger,
        )
        self.bounty_manager = BountyManager(
            config=self.config,
            database=self.db,
            client=None,  # Set after client creation
            logger=self.logger,
        )
        self.pm_handler = PmHandler(
            config=self.config,
            database=self.db,
            client=None,  # Set after client creation
            presence_tracker=self.presence_tracker,
            logger=self.logger,
            earning_engine=self.earning_engine,
            channel_state=self.channel_state,
            gambling_engine=self.gambling_engine,
            spending_engine=self.spending_engine,
            media_client=self.media_client,
            achievement_engine=self.achievement_engine,
            rank_engine=self.rank_engine,
            multiplier_engine=self.multiplier_engine,
            bounty_manager=self.bounty_manager,
        )

        # Sprint 9: EventAnnouncer + GreetingHandler (client set later)
        self.event_announcer = EventAnnouncer(
            config=self.config,
            client=None,
            logger=self.logger,
        )
        self.greeting_handler = GreetingHandler(
            config=self.config,
            database=self.db,
            presence_tracker=self.presence_tracker,
            announcer=self.event_announcer,
            logger=self.logger,
        )

        # Build ignored-user set for event handlers
        self._ignored_users: set[str] = {
            u.lower() for u in (self.config.ignored_users or [])
        }

        # 4. Create KrytenClient
        self.client = KrytenClient(self.config)

        # Wire up client references
        self.presence_tracker._client = self.client
        self.presence_tracker._rank_engine = self.rank_engine
        self.pm_handler._client = self.client
        self.pm_handler._config_path = str(self.config_path)
        self.pm_handler.start_pm_worker()
        self.achievement_engine._client = self.client
        self.rank_engine._client = self.client
        self.competition_engine._client = self.client
        self.bounty_manager._client = self.client
        self.event_announcer._client = self.client

        # 4b. Start MediaCMS HTTP client
        if self.config.mediacms.base_url:
            await self.media_client.start()
            self.logger.info("MediaCMS client started: %s", self.config.mediacms.base_url)

        # 5. Register event handlers BEFORE connect
        @self.client.on("adduser")
        async def handle_join(event):
            try:
                self.events_processed += 1
                rank = getattr(event, "rank", 0) or 0
                self.presence_tracker.update_user_rank(event.channel, event.username, rank)
                is_genuine = await self.presence_tracker.handle_user_join(event.username, event.channel)
                if is_genuine:
                    await self.greeting_handler.on_user_join(event.channel, event.username)
            except Exception:
                self.logger.exception("adduser handler error for %s", getattr(event, "username", "?"))

        @self.client.on("userleave")
        async def handle_leave(event):
            try:
                self.events_processed += 1
                await self.presence_tracker.handle_user_leave(event.username, event.channel)
            except Exception:
                self.logger.exception("userleave handler error for %s", getattr(event, "username", "?"))

        @self.client.on("pm")
        async def handle_pm(event):
            try:
                self.events_processed += 1
                await self.pm_handler.handle_pm(event)
            except Exception:
                self.logger.exception("pm handler error for %s", getattr(event, "username", "?"))

        @self.client.on("chatmsg")
        async def handle_chatmsg(event):
            try:
                self.events_processed += 1
                username = event.username
                channel = event.channel
                message = event.message
                timestamp = event.timestamp

                # Ignored user gate
                if username.lower() in self._ignored_users:
                    return

                # Bot's own messages — detect bot_interaction
                if username.lower() == self.config.bot.username.lower():
                    last_human = self.channel_state.get_last_non_self_message_user(
                        channel, username,
                    )
                    if last_human and self.config.social_triggers.bot_interaction.enabled:
                        await self.earning_engine.evaluate_bot_interaction(
                            last_human, channel, timestamp,
                        )
                    return

                # Main earning pipeline
                outcome = await self.earning_engine.evaluate_chat_message(
                    username, channel, message, timestamp,
                )
                if outcome.total_earned > 0:
                    self.logger.debug(
                        "Chat triggers for %s in %s: %d Z from %d triggers",
                        username, channel, outcome.total_earned, len(outcome.awarded_triggers),
                    )
            except Exception:
                self.logger.exception("chatmsg handler error for %s", getattr(event, "username", "?"))

        @self.client.on("changemedia")
        async def handle_changemedia(event):
            try:
                self.events_processed += 1
                channel = event.channel
                title = event.title
                media_id = event.media_id
                duration = event.duration
                timestamp = event.timestamp

                connected = self.presence_tracker.get_connected_users(channel)

                previous = self.channel_state.handle_media_change(
                    channel, title, media_id, float(duration), connected, timestamp,
                )

                if previous is not None:
                    rewarded = await self.earning_engine.evaluate_survived_full_media(
                        channel, previous, connected, timestamp,
                    )
                    if rewarded:
                        self.logger.info(
                            "survived_full_media: %d users rewarded for '%s' in %s",
                            len(rewarded), previous.title, channel,
                        )
            except Exception:
                self.logger.exception("changemedia handler error for %s", getattr(event, "channel", "?"))

        # 6. Connect to NATS
        await self.client.connect()
        self.logger.info("Connected to NATS")

        # 6b. Seed presence from KV store (users already in room)
        for ch_cfg in self.config.channels:
            try:
                bucket = f"kryten_{ch_cfg.channel}_userlist"
                users = await self.client.kv_get(
                    bucket, "users", default=[], parse_json=True,
                )
                if users:
                    seeded = await self.presence_tracker.seed_initial_users(
                        ch_cfg.channel, users,
                    )
                    self.logger.info(
                        "Seeded %d users from KV for %s",
                        seeded, ch_cfg.channel,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Could not seed userlist for %s: %s",
                    ch_cfg.channel, exc,
                )

        # 6c. Restore persisted metrics counters from KV
        await self.client.get_or_create_kv_store(
            self._COUNTERS_KV_BUCKET,
            description="kryten-economy volatile metrics counters",
        )
        await self._restore_counters()

        # 6d. Start periodic counter persistence (every 5 min)
        self._counter_persistence_task = asyncio.create_task(
            self._counter_persistence_loop(),
        )

        # 7. Subscribe to robot startup for re-initialization
        await self.client.subscribe(
            "kryten.lifecycle.robot.startup",
            self._handle_robot_startup,
        )

        # 8. Start metrics server
        metrics_port = self.config.metrics.port if self.config.metrics else 28286
        self.metrics_server = EconomyMetricsServer(self, port=metrics_port)
        await self.metrics_server.start()
        self.logger.info("Metrics server started on port %d", metrics_port)

        # 9. Start command handler
        self.command_handler = CommandHandler(self, self.client, self.logger)
        await self.command_handler.connect()
        self.logger.info("Command handler ready on kryten.economy.command")

        # 10. Start presence tracker tick
        await self.presence_tracker.start()

        # 11. Start scheduler (Sprint 2: rain, maintenance; Sprint 4: challenges, heists)
        self.scheduler = Scheduler(
            config=self.config,
            database=self.db,
            presence_tracker=self.presence_tracker,
            client=self.client,
            logger=self.logger,
            gambling_engine=self.gambling_engine,
        )
        await self.scheduler.start()

        # 11b. Start scheduled event manager (Sprint 7)
        channels = [ch.channel for ch in self.config.channels]
        self.scheduled_event_manager = ScheduledEventManager(
            config=self.config,
            multiplier_engine=self.multiplier_engine,
            presence_tracker=self.presence_tracker,
            database=self.db,
            client=self.client,
            logger=self.logger,
        )
        await self.scheduled_event_manager.start(channels)

        # 11c. Start admin scheduler (Sprint 8: snapshots, digests)
        self.admin_scheduler = AdminScheduler(
            config=self.config,
            database=self.db,
            client=self.client,
            presence_tracker=self.presence_tracker,
            rank_engine=self.rank_engine,
            logger=self.logger,
        )
        await self.admin_scheduler.start()

        # 11d. Start event announcer (Sprint 9: centralized chat announcements)
        await self.event_announcer.start()

        # 12. Mark running
        self._running = True
        self.logger.info("kryten-economy started successfully (v%s)", __version__)

        # 13. Block on client event loop
        await self.client.run()

    async def stop(self) -> None:
        """Gracefully shut down all components in reverse order."""
        if not self._running:
            return
        self.logger.info("Shutting down kryten-economy...")
        self._running = False

        # Cancel periodic counter persistence and do a final save
        if self._counter_persistence_task:
            self._counter_persistence_task.cancel()
            try:
                await self._counter_persistence_task
            except asyncio.CancelledError:
                pass
        try:
            await self._save_counters()
            self.logger.info("Metrics counters saved on shutdown")
        except Exception:
            self.logger.exception("Failed to save counters on shutdown")

        # Reverse order of startup
        if self.pm_handler:
            await self.pm_handler.stop_pm_worker()
        if self.event_announcer:
            await self.event_announcer.stop()
        if self.admin_scheduler:
            await self.admin_scheduler.stop()
        if self.scheduled_event_manager:
            await self.scheduled_event_manager.stop()
        if self.scheduler:
            await self.scheduler.stop()
        if self.presence_tracker:
            await self.presence_tracker.stop()
        if self.metrics_server:
            await self.metrics_server.stop()
        if self.media_client:
            await self.media_client.stop()
        if self.client:
            await self.client.stop()

        self.logger.info("kryten-economy stopped.")

    async def _handle_robot_startup(self, msg) -> None:
        """Handle kryten-robot restart — re-announce ourselves and await fresh adduser events."""
        self.logger.info("Robot startup detected — re-publishing our startup event")
        if self.client and self.client.lifecycle:
            try:
                await self.client.lifecycle.publish_startup()
                self.logger.info("Re-published economy startup event")
            except Exception:
                self.logger.exception("Failed to re-publish startup event")
