"""Prove the ordering fix by asserting call order, not by relying on FK enforcement.

SQLite (the backend the test suite runs against) does not enforce foreign keys,
so a pure-FK regression test cannot detect the bug. This test instead records
the order of DB writes during resolve_race and asserts the parent row
(race_results) is written before any child row (race_bets).
"""

from __future__ import annotations

import asyncio
import logging

import pytest
import pytest_asyncio

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.race_engine import RaceEngine, RacePhase

from conftest import make_config_dict

CH = "test-channel"


class _RecordingDatabase(EconomyDatabase):
    """Wraps the real store and records save_race_* call order."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.race_writes: list[str] = []

    async def save_race_result(self, *a, **kw):  # noqa: D102
        self.race_writes.append("result")
        await super().save_race_result(*a, **kw)

    async def save_race_bet(self, *a, **kw):  # noqa: D102
        self.race_writes.append("bet")
        await super().save_race_bet(*a, **kw)

    async def save_race_resolution(self, *a, **kw):  # noqa: D102
        # Record the intent this method provides: parent first, then children.
        self.race_writes.append("result+bet")
        await super().save_race_resolution(*a, **kw)


@pytest_asyncio.fixture
async def rec_db(tmp_path) -> _RecordingDatabase:
    db = _RecordingDatabase(str(tmp_path / "race_order.db"), logging.getLogger("t"))
    await db.initialize()
    return db


@pytest_asyncio.fixture
async def rec_engine(rec_db: _RecordingDatabase) -> RaceEngine:
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
    return RaceEngine(EconomyConfig(**cfg_dict), rec_db, logging.getLogger("t"))


async def _seed(db: EconomyDatabase, user: str, balance: int = 5000) -> None:
    from datetime import datetime, timedelta, timezone

    await db.get_or_create_account(user, CH)
    await db.credit(user, CH, balance, tx_type="seed", trigger_id="test")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    loop = asyncio.get_running_loop()

    def _u() -> None:
        conn = db._get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET first_seen=? WHERE username=? AND channel=?",
                (old, user, CH),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _u)


@pytest.mark.asyncio
class TestRaceWriteOrdering:
    async def test_result_is_written_before_any_bet(
        self, rec_engine: RaceEngine, rec_db: _RecordingDatabase
    ) -> None:
        """race_bets.race_id is an FK to race_results.race_id.

        If a bet is ever written before the result row, PostgreSQL raises
        ForeignKeyViolationError. Assert the ordering explicitly because the
        SQLite test backend does not enforce foreign keys.
        """
        await _seed(rec_db, "Alice")
        await _seed(rec_db, "Bob")

        rec_engine.start_race(CH, "Dealer")
        race = rec_engine.get_active_race(CH)
        colors = list(race.racers.keys())
        await rec_engine.place_bet("Alice", CH, 100, colors[0])
        await rec_engine.place_bet("Bob", CH, 200, colors[1])
        race.phase = RacePhase.RACING
        race.racers[colors[0]].progress = 25.0

        await rec_engine.resolve_race(CH)

        assert rec_db.race_writes, "no race writes recorded"
        # Exactly one atomic resolution write, and no bare per-bet writes.
        assert (
            "bet" not in rec_db.race_writes
        ), f"bets written individually (breaks FK ordering): {rec_db.race_writes}"
        assert rec_db.race_writes[-1] == "result+bet"

    async def test_no_bare_save_race_result_then_bets_sequence(
        self, rec_engine: RaceEngine, rec_db: _RecordingDatabase
    ) -> None:
        """No 'result' followed by individual 'bet' entries in sequence."""
        await _seed(rec_db, "Alice")
        rec_engine.start_race(CH, "Dealer")
        race = rec_engine.get_active_race(CH)
        winner = list(race.racers.keys())[0]
        await rec_engine.place_bet("Alice", CH, 100, winner)
        race.phase = RacePhase.RACING
        race.racers[winner].progress = 25.0

        await rec_engine.resolve_race(CH)

        writes = rec_db.race_writes
        assert (
            "result" not in writes or "bet" not in writes
        ), f"mixed result+individual-bet writes: {writes}"

    async def test_both_tables_end_up_populated(
        self, rec_engine: RaceEngine, rec_db: _RecordingDatabase
    ) -> None:
        await _seed(rec_db, "Alice")
        await _seed(rec_db, "Bob")
        rec_engine.start_race(CH, "Dealer")
        race = rec_engine.get_active_race(CH)
        colors = list(race.racers.keys())
        await rec_engine.place_bet("Alice", CH, 100, colors[0])
        await rec_engine.place_bet("Bob", CH, 200, colors[1])
        race.phase = RacePhase.RACING
        race.racers[colors[0]].progress = 25.0
        race_id = race.race_id

        await rec_engine.resolve_race(CH)

        loop = asyncio.get_running_loop()

        def _q(sql: str) -> int:
            conn = rec_db._get_connection()
            try:
                return conn.execute(sql, (race_id,)).fetchone()[0]
            finally:
                conn.close()

        assert (
            await loop.run_in_executor(
                None, _q, "SELECT count(*) FROM race_results WHERE race_id=?"
            )
            == 1
        )
        assert (
            await loop.run_in_executor(
                None, _q, "SELECT count(*) FROM race_bets WHERE race_id=?"
            )
            == 2
        )

    async def test_zero_bet_race_still_records_a_result(
        self, rec_engine: RaceEngine, rec_db: _RecordingDatabase
    ) -> None:
        """A race with no bets must still leave a result row behind."""
        rec_engine.start_race(CH, "Dealer")
        race = rec_engine.get_active_race(CH)
        race.phase = RacePhase.RACING
        winner = list(race.racers.keys())[0]
        race.racers[winner].progress = 25.0
        race_id = race.race_id

        await rec_engine.resolve_race(CH)

        loop = asyncio.get_running_loop()

        def _q() -> int:
            conn = rec_db._get_connection()
            try:
                return conn.execute(
                    "SELECT count(*) FROM race_results WHERE race_id=?", (race_id,)
                ).fetchone()[0]
            finally:
                conn.close()

        assert await loop.run_in_executor(None, _q) == 1
