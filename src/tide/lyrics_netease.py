"""NetEase Cloud Music word-synced lyrics (YRC).

NetEase serves per-word timing for a good share of mainstream songs,
including English ones (5 of 10 popular English tracks in a spot check,
Oct 2026). The API is public but unofficial and China-hosted. Lyric
settings has a toggle to never ask it, and the request carries only the
title and artist.

``fetch_yrc`` picks the search hit whose title, artist and duration agree
with the track, then returns its YRC lines as a word sheet. The timing
was authored against NetEase's copy of the recording, so callers don't
use it as-is: lyric_words.adopt_source_words lines it up against the
playing video's own line timing, and drops it when the lines don't match.

Cached on disk next to the LRClib cache. A miss is cached too, so a song
NetEase lacks costs one lookup.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request

from . import config
from .lyric_words import WordLine, parse_yrc, sheet_from_payload, sheet_payload, similarity

SEARCH_URL = "https://music.163.com/api/search/get"
LYRIC_URL = "https://music.163.com/api/song/lyric/v1"
TIMEOUT_SECONDS = 6.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MISS_TTL_S = 14 * 24 * 3600
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) tide",
    "Referer": "https://music.163.com/",
}
DURATION_SLACK_S = 4.0
MIN_TITLE_SIM = 0.6


def _get_json(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        if resp.status != 200:
            return None
        raw = resp.read(MAX_RESPONSE_BYTES)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _cache_file(key: str):
    name = "ne-" + hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json"
    return config.LYRICS_CACHE_DIR / name


def _load(key: str) -> tuple[bool, list[WordLine] | None]:
    """(hit, sheet). A cached miss is (True, None)."""
    path = _cache_file(key)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None
    if not isinstance(data, dict):
        return False, None
    if data.get("miss"):
        try:
            fresh = time.time() - float(data.get("at", 0)) < MISS_TTL_S
        except (TypeError, ValueError):
            fresh = False
        return (True, None) if fresh else (False, None)
    sheet = sheet_from_payload(data.get("sheet"))
    return (sheet is not None), sheet


def _save(key: str, sheet: list[WordLine] | None) -> None:
    path = _cache_file(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            if sheet is None:
                json.dump({"miss": True, "at": time.time()}, f)
            else:
                json.dump({"sheet": sheet_payload(sheet)}, f)
    except OSError:
        pass


def _first_artist(artists: str) -> str:
    for sep in (",", "&", " feat", " ft.", " x ", " · ", "/"):
        if sep in artists:
            artists = artists.split(sep)[0]
    return artists.strip()


def pick(songs: list, *, title: str, artist: str, duration_seconds: float) -> int | None:
    """The NetEase song id of the hit that is this track, or None."""
    best_id, best_score = None, 0.0
    for s in songs or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "")
        sim_t = similarity(name, title)
        if sim_t < MIN_TITLE_SIM:
            continue
        names = " ".join(str(a.get("name") or "") for a in (s.get("artists") or [])
                         if isinstance(a, dict))
        sim_a = max(similarity(names, artist), similarity(names, _first_artist(artist)))
        if sim_a < 0.5:
            continue
        try:
            dur = float(s.get("duration") or 0) / 1000.0
        except (TypeError, ValueError):
            dur = 0.0
        if duration_seconds > 0 and dur > 0 and abs(dur - duration_seconds) > DURATION_SLACK_S:
            continue
        score = sim_t + sim_a
        if score > best_score:
            try:
                best_id, best_score = int(s.get("id")), score
            except (TypeError, ValueError):
                continue
    return best_id


def fetch_yrc(*, title: str, artist: str, duration_seconds: float = 0) -> list[WordLine] | None:
    """NetEase's word sheet for the track, or None (miss, network error,
    no word timing)."""
    title, artist = (title or "").strip(), (artist or "").strip()
    if not title or not artist:
        return None
    key = f"{title}|{artist}|{int(duration_seconds or 0)}"
    hit, sheet = _load(key)
    if hit:
        return sheet
    try:
        q = urllib.parse.urlencode({"s": f"{title} {_first_artist(artist)}",
                                    "type": 1, "limit": 8})
        found = _get_json(f"{SEARCH_URL}?{q}")
        songs = ((found or {}).get("result") or {}).get("songs") or []
        sid = pick(songs, title=title, artist=artist,
                   duration_seconds=float(duration_seconds or 0))
        sheet = None
        if sid is not None:
            lyr = _get_json(f"{LYRIC_URL}?id={sid}&lv=1&yv=1")
            yrc = ((lyr or {}).get("yrc") or {}).get("lyric") or ""
            parsed = parse_yrc(yrc) if isinstance(yrc, str) else []
            sheet = parsed if any(l.words for l in parsed) else None
    except Exception:
        return None          # network trouble: don't cache, try next time
    _save(key, sheet)
    return sheet
