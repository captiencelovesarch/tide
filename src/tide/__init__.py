"""tide — a brutalist multi-source music client."""
from __future__ import annotations

# Prefer the *installed* package version so a pip/AUR build always reports
# what's actually on disk (the old hardcoded string drifted behind pyproject
# and made the About box + update check lie). The literal fallback covers
# running straight from a source checkout with no installed metadata.
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("tide")
except Exception:
    __version__ = "1.2.5"
