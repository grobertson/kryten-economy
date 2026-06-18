"""Tests for BlackjackEngine — PM-only card game."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from kryten_economy.blackjack_engine import (
    ActiveBlackjack,
    BJOutcome,
    BlackjackEngine,
    Card,
    Hand,
    _new_deck,
)
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase

from conftest import make_config_dict


CH = "test-channel"


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 5000) -> None:
    await db.get_or_create_account(username, CH)
    await db.credit(username, CH, balance, tx_type="seed", trigger_id="test")
    import asyncio
    from datetime import datetime, timedelta, timezone

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    loop = asyncio.get_running_loop()

    def _update():
        conn = db._get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET first_seen = ? WHERE username = ? AND channel = ?",
                (old_ts, username, CH),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _update)


@pytest_asyncio.fixture
async def database(tmp_path) -> EconomyDatabase:
    db_path = str(tmp_path / "test_bj.db")
    db = EconomyDatabase(db_path, logging.getLogger("test"))
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def bj_engine(database: EconomyDatabase) -> BlackjackEngine:
    cfg_dict = make_config_dict()
    cfg_dict.setdefault("gambling", {})["blackjack"] = {
        "enabled": True,
        "min_wager": 10,
        "max_wager": 2000,
        "cooldown_seconds": 0,
        "daily_limit": 50,
        "timeout_seconds": 120,
        "timeout_warning_seconds": 90,
        "dealer_hits_soft_17": True,
        "blackjack_payout": 1.5,
    }
    config = EconomyConfig(**cfg_dict)
    return BlackjackEngine(config, database, logging.getLogger("test"))


class TestHand:
    def test_basic_value(self) -> None:
        hand = Hand([Card("K", "♠"), Card("7", "♥")])
        assert hand.value == 17
        assert not hand.busted
        assert not hand.is_blackjack

    def test_blackjack(self) -> None:
        hand = Hand([Card("A", "♠"), Card("K", "♥")])
        assert hand.value == 21
        assert hand.is_blackjack
        assert hand.soft

    def test_ace_reduction(self) -> None:
        hand = Hand([Card("A", "♠"), Card("8", "♥"), Card("5", "♦")])
        # 11 + 8 + 5 = 24 → reduce ace → 1 + 8 + 5 = 14
        assert hand.value == 14
        assert not hand.busted

    def test_double_ace(self) -> None:
        hand = Hand([Card("A", "♠"), Card("A", "♥")])
        # 11 + 11 = 22 → reduce one ace → 11 + 1 = 12
        assert hand.value == 12

    def test_bust(self) -> None:
        hand = Hand([Card("K", "♠"), Card("Q", "♥"), Card("5", "♦")])
        assert hand.value == 25
        assert hand.busted

    def test_soft_hand(self) -> None:
        hand = Hand([Card("A", "♠"), Card("6", "♥")])
        assert hand.value == 17
        assert hand.soft

    def test_display(self) -> None:
        hand = Hand([Card("A", "♠"), Card("K", "♥")])
        assert "A♠" in hand.display()
        assert "K♥" in hand.display()

    def test_display_hidden(self) -> None:
        hand = Hand([Card("A", "♠"), Card("K", "♥")])
        display = hand.display(hide_second=True)
        assert "A♠" in display
        assert "[?]" in display
        assert "K♥" not in display


class TestDeck:
    def test_new_deck_size(self) -> None:
        deck = _new_deck()
        assert len(deck) == 52

    def test_deck_has_all_cards(self) -> None:
        deck = _new_deck()
        suits = set()
        ranks = set()
        for card in deck:
            suits.add(card.suit)
            ranks.add(card.rank)
        assert len(suits) == 4
        assert len(ranks) == 13


@pytest.mark.asyncio
class TestDeal:
    async def test_deal_success(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        result = await bj_engine.deal("Alice", CH, 100)
        assert "Blackjack" in result or "hand" in result.lower()
        game = bj_engine.get_game("Alice", CH)
        # Game may be None if natural BJ was dealt
        if game:
            assert len(game.player_hand.cards) == 2
            assert len(game.dealer_hand.cards) == 2

    async def test_deal_disabled(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        bj_engine._config.gambling.blackjack.enabled = False
        result = await bj_engine.deal("Alice", CH, 100)
        assert "disabled" in result

    async def test_deal_insufficient_funds(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice", balance=5)
        result = await bj_engine.deal("Alice", CH, 100)
        assert "Insufficient" in result

    async def test_double_deal_blocked(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await bj_engine.deal("Alice", CH, 100)
        if bj_engine.get_game("Alice", CH):
            result = await bj_engine.deal("Alice", CH, 100)
            assert "already have" in result


@pytest.mark.asyncio
class TestHitStand:
    async def test_hit_draws_card(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        # Rig the deck so we don't bust or get blackjack
        await bj_engine.deal("Alice", CH, 100)
        game = bj_engine.get_game("Alice", CH)
        if not game:
            return  # natural BJ

        initial_count = len(game.player_hand.cards)
        result = await bj_engine.hit("Alice", CH)
        # Check if game is still active or resolved
        game = bj_engine.get_game("Alice", CH)
        if game:
            assert len(game.player_hand.cards) == initial_count + 1

    async def test_stand_resolves(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await bj_engine.deal("Alice", CH, 100)
        if not bj_engine.get_game("Alice", CH):
            return  # natural BJ
        result = await bj_engine.stand("Alice", CH)
        # Game should be resolved
        assert bj_engine.get_game("Alice", CH) is None
        assert "Balance" in result

    async def test_hit_no_active_game(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        result = await bj_engine.hit("Alice", CH)
        assert "No active" in result


@pytest.mark.asyncio
class TestDoubleDown:
    async def test_double_down(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice", balance=10000)
        await bj_engine.deal("Alice", CH, 100)
        game = bj_engine.get_game("Alice", CH)
        if not game:
            return  # natural BJ
        result = await bj_engine.double_down("Alice", CH)
        # Should resolve immediately (one card drawn, then stand)
        assert bj_engine.get_game("Alice", CH) is None
        assert "Balance" in result

    async def test_double_after_hit_blocked(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice", balance=10000)
        await bj_engine.deal("Alice", CH, 100)
        game = bj_engine.get_game("Alice", CH)
        if not game:
            return
        await bj_engine.hit("Alice", CH)
        if not bj_engine.get_game("Alice", CH):
            return  # busted
        result = await bj_engine.double_down("Alice", CH)
        assert "only double down on your initial" in result


@pytest.mark.asyncio
class TestTimeout:
    async def test_timeout_warning(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await bj_engine.deal("Alice", CH, 100)
        game = bj_engine.get_game("Alice", CH)
        if not game:
            return
        # Simulate time passage
        from datetime import datetime, timedelta, timezone
        game.last_action_at = datetime.now(timezone.utc) - timedelta(seconds=95)
        results = await bj_engine.check_timeouts(CH)
        # Should get warning
        warned = [r for r in results if "auto-stand" in r[1].lower()]
        assert len(warned) >= 1 or game.warned

    async def test_timeout_auto_stand(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await bj_engine.deal("Alice", CH, 100)
        game = bj_engine.get_game("Alice", CH)
        if not game:
            return
        from datetime import datetime, timedelta, timezone
        game.last_action_at = datetime.now(timezone.utc) - timedelta(seconds=130)
        results = await bj_engine.check_timeouts(CH)
        # Game should be resolved
        assert bj_engine.get_game("Alice", CH) is None
        assert any("Auto-stand" in r[1] for r in results)


@pytest.mark.asyncio
class TestRateLimits:
    """Regression guards for H1 — blackjack must self-enforce its limits."""

    async def _finish_hand(self, bj_engine: BlackjackEngine) -> None:
        if bj_engine.get_game("Alice", CH):
            await bj_engine.stand("Alice", CH)

    async def test_cooldown_enforced(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        bj_engine._config.gambling.blackjack.cooldown_seconds = 60
        await _seed_account(database, "Alice", balance=10000)
        await bj_engine.deal("Alice", CH, 100)
        await self._finish_hand(bj_engine)
        result = await bj_engine.deal("Alice", CH, 100)
        assert "Cooldown" in result

    async def test_daily_limit_enforced(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        bj_engine._config.gambling.blackjack.cooldown_seconds = 0
        bj_engine._config.gambling.blackjack.daily_limit = 1
        await _seed_account(database, "Alice", balance=10000)
        await bj_engine.deal("Alice", CH, 100)
        await self._finish_hand(bj_engine)
        result = await bj_engine.deal("Alice", CH, 100)
        assert "Daily blackjack limit" in result


@pytest.mark.asyncio
class TestLossMessages:
    """Regression for review #6 — a non-bust loss must not say 'Bust!'."""

    async def test_dealer_wins_not_bust(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        from kryten_economy.blackjack_engine import ActiveBlackjack

        await _seed_account(database, "Alice", balance=10000)
        # Player stands on 18; dealer holds 19 and stands (no draw, no bust).
        game = ActiveBlackjack(
            username="Alice",
            channel=CH,
            wager=100,
            deck=[],
            player_hand=Hand([Card("10", "♠"), Card("8", "♥")]),  # 18
            dealer_hand=Hand([Card("10", "♦"), Card("9", "♣")]),  # 19
        )
        bj_engine._games[("alice", CH)] = game
        result = await bj_engine.stand("Alice", CH)
        assert "Dealer wins" in result
        assert "Bust" not in result

    async def test_player_bust_says_bust(
        self, bj_engine: BlackjackEngine, database: EconomyDatabase,
    ) -> None:
        from kryten_economy.blackjack_engine import ActiveBlackjack

        await _seed_account(database, "Alice", balance=10000)
        game = ActiveBlackjack(
            username="Alice",
            channel=CH,
            wager=100,
            deck=[Card("10", "♠")],  # hit draws this → bust
            player_hand=Hand([Card("10", "♥"), Card("9", "♦")]),  # 19
            dealer_hand=Hand([Card("10", "♦"), Card("7", "♣")]),  # 17
        )
        bj_engine._games[("alice", CH)] = game
        result = await bj_engine.hit("Alice", CH)  # 19 + 10 = 29 → bust
        assert "Bust" in result

