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
