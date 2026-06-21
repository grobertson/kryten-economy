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

# Extracts ``(username, color_value)`` from a ``.chat-msg-<user> { … color: … }``
# rule. The color value is captured verbatim (hex, named, etc.) so existing
# rules can be preserved exactly. The lookbehind avoids matching ``-color``
# properties such as ``background-color``.
_CHAT_MSG_COLOR_RE = re.compile(
    r"\.chat-msg-([A-Za-z0-9_-]+)\s*\{[^}]*?(?<![\w-])color\s*:\s*([^;}]+)",
    re.IGNORECASE | re.DOTALL,
)


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


def harvest_managed_colors(
    css: str,
    *,
    begin_marker: str,
    end_marker: str,
    legacy_marker: str,
) -> dict[str, tuple[str, str]]:
    """Extract per-user colors that this module is responsible for.

    Returns ``{lowercased_username: (original_case_username, color_value)}`` for
    rules found in **either** the auto-managed block **or** the channel's legacy
    ``legacy_marker`` rules. Hand-maintained rules elsewhere in the CSS (e.g. bot
    colors under a different comment) are intentionally **not** harvested, so
    they stay untouched.

    This lets an upgrade preserve and import colors that exist only in the CSS
    (never recorded in the database) without clobbering unrelated styling.
    """
    regions: list[str] = []

    managed = re.search(
        re.escape(begin_marker) + r"(.*?)" + re.escape(end_marker),
        css,
        re.DOTALL,
    )
    if managed:
        regions.append(managed.group(1))

    for legacy in re.finditer(
        re.escape(legacy_marker) + r"\s*(\.chat-msg-[A-Za-z0-9_-]+\s*\{[^}]*\})",
        css,
        re.DOTALL,
    ):
        regions.append(legacy.group(1))

    found: dict[str, tuple[str, str]] = {}
    for region in regions:
        for username, value in _CHAT_MSG_COLOR_RE.findall(region):
            found.setdefault(username.lower(), (username, value.strip()))
    return found


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
    protected: set[str] | None = None,
    preserve_existing: bool = True,
    selector_template: str = ".chat-msg-{username}",
    begin_marker: str = "/* BEGIN kryten-economy vanity colors — auto-managed, do not edit */",
    end_marker: str = "/* END kryten-economy vanity colors */",
    legacy_marker: str = "/* ZCoin purchased vanity colors */",
) -> str:
    """Return ``existing_css`` with a freshly-rebuilt managed vanity block.

    ``colors`` maps (lowercased) username -> ``#RRGGBB`` (typically the database
    rows). ``display_overrides`` maps lowercased username -> authoritative
    original-case username for the active purchase; it takes precedence over
    casing harvested from the CSS.

    When ``preserve_existing`` is true (the default), per-user colors already
    present in the CSS — in the managed block or in legacy ``legacy_marker``
    rules — are carried over even if they are absent from ``colors``. This makes
    an upgrade non-destructive: hand-maintained colors that were never recorded
    in the database are preserved rather than dropped. Values in ``colors``
    override harvested ones.

    ``protected`` is a set of usernames (matched case-insensitively) that the
    automation must never write, modify, or remove — bot accounts and
    manually-handled colors. A protected user is never given a database value
    and never appears in the auto-managed block. If a protected user already has
    a color in the managed block or a legacy rule, that color is preserved as a
    plain hand-maintained rule (outside the managed block) so an apply can never
    strip it. Protected colors kept in a separate hand-maintained section (not
    under ``legacy_marker``) are never harvested or stripped, so they stay
    exactly where they are.

    Entries are emitted sorted by lowercased username for a stable,
    diff-friendly result. Existing managed and legacy rules are removed first so
    the block stays the single source of truth without creating duplicates.
    Usernames that are not selector-safe are skipped.
    """
    protected_lower = {p.lower() for p in (protected or set())}
    casing = harvest_username_casing(existing_css)

    # Colors already present in the managed block or legacy rules. Used both to
    # preserve CSS-only colors through the rebuild and to protect protected
    # users' existing rules from being stripped.
    harvested = harvest_managed_colors(
        existing_css,
        begin_marker=begin_marker,
        end_marker=end_marker,
        legacy_marker=legacy_marker,
    )

    # Protected users' existing colors are preserved verbatim *outside* the
    # auto-managed block, so the automation neither removes them nor adopts them
    # into managed output.
    protected_preserved: dict[str, tuple[str, str]] = {
        lower: meta for lower, meta in harvested.items() if lower in protected_lower
    }

    # Build the effective managed color set: harvested CSS colors first
    # (preservation), then database colors override — both excluding protected.
    effective: dict[str, str] = {}
    if preserve_existing:
        for lower_user, (display, value) in harvested.items():
            if lower_user in protected_lower:
                continue
            effective[lower_user] = value
            casing.setdefault(lower_user, display)
    for user, hex_value in colors.items():
        lower_user = user.lower()
        if lower_user in protected_lower:
            continue
        effective[lower_user] = hex_value
        # The database stores canonical CyTube casing, so trust its casing for
        # EVERY managed user (not just the active buyer). When the stored key
        # actually carries case (differs from its lowercase form) it overrides
        # casing harvested from possibly-stale CSS; an all-lowercase legacy key
        # only fills in when nothing better was harvested. ``display_overrides``
        # (the buyer) still takes final precedence below.
        if user != lower_user:
            casing[lower_user] = user
        else:
            casing.setdefault(lower_user, user)

    if display_overrides:
        for lower, original in display_overrides.items():
            casing[lower.lower()] = original

    base = strip_managed_and_legacy(
        existing_css,
        begin_marker=begin_marker,
        end_marker=end_marker,
        legacy_marker=legacy_marker,
    ).rstrip()

    # Re-attach preserved protected rules as plain rules. They carry no marker,
    # so subsequent applies neither harvest nor strip them (they converge to
    # hand-maintained styling and are never touched again).
    preserved_lines: list[str] = []
    for lower_user in sorted(protected_preserved):
        display, value = protected_preserved[lower_user]
        if not is_safe_username(display):
            continue
        preserved_lines.append(
            f"{selector_template.format(username=display)} {{ color: {value}; }}"
        )
    if preserved_lines:
        comment = "/* chat colors preserved for protected users — managed manually */"
        preserved_block = comment + "\n" + "\n".join(preserved_lines)
        base = f"{base}\n\n{preserved_block}".strip() if base else preserved_block

    selectors_and_colors: list[tuple[str, str]] = []
    for lower_user, value in sorted(effective.items(), key=lambda kv: kv[0]):
        display = casing.get(lower_user, lower_user)
        if not is_safe_username(display):
            continue
        selectors_and_colors.append(
            (selector_template.format(username=display), value)
        )

    block = build_managed_block(
        selectors_and_colors,
        begin_marker=begin_marker,
        end_marker=end_marker,
    )

    if base:
        return f"{base}\n\n{block}\n"
    return f"{block}\n"
