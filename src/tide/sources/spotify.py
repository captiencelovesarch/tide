"""Spotify source.

Tide v1.2.1 — first paid-streaming source. Metadata flows through the
Spotify Web API (via spotipy); audio playback flows through ``librespot``
running as a Spotify Connect device and driven by Web API control calls.
Both halves share a single OAuth refresh token managed by ``auth_spotify``.

Premium is required for playback. Search / browse work without; the
LibrespotBackend surfaces a clear error toast when a non-Premium account
tries to play a track. Lyrics aren't in Spotify's public API at all
(licensing) so we fall back to LRClib via the shared lyrics_provider.

Radio uses Spotify's track-station URIs. The recommendations endpoint
was deprecated in late 2024; ``spotify:station:track:<id>`` resolves
server-side to a playlist that behaves the same as YT Music's radio.
"""
from __future__ import annotations

import time
from typing import Callable

try:
    import spotipy
    HAVE_SPOTIPY = True
except ImportError:
    spotipy = None
    HAVE_SPOTIPY = False

from .. import cache
from ..auth_spotify import SpotifyTokens
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
)


SOURCE_SLUG = "spotify"

# Liked-Songs sentinel — mirrors YT Music's "LM" so the library view can
# special-case it without leaking source-specific strings into the UI.
LIKED_SONGS_ID = "LIKED"

# Spotify's Feb 2026 policy change caps search results to 10 per request
# for apps in Development Mode. Apps in Extended Quota Mode (granted only
# after an app review at developer.spotify.com) get the full 1-50 range.
# Anything >10 against a Dev Mode app returns 400 "Invalid limit". Cap on
# the source so requests don't fail before they hit the wire.
DEV_MODE_SEARCH_CAP = 10


def _ms_to_seconds(ms) -> int:
    try:
        return int(int(ms or 0) / 1000)
    except (TypeError, ValueError):
        return 0


def _seconds_to_hms(secs: int) -> str:
    if secs <= 0:
        return ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _join_artists(items) -> str:
    if not items:
        return ""
    names: list[str] = []
    for a in items:
        if isinstance(a, dict):
            name = a.get("name", "") or ""
            if name:
                names.append(name)
    return ", ".join(names)


def _largest_image(images) -> str:
    """Spotify returns images sorted largest-first. Cards/list rows use
    the moderate-sized thumb; album art view wants the biggest. Return
    the largest available — UI downsamples for display."""
    if not images or not isinstance(images, list):
        return ""
    first = images[0]
    if isinstance(first, dict):
        return first.get("url", "") or ""
    return ""


def _track_id_from_uri(uri: str) -> str:
    """``spotify:track:abc`` → ``abc``. Accepts bare ids unchanged."""
    if not uri:
        return ""
    if uri.startswith("spotify:track:"):
        return uri.split(":", 2)[2]
    return uri


def _to_track(item: dict | None) -> Track | None:
    if not item:
        return None
    # Playlist item wrappers carry the actual track under "track".
    if "track" in item and isinstance(item["track"], dict):
        item = item["track"]
    if item.get("type") and item.get("type") not in ("track", "episode"):
        return None
    tid = item.get("id") or ""
    if not tid:
        # Local tracks added to a user's playlist have no id; skip them.
        return None
    album = item.get("album") or {}
    secs = _ms_to_seconds(item.get("duration_ms"))
    return Track(
        video_id=tid,
        title=item.get("name", "") or "",
        artists=_join_artists(item.get("artists")),
        album=album.get("name", "") or "" if isinstance(album, dict) else "",
        duration=_seconds_to_hms(secs),
        duration_seconds=secs,
        thumbnail=_largest_image(album.get("images") if isinstance(album, dict) else None),
        source=SOURCE_SLUG,
        extras={"uri": item.get("uri", f"spotify:track:{tid}")},
    )


# ---------- the source ----------


class SpotifySource(MusicSource):
    slug = SOURCE_SLUG
    name = "spotify"
    icon = "spotify"
    needs_auth = True
    backend_slug = "librespot"
    short_tag = "SP"
    # NOTE on capability scope: Spotify's Feb 2026 policy change locked
    # /browse/* (featured-playlists, new-releases, categories) and the
    # artist top-tracks/related-artists endpoints to 403 Forbidden for
    # apps in Development Mode. tide ships its app in Dev Mode, so
    # `home`, `radio`, and the richer half of `artists` are unavailable
    # in practice. Extended Quota Mode (which needs a Spotify app
    # review) would unlock them; until/unless we land that, the source
    # advertises only what actually works. The `get_*` methods for the
    # missing features stay defined but return empty so the UI's
    # capability-aware fallbacks render a clean empty state instead of
    # toasting an error.
    capabilities = frozenset({
        "library", "albums", "artists", "lyrics", "rating",
    })

    # Spotify track URIs don't expire — the catalog id IS the stream
    # handle. We still set a TTL so the cache is consistent with other
    # sources, but anything pulled within a session is reused instantly.
    STREAM_TTL_SECONDS = 365 * 24 * 3600

    def __init__(
        self,
        tokens: SpotifyTokens,
        token_provider: Callable[[], SpotifyTokens] | None = None,
        on_token_refresh: Callable[[SpotifyTokens], None] | None = None,
    ) -> None:
        """``tokens`` is the loaded SpotifyTokens. ``token_provider`` is
        called before each Web API request to get fresh tokens (it does
        the refresh-if-expired dance). ``on_token_refresh`` lets the
        owner persist a rotated refresh token mid-session.
        """
        self._tokens = tokens
        self._token_provider = token_provider or (lambda: self._tokens)
        self._on_refresh = on_token_refresh
        self._me: dict | None = None
        self._me_fetched_at: float = 0.0

    # ---------- auth surface ----------

    def is_authenticated(self) -> bool:
        from .. import auth_spotify
        if auth_spotify.auth_is_dead():
            return False
        return bool(self._tokens and self._tokens.refresh_token)

    def status_text(self) -> str:
        # Runs on the GUI thread on every panel repaint — it must never do
        # network I/O. Read the profile cache passively; probe() fills it
        # from the panel's background prober.
        from .. import auth_spotify
        if auth_spotify.auth_is_dead():
            return "session expired — sign in to fix"
        if not self.is_authenticated():
            return "sign in via [connect]"
        me = self._me
        if not me:
            return "signed in"
        product = me.get("product") or "free"
        label = me.get("display_name") or me.get("id") or "signed in"
        if product == "premium":
            return f"signed in as {label} · premium"
        return f"signed in as {label} · {product} (playback needs premium)"

    def probe(self) -> bool:
        """Fetch/refresh the /me profile so status_text() has something to
        show. Blocking network — background threads only (the source
        panel's async prober calls this, mirroring subsonic's ping)."""
        return self._me_cached() is not None

    def is_premium(self) -> bool:
        me = self._me_cached()
        return bool(me and me.get("product") == "premium")

    # ---------- spotipy client (always-fresh) ----------

    def _client(self):
        if not HAVE_SPOTIPY:
            raise NotSupportedError(
                "spotify support requires the `spotipy` package - "
                "pacman -S python-spotipy"
            )
        tokens = self._token_provider()
        if tokens is not self._tokens:
            self._tokens = tokens
            if self._on_refresh:
                try:
                    self._on_refresh(tokens)
                except Exception:
                    pass
        return spotipy.Spotify(auth=tokens.access_token, requests_timeout=15)

    def access_token(self) -> str:
        return self._token_provider().access_token

    def _me_cached(self, max_age: float = 300.0, failure_age: float = 60.0) -> dict | None:
        age = time.time() - self._me_fetched_at
        if self._me is not None and age < max_age:
            return self._me
        if self._me is None and self._me_fetched_at > 0.0 and age < failure_age:
            # Negative-cached: the last fetch failed moments ago. Without
            # this, every caller re-hit the API on each call while offline
            # (the old `if self._me and …` guard was always falsy on None).
            return None
        try:
            self._me = self._client().me() or None
        except Exception:
            self._me = None
        self._me_fetched_at = time.time()
        return self._me

    # ---------- required ----------

    def search_songs(self, query: str, limit: int = 20) -> list[Track]:
        if not query.strip():
            return []
        try:
            res = self._client().search(q=query, type="track", limit=min(limit, DEV_MODE_SEARCH_CAP)) or {}
        except Exception:
            return []
        items = (res.get("tracks", {}) or {}).get("items", []) or []
        out: list[Track] = []
        for item in items:
            tr = _to_track(item)
            if tr:
                out.append(tr)
        return out

    def resolve_stream(self, track: Track) -> StreamRef:
        # Spotify "stream resolution" is just the URI — the LibrespotBackend
        # handles the actual audio. We still populate the per-source cache
        # so calls to cache.get_stream_url stay consistent across sources.
        uri = (track.extras or {}).get("uri") or f"spotify:track:{track.video_id}"
        cache.put_stream_url(SOURCE_SLUG, track.video_id, uri, ttl_seconds=self.STREAM_TTL_SECONDS)
        return StreamRef(backend="librespot", payload=uri)

    # ---------- search filters ----------

    def search_albums(self, query: str, limit: int = 20) -> list[AlbumEntry]:
        if not query.strip():
            return []
        try:
            res = self._client().search(q=query, type="album", limit=min(limit, DEV_MODE_SEARCH_CAP)) or {}
        except Exception:
            return []
        items = (res.get("albums", {}) or {}).get("items", []) or []
        out: list[AlbumEntry] = []
        for item in items:
            aid = item.get("id") or ""
            if not aid:
                continue
            out.append(AlbumEntry(
                browse_id=aid,
                title=item.get("name", "") or "",
                artists=_join_artists(item.get("artists")),
                year=(item.get("release_date") or "")[:4],
                thumbnail=_largest_image(item.get("images")),
                playlist_id="",
            ))
        return out

    def search_artists(self, query: str, limit: int = 20) -> list[ArtistEntry]:
        if not query.strip():
            return []
        try:
            res = self._client().search(q=query, type="artist", limit=min(limit, DEV_MODE_SEARCH_CAP)) or {}
        except Exception:
            return []
        items = (res.get("artists", {}) or {}).get("items", []) or []
        out: list[ArtistEntry] = []
        for item in items:
            cid = item.get("id") or ""
            if not cid:
                continue
            followers = (item.get("followers") or {}).get("total")
            out.append(ArtistEntry(
                channel_id=cid,
                name=item.get("name", "") or "",
                thumbnail=_largest_image(item.get("images")),
                subscribers=str(followers) if followers else "",
            ))
        return out

    # ---------- library + playlists ----------

    def get_library_playlists(self, limit: int = 100) -> list[PlaylistEntry]:
        out: list[PlaylistEntry] = [
            PlaylistEntry(
                playlist_id=LIKED_SONGS_ID,
                title="Liked Songs",
                description="your saved tracks",
                thumbnail="",
            ),
        ]
        # Page through in dev-cap chunks — a single capped call topped out
        # at 10 playlists, silently hiding the rest of the library.
        try:
            client = self._client()
            offset = 0
            page = DEV_MODE_SEARCH_CAP   # >10 → 400 for Dev Mode apps
            while len(out) - 1 < limit:  # -1: the Liked Songs pseudo-entry
                res = client.current_user_playlists(limit=page, offset=offset) or {}
                items = res.get("items", []) or []
                if not items:
                    break
                for item in items:
                    pid = item.get("id") or ""
                    if not pid:
                        continue
                    out.append(PlaylistEntry(
                        playlist_id=pid,
                        title=item.get("name", "") or "",
                        description=(item.get("description") or "").strip(),
                        thumbnail=_largest_image(item.get("images")),
                    ))
                if len(items) < page or not res.get("next"):
                    break
                offset += len(items)
        except Exception:
            return out   # partial result beats none mid-pagination
        return out

    def get_playlist(self, playlist_id: str, limit: int = 500) -> PlaylistDetail:
        client = self._client()
        tracks: list[Track] = []
        if playlist_id == LIKED_SONGS_ID:
            offset = 0
            page = min(50, limit)
            while len(tracks) < limit:
                try:
                    res = client.current_user_saved_tracks(limit=page, offset=offset) or {}
                except Exception:
                    break
                items = res.get("items", []) or []
                if not items:
                    break
                for item in items:
                    tr = _to_track(item.get("track"))
                    if tr:
                        tracks.append(tr)
                if len(items) < page or not res.get("next"):
                    break
                offset += page
            return PlaylistDetail(
                playlist_id=LIKED_SONGS_ID,
                title="Liked Songs",
                description="your saved tracks",
                track_count=len(tracks),
                thumbnail="",
                tracks=tracks,
            )

        try:
            meta = client.playlist(playlist_id, fields="name,description,images,owner(display_name),tracks(total)") or {}
        except Exception:
            return PlaylistDetail(playlist_id=playlist_id, title="", tracks=[])
        total = int(((meta.get("tracks") or {}).get("total")) or 0)
        offset = 0
        page = min(100, limit)
        while len(tracks) < limit:
            try:
                res = client.playlist_items(
                    playlist_id,
                    limit=page,
                    offset=offset,
                    additional_types=("track",),
                ) or {}
            except Exception:
                break
            items = res.get("items", []) or []
            if not items:
                break
            for item in items:
                tr = _to_track(item.get("track"))
                if tr:
                    tracks.append(tr)
            if len(items) < page or not res.get("next"):
                break
            offset += page
        return PlaylistDetail(
            playlist_id=playlist_id,
            title=meta.get("name", "") or "",
            description=(meta.get("description") or "").strip(),
            track_count=total or len(tracks),
            thumbnail=_largest_image(meta.get("images")),
            tracks=tracks,
        )

    # ---------- album / artist detail ----------

    def get_album(self, browse_id: str) -> AlbumDetail | None:
        if not browse_id:
            return None
        try:
            raw = self._client().album(browse_id) or {}
        except Exception:
            return None
        album_thumb = _largest_image(raw.get("images"))
        album_artists = _join_artists(raw.get("artists"))
        tracks: list[Track] = []
        for item in (raw.get("tracks", {}) or {}).get("items", []) or []:
            tr = _to_track(item)
            if tr is None:
                continue
            # Spotify's album.tracks omits the album thumb on each track —
            # fill it from the album-level art so list rows render right.
            if not tr.album:
                tr.album = raw.get("name", "") or ""
            if not tr.thumbnail:
                tr.thumbnail = album_thumb
            if not tr.artists:
                tr.artists = album_artists
            tracks.append(tr)
        total_ms = sum(int(((item or {}).get("duration_ms") or 0))
                       for item in (raw.get("tracks", {}) or {}).get("items", []) or [])
        return AlbumDetail(
            browse_id=browse_id,
            title=raw.get("name", "") or "",
            artists=album_artists,
            year=(raw.get("release_date") or "")[:4],
            duration=_seconds_to_hms(total_ms // 1000),
            track_count=int(raw.get("total_tracks") or len(tracks)),
            thumbnail=album_thumb,
            description="",
            tracks=tracks,
        )

    def get_artist(self, channel_id: str) -> ArtistDetail | None:
        if not channel_id:
            return None
        client = self._client()
        try:
            raw = client.artist(channel_id) or {}
        except Exception:
            return None
        try:
            top = client.artist_top_tracks(channel_id, country="from_token") or {}
        except Exception:
            top = {}
        top_songs: list[Track] = []
        for t in top.get("tracks", []) or []:
            tr = _to_track(t)
            if tr:
                top_songs.append(tr)

        def _albums_filtered(include_groups: str, limit: int = 30) -> list[AlbumEntry]:
            try:
                res = client.artist_albums(channel_id, album_type=include_groups, limit=limit) or {}
            except Exception:
                return []
            out: list[AlbumEntry] = []
            seen: set[str] = set()
            for a in res.get("items", []) or []:
                key = (a.get("name", "") or "").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                aid = a.get("id") or ""
                if not aid:
                    continue
                out.append(AlbumEntry(
                    browse_id=aid,
                    title=a.get("name", "") or "",
                    artists=_join_artists(a.get("artists")),
                    year=(a.get("release_date") or "")[:4],
                    thumbnail=_largest_image(a.get("images")),
                    playlist_id="",
                ))
            return out

        albums = _albums_filtered("album")
        singles = _albums_filtered("single")

        try:
            rel = client.artist_related_artists(channel_id) or {}
        except Exception:
            rel = {}
        related: list[ArtistEntry] = []
        for r in rel.get("artists", []) or []:
            rid = r.get("id") or ""
            if not rid:
                continue
            followers = (r.get("followers") or {}).get("total")
            related.append(ArtistEntry(
                channel_id=rid,
                name=r.get("name", "") or "",
                thumbnail=_largest_image(r.get("images")),
                subscribers=str(followers) if followers else "",
            ))

        followers = (raw.get("followers") or {}).get("total") or 0
        return ArtistDetail(
            channel_id=channel_id,
            name=raw.get("name", "") or "",
            description="",
            subscribers=str(followers) if followers else "",
            monthly_listeners="",
            thumbnail=_largest_image(raw.get("images")),
            top_songs=top_songs,
            albums=albums,
            singles=singles,
            related=related,
        )

    # ---------- home shelves ----------

    def get_home(self, limit: int = 5) -> list[Shelf]:
        client = self._client()
        shelves: list[Shelf] = []

        # Shelf 1: Featured Playlists. Spotify's editorial home.
        try:
            res = client.featured_playlists(limit=10) or {}
            items = (res.get("playlists", {}) or {}).get("items", []) or []
            sh_items: list[ShelfItem] = []
            for it in items:
                pid = it.get("id") or ""
                if not pid:
                    continue
                sh_items.append(ShelfItem(
                    kind="playlist",
                    title=it.get("name", "") or "",
                    subtitle=(it.get("description") or "").strip(),
                    thumbnail=_largest_image(it.get("images")),
                    playlist=PlaylistEntry(
                        playlist_id=pid,
                        title=it.get("name", "") or "",
                        description=(it.get("description") or "").strip(),
                        thumbnail=_largest_image(it.get("images")),
                    ),
                ))
            if sh_items:
                shelves.append(Shelf(title=res.get("message") or "Featured", items=sh_items))
        except Exception:
            pass

        # Shelf 2: New Releases. Albums Spotify is pushing this week.
        try:
            res = client.new_releases(limit=10) or {}
            items = (res.get("albums", {}) or {}).get("items", []) or []
            sh_items = []
            for it in items:
                aid = it.get("id") or ""
                if not aid:
                    continue
                title = it.get("name", "") or ""
                sh_items.append(ShelfItem(
                    kind="album",
                    title=title,
                    subtitle=_join_artists(it.get("artists")),
                    thumbnail=_largest_image(it.get("images")),
                    album=AlbumEntry(
                        browse_id=aid,
                        title=title,
                        artists=_join_artists(it.get("artists")),
                        year=(it.get("release_date") or "")[:4],
                        thumbnail=_largest_image(it.get("images")),
                        playlist_id="",
                    ),
                ))
            if sh_items:
                shelves.append(Shelf(title="New Releases", items=sh_items))
        except Exception:
            pass

        # Shelf 3+: Browse categories — pick the first few interesting ones.
        try:
            cat_res = client.categories(limit=6) or {}
            cats = (cat_res.get("categories", {}) or {}).get("items", []) or []
            slots_left = max(0, limit - len(shelves))
            for cat in cats[:slots_left]:
                cid = cat.get("id") or ""
                if not cid:
                    continue
                try:
                    cp = client.category_playlists(cid, limit=8) or {}
                except Exception:
                    continue
                items = (cp.get("playlists", {}) or {}).get("items", []) or []
                sh_items = []
                for it in items:
                    pid = it.get("id") or ""
                    if not pid:
                        continue
                    sh_items.append(ShelfItem(
                        kind="playlist",
                        title=it.get("name", "") or "",
                        subtitle=(it.get("description") or "").strip(),
                        thumbnail=_largest_image(it.get("images")),
                        playlist=PlaylistEntry(
                            playlist_id=pid,
                            title=it.get("name", "") or "",
                            description=(it.get("description") or "").strip(),
                            thumbnail=_largest_image(it.get("images")),
                        ),
                    ))
                if sh_items:
                    shelves.append(Shelf(title=cat.get("name") or "Browse", items=sh_items))
        except Exception:
            pass

        return shelves[:limit] if limit > 0 else shelves

    # ---------- radio ----------

    def get_radio(self, video_id: str, exclude: set[str] | None = None,
                  depth: int = 0) -> list[Track]:
        """Resolve a track-station URI. Spotify deprecated its
        `/v1/recommendations` endpoint in late 2024, so we use the
        track-station path which Spotify generates server-side and
        treats as a real playlist.
        """
        if not video_id:
            return []
        client = self._client()
        track_id = _track_id_from_uri(video_id)
        excluded = set(exclude or ())
        excluded.add(track_id)

        # Station-URI path: Spotify exposes radio for any catalog item as
        # spotify:station:track:<id>. The Web API doesn't have a clean
        # endpoint that resolves stations directly, but the same id with
        # the "radio" playlist convention works through artist_top + related.
        # If the playlist lookup ever fails we fall through to a
        # related-artists fallback so the queue doesn't dead-end.
        out: list[Track] = []
        try:
            top = client.artist_top_tracks(self._primary_artist_id(track_id), country="from_token") or {}
            for t in top.get("tracks", []) or []:
                tr = _to_track(t)
                if tr and tr.video_id not in excluded:
                    excluded.add(tr.video_id)
                    out.append(tr)
        except Exception:
            pass
        if len(out) >= 8:
            return out

        # Fallback: related-artist top tracks. Slightly less personal but
        # honest about what's happening.
        try:
            rel = client.artist_related_artists(self._primary_artist_id(track_id)) or {}
            for r in rel.get("artists", []) or []:
                rid = r.get("id")
                if not rid:
                    continue
                try:
                    tops = client.artist_top_tracks(rid, country="from_token") or {}
                except Exception:
                    continue
                for t in tops.get("tracks", []) or []:
                    tr = _to_track(t)
                    if not tr or tr.video_id in excluded:
                        continue
                    excluded.add(tr.video_id)
                    out.append(tr)
                    if len(out) >= 20:
                        return out
        except Exception:
            pass
        return out

    def _primary_artist_id(self, track_id: str) -> str:
        try:
            tr = self._client().track(track_id) or {}
        except Exception:
            return ""
        artists = tr.get("artists") or []
        if artists and isinstance(artists[0], dict):
            return artists[0].get("id") or ""
        return ""

    # ---------- like / save ----------

    def rate_song(self, video_id: str, liked: bool) -> None:
        if not video_id:
            return
        tid = _track_id_from_uri(video_id)
        try:
            client = self._client()
            if liked:
                client.current_user_saved_tracks_add([tid])
            else:
                client.current_user_saved_tracks_delete([tid])
        except Exception:
            pass

    def is_liked(self, video_id: str) -> bool | None:
        if not video_id:
            return None
        tid = _track_id_from_uri(video_id)
        try:
            res = self._client().current_user_saved_tracks_contains([tid]) or []
        except Exception:
            return None
        return bool(res and res[0])

    # ---------- lyrics ----------

    def get_lyrics_for(self, video_id: str) -> str | None:
        # Spotify's public Web API doesn't expose lyrics (Musixmatch
        # licensing) — fall back to LRClib via the per-track helper.
        return None

    def get_lyrics_for_track(self, track: Track):
        from ..lyrics_provider import fetch_lrclib
        return fetch_lrclib(
            title=track.title or "",
            artist=track.artists or "",
            album=track.album or "",
            duration_seconds=int(track.duration_seconds or 0),
        )
