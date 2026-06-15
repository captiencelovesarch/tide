"""Thin wrapper over ytmusicapi + yt-dlp.

Search returns normalized Track records. Stream URL resolution caches results
on disk with a TTL well below the YouTube URL expiry window.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Iterable

import yt_dlp
from ytmusicapi import YTMusic

from . import config


STREAM_TTL_SECONDS = 4 * 3600  # YouTube URLs typically last ~6h


@dataclass
class Track:
    video_id: str
    title: str
    artists: str
    album: str = ""
    duration: str = ""           # "3:42"
    duration_seconds: int = 0
    thumbnail: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class PlaylistEntry:
    playlist_id: str
    title: str
    description: str = ""
    thumbnail: str = ""


@dataclass
class PlaylistDetail:
    playlist_id: str
    title: str
    description: str = ""
    track_count: int = 0
    thumbnail: str = ""
    tracks: list = field(default_factory=list)


def _join_artists(items: Iterable[dict] | None) -> str:
    if not items:
        return ""
    names = [a.get("name", "") for a in items if isinstance(a, dict)]
    return ", ".join(n for n in names if n)


def _thumb(items: list[dict] | None) -> str:
    if not items:
        return ""
    return items[-1].get("url", "")


def _parse_hms(s: str) -> int:
    parts = [int(p) for p in s.split(":") if p.isdigit()]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def _to_track(item: dict) -> Track | None:
    vid = item.get("videoId")
    if not vid:
        return None
    duration = item.get("duration") or item.get("length") or ""
    secs = int(item.get("duration_seconds") or 0)
    if secs == 0 and duration and ":" in duration:
        secs = _parse_hms(duration)
    album = ""
    alb = item.get("album")
    if isinstance(alb, dict):
        album = alb.get("name", "")
    elif isinstance(alb, str):
        album = alb
    # watch_playlist uses "thumbnail" key (singular), search uses "thumbnails"
    thumbs = item.get("thumbnails") or item.get("thumbnail")
    return Track(
        video_id=vid,
        title=item.get("title", ""),
        artists=_join_artists(item.get("artists")),
        album=album,
        duration=duration,
        duration_seconds=secs,
        thumbnail=_thumb(thumbs),
        extras=item,
    )


class Api:
    """Wraps a YTMusic client. All ytmusicapi-facing code lives here."""

    def __init__(self, yt: YTMusic) -> None:
        self.yt = yt

    def search_songs(self, query: str, limit: int = 20) -> list[Track]:
        if not query.strip():
            return []
        results = self.yt.search(query, filter="songs", limit=limit) or []
        out: list[Track] = []
        for item in results:
            tr = _to_track(item)
            if tr:
                out.append(tr)
        return out

    def get_library_playlists(self, limit: int = 100) -> list["PlaylistEntry"]:
        """Return the user's playlists (including 'Liked Music' as 'LM')."""
        items = self.yt.get_library_playlists(limit=limit) or []
        return [
            PlaylistEntry(
                playlist_id=p.get("playlistId", ""),
                title=p.get("title", ""),
                description=p.get("description", "") or "",
                thumbnail=_thumb(p.get("thumbnails")),
            )
            for p in items
            if p.get("playlistId")
        ]

    def get_playlist(self, playlist_id: str, limit: int = 500) -> "PlaylistDetail":
        """Fetch playlist metadata + tracks. Works for user playlists and 'LM'."""
        if playlist_id == "LM":
            raw = self.yt.get_liked_songs(limit=limit) or {}
        else:
            raw = self.yt.get_playlist(playlistId=playlist_id, limit=limit) or {}
        tracks: list[Track] = []
        for item in raw.get("tracks", []) or []:
            tr = _to_track(item)
            if tr:
                tracks.append(tr)
        return PlaylistDetail(
            playlist_id=playlist_id,
            title=raw.get("title", "") or "",
            description=raw.get("description", "") or "",
            track_count=int(raw.get("trackCount") or len(tracks)),
            thumbnail=_thumb(raw.get("thumbnails")),
            tracks=tracks,
        )

    def get_lyrics_for(self, video_id: str) -> str | None:
        """Return plain lyrics text for `video_id`, or None if unavailable.

        Skips timestamped fetch (which needs the mobile client context that
        cookie-auth can't use). For tide's purposes static text is fine.
        """
        if not video_id:
            return None
        try:
            wp = self.yt.get_watch_playlist(videoId=video_id)
        except Exception:
            return None
        browse_id = wp.get("lyrics") if isinstance(wp, dict) else None
        if not browse_id:
            return None
        try:
            lyr = self.yt.get_lyrics(browse_id)
        except Exception:
            return None
        if not lyr:
            return None
        text = lyr.get("lyrics") if isinstance(lyr, dict) else getattr(lyr, "lyrics", None)
        if not text or not isinstance(text, str):
            return None
        return text

    def get_radio(self, video_id: str, exclude: set[str] | None = None) -> list[Track]:
        """Return the YT Music auto-generated radio for a track.

        Drops the seed track itself plus anything in `exclude` (typically the
        current queue's video_ids), so callers can splice the result without
        introducing dupes.
        """
        if not video_id:
            return []
        excluded = set(exclude or ())
        excluded.add(video_id)
        res = self.yt.get_watch_playlist(videoId=video_id, radio=True)
        out: list[Track] = []
        for item in res.get("tracks", []) or []:
            tr = _to_track(item)
            if not tr or tr.video_id in excluded:
                continue
            excluded.add(tr.video_id)
            out.append(tr)
        return out


# ---------- stream URL resolution (yt-dlp) ----------

_stream_cache_mem: dict[str, tuple[str, float]] = {}


def _load_disk_cache() -> dict[str, tuple[str, float]]:
    path = config.STREAM_CACHE_FILE
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: (v["url"], float(v["expires_at"])) for k, v in raw.items()}
    except Exception:
        return {}


def _save_disk_cache(cache: dict[str, tuple[str, float]]) -> None:
    path = config.STREAM_CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {k: {"url": u, "expires_at": exp} for k, (u, exp) in cache.items()}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(serializable, f)
    tmp.replace(path)


def _prune_expired(cache: dict[str, tuple[str, float]]) -> None:
    now = time.time()
    expired = [k for k, (_, exp) in cache.items() if exp <= now]
    for k in expired:
        cache.pop(k, None)


def resolve_stream_url(video_id: str) -> str:
    """Return a playable audio URL for the given YT Music video id.

    Uses an in-memory + on-disk TTL cache so re-queueing the same track in
    a session doesn't repeatedly hit yt-dlp.
    """
    now = time.time()

    cached = _stream_cache_mem.get(video_id)
    if cached and cached[1] > now:
        return cached[0]

    if not _stream_cache_mem:
        _stream_cache_mem.update(_load_disk_cache())
        _prune_expired(_stream_cache_mem)
        cached = _stream_cache_mem.get(video_id)
        if cached and cached[1] > now:
            return cached[0]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "noplaylist": True,
    }
    url = f"https://music.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    stream_url = info.get("url")
    if not stream_url and "requested_formats" in info:
        stream_url = info["requested_formats"][0].get("url")
    if not stream_url:
        raise RuntimeError(f"no playable audio stream for {video_id}")

    expires_at = now + STREAM_TTL_SECONDS
    _stream_cache_mem[video_id] = (stream_url, expires_at)
    try:
        _save_disk_cache(_stream_cache_mem)
    except Exception:
        pass
    return stream_url
