"""Tests for MediaCMSClient — HTTP wrapper with caching."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from kryten_economy.config import MediaCMSConfig
from kryten_economy.media_client import MediaCMSClient


def _make_client(**overrides) -> MediaCMSClient:
    cfg = MediaCMSConfig(
        base_url="https://media.test.com",
        api_token="test-token",
        search_results_limit=10,
        **overrides,
    )
    import logging
    return MediaCMSClient(cfg, logging.getLogger("test"))


def _fake_media(mid: str = "abc123", title: str = "Test Video", dur: int = 600) -> dict:
    """Simulate raw API response from MediaCMS."""
    return {
        "friendly_token": mid,
        "title": title,
        "duration": dur,
        "media_type": "video",
        "media_id": mid,
    }


# ═══════════════════════════════════════════════════════════════
#  search()
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_search_returns_results():
    """Search parses API results into normalised dicts."""
    client = _make_client()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"results": [_fake_media(), _fake_media("xyz", "Other", 120)]})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    results = await client.search("test")
    assert len(results) == 2
    assert results[0]["title"] == "Test Video"
    assert results[0]["id"] == "abc123"
    assert results[0]["media_type"] == "cm"
    assert results[0]["media_id"] == "https://media.test.com/api/v1/media/cytube/abc123.json?format=json"
    assert results[1]["duration"] == 120

    # Verify correct API params: 'q' (not 'search'), no page_size
    call_args = mock_session.get.call_args
    assert call_args[0][0] == "/api/v1/media"
    assert call_args[1]["params"]["q"] == "test"
    assert "search" not in call_args[1]["params"]


@pytest.mark.asyncio
async def test_search_client_side_limit():
    """Results are truncated to search_results_limit client-side."""
    cfg = MediaCMSConfig(
        base_url="https://media.test.com",
        api_token="test-token",
        search_results_limit=3,
    )
    import logging
    client = MediaCMSClient(cfg, logging.getLogger("test"))
    items = [_fake_media(f"id{i}", f"Video {i}", 100 + i) for i in range(10)]
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"results": items})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    results = await client.search("many")
    assert len(results) == 3
    assert results[0]["id"] == "id0"
    assert results[2]["id"] == "id2"


@pytest.mark.asyncio
async def test_search_empty_results():
    """Search returns [] when no results."""
    client = _make_client()
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"results": []})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    results = await client.search("nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_search_network_error():
    """Search returns [] on network error."""
    client = _make_client()
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=aiohttp.ClientError("Connection failed"))
    client._session = mock_session

    results = await client.search("test")
    assert results == []


@pytest.mark.asyncio
async def test_search_no_session():
    """Search returns [] when session not started."""
    client = _make_client()
    results = await client.search("test")
    assert results == []


# ═══════════════════════════════════════════════════════════════
#  get_by_id()
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_by_id_found():
    """get_by_id returns normalised dict for existing item."""
    client = _make_client()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=_fake_media("vid1", "Found Video", 300))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    result = await client.get_by_id("vid1")
    assert result is not None
    assert result["title"] == "Found Video"
    assert result["duration"] == 300


@pytest.mark.asyncio
async def test_get_by_id_not_found():
    """get_by_id returns None for 404."""
    client = _make_client()
    mock_resp = AsyncMock()
    mock_resp.status = 404
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    result = await client.get_by_id("nonexistent")
    assert result is None


# ═══════════════════════════════════════════════════════════════
#  get_duration()
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_duration():
    """get_duration returns seconds from get_by_id."""
    client = _make_client()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=_fake_media("d1", "Dur Video", 1800))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    dur = await client.get_duration("d1")
    assert dur == 1800


# ═══════════════════════════════════════════════════════════════
#  Caching
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cache_hit():
    """Second search for same query uses cache, not HTTP."""
    client = _make_client()
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"results": [_fake_media()]})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    r1 = await client.search("cached")
    r2 = await client.search("cached")

    assert r1 == r2
    # HTTP called only once — second call hit cache
    assert mock_session.get.call_count == 1


@pytest.mark.asyncio
async def test_cache_expiry():
    """Expired cache entry triggers fresh fetch."""
    client = _make_client()
    client._cache_ttl = 1  # 1 second TTL

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"results": [_fake_media()]})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    client._session = mock_session

    await client.search("expiring")

    # Manually expire the cache
    for key in list(client._cache):
        client._cache[key] = (time.time() - 10, client._cache[key][1])

    await client.search("expiring")
    assert mock_session.get.call_count == 2
