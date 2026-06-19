"""Tests for vanity chat-color CSS merge logic (kryten_economy.css_vanity)."""

from __future__ import annotations

from kryten_economy.css_vanity import (
    harvest_username_casing,
    is_safe_username,
    merge_vanity_css,
    strip_managed_and_legacy,
)

BEGIN = "/* BEGIN kryten-economy vanity colors — auto-managed, do not edit */"
END = "/* END kryten-economy vanity colors */"
LEGACY = "/* ZCoin purchased vanity colors */"


class TestSafeUsername:
    def test_accepts_typical_cytube_usernames(self):
        for name in ["Rat-Bastard", "TeenageDraculerX", "2kings", "Whatd_You_Expect"]:
            assert is_safe_username(name)

    def test_rejects_css_injection_attempts(self):
        for name in ["a{}", "x } body{display:none", "a b", "evil*", ""]:
            assert not is_safe_username(name)


class TestHarvestCasing:
    def test_maps_lowercase_to_original(self):
        css = ".chat-msg-TeenageDraculerX { color: #fff; }\n.chat-msg-Rat-Bastard{color:#000;}"
        casing = harvest_username_casing(css)
        assert casing["teenagedraculerx"] == "TeenageDraculerX"
        assert casing["rat-bastard"] == "Rat-Bastard"

    def test_first_occurrence_wins(self):
        css = ".chat-msg-Bob{} .chat-msg-bob{}"
        assert harvest_username_casing(css)["bob"] == "Bob"


class TestMergeVanityCss:
    def test_appends_managed_block_preserving_hand_css(self):
        existing = "body { color: #fff; }\n.chat-msg-FaxyBrown { color: #ff8a24; }\n"
        out = merge_vanity_css(
            existing,
            {"alice": "#112233"},
            display_overrides={"alice": "Alice"},
            begin_marker=BEGIN,
            end_marker=END,
            legacy_marker=LEGACY,
        )
        # Hand CSS preserved
        assert "body { color: #fff; }" in out
        assert ".chat-msg-FaxyBrown { color: #ff8a24; }" in out
        # Managed block added with correct casing
        assert BEGIN in out and END in out
        assert ".chat-msg-Alice { color: #112233; }" in out

    def test_absorbs_legacy_rules_without_duplicates(self):
        existing = (
            "body{}\n"
            f"{LEGACY}\n.chat-msg-DoodooButtchump {{ color: #c5b358; }}\n"
            f"{LEGACY}\n.chat-msg-Rat-Bastard {{ color: #cf28fd; }}\n"
        )
        out = merge_vanity_css(
            existing,
            {"doodoobuttchump": "#c5b358", "rat-bastard": "#cf28fd"},
            begin_marker=BEGIN,
            end_marker=END,
            legacy_marker=LEGACY,
        )
        # Legacy marker comments removed; rules now live only in the managed block
        assert LEGACY not in out
        assert out.count(".chat-msg-Rat-Bastard") == 1
        assert out.count(".chat-msg-DoodooButtchump") == 1
        # Original casing preserved from the legacy selectors
        assert ".chat-msg-Rat-Bastard { color: #cf28fd; }" in out

    def test_replaces_prior_managed_block_idempotently(self):
        existing = "body{}\n"
        first = merge_vanity_css(
            existing, {"alice": "#111111"},
            display_overrides={"alice": "Alice"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        # Apply again with an updated color — should not stack blocks
        second = merge_vanity_css(
            first, {"alice": "#222222"},
            display_overrides={"alice": "Alice"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert second.count(BEGIN) == 1
        assert second.count(END) == 1
        assert ".chat-msg-Alice { color: #222222; }" in second
        assert "#111111" not in second

    def test_skips_unsafe_username(self):
        out = merge_vanity_css(
            "body{}\n",
            {"a}body{display:none": "#000000", "alice": "#abcabc"},
            display_overrides={"alice": "Alice"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert "display:none" not in out
        assert ".chat-msg-Alice { color: #abcabc; }" in out

    def test_sorted_output_is_stable(self):
        out = merge_vanity_css(
            "body{}\n",
            {"charlie": "#333333", "alice": "#111111", "bob": "#222222"},
            display_overrides={"charlie": "Charlie", "alice": "Alice", "bob": "Bob"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert out.index("Alice") < out.index("Bob") < out.index("Charlie")


class TestStrip:
    def test_removes_managed_block_only(self):
        css = f"keep{{}}\n{BEGIN}\n.chat-msg-X {{ color: #000; }}\n{END}\ntail{{}}"
        out = strip_managed_and_legacy(
            css, begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert "keep{}" in out
        assert "tail{}" in out
        assert BEGIN not in out
        assert ".chat-msg-X" not in out
