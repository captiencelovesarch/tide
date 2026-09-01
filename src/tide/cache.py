"""Cache for stream URLs, album art, and JSON payloads.

Stream URLs are TTL'd because most CDN URLs expire (~6h for YouTube; longer
for some, effectively infinite for Bandcamp). v1.2 splits the cache so each
source gets its own file with its own retention policy. Album art is
mtime-pruned in one shared directory.

v1.5 adds a generic JSON payload cache for browse-shaped data (song
insights, charts, moods, related shelves, …) so community surfaces don't
re-hit the network on every navigation. Same shape as the stream cache but
namespaced by caller instead of by source.

Storage layout::

    ~/.cache/tide/streams/<source_slug>.json   {video_id: {url, expires_at}}
    ~/.cache/tide/data/<namespace>.json        {key: {payload, expires_at}}
    ~/.cache/tide/blobs/<namespace>/<sha1>.z   one zlib'd {payload, expires_at}

Each source picks its own TTL when calling ``put_stream_url(source, ...)``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import zlib
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


# ---------- generic JSON payload cache (v1.5) ----------

# Browse-shaped data: song insights, charts, moods, related shelves, credits.
# Payloads are whatever json.dump accepts. Unlike stream URLs these carry no
# credentials, but the files stay 0600 anyway — insights and history-adjacent
# payloads describe what the user listens to, which is nobody else's business.

DATA_MAX_ENTRIES = 400          # per namespace

# One lock for all namespaces: workers from several views can write at once
# (insight fetch + related fetch + a home refresh), and unlike the stream
# cache's "worst case one doomed extra pass", a torn read-modify-write here
# would silently drop another writer's payload.
_DATA_LOCK = threading.Lock()

# namespace → {key: (payload, expires_at)}
_data_mem: dict[str, dict[str, tuple[object, float]]] = {}


def _data_dir() -> Path:
    p = config.CACHE_DIR / "data"
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def _data_file(namespace: str) -> Path:
    safe = "".join(c for c in namespace if c.isalnum() or c in "._-") or "default"
    return _data_dir() / f"{safe}.json"


def _data_load(namespace: str) -> dict[str, tuple[object, float]]:
    if namespace in _data_mem:
        return _data_mem[namespace]
    out: dict[str, tuple[object, float]] = {}
    path = _data_file(namespace)
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            out = {k: (v["payload"], float(v["expires_at"])) for k, v in raw.items()}
        except Exception:
            out = {}
    _data_mem[namespace] = out
    _data_prune(out)
    return out


def _data_prune(mem: dict[str, tuple[object, float]]) -> None:
    now = time.time()
    for k in [k for k, (_, exp) in mem.items() if exp <= now]:
        mem.pop(k, None)
    if len(mem) <= DATA_MAX_ENTRIES:
        return
    by_age = sorted(mem.items(), key=lambda kv: kv[1][1])
    keep = dict(by_age[-DATA_MAX_ENTRIES:])
    mem.clear()
    mem.update(keep)


def _data_save(namespace: str, mem: dict[str, tuple[object, float]]) -> None:
    path = _data_file(namespace)
    serializable = {k: {"payload": p, "expires_at": exp}
                    for k, (p, exp) in mem.items()
                    if exp != NEVER_EXPIRES}     # inf isn't valid JSON
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(serializable, f)
    tmp.replace(path)


def get_json(namespace: str, key: str):
    """Return the cached payload for ``key`` in ``namespace``, or None if
    absent/expired. Payload is whatever ``put_json`` stored (post-JSON, so
    tuples come back as lists)."""
    with _DATA_LOCK:
        mem = _data_load(namespace)
        hit = mem.get(key)
        if not hit:
            return None
        payload, exp = hit
        if exp <= time.time():
            mem.pop(key, None)
            return None
        return payload


def put_json(namespace: str, key: str, payload, ttl_seconds: float) -> None:
    """Persist ``payload`` under ``namespace``/``key`` for ``ttl_seconds``."""
    with _DATA_LOCK:
        mem = _data_load(namespace)
        mem[key] = (payload, time.time() + ttl_seconds)
        _data_prune(mem)
        try:
            _data_save(namespace, mem)
        except Exception:
            pass


def update_json(namespace: str, key: str, partial: dict, ttl_seconds: float):
    """Merge ``partial``'s non-empty values into the cached dict for ``key``
    (creating it if absent) and refresh the TTL. Returns the merged dict.

    Exists for split writers: song insights arrive from two directions —
    the yt-dlp resolver knows likes, ``get_song`` knows views — and either
    may land first. Plain ``put_json`` from both would drop whichever half
    arrived earlier. Zero/empty values never overwrite a known value.
    """
    with _DATA_LOCK:
        mem = _data_load(namespace)
        hit = mem.get(key)
        base: dict = {}
        if hit and hit[1] > time.time() and isinstance(hit[0], dict):
            base = dict(hit[0])
        for k, v in partial.items():
            if v or k not in base:
                base[k] = v
        mem[key] = (base, time.time() + ttl_seconds)
        _data_prune(mem)
        try:
            _data_save(namespace, mem)
        except Exception:
            pass
        return base


def clear_namespace(namespace: str) -> None:
    """Drop a whole namespace, memory and disk. Sign-out calls this for
    account-derived namespaces (home, library) so one user's shelves don't
    greet the next sign-in."""
    with _DATA_LOCK:
        _data_mem.pop(namespace, None)
        try:
            _data_file(namespace).unlink(missing_ok=True)
        except OSError:
            pass


# ---------- per-key blob store ----------
# Pulse maps are ~100-300KB per track and get resaved every 30s while music
# plays. Through the data store above that meant re-serializing the whole
# namespace under _DATA_LOCK per save and keeping every payload in _data_mem
# for the life of the process; one compressed file per key makes a save
# O(one entry) and shares no lock with the browse caches.

BLOB_MAX_ENTRIES = 400              # per namespace, mtime-pruned on put
BLOB_MAX_BYTES = 12 * 1024 * 1024   # serialized envelope cap, refused on put

_BLOB_LOCK = threading.Lock()


def _blob_dir(namespace: str) -> Path:
    safe = "".join(c for c in namespace if c.isalnum() or c in "._-") or "default"
    p = config.CACHE_DIR / "blobs" / safe
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def _blob_file(namespace: str, key: str) -> Path:
    return _blob_dir(namespace) / (hashlib.sha1(key.encode()).hexdigest() + ".z")


def get_blob_json(namespace: str, key: str):
    """Payload for ``namespace``/``key``, or None if absent/expired/corrupt.
    Same post-JSON contract as ``get_json``."""
    path = _blob_file(namespace, key)
    try:
        with _BLOB_LOCK:
            data = path.read_bytes()
        envelope = json.loads(zlib.decompress(data))
        if float(envelope["expires_at"]) <= time.time():
            with _BLOB_LOCK:
                path.unlink(missing_ok=True)
            return None
        return envelope["payload"]
    except Exception:
        return None


def put_blob_json(namespace: str, key: str, payload, ttl_seconds: float,
                  max_bytes: int = BLOB_MAX_BYTES) -> bool:
    """Persist ``payload`` in its own file. Returns False without writing
    when the serialized form exceeds ``max_bytes`` — readers cap too, so an
    oversized entry would only burn disk."""
    raw = json.dumps({"payload": payload,
                      "expires_at": time.time() + ttl_seconds},
                     separators=(",", ":")).encode()
    if len(raw) > max_bytes:
        return False
    path = _blob_file(namespace, key)
    blob = zlib.compress(raw, 6)
    try:
        with _BLOB_LOCK:
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            tmp.replace(path)
            _blob_prune(path.parent)
    except OSError:
        return False
    return True


def _blob_prune(directory: Path) -> None:
    # entry cap by mtime; expiry stays lazy — it lives inside the files and
    # checking it here would mean decompressing the whole namespace.
    try:
        files = sorted((entry.stat().st_mtime, Path(entry.path))
                       for entry in os.scandir(directory)
                       if entry.name.endswith(".z"))
    except OSError:
        return
    for _mtime, path in files[:max(0, len(files) - BLOB_MAX_ENTRIES)]:
        try:
            path.unlink()
        except OSError:
            pass


def clear_blob_namespace(namespace: str) -> None:
    with _BLOB_LOCK:
        try:
            for entry in os.scandir(_blob_dir(namespace)):
                Path(entry.path).unlink(missing_ok=True)
        except OSError:
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
