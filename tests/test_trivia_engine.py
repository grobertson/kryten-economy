"""Tests for TriviaEngine — spectacle game with wagered Q&A."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.trivia_client import TriviaQuestion
from kryten_economy.trivia_engine import TriviaEngine

from conftest import make_config_dict


CH = "test-channel"

SAMPLE_QUESTION = TriviaQuestion(
    category="Science",
    difficulty="medium",
    question="What is the chemical symbol for gold?",
    correct_answer="Au",
    incorrect_answers=["Ag", "Fe", "Cu"],
    all_answers=["Au", "Ag", "Fe", "Cu"],
)


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 5000) -> None:
    """Create account with sufficient age to bypass minimums."""
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
    db_path = str(tmp_path / "test_trivia.db")
    db = EconomyDatabase(db_path, logging.getLogger("test"))
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def trivia_engine(database: EconomyDatabase) -> TriviaEngine:
    cfg_dict = make_config_dict()
    cfg_dict.setdefault("gambling", {})["trivia"] = {
        "enabled": True,
        "min_wager": 10,
        "max_wager": 1000,
        "answer_window_seconds": 30,
        "betting_window_seconds": 15,
        "difficulty": "random",
        "payout_multipliers": {"easy": 1.5, "medium": 2.0, "hard": 3.0},
        "question_cache_size": 5,
        "announce_public": True,
    }
    config = EconomyConfig(**cfg_dict)
    engine = TriviaEngine(config, database, logging.getLogger("test"))
    # Pre-load cache with our test question
    engine._client._cache = [SAMPLE_QUESTION]
    return engine


@pytest.mark.asyncio
class TestTriviaStart:
    async def test_start_trivia(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        result = await trivia_engine.start_trivia(CH, "Alice", 100)
        assert result.startswith("trivia_started:")
        active = trivia_engine.get_active_trivia(CH)
        assert active is not None
        assert active.question.correct_answer == "Au"
        assert "Alice" in active.wagers

    async def test_start_disabled(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        trivia_engine._config.gambling.trivia.enabled = False
        result = await trivia_engine.start_trivia(CH, "Alice", 100)
        assert "disabled" in result

    async def test_start_while_active(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        # Add another question to cache for second attempt
        trivia_engine._client._cache.append(SAMPLE_QUESTION)
        await trivia_engine.start_trivia(CH, "Alice", 100)
        result = await trivia_engine.start_trivia(CH, "Bob", 50)
        assert "already in progress" in result


@pytest.mark.asyncio
class TestTriviaAnswers:
    async def test_submit_correct_letter(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        result = trivia_engine.submit_answer("Alice", CH, "A")
        assert result is not None
        assert result.startswith("trivia_answer:")

    async def test_submit_full_text(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        result = trivia_engine.submit_answer("Alice", CH, "Au")
        assert result is not None

    async def test_cannot_answer_without_bet(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        result = trivia_engine.submit_answer("Bob", CH, "A")
        assert result is None

    async def test_first_answer_only(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        trivia_engine.submit_answer("Alice", CH, "B")
        result = trivia_engine.submit_answer("Alice", CH, "A")
        assert result is None  # Second answer rejected


@pytest.mark.asyncio
class TestTriviaResolve:
    async def test_correct_answer_wins(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        trivia_engine.submit_answer("Alice", CH, "A")  # Au is correct, at index 0
        result = await trivia_engine.resolve_trivia(CH)
        assert result is not None
        lines, per_user_pm = result
        assert "Au" in lines[0]
        assert "✅" in per_user_pm["Alice"]

    async def test_wrong_answer_loses(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        trivia_engine.submit_answer("Alice", CH, "C")  # Fe is wrong
        result = await trivia_engine.resolve_trivia(CH)
        assert result is not None
        lines, per_user_pm = result
        assert "❌" in per_user_pm["Alice"]

    async def test_no_answer_loses(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)
        # Don't submit any answer
        result = await trivia_engine.resolve_trivia(CH)
        assert result is not None
        _, per_user_pm = result
        assert "❌" in per_user_pm["Alice"]
        assert "no answer" in per_user_pm["Alice"]


@pytest.mark.asyncio
class TestTriviaJoinGating:
    """Regression guard for H2 — joining must enforce the account-age gate."""

    async def test_join_rejects_new_account(
        self, trivia_engine: TriviaEngine, database: EconomyDatabase,
    ) -> None:
        trivia_engine._config.gambling.min_account_age_minutes = 60
        # Aged initiator can start
        await _seed_account(database, "Alice")
        await trivia_engine.start_trivia(CH, "Alice", 100)

        # Brand-new account (first_seen = now) tries to join
        await database.get_or_create_account("Newbie", CH)
        await database.credit("Newbie", CH, 5000, tx_type="seed", trigger_id="test")
        result = await trivia_engine.place_bet("Newbie", CH, 50)
        assert "minutes before gambling" in result

