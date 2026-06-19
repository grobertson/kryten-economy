"""Vanity chat-color CSS management.

Builds and merges an auto-managed block of per-user chat colors into a
CyTube channel's custom CSS. The managed block is delimited by sentinel
comment markers so that hand-maintained CSS (including bot colors and any
other styling) is preserved untouched.

On each apply the full managed block is rebuilt from the database, which lets
the automation absorb the channel's existing ``legacy_marker`` rules into a
single managed block (no duplicates) the first time it runs.

CyTube chat-message CSS classes are case-sensitive (``.chat-msg-TeenageDraculerX``)
but the economy database stores usernames lowercased. To keep existing rules
working, the original casing is harvested from the current CSS (and from the
``display_overrides`` supplied for the active purchase), so rebuilt selectors
preserve the exact case CyTube emits.

All functions here are pure (no I/O) so they can be unit-tested in isolation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# CyTube usernames are limited to these characters. Restricting the selector to
# this set prevents CSS injection via a crafted username (OWASP A03).
_SAFE_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,30}$")

# Harvests the username token from a ``.chat-msg-<user>`` selector.
_CHAT_MSG_RE = re.compile(r"\.chat-msg-([A-Za-z0-9_-]+)")


def is_safe_username(username: str) -> bool:
    """Return True if ``username`` is safe to interpolate into a CSS selector."""
    return bool(_SAFE_USERNAME_RE.match(username))


def harvest_username_casing(css: str) -> dict[str, str]:
    """Map lowercased username -> original-case username from ``.chat-msg-*`` rules.

    Used to preserve the exact casing CyTube expects when rebuilding selectors
    from the (lowercased) database.
    """
    casing: dict[str, str] = {}
    for token in _CHAT_MSG_RE.findall(css):
        casing.setdefault(token.lower(), token)
    return casing


def build_managed_block(
    selectors_and_colors: list[tuple[str, str]],
    *,
    begin_marker: str,
    end_marker: str,
) -> str:
    """Render the managed CSS block from ``(selector, hex)`` pairs.

    The block is always wrapped in the begin/end sentinel markers, even when
    empty, so a subsequent merge can find and replace it.
    """
    lines = [begin_marker]
    for selector, hex_value in selectors_and_colors:
        lines.append(f"{selector} {{ color: {hex_value}; }}")
    lines.append(end_marker)
    return "\n".join(lines)


def strip_managed_and_legacy(
    css: str,
    *,
    begin_marker: str,
    end_marker: str,
    legacy_marker: str,
) -> str:
    """Remove any existing managed block and legacy per-user color rules.

    - The managed block is everything between ``begin_marker`` and
      ``end_marker`` (inclusive).
    - Legacy rules are ``legacy_marker`` comments followed by a single
      ``.chat-msg-*`` rule, matching the channel's historical hand-maintained
      convention.

    Hand-maintained CSS that does not match either pattern is left intact.
    """
    # Remove the managed block (inclusive of its markers).
    managed_re = re.compile(
        re.escape(begin_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    css = managed_re.sub("", css)

    # Remove legacy "marker + .chat-msg-<user> { ... }" rules.
    legacy_re = re.compile(
        re.escape(legacy_marker) + r"\s*\.chat-msg-[A-Za-z0-9_-]+\s*\{[^}]*\}",
        re.DOTALL,
    )
    css = legacy_re.sub("", css)

    # Collapse the runs of blank lines that stripping may leave behind.
    css = re.sub(r"\n{3,}", "\n\n", css)
    return css


def merge_vanity_css(
    existing_css: str,
    colors: Mapping[str, str],
    *,
    display_overrides: Mapping[str, str] | None = None,
    selector_template: str = ".chat-msg-{username}",
    begin_marker: str = "/* BEGIN kryten-economy vanity colors — auto-managed, do not edit */",
    end_marker: str = "/* END kryten-economy vanity colors */",
    legacy_marker: str = "/* ZCoin purchased vanity colors */",
) -> str:
    """Return ``existing_css`` with a freshly-rebuilt managed vanity block.

    ``colors`` maps (lowercased) username -> ``#RRGGBB``. ``display_overrides``
    maps lowercased username -> authoritative original-case username for the
    active purchase; it takes precedence over casing harvested from the CSS.

    Entries are emitted sorted by lowercased username for a stable,
    diff-friendly result. Existing managed and legacy rules are removed first so
    the block stays the single source of truth without creating duplicates.
    Usernames that are not selector-safe are skipped.
    """
    casing = harvest_username_casing(existing_css)
    if display_overrides:
        for lower, original in display_overrides.items():
            casing[lower.lower()] = original

    base = strip_managed_and_legacy(
        existing_css,
        begin_marker=begin_marker,
        end_marker=end_marker,
        legacy_marker=legacy_marker,
    ).rstrip()

    selectors_and_colors: list[tuple[str, str]] = []
    for lower_user, hex_value in sorted(colors.items(), key=lambda kv: kv[0].lower()):
        display = casing.get(lower_user.lower(), lower_user)
        if not is_safe_username(display):
            continue
        selectors_and_colors.append(
            (selector_template.format(username=display), hex_value)
        )

    block = build_managed_block(
        selectors_and_colors,
        begin_marker=begin_marker,
        end_marker=end_marker,
    )

    if base:
        return f"{base}\n\n{block}\n"
    return f"{block}\n"
