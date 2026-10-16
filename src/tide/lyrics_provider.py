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
import urllib.parse
import urllib.request
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
                          word_lines=words)
    if not result.is_empty:
        _save_cache(key, result)
    return result if not result.is_empty else None
