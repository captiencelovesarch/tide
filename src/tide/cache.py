"""Cache for stream URLs and album art.

Stream URLs are TTL'd because most CDN URLs expire (~6h for YouTube; longer
for some, effectively infinite for Bandcamp). v1.2 splits the cache so each
source gets its own file with its own retention policy. Album art is
mtime-pruned in one shared directory.

Storage layout::

    ~/.cache/tide/streams/<source_slug>.json   {video_id: {url, expires_at}}

Each source picks its own TTL when calling ``put_stream_url(source, ...)``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config


STREAM_TTL_SECONDS = 4 * 3600          # default; sources override
STREAM_MAX_ENTRIES = 500               # per source
STREAM_MAX_BYTES = 5 * 1024 * 1024     # per source
ART_MAX_FILES = 1000

# infinity stand-in for sources whose URLs never expire (e.g. Bandcamp)
NEVER_EXPIRES = float("inf")


# In-memory mirror keyed by source slug → {video_id: (url, expires_at)}
_mem: dict[str, dict[str, tuple[str, float]]] = {}


# ---------- per-source paths ----------

def _streams_dir() -> Path:
    p = config.CACHE_DIR / "streams"
    p.mkdir(parents=True, exist_ok=True)
    # Stream URLs are access-granting (CDN links, and historically subsonic
    # URLs with embedded auth) — keep the whole directory private.
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def _stream_file(source: str) -> Path:
    safe = "".join(c for c in source if c.isalnum() or c in "._-") or "default"
    return _streams_dir() / f"{safe}.json"


def _legacy_stream_file() -> Path:
    return config.STREAM_CACHE_FILE


# ---------- disk i/o ----------

def _load_disk(source: str) -> dict[str, tuple[str, float]]:
    path = _stream_file(source)
    if not path.is_file():
        # One-time migration: if the pre-v1.2 streams.json exists and this
        # is the ytmusic cache, adopt it.
        if source == "ytmusic" and _legacy_stream_file().is_file():
            try:
                with open(_legacy_stream_file(), encoding="utf-8") as f:
                    raw = json.load(f)
                out = {k: (v["url"], float(v["expires_at"])) for k, v in raw.items()}
                _save_disk(source, out)
                try:
                    _legacy_stream_file().unlink()
                except OSError:
                    pass
                return out
            except Exception:
                return {}
        return {}
    try:
        os.chmod(path, 0o600)   # tighten files written by older versions
    except OSError:
        pass
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: (v["url"], float(v["expires_at"])) for k, v in raw.items()}
    except Exception:
        return {}


def _save_disk(source: str, cache: dict[str, tuple[str, float]]) -> None:
    path = _stream_file(source)
    serializable = {k: {"url": u, "expires_at": exp} for k, (u, exp) in cache.items()}
    tmp = path.with_suffix(".tmp")
    # 0o600 at create time (not chmod-after) so there's no window where the
    # URLs are world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(serializable, f)
    tmp.replace(path)


def _prune(cache: dict[str, tuple[str, float]]) -> None:
    now = time.time()
    expired = [k for k, (_, exp) in cache.items() if exp <= now]
    for k in expired:
        cache.pop(k, None)
    if len(cache) <= STREAM_MAX_ENTRIES:
        return
    by_age = sorted(cache.items(), key=lambda kv: kv[1][1])
    keep = dict(by_age[-STREAM_MAX_ENTRIES:])
    cache.clear()
    cache.update(keep)


def _ensure_loaded(source: str) -> dict[str, tuple[str, float]]:
    if source not in _mem:
        _mem[source] = _load_disk(source)
        _prune(_mem[source])
    return _mem[source]


# ---------- public api ----------

def clear_source(source: str) -> None:
    """Drop every cached URL for ``source``, in memory and on disk. Called
    on sign-out / reconfiguration so credential-bearing URLs (subsonic bakes
    auth into the query string) don't outlive the credentials themselves."""
    _mem.pop(source, None)
    try:
        _stream_file(source).unlink(missing_ok=True)
    except OSError:
        pass


def remove_stream_url(source: str, video_id: str) -> None:
    """Drop one cached URL, in memory and on disk. Called when playback
    proves the URL dead (mpv got a 403) — without this, every retry replays
    the same poison URL from disk for the rest of its TTL."""
    mem = _ensure_loaded(source)
    if mem.pop(video_id, None) is None:
        return
    try:
        _save_disk(source, mem)
    except Exception:
        pass


def get_stream_url(source: str, video_id: str) -> str | None:
    """Return a cached URL for ``video_id`` under ``source`` if still valid."""
    mem = _ensure_loaded(source)
    cached = mem.get(video_id)
    if not cached:
        return None
    url, exp = cached
    if exp <= time.time():
        return None
    return url


def put_stream_url(source: str, video_id: str, url: str,
                   ttl_seconds: float = STREAM_TTL_SECONDS) -> None:
    """Persist a URL with the given TTL (use ``cache.NEVER_EXPIRES`` for
    immortal sources)."""
    mem = _ensure_loaded(source)
    expires = time.time() + ttl_seconds if ttl_seconds != NEVER_EXPIRES else NEVER_EXPIRES
    mem[video_id] = (url, expires)
    _prune(mem)
    try:
        _save_disk(source, mem)
    except Exception:
        pass
    _enforce_byte_cap(source)


def _enforce_byte_cap(source: str) -> None:
    path = _stream_file(source)
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= STREAM_MAX_BYTES:
        return
    mem = _ensure_loaded(source)
    by_age = sorted(mem.items(), key=lambda kv: kv[1][1])
    keep = dict(by_age[len(by_age) // 2:])
    mem.clear()
    mem.update(keep)
    try:
        _save_disk(source, mem)
    except Exception:
        pass


# ---------- art cache prune ----------

def prune_art_cache() -> int:
    art_dir: Path = config.ART_CACHE_DIR
    if not art_dir.is_dir():
        return 0
    try:
        entries = [p for p in art_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    if len(entries) <= ART_MAX_FILES:
        return 0
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    to_remove = entries[ART_MAX_FILES:]
    removed = 0
    for p in to_remove:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed
