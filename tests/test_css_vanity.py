"""Tests for vanity chat-color CSS merge logic (kryten_economy.css_vanity)."""

from __future__ import annotations

from kryten_economy.css_vanity import (
    harvest_managed_colors,
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


class TestHarvestManagedColors:
    def test_harvests_legacy_and_managed_only(self):
        css = (
            "body{}\n"
            ".chat-msg-BotAccount { color: #111111; }\n"  # hand CSS, no marker
            f"{LEGACY}\n.chat-msg-OldTimer {{ color: #abcdef; }}\n"
            f"{BEGIN}\n.chat-msg-Alice {{ color: #222222; }}\n{END}\n"
        )
        harvested = harvest_managed_colors(
            css, begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        # Legacy + managed are captured (with original casing)…
        assert harvested["oldtimer"] == ("OldTimer", "#abcdef")
        assert harvested["alice"] == ("Alice", "#222222")
        # …but an unmarked hand-maintained rule (e.g. a bot) is NOT.
        assert "botaccount" not in harvested


class TestUpgradePreservation:
    def test_preserves_css_only_color_absent_from_db(self):
        """A user whose color is only in a legacy CSS rule survives a rebuild."""
        existing = (
            "body{}\n"
            f"{LEGACY}\n.chat-msg-OldTimer {{ color: #abcdef; }}\n"
        )
        # DB only knows about the active buyer.
        out = merge_vanity_css(
            existing,
            {"alice": "#112233"},
            display_overrides={"alice": "Alice"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert ".chat-msg-OldTimer { color: #abcdef; }" in out
        assert ".chat-msg-Alice { color: #112233; }" in out
        assert LEGACY not in out  # legacy rule folded into the managed block

    def test_db_value_overrides_harvested_value(self):
        existing = f"{BEGIN}\n.chat-msg-Alice {{ color: #111111; }}\n{END}\n"
        out = merge_vanity_css(
            existing,
            {"alice": "#222222"},
            display_overrides={"alice": "Alice"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert ".chat-msg-Alice { color: #222222; }" in out
        assert "#111111" not in out

    def test_protected_user_excluded_even_if_in_css(self):
        existing = (
            "body{}\n"
            f"{LEGACY}\n.chat-msg-ZcoinBank {{ color: #1cfcfc; }}\n"
        )
        out = merge_vanity_css(
            existing,
            {"alice": "#112233"},
            display_overrides={"alice": "Alice"},
            protected={"zcoinbank"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        # The protected user never appears in the auto-managed block…
        managed = out.split(BEGIN, 1)[1]
        assert "ZcoinBank" not in managed
        # …but their color is preserved (never removed) as a plain rule.
        assert ".chat-msg-ZcoinBank { color: #1cfcfc; }" in out
        assert ".chat-msg-Alice { color: #112233; }" in out

    def test_protected_user_in_separate_section_is_untouched(self):
        """A bot color in a hand-maintained section (no legacy marker) is left as-is."""
        existing = (
            "body{}\n"
            "/* minor coloring to bot messages only */\n"
            ".chat-msg-FaxyBrown { color: #ff8a24; }\n"
        )
        out = merge_vanity_css(
            existing,
            {"alice": "#112233"},
            display_overrides={"alice": "Alice"},
            protected={"faxybrown"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        # Untouched: still present exactly once, not duplicated into the block.
        assert out.count(".chat-msg-FaxyBrown") == 1
        assert "/* minor coloring to bot messages only */" in out
        assert "FaxyBrown" not in out.split(BEGIN, 1)[1]

    def test_protected_preservation_converges(self):
        """Re-applying does not duplicate a preserved protected rule."""
        existing = (
            "body{}\n"
            f"{LEGACY}\n.chat-msg-ZcoinBank {{ color: #1cfcfc; }}\n"
        )
        first = merge_vanity_css(
            existing, {"alice": "#112233"},
            display_overrides={"alice": "Alice"}, protected={"zcoinbank"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        second = merge_vanity_css(
            first, {"alice": "#112233"},
            display_overrides={"alice": "Alice"}, protected={"zcoinbank"},
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert second.count(".chat-msg-ZcoinBank") == 1

    def test_preserve_disabled_drops_css_only_color(self):
        existing = (
            "body{}\n"
            f"{LEGACY}\n.chat-msg-OldTimer {{ color: #abcdef; }}\n"
        )
        out = merge_vanity_css(
            existing,
            {"alice": "#112233"},
            display_overrides={"alice": "Alice"},
            preserve_existing=False,
            begin_marker=BEGIN, end_marker=END, legacy_marker=LEGACY,
        )
        assert "OldTimer" not in out
        assert ".chat-msg-Alice { color: #112233; }" in out
