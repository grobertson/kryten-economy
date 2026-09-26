"""Regression tests for the encapsulated Sortie 2 data boundary."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from kryten_economy.command_handler import CommandHandler
from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.multiplier_engine import MultiplierEngine
from kryten_economy.presence_tracker import PresenceTracker


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    client: MagicMock,
) -> CommandHandler:
    app = MagicMock()
    app.config = config
    app.db = database
    app.client = client
    app.logger = logging.getLogger("test.app")
    app.commands_processed = 0
    app.uptime_seconds = 42.5
    app.presence_tracker = PresenceTracker(
        config=config,
        database=database,
        client=client,
        logger=logging.getLogger("test.presence"),
    )
    app.multiplier_engine = MultiplierEngine(
        config=config,
        presence_tracker=app.presence_tracker,
        logger=logging.getLogger("test.multiplier"),
    )
    return CommandHandler(app, client, logging.getLogger("test.command"))


async def test_balance_search_preserves_order_and_result_shape(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> None:
    """Account search stays balance-sorted, pattern-filtered, channel-scoped."""
    await database.credit("Alice", "testchannel", 300, "seed")
    await database.credit("Alicia", "testchannel", 200, "seed")
    await database.credit("Bob", "testchannel", 100, "seed")
    await database.credit("Hidden", "other", 999, "seed")

    direct = await database.search_accounts("testchannel", "Ali", 50)
    assert direct == [
        {
            "username": "Alice",
            "balance": 300,
            "lifetime_earned": 300,
            "rank_name": "Extra",
        },
        {
            "username": "Alicia",
            "balance": 200,
            "lifetime_earned": 200,
            "rank_name": "Extra",
        },
    ]

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._handle_command(
        {
            "command": "balance.search",
            "channel": "testchannel",
            "pattern": "Ali",
            "limit": 50,
        }
    )

    assert response["success"] is True
    assert response["data"] == {
        "channel": "testchannel",
        "pattern": "Ali",
        "count": 2,
        "results": direct,
    }


async def test_transactions_list_preserves_pagination_and_response_shape(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> None:
    """User transaction listing keeps newest-first order and exact offsets."""
    await database.credit("Alice", "testchannel", 10, "one")
    await database.credit("Bob", "testchannel", 5, "other-user")
    await database.credit("Alice", "testchannel", 20, "two")
    await database.credit("Alice", "other", 999, "other-channel")

    expected = await database.get_recent_transactions("Alice", "testchannel", limit=1, offset=1)
    assert [row["amount"] for row in expected] == [10]

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._handle_command(
        {
            "command": "transactions.list",
            "username": "Alice",
            "channel": "testchannel",
            "limit": 2,
            "offset": 1,
        }
    )

    assert response["success"] is True
    assert response["data"] == {
        "username": "Alice",
        "channel": "testchannel",
        "limit": 2,
        "offset": 1,
        "transactions": expected,
    }


async def test_transactions_recent_preserves_channel_scope_and_order(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> None:
    """Channel-wide transaction listing excludes other channels."""
    await database.credit("Alice", "testchannel", 10, "one")
    await database.credit("Bob", "testchannel", 5, "two")
    await database.credit("Alice", "testchannel", 20, "three")
    await database.credit("Eve", "other", 999, "other-channel")

    expected = await database.get_recent_channel_transactions("testchannel", limit=2)
    assert [(row["username"], row["amount"]) for row in expected] == [
        ("Alice", 20),
        ("Bob", 5),
    ]

    handler = _make_handler(sample_config, database, mock_client)
    response = await handler._handle_command(
        {
            "command": "transactions.recent",
            "channel": "testchannel",
            "limit": 2,
        }
    )

    assert response["success"] is True
    assert response["data"] == {
        "channel": "testchannel",
        "limit": 2,
        "transactions": expected,
    }


async def test_events_list_uses_public_state_accessors(
    sample_config: EconomyConfig,
    database: EconomyDatabase,
    mock_client: MagicMock,
) -> None:
    """Scheduled and ad-hoc event details are exposed through public accessors."""
    handler = _make_handler(sample_config, database, mock_client)
    end_time = datetime.now(timezone.utc) + timedelta(hours=1)
    handler._app.multiplier_engine.set_scheduled_event("testchannel", "Movie Night", 2.0, end_time)
    handler._app.multiplier_engine.start_adhoc_event("Flash Event", 1.5, 30)

    response = await handler._handle_command(
        {
            "command": "events.list",
            "channel": "testchannel",
        }
    )

    assert response["success"] is True
    assert [(event["type"], event["name"]) for event in response["data"]["events"]] == [
        ("scheduled", "Movie Night"),
        ("adhoc", "Flash Event"),
    ]

    scheduled = handler._app.multiplier_engine.get_scheduled_event("testchannel")
    assert scheduled is not None
    scheduled["name"] = "mutated copy"
    assert (
        handler._app.multiplier_engine.get_scheduled_event("testchannel")["name"] == "Movie Night"
    )


def test_raw_sqlite_access_is_confined_to_database_module() -> None:
    """No production module outside database.py uses raw *SQLite* directly.

    Parsed with :mod:`tokenize` rather than a plain substring search so that
    prose mentioning SQLite (e.g. the PostgreSQL store's docstrings explaining how
    it mirrors the SQLite contract) does not trip the check.

    The PostgreSQL store legitimately calls ``.execute()`` — that is asyncpg's
    API, not SQLite — so ``db/`` is exempted while everything else, including
    every command handler and engine, must stay free of SQLite identifiers.

    ``migrate_sqlite_to_pg.py`` is a second, narrower exemption: it is the
    one-shot offline ETL whose entire purpose is to read the legacy SQLite file
    directly. It is never imported by the running service (it is an operator
    entry point, invoked as ``python -m kryten_economy.migrate_sqlite_to_pg``),
    so it cannot bypass the store abstraction at runtime. The exemption is keyed
    on that one module so any *new* module reaching for ``sqlite3`` still fails.
    """
    import io
    import tokenize

    package_root = Path(__file__).resolve().parent.parent / "kryten_economy"
    exempt = {"migrate_sqlite_to_pg.py"}
    violations: list[str] = []

    for path in package_root.rglob("*.py"):
        if path.name == "database.py":
            continue
        if path.name in exempt:
            continue
        # The asyncpg store is the sanctioned PostgreSQL engine: it must call
        # .execute() to talk to PostgreSQL at all.
        if path.parent.name == "db" and path.name in {"database_pg.py", "protocol.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        offenders: set[str] = set()
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        for index, token in enumerate(tokens):
            if token.type != tokenize.NAME:
                continue
            if token.string == "sqlite3":
                offenders.add("sqlite3")
            elif token.string == "_get_connection":
                offenders.add("_get_connection")
            # A real attribute call tokenizes as NAME '.' NAME '(' -> only
            # flag a genuine `.execute(...)` reference, never prose.
            elif (
                token.string == "execute"
                and index >= 1
                and tokens[index - 1].type == tokenize.OP
                and tokens[index - 1].string == "."
                and index + 1 < len(tokens)
                and tokens[index + 1].type == tokenize.OP
                and tokens[index + 1].string == "("
            ):
                offenders.add(".execute(")
        if offenders:
            violations.append(f"{path.relative_to(package_root).as_posix()}: {sorted(offenders)}")

    assert violations == []
