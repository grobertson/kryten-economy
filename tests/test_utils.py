"""Tests for kryten_economy.utils module."""

from __future__ import annotations

from datetime import datetime, timezone

from kryten_economy.utils import (
    iso_week_str,
    normalize_channel,
    now_utc,
    parse_timestamp,
    today_str,
)


class TestNormalizeChannel:
    def test_basic(self):
        assert normalize_channel("MyChannel") == "mychannel"

    def test_spaces(self):
        assert normalize_channel("My Channel") == "my_channel"


class TestParseTimestamp:
    def test_iso_format(self):
        dt = parse_timestamp("2026-01-15T10:30:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 10

    def test_with_timezone(self):
        dt = parse_timestamp("2026-01-15T10:30:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none(self):
        assert parse_timestamp(None) is None

    def test_invalid(self):
        assert parse_timestamp("not-a-date") is None

    def test_empty(self):
        assert parse_timestamp("") is None


class TestTodayStr:
    def test_format(self):
        result = today_str()
        assert len(result) == 10
        assert result[4] == "-"


class TestNowUtc:
    def test_timezone_aware(self):
        dt = now_utc()
        assert dt.tzinfo == timezone.utc


class TestIsoWeekStr:
    def test_known_date(self):
        dt = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday
        result = iso_week_str(dt)
        assert result.startswith("2026-W")

    def test_default_now(self):
        result = iso_week_str()
        assert "-W" in result
