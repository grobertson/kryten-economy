"""Tests for RaceEngine — weighted simulation, betting, traits, events."""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.race_engine import (
    RaceEngine,
    RacePhase,
    RacerTrait,
)

from conftest import make_config_dict


CH = "test-channel"


async def _seed_account(db: EconomyDatabase, username: str, balance: int = 5000) -> None:
    """Create account with sufficient age to bypass minimums."""
    await db.get_or_create_account(username, CH)
    await db.credit(username, CH, balance, tx_type="seed", trigger_id="test")
    # Backdate first_seen so gambling age gate passes
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
    db_path = str(tmp_path / "test_race.db")
    db = EconomyDatabase(db_path, logging.getLogger("test"))
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def race_engine(database: EconomyDatabase) -> RaceEngine:
    cfg_dict = make_config_dict()
    cfg_dict.setdefault("gambling", {})["race"] = {
        "enabled": True,
        "betting_window_seconds": 5,
        "tick_interval_seconds": 0.5,
        "finish_distance": 10.0,
        "min_bet": 10,
        "max_bet": 5000,
        "house_rake_pct": 0.05,
        "odds_mode": "pool",
        "announce_public": True,
        "live_betting": {"enabled": True, "cutoff_pct": 0.75},
        "random_events": {"enabled": False, "chance_per_tick": 0.0},
        "traits": {"enabled": True},
        "commentary": {"mode": "static", "max_lines_per_race": 3},
    }
    config = EconomyConfig(**cfg_dict)
    return RaceEngine(config, database, logging.getLogger("test"))


@pytest.mark.asyncio
class TestRaceStart:
    async def test_start_race(self, race_engine: RaceEngine) -> None:
        result = race_engine.start_race(CH, "Alice")
        assert result.startswith("race_started:")
        race = race_engine.get_active_race(CH)
        assert race is not None
        assert race.phase == RacePhase.BETTING
        assert len(race.racers) == 4

    async def test_start_while_active(self, race_engine: RaceEngine) -> None:
        race_engine.start_race(CH, "Alice")
        result = race_engine.start_race(CH, "Bob")
        assert "already in progress" in result

    async def test_start_disabled(self, race_engine: RaceEngine) -> None:
        race_engine._config.gambling.race.enabled = False
        result = race_engine.start_race(CH, "Alice")
        assert "disabled" in result


@pytest.mark.asyncio
class TestBetting:
    async def test_place_bet(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        race_engine.start_race(CH, "Bob")
        race = race_engine.get_active_race(CH)
        color = list(race.racers.keys())[0]
        result = await race_engine.place_bet("Alice", CH, 100, color)
        assert result.startswith("race_bet:")
        assert len(race.bets) == 1

    async def test_invalid_color(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        race_engine.start_race(CH, "Bob")
        result = await race_engine.place_bet("Alice", CH, 100, "Purple")
        assert "Invalid racer" in result

    async def test_insufficient_funds(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice", balance=5)
        race_engine.start_race(CH, "Bob")
        race = race_engine.get_active_race(CH)
        color = list(race.racers.keys())[0]
        result = await race_engine.place_bet("Alice", CH, 100, color)
        assert "Insufficient" in result

    async def test_double_bet(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        race_engine.start_race(CH, "Bob")
        race = race_engine.get_active_race(CH)
        color = list(race.racers.keys())[0]
        await race_engine.place_bet("Alice", CH, 100, color)
        result = await race_engine.place_bet("Alice", CH, 50, color)
        assert "already placed" in result


@pytest.mark.asyncio
class TestSimulation:
    async def test_tick_advances_racers(self, race_engine: RaceEngine) -> None:
        race_engine.start_race(CH, "Alice")
        race = race_engine.get_active_race(CH)
        race.phase = RacePhase.RACING
        race_engine.tick(CH)
        for racer in race.racers.values():
            # May or may not advance (random), but should not go negative
            assert racer.progress >= 0

    async def test_race_finishes(self, race_engine: RaceEngine) -> None:
        race_engine.start_race(CH, "Alice")
        race = race_engine.get_active_race(CH)
        race.phase = RacePhase.RACING
        # Force a racer near finish
        first = list(race.racers.values())[0]
        first.progress = 9.9
        first.speed_base = 5.0  # Will almost certainly finish
        _, _, finished = race_engine.tick(CH)
        # With speed 5.0, random.random() * 5.0 is 0-5, so 9.9+anything > 10
        assert finished is True


@pytest.mark.asyncio
class TestResolution:
    async def test_resolve_pool_mode(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        await _seed_account(database, "Alice")
        await _seed_account(database, "Bob")
        race_engine.start_race(CH, "Dealer")
        race = race_engine.get_active_race(CH)
        colors = list(race.racers.keys())
        winner_color = colors[0]

        await race_engine.place_bet("Alice", CH, 100, winner_color)
        await race_engine.place_bet("Bob", CH, 200, colors[1])

        race.phase = RacePhase.RACING
        # Force the winner
        race.racers[winner_color].progress = 25.0

        result = await race_engine.resolve_race(CH)
        assert result is not None
        lines, bets, per_user_pm = result
        assert "WINS" in lines[0] or "wins" in lines[0].lower() or "🏆" in lines[0]
        assert "Alice" in per_user_pm
        assert "Bob" in per_user_pm
        assert "✅" in per_user_pm["Alice"]
        assert "❌" in per_user_pm["Bob"]

    async def test_close_betting_no_bets(self, race_engine: RaceEngine) -> None:
        race_engine.start_race(CH, "Alice")
        success = race_engine.close_betting(CH)
        assert success is False
        # Race should be cleaned up
        assert race_engine.get_active_race(CH) is None

    async def test_winners_line_matches_credit(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        """Regression guard for M4 — the displayed net must equal the credited
        payout minus the stake (single source of truth)."""
        import re

        await _seed_account(database, "Alice")
        await _seed_account(database, "Bob")
        race_engine.start_race(CH, "Dealer")
        race = race_engine.get_active_race(CH)
        colors = list(race.racers.keys())
        winner_color, loser_color = colors[0], colors[1]

        await race_engine.place_bet("Alice", CH, 100, winner_color)
        await race_engine.place_bet("Bob", CH, 200, loser_color)

        race.phase = RacePhase.RACING
        race.racers[winner_color].progress = 25.0

        alice_before = (await database.get_account("Alice", CH))["balance"]
        result = await race_engine.resolve_race(CH)
        alice_after = (await database.get_account("Alice", CH))["balance"]

        assert result is not None
        lines, _bets, _pm = result
        credited = alice_after - alice_before

        winners_line = next((line for line in lines if line.startswith("Winners:")), "")
        match = re.search(r"\+([\d,]+)", winners_line)
        assert match is not None, f"no payout in winners line: {winners_line!r}"
        displayed_net = int(match.group(1).replace(",", ""))

        # credited == payout; displayed value is net = payout - stake(100)
        assert credited - 100 == displayed_net



class TestTraits:
    def test_sprinter_fast_start(self) -> None:
        from kryten_economy.race_engine import RacerState

        racer = RacerState("Red", "🔴", 1.0, 0.3, RacerTrait.SPRINTER)
        speed = RaceEngine._apply_trait(racer, 1.0, 0.1)  # early
        assert speed == 1.5

    def test_sprinter_slow_finish(self) -> None:
        from kryten_economy.race_engine import RacerState

        racer = RacerState("Red", "🔴", 1.0, 0.3, RacerTrait.SPRINTER)
        speed = RaceEngine._apply_trait(racer, 1.0, 0.8)  # late
        assert speed == 0.85

    def test_closer_late_surge(self) -> None:
        from kryten_economy.race_engine import RacerState

        racer = RacerState("Green", "🟢", 1.0, 0.2, RacerTrait.CLOSER)
        speed = RaceEngine._apply_trait(racer, 1.0, 0.8)
        assert speed == 1.5

    def test_steady_no_change(self) -> None:
        from kryten_economy.race_engine import RacerState

        racer = RacerState("Blue", "🔵", 1.0, 0.4, RacerTrait.STEADY)
        speed = RaceEngine._apply_trait(racer, 1.0, 0.5)
        assert speed == 1.0


class TestProgressDisplay:
    def test_progress_bar_format(self, race_engine: RaceEngine) -> None:
        race_engine.start_race(CH, "Alice")
        race = race_engine.get_active_race(CH)
        race.phase = RacePhase.RACING
        lines = race_engine._build_progress_display(race)
        assert len(lines) == 4
        for line in lines:
            assert "|" in line
            assert "█" in line or "░" in line


class TestRaceConfigValidation:
    """Regression for review — finish_distance is a divisor; reject <= 0 at load."""

    def test_finish_distance_zero_rejected(self) -> None:
        import pytest as _pytest
        from pydantic import ValidationError

        from kryten_economy.config import RaceConfig

        with _pytest.raises(ValidationError):
            RaceConfig(finish_distance=0)

    def test_finish_distance_negative_rejected(self) -> None:
        import pytest as _pytest
        from pydantic import ValidationError

        from kryten_economy.config import RaceConfig

        with _pytest.raises(ValidationError):
            RaceConfig(finish_distance=-5)


@pytest.mark.asyncio
class TestRaceStartLine:
    """Engine ↔ narrator wiring for the LLM race-start line."""

    async def test_static_default_when_no_story(self, race_engine: RaceEngine) -> None:
        race_engine.start_race(CH, "Alice")
        assert "Betting is closed" in race_engine.get_race_start_line(CH)

    async def test_uses_llm_story_start_when_present(self, race_engine: RaceEngine) -> None:
        from kryten_economy.race_narrator import RaceStory

        race_engine.start_race(CH, "Alice")
        race_engine._narrator._stories[CH] = RaceStory(
            start="🏁 LLM: AND THEY'RE OFF!",
            lead_change="{racer} leads",
            finish="{racer} wins",
        )
        assert race_engine.get_race_start_line(CH) == "🏁 LLM: AND THEY'RE OFF!"

    async def test_resolve_consumes_story(
        self, race_engine: RaceEngine, database: EconomyDatabase,
    ) -> None:
        from kryten_economy.race_narrator import RaceStory

        await _seed_account(database, "Alice")
        race_engine.start_race(CH, "Bob")
        race = race_engine.get_active_race(CH)
        color = list(race.racers.keys())[0]
        await race_engine.place_bet("Alice", CH, 100, color)

        race_engine._narrator._stories[CH] = RaceStory(
            start="s", lead_change="lc", finish="🏆 {racer} finishes!",
        )
        race.phase = RacePhase.RACING
        race.racers[color].progress = 25.0
        await race_engine.resolve_race(CH)
        # Story cleared after resolution
        assert not race_engine._narrator.has_story(CH)


