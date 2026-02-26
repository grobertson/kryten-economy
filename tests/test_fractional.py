"""Tests for fractional accumulator logic."""

from __future__ import annotations

import pytest

from kryten_economy.earning_engine import EarningEngine


CH = "testchannel"


def test_half_z_no_credit(earning_engine):
    """0.5 → accumulator = 0.5, credit = 0."""
    result = earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.5)
    assert result == 0


def test_two_halves_credit_one(earning_engine):
    """0.5 + 0.5 → credit = 1, accumulator resets to 0."""
    earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.5)
    result = earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.5)
    assert result == 1


def test_three_thirds_credit_one(earning_engine):
    """0.33 + 0.33 + 0.34 → credit = 1."""
    earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.33)
    earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.33)
    result = earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.34)
    assert result == 1


def test_different_triggers_independent(earning_engine):
    """Accumulators for different trigger_ids are separate."""
    earning_engine._accumulate_fractional("alice", CH, "trigger.A", 0.5)
    result = earning_engine._accumulate_fractional("alice", CH, "trigger.B", 0.5)
    assert result == 0  # trigger.B only has 0.5, not 1.0


def test_different_users_independent(earning_engine):
    """Accumulators for different usernames are separate."""
    earning_engine._accumulate_fractional("alice", CH, "test.trigger", 0.5)
    result = earning_engine._accumulate_fractional("bob", CH, "test.trigger", 0.5)
    assert result == 0  # bob only has 0.5


def test_whole_number_credits_immediately(earning_engine):
    """1.0 → credit = 1 immediately."""
    result = earning_engine._accumulate_fractional("alice", CH, "test.trigger", 1.0)
    assert result == 1
