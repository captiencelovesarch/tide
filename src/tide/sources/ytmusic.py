"""YouTube Music source.

The original v1.0/v1.1 client class lived at ``tide.api.Api``. v1.2 lifts
it into the source abstraction: it now implements ``MusicSource``, and a
``tide.api`` shim keeps existing imports working.

Stream resolution is delegated to ``yt-dlp`` and cached per source via
``tide.cache`` so URLs survive page navigations without being refetched.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Iterable

import yt_dlp
from ytmusicapi import YTMusic

from .. import cache
from .. import perf
from .base import (
    AlbumDetail,
    AlbumEntry,
    ArtistDetail,
    ArtistEntry,
    MusicSource,
    NotSupportedError,
    PlaylistDetail,
    PlaylistEntry,
    Shelf,
    ShelfItem,
    StreamRef,
    Track,
    safe_int,
)


SOURCE_SLUG = "ytmusic"

# Anonymous client used ONLY for timed lyrics — the mobile context that
# serves timestamps rejects signed-in browser cookies with HTTP 400 (see
# YTMusicSource._yt_timed_lyrics). Lazy: constructed on the first lyrics
# fetch, which always runs on a worker thread.
_anon_lyrics: YTMusic | None = None
_ANON_LYRICS_LOCK = threading.Lock()


def _anon_lyrics_client() -> YTMusic:
    global _anon_lyrics
    with _ANON_LYRICS_LOCK:
        if _anon_lyrics is None:
            _anon_lyrics = YTMusic()
        return _anon_lyrics


_AUTH_ERROR_MARKERS = ("http 401", "unauthenticated", "authentication credential")


# The *other* way a YT Music session dies, and the one that actually bit:
# YouTube keeps answering HTTP 200 and simply serves the signed-out payload.
# The account menu comes back with no activeAccountHeaderRenderer, so
# ytmusicapi's parser raises a KeyError walking to the account name. Nothing
# in that path is a 401, which is why _AUTH_ERROR_MARKERS never matched and
# the app degraded silently into anonymous results instead of saying so.
_SIGNED_OUT_MARKERS = ("activeaccountheaderrenderer", "accountname")


def _is_auth_error(exc: Exception) -> bool:
    """True when an exception from ytmusicapi looks like dead cookie auth.

    Expired/rotated cookies surface as ``YTMusicServerError("Server returned
    HTTP 401: Unauthorized.\\n<google detail>")`` where the detail mentions
    the missing "authentication credential". Matched on message text rather
    than exception type so a requests-level error from a future ytmusicapi
    still registers.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _is_signed_out_payload(exc: Exception) -> bool:
    """True when a 200-OK response carried the anonymous account menu.

    Deliberately narrow: only the account-header lookup is checked, and only
    from ``probe_auth``. A broad "any KeyError means signed out" rule would
    turn every future YouTube layout tweak into a bogus sign-in prompt.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _SIGNED_OUT_MARKERS)


def _ago(seconds: float) -> str:
    """Coarse duration for status lines: '3d', '5h', '12m', 'just now'."""
    seconds = max(0.0, seconds)
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return "<1m"


class _AuthSentinel:
    """Transparent wrapper around a YTMusic client that watches every call
    for auth-shaped failures.

    Cookie auth doesn't announce its own death — YouTube just starts
    returning 401 and the layers above either surface a generic error
    string or swallow it and render empty (home → ``[]``, artist → ``None``,
    …), so the user never learns the fix is "import fresh cookies". The
    sentinel sees the 401 in flight, reports it via the callback, and
    re-raises, so callers keep exactly their old behavior.
    """

    def __init__(self, yt: YTMusic, on_auth_error) -> None:
        self._yt = yt
        self._on_auth_error = on_auth_error

    def __getattr__(self, name: str):
        attr = getattr(self._yt, name)
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                if _is_auth_error(exc):
                    try:
                        self._on_auth_error()
                    except Exception:
                        pass
                raise

        return guarded


def _join_artists(items: Iterable[dict] | None) -> str:
    if not items:
        return ""
    names = [a.get("name", "") for a in items if isinstance(a, dict)]
    return ", ".join(n for n in names if n)


_GUSERCONTENT_SIZE = re.compile(r"=w\d+-h\d+")
_GUSERCONTENT_S = re.compile(r"=s\d+(?=[-$]|$)")
_YTIMG_SMALL = re.compile(r"/(default|mqdefault|sddefault)\.jpg\b")


def _upscale_art_url(url: str, px: int = 544) -> str:
    """Ask the CDN for real resolution instead of the payload's thumbnail.

    YT Music's API answers with tiny variants (60–120px) even though the
    googleusercontent CDN will happily serve the same image at any size —
    the size lives in the URL. Without this, the now-playing strip and the
    mini player upscale a 120px jpeg and look like 144p."""
    if "googleusercontent.com" in url or "ggpht.com" in url:
        if _GUSERCONTENT_SIZE.search(url):
            return _GUSERCONTENT_SIZE.sub(f"=w{px}-h{px}", url)
        if _GUSERCONTENT_S.search(url):
            return _GUSERCONTENT_S.sub(f"=s{px}", url)
        return url
    if "i.ytimg.com" in url:
        # Video thumbs: hqdefault (480w) always exists; the larger named
        # variants often 404, so stop there.
        return _YTIMG_SMALL.sub("/hqdefault.jpg", url)
    return url


def _thumb(items: list[dict] | None) -> str:
    if not items:
        return ""
    return _upscale_art_url(items[-1].get("url", ""))


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
    secs = safe_int(item.get("duration_seconds"))
    if secs == 0 and duration and ":" in duration:
        secs = _parse_hms(duration)
    album = ""
    alb = item.get("album")
    if isinstance(alb, dict):
        album = alb.get("name", "")
    elif isinstance(alb, str):
        album = alb
    thumbs = item.get("thumbnails") or item.get("thumbnail")
    return Track(
        video_id=vid,
        title=item.get("title", ""),
        artists=_join_artists(item.get("artists")),
        album=album,
        duration=duration,
        duration_seconds=secs,
        thumbnail=_thumb(thumbs),
        source=SOURCE_SLUG,
        extras=item,
    )


class YTMusicSource(MusicSource):
    slug = SOURCE_SLUG
    name = "youtube music"
    icon = "ytmusic"
    needs_auth = True
    supports_in_app_auth = True
    backend_slug = "mpv"
    short_tag = "YT"
    capabilities = frozenset({
        "library", "albums", "artists", "videos",
        "home", "radio", "lyrics", "rating",
    })

    STREAM_TTL_SECONDS = 4 * 3600          # YT CDN URLs last ~6h

    def __init__(self, yt: YTMusic) -> None:
        self.yt = _AuthSentinel(yt, self._on_auth_error)
        self._signed_out = False
        self._auth_expired = False
        # Unix time of the last probe that came back genuinely signed in.
        self._last_auth_ok: float | None = None

    # ---------- auth surface ----------

    def _on_auth_error(self) -> None:
        """Sentinel callback — runs on whatever worker thread hit the 401.

        One-shot per session: the flag flips before the registry emits, and
        ``begin_auth()`` resets it, so a dead cookie jar produces exactly one
        notification instead of one per failed request.
        """
        if self._auth_expired or self._signed_out:
            return
        self._auth_expired = True
        from . import registry
        registry().notify_auth_expired(self.slug)

    def probe_auth(self) -> None:
        """One cheap authenticated round-trip.

        Called off-thread at startup and on a timer so a dead session is
        reported on its own, rather than at whatever random moment the user
        next changes songs. Raises on failure; classification/reporting is
        handled here and by the sentinel, so callers can swallow freely.

        Two distinct deaths are checked, because YouTube uses both:
          * HTTP 401 — caught by the sentinel wrapping this call.
          * HTTP 200 carrying the *signed-out* account menu — no error status
            at all, just anonymous data. This is the one that made playback
            "randomly" return things the user doesn't recognise, and it needs
            an explicit check because nothing about it looks like a failure.
        """
        try:
            info = self.yt.get_account_info()
        except Exception as exc:
            if _is_signed_out_payload(exc):
                self._on_auth_error()
            raise
        # Parsed fine but nameless == anonymous session.
        if isinstance(info, dict) and not info.get("accountName"):
            self._on_auth_error()
            raise RuntimeError("youtube music returned a signed-out session")
        self._last_auth_ok = time.time()

    def is_authenticated(self) -> bool:
        return self.yt is not None and not self._signed_out and not self._auth_expired

    def sign_out(self) -> None:
        """Delete the saved cookie auth and mark this live source signed-out.

        Without this override the Sources-tab sign-out button hit the base
        no-op, so the cookie file was never removed and the row kept saying
        "signed in". We delete ``browser.json`` (and the legacy oauth file)
        so the next launch re-runs the import wizard, and flip a flag so the
        row reflects the change immediately. The in-memory ``yt`` client is
        left intact — every browse/search method dereferences it, and the
        established re-auth UX is "restart to sign back in" — so nulling it
        would only invite AttributeErrors before the restart. The embedded
        webview profile is left untouched too, so re-signing-in can harvest
        fresh cookies from the still-live Google session in one click.
        """
        from .. import auth
        auth.clear_saved_auth()
        self._signed_out = True

    def reload_client(self) -> bool:
        """Rebuild the live client from whatever is currently saved on disk.

        The silent "refresh token" path rewrites ``browser.json`` from the
        user's browser without any dialog; this is how the already-running
        source picks those cookies up instead of needing a restart. Returns
        True iff a client could be built.
        """
        from .. import auth
        try:
            self.yt = _AuthSentinel(auth.yt_client(), self._on_auth_error)
        except Exception:
            return False
        self._signed_out = False
        self._auth_expired = False
        self._last_auth_ok = None      # unverified until the next probe
        return True

    def begin_auth(self, parent_widget) -> bool:
        """Run the import wizard and refresh this live source's client so the
        user can sign back in *without restarting tide* — the recovery path
        for ``sign_out()``. Returns True iff auth now exists.

        The wizard writes ``browser.json`` itself on success; we just rebuild
        the YTMusic client from it and clear the signed-out flag so the row
        flips back to authenticated immediately.
        """
        from ..ui.wizard import SignInDialog
        dlg = SignInDialog(parent_widget)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return False
        return self.reload_client()

    def status_text(self) -> str:
        if self._auth_expired and not self._signed_out:
            return "token expired — use [refresh token] to fix"
        if not self.is_authenticated():
            return "sign in via [import]"
        # Lead with when the session was last *verified*, not with the cookie
        # deadline. Google invalidates sessions server-side long before the
        # cookie timestamp lapses — a jar stamped "expires in 394 days" can be
        # dead today — so the countdown alone would be false reassurance.
        parts = ["signed in (cookie import)"]
        if self._last_auth_ok is not None:
            parts.append(f"verified {_ago(time.time() - self._last_auth_ok)} ago")
        from .. import auth
        try:
            remaining = auth.seconds_until_expiry()
        except Exception:
            remaining = None
        # Only worth saying when the cookie deadline is actually the binding
        # constraint; otherwise it's noise.
        if remaining is not None and remaining <= 14 * 86400:
            parts.append(
                "cookies expired" if remaining <= 0
                else f"cookies expire in {_ago(remaining)}"
            )
        return " · ".join(parts)

    # ---------- required ----------

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

    def resolve_stream(self, track: Track) -> StreamRef:
        url = resolve_stream_url(track.video_id)
        return StreamRef(backend="mpv", payload=url)

    # ---------- search filter dispatch ----------

    def search_albums(self, query: str, limit: int = 20) -> list[AlbumEntry]:
        if not query.strip():
            return []
        results = self.yt.search(query, filter="albums", limit=limit) or []
        out: list[AlbumEntry] = []
        for item in results:
            bid = item.get("browseId") or ""
            if not bid:
                continue
            out.append(AlbumEntry(
                browse_id=bid,
                title=item.get("title", "") or "",
                artists=_join_artists(item.get("artists")),
                year=str(item.get("year") or ""),
                thumbnail=_thumb(item.get("thumbnails")),
                playlist_id=item.get("playlistId", "") or "",
            ))
        return out

    def search_artists(self, query: str, limit: int = 20) -> list[ArtistEntry]:
        if not query.strip():
            return []
        results = self.yt.search(query, filter="artists", limit=limit) or []
        out: list[ArtistEntry] = []
        for item in results:
            cid = item.get("browseId") or ""
            if not cid:
                continue
            out.append(ArtistEntry(
                channel_id=cid,
                name=item.get("artist", "") or "",
                thumbnail=_thumb(item.get("thumbnails")),
                subscribers=str(item.get("subscribers") or ""),
            ))
        return out

    def search_videos(self, query: str, limit: int = 20) -> list[Track]:
        if not query.strip():
            return []
        results = self.yt.search(query, filter="videos", limit=limit) or []
        out: list[Track] = []
        for item in results:
            tr = _to_track(item)
            if tr:
                out.append(tr)
        return out

    # ---------- library + discovery ----------

    def get_library_playlists(self, limit: int = 100) -> list[PlaylistEntry]:
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

    def get_playlist(self, playlist_id: str, limit: int = 500) -> PlaylistDetail:
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

    def get_home(self, limit: int = 5) -> list[Shelf]:
        try:
            raw = self.yt.get_home(limit=limit) or []
        except Exception:
            return []
        out: list[Shelf] = []
        for shelf in raw:
            items: list[ShelfItem] = []
            for c in shelf.get("contents", []) or []:
                item = self._shelf_item_from_raw(c)
                if item:
                    items.append(item)
            if items:
                out.append(Shelf(title=shelf.get("title", "") or "", items=items))
        return out

    def _shelf_item_from_raw(self, c: dict) -> ShelfItem | None:
        thumb = _thumb(c.get("thumbnails"))
        title = c.get("title", "") or ""
        if c.get("videoId"):
            tr = _to_track(c)
            if tr is None:
                return None
            return ShelfItem(
                kind="video" if c.get("videoType") == "MUSIC_VIDEO_TYPE_OMV" else "song",
                title=title, subtitle=tr.artists, thumbnail=thumb, track=tr,
            )
        if c.get("browseId", "").startswith("MPREb") or c.get("type") == "Album":
            return ShelfItem(
                kind="album", title=title,
                subtitle=_join_artists(c.get("artists")),
                thumbnail=thumb,
                album=AlbumEntry(
                    browse_id=c.get("browseId", ""),
                    title=title,
                    artists=_join_artists(c.get("artists")),
                    year=str(c.get("year") or ""),
                    thumbnail=thumb,
                    playlist_id=c.get("playlistId", "") or "",
                ),
            )
        if c.get("browseId", "").startswith("UC") and c.get("type") != "playlist":
            return ShelfItem(
                kind="artist", title=title, subtitle="artist", thumbnail=thumb,
                artist=ArtistEntry(
                    channel_id=c.get("browseId", ""),
                    name=title, thumbnail=thumb,
                ),
            )
        if c.get("playlistId"):
            return ShelfItem(
                kind="playlist", title=title,
                subtitle=c.get("description", "") or "",
                thumbnail=thumb,
                playlist=PlaylistEntry(
                    playlist_id=c.get("playlistId", ""),
                    title=title,
                    description=c.get("description", "") or "",
                    thumbnail=thumb,
                ),
            )
        return None

    def get_artist(self, channel_id: str) -> ArtistDetail | None:
        if not channel_id:
            return None
        try:
            raw = self.yt.get_artist(channel_id)
        except Exception:
            return None
        songs: list[Track] = []
        for s in (raw.get("songs", {}) or {}).get("results", []) or []:
            tr = _to_track(s)
            if tr:
                songs.append(tr)
        def _entries(key: str) -> list[AlbumEntry]:
            out: list[AlbumEntry] = []
            for a in (raw.get(key, {}) or {}).get("results", []) or []:
                bid = a.get("browseId") or ""
                if not bid:
                    continue
                out.append(AlbumEntry(
                    browse_id=bid,
                    title=a.get("title", "") or "",
                    artists=_join_artists(a.get("artists")),
                    year=str(a.get("year") or ""),
                    thumbnail=_thumb(a.get("thumbnails")),
                    playlist_id=a.get("playlistId", "") or "",
                ))
            return out
        related: list[ArtistEntry] = []
        for r in (raw.get("related", {}) or {}).get("results", []) or []:
            cid = r.get("browseId") or ""
            if not cid:
                continue
            related.append(ArtistEntry(
                channel_id=cid,
                name=r.get("title", "") or "",
                thumbnail=_thumb(r.get("thumbnails")),
                subscribers=str(r.get("subscribers") or ""),
            ))
        return ArtistDetail(
            channel_id=channel_id,
            name=raw.get("name", "") or "",
            description=raw.get("description", "") or "",
            subscribers=str(raw.get("subscribers") or ""),
            monthly_listeners=str(raw.get("monthlyListeners") or ""),
            thumbnail=_thumb(raw.get("thumbnails")),
            top_songs=songs,
            albums=_entries("albums"),
            singles=_entries("singles"),
            related=related,
        )

    def get_album(self, browse_id: str) -> AlbumDetail | None:
        if not browse_id:
            return None
        try:
            raw = self.yt.get_album(browse_id)
        except Exception:
            return None
        tracks: list[Track] = []
        album_thumb = _thumb(raw.get("thumbnails"))
        album_artists = _join_artists(raw.get("artists"))
        for item in raw.get("tracks", []) or []:
            tr = _to_track(item)
            if tr is None:
                continue
            if not tr.album:
                tr.album = raw.get("title", "") or ""
            if not tr.thumbnail:
                tr.thumbnail = album_thumb
            if not tr.artists:
                tr.artists = album_artists
            tracks.append(tr)
        track_count = int(raw.get("trackCount") or len(tracks))
        return AlbumDetail(
            browse_id=browse_id,
            title=raw.get("title", "") or "",
            artists=album_artists,
            year=str(raw.get("year") or ""),
            duration=str(raw.get("duration") or ""),
            track_count=track_count,
            thumbnail=album_thumb,
            description=raw.get("description", "") or "",
            tracks=tracks,
        )

    # ---------- like / radio / lyrics ----------

    def rate_song(self, video_id: str, liked: bool) -> None:
        if not video_id:
            return
        rating = "LIKE" if liked else "INDIFFERENT"
        self.yt.rate_song(video_id, rating)

    def is_liked(self, video_id: str) -> bool | None:
        if not video_id:
            return None
        try:
            wp = self.yt.get_watch_playlist(videoId=video_id, limit=1)
        except Exception:
            return None
        tracks = wp.get("tracks", []) if isinstance(wp, dict) else []
        if not tracks:
            return None
        status = tracks[0].get("likeStatus")
        if status == "LIKE":
            return True
        if status in ("INDIFFERENT", "DISLIKE"):
            return False
        return None

    def get_lyrics_for(self, video_id: str) -> str | None:
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

    def _yt_timed_lyrics(self, video_id: str):
        """YouTube Music's own line-synced lyrics for this exact video.

        Preferred over LRClib for timing: LRClib matches by title/artist
        text and its sync regularly belongs to a *different* recording
        (album cut vs music video vs remaster), which is exactly what makes
        the karaoke highlight drift off the audio. YT's timestamps are
        authored against the video being played, so they can't mismatch.

        Quirk that shapes this code: YT only serves timestamps to mobile
        clients (``as_mobile()``), and it rejects the mobile context with
        HTTP 400 when the request carries signed-in browser cookies. Lyrics
        are public data, so a dedicated ANONYMOUS client does the timed
        fetch — the signed-in client is never touched. Verified live:
        authed+mobile → 400; anonymous+mobile → hasTimestamps=True.
        """
        from ..lyrics_provider import LyricsResult
        if not video_id:
            return None
        try:
            client = _anon_lyrics_client()
            # as_mobile() mutates the shared client's context for the
            # duration of the block — serialize so overlapping lyric
            # workers (fast track skips) can't interleave contexts.
            with _ANON_LYRICS_LOCK, client.as_mobile():
                wp = client.get_watch_playlist(videoId=video_id)
                browse_id = wp.get("lyrics") if isinstance(wp, dict) else None
                if not browse_id:
                    return None
                lyr = client.get_lyrics(browse_id, timestamps=True)
        except Exception:
            return None
        if not lyr:
            return None
        body = lyr.get("lyrics") if isinstance(lyr, dict) else getattr(lyr, "lyrics", None)
        if not isinstance(body, list):
            return None
        # LyricLine objects (or dicts) with `text` + `start_time` in ms.
        timed: list[tuple[float, str]] = []
        texts: list[str] = []
        for ln in body:
            if isinstance(ln, dict):
                text = str(ln.get("text") or "")
                start = ln.get("start_time")
            else:
                text = str(getattr(ln, "text", "") or "")
                start = getattr(ln, "start_time", None)
            if start is None:
                continue
            try:
                secs = float(start) / 1000.0
            except (TypeError, ValueError):
                continue
            texts.append(text)
            timed.append((secs, text))
        timed.sort(key=lambda x: x[0])
        if not timed:
            return None
        return LyricsResult(plain_text="\n".join(texts), timed_lines=timed)

    def get_lyrics_for_track(self, track: Track):
        from ..lyrics_provider import LyricsResult, fetch_lrclib
        # 1. YT's own sync for this exact video — immune to wrong-version
        #    drift, so it wins whenever it exists.
        timed = self._yt_timed_lyrics(track.video_id)
        if timed is not None:
            return timed
        # 2. Plain lyrics from the signed-in client + LRClib's community
        #    sync as the timing fallback (previous behavior).
        plain = self.get_lyrics_for(track.video_id) or ""
        lrc = fetch_lrclib(
            title=track.title or "",
            artist=track.artists or "",
            album=track.album or "",
            duration_seconds=int(track.duration_seconds or 0),
        )
        if lrc is not None and lrc.has_timed:
            return LyricsResult(plain_text=plain or lrc.plain_text,
                                timed_lines=lrc.timed_lines)
        if plain:
            return LyricsResult(plain_text=plain)
        return lrc

    def get_radio(self, video_id: str, exclude: set[str] | None = None) -> list[Track]:
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


# ---------- yt-dlp stream URL resolution ----------

# When the authenticated (cookiefile) yt-dlp pass fails, every subsequent
# resolve pays for the same doomed attempt before falling back to anonymous
# — roughly doubling the click-to-audio wait until the user re-imports
# cookies. Remember the failure per cookiefile mtime and skip the auth pass
# for a while; a cookie re-import rewrites the jar (new mtime) and re-arms
# it automatically. Worker threads race on this global unlocked — worst
# case is one extra doomed pass, which is today's behavior anyway.
_auth_pass_broken: tuple[float, float] | None = None  # (cookiefile mtime, failed_at)
_AUTH_RETRY_SECS = 30 * 60


def _should_try_auth_pass(cookiefile: str) -> bool:
    if _auth_pass_broken is None:
        return True
    broken_mtime, failed_at = _auth_pass_broken
    try:
        mtime = os.path.getmtime(cookiefile)
    except OSError:
        return False
    if mtime != broken_mtime:
        return True
    return (time.monotonic() - failed_at) >= _AUTH_RETRY_SECS


def resolve_stream_url(video_id: str) -> str:
    """Return a playable audio URL for the given YT Music video id.

    Uses tide.cache for the per-source TTL cache.
    """
    cached_url = cache.get_stream_url(SOURCE_SLUG, video_id)
    if cached_url:
        perf.mark(f"resolve {video_id}: disk-cache hit")
        return cached_url

    global _auth_pass_broken

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "noplaylist": True,
    }
    url = f"https://music.youtube.com/watch?v={video_id}"

    # Resolve as the signed-in user when we have cookies. This is what lets
    # playback work with no browser tab open — yt-dlp authenticates from the
    # cookies harvested at sign-in instead of hitting YouTube anonymously
    # (which triggers bot-checks / age-gates). If the cookies are stale and
    # the authenticated pass fails, fall back to an anonymous resolve so a
    # bad cookie jar can never be *worse* than having none.
    from .. import auth
    cookiefile = auth.yt_dlp_cookiefile()

    info = None
    if cookiefile and _should_try_auth_pass(cookiefile):
        t0 = time.monotonic()
        try:
            with yt_dlp.YoutubeDL({**opts, "cookiefile": cookiefile}) as ydl:
                info = ydl.extract_info(url, download=False)
            _auth_pass_broken = None
            perf.mark(f"resolve {video_id}: auth pass ok "
                      f"({(time.monotonic() - t0) * 1000:.0f}ms)")
        except Exception:
            info = None
            try:
                _auth_pass_broken = (os.path.getmtime(cookiefile),
                                     time.monotonic())
            except OSError:
                pass
            perf.mark(f"resolve {video_id}: auth pass FAILED "
                      f"({(time.monotonic() - t0) * 1000:.0f}ms)")
    elif cookiefile:
        perf.mark(f"resolve {video_id}: auth pass skipped (recent failure)")
    if info is None:
        t0 = time.monotonic()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        perf.mark(f"resolve {video_id}: anon pass "
                  f"({(time.monotonic() - t0) * 1000:.0f}ms)")

    stream_url = info.get("url")
    if not stream_url and "requested_formats" in info:
        stream_url = info["requested_formats"][0].get("url")
    if not stream_url:
        raise RuntimeError(f"no playable audio stream for {video_id}")

    cache.put_stream_url(SOURCE_SLUG, video_id, stream_url, ttl_seconds=YTMusicSource.STREAM_TTL_SECONDS)
    return stream_url
