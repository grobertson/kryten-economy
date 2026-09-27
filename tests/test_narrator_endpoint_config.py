"""Tests for narrator LLM endpoint configuration.

The production bug: both narrators had their own copy of the LLM config with
drifting defaults, and the race narrator's endpoint was ``http://localhost``
with no port. aiohttp resolved that to port 80, the connection was refused, the
narrator caught it, logged a warning, and silently used static text forever.

These tests pin the shared defaults and make malformed endpoints fail at
config-load time rather than as an endless warning in the log.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kryten_economy.config import (
    BaseNarratorLLMConfig,
    HeistLLMConfig,
    RaceLLMConfig,
)


class TestSharedDefaults:
    def test_both_narrators_share_one_base(self) -> None:
        assert HeistLLMConfig.__mro__[1] is BaseNarratorLLMConfig
        assert RaceLLMConfig.__mro__[1] is BaseNarratorLLMConfig

    def test_endpoints_agree_by_default(self) -> None:
        """The two narrators must not drift apart again."""
        assert HeistLLMConfig().endpoint == RaceLLMConfig().endpoint

    def test_models_agree_by_default(self) -> None:
        assert HeistLLMConfig().model == RaceLLMConfig().model

    def test_default_endpoint_is_empty_not_a_guess(self) -> None:
        """Default must be LLM-off, not a hardcoded localhost that cannot work."""
        assert HeistLLMConfig().endpoint == ""
        assert RaceLLMConfig().endpoint == ""

    def test_no_default_points_at_a_bare_localhost(self) -> None:
        """Regression: the old default was http://localhost:11434, which is
        unreachable from inside a container and also a hardcoded guess."""
        for cls in (HeistLLMConfig, RaceLLMConfig):
            ep = cls().endpoint
            assert "localhost" not in ep, f"{cls.__name__} default points at localhost"


class TestEndpointValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "http://localhost",  # the live bug: silently resolves to port 80
            "http://localhost/",
            "http://127.0.0.1",
            "localhost:1234",
            "1234",
            "ftp://host:1234/v1",
            "host.containers.internal:1234",
            "//host:1234/v1",
        ],
    )
    def test_malformed_endpoint_rejected(self, bad: str) -> None:
        for cls in (HeistLLMConfig, RaceLLMConfig):
            with pytest.raises(ValidationError):
                cls(endpoint=bad)

    @pytest.mark.parametrize(
        "good",
        [
            "",
            "http://host.containers.internal:1234/v1/chat/completions",
            "https://openrouter.ai/api/v1/chat/completions",
            "http://10.89.20.1:1234/v1/chat/completions",
        ],
    )
    def test_valid_endpoint_accepted(self, good: str) -> None:
        for cls in (HeistLLMConfig, RaceLLMConfig):
            assert cls(endpoint=good).endpoint == good

    def test_https_may_omit_the_port(self) -> None:
        """https defaults to 443, which is a sane real default."""
        assert HeistLLMConfig(endpoint="https://example.com/v1").endpoint

    def test_error_message_mentions_the_problem(self) -> None:
        with pytest.raises(ValidationError) as exc:
            HeistLLMConfig(endpoint="http://localhost")
        msg = str(exc.value).lower()
        assert "port" in msg
