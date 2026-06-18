"""Async client for the Open Trivia Database (opentdb.com).

Fetches trivia questions with session-token management to avoid repeats
and a local cache to tolerate transient API outages.
"""

from __future__ import annotations

import html
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import aiohttp


OPENTDB_BASE = "https://opentdb.com"


@dataclass
class TriviaQuestion:
    """A single trivia question ready for presentation."""

    category: str
    difficulty: str  # easy, medium, hard
    question: str
    correct_answer: str
    incorrect_answers: list[str]
    all_answers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.all_answers:
            answers = self.incorrect_answers + [self.correct_answer]
            random.shuffle(answers)
            self.all_answers = answers

    @property
    def correct_letter(self) -> str:
        """Return the letter (A/B/C/D) of the correct answer."""
        for i, ans in enumerate(self.all_answers):
            if ans == self.correct_answer:
                return chr(65 + i)  # A=65
        return "?"

    def format_display(self) -> str:
        """Format question + options for chat display."""
        lines = [
            f"🧠 TRIVIA TIME! Category: {self.category} | Difficulty: {self.difficulty.title()}",
            "",
            f"Q: {self.question}",
        ]
        for i, ans in enumerate(self.all_answers):
            letter = chr(65 + i)
            lines.append(f"  {letter}) {ans}")
        return "\n".join(lines)


class TriviaClient:
    """Async client for opentdb.com with caching and session tokens."""

    def __init__(
        self,
        cache_size: int = 20,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cache_size = cache_size
        self._logger = logger or logging.getLogger(__name__)
        self._cache: list[TriviaQuestion] = []
        self._session_token: str | None = None
        self._http: aiohttp.ClientSession | None = None

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    # ── Session token ─────────────────────────────────────────

    async def _ensure_token(self) -> None:
        """Obtain or refresh the session token (avoids repeat questions)."""
        if self._session_token:
            return
        try:
            http = await self._get_http()
            async with http.get(
                f"{OPENTDB_BASE}/api_token.php",
                params={"command": "request"},
            ) as resp:
                data = await resp.json(content_type=None)
                if data.get("response_code") == 0:
                    self._session_token = data["token"]
                    self._logger.info("OpenTDB session token acquired")
                else:
                    self._logger.warning("OpenTDB token request failed: %s", data)
        except Exception:
            self._logger.warning("Failed to acquire OpenTDB token", exc_info=True)

    async def _reset_token(self) -> None:
        """Reset the session token when all questions exhausted."""
        if not self._session_token:
            return
        try:
            http = await self._get_http()
            async with http.get(
                f"{OPENTDB_BASE}/api_token.php",
                params={"command": "reset", "token": self._session_token},
            ) as resp:
                await resp.json(content_type=None)
                self._logger.info("OpenTDB session token reset")
        except Exception:
            self._session_token = None

    # ── Fetch questions ───────────────────────────────────────

    async def _fetch_batch(
        self,
        amount: int = 10,
        category: int | None = None,
        difficulty: str | None = None,
    ) -> list[TriviaQuestion]:
        """Fetch a batch of questions from the API."""
        await self._ensure_token()

        params: dict[str, Any] = {
            "amount": amount,
            "type": "multiple",  # always multiple choice
        }
        if category is not None:
            params["category"] = category
        if difficulty and difficulty != "random":
            params["difficulty"] = difficulty
        if self._session_token:
            params["token"] = self._session_token

        try:
            http = await self._get_http()
            async with http.get(
                f"{OPENTDB_BASE}/api.php",
                params=params,
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception:
            self._logger.warning("OpenTDB API request failed", exc_info=True)
            return []

        code = data.get("response_code", -1)
        if code == 4:
            # Token exhausted — reset and retry once
            await self._reset_token()
            await self._ensure_token()
            params["token"] = self._session_token
            try:
                http = await self._get_http()
                async with http.get(f"{OPENTDB_BASE}/api.php", params=params) as resp:
                    data = await resp.json(content_type=None)
                code = data.get("response_code", -1)
            except Exception:
                return []

        if code != 0:
            self._logger.warning("OpenTDB returned code %d", code)
            return []

        questions: list[TriviaQuestion] = []
        for item in data.get("results", []):
            questions.append(TriviaQuestion(
                category=html.unescape(item["category"]),
                difficulty=item["difficulty"],
                question=html.unescape(item["question"]),
                correct_answer=html.unescape(item["correct_answer"]),
                incorrect_answers=[html.unescape(a) for a in item["incorrect_answers"]],
            ))

        return questions

    # ── Public API ────────────────────────────────────────────

    async def get_question(
        self,
        category: int | None = None,
        difficulty: str | None = None,
    ) -> TriviaQuestion | None:
        """Get one question, using cache when available.

        Returns None if the API is unavailable and cache is empty.
        """
        # Try cache first
        if self._cache:
            # If difficulty filter requested, try to match
            if difficulty and difficulty != "random":
                for i, q in enumerate(self._cache):
                    if q.difficulty == difficulty:
                        return self._cache.pop(i)
            return self._cache.pop(0)

        # Fetch a batch
        batch = await self._fetch_batch(
            amount=self._cache_size,
            category=category,
            difficulty=difficulty,
        )
        if not batch:
            return None

        # Take one, cache the rest
        question = batch[0]
        self._cache.extend(batch[1:])
        return question

    async def prefetch(
        self,
        category: int | None = None,
        difficulty: str | None = None,
    ) -> int:
        """Pre-fill the cache. Returns number of questions cached."""
        if len(self._cache) >= self._cache_size:
            return len(self._cache)

        needed = self._cache_size - len(self._cache)
        batch = await self._fetch_batch(
            amount=min(needed, 50),
            category=category,
            difficulty=difficulty,
        )
        self._cache.extend(batch)
        self._logger.info("Trivia cache: %d questions", len(self._cache))
        return len(self._cache)
