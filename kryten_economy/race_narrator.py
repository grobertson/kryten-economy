"""Race narrator — static / LLM / hybrid commentary for races.

Supports three modes controlled by ``RaceCommentaryConfig.mode``:

- **static** (default) — picks from the built-in pools in ``race_narratives``
  plus any ``custom_*`` lines from config.
- **llm** — calls an OpenAI-compatible chat-completions endpoint once per race
  to generate a themed set of commentary lines.
- **hybrid** — tries LLM first; falls back to static on timeout/error.

LLM stories are generated and cached *per channel* (so concurrent races in
different channels never clobber each other) and consumed when the race
resolves. The static pools always remain available as a fallback.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import race_narratives

if TYPE_CHECKING:
    from .config import RaceCommentaryConfig


@dataclass
class RaceStory:
    """A themed set of commentary lines from a single LLM call.

    Empty fields fall back to the static pools. ``lead_change``/``event``/
    ``finish`` are templates formatted with ``{racer}``/``{emoji}`` each time
    they are used; ``start`` is emitted verbatim when the race begins.
    """

    start: str = ""
    lead_change: str = ""
    event: str = ""
    finish: str = ""


class RaceNarrator:
    """Generate commentary lines for race events (static / LLM / hybrid)."""

    def __init__(
        self,
        config: RaceCommentaryConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._log = logger or logging.getLogger(__name__)
        self._build_pools(config)
        # Per-channel narration state (LLM story + commentary budget) so
        # concurrent races in different channels stay independent.
        self._stories: dict[str, RaceStory] = {}
        self._counts: dict[str, int] = {}
        # channel → race_id that the current story/budget belongs to. Used to
        # discard a slow background LLM prep that finishes after its race was
        # cancelled/resolved (or replaced by a new race in the same channel).
        self._race_ids: dict[str, str] = {}

    def _build_pools(self, config: RaceCommentaryConfig) -> None:
        """(Re)build all static narrative pools from built-in + custom lines."""
        self._cfg = config

        # Merge built-in + custom pools
        self._start_lines = list(race_narratives.START_LINES) + list(config.custom_start_lines)
        self._finish_lines = list(race_narratives.FINISH_LINES) + list(config.custom_finish_lines)
        self._event_lines = dict(race_narratives.EVENT_LINES)
        # custom event lines get appended to the "speed_boost" bucket
        if config.custom_event_lines:
            self._event_lines.setdefault("speed_boost", ())
            self._event_lines["speed_boost"] = (
                tuple(self._event_lines["speed_boost"]) + tuple(config.custom_event_lines)
            )

        self._lead_change_lines = list(race_narratives.LEAD_CHANGE_LINES)
        self._close_finish_lines = list(race_narratives.CLOSE_FINISH_LINES)
        self._payout_lines = list(race_narratives.PAYOUT_LINES)

    @property
    def max_lines(self) -> int:
        return self._cfg.max_lines_per_race

    def update_config(self, new_config: RaceCommentaryConfig) -> None:
        self._build_pools(new_config)

    # ── Per-race lifecycle ────────────────────────────────────

    def reset_for_race(self, channel: str, race_id: str) -> None:
        """Reset per-race state for a channel and bind it to ``race_id``.

        ``race_id`` ties any later background LLM story to this specific race,
        so a slow prep task from a previous race can't overwrite the story of a
        race that has since started in the same channel.
        """
        self._counts[channel] = 0
        self._stories.pop(channel, None)
        self._race_ids[channel] = race_id

    def consume_story(self, channel: str) -> None:
        """Clear per-race narration state once a race resolves."""
        self._stories.pop(channel, None)
        self._counts.pop(channel, None)
        self._race_ids.pop(channel, None)

    def has_story(self, channel: str) -> bool:
        return channel in self._stories

    def get_story_start(self, channel: str) -> str | None:
        """Return the LLM story's start line for a channel, if one exists."""
        story = self._stories.get(channel)
        return story.start if (story and story.start) else None

    # ── LLM generation ────────────────────────────────────────

    async def prepare_story(self, channel: str, race_id: str) -> None:
        """Pre-generate an LLM story for a channel if the mode requires it.

        A no-op for static mode. Safe to run as a background task during the
        betting window so the themed story is ready before the first
        commentary line. On failure (or ``mode='hybrid'`` timeout) nothing is
        cached and the getters transparently fall back to the static pools.

        ``race_id`` binds the result to a specific race: if the race was
        cancelled/resolved — or replaced by a new race in the same channel —
        while the (possibly slow) LLM call was in flight, the story is
        discarded instead of clobbering the current race's commentary.
        """
        mode = self._cfg.mode.lower()
        if mode not in ("llm", "hybrid"):
            return
        story = await self._generate_llm_story()
        # The race may have ended (or a new one started in this channel) while
        # we awaited the LLM. Only commit if the story still belongs to the
        # race that is currently active here.
        if self._race_ids.get(channel) != race_id:
            self._log.debug(
                "Discarding stale LLM race story for %s (race %s no longer active)",
                channel, race_id,
            )
            return
        if story is not None:
            self._stories[channel] = story
        elif mode == "llm":
            self._log.warning(
                "LLM race narrator failed with mode='llm'; using static fallback",
            )

    async def _generate_llm_story(self) -> RaceStory | None:
        """Call an OpenAI-compatible endpoint to generate race commentary.

        Returns ``None`` on any failure so callers can fall back to static.
        """
        llm_cfg = self._cfg.llm
        if not llm_cfg.endpoint:
            return None

        try:
            import aiohttp
        except ImportError:
            self._log.warning("aiohttp not installed — cannot use LLM narrator")
            return None

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if llm_cfg.api_key:
            headers["Authorization"] = f"Bearer {llm_cfg.api_key}"

        payload = {
            "model": llm_cfg.model,
            "messages": [
                {"role": "system", "content": llm_cfg.system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Generate fresh commentary for one race. Keep each line "
                        "under 200 characters. Use emoji freely. Return ONLY valid "
                        "JSON with keys: start, lead_change, event, finish."
                    ),
                },
            ],
            "temperature": llm_cfg.temperature,
            "max_tokens": llm_cfg.max_tokens,
        }

        retries = llm_cfg.max_retries + 1
        for attempt in range(retries):
            try:
                timeout = aiohttp.ClientTimeout(total=llm_cfg.timeout_seconds)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        llm_cfg.endpoint,
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            self._log.warning(
                                "LLM race narrator HTTP %s (attempt %d): %s",
                                resp.status, attempt + 1, body[:200],
                            )
                            continue
                        data = await resp.json()
            except Exception as exc:
                self._log.warning(
                    "LLM race narrator error (attempt %d): %s", attempt + 1, exc,
                )
                continue

            # Parse the response
            try:
                content = data["choices"][0]["message"]["content"].strip()
                # Strip markdown code fences if present
                if content.startswith("```"):
                    content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

                story_data = json.loads(content)
                story = RaceStory(
                    start=story_data.get("start", ""),
                    lead_change=story_data.get("lead_change", ""),
                    event=story_data.get("event", ""),
                    finish=story_data.get("finish", ""),
                )
                # Validate we got the core commentary
                if story.lead_change and story.finish:
                    self._log.debug("LLM race narrator generated story successfully")
                    return story
                self._log.warning("LLM race narrator returned incomplete story")
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                self._log.warning(
                    "LLM race narrator parse error (attempt %d): %s", attempt + 1, exc,
                )
                continue

        return None

    # ── Commentary budget ─────────────────────────────────────

    def _can_emit(self, channel: str) -> bool:
        """Whether more capped commentary may be emitted this race."""
        return self._counts.get(channel, 0) < self._cfg.max_lines_per_race

    def _spend(self, channel: str) -> None:
        self._counts[channel] = self._counts.get(channel, 0) + 1

    @staticmethod
    def _safe_format(template: str, **kwargs: str) -> str:
        """Format a (possibly LLM-authored) template, tolerating bad input.

        Unknown placeholders raise ``KeyError``/``IndexError``; a malformed
        template (e.g. an unmatched ``{``) raises ``ValueError``. All three are
        caught so an LLM-authored line can always fall back to its raw text.
        """
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template

    # ── Line getters ──────────────────────────────────────────

    def get_start_line(self, channel: str) -> str:
        story = self._stories.get(channel)
        if story and story.start:
            return story.start
        return random.choice(self._start_lines)

    def get_lead_change_line(self, channel: str, racer: str, emoji: str) -> str | None:
        if not self._can_emit(channel):
            return None
        self._spend(channel)
        story = self._stories.get(channel)
        if story and story.lead_change:
            return self._safe_format(story.lead_change, racer=racer, emoji=emoji)
        return random.choice(self._lead_change_lines).format(racer=racer, emoji=emoji)

    def get_event_line(self, channel: str, event_type: str, racer: str, emoji: str) -> str | None:
        if not self._can_emit(channel):
            return None
        story = self._stories.get(channel)
        if story and story.event:
            self._spend(channel)
            return self._safe_format(story.event, racer=racer, emoji=emoji)
        pool = self._event_lines.get(event_type, self._event_lines.get("speed_boost", ()))
        if not pool:
            return None
        self._spend(channel)
        return random.choice(pool).format(racer=racer, emoji=emoji)

    def get_close_finish_line(self, channel: str) -> str | None:
        if not self._can_emit(channel):
            return None
        self._spend(channel)
        return random.choice(self._close_finish_lines)

    def get_finish_line(self, channel: str, racer: str, emoji: str) -> str:
        story = self._stories.get(channel)
        if story and story.finish:
            return self._safe_format(story.finish, racer=racer, emoji=emoji)
        return random.choice(self._finish_lines).format(racer=racer, emoji=emoji)

    def get_payout_line(self, user: str, payout: str, symbol: str) -> str:
        line = random.choice(self._payout_lines)
        return line.format(user=user, payout=payout, symbol=symbol)
