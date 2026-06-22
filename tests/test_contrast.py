"""Tests for the chat-color readability metric (kryten_economy.contrast).

The metric combines APCA perceptual lightness contrast with a chroma penalty
for near-monochromatic reds, scored against the near-black chat background. These
pin the behaviour the live guard depends on: harsh reds and dark colors reject,
pinks/normal colors pass.
"""

from __future__ import annotations

import pytest

from kryten_economy.contrast import (
    LEVEL_OK,
    LEVEL_REJECT,
    LEVEL_WARN,
    apca_contrast,
    chroma_factor,
    classify_contrast,
    evaluate_color,
    readability,
    readability_score,
)

BG = "#111111"
MIN, WARN = 30.0, 40.0


class TestApca:
    def test_white_on_black_is_high(self):
        # APCA reference: white text on black is ~107 Lc.
        assert readability("#FFFFFF", "#000000") == pytest.approx(107.9, abs=0.5)

    def test_gray_on_white_reference(self):
        # #888 on white is a well-known APCA ~63 reference point.
        assert readability("#888888", "#FFFFFF") == pytest.approx(63.1, abs=0.5)

    def test_reverse_polarity_is_negative(self):
        # Light text on dark bg => negative signed Lc; readability is its magnitude.
        assert apca_contrast("#FFFFFF", BG) < 0
        assert readability("#FFFFFF", BG) > 0


class TestChromaFactor:
    def test_pure_red_is_fully_penalized(self):
        assert chroma_factor("#FF0000") == 0.0

    def test_pink_is_unpenalized(self):
        # Hot pink carries plenty of blue -> factor saturates at 1.0.
        assert chroma_factor("#FF1A98") == 1.0

    def test_ramps_with_green_blue(self):
        # Half the knee's worth of G+B -> ~0.5.
        assert chroma_factor("#FF0050") == pytest.approx(0x50 / 160, abs=0.01)


class TestReadabilityScore:
    def test_pure_red_collapses_to_zero(self):
        # Decent APCA Lc, but zero non-red content -> combined score 0.
        assert readability("#FF0000", BG) > 30
        assert readability_score("#FF0000", BG) == 0.0

    def test_pink_keeps_its_score(self):
        # Pink: chroma factor 1, so score == Lc.
        assert readability_score("#FF69B4", BG) == pytest.approx(readability("#FF69B4", BG), abs=0.05)


class TestClassify:
    @pytest.mark.parametrize("hexv", ["#800000", "#AA0000", "#DC143C", "#FF0000", "#FF3300", "#0000FF", "#000080"])
    def test_harsh_or_dark_colors_reject(self, hexv):
        level, _ = classify_contrast(hexv, BG, min_lc=MIN, warn_lc=WARN)
        assert level == LEVEL_REJECT, hexv

    @pytest.mark.parametrize("hexv", ["#FF69B4", "#FF7F50", "#FF6347", "#50C878", "#FFD700", "#DA70D6", "#AAAAAA", "#FFFFFF"])
    def test_good_colors_pass(self, hexv):
        level, _ = classify_contrast(hexv, BG, min_lc=MIN, warn_lc=WARN)
        assert level == LEVEL_OK, hexv

    def test_borderline_warns(self):
        # Deep pink ~39.6 sits in the warn band (30..40).
        level, score = classify_contrast("#FF1493", BG, min_lc=MIN, warn_lc=WARN)
        assert level == LEVEL_WARN
        assert MIN <= score < WARN


class TestEvaluate:
    def test_reject_red_message_mentions_green_blue(self):
        v = evaluate_color("#FF0000", BG, min_lc=MIN, warn_lc=WARN)
        assert v["level"] == LEVEL_REJECT
        assert v["acceptable"] is False
        assert "green or blue" in v["message"]

    def test_reject_dark_message_says_lighter(self):
        v = evaluate_color("#000080", BG, min_lc=MIN, warn_lc=WARN)  # navy: dark, not red
        assert v["level"] == LEVEL_REJECT
        assert "lighter" in v["message"]

    def test_ok_has_no_message(self):
        v = evaluate_color("#FF69B4", BG, min_lc=MIN, warn_lc=WARN)
        assert v["level"] == LEVEL_OK
        assert v["acceptable"] is True
        assert v["message"] == ""

    def test_warn_has_message_and_acceptable(self):
        v = evaluate_color("#FF1493", BG, min_lc=MIN, warn_lc=WARN)
        assert v["level"] == LEVEL_WARN
        assert v["acceptable"] is True
        assert "low-contrast" in v["message"]
