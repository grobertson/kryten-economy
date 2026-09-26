"""Initial economy schema for PostgreSQL.

Translated from the SQLite DDL in ``kryten_economy/database.py::_create_tables``
(the historical source of truth) per SPEC-Sortie-3 §4.1 type mapping:

- ``INTEGER PRIMARY KEY AUTOINCREMENT`` -> ``bigint GENERATED ALWAYS AS IDENTITY``
- integer boolean flags (``... DEFAULT 0``)  -> ``boolean DEFAULT false``
- integer counters/amounts -> ``bigint`` (balances/amounts) or ``integer``
- ``TIMESTAMP DEFAULT CURRENT_TIMESTAMP``   -> ``timestamptz DEFAULT now()``
- ``TEXT`` -> ``text`` (``metadata`` stays ``text``: callers pass JSON *strings*)
- ``date TEXT`` (daily_activity/streaks/etc.) -> ``date``

Boundary note (SORTIE-3 boundary policy): the *storage* types above are
PostgreSQL-native, but ``EconomyDatabasePg`` normalises values on the way out so
the store edge keeps SQLite-compatible Python types — timestamps as ISO-8601
strings and boolean flags as ints. That normalisation is deliberately NOT
encoded here, because it is a runtime concern of the data layer, not the schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Sprint 1: Core tables ───────────────────────────────────────
    op.create_table(
        "accounts",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("balance", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("lifetime_earned", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("lifetime_spent", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column(
            "lifetime_gambled_in", sa.BigInteger(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column(
            "lifetime_gambled_out", sa.BigInteger(), server_default=sa.text("0"), nullable=True
        ),
        sa.Column("rank_name", sa.Text(), server_default=sa.text("'Extra'"), nullable=True),
        sa.Column("cytube_level", sa.Integer(), server_default=sa.text("1"), nullable=True),
        sa.Column("chat_color", sa.Text(), nullable=True),
        sa.Column("custom_greeting", sa.Text(), nullable=True),
        sa.Column("custom_title", sa.Text(), nullable=True),
        sa.Column("channel_gif_url", sa.Text(), nullable=True),
        sa.Column(
            "channel_gif_approved", sa.Boolean(), server_default=sa.text("false"), nullable=True
        ),
        sa.Column("personal_currency_name", sa.Text(), nullable=True),
        sa.Column(
            "welcome_wallet_claimed", sa.Boolean(), server_default=sa.text("false"), nullable=True
        ),
        sa.Column("economy_banned", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("quiet_mode", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("last_active", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel"),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("trigger_id", sa.Text(), nullable=True),
        sa.Column("related_user", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_transactions_username_channel", "transactions", ["username", "channel"])
    op.create_index("idx_transactions_created_at", "transactions", ["created_at"])
    op.create_index("idx_transactions_type", "transactions", ["type"])

    op.create_table(
        "daily_activity",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("minutes_present", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("minutes_active", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("messages_sent", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("long_messages", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("gifs_posted", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("unique_emotes_used", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("kudos_given", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("kudos_received", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("laughs_received", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("bot_interactions", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("z_earned", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("z_spent", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("z_gambled_in", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("z_gambled_out", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column(
            "first_message_claimed", sa.Boolean(), server_default=sa.text("false"), nullable=True
        ),
        sa.Column("free_spin_used", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("queues_used", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel", "date"),
    )
    op.create_index("idx_daily_activity_date", "daily_activity", ["date"])

    # ── Sprint 2: Streaks & milestones ─────────────────────────────
    op.create_table(
        "streaks",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("current_daily_streak", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("longest_daily_streak", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("last_streak_date", sa.Date(), nullable=True),
        sa.Column(
            "weekend_seen_this_week", sa.Boolean(), server_default=sa.text("false"), nullable=True
        ),
        sa.Column(
            "weekday_seen_this_week", sa.Boolean(), server_default=sa.text("false"), nullable=True
        ),
        sa.Column(
            "bridge_claimed_this_week", sa.Boolean(), server_default=sa.text("false"), nullable=True
        ),
        sa.Column("week_number", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel"),
    )

    op.create_table(
        "hourly_milestones",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("hours_1", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("hours_3", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("hours_6", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("hours_12", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("hours_24", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel", "date"),
    )

    op.create_table(
        "trigger_cooldowns",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("trigger_id", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel", "trigger_id"),
    )

    # ── Sprint 3: Trigger analytics ─────────────────────────────────
    op.create_table(
        "trigger_analytics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("trigger_id", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("unique_users", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_z_awarded", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "trigger_id", "date"),
    )

    # ── Sprint 4: Gambling tables ───────────────────────────────────
    op.create_table(
        "gambling_stats",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("total_spins", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_flips", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_challenges", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_heists", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_races", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_trivias", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_blackjacks", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("biggest_win", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("biggest_loss", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("net_gambling", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel"),
    )

    op.create_table(
        "pending_challenges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("challenger", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("wager", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "race_results",
        sa.Column("race_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("winner_color", sa.Text(), nullable=False),
        sa.Column("total_pool", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("participants", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("race_id"),
    )

    op.create_table(
        "race_bets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("race_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("payout", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("phase", sa.Text(), server_default=sa.text("'pre'"), nullable=True),
        sa.Column(
            "placed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(["race_id"], ["race_results.race_id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "trivia_stats",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("correct", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("incorrect", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("streak", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("best_streak", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_wagered", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_won", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel"),
    )

    op.create_table(
        "blackjack_stats",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("games_played", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("wins", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("losses", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("pushes", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("blackjacks", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_wagered", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("total_won", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel"),
    )

    # ── Sprint 5: Spending tables ───────────────────────────────────
    op.create_table(
        "tip_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("receiver", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_tip_sender", "tip_history", ["sender", "channel"])
    op.create_index("idx_tip_receiver", "tip_history", ["receiver", "channel"])
    op.create_index("idx_tip_date", "tip_history", ["created_at"])

    op.create_table(
        "pending_approvals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("cost", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_approval_status", "pending_approvals", ["status", "channel"])
    op.create_index("idx_approval_user", "pending_approvals", ["username", "channel"])

    op.create_table(
        "vanity_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=True),
        sa.Column(
            "purchased_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", "channel", "item_type"),
    )
    op.create_index("idx_vanity_user", "vanity_items", ["username", "channel"])

    # ── Sprint 6: Achievements ─────────────────────────────────────
    op.create_table(
        "achievements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("achievement_id", sa.Text(), nullable=False),
        sa.Column(
            "awarded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", "channel", "achievement_id"),
    )
    op.create_index("idx_achievements_user", "achievements", ["username", "channel"])
    op.create_index("idx_achievements_id", "achievements", ["achievement_id", "channel"])

    # ── Sprint 7: Bounties ──────────────────────────────────────────
    op.create_table(
        "bounties",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("creator", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=True),
        sa.Column("winner", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_bounties_status", "bounties", ["channel", "status"])
    op.create_index("idx_bounties_creator", "bounties", ["creator", "channel"])

    # ── Sprint 8: Snapshots & Bans ──────────────────────────────────
    op.create_table(
        "economy_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column(
            "snapshot_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("total_accounts", sa.Integer(), nullable=True),
        sa.Column("total_z_circulation", sa.BigInteger(), nullable=True),
        sa.Column("active_economy_users_today", sa.Integer(), nullable=True),
        sa.Column("z_earned_today", sa.BigInteger(), nullable=True),
        sa.Column("z_spent_today", sa.BigInteger(), nullable=True),
        sa.Column("z_gambled_net_today", sa.BigInteger(), nullable=True),
        sa.Column("median_balance", sa.BigInteger(), nullable=True),
        sa.Column("participation_rate", sa.Float(), nullable=True),
        sa.Column("inflation_multiplier", sa.Float(), server_default=sa.text("1.0"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_snapshots_channel", "economy_snapshots", ["channel", "snapshot_time"])

    op.create_table(
        "banned_users",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("banned_by", sa.Text(), nullable=False),
        sa.Column(
            "banned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("username", "channel"),
    )

    # ── Sprint 5: Queue spend requests (idempotency) ───────────────
    op.create_table(
        "queue_spend_requests",
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("cost_z", sa.BigInteger(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("transaction_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("refunded", sa.Boolean(), server_default=sa.text("false"), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("idx_qsr_username_channel", "queue_spend_requests", ["username", "channel"])

    # ── Service metrics (lifetime counters) ─────────────────────────
    op.create_table(
        "service_metrics",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    for table in (
        "service_metrics",
        "queue_spend_requests",
        "banned_users",
        "economy_snapshots",
        "bounties",
        "achievements",
        "vanity_items",
        "pending_approvals",
        "tip_history",
        "blackjack_stats",
        "trivia_stats",
        "race_bets",
        "race_results",
        "pending_challenges",
        "gambling_stats",
        "trigger_analytics",
        "trigger_cooldowns",
        "hourly_milestones",
        "streaks",
        "daily_activity",
        "transactions",
        "accounts",
    ):
        op.drop_table(table)
