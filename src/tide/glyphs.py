"""Transport glyph registry: named packs map glyph keys to strings,
``glyph()`` resolves user override → active pack → default pack,
``set_overrides()`` is the glyph editor's hook.

The "default" pack is a unicode-precise transcription of the shipped
glyphs (tests pin it). Chosen the hard way: pause is ▮▮ (U+25AE ×2), not
⏸ — U+23F8 lives only in symbol/emoji fallback fonts whose metrics ride
above the baseline, so it floated over its neighbors; U+25AE shares
Geometric Shapes with ▶, same font, same baseline (window.py's war
story). heading_dash is ─ (U+2500), the ``── heading ────`` rule char.
Registered packs must be complete — the default-pack fallback only
matters for overrides cleared one at a time in the editor.
"""
from __future__ import annotations


# the full vocabulary — packs must cover every key
KEYS: tuple[str, ...] = (
    "play",
    "pause",
    "loading",
    "prev",
    "next",
    "shuffle",
    "repeat",
    "repeat_one",
    "like_on",
    "like_off",
    "sleep",
    "fullscreen",
    "heading_dash",
)

# call sites: window.py's buttons, variants.py, mini/fullscreen _on_state
DEFAULT_PACK: dict[str, str] = {
    "play": "▶",            # U+25B6
    "pause": "▮▮",          # U+25AE ×2 — NOT ⏸, see module docstring
    "loading": "…",         # U+2026
    "prev": "◂◂",           # U+25C2 ×2
    "next": "▸▸",           # U+25B8 ×2
    "shuffle": "⇋",         # U+21CB
    "repeat": "↻",          # U+21BB
    "repeat_one": "↻¹",     # U+21BB U+00B9 — the superscript says WHICH
    "like_on": "♥",         # U+2665
    "like_off": "♡",        # U+2661
    "sleep": "zzz",
    "fullscreen": "⤢",      # U+2922
    "heading_dash": "─",    # U+2500
}


_packs: dict[str, dict[str, str]] = {"default": dict(DEFAULT_PACK)}
_active: str = "default"
# user overrides layered over the active pack (the glyph editor writes)
_overrides: dict[str, str] = {}


def glyph(key: str) -> str:
    """Resolve override → active pack → default pack. KeyError outside
    the vocabulary — a call-site programming error, not user data."""
    if key not in DEFAULT_PACK:
        raise KeyError(key)
    if key in _overrides:
        return _overrides[key]
    pack = _packs[_active]
    if key in pack:
        return pack[key]
    return DEFAULT_PACK[key]


def set_pack(name: str) -> None:
    """Switch the active pack. KeyError on an unregistered name so a
    typo'd preset field surfaces instead of silently drawing defaults."""
    global _active
    if name not in _packs:
        raise KeyError(name)
    _active = name


def active_pack() -> str:
    return _active


def packs() -> tuple[str, ...]:
    """Registered pack names, registration order."""
    return tuple(_packs)


def register_pack(name: str, mapping: dict[str, str]) -> None:
    """Add or replace a named pack. Must be complete — a missing key
    would fall back per-glyph and look like a bug in whichever surface
    hit it. Extra keys are allowed so a newer pack file still loads."""
    missing = [k for k in KEYS if k not in mapping]
    if missing:
        raise ValueError(f"glyph pack {name!r} missing keys: {missing}")
    _packs[name] = dict(mapping)


def set_overrides(mapping: dict[str, str]) -> None:
    """Replace the override layer wholesale. Unknown keys are dropped —
    persisted state from another version must never crash."""
    global _overrides
    _overrides = {k: v for k, v in dict(mapping).items() if k in DEFAULT_PACK}
