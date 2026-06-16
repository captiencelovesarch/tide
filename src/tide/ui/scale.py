"""UI scale.

Single source of truth for "how big" the UI is. A scale factor multiplies
the theme's ``typography.size_pt`` (which the QApplication font and QSS
``font-size`` substitution both read from), so every widget that uses
``self.font()`` / ``QFontMetrics`` for its measurements picks up the new
size automatically — list rows, label heights, button text, custom paints.

A small set of widgets has hardcoded pixel sizes that don't derive from
font metrics (album art tile, now-playing strip min-height). They go
through ``px(base)`` to multiply at construction and re-read on
``theme_changed`` (the theme manager re-emits that signal after a scale
change, which keeps fixed-size widgets in sync without a separate
notification path).

Public surface:
  * ``Scale`` — preset enum
  * ``factor()`` — current multiplier
  * ``set_factor(value)`` — push a new value (does NOT auto-reapply theme;
    caller does that to control the order of operations)
  * ``px(base, *, minimum=1)`` — scale a fixed integer pixel size
  * ``round_pt(base_pt)`` — scale a font point size, rounded to nearest int
"""
from __future__ import annotations

from enum import Enum


class Scale(str, Enum):
    """Named scale presets. Values are the string keys persisted in
    ``settings.toml`` (``ui_scale``)."""

    COMPACT = "compact"
    NORMAL = "normal"
    LARGE = "large"
    HUGE = "huge"

    @classmethod
    def parse(cls, value) -> "Scale":
        if isinstance(value, cls):
            return value
        if not value:
            return cls.NORMAL
        v = str(value).strip().lower()
        for s in cls:
            if s.value == v:
                return s
        return cls.NORMAL


# Preset multipliers. Tuned to be visually distinct without breaking layout
# at the extremes:
#   compact 0.85 — denser, still legible on small / hidpi screens
#   normal  1.00 — baseline (designed-against)
#   large   1.15 — comfortable on a typical 1440p desktop
#   huge    1.30 — for accessibility / 4K-from-couch viewing
_FACTORS: dict[Scale, float] = {
    Scale.COMPACT: 0.85,
    Scale.NORMAL: 1.00,
    Scale.LARGE: 1.15,
    Scale.HUGE: 1.30,
}


_current: Scale = Scale.NORMAL


def current() -> Scale:
    """Currently-selected preset."""
    return _current


def factor() -> float:
    """Current multiplier as a float (e.g. 1.15 for LARGE)."""
    return _FACTORS[_current]


def set_factor(value) -> None:
    """Push a new scale. The caller is responsible for any UI refresh
    (typically: re-applying the active theme so the QApplication font and
    QSS substitution pick up the new factor).
    """
    global _current
    _current = Scale.parse(value)


def px(base: int, *, minimum: int = 1) -> int:
    """Scale a base pixel size, clamped to a minimum so tiny widgets don't
    collapse at COMPACT. Rounded to nearest int (no fractional pixels —
    Qt accepts them on QPainter but treats them inconsistently in
    layout)."""
    return max(minimum, int(round(base * factor())))


def round_pt(base_pt: float) -> int:
    """Scale a base font point size, rounded to nearest int with a floor of
    1pt so the font doesn't disappear at extreme low scales."""
    return max(1, int(round(base_pt * factor())))


def margins(left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int]:
    """Convenience for ``setContentsMargins(*scale.margins(16, 14, 16, 8))``
    — every value scales through ``px``. Keep the arg order matching Qt's
    setter so the call site reads naturally."""
    return px(left), px(top), px(right), px(bottom)
