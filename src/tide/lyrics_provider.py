"""LRClib lyrics fallback.

YouTube Music's lyrics endpoint covers many tracks but not all. LRClib
(https://lrclib.net) is a community-curated lyrics database with both
plain and timed (LRC-format) lyrics. When YT Music returns nothing, we
ask LRClib by title + artist + (optional) album + duration.

Result shape:

  LyricsResult(plain_text="...", timed_lines=[(seconds, "line"), ...],
               line_ends=[seconds | None, ...], word_lines=[WordLine, ...])

``line_ends`` (parallel to timed_lines, may be empty) is when each line
stops being sung, where the source says; ``word_lines`` is a word-synced
sheet when the source has one (LRClib's lyricsfile ``words`` or enhanced
LRC tags). Both feed the per-word karaoke (lyric_timing).

If neither plain nor timed is available, the result is None and callers
fall back to the "no lyrics" empty state.

Caching: ~/.cache/tide/lyrics/<sha1>.json so re-listens don't re-query.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .lyric_words import (
    lyricsfile_line_ends, parse_enhanced_lrc, parse_lyricsfile, sheet_payload,
)


USER_AGENT = "tide/1.0 (https://github.com/captiencelovesarch/tide)"
LRCLIB_URL = "https://lrclib.net/api/get"
TIMEOUT_SECONDS = 5.0
# Bound the LRClib response so a hostile/MITM'd endpoint can't OOM us with a
# giant body. Lyrics are a few KB; 8 MB is already absurdly generous.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass
class LyricsResult:
    plain_text: str = ""
    timed_lines: list[tuple[float, str]] = field(default_factory=list)
    line_ends: list = field(default_factory=list)
    word_lines: list = field(default_factory=list)
    # where the lines came from, as the lyrics menu names it:
    # "youtube music" | "lrclib" | "netease" ("" = unknown)
    source: str = ""

    @property
    def has_timed(self) -> bool:
        return bool(self.timed_lines)

    @property
    def is_empty(self) -> bool:
        return not self.plain_text and not self.timed_lines


_LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
# enhanced-LRC word stamps: kept out of the line text (lyric_words reads them)
_LRC_WORD_TAG = re.compile(r"<\d+:\d+(?:\.\d+)?>")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse LRC-format lyrics into ``[(seconds, line), ...]``.

    Handles multiple timestamps per line (very common). Skips metadata
    tags like ``[ar: artist]``.
    """
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        timestamps: list[float] = []
        rest = raw
        while True:
            m = _LRC_LINE.match(rest)
            if not m:
                break
            # Timestamps are untrusted remote text. `\d+` is unbounded, and
            # int() on a >4300-digit string raises ValueError (Python 3.11+
            # int/str limit) — a crafted LRC line could otherwise abort the
            # whole parse. Skip any line whose numbers won't convert.
            try:
                mm = int(m.group(1))
                ss = float(m.group(2))
            except ValueError:
                rest = m.group(3)
                continue
            timestamps.append(mm * 60 + ss)
            rest = m.group(3)
        if not timestamps:
            continue
        line = " ".join(_LRC_WORD_TAG.sub("", rest).split())
        for t in timestamps:
            out.append((t, line))
    out.sort(key=lambda x: x[0])
    return out


def _cache_path(query_key: str) -> Path:
    name = hashlib.sha1(query_key.encode("utf-8")).hexdigest() + ".json"
    return config.LYRICS_CACHE_DIR / name


def _load_cache(key: str) -> LyricsResult | None:
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    # A poisoned cache file (attacker with local write to ~/.cache/tide) could
    # hold rows of the wrong shape/type — e.g. [1,2,3] (unpack error) or
    # ["x","y"] (float() error). Parse defensively so a bad cache degrades to
    # a miss rather than raising out of the fetch path.
    if not isinstance(data, dict):
        return None
    timed_raw = data.get("timed") or []
    timed: list[tuple[float, str]] = []
    if isinstance(timed_raw, list):
        for row in timed_raw:
            if not (isinstance(row, (list, tuple)) and len(row) == 2):
                continue
            t, line = row
            if not isinstance(line, str):
                continue
            try:
                timed.append((float(t), line))
            except (ValueError, TypeError):
                continue
    plain = data.get("plain", "")
    ends: list = []
    raw_ends = data.get("ends")
    if isinstance(raw_ends, list) and len(raw_ends) == len(timed):
        for e in raw_ends:
            try:
                ends.append(None if e is None else float(e))
            except (TypeError, ValueError):
                ends = []
                break
    from .lyric_words import sheet_from_payload
    words = sheet_from_payload(data.get("words")) if data.get("words") else None
    return LyricsResult(plain_text=plain if isinstance(plain, str) else "", timed_lines=timed,
                        line_ends=ends, word_lines=words or [])


def _save_cache(key: str, result: LyricsResult) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "plain": result.plain_text,
                "timed": [(t, line) for t, line in result.timed_lines],
                "ends": list(result.line_ends),
                "words": sheet_payload(result.word_lines) if result.word_lines else "",
            }, f)
    except OSError:
        pass


def fetch_lrclib(*, title: str, artist: str, album: str = "",
                  duration_seconds: int = 0) -> LyricsResult | None:
    """Query LRClib for the track. Returns None on miss or network error."""
    if not title or not artist:
        return None
    # v2: the cache entry also holds line ends + word timing
    key = f"v2|{title}|{artist}|{album}|{int(duration_seconds)}"
    cached = _load_cache(key)
    if cached is not None and not cached.is_empty:
        cached.source = "lrclib"
        return cached

    params = {
        "track_name": title.strip(),
        "artist_name": artist.strip(),
    }
    if album:
        params["album_name"] = album.strip()
    if duration_seconds > 0:
        params["duration"] = str(int(duration_seconds))
    url = LRCLIB_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return None
            raw = resp.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    plain = (data.get("plainLyrics") or "").strip()
    synced = (data.get("syncedLyrics") or "").strip()
    timed = parse_lrc(synced) if synced else []
    lyricsfile = data.get("lyricsfile") if isinstance(data.get("lyricsfile"), str) else ""
    words = parse_lyricsfile(lyricsfile) if lyricsfile else []
    if not words and synced:
        words = parse_enhanced_lrc(synced)
    ends = lyricsfile_line_ends(lyricsfile, timed) if lyricsfile else []
    result = LyricsResult(plain_text=plain, timed_lines=timed, line_ends=ends,
                          word_lines=words, source="lrclib")
    if not result.is_empty:
        _save_cache(key, result)
    return result if not result.is_empty else None


def fetch_netease(track) -> LyricsResult | None:
    """NetEase's word-synced lyrics for ``track`` as a whole result (the
    lyrics menu's "netease" pick). Its timing is NetEase's own recording,
    so it can sit off the video; the timing nudge is there for that."""
    from .lyrics_netease import fetch_yrc
    try:
        sheet = fetch_yrc(title=getattr(track, "title", "") or "",
                          artist=getattr(track, "artists", "") or "",
                          duration_seconds=float(getattr(track, "duration_seconds", 0) or 0))
    except Exception:
        return None
    if not sheet:
        return None
    return LyricsResult(plain_text="\n".join(l.text for l in sheet),
                        timed_lines=[(l.start, l.text) for l in sheet],
                        line_ends=[l.end for l in sheet],
                        word_lines=list(sheet), source="netease")


# ---------- one fetch per song, shared ----------
# The lyrics views, the headless tracker (discord, plugins) and the
# next-song prefetch all ask for the same song, often at the same moment.
# The first asks the network; the rest wait for its answer and share it.
# Answers are kept for the session (the newest MEMO_SIZE songs); a miss
# isn't, so a network blip doesn't stick.
MEMO_SIZE = 64
_memo: "OrderedDict[str, LyricsResult]" = OrderedDict()
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _shared(key: str, fn, force: bool = False):
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
        if force:
            _memo.pop(key, None)
    with lock:
        with _guard:
            if key in _memo:
                _memo.move_to_end(key)
                return _memo[key]
        result = fn()
        if result is not None:
            with _guard:
                _memo[key] = result
                while len(_memo) > MEMO_SIZE:
                    old, _r = _memo.popitem(last=False)
                    _locks.pop(old, None)
        return result


def fetch_from(api_obj, track, source: str | None, *, force: bool = False) -> LyricsResult | None:
    """``track``'s lyrics from one source, or tide's usual order (the
    track's own source, then lrclib) when ``source`` is None. Shared: a
    second caller for the same song gets the first one's answer.
    ``force`` asks again (the lyrics view's try again)."""
    key = (f"{getattr(track, 'source', '') or 'ytmusic'}:{getattr(track, 'video_id', '')}"
           f"|{source or 'auto'}")
    return _shared(key, lambda: _fetch_from(api_obj, track, source), force)


def _fetch_from(api_obj, track, source: str | None) -> LyricsResult | None:
    if source is None:
        return api_obj.get_lyrics_for_track(track)
    if source == "lrclib":
        return fetch_lrclib(title=track.title or "", artist=track.artists or "",
                            album=track.album or "",
                            duration_seconds=int(track.duration_seconds or 0))
    if source == "netease":
        return fetch_netease(track)
    if source == "youtube music":
        timed = getattr(api_obj, "_yt_timed_lyrics", None)
        result = timed(track.video_id) if callable(timed) else None
        if result is not None:
            return result
        plain_fn = getattr(api_obj, "get_lyrics_for", None)
        plain = plain_fn(track.video_id) if callable(plain_fn) else None
        return LyricsResult(plain_text=plain, source="youtube music") if plain else None
    return None


# ---------- a disk cache for sources that have none of their own ----------
# YouTube Music's lyrics (timed and plain) were fetched again every play.
# Kept like lrclib's, as LyricsResult rows; a miss is kept for a few days
# so a song with no lyrics costs one lookup, and a hit is asked again after
# a month in case the sync was fixed.
HIT_TTL_S = 30 * 24 * 3600
MISS_TTL_S = 3 * 24 * 3600


def cache_get(key: str) -> tuple[bool, LyricsResult | None]:
    """(hit, result) for ``key``. A fresh cached miss is (True, None)."""
    path = _cache_path(key)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None
    if not isinstance(data, dict):
        return False, None
    try:
        age = time.time() - float(data.get("at", 0) or 0)
    except (TypeError, ValueError):
        return False, None
    if data.get("miss"):
        return (True, None) if age < MISS_TTL_S else (False, None)
    if age >= HIT_TTL_S:
        return False, None
    result = _load_cache(key)
    if result is None or result.is_empty:
        return False, None
    src = data.get("source")
    result.source = src if isinstance(src, str) else ""
    return True, result


def cache_put(key: str, result: LyricsResult | None) -> None:
    if result is None or result.is_empty:
        path = _cache_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"miss": True, "at": time.time()}, f)
        except OSError:
            pass
        return
    _save_cache(key, result)
    path = _cache_path(key)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["source"], data["at"] = result.source, time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except (OSError, ValueError):
        pass
