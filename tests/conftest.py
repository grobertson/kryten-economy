"""Shared test fixtures for kryten-economy."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from kryten_economy.channel_state import ChannelStateTracker, MediaInfo
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.earning_engine import EarningEngine
from kryten_economy.gambling_engine import GamblingEngine
from kryten_economy.media_client import MediaCMSClient
from kryten_economy.spending_engine import SpendingEngine

# Sprint 6
from kryten_economy.achievement_engine import AchievementEngine
from kryten_economy.rank_engine import RankEngine

# Sprint 7
from kryten_economy.bounty_manager import BountyManager
from kryten_economy.competition_engine import CompetitionEngine
from kryten_economy.multiplier_engine import MultiplierEngine

# Sprint 8
from kryten_economy.admin_scheduler import AdminScheduler
from kryten_economy.pm_handler import PmHandler, PmRateLimiter
from kryten_economy.presence_tracker import PresenceTracker

# Sprint 9
from kryten_economy.event_announcer import EventAnnouncer
from kryten_economy.greeting_handler import GreetingHandler


# ── Minimal config dict matching EconomyConfig schema ────────

def make_config_dict(**overrides) -> dict:
    """Build a valid config dict with sensible test defaults."""
    base = {
        "nats": {"servers": ["nats://localhost:4222"]},
        "channels": [{"domain": "cytu.be", "channel": "testchannel"}],
        "service": {"name": "economy"},
        "database": {"path": ":memory:"},
        "currency": {"name": "Z-Coin", "symbol": "Z", "plural": "Z-Coins"},
        "bot": {"username": "TestBot"},
        "ignored_users": ["IgnoredBot"],
        "onboarding": {
            "welcome_wallet": 100,
            "welcome_message": "Welcome! Here's {amount} {currency}.",
        },
        "presence": {
            "base_rate_per_minute": 1,
            "join_debounce_minutes": 5,
            "greeting_absence_minutes": 30,
            "hourly_milestones": {1: 10, 3: 30},
            "night_watch": {"enabled": False, "hours": [2, 3, 4, 5], "multiplier": 1.5},
        },
        "streaks": {
            "daily": {
                "enabled": True,
                "min_presence_minutes": 15,
                "rewards": {2: 10, 3: 20, 7: 100},
                "milestone_7_bonus": 200,
                "milestone_30_bonus": 2000,
            },
            "weekend_weekday_bridge": {
                "enabled": True,
                "bonus": 500,
            },
        },
        "rain": {
            "enabled": True,
            "interval_minutes": 45,
            "min_amount": 5,
            "max_amount": 25,
            "pm_notification": True,
            "message": "Rain! {amount} {currency}.",
        },
        "balance_maintenance": {
            "mode": "interest",
            "interest": {"daily_rate": 0.001, "max_daily_interest": 10, "min_balance_to_earn": 100},
            "decay": {"enabled": False, "daily_rate": 0.005, "exempt_below": 50000},
        },
        "retention": {
            "welcome_back": {"enabled": True, "days_absent": 7, "bonus": 100, "message": "Welcome back! {amount} {currency}"},
            "inactivity_nudge": {"enabled": False},
        },
        "chat_triggers": {
            "long_message": {"enabled": True, "min_chars": 30, "reward": 1, "max_per_hour": 30, "hidden": True},
            "laugh_received": {"enabled": True, "reward_per_laugher": 2, "max_laughers_per_joke": 10, "self_excluded": True, "hidden": True},
            "kudos_received": {"enabled": True, "reward": 3, "self_excluded": True, "hidden": True},
            "first_message_of_day": {"enabled": True, "reward": 5, "hidden": True},
            "conversation_starter": {"enabled": True, "min_silence_minutes": 10, "reward": 10, "hidden": True},
        },
        "content_triggers": {
            "first_after_media_change": {"enabled": True, "window_seconds": 30, "reward": 3, "hidden": True},
            "comment_during_media": {"enabled": True, "reward_per_message": 0.5, "max_per_item_base": 10, "scale_with_duration": True, "hidden": True},
            "like_current": {"enabled": True, "reward": 2, "hidden": True},
            "survived_full_media": {"enabled": True, "min_presence_percent": 80, "reward": 5, "hidden": True},
            "present_at_event_start": {"enabled": True, "default_reward": 100, "hidden": True},
        },
        "social_triggers": {
            "greeted_newcomer": {"enabled": True, "window_seconds": 60, "reward": 3, "bot_joins_excluded": True, "hidden": True},
            "mentioned_by_other": {"enabled": True, "reward": 1, "max_per_hour_same_user": 5, "hidden": True},
            "bot_interaction": {"enabled": True, "reward": 2, "max_per_day": 10, "hidden": True},
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def sample_config_dict() -> dict:
    """Return a config dict suitable for tests."""
    return make_config_dict()


@pytest.fixture
def sample_config(sample_config_dict: dict) -> EconomyConfig:
    """Return a parsed EconomyConfig."""
    return EconomyConfig(**sample_config_dict)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """Return a temporary SQLite database path."""
    return str(tmp_path / "test_economy.db")


@pytest_asyncio.fixture
async def database(tmp_db_path: str) -> AsyncGenerator[EconomyDatabase, None]:
    """Provide an initialized database with temp file."""
    import logging
    db = EconomyDatabase(tmp_db_path, logging.getLogger("test"))
    await db.initialize()
    yield db


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a mock KrytenClient with async methods."""
    client = MagicMock()
    client.send_pm = AsyncMock(return_value="corr-id-123")
    client.send_chat = AsyncMock(return_value="corr-id-456")
    client.connect = AsyncMock()
    client.run = AsyncMock()
    client.stop = AsyncMock()
    client.subscribe = AsyncMock()
    client.subscribe_request_reply = AsyncMock()
    client.add_media = AsyncMock()
    client.safe_set_channel_rank = AsyncMock(return_value={"success": True})
    return client


# ── Sprint 3 fixtures ───────────────────────────────────────

@pytest.fixture
def channel_state(sample_config: EconomyConfig) -> ChannelStateTracker:
    """ChannelStateTracker with test config."""
    return ChannelStateTracker(sample_config, logging.getLogger("test"))


@pytest_asyncio.fixture
async def earning_engine(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    channel_state: ChannelStateTracker,
) -> EarningEngine:
    """EarningEngine with test dependencies."""
    return EarningEngine(
        sample_config, database, channel_state, logging.getLogger("test"),
    )


@pytest.fixture
def sample_media_info() -> MediaInfo:
    """A MediaInfo for a 30-minute video."""
    return MediaInfo(
        title="Test Video",
        media_id="test123",
        duration_seconds=1800,
        started_at=datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        users_present_at_start={"alice", "bob"},
    )


# ── Sprint 4 fixtures ───────────────────────────────────────

@pytest_asyncio.fixture
async def gambling_engine(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
) -> GamblingEngine:
    """GamblingEngine with test dependencies."""
    return GamblingEngine(
        sample_config, database, logging.getLogger("test"),
    )


# ── Sprint 5 fixtures ───────────────────────────────────────

@pytest.fixture
def mock_media_client() -> MagicMock:
    """Mock MediaCMSClient with async methods."""
    client = MagicMock(spec=MediaCMSClient)
    client.search = AsyncMock(return_value=[])
    client.get_by_id = AsyncMock(return_value=None)
    client.get_duration = AsyncMock(return_value=None)
    client.start = AsyncMock()
    client.stop = AsyncMock()
    return client


@pytest_asyncio.fixture
async def spending_engine(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_media_client: MagicMock,
) -> SpendingEngine:
    """SpendingEngine with test dependencies."""
    return SpendingEngine(
        sample_config, database, mock_media_client, logging.getLogger("test"),
    )


# ── Sprint 6 fixtures ───────────────────────────────────────

@pytest_asyncio.fixture
async def achievement_engine(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> AchievementEngine:
    """AchievementEngine with test dependencies."""
    return AchievementEngine(
        sample_config, database, mock_client, logging.getLogger("test"),
    )


@pytest_asyncio.fixture
async def rank_engine(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> RankEngine:
    """RankEngine with test dependencies."""
    return RankEngine(
        sample_config, database, mock_client, logging.getLogger("test"),
    )


# ── Sprint 7 fixtures ───────────────────────────────────────

@pytest.fixture
def multiplier_engine(
    sample_config: EconomyConfig,
) -> MultiplierEngine:
    """MultiplierEngine with mocked presence tracker."""
    mock_presence = MagicMock()
    mock_presence.get_connected_users = MagicMock(return_value=set())
    return MultiplierEngine(
        sample_config, mock_presence, logging.getLogger("test"),
    )


@pytest_asyncio.fixture
async def competition_engine(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> CompetitionEngine:
    """CompetitionEngine with test dependencies."""
    return CompetitionEngine(
        sample_config, database, mock_client, logging.getLogger("test"),
    )


@pytest_asyncio.fixture
async def bounty_manager(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> BountyManager:
    """BountyManager with test dependencies."""
    return BountyManager(
        sample_config, database, mock_client, logging.getLogger("test"),
    )


# ── Sprint 8 fixtures ───────────────────────────────────────

@pytest_asyncio.fixture
async def presence_tracker(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> PresenceTracker:
    """PresenceTracker with test dependencies (not started)."""
    tracker = PresenceTracker(
        config=sample_config,
        database=database,
        client=mock_client,
        logger=logging.getLogger("test"),
    )
    return tracker


@pytest_asyncio.fixture
async def pm_handler(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    presence_tracker: PresenceTracker,
    earning_engine: EarningEngine,
    channel_state: ChannelStateTracker,
    gambling_engine: GamblingEngine,
    spending_engine: SpendingEngine,
    achievement_engine: AchievementEngine,
    rank_engine: RankEngine,
    multiplier_engine: MultiplierEngine,
    bounty_manager: BountyManager,
) -> PmHandler:
    """PmHandler with all Sprint 8 dependencies wired."""
    handler = PmHandler(
        config=sample_config,
        database=database,
        client=mock_client,
        presence_tracker=presence_tracker,
        logger=logging.getLogger("test"),
        earning_engine=earning_engine,
        channel_state=channel_state,
        gambling_engine=gambling_engine,
        spending_engine=spending_engine,
        achievement_engine=achievement_engine,
        rank_engine=rank_engine,
        multiplier_engine=multiplier_engine,
        bounty_manager=bounty_manager,
    )
    handler._config_path = None  # No reload in tests by default
    return handler


@pytest_asyncio.fixture
async def admin_scheduler(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
    presence_tracker: PresenceTracker,
    rank_engine: RankEngine,
) -> AdminScheduler:
    """AdminScheduler with test dependencies."""
    return AdminScheduler(
        config=sample_config,
        database=database,
        client=mock_client,
        presence_tracker=presence_tracker,
        rank_engine=rank_engine,
        logger=logging.getLogger("test"),
    )


# ── Sprint 9: MockKrytenClient ──────────────────────────────

class MockKrytenClient:
    """Mock kryten-py client for integration testing.

    Records all method calls for assertion.
    """

    def __init__(self) -> None:
        self.sent_pms: list[tuple[str, str, str]] = []
        self.sent_chats: list[tuple[str, str]] = []
        self.rank_changes: list[tuple[str, str, int]] = []
        self.media_adds: list[dict] = []
        self._handlers: dict[str, list] = {}
        self._request_reply_handlers: dict[str, Any] = {}
        self._kv_store: dict[str, dict[str, Any]] = {}

    async def send_pm(
        self, channel: str, username: str, message: str, *, domain: str | None = None,
    ) -> str:
        self.sent_pms.append((channel, username, message))
        return "mock-corr-id"

    async def send_chat(
        self, channel: str, message: str, *, domain: str | None = None,
    ) -> str:
        self.sent_chats.append((channel, message))
        return "mock-corr-id"

    async def safe_set_channel_rank(
        self, channel: str, username: str, rank: int,
        *, domain: str | None = None, check_rank: bool = True, timeout: float = 2.0,
    ) -> dict:
        self.rank_changes.append((channel, username, rank))
        return {"success": True}

    async def add_media(
        self, channel: str, media_type: str, media_id: str,
        *, position: str = "end", temp: bool = True, domain: str | None = None,
    ) -> str:
        self.media_adds.append({
            "channel": channel, "media_type": media_type,
            "media_id": media_id, "position": position, "temp": temp,
        })
        return "mock-corr-id"

    async def kv_get(
        self, bucket_name: str, key: str, default: Any = None, parse_json: bool = False,
    ) -> Any:
        return self._kv_store.get(bucket_name, {}).get(key, default)

    async def kv_put(
        self, bucket_name: str, key: str, value: Any, *, as_json: bool = False,
    ) -> None:
        self._kv_store.setdefault(bucket_name, {})[key] = value

    async def nats_request(self, subject: str, request: Any, timeout: float = 5) -> dict:
        return {}

    async def connect(self) -> None:
        pass

    async def run(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def subscribe(self, subject: str, handler: Any) -> None:
        pass

    async def subscribe_request_reply(self, subject: str, handler: Any) -> None:
        self._request_reply_handlers[subject] = handler

    async def get_or_create_kv_store(self, bucket_name: str, description: str = "") -> Any:
        self._kv_store.setdefault(bucket_name, {})
        return MagicMock()

    def on(self, event_name: str, channel: str | None = None, domain: str | None = None):
        """Match kryten-py's ``on()`` decorator signature."""
        def decorator(func):
            self._handlers.setdefault(event_name, []).append(func)
            return func
        return decorator

    def on_group_restart(self, callback):
        pass

    async def fire_event(self, event_name: str, event: Any) -> None:
        """Test helper: simulate an incoming event."""
        for handler in self._handlers.get(event_name, []):
            await handler(event)


@pytest.fixture
def mock_kryten_client() -> MockKrytenClient:
    """Return a MockKrytenClient for integration tests."""
    return MockKrytenClient()


# ── Sprint 9 fixtures ────────────────────────────────────────

@pytest_asyncio.fixture
async def event_announcer(
    sample_config: EconomyConfig,
    mock_client: MagicMock,
) -> AsyncGenerator[EventAnnouncer, None]:
    """EventAnnouncer with test dependencies."""
    announcer = EventAnnouncer(
        config=sample_config,
        client=mock_client,
        logger=logging.getLogger("test"),
    )
    # Don't start flush loop by default — tests control timing
    yield announcer
    await announcer.stop()


@pytest_asyncio.fixture
async def greeting_handler(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    presence_tracker: PresenceTracker,
    event_announcer: EventAnnouncer,
) -> GreetingHandler:
    """GreetingHandler with test dependencies."""
    return GreetingHandler(
        config=sample_config,
        database=database,
        presence_tracker=presence_tracker,
        announcer=event_announcer,
        logger=logging.getLogger("test"),
    )


@pytest.fixture
def rate_limiter() -> PmRateLimiter:
    """PmRateLimiter with default 10/min limit."""
    return PmRateLimiter(max_per_minute=10)
