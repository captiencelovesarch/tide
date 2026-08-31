"""Transport glyph vocabulary as data.

Every transport glyph in the app is currently hand-typed at its call site
— the main window strip, the ControlsBundle variants, the mini player and
the fullscreen player each carry their own copy of ▶ / ▮▮ / ↻¹ / ♥. This
registry makes the vocabulary one piece of data: named packs map glyph
keys to strings, ``glyph()`` resolves through a user-override layer, and
``set_overrides()`` is the hook the Phase-2 glyph editor rides.

The "default" pack is a verbatim transcription of the shipped glyphs —
unicode-precise, because some of these were chosen the hard way:

- pause is ▮▮ (U+25AE ×2), not ⏸ — U+23F8 lives only in symbol/emoji
  fallback fonts whose metrics ride above the baseline, so the pause
  glyph floated over its neighbors. U+25AE is in Geometric Shapes, the
  same block as ▶, so whatever font serves the play triangle serves this,
  at the same baseline (see window.py's war story).
- heading_dash is ─ (U+2500 box drawings light horizontal), the rule
  character the ``── heading ────`` section lines are drawn from.

Call sites don't import this yet — that migration is a later integration
pass. The tests pin the vocabulary so the migration can't drift a glyph.

Resolution order for ``glyph(key)``: user override → active pack →
default pack. Registered packs must be complete (every key present), so
the default-pack fallback only matters for overrides cleared one at a
time in the future editor.
"""
from __future__ import annotations


# The full vocabulary. Anything the transport surfaces draw as a glyph
# gets a key here; packs must cover all of them.
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

# Verbatim transcription of the shipped glyphs (call sites: window.py's
# _on_state/_refresh_mode_buttons/_refresh_like_button/sleep+fullscreen
# buttons, variants.py's ControlsBundle, mini.py/fullscreen.py _on_state).
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
# User-level per-glyph overrides, layered on top of whatever pack is
# active. The Phase-2 glyph editor writes these.
_overrides: dict[str, str] = {}


def glyph(key: str) -> str:
    """Resolve a glyph: override → active pack → default pack. Raises
    KeyError for a key outside the vocabulary — that's a programming
    error at the call site, not user data."""
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
    """Registered pack names, registration order. The future settings
    picker reads this — no config file involved."""
    return tuple(_packs)


def register_pack(name: str, mapping: dict[str, str]) -> None:
    """Add (or replace) a named pack. Packs must be complete — a pack
    missing keys would silently fall back per-glyph and look like a bug
    in whichever surface hit the hole first. Extra keys are allowed so a
    newer pack file loads on this version."""
    missing = [k for k in KEYS if k not in mapping]
    if missing:
        raise ValueError(f"glyph pack {name!r} missing keys: {missing}")
    _packs[name] = dict(mapping)


def set_overrides(mapping: dict[str, str]) -> None:
    """Replace the user-override layer wholesale. Unknown keys are
    dropped silently — these will eventually come from persisted user
    state, and stale keys from another version must never crash."""
    global _overrides
    _overrides = {k: v for k, v in dict(mapping).items() if k in DEFAULT_PACK}
