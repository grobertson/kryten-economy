"""Tests for TriviaClient — Open Trivia DB client with caching."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from kryten_economy.trivia_client import TriviaClient, TriviaQuestion


@pytest.fixture
def client() -> TriviaClient:
    return TriviaClient(cache_size=5, logger=logging.getLogger("test"))


class TestTriviaQuestion:
    def test_all_answers_shuffled(self) -> None:
        q = TriviaQuestion(
            category="Science",
            difficulty="easy",
            question="What is H2O?",
            correct_answer="Water",
            incorrect_answers=["Fire", "Air", "Earth"],
        )
        assert len(q.all_answers) == 4
        assert "Water" in q.all_answers
        assert "Fire" in q.all_answers

    def test_correct_letter(self) -> None:
        q = TriviaQuestion(
            category="Science",
            difficulty="easy",
            question="What is H2O?",
            correct_answer="Water",
            incorrect_answers=["Fire", "Air", "Earth"],
            all_answers=["Fire", "Water", "Air", "Earth"],
        )
        assert q.correct_letter == "B"

    def test_format_display(self) -> None:
        q = TriviaQuestion(
            category="Science",
            difficulty="medium",
            question="What is H2O?",
            correct_answer="Water",
            incorrect_answers=["Fire", "Air", "Earth"],
            all_answers=["Fire", "Water", "Air", "Earth"],
        )
        display = q.format_display()
        assert "TRIVIA TIME" in display
        assert "Science" in display
        assert "Medium" in display
        assert "A)" in display
        assert "B)" in display


@pytest.mark.asyncio
class TestTriviaClientCache:
    async def test_get_from_cache(self, client: TriviaClient) -> None:
        q = TriviaQuestion(
            category="Test",
            difficulty="easy",
            question="Test?",
            correct_answer="Yes",
            incorrect_answers=["No", "Maybe", "Never"],
        )
        client._cache = [q]
        result = await client.get_question()
        assert result is not None
        assert result.question == "Test?"
        assert len(client._cache) == 0  # consumed

    async def test_get_from_cache_with_difficulty(self, client: TriviaClient) -> None:
        easy = TriviaQuestion("C", "easy", "E?", "Y", ["N", "M", "X"])
        hard = TriviaQuestion("C", "hard", "H?", "Y", ["N", "M", "X"])
        client._cache = [easy, hard]
        result = await client.get_question(difficulty="hard")
        assert result is not None
        assert result.difficulty == "hard"
        assert len(client._cache) == 1

    async def test_get_empty_cache_no_api(self, client: TriviaClient) -> None:
        # Mock the HTTP call to fail
        with patch.object(client, "_fetch_batch", return_value=[]):
            result = await client.get_question()
            assert result is None
