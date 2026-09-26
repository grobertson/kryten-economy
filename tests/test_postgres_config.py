"""Tests for Sortie 1 PostgreSQL configuration and connection-pool scaffolding."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from kryten_economy.config import DatabaseConfig, EconomyConfig, PostgresConfig
from kryten_economy.db.pool import create_pool, resolve_dsn
from kryten_economy.main import EconomyApp


def _economy_config(database: dict | None = None) -> EconomyConfig:
    """Build a minimal valid service config with an optional database block."""
    return EconomyConfig(
        nats={"servers": ["nats://localhost:4222"]},
        channels=[{"domain": "cytu.be", "channel": "test"}],
        database=database or {},
    )


class TestDatabaseConfig:
    """Database backend configuration remains backward-compatible with SQLite."""

    def test_sqlite_is_default_backend(self) -> None:
        cfg = DatabaseConfig()

        assert cfg.backend == "sqlite"
        assert cfg.path == "economy.db"
        assert cfg.postgres == PostgresConfig()

    def test_sqlite_only_block_parses(self) -> None:
        cfg = DatabaseConfig(path="custom.db")

        assert cfg.backend == "sqlite"
        assert cfg.path == "custom.db"

    def test_postgres_block_parses(self) -> None:
        cfg = DatabaseConfig.model_validate(
            {
                "backend": "postgres",
                "postgres": {
                    "host": "db.internal",
                    "port": 5544,
                    "user": "economy",
                    "dbname": "economy_test",
                    "pool_min_size": 2,
                    "pool_max_size": 6,
                },
            }
        )

        assert cfg.backend == "postgres"
        assert cfg.postgres.host == "db.internal"
        assert cfg.postgres.port == 5544
        assert cfg.postgres.pool_min_size == 2
        assert cfg.postgres.pool_max_size == 6

    def test_invalid_backend_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            DatabaseConfig(backend="mysql")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("pool_min_size", "pool_max_size"),
        [(2, 1), (-1, 8), (1, 0)],
    )
    def test_invalid_pool_bounds_are_rejected(self, pool_min_size: int, pool_max_size: int) -> None:
        with pytest.raises(ValueError):
            PostgresConfig(
                pool_min_size=pool_min_size,
                pool_max_size=pool_max_size,
            )


class TestResolveDsn:
    """DSN resolution mirrors the established kryten-llm precedence contract."""

    def test_dsn_env_has_highest_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRYTEN_ECONOMY_DSN", "postgresql://env-db/economy")
        cfg = PostgresConfig(
            dsn_env="KRYTEN_ECONOMY_DSN",
            dsn="postgresql://literal-db/economy",
        )

        assert resolve_dsn(cfg) == "postgresql://env-db/economy"

    def test_literal_dsn_is_second(self) -> None:
        cfg = PostgresConfig(dsn="postgresql://literal-db/economy")

        assert resolve_dsn(cfg) == "postgresql://literal-db/economy"

    def test_assembled_dsn_uses_literal_password(self) -> None:
        cfg = PostgresConfig(
            host="db.internal",
            port=5544,
            user="economy",
            dbname="economy_test",
            password="fallback",
        )

        assert resolve_dsn(cfg) == "postgresql://economy:fallback@db.internal:5544/economy_test"

    def test_password_env_is_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KRYTEN_ECONOMY_PG_PASSWORD", "from-env")
        cfg = PostgresConfig(
            host="localhost",
            port=5432,
            user="kryten",
            dbname="kryten_economy",
            password_env="KRYTEN_ECONOMY_PG_PASSWORD",
            password="from-config",
        )

        assert resolve_dsn(cfg) == "postgresql://kryten:from-env@localhost:5432/kryten_economy"

    def test_missing_password_env_does_not_fall_back_to_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KRYTEN_ECONOMY_PG_PASSWORD", raising=False)
        cfg = PostgresConfig(
            password_env="KRYTEN_ECONOMY_PG_PASSWORD",
            password="from-config",
        )

        assert resolve_dsn(cfg) == "postgresql://kryten:@localhost:5432/kryten_economy"

    @pytest.mark.parametrize("env_value", [None, ""])
    def test_configured_dsn_env_must_be_nonempty(
        self, monkeypatch: pytest.MonkeyPatch, env_value: str | None
    ) -> None:
        if env_value is None:
            monkeypatch.delenv("KRYTEN_ECONOMY_DSN", raising=False)
        else:
            monkeypatch.setenv("KRYTEN_ECONOMY_DSN", env_value)
        cfg = PostgresConfig(dsn_env="KRYTEN_ECONOMY_DSN")

        with pytest.raises(ValueError, match="env var is empty"):
            resolve_dsn(cfg)


class TestPoolFactory:
    """The asyncpg factory passes resolved DSN and configured bounds unchanged."""

    @pytest.mark.asyncio
    async def test_create_pool_forwards_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = object()
        factory = AsyncMock(return_value=pool)
        monkeypatch.setattr(asyncpg, "create_pool", factory)
        cfg = PostgresConfig(
            dsn="postgresql://db.internal/economy",
            pool_min_size=2,
            pool_max_size=5,
        )

        result = await create_pool(cfg)

        assert result is pool
        factory.assert_awaited_once_with(
            dsn="postgresql://db.internal/economy",
            min_size=2,
            max_size=5,
        )

    @pytest.mark.asyncio
    async def test_create_pool_rejects_unexpected_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=None))

        with pytest.raises(RuntimeError, match="returned no pool"):
            await create_pool(PostgresConfig(dsn="postgresql://db.internal/economy"))


class TestApplicationPoolLifecycle:
    """EconomyApp owns the optional pool and closes it during shutdown.

    Sortie 3 made ``database.backend`` genuinely dispatch, so the postgres
    branch now builds an :class:`EconomyDatabasePg` over the pool instead of
    the SQLite store.
    """

    @pytest.mark.asyncio
    async def test_postgres_resources_open_and_close(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        pool = MagicMock()
        pool.close = AsyncMock()
        create_pool_mock = AsyncMock(return_value=pool)
        store = MagicMock()
        store.initialize = AsyncMock()
        store_factory = MagicMock(return_value=store)
        monkeypatch.setattr("kryten_economy.main.create_pool", create_pool_mock)
        monkeypatch.setattr("kryten_economy.main.EconomyDatabasePg", store_factory)
        # The SQLite store must NOT be constructed for a postgres config.
        sqlite_factory = MagicMock()
        monkeypatch.setattr("kryten_economy.main.EconomyDatabase", sqlite_factory)

        db_path = str(tmp_path / "economy.db")
        cfg = _economy_config(
            {
                "backend": "postgres",
                "path": db_path,
                "postgres": {
                    "dsn": "postgresql://db.internal/economy",
                    "pool_min_size": 2,
                    "pool_max_size": 4,
                },
            }
        )
        app = EconomyApp(str(tmp_path / "config.yaml"))

        await app._initialize_database_resources(cfg)
        await app.stop()

        create_pool_mock.assert_awaited_once_with(cfg.database.postgres)
        store_factory.assert_called_once_with(pool, app.logger)
        store.initialize.assert_awaited_once()
        sqlite_factory.assert_not_called()
        pool.close.assert_awaited_once()
        assert app._pg_pool is None

    @pytest.mark.asyncio
    async def test_sqlite_resources_do_not_create_postgres_pool(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        create_pool_mock = AsyncMock()
        database = MagicMock()
        database.initialize = AsyncMock()
        database_factory = MagicMock(return_value=database)
        monkeypatch.setattr("kryten_economy.main.create_pool", create_pool_mock)
        monkeypatch.setattr("kryten_economy.main.EconomyDatabase", database_factory)

        db_path = str(tmp_path / "economy.db")
        cfg = _economy_config({"path": db_path})
        app = EconomyApp(str(tmp_path / "config.yaml"))

        await app._initialize_database_resources(cfg)

        create_pool_mock.assert_not_awaited()
        database_factory.assert_called_once_with(db_path, app.logger)
        database.initialize.assert_awaited_once()
        assert app._pg_pool is None
