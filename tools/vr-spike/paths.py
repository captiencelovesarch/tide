"""where the spike writes things. no openvr, so offline commands can use it."""
from __future__ import annotations

import os
from pathlib import Path

OUT = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tide-vr-spike"


def out_dir(*parts: str) -> Path:
    p = OUT.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p
