"""Alembic environment for the kryten-economy PostgreSQL schema.

Sprint 12: Alembic is the single schema authority. This module is deliberately
*schema-only* — it does not import ``EconomyDatabasePg`` and does not use
SQLAlchemy ORM metadata. The economy data layer is written directly against
``asyncpg``; this environment manages DDL and version tracking only.

Migrations run through a **synchronous** SQLAlchemy engine (psycopg2). Alembic
only issues DDL, so there is no reason to pull in SQLAlchemy's asyncio shim and
its ``greenlet`` dependency; the application's own async ``asyncpg`` pool is the
only thing that talks to the database at runtime.

The database URL is never stored in ``alembic.ini`` (it can contain a secret).
It is resolved at runtime, in priority order:

1. ``KRYTEN_ECONOMY_ALEMBIC_URL`` environment variable (used by CI/tests).
2. The service's own ``database.postgres`` block, loaded from ``config.yaml``
   and resolved through :func:`kryten_economy.db.pool.resolve_dsn`.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from kryten_economy.db.pool import resolve_dsn

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _is_asyncio_url(url: str) -> bool:
    """Return True when the driver is an async one Alembic cannot use directly."""
    driver = url.split("://", 1)[0].lower()
    return driver.endswith("+asyncpg") or driver.endswith("+aiopg")


def _database_url_for_alembic() -> str:
    """Resolve the migration URL and force it onto a synchronous driver."""
    env_url = os.environ.get("KRYTEN_ECONOMY_ALEMBIC_URL")
    if env_url:
        url = env_url
    else:
        config_path = os.environ.get("KRYTEN_ECONOMY_CONFIG", "config.yaml")
        try:
            from kryten_economy.config import load_config

            cfg = load_config(config_path)
            url = resolve_dsn(cfg.database.postgres)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Cannot resolve a PostgreSQL URL for Alembic. Set "
                "KRYTEN_ECONOMY_ALEMBIC_URL, or point KRYTEN_ECONOMY_CONFIG at a "
                "config.yaml that contains a database.postgres block."
            ) from exc
        except Exception as exc:  # pragma: no cover - surfaced to the operator
            raise RuntimeError(
                f"Failed to resolve PostgreSQL URL for Alembic: {exc}"
            ) from exc

    # Alembic's migration engine is synchronous; swap async drivers for psycopg2.
    if _is_asyncio_url(url):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    elif "+" not in url.split("://", 1)[0]:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url_for_alembic(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Entry point for online (connected) migrations."""
    connectable = create_engine(
        _database_url_for_alembic(),
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
