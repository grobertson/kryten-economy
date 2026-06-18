"""Tests for RaceNarrator — static / LLM / hybrid race commentary."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from kryten_economy.config import RaceCommentaryConfig, RaceLLMConfig
from kryten_economy.race_narrator import RaceNarrator, RaceStory


CH = "test-channel"


def _narrator(mode: str = "static", **llm_overrides) -> RaceNarrator:
    cfg = RaceCommentaryConfig(
        mode=mode,
        max_lines_per_race=3,
        llm=RaceLLMConfig(**llm_overrides),
    )
    return RaceNarrator(cfg, logging.getLogger("test"))


SAMPLE_STORY = RaceStory(
    start="🏁 The themed race begins!",
    lead_change="{emoji} {racer} surges ahead in style!",
    event="{emoji} {racer} pulls a slick move!",
    finish="🏆 {emoji} {racer} takes the themed crown!",
)


class TestStaticMode:
    def test_start_line_is_static(self) -> None:
        n = _narrator("static")
        line = n.get_start_line(CH)
        assert isinstance(line, str) and line

    def test_budget_caps_capped_lines(self) -> None:
        n = _narrator("static")
        n.reset_for_race(CH, "race-1")
        emitted = []
        for _ in range(10):
            line = n.get_lead_change_line(CH, "Red", "🔴")
            if line is not None:
                emitted.append(line)
        # max_lines_per_race == 3
        assert len(emitted) == 3

    def test_finish_line_not_budget_capped(self) -> None:
        n = _narrator("static")
        n.reset_for_race(CH, "race-1")
        # Exhaust the budget
        for _ in range(5):
            n.get_lead_change_line(CH, "Red", "🔴")
        # Finish line still emits
        line = n.get_finish_line(CH, "Blue", "🔵")
        assert "Blue" in line


@pytest.mark.asyncio
class TestPrepareStory:
    async def test_static_mode_is_noop(self) -> None:
        n = _narrator("static")
        await n.prepare_story(CH, "race-1")
        assert not n.has_story(CH)

    async def test_llm_mode_caches_story(self) -> None:
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=SAMPLE_STORY),
        ):
            await n.prepare_story(CH, "race-1")
        assert n.has_story(CH)
        assert n.get_story_start(CH) == "🏁 The themed race begins!"

    async def test_getters_use_cached_story(self) -> None:
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=SAMPLE_STORY),
        ):
            await n.prepare_story(CH, "race-1")
        n.reset_for_race(CH, "race-2")
        # reset clears the story too — re-prepare
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=SAMPLE_STORY),
        ):
            await n.prepare_story(CH, "race-2")

        assert n.get_start_line(CH) == "🏁 The themed race begins!"
        lead = n.get_lead_change_line(CH, "Red", "🔴")
        assert lead == "🔴 Red surges ahead in style!"
        finish = n.get_finish_line(CH, "Blue", "🔵")
        assert finish == "🏆 🔵 Blue takes the themed crown!"

    async def test_hybrid_falls_back_to_static(self) -> None:
        n = _narrator("hybrid")
        n.reset_for_race(CH, "race-1")
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=None),
        ):
            await n.prepare_story(CH, "race-1")
        assert not n.has_story(CH)
        # Static getters still work
        line = n.get_finish_line(CH, "Green", "🟢")
        assert "Green" in line

    async def test_consume_story_clears_state(self) -> None:
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=SAMPLE_STORY),
        ):
            await n.prepare_story(CH, "race-1")
        assert n.has_story(CH)
        n.consume_story(CH)
        assert not n.has_story(CH)

    async def test_per_channel_isolation(self) -> None:
        n = _narrator("llm")
        n.reset_for_race("chan-a", "race-a")
        story_a = RaceStory(
            start="A start", lead_change="A {racer}", event="A ev", finish="A fin {racer}",
        )
        with patch.object(n, "_generate_llm_story", AsyncMock(return_value=story_a)):
            await n.prepare_story("chan-a", "race-a")
        # chan-b has no story
        assert n.has_story("chan-a")
        assert not n.has_story("chan-b")
        assert n.get_story_start("chan-a") == "A start"
        assert n.get_story_start("chan-b") is None

    async def test_safe_format_tolerates_bad_placeholders(self) -> None:
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        bad = RaceStory(
            start="ok",
            lead_change="{winner} took it!",  # unknown placeholder → KeyError
            event="ev",
            finish="done {oops}",  # unknown placeholder → KeyError
        )
        with patch.object(n, "_generate_llm_story", AsyncMock(return_value=bad)):
            await n.prepare_story(CH, "race-1")
        # Should not raise; returns the raw template on KeyError
        assert n.get_lead_change_line(CH, "Red", "🔴") == "{winner} took it!"
        assert n.get_finish_line(CH, "Red", "🔴") == "done {oops}"

    async def test_safe_format_tolerates_malformed_template(self) -> None:
        """A malformed template (unmatched brace) raises ValueError → raw text."""
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        bad = RaceStory(
            start="ok",
            lead_change="{emoji} {racer} leads { unbalanced",  # ValueError
            event="ev",
            finish="winner is {racer} }",  # ValueError
        )
        with patch.object(n, "_generate_llm_story", AsyncMock(return_value=bad)):
            await n.prepare_story(CH, "race-1")
        # Must not raise; falls back to the raw template text.
        assert n.get_lead_change_line(CH, "Red", "🔴") == "{emoji} {racer} leads { unbalanced"
        assert n.get_finish_line(CH, "Red", "🔴") == "winner is {racer} }"

    async def test_stale_prep_for_replaced_race_is_discarded(self) -> None:
        """A slow prep that completes after a new race started is dropped."""
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        # A new race begins in the same channel before race-1's prep finishes.
        n.reset_for_race(CH, "race-2")
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=SAMPLE_STORY),
        ):
            await n.prepare_story(CH, "race-1")
        # race-1's late story must not clobber the active race-2.
        assert not n.has_story(CH)

    async def test_prep_after_resolve_is_discarded(self) -> None:
        """A prep that finishes after its race resolved is dropped."""
        n = _narrator("llm")
        n.reset_for_race(CH, "race-1")
        n.consume_story(CH)  # race resolved before prep returned
        with patch.object(
            n, "_generate_llm_story", AsyncMock(return_value=SAMPLE_STORY),
        ):
            await n.prepare_story(CH, "race-1")
        assert not n.has_story(CH)


@pytest.mark.asyncio
class TestLLMGeneration:
    async def test_no_endpoint_returns_none(self) -> None:
        n = _narrator("llm", endpoint="")
        story = await n._generate_llm_story()
        assert story is None

    async def test_bad_endpoint_returns_none(self) -> None:
        # Unreachable port — fast fail with no retries
        n = _narrator(
            "llm",
            endpoint="http://localhost:9/v1/chat/completions",
            timeout_seconds=1,
            max_retries=0,
        )
        story = await n._generate_llm_story()
        assert story is None

    async def test_incomplete_story_rejected(self) -> None:
        """A response missing the core fields yields None (→ static fallback)."""
        n = _narrator("llm")

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {
                    "choices": [
                        {"message": {"content": '{"start": "only a start"}'}},
                    ],
                }

            async def text(self):
                return ""

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        with patch("aiohttp.ClientSession", _Session):
            story = await n._generate_llm_story()
        assert story is None

    async def test_valid_story_parsed(self) -> None:
        n = _narrator("llm")

        payload = (
            '{"start": "Go!", "lead_change": "{racer} leads", '
            '"event": "{racer} boosts", "finish": "{racer} wins"}'
        )

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"choices": [{"message": {"content": payload}}]}

            async def text(self):
                return ""

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        with patch("aiohttp.ClientSession", _Session):
            story = await n._generate_llm_story()
        assert story is not None
        assert story.start == "Go!"
        assert story.lead_change == "{racer} leads"
        assert story.finish == "{racer} wins"
