"""Configuration system for kryten-economy.

All Pydantic models for all sprints are defined here with sensible defaults.
Only Sprint 1 & 2 fields are consumed at runtime; later-sprint sections are
defined so the config.example.yaml can be validated immediately.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from kryten import KrytenConfig
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
#  Sprint 1 — Core Foundation
# ═══════════════════════════════════════════════════════════════

class DatabaseConfig(BaseModel):
    path: str = "economy.db"


class CurrencyConfig(BaseModel):
    name: str = "Z-Coin"
    symbol: str = "Z"
    plural: str = "Z-Coins"


class BotConfig(BaseModel):
    username: str = "ZCoinBot"


class OnboardingConfig(BaseModel):
    welcome_wallet: int = 100
    welcome_message: str = (
        "Welcome! You've got {amount} {currency}. "
        "Stick around and you'll earn more. Try 'help' to see what you can do."
    )
    min_account_age_minutes: int = 0
    min_messages_to_earn: int = 0


class NightWatchConfig(BaseModel):
    """Night-watch multiplier for off-peak presence earning."""
    enabled: bool = False
    hours: list[int] = Field(default=[2, 3, 4, 5, 6, 7], description="UTC hours (24h format)")
    multiplier: float = 1.5


class PresenceConfig(BaseModel):
    base_rate_per_minute: int = 1
    active_bonus_per_minute: int = 0
    afk_threshold_minutes: int = 5
    join_debounce_minutes: int = 5
    greeting_absence_minutes: int = 30
    # Sprint 2 fields defined here with defaults
    hourly_milestones: dict[int, int] = Field(
        default={1: 10, 3: 30, 6: 75, 12: 200, 24: 1000},
        description="Hours → Z reward mapping for dwell milestones",
    )
    night_watch: NightWatchConfig = Field(default_factory=NightWatchConfig)


# ═══════════════════════════════════════════════════════════════
#  Sprint 2 — Streaks, Milestones & Dwell Incentives
# ═══════════════════════════════════════════════════════════════

class DailyStreakConfig(BaseModel):
    enabled: bool = True
    min_presence_minutes: int = 15
    rewards: dict[int, int] = Field(
        default={2: 10, 3: 20, 4: 30, 5: 50, 6: 75, 7: 100},
        description="Streak day number → Z reward",
    )
    milestone_7_bonus: int = 200
    milestone_30_bonus: int = 2000


class WeekendBridgeConfig(BaseModel):
    enabled: bool = True
    bonus: int = 500
    announce_on_weekend: bool = True
    message: str = "Connect any weekday this week for a {amount} {currency} bridge bonus!"


class StreaksConfig(BaseModel):
    daily: DailyStreakConfig = Field(default_factory=DailyStreakConfig)
    weekend_weekday_bridge: WeekendBridgeConfig = Field(default_factory=WeekendBridgeConfig)


class RainConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 45
    min_amount: int = 5
    max_amount: int = 25
    pm_notification: bool = True
    message: str = "☔ Rain drop! You received {amount} {currency} just for being here."


class InterestConfig(BaseModel):
    daily_rate: float = 0.001
    max_daily_interest: int = 10
    min_balance_to_earn: int = 100


class DecayConfig(BaseModel):
    enabled: bool = False
    daily_rate: float = 0.005
    exempt_below: int = 50000
    label: str = "Vault maintenance fee"


class BalanceMaintenanceConfig(BaseModel):
    mode: str = Field(default="interest", description="'interest', 'decay', or 'none'")
    interest: InterestConfig = Field(default_factory=InterestConfig)
    decay: DecayConfig = Field(default_factory=DecayConfig)


class WelcomeBackConfig(BaseModel):
    enabled: bool = True
    days_absent: int = 7
    bonus: int = 100
    message: str = "Welcome back! Here's {amount} {currency}. You've been missed. 💚"


class InactivityNudgeConfig(BaseModel):
    enabled: bool = False
    days_absent: int = 14
    message: str = "We miss you! Your balance of {balance} {currency} is waiting."


class RetentionConfig(BaseModel):
    welcome_back: WelcomeBackConfig = Field(default_factory=WelcomeBackConfig)
    inactivity_nudge: InactivityNudgeConfig = Field(default_factory=InactivityNudgeConfig)


# ═══════════════════════════════════════════════════════════════
#  Sprint 3 — Chat Earning Triggers
# ═══════════════════════════════════════════════════════════════

class LongMessageTrigger(BaseModel):
    enabled: bool = True
    min_chars: int = 30
    reward: int = 1
    max_per_hour: int = 30
    hidden: bool = True


class LaughReceivedTrigger(BaseModel):
    enabled: bool = True
    reward_per_laugher: int = 2
    max_laughers_per_joke: int = 10
    self_excluded: bool = True
    hidden: bool = True


class KudosReceivedTrigger(BaseModel):
    enabled: bool = True
    reward: int = 3
    self_excluded: bool = True
    hidden: bool = True


class FirstMessageOfDayTrigger(BaseModel):
    enabled: bool = True
    reward: int = 5
    hidden: bool = True


class ConversationStarterTrigger(BaseModel):
    enabled: bool = True
    min_silence_minutes: int = 10
    reward: int = 10
    hidden: bool = True


class FirstAfterMediaChangeTrigger(BaseModel):
    enabled: bool = True
    window_seconds: int = 30
    reward: int = 3
    hidden: bool = True


class ChatTriggersConfig(BaseModel):
    long_message: LongMessageTrigger = Field(default_factory=LongMessageTrigger)
    laugh_received: LaughReceivedTrigger = Field(default_factory=LaughReceivedTrigger)
    kudos_received: KudosReceivedTrigger = Field(default_factory=KudosReceivedTrigger)
    first_message_of_day: FirstMessageOfDayTrigger = Field(default_factory=FirstMessageOfDayTrigger)
    conversation_starter: ConversationStarterTrigger = Field(default_factory=ConversationStarterTrigger)


class CommentDuringMediaTrigger(BaseModel):
    enabled: bool = True
    reward_per_message: float = 0.5
    max_per_item_base: int = 10
    scale_with_duration: bool = True
    hidden: bool = True


class LikeCurrentTrigger(BaseModel):
    enabled: bool = True
    reward: int = 2
    hidden: bool = True


class SurvivedFullMediaTrigger(BaseModel):
    enabled: bool = True
    min_presence_percent: int = 80
    reward: int = 5
    hidden: bool = True


class PresentAtEventStartTrigger(BaseModel):
    enabled: bool = True
    default_reward: int = 100
    hidden: bool = True


class ContentTriggersConfig(BaseModel):
    first_after_media_change: FirstAfterMediaChangeTrigger = Field(default_factory=FirstAfterMediaChangeTrigger)
    comment_during_media: CommentDuringMediaTrigger = Field(default_factory=CommentDuringMediaTrigger)
    like_current: LikeCurrentTrigger = Field(default_factory=LikeCurrentTrigger)
    survived_full_media: SurvivedFullMediaTrigger = Field(default_factory=SurvivedFullMediaTrigger)
    present_at_event_start: PresentAtEventStartTrigger = Field(default_factory=PresentAtEventStartTrigger)


class GreetedNewcomerTrigger(BaseModel):
    enabled: bool = True
    window_seconds: int = 60
    reward: int = 3
    bot_joins_excluded: bool = True
    hidden: bool = True


class MentionedByOtherTrigger(BaseModel):
    enabled: bool = True
    reward: int = 1
    max_per_hour_same_user: int = 5
    hidden: bool = True


class BotInteractionTrigger(BaseModel):
    enabled: bool = True
    reward: int = 2
    max_per_day: int = 10
    hidden: bool = True


class SocialTriggersConfig(BaseModel):
    greeted_newcomer: GreetedNewcomerTrigger = Field(default_factory=GreetedNewcomerTrigger)
    mentioned_by_other: MentionedByOtherTrigger = Field(default_factory=MentionedByOtherTrigger)
    bot_interaction: BotInteractionTrigger = Field(default_factory=BotInteractionTrigger)


# ═══════════════════════════════════════════════════════════════
#  Sprint 4 — Gambling
# ═══════════════════════════════════════════════════════════════

class SpinPayoutConfig(BaseModel):
    symbols: str
    multiplier: float
    probability: float


class SpinConfig(BaseModel):
    enabled: bool = True
    min_wager: int = 10
    max_wager: int = 500
    cooldown_seconds: int = 30
    daily_limit: int = 50
    payouts: list[SpinPayoutConfig] = Field(default_factory=lambda: [
        SpinPayoutConfig(symbols="🍒🍒🍒", multiplier=3, probability=0.10),
        SpinPayoutConfig(symbols="🍋🍋🍋", multiplier=5, probability=0.05),
        SpinPayoutConfig(symbols="💎💎💎", multiplier=10, probability=0.02),
        SpinPayoutConfig(symbols="7️⃣7️⃣7️⃣", multiplier=50, probability=0.002),
        SpinPayoutConfig(symbols="partial", multiplier=2, probability=0.15),
        SpinPayoutConfig(symbols="loss", multiplier=0, probability=0.678),
    ])
    announce_jackpots_public: bool = True
    jackpot_announce_threshold: int = 500


class FlipConfig(BaseModel):
    enabled: bool = True
    min_wager: int = 10
    max_wager: int = 1000
    win_chance: float = 0.45
    cooldown_seconds: int = 15
    daily_limit: int = 100


class ChallengeConfig(BaseModel):
    enabled: bool = True
    min_wager: int = 50
    max_wager: int = 5000
    accept_timeout_seconds: int = 120
    rake_percent: float = 5
    announce_public: bool = True


class DailyFreeSpinConfig(BaseModel):
    enabled: bool = True
    equivalent_wager: int = 50


class HeistConfig(BaseModel):
    enabled: bool = False
    min_participants: int = 3
    join_window_seconds: int = 120
    success_chance: float = 0.40
    push_chance: float = 0.15
    push_fee_pct: float = 0.05
    payout_multiplier: float = 1.5
    crew_bonus_per_player: float = 0.25
    cooldown_seconds: int = 180
    min_wager: int = 20
    max_wager: int = 5000
    announce_public: bool = True


class GamblingConfig(BaseModel):
    enabled: bool = True
    min_account_age_minutes: int = 60
    spin: SpinConfig = Field(default_factory=SpinConfig)
    flip: FlipConfig = Field(default_factory=FlipConfig)
    challenge: ChallengeConfig = Field(default_factory=ChallengeConfig)
    daily_free_spin: DailyFreeSpinConfig = Field(default_factory=DailyFreeSpinConfig)
    heist: HeistConfig = Field(default_factory=HeistConfig)


# ═══════════════════════════════════════════════════════════════
#  Sprint 5 — Spending, Queue, Tips & Shop
# ═══════════════════════════════════════════════════════════════

class QueueTierConfig(BaseModel):
    max_minutes: int
    label: str
    cost: int


class BlackoutWindowConfig(BaseModel):
    name: str
    cron: str
    duration_hours: int


class SpendingConfig(BaseModel):
    queue_tiers: list[QueueTierConfig] = Field(default_factory=lambda: [
        QueueTierConfig(max_minutes=15, label="Short / Music Video", cost=2500),
        QueueTierConfig(max_minutes=35, label="30-min Episode", cost=5000),
        QueueTierConfig(max_minutes=65, label="60-min Episode", cost=7500),
        QueueTierConfig(max_minutes=999, label="Movie", cost=10000),
    ])
    interrupt_play_next: int = 100000
    force_play_now: int = 1000000
    force_play_requires_admin: bool = True
    max_queues_per_day: int = 3
    queue_cooldown_minutes: int = 30
    blackout_windows: list[BlackoutWindowConfig] = Field(default_factory=list)


class MediaCMSConfig(BaseModel):
    base_url: str = "https://media.example.com"
    api_token: str = "your-token-here"
    search_results_limit: int = 10


class VanityItemConfig(BaseModel):
    enabled: bool = True
    cost: int
    description: str = ""


class ChatColorPaletteEntry(BaseModel):
    name: str
    hex: str


class ChatColorConfig(BaseModel):
    enabled: bool = True
    cost: int = 7500
    description: str = "Choose a color for your chat messages from the approved palette"
    palette: list[ChatColorPaletteEntry] = Field(default_factory=lambda: [
        ChatColorPaletteEntry(name="Crimson", hex="#DC143C"),
        ChatColorPaletteEntry(name="Gold", hex="#FFD700"),
        ChatColorPaletteEntry(name="Emerald", hex="#50C878"),
        ChatColorPaletteEntry(name="Royal Blue", hex="#4169E1"),
        ChatColorPaletteEntry(name="Orchid", hex="#DA70D6"),
        ChatColorPaletteEntry(name="Coral", hex="#FF7F50"),
        ChatColorPaletteEntry(name="Teal", hex="#008080"),
        ChatColorPaletteEntry(name="Silver Screen", hex="#C0C0C0"),
    ])


class ChannelGifConfig(BaseModel):
    enabled: bool = True
    cost: int = 50000
    description: str = "Personalized channel GIF (requires admin approval)"
    requires_admin_approval: bool = True


class ShoutoutConfig(BaseModel):
    enabled: bool = True
    cost: int = 500
    description: str = "Bot posts your custom message in public chat"
    max_length: int = 200
    cooldown_minutes: int = 60


class DailyFortuneConfig(BaseModel):
    enabled: bool = True
    cost: int = 100
    description: str = "Receive a random fortune / horoscope"


class RenameCurrencyConfig(BaseModel):
    enabled: bool = True
    cost: int = 25000
    description: str = "Your balance displays with a custom currency name (e.g. 'TacoBucks')"


class VanityShopConfig(BaseModel):
    custom_greeting: VanityItemConfig = Field(
        default_factory=lambda: VanityItemConfig(cost=5000, description="Bot greets you by name when you join")
    )
    custom_title: VanityItemConfig = Field(
        default_factory=lambda: VanityItemConfig(cost=10000, description="Custom title shown in bot announcements")
    )
    chat_color: ChatColorConfig = Field(default_factory=ChatColorConfig)
    channel_gif: ChannelGifConfig = Field(default_factory=ChannelGifConfig)
    shoutout: ShoutoutConfig = Field(default_factory=ShoutoutConfig)
    daily_fortune: DailyFortuneConfig = Field(default_factory=DailyFortuneConfig)
    rename_currency_personal: RenameCurrencyConfig = Field(default_factory=RenameCurrencyConfig)


class TippingConfig(BaseModel):
    enabled: bool = True
    min_amount: int = 1
    max_per_day: int = 5000
    min_account_age_minutes: int = 30
    self_tip_blocked: bool = True


# ═══════════════════════════════════════════════════════════════
#  Sprint 6 — Achievements & Ranks
# ═══════════════════════════════════════════════════════════════

class AchievementConditionConfig(BaseModel):
    type: str
    threshold: int | None = None
    field: str | None = None


class AchievementConfig(BaseModel):
    id: str
    description: str = ""
    condition: AchievementConditionConfig
    reward: int = 0
    reward_percent_of_earnings: int | None = None
    hidden: bool = True


class RankTierConfig(BaseModel):
    name: str
    min_lifetime_earned: int = 0
    perks: list[str] = Field(default_factory=list)
    cytube_level_promotion: int | None = None


class RanksConfig(BaseModel):
    earn_multiplier_per_rank: float = 0.0
    spend_discount_per_rank: float = 0.02
    tiers: list[RankTierConfig] = Field(default_factory=lambda: [
        RankTierConfig(name="Extra", min_lifetime_earned=0),
        RankTierConfig(name="Grip", min_lifetime_earned=1000, perks=["1 free daily fortune"]),
        RankTierConfig(name="Key Grip", min_lifetime_earned=5000, perks=["2% spend discount"]),
        RankTierConfig(name="Gaffer", min_lifetime_earned=15000, perks=["4% discount", "rain drops +20%"]),
        RankTierConfig(name="Best Boy", min_lifetime_earned=40000, perks=["6% discount", "+1 queue/day"]),
        RankTierConfig(
            name="Associate Producer", min_lifetime_earned=100000, perks=["8% discount", "premium vanity items"]
        ),
        RankTierConfig(name="Producer", min_lifetime_earned=250000, perks=["10% discount", "priority queue position"]),
        RankTierConfig(name="Director", min_lifetime_earned=500000, perks=["12% discount", "+2 queues/day"]),
        RankTierConfig(name="Executive Producer", min_lifetime_earned=1000000, perks=["15% discount"]),
        RankTierConfig(
            name="Studio Mogul",
            min_lifetime_earned=5000000,
            perks=["20% discount", "custom everything", "legendary status"],
            cytube_level_promotion=2,
        ),
    ])


class CytubePromotionConfig(BaseModel):
    enabled: bool = True
    purchasable: bool = True
    cost: int = 500000
    min_rank: str = "Associate Producer"


# ═══════════════════════════════════════════════════════════════
#  Sprint 7 — Events, Multipliers & Bounties
# ═══════════════════════════════════════════════════════════════

class CompetitionConditionConfig(BaseModel):
    type: str
    field: str | None = None
    threshold: int | None = None


class CompetitionConfig(BaseModel):
    id: str
    description: str = ""
    condition: CompetitionConditionConfig
    reward: int = 0
    reward_percent_of_earnings: int | None = None
    hidden: bool = True


class OffPeakMultiplierConfig(BaseModel):
    enabled: bool = True
    days: list[int] = Field(default=[1, 2, 3, 4], description="Day of week (0=Sun)")
    hours: list[int] = Field(default=[6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
    multiplier: float = 2.0
    announce: bool = True


class HighPopulationMultiplierConfig(BaseModel):
    enabled: bool = True
    min_users: int = 10
    multiplier: float = 1.5
    hidden: bool = True


class HolidayDateConfig(BaseModel):
    date: str
    name: str
    multiplier: float


class HolidaysMultiplierConfig(BaseModel):
    enabled: bool = True
    dates: list[HolidayDateConfig] = Field(default_factory=lambda: [
        HolidayDateConfig(date="12-25", name="Christmas", multiplier=3.0),
        HolidayDateConfig(date="10-31", name="Halloween", multiplier=2.0),
    ])
    announce: bool = True


class ScheduledEventConfig(BaseModel):
    name: str
    cron: str
    duration_hours: int
    multiplier: float = 2.0
    presence_bonus: int = 0
    announce: bool = True


class MultipliersConfig(BaseModel):
    off_peak: OffPeakMultiplierConfig = Field(default_factory=OffPeakMultiplierConfig)
    high_population: HighPopulationMultiplierConfig = Field(default_factory=HighPopulationMultiplierConfig)
    holidays: HolidaysMultiplierConfig = Field(default_factory=HolidaysMultiplierConfig)
    scheduled_events: list[ScheduledEventConfig] = Field(default_factory=list)


class BountyConfig(BaseModel):
    enabled: bool = True
    min_amount: int = 100
    max_amount: int = 50000
    max_open_per_user: int = 3
    default_expiry_hours: int = 168  # 7 days
    expiry_refund_percent: int = 50  # 50% refund on expiry
    description_max_length: int = 200


# ═══════════════════════════════════════════════════════════════
#  Sprint 8 — Admin & Reporting
# ═══════════════════════════════════════════════════════════════

class AdminConfig(BaseModel):
    owner_level: int = 4


class AnnouncementTemplatesConfig(BaseModel):
    queue: str = '🎬 {user} just queued "{title}"! ({cost} {currency})'
    jackpot: str = "🎰 JACKPOT! {user} just won {amount} {currency}!"
    rank_up: str = "⭐ {user} is now a {rank}!"
    streak: str = "🔥 {user} hit a {days}-day streak!"
    greeting: str = "👋 {greeting}"
    rain: str = "☔ Rain! {count} users just got free {currency}."
    challenge_win: str = "⚔️ {winner} defeated {loser} and won {amount} {currency}!"
    flip_win: str = "🪙 {user} flipped and won {amount} {currency}!"
    free_spin_win: str = "🎁 {user} won {amount} {currency} on a FREE spin!"


class AnnouncementsConfig(BaseModel):
    queue_purchase: bool = True
    gambling_jackpot: bool = True
    jackpot_min_amount: int = 500
    achievement_milestone: bool = True
    rank_promotion: bool = True
    challenge_result: bool = True
    heist_result: bool = True
    rain_drop: bool = True
    daily_champion: bool = True
    streak_milestone: bool = True
    custom_greeting: bool = True
    templates: AnnouncementTemplatesConfig = Field(default_factory=AnnouncementTemplatesConfig)


class UserDigestConfig(BaseModel):
    enabled: bool = True
    send_hour_utc: int = 4
    message: str = (
        "📊 Daily Summary:\n"
        "Earned: {earned} {currency} | Spent: {spent} | Balance: {balance}\n"
        "Rank: {rank} | Streak: {streak} days 🔥\n"
        "Next goal: {next_goal_description} ({days_away} days away)"
    )


class AdminDigestConfig(BaseModel):
    enabled: bool = True
    send_hour_utc: int = 5


class DigestConfig(BaseModel):
    user_digest: UserDigestConfig = Field(default_factory=UserDigestConfig)
    admin_digest: AdminDigestConfig = Field(default_factory=AdminDigestConfig)


# ═══════════════════════════════════════════════════════════════
#  Sprint 9 — Polish & Hardening
# ═══════════════════════════════════════════════════════════════

class CommandsConfig(BaseModel):
    rate_limit_per_minute: int = 10


# NOTE: We do NOT define a local MetricsConfig — we reuse KrytenConfig's
# kryten.config.MetricsConfig which includes port, health_path, metrics_path.
# The EconomyConfig.metrics field is inherited from KrytenConfig.


# ═══════════════════════════════════════════════════════════════
#  Top-Level Economy Config
# ═══════════════════════════════════════════════════════════════

class EconomyConfig(KrytenConfig):
    """Full economy config — extends KrytenConfig with all economy sub-models."""

    # Sprint 1 — Core Foundation
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    ignored_users: list[str] = Field(default_factory=list)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)

    # Sprint 2 — Streaks, Milestones & Dwell
    streaks: StreaksConfig = Field(default_factory=StreaksConfig)
    rain: RainConfig = Field(default_factory=RainConfig)
    balance_maintenance: BalanceMaintenanceConfig = Field(default_factory=BalanceMaintenanceConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

    # Sprint 3 — Chat Earning Triggers
    chat_triggers: ChatTriggersConfig = Field(default_factory=ChatTriggersConfig)
    content_triggers: ContentTriggersConfig = Field(default_factory=ContentTriggersConfig)
    social_triggers: SocialTriggersConfig = Field(default_factory=SocialTriggersConfig)

    # Sprint 4 — Gambling
    gambling: GamblingConfig = Field(default_factory=GamblingConfig)

    # Sprint 5 — Spending, Queue, Tips & Shop
    spending: SpendingConfig = Field(default_factory=SpendingConfig)
    mediacms: MediaCMSConfig = Field(default_factory=MediaCMSConfig)
    vanity_shop: VanityShopConfig = Field(default_factory=VanityShopConfig)
    tipping: TippingConfig = Field(default_factory=TippingConfig)

    # Sprint 6 — Achievements & Ranks
    achievements: list[AchievementConfig] = Field(default_factory=list)
    ranks: RanksConfig = Field(default_factory=RanksConfig)
    cytube_promotion: CytubePromotionConfig = Field(default_factory=CytubePromotionConfig)

    # Sprint 7 — Events, Multipliers & Bounties
    daily_competitions: list[CompetitionConfig] = Field(default_factory=list)
    multipliers: MultipliersConfig = Field(default_factory=MultipliersConfig)
    bounties: BountyConfig = Field(default_factory=BountyConfig)

    # Sprint 8 — Admin & Reporting
    admin: AdminConfig = Field(default_factory=AdminConfig)
    announcements: AnnouncementsConfig = Field(default_factory=AnnouncementsConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)

    # Sprint 9 — Polish & Hardening
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    # NOTE: metrics is inherited from KrytenConfig (kryten.config.MetricsConfig)
    #       which includes port, health_path, metrics_path


# ═══════════════════════════════════════════════════════════════
#  Config Loading
# ═══════════════════════════════════════════════════════════════

def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in string values."""
    if isinstance(obj, str):
        return re.sub(
            r"\$\{([^}:]+)(?::-(.*?))?\}",
            lambda m: os.environ.get(m.group(1), m.group(2) or ""),
            obj,
        )
    elif isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    return obj


def load_config(config_path: str) -> EconomyConfig:
    """Load and validate YAML config file into EconomyConfig."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Config file must contain a YAML mapping at the top level.")

    raw = _expand_env_vars(raw)
    return EconomyConfig(**raw)
