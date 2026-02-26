"""MediaCMS API client — async HTTP wrapper with caching.

Provides search() and get_by_id() methods for querying the MediaCMS catalog.
All tests mock the HTTP layer — never call a real MediaCMS instance.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .config import MediaCMSConfig


class MediaCMSClient:
    """Async client for the MediaCMS catalog API."""

    def __init__(self, config: MediaCMSConfig, logger: logging.Logger) -> None:
        self._config = config
        self._logger = logger
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, Any]] = {}  # {key: (expiry_ts, data)}
        self._cache_ttl = 300  # 5 minutes default

    async def start(self) -> None:
        """Create the HTTP session."""
        headers: dict[str, str] = {}
        if self._config.api_token:
            headers["Authorization"] = f"Token {self._config.api_token}"
        self._session = aiohttp.ClientSession(
            base_url=self._config.base_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10.0),
        )

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def search(self, query: str) -> list[dict]:
        """Search the MediaCMS catalog.

        Returns list of dicts with: id, title, duration, media_type, media_id.
        Returns [] on error or empty results.
        """
        cache_key = f"search:{query.lower().strip()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        if not self._session:
            return []

        try:
            async with self._session.get(
                "/api/v1/media",
                params={
                    "search": query,
                    "page_size": self._config.search_results_limit,
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                results = self._parse_search_results(data)
                self._set_cached(cache_key, results)
                return results
        except Exception as e:
            self._logger.error("MediaCMS search failed for '%s': %s", query, e)
            return []

    async def get_by_id(self, media_id: str) -> dict | None:
        """Fetch a single media item by its ID.

        Returns dict with: id, title, duration, media_type, media_id.
        Returns None if not found or on error.
        """
        cache_key = f"item:{media_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        if not self._session:
            return None

        try:
            async with self._session.get(f"/api/v1/media/{media_id}") as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json()
                result = self._parse_media_item(data)
                self._set_cached(cache_key, result)
                return result
        except Exception as e:
            self._logger.error("MediaCMS get_by_id failed for '%s': %s", media_id, e)
            return None

    async def get_duration(self, media_id: str) -> int | None:
        """Get the duration of a media item in seconds."""
        item = await self.get_by_id(media_id)
        return item["duration"] if item else None

    # ══════════════════════════════════════════════════════════
    #  Internal Helpers
    # ══════════════════════════════════════════════════════════

    def _parse_search_results(self, data: dict | list) -> list[dict]:
        """Parse the API response into a normalised result list."""
        results = data.get("results", data) if isinstance(data, dict) else data
        return [self._parse_media_item(item) for item in results]

    @staticmethod
    def _parse_media_item(item: dict) -> dict:
        """Parse a single media item from API response."""
        return {
            "id": item.get("friendly_token", item.get("id", "")),
            "title": item.get("title", "Unknown"),
            "duration": item.get("duration", 0),
            "media_type": item.get("media_type", "yt"),
            "media_id": item.get("media_id", item.get("friendly_token", "")),
        }

    def _get_cached(self, key: str) -> Any | None:
        """Return cached value if not expired, else None."""
        if key in self._cache:
            expiry, data = self._cache[key]
            if time.time() < expiry:
                return data
            del self._cache[key]
        return None

    def _set_cached(self, key: str, data: Any) -> None:
        """Cache a result with configured TTL."""
        self._cache[key] = (time.time() + self._cache_ttl, data)
