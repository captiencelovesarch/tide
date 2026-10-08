"""Readable text: hold ``fg`` and ``dim`` above a contrast floor.

Every theme picks its own greys, and a grey that reads beautifully on
one surface is a rumour on another — the light themes are where it
bites, because a muted ``dim`` on a near-white ``bg`` (and on the
adaptive field, which normalises itself to ~0.74 luminance under a light
theme) lands somewhere around 1.9:1 and simply isn't there any more.

This module derives corrected inks from a token table. It is deliberately
a pure function of the tokens — no Qt widgets, no manager, no settings —
so it can be unit-tested on its own and called from theme composition
without dragging the UI layer into ``theming``.

The correction is a blend toward whichever pole the surface isn't, found
by bisection, which keeps as much of the theme's own hue as the floor
allows. It only ever RAISES contrast: an ink that already clears its
floor is returned untouched, so a well-tuned theme is passed through
byte-identical and the whole pass is invisible.
"""
from __future__ import annotations

from PySide6.QtGui import QColor


# WCAG 2.x contrast floors. fg carries body text, so it gets AA (4.5:1);
# dim is secondary — captions, the album/status line — and gets AA-large
# (3.0:1). Pushing dim all the way to 4.5 would erase the distinction
# between the two tokens, which is a real part of every theme's design.
MIN_FG_CONTRAST = 4.5
MIN_DIM_CONTRAST = 3.0

# Bisection depth. 12 halvings resolve the blend to ~0.02% — far finer
# than 8-bit colour can express, so the result is stable.
_STEPS = 12

_INK_FLOORS: tuple[tuple[str, float], ...] = (
    ("fg", MIN_FG_CONTRAST),
    ("dim", MIN_DIM_CONTRAST),
)

# Surfaces an ink can find itself on. Panels paint @bg / @bg_alt, and the
# adaptive field keeps itself inside the same brightness family by
# design (central_bg's _LIQ_MEAN_L_DARK / _LIQ_MEAN_L_LIGHT), so the
# worst of these two is a fair stand-in for "what's behind this text".
_SURFACE_KEYS: tuple[str, ...] = ("bg", "bg_alt")


def _linearize(channel: float) -> float:
    """sRGB → linear light, per WCAG."""
    if channel <= 0.03928:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(c: QColor) -> float:
    """WCAG relative luminance. NOT the cheap dot-product used for the
    accent picker — contrast ratios are only meaningful on linear light."""
    return (0.2126 * _linearize(c.redF())
            + 0.7152 * _linearize(c.greenF())
            + 0.0722 * _linearize(c.blueF()))


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG contrast ratio, 1.0 (identical) … 21.0 (black on white)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lo, hi = min(la, lb), max(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    """Linear RGB-space mix, ``t=0`` → a, ``t=1`` → b."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def readable_ink(ink: QColor, surface: QColor, minimum: float) -> QColor:
    """``ink`` nudged just far enough to clear ``minimum`` on ``surface``.

    Returns a copy of ``ink`` unchanged when it already passes. Otherwise
    bisects the blend toward black (on a light surface) or white (on a
    dark one) for the *smallest* shift that clears the floor — the theme
    keeps its hue wherever the floor leaves room for it.
    """
    if contrast_ratio(ink, surface) >= minimum:
        return QColor(ink)
    pole = QColor(0, 0, 0) if relative_luminance(surface) > 0.5 else QColor(255, 255, 255)
    if contrast_ratio(pole, surface) < minimum:
        # The surface is a mid-tone: neither pole can reach the floor, so
        # take the best available rather than returning something worse.
        return pole
    lo, hi = 0.0, 1.0
    best = pole
    for _ in range(_STEPS):
        mid = (lo + hi) / 2.0
        candidate = _blend(ink, pole, mid)
        if contrast_ratio(candidate, surface) >= minimum:
            best, hi = candidate, mid
        else:
            lo = mid
    return best


def _worst_surface(ink: QColor, surfaces: list[QColor]) -> QColor:
    """The surface this ink reads worst against — correcting for that one
    also carries the others, since they share a brightness family."""
    return min(surfaces, key=lambda s: contrast_ratio(ink, s))


def readable_text_tokens(tokens: dict) -> dict[str, str]:
    """Corrections for ``fg`` / ``dim`` in ``tokens``, as ``{key: hex}``.

    Only keys that actually needed moving appear in the result, so an
    empty dict means "this palette was already readable" and the caller
    can skip its restyle entirely.
    """
    surfaces = []
    for key in _SURFACE_KEYS:
        c = QColor(str(tokens.get(key, "")))
        if c.isValid():
            surfaces.append(c)
    if not surfaces:
        return {}

    out: dict[str, str] = {}
    for key, floor in _INK_FLOORS:
        ink = QColor(str(tokens.get(key, "")))
        if not ink.isValid():
            continue
        fixed = readable_ink(ink, _worst_surface(ink, surfaces), floor)
        if fixed.name() != ink.name():
            out[key] = fixed.name()
    return out


# ---------- local inks: text against the patch of backdrop behind it ----------
#
# The token pass above holds fg / dim against the theme's flat surfaces.
# The adaptive backdrop is not flat: a cover can push one corner of the
# window to saturated red while another stays near black, and a grey that
# reads fine on the dark side is gone on the red one. ui/legibility.py asks
# CentralBg for the luminance range under each run of text and hands it to
# legible_ink() below, which moves just that run's ink.
#
# The measure here is APCA (the WCAG 3 draft) rather than the WCAG 2 ratio,
# because the two disagree exactly where this matters: WCAG 2 rates black on
# pure red above white on pure red, and would flip a white title to black
# the moment a red cover came up. APCA rates white on red well and black on
# red poorly, which is what an eye says too.

# APCA-W3 0.0.98G-4g constants.
_APCA_COEF = (0.2126729, 0.7151522, 0.0721750)
_APCA_BLK_THRS = 0.022
_APCA_BLK_CLMP = 1.414
_APCA_SCALE = 1.14
_APCA_OFFSET = 0.027
_APCA_LO_CLIP = 0.1
_APCA_DELTA_Y_MIN = 0.0005

# Per-channel screen luminance, indexed by the 8-bit value. CentralBg
# builds its luminance grid from the same tables, so a probe and an ink
# are measured on one scale.
APCA_LUT: tuple[tuple[float, ...], ...] = tuple(
    tuple(k * (i / 255.0) ** 2.4 for i in range(256)) for k in _APCA_COEF
)

# How far a local ink is allowed to sag before it moves, as a share of the
# contrast the theme designed it with (ink on its own bg). Under the ratio
# nothing changes, so text on a patch about as dark as the theme's bg keeps
# the theme's exact colour. The design is capped first: nobody needs white
# on black's 106 everywhere, 75 is already body-text territory.
LOCAL_OK_SHARE = 0.7
LOCAL_DESIGN_CAP = 75.0
# Where an ink that has lost everything gets pulled back to. Between the
# two thresholds the target ramps smoothly, so a run that is only a little
# short moves a little, and one sitting on its own colour moves a lot.
# fg-like inks (high design) land at the top, dim and accent at the bottom.
LOCAL_FLOOR_MIN = 40.0
LOCAL_FLOOR_MAX = 60.0
LOCAL_FLOOR_SHARE = 0.6
# How much more blend the other direction must cost before an ink that went
# light goes dark (or back). Without it, text on a patch where both work
# about equally would flip on every small drift of the backdrop.
LOCAL_FLIP_STICK = 0.15
# When neither pole reaches the target, how much better (in Lc) the far
# pole has to be before an ink crosses over to it.
LOCAL_CROSS_MARGIN = 10.0

_WHITE = QColor(255, 255, 255)
_BLACK = QColor(0, 0, 0)


def apca_y(c: QColor) -> float:
    """APCA screen luminance of an opaque colour, 0..1."""
    r, g, b = APCA_LUT
    return r[c.red()] + g[c.green()] + b[c.blue()]


def _soft_clamp(y: float) -> float:
    y = max(0.0, y)
    if y >= _APCA_BLK_THRS:
        return y
    return y + (_APCA_BLK_THRS - y) ** _APCA_BLK_CLMP


def apca_lc(y_text: float, y_bg: float) -> float:
    """Signed APCA lightness contrast. Positive: dark text on a lighter
    background. Negative: light text on a darker one. 0 when the two are
    too close to read at all. Magnitudes: ~15 invisible, 30 the least any
    text should have, 45 headlines, 60 content text, 75 body text."""
    yt, yb = _soft_clamp(y_text), _soft_clamp(y_bg)
    if abs(yb - yt) < _APCA_DELTA_Y_MIN:
        return 0.0
    if yb > yt:
        sapc = (yb ** 0.56 - yt ** 0.57) * _APCA_SCALE
        return 0.0 if sapc < _APCA_LO_CLIP else (sapc - _APCA_OFFSET) * 100.0
    sapc = (yb ** 0.65 - yt ** 0.62) * _APCA_SCALE
    return 0.0 if sapc > -_APCA_LO_CLIP else (sapc + _APCA_OFFSET) * 100.0


def worst_lc(y_text: float, y_lo: float, y_hi: float) -> float:
    """The weakest |Lc| an ink of luminance ``y_text`` has anywhere on a
    backdrop spanning ``y_lo..y_hi``. Inside the span it collides with
    some part of it, so that is 0."""
    if y_text < y_lo:
        return abs(apca_lc(y_text, y_lo))
    if y_text > y_hi:
        return abs(apca_lc(y_text, y_hi))
    return 0.0


def _solve_toward(ink: QColor, pole: QColor, measure, target: float
                  ) -> tuple[float | None, float]:
    """Smallest blend ``t`` of ``ink`` toward ``pole`` whose ``measure``
    clears ``target``. ``measure`` rises with ``t``. Returns (t or None
    when even the pole falls short, the pole's own measure)."""
    at_pole = measure(apca_y(pole))
    if at_pole < target:
        return None, at_pole
    lo, hi = 0.0, 1.0
    for _ in range(_STEPS):
        mid = (lo + hi) / 2.0
        if measure(apca_y(_blend(ink, pole, mid))) >= target:
            hi = mid
        else:
            lo = mid
    return hi, at_pole


def legible_ink(ink: QColor, y_lo: float, y_hi: float, y_ref: float,
                prefer: int = 0) -> tuple[QColor, int]:
    """``ink`` as it should be drawn over a backdrop spanning luminance
    ``y_lo..y_hi``, for a theme whose own background is ``y_ref``.

    Returns (colour, side): side 0 means unchanged, +1 moved toward white,
    -1 toward black. Pass the last side back as ``prefer`` and the ink
    stays on it unless the other is clearly cheaper (LOCAL_FLIP_STICK).

    An ink is judged in the polarity it was designed in (a dark theme's
    inks are light on dark). Reading the other way round only counts once
    it clears the floor: dim grey that ends up a shade darker than a red
    patch is not "dim text", it is mud. The move is the smallest blend
    toward one pole that reaches the target, so the ink keeps as much of
    its hue as the patch allows.
    """
    y_ink = apca_y(ink)
    designed = abs(apca_lc(y_ink, y_ref))
    ok = min(designed, LOCAL_DESIGN_CAP) * LOCAL_OK_SHARE
    if ok <= 0.0:
        # Unreadable on its own theme too: a decoration, not text to save.
        return QColor(ink), 0
    floor = max(ok, min(LOCAL_FLOOR_MAX,
                        max(LOCAL_FLOOR_MIN, designed * LOCAL_FLOOR_SHARE)))

    # Light-on-dark only ever reads against the brightest point of the
    # patch, dark-on-light only against the dimmest.
    def up(y: float) -> float:
        return max(0.0, -apca_lc(y, y_hi))

    def down(y: float) -> float:
        return max(0.0, apca_lc(y, y_lo))

    home = 1 if y_ink >= y_ref else -1
    same, other = (up, down) if home > 0 else (down, up)
    have = same(y_ink)
    if have >= ok or other(y_ink) >= floor:
        return QColor(ink), 0

    d = 1.0 - have / ok
    target = ok + (floor - ok) * d * d * (3.0 - 2.0 * d)
    home_pole, away_pole = (_WHITE, _BLACK) if home > 0 else (_BLACK, _WHITE)
    t_home, best_home = _solve_toward(ink, home_pole, same, target)
    t_away, best_away = _solve_toward(ink, away_pole, other, floor)

    if t_home is None and t_away is None:
        # A mid-tone patch neither pole clears (white and black both land
        # around 50 on orange). Staying home costs the least: the ink
        # only crosses over when the far pole is clearly better.
        if prefer == -home:
            side = -home if best_away + LOCAL_CROSS_MARGIN >= best_home else home
        else:
            side = home if best_home + LOCAL_CROSS_MARGIN >= best_away else -home
        return QColor(home_pole if side == home else away_pole), side
    if t_away is None:
        side = home
    elif t_home is None:
        side = -home
    else:
        # Both work: the cheaper move, home on a tie, and whichever side
        # the ink was already on unless the other saves a real amount.
        t_side = {home: t_home, -home: t_away}
        side = home if t_home <= t_away else -home
        if prefer in t_side and t_side[prefer] <= t_side[-prefer] + LOCAL_FLIP_STICK:
            side = prefer
    if side == home:
        return _blend(ink, home_pole, t_home), side
    return _blend(ink, away_pole, t_away), side
