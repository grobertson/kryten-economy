"""Perceptual contrast scoring for chat-color readability guards.

Chat colors render as light text on a near-black chat background. A single
textbook metric is not enough here, because two *different* failure modes make a
color hard to read on black:

1. **Low lightness contrast** — dark colors (maroon, navy, dark red) barely
   separate from the background. WCAG 2.x ratio handles this poorly; APCA — the
   Accessible Perceptual Contrast Algorithm adopted for WCAG 3 — models perceived
   lightness contrast and flags these correctly.
2. **Chromatic / acuity failure** — a *bright* but near-monochromatic red
   (``#FF0000``: full red, zero green & blue) has plenty of APCA contrast yet
   reads badly on black: long-wavelength red focuses behind the retina, so red
   text on a dark field shimmers and is fatiguing. APCA alone cannot see this —
   pure red (Lc ~37) scores almost identically to hot pink (Lc ~40), but only
   one of them is actually comfortable to read.

The empirical separator between "bad red" and "good pink" is the amount of
*non-red* channel content (green + blue): pinks/corals carry blue and read fine;
pure/dark reds have little or none. We therefore combine the two scales into one
**readability score**::

    score = apca_Lc(text, bg)  ×  chroma_factor(text)

where ``chroma_factor`` ramps 0→1 as ``green + blue`` rises from 0 to
:data:`CHROMA_KNEE`. This penalty only ever bites red-dominant colors (any other
hue already has high G+B), so it surgically blocks harsh reds while leaving every
normal color's score untouched. Callers compare the single combined score against
configurable thresholds.

Pure functions, no I/O — trivially unit-testable and mirrored in the
kryten-webqueue dashboard for a live preview that agrees with the server.
"""

from __future__ import annotations

# APCA-W3 (0.1.9) constants, sRGB.
_MAIN_TRC = 2.4
_R_CO = 0.2126729
_G_CO = 0.7151522
_B_CO = 0.0721750
_NORM_BG = 0.56
_NORM_TXT = 0.57
_REV_TXT = 0.62
_REV_BG = 0.65
_BLK_THRS = 0.022
_BLK_CLMP = 1.414
_SCALE_BOW = 1.14
_SCALE_WOB = 1.14
_LO_BOW_OFFSET = 0.027
_LO_WOB_OFFSET = 0.027
_DELTA_Y_MIN = 0.0005
_LO_CLIP = 0.1

# Green+blue sum (0..510) at/above which a color incurs no chroma penalty. Below
# it the APCA score is scaled down linearly. 160 was chosen empirically: it
# blocks pure red (#FF0000, G+B=0) and dark/red-orange reds while leaving crimson
# the only palette hue affected, and passes every pink/coral (G+B >= ~167).
CHROMA_KNEE = 160

# Outcome levels for a candidate colour.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_REJECT = "reject"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse ``#RGB`` or ``#RRGGBB`` (with/without ``#``) to an (r, g, b) tuple."""
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"Invalid hex colour: {value!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _screen_luminance(value: str) -> float:
    """APCA screen luminance Y for an sRGB hex colour (simple 2.4 exponent)."""
    r, g, b = _hex_to_rgb(value)
    return (
        _R_CO * (r / 255.0) ** _MAIN_TRC
        + _G_CO * (g / 255.0) ** _MAIN_TRC
        + _B_CO * (b / 255.0) ** _MAIN_TRC
    )


def apca_contrast(text_hex: str, bg_hex: str) -> float:
    """Return the signed APCA Lc for ``text_hex`` over ``bg_hex``.

    Positive => dark text on a light background (normal polarity); negative =>
    light text on a dark background (reverse polarity — the chat case).
    """
    ytxt = _screen_luminance(text_hex)
    ybg = _screen_luminance(bg_hex)

    # Soft-clamp very dark colours so tiny luminance differences near black don't
    # explode the contrast estimate.
    if ytxt <= _BLK_THRS:
        ytxt += (_BLK_THRS - ytxt) ** _BLK_CLMP
    if ybg <= _BLK_THRS:
        ybg += (_BLK_THRS - ybg) ** _BLK_CLMP

    if abs(ybg - ytxt) < _DELTA_Y_MIN:
        return 0.0

    if ybg > ytxt:  # dark text on light bg
        sapc = (ybg ** _NORM_BG - ytxt ** _NORM_TXT) * _SCALE_BOW
        output = 0.0 if sapc < _LO_CLIP else sapc - _LO_BOW_OFFSET
    else:  # light text on dark bg (our chat case)
        sapc = (ybg ** _REV_BG - ytxt ** _REV_TXT) * _SCALE_WOB
        output = 0.0 if sapc > -_LO_CLIP else sapc + _LO_WOB_OFFSET

    return output * 100.0


def readability(text_hex: str, bg_hex: str) -> float:
    """Absolute APCA Lc for ``text_hex`` over ``bg_hex`` — higher is more readable."""
    return abs(apca_contrast(text_hex, bg_hex))


def chroma_factor(text_hex: str, *, knee: int = CHROMA_KNEE) -> float:
    """Penalty multiplier (0..1) for near-monochromatic red colours.

    Ramps linearly from 0 to 1 as the colour's ``green + blue`` content rises
    from 0 to ``knee``. A red-dominant colour (low G+B) is pushed toward 0; any
    colour carrying meaningful green/blue (every non-red hue) gets ~1.0 and is
    left unpenalised.
    """
    _r, g, b = _hex_to_rgb(text_hex)
    if knee <= 0:
        return 1.0
    return min(g + b, knee) / float(knee)


def readability_score(
    text_hex: str, bg_hex: str, *, knee: int = CHROMA_KNEE
) -> float:
    """Combined readability score: APCA Lc scaled by the chroma penalty.

    This is the single number the guard and UI threshold against. It is high only
    when a colour is *both* light enough (APCA) *and* not a harsh near-mono red
    (chroma). Returns a value rounded to one decimal.
    """
    return round(readability(text_hex, bg_hex) * chroma_factor(text_hex, knee=knee), 1)


def classify_contrast(
    text_hex: str,
    bg_hex: str,
    *,
    min_lc: float,
    warn_lc: float,
    knee: int = CHROMA_KNEE,
) -> tuple[str, float]:
    """Classify a candidate colour's readability against ``bg_hex``.

    Returns ``(level, score)`` where ``level`` is one of :data:`LEVEL_OK`,
    :data:`LEVEL_WARN`, or :data:`LEVEL_REJECT`, and ``score`` is the combined
    :func:`readability_score`. ``score < min_lc`` rejects (unreadable);
    ``score < warn_lc`` warns (readable but hard for low vision); otherwise ok.
    """
    score = readability_score(text_hex, bg_hex, knee=knee)
    if score < min_lc:
        return LEVEL_REJECT, score
    if score < warn_lc:
        return LEVEL_WARN, score
    return LEVEL_OK, score


def evaluate_color(
    text_hex: str,
    bg_hex: str,
    *,
    min_lc: float,
    warn_lc: float,
    knee: int = CHROMA_KNEE,
) -> dict:
    """Full readability verdict for a colour, for the preview/check API.

    Returns ``{hex, lc, score, level, acceptable, message}`` where ``lc`` is the
    raw APCA Lc, ``score`` the combined score, ``level`` the classification, and
    ``acceptable`` is False only when rejected.
    """
    lc = round(readability(text_hex, bg_hex), 1)
    level, score = classify_contrast(
        text_hex, bg_hex, min_lc=min_lc, warn_lc=warn_lc, knee=knee
    )
    if level == LEVEL_REJECT:
        # Tailor the hint to the failure mode: a near-mono red that APCA alone
        # would pass (good lc, chroma killed it) vs. a genuinely too-dark colour.
        if lc >= min_lc and chroma_factor(text_hex, knee=knee) < 1.0:
            message = (
                "Pure/near-pure red is hard to read on the dark chat background. "
                "Add a little green or blue — e.g. pink or coral instead of red."
            )
        else:
            message = (
                "That color is too dark to read on the dark chat background. "
                "Try a lighter shade."
            )
    elif level == LEVEL_WARN:
        message = (
            "That color is readable but low-contrast — viewers with vision "
            "issues may have trouble reading your chat lines."
        )
    else:
        message = ""
    return {
        "hex": text_hex,
        "lc": lc,
        "score": score,
        "level": level,
        "acceptable": level != LEVEL_REJECT,
        "message": message,
    }

