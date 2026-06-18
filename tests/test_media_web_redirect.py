"""Tests for the web-queue redirect of search/queue/playnext commands."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kryten_economy.config import EconomyConfig
from kryten_economy.database import EconomyDatabase
from kryten_economy.pm_handler import PmHandler
from kryten_economy.presence_tracker import PresenceTracker
from kryten_economy.spending_engine import SpendingEngine

CH = "testchannel"


def _make_handler(
    config: EconomyConfig,
    database: EconomyDatabase,
    spending_engine: SpendingEngine,
    mock_media_client: MagicMock,
) -> PmHandler:
    logger = logging.getLogger("test")
    presence = PresenceTracker(config, database, logger)
    return PmHandler(
        config=config,
        database=database,
        client=None,
        presence_tracker=presence,
        logger=logger,
        spending_engine=spending_engine,
        media_client=mock_media_client,
    )


@pytest.mark.asyncio
async def test_redirect_enabled_by_default(sample_config: EconomyConfig):
    """The web-queue redirect ships enabled by default."""
    assert sample_config.mediacms.web_queue_redirect is True
    assert sample_config.mediacms.web_queue_url == "https://queue.dropsugar.co/"


@pytest.mark.asyncio
async def test_search_redirects_to_web(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """`search` returns the web-queue redirect and never hits MediaCMS."""
    mock_media_client.search = AsyncMock()
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_search("Alice", CH, ["anything"])

    assert "https://queue.dropsugar.co/" in resp
    assert "moved to the web" in resp.lower()
    mock_media_client.search.assert_not_called()
    # No stale search state should be created for the number-selection flow.
    assert "alice" not in handler._last_search


@pytest.mark.asyncio
async def test_queue_redirects_to_web(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """`queue` returns the web-queue redirect and never debits the account."""
    mock_media_client.get_by_id = AsyncMock()
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_queue("Alice", CH, ["v1"])

    assert "https://queue.dropsugar.co/" in resp
    mock_media_client.get_by_id.assert_not_called()
    assert "alice" not in handler._pending_confirm


@pytest.mark.asyncio
async def test_playnext_redirects_to_web(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """`playnext` returns the web-queue redirect."""
    mock_media_client.get_by_id = AsyncMock()
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_playnext("Alice", CH, ["v1"])

    assert "https://queue.dropsugar.co/" in resp
    mock_media_client.get_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_help_points_to_web_queue(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """The help text Media section points at the web queue when redirect is on."""
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_help("Alice", CH, [])

    assert "https://queue.dropsugar.co/" in resp


@pytest.mark.asyncio
async def test_redirect_disabled_runs_legacy_search(
    sample_config: EconomyConfig, database: EconomyDatabase,
    spending_engine: SpendingEngine, mock_media_client: MagicMock,
):
    """With the redirect disabled, the legacy search flow runs again."""
    sample_config.mediacms.web_queue_redirect = False
    mock_media_client.search = AsyncMock(return_value=[])
    handler = _make_handler(sample_config, database, spending_engine, mock_media_client)

    resp = await handler._cmd_search("Alice", CH, ["nothing"])

    assert "https://queue.dropsugar.co/" not in resp
    mock_media_client.search.assert_called_once()
