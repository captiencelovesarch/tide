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
import shutil
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
    Comment,
    CreditSection,
    MusicSource,
    NotSupportedError,
    PlaylistDetail,
    PlaylistEntry,
    Shelf,
    ShelfItem,
    SongInsights,
    StreamRef,
    Track,
    parse_count,
    safe_int,
)


SOURCE_SLUG = "ytmusic"

# JSON-cache namespaces + TTLs for the v1.5 community layer. Insights go
# stale fast enough to matter (view counts move); related/credits are
# editorial and effectively static per video.
_NS_INSIGHTS = "ytmusic.insights"
_INSIGHTS_TTL = 24 * 3600
_NS_RELATED = "ytmusic.related"
_RELATED_TTL = 7 * 86400
_NS_CREDITS = "ytmusic.credits"
_CREDITS_TTL = 7 * 86400
# Home-engine feeds: charts refresh daily-ish, moods are near-static.
_NS_BROWSE = "ytmusic.browse"
_EXPLORE_TTL = 6 * 3600
_CHARTS_TTL = 6 * 3600
_MOODS_TTL = 24 * 3600

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


def _rel_time(epoch: object) -> str:
    """Comment-age form: '2y', '8mo', '3d'. Empty when the timestamp is
    missing/garbage — the row just shows no age."""
    ts = safe_int(epoch)
    if ts <= 0:
        return ""
    delta = max(0.0, time.time() - ts)
    if delta >= 365 * 86400:
        return f"{int(delta // (365 * 86400))}y"
    if delta >= 30 * 86400:
        return f"{int(delta // (30 * 86400))}mo"
    return _ago(delta)


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


# Radio paging: one watch-playlist page is about 50 tracks; past a few
# pages the radio has wandered anyway, and the anchor moves on.
_RADIO_PAGE = 50
_RADIO_MAX = 250
# videoType values radio leaves out (see get_radio).
_UGC = "MUSIC_VIDEO_TYPE_UGC"
_RADIO_SKIP_TYPES = (_UGC, "MUSIC_VIDEO_TYPE_PODCAST_EPISODE")


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
        "insights", "comments", "credits", "related", "history_sync",
        "explore", "charts", "moods",
        "library_full", "playlist_edit", "subscribe", "remote_history",
        "playlist_search", "suggest", "taste",
    })

    STREAM_TTL_SECONDS = 4 * 3600          # YT CDN URLs last ~6h

    # get_song responses memoized per session: one track start wants the
    # same payload twice (insights for the strip + the history ping), and
    # re-opening the song page shouldn't re-fetch either.
    _SONG_MEMO_CAP = 32

    def __init__(self, yt: YTMusic | None) -> None:
        # ``yt`` is None when nothing is signed in yet (no browser.json, or
        # the user cancelled the import). The source still constructs so it
        # can be registered and hold down its Sources row; begin_auth() /
        # reload_client() fill the client in live, no restart needed. Note
        # the sentinel is NOT wrapped around None — is_authenticated() tests
        # ``self.yt is not None``, and a wrapper would read as signed in.
        self.yt = _AuthSentinel(yt, self._on_auth_error) if yt is not None else None
        self._signed_out = yt is None
        self._auth_expired = False
        # Unix time of the last probe that came back genuinely signed in.
        self._last_auth_ok: float | None = None
        self._song_memo: dict[str, dict] = {}
        self._song_memo_lock = threading.Lock()
        # Filled by probe_auth from get_account_info; "" until verified.
        self.account_name = ""

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
        # The home greeting wants a first name; the probe already paid for
        # the round-trip, so bank it.
        if isinstance(info, dict):
            self.account_name = str(info.get("accountName") or "")

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
            return "token expired. use [refresh token] to fix"
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
        subscribed = raw.get("subscribed")
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
            subscribed=bool(subscribed) if subscribed is not None else None,
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
            playlist_id=str(raw.get("audioPlaylistId") or ""),
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

    def get_radio(self, video_id: str, exclude: set[str] | None = None,
                  depth: int = 0) -> list[Track]:
        """The seed's radio, read ``depth`` pages further down each time
        (ytmusicapi follows the continuations up to ``limit``), so a long
        session stays on the radio of the song the user picked.

        Fan uploads and podcast episodes are dropped unless the seed is a
        fan upload itself: a radio seeded from a regular song otherwise
        drifts into slowed / sped-up re-uploads and, now and then, a
        podcast."""
        if not video_id:
            return []
        excluded = set(exclude or ())
        excluded.add(video_id)
        limit = min(_RADIO_PAGE * (max(0, int(depth)) + 1), _RADIO_MAX)
        res = self.yt.get_watch_playlist(videoId=video_id, radio=True,
                                         limit=limit)
        items = res.get("tracks", []) or []
        seed_type = next((item.get("videoType") for item in items
                          if item.get("videoId") == video_id), None)
        banned = set(_RADIO_SKIP_TYPES)
        if seed_type == _UGC:
            banned.discard(_UGC)
        out: list[Track] = []
        for item in items:
            if item.get("videoType") in banned:
                continue
            tr = _to_track(item)
            if not tr or tr.video_id in excluded:
                continue
            excluded.add(tr.video_id)
            out.append(tr)
        return out

    # ---------- v1.5 community / depth ----------

    def dislike_song(self, video_id: str) -> None:
        if not video_id:
            return
        self.yt.rate_song(video_id, "DISLIKE")

    def _get_song_memo(self, video_id: str) -> dict | None:
        """``get_song`` with a small per-session memo. None on any failure —
        callers treat a missing payload as 'no insights', never as an error
        state worth surfacing."""
        with self._song_memo_lock:
            hit = self._song_memo.get(video_id)
        if hit is not None:
            return hit
        try:
            song = self.yt.get_song(video_id)
        except Exception:
            return None
        if not isinstance(song, dict):
            return None
        with self._song_memo_lock:
            self._song_memo[video_id] = song
            while len(self._song_memo) > self._SONG_MEMO_CAP:
                self._song_memo.pop(next(iter(self._song_memo)))
        return song

    @staticmethod
    def _harvest_song_payload(video_id: str, song: dict) -> dict:
        """Merge what ``get_song`` knows (views, channel, year) into the
        insights cache. Likes aren't in this payload — those come from the
        yt-dlp harvest — which is why this merges instead of overwriting."""
        partial: dict = {"_song": True}
        vd = song.get("videoDetails") or {}
        views = parse_count(vd.get("viewCount"))
        if views:
            partial["views"] = views
        if vd.get("author"):
            partial["channel"] = str(vd["author"])
        mf = (song.get("microformat") or {}).get("microformatDataRenderer") or {}
        date = str(mf.get("publishDate") or mf.get("uploadDate") or "")
        if len(date) >= 4 and date[:4].isdigit():
            partial["year"] = date[:4]
        return cache.update_json(_NS_INSIGHTS, video_id, partial, _INSIGHTS_TTL)

    @staticmethod
    def _insights_from_dict(video_id: str, d: dict) -> SongInsights:
        return SongInsights(
            video_id=video_id,
            views=safe_int(d.get("views")),
            likes=safe_int(d.get("likes")),
            comment_count=safe_int(d.get("comment_count")),
            year=str(d.get("year") or ""),
            channel=str(d.get("channel") or ""),
        )

    def get_song_insights(self, video_id: str) -> SongInsights | None:
        if not video_id:
            return None
        cached = cache.get_json(_NS_INSIGHTS, video_id)
        # The cache may hold a partial written by the yt-dlp harvest (likes
        # but no views). "_song" marks that get_song already contributed;
        # without it, one round-trip fills the rest.
        if isinstance(cached, dict) and cached.get("_song"):
            return self._insights_from_dict(video_id, cached)
        song = self._get_song_memo(video_id)
        if song is not None:
            merged = self._harvest_song_payload(video_id, song)
            return self._insights_from_dict(video_id, merged)
        if isinstance(cached, dict):
            return self._insights_from_dict(video_id, cached)
        return None

    def report_play(self, track: Track) -> bool:
        """The 'I listened to this' ping — the same videostats call the YT
        Music web player sends. This is what makes the account's history,
        Listen Again, and recommendations learn from plays in tide. Only
        invoked when the user opted in (see settings.report_plays).
        """
        if not track.video_id or not self.is_authenticated():
            return False
        song = self._get_song_memo(track.video_id)
        if song is None:
            return False
        self._harvest_song_payload(track.video_id, song)
        try:
            resp = self.yt.add_history_item(song)
        except KeyError:
            # Signed-out/limited get_song payloads lack playbackTracking.
            return False
        status = getattr(resp, "status_code", None)
        return status in (200, 204)

    def get_related_for(self, track: Track) -> list[Shelf]:
        """Related-content shelves ("you might also like", "other
        performances", "about the artist"). Two round-trips on a cache miss:
        the watch playlist carries the related browseId, then the browse
        itself. Raw payload is cached; parsing re-runs per call (cheap, and
        it keeps the cache format decoupled from the dataclasses)."""
        vid = track.video_id
        if not vid:
            return []
        raw = cache.get_json(_NS_RELATED, vid)
        if raw is None:
            try:
                wp = self.yt.get_watch_playlist(videoId=vid, limit=1)
                browse_id = wp.get("related") if isinstance(wp, dict) else None
                if not browse_id:
                    return []
                raw = self.yt.get_song_related(browse_id) or []
            except Exception:
                return []
            cache.put_json(_NS_RELATED, vid, raw, _RELATED_TTL)
        out: list[Shelf] = []
        for shelf in raw:
            if not isinstance(shelf, dict):
                continue
            contents = shelf.get("contents")
            # The "About the artist" section arrives with a description
            # string where every other shelf has a list — the artist page
            # owns that text; skip it here.
            if not isinstance(contents, list):
                continue
            items: list[ShelfItem] = []
            for c in contents:
                if not isinstance(c, dict):
                    continue
                item = self._shelf_item_from_raw(c)
                if item:
                    items.append(item)
            if items:
                out.append(Shelf(title=shelf.get("title", "") or "", items=items))
        return out

    def get_credits_for(self, track: Track) -> list[CreditSection]:
        """Song credits. The MPTC credits browseId only rides along on album
        track listings, so tracks that arrived via search/home need their
        album fetched first to find themselves in it. Cached per video —
        including the empty result, so credit-less tracks don't re-fetch an
        album on every song-page open."""
        vid = track.video_id
        if not vid:
            return []
        cached = cache.get_json(_NS_CREDITS, vid)
        if cached is not None:
            return [CreditSection(title=s.get("title", ""), names=list(s.get("names") or []))
                    for s in cached if isinstance(s, dict)]
        credits_id = str(track.extras.get("creditsBrowseId") or "")
        if not credits_id:
            album = track.extras.get("album")
            album_id = album.get("id", "") if isinstance(album, dict) else ""
            if album_id:
                try:
                    raw_album = self.yt.get_album(album_id) or {}
                except Exception:
                    return []
                for t in raw_album.get("tracks", []) or []:
                    if isinstance(t, dict) and t.get("videoId") == vid:
                        credits_id = str(t.get("creditsBrowseId") or "")
                        break
        sections: list[dict] = []
        if credits_id:
            try:
                raw = self.yt.get_song_credits(credits_id) or {}
            except Exception:
                return []
            order = ("performed_by", "written_by", "produced_by")
            for key in order:
                block = raw.get(key)
                if isinstance(block, dict) and block.get("data"):
                    sections.append({
                        "title": str(block.get("localized_title")
                                     or key.replace("_", " ")),
                        "names": [str(n) for n in block["data"]],
                    })
            for block in raw.get("other_sections") or []:
                if isinstance(block, dict) and block.get("data"):
                    sections.append({
                        "title": str(block.get("localized_title") or ""),
                        "names": [str(n) for n in block["data"]],
                    })
            meta = raw.get("music_metadata_provided_by")
            if isinstance(meta, dict) and meta.get("data"):
                sections.append({
                    "title": str(meta.get("localized_title")
                                 or "music metadata provided by"),
                    "names": [str(n) for n in meta["data"]],
                })
        cache.put_json(_NS_CREDITS, vid, sections, _CREDITS_TTL)
        return [CreditSection(title=s["title"], names=s["names"]) for s in sections]

    # ---------- v1.5 browse (home engine feeds) ----------

    def _album_entry_from_raw(self, a: dict) -> AlbumEntry | None:
        bid = a.get("browseId") or ""
        if not bid:
            return None
        return AlbumEntry(
            browse_id=bid,
            title=a.get("title", "") or "",
            artists=_join_artists(a.get("artists")),
            year=str(a.get("year") or ""),
            thumbnail=_thumb(a.get("thumbnails")),
            playlist_id=a.get("audioPlaylistId", "") or a.get("playlistId", "") or "",
        )

    @staticmethod
    def _chart_entries(items: list, to_item) -> list:
        """Wrap parsed chart rows in ChartEntry, tolerating rank garbage.
        Unranked payloads (anonymous charts) get positional ranks so the
        list still reads as a chart."""
        from .base import ChartEntry
        out = []
        for i, raw in enumerate(items or []):
            if not isinstance(raw, dict):
                continue
            item = to_item(raw)
            if item is None:
                continue
            rank = safe_int(raw.get("rank"), 0) or (i + 1)
            trend = str(raw.get("trend") or "").lower()
            out.append(ChartEntry(rank=rank, trend=trend, item=item))
        return out

    def get_explore_data(self) -> dict:
        """New releases + top songs + new videos + moods, one browse call.

        Cached raw for 6h. ``top_songs`` only exists on premium sessions —
        the home engine renders whatever keys come back and no block is
        mandatory. Moods fall back to the dedicated categories browse when
        the explore payload omits them (they're near-static, 24h cache).
        """
        from .base import MoodCategory
        raw = cache.get_json(_NS_BROWSE, "explore")
        if raw is None:
            try:
                raw = self.yt.get_explore() or {}
            except Exception:
                raw = {}
            if raw:
                cache.put_json(_NS_BROWSE, "explore", raw, _EXPLORE_TTL)
        out: dict = {}
        releases = []
        for a in raw.get("new_releases") or []:
            entry = self._album_entry_from_raw(a) if isinstance(a, dict) else None
            if entry is not None:
                releases.append(entry)
        if releases:
            out["new_releases"] = releases
        top = raw.get("top_songs")
        if isinstance(top, dict) and top.get("items"):
            def _song_item(r: dict) -> ShelfItem | None:
                tr = _to_track(r)
                if tr is None:
                    return None
                return ShelfItem(kind="song", title=tr.title,
                                 subtitle=tr.artists, thumbnail=tr.thumbnail,
                                 track=tr)
            entries = self._chart_entries(top["items"], _song_item)
            if entries:
                out["top_songs"] = entries
        videos = []
        for v in raw.get("new_videos") or []:
            tr = _to_track(v) if isinstance(v, dict) else None
            if tr is not None:
                videos.append(tr)
        if videos:
            out["new_videos"] = videos
        moods = raw.get("moods_and_genres")
        sections: list = []
        if moods:
            cats = [MoodCategory(title=str(m.get("title") or ""),
                                 params=str(m.get("params") or ""))
                    for m in moods if isinstance(m, dict) and m.get("params")]
            if cats:
                sections = [("moods", cats)]
        if not sections:
            sections = self._mood_sections()
        if sections:
            out["moods"] = sections
        return out

    def _mood_sections(self) -> list:
        from .base import MoodCategory
        raw = cache.get_json(_NS_BROWSE, "moods")
        if raw is None:
            try:
                raw = self.yt.get_mood_categories() or {}
            except Exception:
                return []
            if raw:
                cache.put_json(_NS_BROWSE, "moods", raw, _MOODS_TTL)
        sections = []
        for title, cats in raw.items():
            if not isinstance(cats, list):
                continue
            parsed = [MoodCategory(title=str(c.get("title") or ""),
                                   params=str(c.get("params") or ""))
                      for c in cats if isinstance(c, dict) and c.get("params")]
            if parsed:
                sections.append((str(title), parsed))
        return sections

    def get_charts_data(self, country: str = "ZZ") -> dict:
        """Ranked artists + chart playlists for one country. Anonymous
        sessions get unranked artists (positional ranks stand in) and the
        songs chart lives in explore — this bundle is the reliable part."""
        key = f"charts.{country or 'ZZ'}"
        raw = cache.get_json(_NS_BROWSE, key)
        if raw is None:
            try:
                raw = self.yt.get_charts(country=country or "ZZ") or {}
            except Exception:
                raw = {}
            if raw:
                cache.put_json(_NS_BROWSE, key, raw, _CHARTS_TTL)
        out: dict = {}
        def _artist_item(r: dict) -> ShelfItem | None:
            cid = r.get("browseId") or ""
            if not cid:
                return None
            subs = str(r.get("subscribers") or "")
            return ShelfItem(
                kind="artist", title=r.get("title", "") or "",
                subtitle=(f"{subs} subscribers" if subs else "artist"),
                thumbnail=_thumb(r.get("thumbnails")),
                artist=ArtistEntry(channel_id=cid,
                                   name=r.get("title", "") or "",
                                   thumbnail=_thumb(r.get("thumbnails")),
                                   subscribers=subs),
            )
        artists = self._chart_entries(raw.get("artists"), _artist_item)
        if artists:
            out["artists"] = artists
        playlists = []
        for name in ("daily", "weekly", "videos", "genres", "trending"):
            for p in raw.get(name) or []:
                if not isinstance(p, dict) or not p.get("playlistId"):
                    continue
                playlists.append(PlaylistEntry(
                    playlist_id=p["playlistId"],
                    title=p.get("title", "") or "",
                    thumbnail=_thumb(p.get("thumbnails")),
                ))
        if playlists:
            out["playlists"] = playlists
        selected = ((raw.get("countries") or {}).get("selected") or {})
        label = selected.get("text") if isinstance(selected, dict) else str(selected or "")
        if label:
            out["selected"] = str(label)
        return out

    def get_mood_playlists_list(self, params: str) -> list[PlaylistEntry]:
        if not params:
            return []
        key = f"mood.{params[:48]}"
        raw = cache.get_json(_NS_BROWSE, key)
        if raw is None:
            try:
                raw = self.yt.get_mood_playlists(params) or []
            except Exception:
                return []
            cache.put_json(_NS_BROWSE, key, raw, _MOODS_TTL)
        out = []
        for p in raw:
            if not isinstance(p, dict) or not p.get("playlistId"):
                continue
            out.append(PlaylistEntry(
                playlist_id=p["playlistId"],
                title=p.get("title", "") or "",
                description=_join_artists(p.get("author")) or str(p.get("description") or ""),
                thumbnail=_thumb(p.get("thumbnails")),
            ))
        return out

    # ---------- v1.5 library parity ----------

    _ORDERS = ("a_to_z", "z_to_a", "recently_added")

    def get_library_songs_list(self, limit: int = 200,
                               order: str | None = None) -> list[Track]:
        kwargs: dict = {"limit": limit}
        if order in self._ORDERS:
            kwargs["order"] = order
        items = self.yt.get_library_songs(**kwargs) or []
        out: list[Track] = []
        for item in items:
            tr = _to_track(item)
            if tr:
                out.append(tr)
        return out

    def get_library_albums_list(self, limit: int = 100,
                                order: str | None = None) -> list[AlbumEntry]:
        kwargs: dict = {"limit": limit}
        if order in self._ORDERS:
            kwargs["order"] = order
        items = self.yt.get_library_albums(**kwargs) or []
        out: list[AlbumEntry] = []
        for a in items:
            entry = self._album_entry_from_raw(a) if isinstance(a, dict) else None
            if entry is not None:
                out.append(entry)
        return out

    def _artist_entries(self, items: list) -> list[ArtistEntry]:
        out: list[ArtistEntry] = []
        for r in items or []:
            if not isinstance(r, dict):
                continue
            cid = r.get("browseId") or ""
            if not cid:
                continue
            out.append(ArtistEntry(
                channel_id=cid,
                name=r.get("artist", "") or r.get("title", "") or "",
                thumbnail=_thumb(r.get("thumbnails")),
                subscribers=str(r.get("subscribers") or ""),
            ))
        return out

    def get_library_artists_list(self, limit: int = 100,
                                 order: str | None = None) -> list[ArtistEntry]:
        kwargs: dict = {"limit": limit}
        if order in self._ORDERS:
            kwargs["order"] = order
        return self._artist_entries(self.yt.get_library_artists(**kwargs))

    def get_library_subscriptions_list(self, limit: int = 100) -> list[ArtistEntry]:
        return self._artist_entries(
            self.yt.get_library_subscriptions(limit=limit))

    def create_playlist_remote(self, title: str, description: str = "",
                               video_ids: list | None = None) -> str:
        res = self.yt.create_playlist(title, description,
                                      video_ids=list(video_ids or []) or None)
        # Success is a bare playlist-id string; failure comes back as the
        # raw response dict.
        return res if isinstance(res, str) else ""

    def add_to_playlist(self, playlist_id: str, video_ids: list) -> bool:
        if not playlist_id or not video_ids:
            return False
        res = self.yt.add_playlist_items(playlist_id, videoIds=list(video_ids))
        status = res.get("status") if isinstance(res, dict) else res
        return "SUCCEEDED" in str(status)

    def remove_from_playlist(self, playlist_id: str, tracks: list) -> bool:
        """YT wants videoId + setVideoId per row; both ride in the raw
        playlist item that get_playlist stashed into track.extras. Tracks
        that arrived any other way can't be removed remotely — skip them."""
        videos = []
        for tr in tracks:
            extras = getattr(tr, "extras", None) or {}
            if extras.get("videoId") and extras.get("setVideoId"):
                videos.append({"videoId": extras["videoId"],
                               "setVideoId": extras["setVideoId"]})
        if not playlist_id or not videos:
            return False
        res = self.yt.remove_playlist_items(playlist_id, videos)
        return "SUCCEEDED" in str(res)

    def edit_playlist_remote(self, playlist_id: str, *, title: str | None = None,
                             description: str | None = None,
                             privacy: str | None = None) -> bool:
        if not playlist_id:
            return False
        kwargs: dict = {}
        if title is not None:
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if privacy is not None:
            kwargs["privacyStatus"] = privacy
        if not kwargs:
            return True
        res = self.yt.edit_playlist(playlist_id, **kwargs)
        return "SUCCEEDED" in str(res)

    def delete_playlist_remote(self, playlist_id: str) -> bool:
        if not playlist_id:
            return False
        res = self.yt.delete_playlist(playlist_id)
        return "SUCCEEDED" in str(res) or isinstance(res, str)

    def set_artist_subscribed(self, channel_id: str, subscribed: bool) -> bool:
        if not channel_id:
            return False
        if subscribed:
            self.yt.subscribe_artists([channel_id])
        else:
            self.yt.unsubscribe_artists([channel_id])
        return True

    def add_album_to_library(self, album) -> bool:
        pid = getattr(album, "playlist_id", "") or ""
        if not pid:
            return False
        res = self.yt.rate_playlist(pid, "LIKE")
        return res is not None

    def get_remote_history(self, limit: int = 200) -> list[Track]:
        items = self.yt.get_history() or []
        out: list[Track] = []
        for item in items[:limit]:
            tr = _to_track(item)
            if tr is None:
                continue
            # get_history rows carry "played" (period label) + feedbackToken
            # for removal — both stay in extras for the history view.
            out.append(tr)
        return out

    def remove_remote_history(self, tracks: list) -> bool:
        tokens = [t.extras.get("feedbackToken")
                  for t in tracks
                  if getattr(t, "extras", None) and t.extras.get("feedbackToken")]
        if not tokens:
            return False
        self.yt.remove_history_items(tokens)
        return True

    def search_playlists(self, query: str, limit: int = 20) -> list[PlaylistEntry]:
        if not query.strip():
            return []
        results = self.yt.search(query, filter="playlists", limit=limit) or []
        out: list[PlaylistEntry] = []
        for item in results:
            pid = item.get("browseId") or item.get("playlistId") or ""
            # Playlist browse ids arrive as "VL<id>" — the playlist endpoint
            # wants the bare id.
            if pid.startswith("VL"):
                pid = pid[2:]
            if not pid:
                continue
            out.append(PlaylistEntry(
                playlist_id=pid,
                title=item.get("title", "") or "",
                description=str(item.get("author") or item.get("itemCount") or ""),
                thumbnail=_thumb(item.get("thumbnails")),
            ))
        return out

    def get_search_suggestions_list(self, query: str) -> list[str]:
        if not query.strip():
            return []
        out = self.yt.get_search_suggestions(query) or []
        return [s for s in out if isinstance(s, str)]

    def get_taste_profile(self) -> list[str]:
        raw = self.yt.get_tasteprofile() or {}
        return sorted(k for k in raw.keys() if isinstance(k, str))

    def set_taste_profile(self, artists: list) -> bool:
        names = [str(a) for a in artists if a]
        if not names:
            return False
        self.yt.set_tasteprofile(names)
        return True

    def get_comments(self, video_id: str, *, sort: str = "top",
                     limit: int = 60) -> list[Comment]:
        """Public comments via yt-dlp (ytmusicapi has no comments surface).

        Deliberately ANONYMOUS — comments are public data, and this is a
        yt-dlp extraction that must never be able to touch (or depend on)
        the cookie jar. Slow by nature (one full extraction + N comment
        pages, seconds); callers own the worker thread and the spinner.
        Not cached here: the comments view session-caches per video, and a
        sort flip should genuinely re-fetch.
        """
        if not video_id:
            return []
        sort_key = "new" if sort == "new" else "top"
        # max_comments spec: max-comments,max-parents,max-replies,
        # max-replies-per-thread. A small reply budget keeps threads
        # skimmable without stretching the fetch.
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "getcomments": True,
            "extractor_args": {"youtube": {
                "max_comments": [str(limit + 40), str(limit), "40", "5"],
                "comment_sort": [sort_key],
            }},
        }
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return []
        # A full watch-page extraction rode along — bank its counts.
        _harvest_ytdlp_info(video_id, info)
        out: list[Comment] = []
        for c in info.get("comments") or []:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()
            if not text:
                continue
            parent = str(c.get("parent") or "")
            out.append(Comment(
                comment_id=str(c.get("id") or ""),
                author=str(c.get("author") or ""),
                text=text,
                likes=safe_int(c.get("like_count")),
                time_text=_rel_time(c.get("timestamp")),
                parent_id="" if parent == "root" else parent,
                pinned=bool(c.get("is_pinned")),
                hearted=bool(c.get("is_favorited")),
                by_uploader=bool(c.get("author_is_uploader")),
                author_thumbnail=str(c.get("author_thumbnail") or ""),
            ))
        return out


# ---------- yt-dlp stream URL resolution ----------

# When the authenticated (cookiefile) yt-dlp pass fails *because the jar is
# dead*, every subsequent resolve pays for the same doomed attempt before
# falling back — roughly doubling the click-to-audio wait until the user
# re-imports cookies. Remember the failure per cookiefile mtime and skip the
# auth pass for a while; a cookie re-import rewrites the jar (new mtime) and
# re-arms it automatically. Worker threads race on this global unlocked —
# worst case is one extra doomed pass, which is today's behavior anyway.
_auth_pass_broken: tuple[float, float] | None = None  # (cookiefile mtime, failed_at)
_AUTH_RETRY_SECS = 30 * 60

# Only jar-shaped failures may trip the memo. YouTube gates *individual
# songs* behind PO tokens now, and for those the auth pass dies with
# "Requested format is not available" — a per-song condition. Tripping the
# memo on it turned one gated song into thirty minutes of anonymous
# resolves for EVERY song, and the anonymous default client's URLs are the
# ones that 403 in mpv (see below), so a single gated track poisoned the
# whole session.
#
# Deliberately narrow. YouTube's bot-check reads "Sign in to confirm
# you're not a bot. Use --cookies-from-browser or --cookies for the
# authentication." — IP reputation, not the jar — so neither "sign in"
# nor "cookie" is safe to match (the remedy text names cookies!). It hit
# a live session once and converted a transient hiccup into 30 minutes of
# anonymous-only resolves, which mostly fail outright now — the player
# looked completely broken until a restart cleared the memo.
# "no longer valid" is yt-dlp's actual rotated-cookies message ("The
# provided YouTube account cookies are no longer valid…").
_JAR_ERROR_MARKERS = ("no longer valid", "401", "unauthorized")

# Guards the cookie jar FILE, never a network call. YoutubeDL reads the jar
# at construction and REWRITES it on close (save_cookies — this is how
# rotated cookies persist), so two auth passes sharing one file race a
# truncating write against a read and the loser sees a half-written jar.
# The first fix held this lock across the whole extraction, which queued
# every auth resolve behind the others: warm() fires five 500ms apart, each
# pass takes ~4s, and a click on the fifth track waited ~19s (measured).
# Now each pass runs on its own snapshot of the jar and only the copy in
# and the atomic swap back out are serialized.
_AUTH_JAR_LOCK = threading.Lock()


def _looks_like_dead_jar(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _JAR_ERROR_MARKERS)


# Clients retried, in order, when the default resolve raises or hands back a
# dead URL. YouTube's per-song PO-token gating breaks the default path two
# ways at once: the cookie/web pass loses all its formats ("Requested format
# is not available") and the anonymous default (android_vr) still *returns*
# a URL — which then answers HTTP 403 to every request mpv makes.
# web_music is what the YT Music web player itself uses and keeps serving a
# playable (muxed) format for gated songs; android is the backstop. Both
# verified live against gated tracks. Run anonymously on purpose: they're
# the escape hatch, so a dead cookie jar must not be able to break them.
_FALLBACK_CLIENTS = ("web_music", "android")

# The probe mimics the request it is standing in for: mpv opens the stream
# with a plain unranged GET.
_PROBE_UA = "mpv 0.40.0"


def _stream_url_alive(stream_url: str, timeout: float = 5.0) -> bool:
    """One cheap GET against the resolved URL — headers only, then close.

    yt-dlp "succeeding" no longer means the URL works: PO-token-gated
    formats extract fine and then 403 on first contact. Catching that here
    costs one ~100ms round trip and is the difference between trying the
    next client and caching a poison URL for four hours.
    """
    req = urllib.request.Request(stream_url, headers={"User-Agent": _PROBE_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


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


def _extract_stream_url(url: str, opts: dict) -> tuple[str | None, dict]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        return None, {}
    stream_url = info.get("url")
    if not stream_url and "requested_formats" in info:
        stream_url = info["requested_formats"][0].get("url")
    return stream_url, info


def _extract_with_jar_snapshot(url: str, opts: dict) -> tuple[str | None, dict]:
    """Run an auth pass against a private copy of the cookie jar.

    yt-dlp gets a snapshot it can read and rewrite freely; if it rotated
    anything, the snapshot replaces the real jar with an atomic rename, so
    a concurrent reader sees the old file or the new one, never half of
    either. Two passes that both rotate: last one wins, and both results
    are cookies YouTube just handed out.
    """
    jar = opts["cookiefile"]
    fd, snap = tempfile.mkstemp(prefix=".yt_cookies.", suffix=".txt",
                                dir=os.path.dirname(jar) or None)
    os.close(fd)
    try:
        with _AUTH_JAR_LOCK:
            shutil.copyfile(jar, snap)
        with open(snap, "rb") as fh:
            before = fh.read()
        try:
            return _extract_stream_url(url, {**opts, "cookiefile": snap})
        finally:
            try:
                with open(snap, "rb") as fh:
                    rotated = fh.read() != before
                if rotated:
                    with _AUTH_JAR_LOCK:
                        os.replace(snap, jar)
            except OSError:
                pass
    finally:
        try:
            os.unlink(snap)
        except OSError:
            pass


def _harvest_ytdlp_info(video_id: str, info: dict) -> None:
    """Bank the community numbers riding along on a yt-dlp extraction.

    Every resolve already downloads view/like/comment counts and then used
    to discard them — for the playing track, insights are free. Merged (not
    overwritten) because ``get_song`` writes views/year into the same entry
    and either side can land first.
    """
    partial: dict = {}
    for src_key, dst_key in (("view_count", "views"),
                             ("like_count", "likes"),
                             ("comment_count", "comment_count")):
        v = safe_int(info.get(src_key))
        if v:
            partial[dst_key] = v
    channel = info.get("channel") or info.get("uploader") or ""
    if channel:
        partial["channel"] = str(channel)
    upload_date = str(info.get("upload_date") or "")
    if len(upload_date) >= 4 and upload_date[:4].isdigit():
        partial["year"] = upload_date[:4]
    if partial:
        try:
            cache.update_json(_NS_INSIGHTS, video_id, partial, _INSIGHTS_TTL)
        except Exception:
            pass


def resolve_stream_url(video_id: str) -> str:
    """Return a playable audio URL for the given YT Music video id.

    Uses tide.cache for the per-source TTL cache. A URL is only returned
    (and only cached) after answering an HTTP probe, because extraction
    success stopped implying playability — see ``_stream_url_alive``.
    """
    cached_url = cache.get_stream_url(SOURCE_SLUG, video_id)
    if cached_url:
        # A hit is only a hit if the URL still answers. CDN URLs die inside
        # our TTL for reasons we can't see from here (network/IP change,
        # server-side invalidation), and handing mpv a dead one costs a
        # "player error" the user has to notice and retry. ~100ms to check.
        if _stream_url_alive(cached_url):
            perf.mark(f"resolve {video_id}: disk-cache hit")
            return cached_url
        cache.remove_stream_url(SOURCE_SLUG, video_id)
        perf.mark(f"resolve {video_id}: disk-cache entry dead — re-resolving")

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

    def run_pass(label: str, pass_opts: dict) -> tuple[str, dict] | None:
        """One extraction. The live URL and its info, or None."""
        global _auth_pass_broken
        t0 = time.monotonic()
        try:
            if label == "auth":
                stream_url, info = _extract_with_jar_snapshot(url, pass_opts)
            else:
                stream_url, info = _extract_stream_url(url, pass_opts)
        except Exception as exc:
            if label == "auth" and _looks_like_dead_jar(exc):
                try:
                    _auth_pass_broken = (os.path.getmtime(cookiefile),
                                         time.monotonic())
                except OSError:
                    pass
            perf.mark(f"resolve {video_id}: {label} pass FAILED "
                      f"({(time.monotonic() - t0) * 1000:.0f}ms)")
            return None
        if label == "auth":
            _auth_pass_broken = None
        if not stream_url:
            perf.mark(f"resolve {video_id}: {label} pass returned no url")
            return None
        if not _stream_url_alive(stream_url):
            perf.mark(f"resolve {video_id}: {label} pass URL dead on probe "
                      f"({(time.monotonic() - t0) * 1000:.0f}ms)")
            return None
        perf.mark(f"resolve {video_id}: {label} pass ok "
                  f"({(time.monotonic() - t0) * 1000:.0f}ms)")
        return stream_url, info

    def accept(hit: tuple[str, dict]) -> str:
        stream_url, info = hit
        _harvest_ytdlp_info(video_id, info)
        cache.put_stream_url(SOURCE_SLUG, video_id, stream_url,
                             ttl_seconds=YTMusicSource.STREAM_TTL_SECONDS)
        return stream_url

    if cookiefile and _should_try_auth_pass(cookiefile):
        hit = run_pass("auth", {**opts, "cookiefile": cookiefile})
        if hit is not None:
            return accept(hit)
    elif cookiefile:
        perf.mark(f"resolve {video_id}: auth pass skipped (recent failure)")

    # The fallbacks run side by side and the first one in preference order
    # that comes back live wins. Back to back, a gated song paid a full
    # ~4s extraction per client before reaching the one that works.
    fallbacks: list[tuple[str, dict]] = [("anon", opts)]
    for client in _FALLBACK_CLIENTS:
        fallbacks.append((client, {
            **opts,
            "extractor_args": {"youtube": {"player_client": [client]}},
        }))
    pool = ThreadPoolExecutor(max_workers=len(fallbacks),
                              thread_name_prefix="tide-resolve")
    try:
        futures = [pool.submit(run_pass, label, pass_opts)
                   for label, pass_opts in fallbacks]
        for fut in futures:
            hit = fut.result()
            if hit is not None:
                return accept(hit)
    finally:
        # Don't wait on the losers; they finish in the background and
        # their results are dropped.
        pool.shutdown(wait=False, cancel_futures=True)

    raise RuntimeError(f"no playable audio stream for {video_id}")
