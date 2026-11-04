"""Per-song lyric choices from the lyrics view's right-click menu: which
source to take a song's lyrics from, and a timing nudge for that one
song (on top of the global "lyrics offset" setting).

Kept in DATA_DIR/lyrics-picks.json, keyed by lyric_timing.track_key.
A song with nothing picked has no entry. The newest MAX_SONGS are kept.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

PATH: Path = config.DATA_DIR / "lyrics-picks.json"
MAX_SONGS = 5000
SOURCES = ("youtube music", "lrclib", "netease")
# a song's sheet can be a whole cut off (a different version's sync)
OFFSET_RANGE_MS = 10_000

_data: dict | None = None
_loaded_from: Path | None = None


def _load() -> dict:
    global _data, _loaded_from
    if _data is not None and _loaded_from == PATH:
        return _data
    data: dict = {}
    try:
        with open(PATH, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if isinstance(key, str) and isinstance(entry, dict):
                    clean = _clean(entry)
                    if clean:
                        data[key] = clean
    except (OSError, ValueError):
        pass
    _data, _loaded_from = data, PATH
    return data


def _clean(entry: dict) -> dict:
    out: dict = {}
    src = entry.get("source")
    if src in SOURCES:
        out["source"] = src
    try:
        ms = int(entry.get("offset_ms", 0) or 0)
    except (TypeError, ValueError):
        ms = 0
    ms = max(-OFFSET_RANGE_MS, min(OFFSET_RANGE_MS, ms))
    if ms:
        out["offset_ms"] = ms
    return out


def _save(data: dict) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, PATH)
    except OSError:
        pass


def _update(key: str | None, **fields) -> None:
    if not key:
        return
    data = _load()
    entry = dict(data.pop(key, {}))     # re-inserted last: newest
    entry.update(fields)
    entry = _clean(entry)
    if entry:
        data[key] = entry
    while len(data) > MAX_SONGS:
        data.pop(next(iter(data)))
    _save(data)


def source(key: str | None) -> str | None:
    """The source picked for this song, or None (tide's usual order)."""
    return _load().get(key, {}).get("source") if key else None


def set_source(key: str | None, src: str | None) -> None:
    _update(key, source=src if src in SOURCES else None)


def offset_ms(key: str | None) -> int:
    """This song's timing nudge; positive shows its lyrics later."""
    return int(_load().get(key, {}).get("offset_ms", 0)) if key else 0


def set_offset_ms(key: str | None, ms: int) -> None:
    _update(key, offset_ms=int(ms))
