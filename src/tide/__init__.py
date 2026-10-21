"""tide — a multi-source music player with two personalities, brutalist
and modern. presets.py flips them; everything under the look is shared."""
from __future__ import annotations

# Prefer the *installed* package version so a pip/AUR build always reports
# what's actually on disk (the old hardcoded string drifted behind pyproject
# and made the About box + update check lie). The literal fallback covers
# running straight from a source checkout with no installed metadata.
try:
    from importlib.metadata import distribution as _distribution
    from pathlib import Path as _Path

    _dist = _distribution("tide")
    # a source checkout run next to an installed tide (python -m tide from
    # src/) would report the installed one's version, and the plugin store
    # judges min_tide by it
    if _Path(_dist.locate_file("tide/__init__.py")).resolve() != _Path(__file__).resolve():
        raise LookupError("not the installed copy")
    __version__ = _dist.version
except Exception:
    __version__ = "3.0.0"
