"""One-shot script to write comprehensive config.yaml — delete after use."""
content = """\
# =====================================================================
#  kryten-economy -- Channel-Z Dev Config
#  All available config options are listed here, even if they are
#  at their default values, so the full config surface is visible.
# =====================================================================

# -- NATS Messaging ---------------------------------------------------
# Connection URLs for the NATS message broker. The bot publishes
# and subscribes to economy events over NATS.
nats:
  servers: ["nats://localhost:4222"]

# -- Channel Scope ----------------------------------------------------
# Which cytu.be channels this bot instance manages.
channels:
  - domain: cytu.be
    channel: Channel-Z

# -- Service Identity -------------------------------------------------
# Kryten service registration fields. Used for heartbeat, lifecycle
# events, and service discovery on the NATS bus.
service:
  name: economy
  version: "0.1.0"
  enable_lifecycle: true      # Emit started/stopped lifecycle events
  enable_heartbeat: true      # Send periodic heartbeat pings
  enable_discovery: true      # Respond to service-discovery queries
  heartbeat_interval: 30      # Seconds between heartbeat pings

# -- Metrics / Health -------------------------------------------------
# Prometheus metrics and HTTP health endpoint.
# Health check: http://localhost:<port>/health
metrics:
  port: 28290

# =====================================================================
#  Sprint 1 -- Core Foundation
# =====================================================================

# -- Database ---------------------------------------------------------
# Path to the SQLite database file (relative to working directory).
database:
  path: economy-dev.db

# -- Currency ---------------------------------------------------------
# Name and symbol for the channel virtual currency.
# {currency} and {symbol} placeholders in messages resolve from these.
currency:
  name: "Z-Coin"
  symbol: "Z"
  plural: "Z-Coins"

# -- Bot Identity -----------------------------------------------------
# The cytu.be username this bot runs under. Used to ignore self-messages.
bot:
  username: "ZCoinBot"

# -- Ignored Users ----------------------------------------------------
# Bot / puppet accounts that should never earn currency or trigger events.
ignored_users:
  - "SaveTheRobots"
  - "FaxyBrown"
  - "VHSOracle"

# -- Onboarding -------------------------------------------------------
# Settings for first-time account creation. The welcome wallet is
# a one-time Z grant on new account creation.
# Placeholders: {amount}, {currency}
onboarding:
  welcome_wallet: 250                    # Z given on first join
  welcome_message: >
    Welcome to Channel-Z!! You've got {amount} {currency} to start.
    Stick around and you'll earn more and gain the ability to do cool things, like add to our queue or purchase a username-Gif. \\n\\nTry 'help' to see what you can do.
  min_account_age_minutes: 0            # Min site account age to auto-enroll (0 = anyone)
  min_messages_to_earn: 0              # Min chat messages before earning starts

# -- Presence Earning -------------------------------------------------
# Passive Z earned just for being in the channel. base_rate fires
# every minute for all present users. active_bonus is awarded when
# the user has typed recently (not AFK).
# NOTE: hourly_milestones and night_watch live under presence (NOT under rain).
presence:
  base_rate_per_minute: 2              # Z per minute for any present user
  active_bonus_per_minute: 2           # Additional Z if not AFK (typed recently)
  afk_threshold_minutes: 5            # Minutes of silence before flagged AFK
  join_debounce_minutes: 5            # Re-join events within this window are ignored
  greeting_absence_minutes: 90        # Must have been absent this long to get a greeting

  # Hours-of-dwell -> bonus Z for staying that long continuously.
  # Keys are hours (int), values are the one-time reward (int).
  hourly_milestones:
    1:  10
    3:  30
    6:  75
    12: 200
    24: 1000

  # Night-watch: earn extra during low-traffic hours to reward loyal night owls.
  night_watch:
    enabled: true
    hours: [2, 3, 4, 5, 6, 7]         # UTC hours (24-hour) when multiplier applies
    multiplier: 1.5                    # Presence earning x this during night hours

# =====================================================================
#  Sprint 2 -- Streaks, Milestones & Dwell Incentives
# =====================================================================

# -- Rain -------------------------------------------------------------
# Periodic random Z drops to all present users. Set enabled: false to
# stop auto-rain; admins can still trigger rain manually with !rain.
rain:
  enabled: false                       # Auto-rain on a schedule (false = manual only)
  interval_minutes: 45                 # How often auto-rain fires
  min_amount: 5                        # Minimum Z per user per rain event
  max_amount: 25                       # Maximum Z per user per rain event
  pm_notification: true               # DM each recipient with their rain amount
  message: "Rain drop! You received {amount} {currency} just for being here."

# -- Daily Streaks ----------------------------------------------------
# Reward users who check in on consecutive days. Streak counters reset
# if the user misses a full calendar day. Milestone bonuses stack on
# top of the per-day reward table.
streaks:
  daily:
    enabled: true
    min_presence_minutes: 15           # Must be present this long to count the day
    rewards:                           # Streak length (days) -> bonus Z
      2:  10
      3:  20
      4:  30
      5:  50
      6:  75
      7: 100
    milestone_7_bonus: 200             # Extra bonus at the 7-day milestone
    milestone_30_bonus: 2000           # Extra bonus at the 30-day milestone

  # Weekend-weekday bridge: reward users who bridge the Mon-Fri gap over the weekend.
  weekend_weekday_bridge:
    enabled: true
    bonus: 500                         # Z awarded for the bridge
    announce_on_weekend: true          # Announce the opportunity on weekends
    message: "Connect any weekday this week for a {amount} {currency} bridge bonus!"

# -- Balance Maintenance ----------------------------------------------
# Optional interest or decay on idle balances.
# mode: "interest" -- balances grow passively (rich get richer, gently)
# mode: "decay"    -- inactive balances shrink (encourages spending)
# mode: "none"     -- no automatic balance changes
balance_maintenance:
  mode: none                           # "interest", "decay", or "none"

  # Interest: daily_rate % applied to balance, capped at max_daily_interest.
  interest:
    daily_rate: 0.001                  # 0.1% per day
    max_daily_interest: 10             # Hard cap on daily interest Z
    min_balance_to_earn: 100           # Must hold at least this much to earn interest

  # Decay: idle balances are trimmed daily. Exempt low balances from decay.
  decay:
    enabled: false
    daily_rate: 0.005                  # 0.5% per day removed
    exempt_below: 50000                # Balances below this are not decayed
    label: "Vault maintenance fee"     # Label shown in transaction history

# -- Retention --------------------------------------------------------
# Bonuses for users returning after an absence, and optional nudge
# messages sent to very long-absent users.
retention:
  # welcome_back: one-time bonus when a user returns after days_absent days.
  welcome_back:
    enabled: true
    days_absent: 7                     # Must have been gone this many days to qualify
    bonus: 100                         # Z granted on return
    message: "Welcome back! Here's {amount} {currency}. You've been missed."

  # inactivity_nudge: send a DM to users who haven't been seen in a long time.
  inactivity_nudge:
    enabled: false
    days_absent: 14
    message: "We miss you! Your balance of {balance} {currency} is waiting."

# =====================================================================
#  Sprint 3 -- Chat Earning Triggers
# =====================================================================

# -- Chat Triggers ----------------------------------------------------
# Reward users for positive chat behaviours. Most are hidden so they
# feel like pleasant surprises rather than overt game mechanics.
chat_triggers:
  # Long messages: reward substantive contributions.
  long_message:
    enabled: true
    min_chars: 30                      # Message must be at least this many characters
    reward: 1                          # Z per qualifying message
    max_per_hour: 30                   # Stop rewarding after this many per hour
    hidden: true                       # Not listed in !help output

  # Laugh received: earn Z when others react with laughter to your message.
  laugh_received:
    enabled: true
    reward_per_laugher: 2              # Z per person who laughs
    max_laughers_per_joke: 10          # Cap per message
    self_excluded: true                # You can't make yourself laugh
    hidden: true

  # Kudos received: earn Z when someone kudos / praises you.
  kudos_received:
    enabled: true
    reward: 3
    self_excluded: true
    hidden: true

  # First message of day: bonus for your first chat message each calendar day.
  first_message_of_day:
    enabled: true
    reward: 5
    hidden: true

  # Conversation starter: reward breaking silence in a quiet room.
  conversation_starter:
    enabled: true
    min_silence_minutes: 10            # Room must have been quiet this long
    reward: 10
    hidden: true

# -- Content Triggers -------------------------------------------------
# Rewards tied to media playback events.
content_triggers:
  # First message after the media changes: greet the new video.
  first_after_media_change:
    enabled: true
    window_seconds: 30                 # How long after change the bonus is available
    reward: 3
    hidden: true

  # Comment during media: earn for chatting while something is playing.
  comment_during_media:
    enabled: true
    reward_per_message: 0.5            # Fractional Z per message (accumulated)
    max_per_item_base: 10              # Max rewards per media item
    scale_with_duration: true          # Longer items -> higher cap
    hidden: true

  # Like current: earn for liking the currently-playing item.
  like_current:
    enabled: true
    reward: 2
    hidden: true

  # Survived full media: earn for staying through an entire item.
  survived_full_media:
    enabled: true
    min_presence_percent: 80           # Must be present for 80% of the runtime
    reward: 5
    hidden: true

  # Present at event start: bonus for being in the room when an event kicks off.
  present_at_event_start:
    enabled: true
    default_reward: 100
    hidden: true

# -- Social Triggers --------------------------------------------------
# Rewards for positive social interactions.
social_triggers:
  # Greeted newcomer: earn for welcoming someone right after they join.
  greeted_newcomer:
    enabled: true
    window_seconds: 60                 # Must greet within this window
    reward: 3
    bot_joins_excluded: true           # Bot rejoins don't trigger this
    hidden: true

  # Mentioned by other: earn when another user @-mentions you.
  mentioned_by_other:
    enabled: true
    reward: 1
    max_per_hour_same_user: 5          # Cap same-user mentions per hour
    hidden: true

  # Bot interaction: earn for sending intentional commands to the bot.
  bot_interaction:
    enabled: true
    reward: 2
    max_per_day: 10
    hidden: true

# =====================================================================
#  Sprint 4 -- Gambling
# =====================================================================

# -- Gambling ---------------------------------------------------------
# Top-level gambling toggle. min_account_age_minutes prevents brand-new
# accounts from immediately gambling away a welcome bonus.
gambling:
  enabled: true
  min_account_age_minutes: 60         # Account must be this old to gamble

  # Slots / Spin -------------------------------------------------------
  # Symbols are emoji strings; probability values must sum to 1.0.
  # Jackpot announcements fire when a win exceeds jackpot_announce_threshold.
  spin:
    enabled: true
    min_wager: 10
    max_wager: 5000
    cooldown_seconds: 10              # Seconds between spins for a user
    daily_limit: 50                   # Max spins per user per day
    announce_jackpots_public: true    # Post big wins to chat
    jackpot_announce_threshold: 500   # Win must be >= this to announce publicly
    payouts:
      - symbols: "three-cherries"
        multiplier: 3
        probability: 0.10
      - symbols: "three-lemons"
        multiplier: 5
        probability: 0.05
      - symbols: "three-diamonds"
        multiplier: 10
        probability: 0.02
      - symbols: "triple-7"
        multiplier: 50
        probability: 0.002
      - symbols: "partial"            # 2-of-3 match (any combo)
        multiplier: 2
        probability: 0.15
      - symbols: "loss"               # No match
        multiplier: 0
        probability: 0.678

  # Coin Flip ----------------------------------------------------------
  # Simple heads/tails. win_chance < 0.5 gives the house an edge.
  flip:
    enabled: true
    min_wager: 5
    max_wager: 10000
    win_chance: 0.45                  # Probability of winning (10% house edge)
    cooldown_seconds: 5
    daily_limit: 100

  # PvP Challenge ------------------------------------------------------
  # User-vs-user wager. A rake is taken from the pot and burned.
  challenge:
    enabled: true
    min_wager: 10
    max_wager: 25000
    accept_timeout_seconds: 60        # Seconds the challenged user has to accept
    rake_percent: 5                   # % of pot taken as house fee
    announce_public: true             # Announce challenge results to chat

  # Daily Free Spin ----------------------------------------------------
  # One free spin per day (uses a phantom wager for payout calculation).
  daily_free_spin:
    enabled: true
    equivalent_wager: 50              # Phantom wager used to calculate winnings

  # Heist --------------------------------------------------------------
  # Cooperative multi-player heist. One user starts it, others join
  # before join_window_seconds expires. Rewards scale with crew size.
  # push: partial refund instead of a clean win/loss.
  # crew_bonus_per_player: extra multiplier per additional crew member
  #   e.g. base 1.5x + 0.25x per player -> 5-person crew = 2.5x
  heist:
    enabled: true
    min_wager: 20
    max_wager: 5000
    min_participants: 2               # Minimum crew size required to resolve
    join_window_seconds: 120          # How long others can join after start
    success_chance: 0.40             # Base win probability
    push_chance: 0.15                # Probability of push (partial refund)
    push_fee_pct: 0.05               # % of wager lost on a push
    payout_multiplier: 1.5           # Base win multiplier for a solo heist
    crew_bonus_per_player: 0.25      # Extra multiplier per additional crew member
    cooldown_seconds: 180            # Per-channel cooldown after a heist resolves
    announce_public: true            # Post heist narrative to chat

# =====================================================================
#  Sprint 5 -- Spending, Queue, Tips & Shop
# =====================================================================

# -- Spending / Video Queue -------------------------------------------
# Costs to add to the CyTube video queue. queue_tiers maps video
# duration ranges to Z costs (tiered by length). interrupt_play_next
# and force_play_now are premium actions with higher costs.
# Rank discounts apply automatically based on the user's rank tier.
spending:
  queue_tiers:                         # Cost to queue a video, tiered by duration
    - max_minutes: 15
      label: "Short / Music Video"
      cost: 25000
    - max_minutes: 35
      label: "30-min Episode"
      cost: 50000
    - max_minutes: 65
      label: "60-min Episode"
      cost: 75000
    - max_minutes: 999
      label: "Movie"
      cost: 100000
  interrupt_play_next: 1000000         # Cost to jump to the front of the queue
  force_play_now: 10000000             # Cost to immediately replace the current video
  force_play_requires_admin: true      # Restrict force-play to admins only
  max_queues_per_day: 3                # Max queue submissions per user per day
  queue_cooldown_minutes: 30           # Cooldown between submissions (minutes)
  blackout_windows: []                 # Time windows where queuing is blocked
  # Example blackout:
  # blackout_windows:
  #   - name: "Movie Night"
  #     cron: "0 20 * * 5"
  #     duration_hours: 4

# -- MediaCMS ---------------------------------------------------------
# Connection details for the DropSugar / MediaCMS media library.
# api_token is used to fetch media metadata for queue validation.
mediacms:
  base_url: "https://www.dropsugar.co"
  api_token: "07cef36ea6fb4730220de94f106e769cf0f6c9cf"
  search_results_limit: 10            # Max results returned by !search

# -- Vanity Shop ------------------------------------------------------
# One-time Z purchases for cosmetic features. Each item can be
# disabled individually. Prices here are 10x the base defaults.
vanity_shop:
  # Bot announces your name with a custom greeting when you join.
  custom_greeting:
    enabled: true
    cost: 50000
    description: "Bot greets you by name when you join"

  # A flair label shown beside your name in bot announcements.
  custom_title:
    enabled: true
    cost: 100000
    description: "Custom title shown in bot announcements"

  # Choose a color for your chat username from the approved palette.
  chat_color:
    enabled: true
    cost: 75000
    description: "Choose a color for your chat messages from the approved palette"
    palette:
      - name: "Crimson"
        hex: "#DC143C"
      - name: "Gold"
        hex: "#FFD700"
      - name: "Emerald"
        hex: "#50C878"
      - name: "Royal Blue"
        hex: "#4169E1"
      - name: "Orchid"
        hex: "#DA70D6"
      - name: "Coral"
        hex: "#FF7F50"
      - name: "Teal"
        hex: "#008080"
      - name: "Silver Screen"
        hex: "#C0C0C0"

  # Personalized channel GIF associated with your account.
  channel_gif:
    enabled: true
    cost: 500000
    description: "Personalized channel GIF (requires admin approval)"
    requires_admin_approval: true

  # Bot posts your custom message to public chat.
  shoutout:
    enabled: true
    cost: 5000
    description: "Bot posts your custom message in public chat"
    max_length: 200                    # Max characters in shoutout text
    cooldown_minutes: 60              # Per-user cooldown between shoutouts (minutes)

  # Pay to receive a random fortune / horoscope via DM.
  daily_fortune:
    enabled: true
    cost: 1000
    description: "Receive a random fortune / horoscope"

  # Your balance display uses a custom currency name.
  rename_currency_personal:
    enabled: true
    cost: 250000
    description: "Your balance displays with a custom currency name (e.g. TacoBucks)"

# -- Tipping ----------------------------------------------------------
# Allow users to send Z directly to other users. max_per_day is the
# total Z a single user can SEND (not receive) in one calendar day.
tipping:
  enabled: true
  min_amount: 1                        # Minimum tip amount
  max_per_day: 10000                   # Max Z a user can tip out per day
  min_account_age_minutes: 30          # Sender account must be this old to tip
  self_tip_blocked: true               # Prevent self-tipping exploits

# =====================================================================
#  Sprint 6 -- Achievements & Ranks
# =====================================================================

# -- Achievements -----------------------------------------------------
# Custom achievement badges. Each has a condition evaluated against
# the user's stats and an optional Z reward. hidden: true means the
# achievement is a surprise (not listed in !achievements).
# Leave as an empty list if not using achievements yet.
achievements: []
# Example:
# achievements:
#   - id: first_queue
#     description: "Queue your first video"
#     condition:
#       type: stat_threshold
#       field: queues_submitted
#       threshold: 1
#     reward: 500
#     hidden: false

# -- Ranks ------------------------------------------------------------
# Progression tiers based on lifetime earned Z. Users promote
# automatically when they cross a tier threshold. Perks are
# informational strings shown in !rank output.
# earn_multiplier_per_rank: additive earn bonus per rank above Extra (0 = off)
# spend_discount_per_rank: % spend discount per rank above Extra
# cytube_level_promotion: triggers a CyTube permission level change on promotion
ranks:
  earn_multiplier_per_rank: 0.0        # Additive earn bonus per rank step (0 = off)
  spend_discount_per_rank: 0.02        # 2% spend discount per rank above Extra
  tiers:
    - name: "Extra"
      min_lifetime_earned: 0
    - name: "Grip"
      min_lifetime_earned: 1000
      perks: ["1 free daily fortune"]
    - name: "Key Grip"
      min_lifetime_earned: 5000
      perks: ["2% spend discount"]
    - name: "Gaffer"
      min_lifetime_earned: 15000
      perks: ["4% discount", "rain drops +20%"]
    - name: "Best Boy"
      min_lifetime_earned: 40000
      perks: ["6% discount", "+1 queue/day"]
    - name: "Associate Producer"
      min_lifetime_earned: 100000
      perks: ["8% discount", "premium vanity items"]
    - name: "Producer"
      min_lifetime_earned: 250000
      perks: ["10% discount", "priority queue position"]
    - name: "Director"
      min_lifetime_earned: 500000
      perks: ["12% discount", "+2 queues/day"]
    - name: "Executive Producer"
      min_lifetime_earned: 1000000
      perks: ["15% discount"]
    - name: "Studio Mogul"
      min_lifetime_earned: 5000000
      perks: ["20% discount", "custom everything", "legendary status"]
      cytube_level_promotion: 2        # Promote to CyTube permission level 2

# -- CyTube Rank Promotion --------------------------------------------
# Users can purchase a CyTube permission-level promotion using Z.
# Requires min_rank or higher to be eligible.
cytube_promotion:
  enabled: true
  purchasable: true
  cost: 500000
  min_rank: "Associate Producer"       # Minimum rank required to purchase

# =====================================================================
#  Sprint 7 -- Events, Multipliers & Bounties
# =====================================================================

# -- Daily Competitions -----------------------------------------------
# Optional daily leaderboard competitions. Winner(s) receive a Z reward.
# Leave empty if not using daily competitions.
daily_competitions: []
# Example:
# daily_competitions:
#   - id: top_chatter
#     description: "Most chat messages today"
#     condition:
#       type: stat_threshold
#       field: messages_today
#     reward: 1000

# -- Multipliers ------------------------------------------------------
# Earning multipliers applied on top of base presence / chat rates.
# Multiple active multipliers stack multiplicatively.
multipliers:
  # Off-peak: bonus multiplier during low-traffic weekday hours.
  off_peak:
    enabled: true
    days: [1, 2, 3, 4]               # Day of week (0=Sun, 1=Mon ... 6=Sat)
    hours: [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]  # UTC hours
    multiplier: 2.0
    announce: true                    # Announce when off-peak multiplier activates

  # High population: bonus when the room has many viewers (party bonus).
  high_population:
    enabled: true
    min_users: 10                     # Headcount threshold to activate
    multiplier: 1.5
    hidden: true                      # Don't announce automatically

  # Holidays: seasonal multipliers on specific calendar dates (MM-DD format).
  holidays:
    enabled: true
    announce: true
    dates:
      - date: "12-25"
        name: "Christmas"
        multiplier: 3.0
      - date: "10-31"
        name: "Halloween"
        multiplier: 2.0

  # Scheduled events: cron-triggered multiplier windows (e.g. Movie Night).
  scheduled_events: []
  # Example:
  # scheduled_events:
  #   - name: "Movie Night"
  #     cron: "0 20 * * 5"
  #     duration_hours: 3
  #     multiplier: 2.0
  #     presence_bonus: 50            # Flat Z bonus per minute during event
  #     announce: true

# -- Bounties ---------------------------------------------------------
# Users can place Z bounties on tasks / community goals. The poster
# puts up the Z upfront; it is awarded when the condition is met.
# Expired bounties refund a portion back to the poster.
bounties:
  enabled: true
  min_amount: 100                      # Minimum bounty post size
  max_amount: 50000                    # Maximum bounty post size
  max_open_per_user: 3                 # Max simultaneous open bounties per user
  default_expiry_hours: 168            # Auto-expire after 7 days (168 hours)
  expiry_refund_percent: 50            # % of bounty returned to poster on expiry
  description_max_length: 200         # Max characters in bounty description

# =====================================================================
#  Sprint 8 -- Admin & Reporting
# =====================================================================

# -- Admin ------------------------------------------------------------
# owner_level is the CyTube permission level that grants full bot
# admin access (grant/revoke, force commands, view all admin reports).
admin:
  owner_level: 3

# -- Announcements ----------------------------------------------------
# Toggle which events are announced to public chat and customise
# the message templates. jackpot_min_amount filters small wins from
# being publicly announced.
# Template placeholders vary by event; see each template string.
announcements:
  queue_purchase: true                 # Announce queue additions to chat
  gambling_jackpot: true               # Announce big gambling wins
  jackpot_min_amount: 500              # Minimum win to trigger jackpot announcement
  achievement_milestone: true          # Announce when users earn achievements
  rank_promotion: true                 # Announce rank-ups
  challenge_result: true               # Announce PvP challenge outcomes
  heist_result: true                   # Announce heist outcomes (narrative text)
  rain_drop: true                      # Announce rain events to chat
  daily_champion: true                 # Announce daily competition winners
  streak_milestone: true               # Announce streak milestones
  custom_greeting: true                # Use custom greeting template on join

  # Message templates. Placeholders: {user}, {amount}, {currency}, {title},
  # {cost}, {rank}, {days}, {greeting}, {count}, {winner}, {loser}
  templates:
    queue:         "Queued: {title} by {user} ({cost} {currency})"
    jackpot:       "JACKPOT! {user} just won {amount} {currency}!"
    rank_up:       "{user} is now a {rank}!"
    streak:        "{user} hit a {days}-day streak!"
    greeting:      "{greeting}"
    rain:          "Rain! {count} users just got free {currency}. {user} made it rain!"
    challenge_win: "{winner} defeated {loser} and won {amount} {currency}!"
    flip_win:      "{user} flipped a coin and won {amount} {currency}!"
    free_spin_win: "{user} won {amount} {currency} on a FREE spin! Try yours DAILY!"

# -- Digest -----------------------------------------------------------
# Daily summary messages. The admin digest goes to the bot owner;
# the user digest is a DM to each account with their daily stats.
# send_hour_utc: 0-23 UTC hour when the digest fires.
digest:
  # Admin digest: daily summary sent to channel owner via DM.
  admin_digest:
    enabled: true
    send_hour_utc: 5                   # Send at 05:00 UTC

  # User digest: individual DM to each user with their daily recap.
  user_digest:
    enabled: false                     # Disabled by default (can be chatty)
    send_hour_utc: 4                   # Send at 04:00 UTC
    message: "Daily: earned {earned} | spent {spent} | balance {balance} | rank {rank} | streak {streak}d"

# =====================================================================
#  Sprint 9 -- Polish & Hardening
# =====================================================================

# -- Commands ---------------------------------------------------------
# Bot command rate-limiting. Exceeding rate_limit_per_minute causes
# the bot to silently drop the command until the window resets.
commands:
  rate_limit_per_minute: 10
"""

with open(r"D:\Devel\Kryten-Ecosystem\kryten-economy\config.yaml", "w", encoding="utf-8") as f:
    f.write(content)

print(f"Written successfully — {content.count(chr(10))} lines")
