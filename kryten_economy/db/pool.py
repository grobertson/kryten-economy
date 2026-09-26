"""PostgreSQL connection configuration and pool construction."""

from __future__ import annotations

import os

import asyncpg

from ..config import PostgresConfig


def resolve_dsn(cfg: PostgresConfig) -> str:
    """Resolve a PostgreSQL DSN without requiring secrets in YAML.

    Resolution follows the established kryten-llm pattern: ``dsn_env`` first,
    then a literal ``dsn``, then an assembled URL. When ``password_env`` is
    configured, it is the password source even when the environment variable is
    unset; this keeps the behavior identical to the shared reference pattern.
    """
    if cfg.dsn_env:
        dsn = os.environ.get(cfg.dsn_env)
        if not dsn:
            raise ValueError(
                f"Postgres dsn_env '{cfg.dsn_env}' is set but the env var is empty or unset"
            )
        return dsn

    if cfg.dsn:
        return cfg.dsn

    if cfg.password_env:
        password = os.environ.get(cfg.password_env, "")
    else:
        password = cfg.password or ""

    return f"postgresql://{cfg.user}:{password}@{cfg.host}:{cfg.port}/{cfg.dbname}"


async def create_pool(cfg: PostgresConfig) -> asyncpg.Pool:
    """Create an asyncpg connection pool from the resolved DSN."""
    pool = await asyncpg.create_pool(
        dsn=resolve_dsn(cfg),
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
    )
    if pool is None:
        raise RuntimeError("asyncpg.create_pool() returned no pool")
    return pool
