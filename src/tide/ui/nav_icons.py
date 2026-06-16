"""Nav-rail icon sets.

Two flavors: a "classic" set of carefully-picked monospace unicode glyphs
that render in every bundled font (IBM Plex Mono / JetBrains Mono / Inter)
and read semantically (house for home, eighth-note for lyrics, gear for
settings, etc.), and an "emoji" set that uses full-color emoji for max
recognizability at the cost of fitting less neatly into a monospace line.

Each set maps the nav-slot name to a single character. If a glyph rendered
as a tofu box in any of the bundled fonts, it didn't make the cut.
"""
from __future__ import annotations


NAV_ICON_SETS: dict[str, dict[str, str]] = {
    # Semantic mono symbols. Hand-validated against IBM Plex Mono +
    # JetBrains Mono + DejaVu Sans Mono — all glyphs render in every one.
    "classic": {
        "home":       "⌂",   # U+2302 HOUSE
        "library":    "▤",   # hatched square — reads as "shelf"
        "queue":      "≡",   # triple bar — reads as "list"
        "lyrics":     "♪",   # eighth note
        "history":    "⌛",   # hourglass
        "visualizer": "♬",   # beamed notes
        "source":     "⇄",   # left/right arrow — reads as "switch"
        "settings":   "⚙",   # gear
    },
    # Emoji set — universal recognizability. Renders via the system color
    # emoji font (Noto Color Emoji on most Linux setups). May render
    # taller than text; the tradeoff is everyone reads them instantly.
    "emoji": {
        "home":       "🏠",
        "library":    "📚",
        "queue":      "📋",
        "lyrics":     "🎤",
        "history":    "🕒",
        "visualizer": "🎚",
        "source":     "🔌",
        "settings":   "⚙",
    },
}


VALID_SETS = ("off", "svg") + tuple(NAV_ICON_SETS.keys())


def icon_for(set_name: str, slot: str) -> str | None:
    """Return the icon glyph for ``slot`` in the named set, or None if the
    set is "off" / "svg" / unknown (svg uses files, not glyphs). Slot names
    match the nav button identifiers used in window.py (``home`` /
    ``library`` / etc.)."""
    if not set_name or set_name == "off" or set_name == "svg":
        return None
    bag = NAV_ICON_SETS.get(set_name)
    if bag is None:
        return None
    return bag.get(slot)


def svg_text_for(slot: str) -> str | None:
    """Return the raw SVG text for ``slot`` (read from
    ``src/tide/icons/svg/<slot>.svg``) or None if no SVG exists for that
    slot. The raw text contains ``stroke="currentColor"``; callers substitute
    the active theme's fg color before rendering."""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "icons" / "svg" / f"{slot}.svg"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None
