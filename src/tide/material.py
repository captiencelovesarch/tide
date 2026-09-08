"""Derived material tokens: surface tiers, an outline, the type scale.

Every theme ships four greys and an accent, and that is all a theme
author should have to think about. The modern personality wants more
than that to build with: something to lift a card off the backdrop, a
hairline that reads on OLED black and on paper alike, a heading size
above body. Rather than ask sixteen bundled themes (and every third-party
one) to declare those, they are derived here from what the theme already
says.

Surfaces are translucent overlays, not opaque colours: white at a few
percent on a dark ground, black on a light one. Two properties fall out
of that for free. The adaptive backdrop shows through a panel instead of
being hidden by it, and a near-black theme like blackwater gets material
that is actually visible, which its own ``bg_alt`` (four percent above
pure black) is not.

Pure functions of the token table. No Qt widgets, no manager: testable
on their own, and callable from ``theming`` at compose time. A theme
that declares any of these keys in ``[tokens]`` wins over the derivation
(``theming.Theme.token`` and ``theming._substitute`` both consult the
declared table first).
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtGui import QColor

from . import contrast


# Keys this module can supply. Declared tokens shadow them.
SURFACE_KEYS: tuple[str, ...] = ("surface_1", "surface_2", "surface_3", "outline")
TYPE_KEYS: tuple[str, ...] = (
    "font_size_title", "font_size_display", "weight_title", "weight_display",
)
DERIVED_KEYS: tuple[str, ...] = SURFACE_KEYS + TYPE_KEYS

# Overlay alphas per tier. Dark grounds take a touch more because a white
# veil loses more to gamma than a black one does; both ladders were set by
# eye against blackwater (#000), adaptive (#0a0a0a), nord (#2e3440) and
# paper (#f4efe6) so tier 1 is just visible and tier 3 is clearly a
# surface on all four.
_DARK_ALPHAS = {"surface_1": 0.05, "surface_2": 0.09, "surface_3": 0.14,
                "outline": 0.10}
_LIGHT_ALPHAS = {"surface_1": 0.04, "surface_2": 0.07, "surface_3": 0.11,
                 "outline": 0.12}

# Type scale. Title is section headings and the now-playing title; display
# is the home greeting. Ratios, not points, so a theme at 10pt and one at
# 12pt keep the same proportions.
TITLE_SCALE = 1.25
DISPLAY_SCALE = 1.7
WEIGHT_TITLE = 500
WEIGHT_DISPLAY = 600


def is_dark(bg: str, fallback: bool = True) -> bool:
    """Polarity of a ground colour: True when it is dark. Falls back to
    the theme's own ``dark`` flag when ``bg`` doesn't parse."""
    c = QColor(str(bg or ""))
    if not c.isValid():
        return bool(fallback)
    return contrast.relative_luminance(c) < 0.5


def _argb(alpha: float, white: bool) -> str:
    a = max(0, min(255, round(alpha * 255)))
    rgb = "ffffff" if white else "000000"
    return f"#{a:02x}{rgb}"


@lru_cache(maxsize=64)
def surface_tokens(bg: str, dark_fallback: bool = True) -> dict[str, str]:
    """Surface tiers + outline for a ground colour, as ``#AARRGGBB``.

    The format parses in both places a token lands: Qt stylesheets accept
    ``#aarrggbb`` and so does ``QColor(str)``, so custom painters and QSS
    see the same value. Cached: ``Theme.token`` is a paint-time call.
    """
    dark = is_dark(bg, dark_fallback)
    alphas = _DARK_ALPHAS if dark else _LIGHT_ALPHAS
    return {key: _argb(alphas[key], white=dark) for key in SURFACE_KEYS}


def title_pt(base_pt: float) -> int:
    """Title size in points at the current UI scale."""
    from .ui import scale as _scale
    return max(1, _scale.round_pt(float(base_pt) * TITLE_SCALE))


def display_pt(base_pt: float) -> int:
    """Display size in points at the current UI scale."""
    from .ui import scale as _scale
    return max(1, _scale.round_pt(float(base_pt) * DISPLAY_SCALE))


def type_tokens(base_pt: float) -> dict[str, str]:
    """QSS-ready type tokens for a theme's base point size."""
    return {
        "font_size_title": f"{title_pt(base_pt)}pt",
        "font_size_display": f"{display_pt(base_pt)}pt",
        "weight_title": str(WEIGHT_TITLE),
        "weight_display": str(WEIGHT_DISPLAY),
    }


def derived(tokens: dict, dark_fallback: bool = True) -> dict[str, str]:
    """Every derivable token for ``tokens`` that the table doesn't declare.

    Surfaces come from ``bg``; the type scale is deliberately NOT here
    (it depends on the UI scale, which is runtime state, so ``theming``
    adds it at substitution time).
    """
    out: dict[str, str] = {}
    for key, value in surface_tokens(str(tokens.get("bg", "")), dark_fallback).items():
        if not str(tokens.get(key, "")).strip():
            out[key] = value
    return out


def composite(ground: str, overlay: str) -> QColor:
    """``overlay`` (any alpha) flattened onto ``ground`` as a solid colour.
    For surfaces that must be opaque: a popup window over the desktop
    cannot borrow the backdrop the way an in-window panel does, so it
    takes the tier's look as one flat colour instead."""
    g = QColor(str(ground or ""))
    o = QColor(str(overlay or ""))
    if not g.isValid():
        g = QColor(0, 0, 0)
    if not o.isValid():
        return g
    a = o.alphaF()
    return QColor(
        round(g.red() * (1 - a) + o.red() * a),
        round(g.green() * (1 - a) + o.green() * a),
        round(g.blue() * (1 - a) + o.blue() * a),
    )


def panel_colors(theme) -> tuple[QColor, QColor]:
    """(fill, outline) for an opaque popover panel under ``theme``: the
    second surface tier and the outline, each flattened onto bg."""
    bg = theme.token("bg", "#0b0b0b") if theme is not None else "#0b0b0b"
    tier = theme.token("surface_2", "#17ffffff") if theme is not None else "#17ffffff"
    line = theme.token("outline", "#1affffff") if theme is not None else "#1affffff"
    return composite(bg, tier), composite(bg, line)
