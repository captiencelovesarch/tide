"""modern's icon sets (2.3).

Every set answers the same keys as ``icons/svg`` (the "regular" set,
which shipped alone through 2.2). Three are derived on load, three are
drawn:

  * ``thin`` / ``regular`` / ``bold`` — the regular outlines at three
    stroke weights.
  * ``filled`` — solid shapes (``icons/sets/filled``); keys it doesn't
    draw fall back to the regular outline at a heavier stroke.
  * ``duotone`` — the filled shape as a faint underlay beneath the
    regular outline, both in the same ink.
  * ``rounded`` — chunkier, softer shapes (``icons/sets/rounded``);
    undrawn keys fall back to the regular outline, rounder stroke.

The active set is process state (:func:`set_active`), like the glyph
packs: buttons resolve their SVG at restyle time through
:func:`svg_for`, so a set change lands with one theme re-emit. The
returned text still says ``currentColor``; callers ink it.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"
REGULAR_DIR = ICONS_DIR / "svg"
SETS_DIR = ICONS_DIR / "sets"

SETS: tuple[str, ...] = ("regular", "thin", "bold", "filled", "duotone",
                         "rounded")
LABELS: dict[str, str] = {
    "regular": "regular · the outlines tide ships",
    "thin": "thin · hairline outlines",
    "bold": "bold · heavy outlines",
    "filled": "filled · solid shapes",
    "duotone": "duotone · outline over a soft fill",
    "rounded": "rounded · chunky and soft",
}
# Root stroke width per set; None = leave the file's own.
_STROKE: dict[str, str | None] = {
    "regular": None,
    "thin": "1.25",
    "bold": "2.5",
    "filled": "2.3",
    "duotone": None,
    "rounded": "2.3",
}
# Keys the sample rows in the picker show, in order.
SAMPLE_KEYS: tuple[str, ...] = ("shuffle", "prev", "play", "next", "repeat",
                                "like_on", "audio_fx", "sleep", "fullscreen")

_active = "regular"

_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.S)
_SW_RE = re.compile(r'(<svg\b[^>]*?\bstroke-width=")([^"]*)(")', re.S)


def set_active(name: str) -> bool:
    """Switch the active set; unknown names fall back to regular.
    True when it changed."""
    global _active
    name = name if name in SETS else "regular"
    if name == _active:
        return False
    _active = name
    return True


def active() -> str:
    return _active


@lru_cache(maxsize=None)
def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _regular(key: str) -> str | None:
    if not key or "/" in key or key.startswith("."):
        return None
    return _read(str(REGULAR_DIR / f"{key}.svg"))


def _drawn(set_name: str, key: str) -> str | None:
    if not key or "/" in key or key.startswith("."):
        return None
    return _read(str(SETS_DIR / set_name / f"{key}.svg"))


def _restroke(svg: str, width: str | None) -> str:
    if not width:
        return svg
    if _SW_RE.search(svg):
        return _SW_RE.sub(lambda m: m.group(1) + width + m.group(3), svg, count=1)
    return _ROOT_RE.sub(lambda m: m.group(0)[:-1] + f' stroke-width="{width}">',
                        svg, count=1)


def _body(svg: str) -> str:
    """Everything between the root tags."""
    start = _ROOT_RE.search(svg)
    end = svg.rfind("</svg>")
    if start is None or end < 0:
        return ""
    return svg[start.end():end]


@lru_cache(maxsize=None)
def _resolve(set_name: str, key: str) -> str | None:
    regular = _regular(key)
    if set_name == "regular":
        return regular
    if set_name in ("thin", "bold"):
        return _restroke(regular, _STROKE[set_name]) if regular else None
    if set_name == "duotone":
        if regular is None:
            return None
        under = _drawn("filled", key)
        if under is None:
            return regular
        # The filled shape, faint, then the outline over it. fill-/
        # stroke-opacity on the group (not ``opacity``): QtSvg renders
        # SVG Tiny, which has the per-paint opacities.
        layer = (f'<g fill-opacity="0.32" stroke-opacity="0.32">'
                 f'{_body(under)}</g>')
        root = _ROOT_RE.search(regular)
        return regular[:root.end()] + layer + regular[root.end():]
    drawn = _drawn(set_name, key)
    if drawn is not None:
        return drawn
    return _restroke(regular, _STROKE.get(set_name)) if regular else None


def svg_for(key: str, set_name: str | None = None) -> str | None:
    """The SVG text for ``key`` in ``set_name`` (default: the active
    set), or None when no icon exists for the key in any set."""
    name = set_name if set_name in SETS else _active
    return _resolve(name, key)


def keys() -> tuple[str, ...]:
    """Every key the regular set draws."""
    return tuple(sorted(p.stem for p in REGULAR_DIR.glob("*.svg")))
