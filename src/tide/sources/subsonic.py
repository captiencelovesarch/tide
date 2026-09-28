"""Subsonic / Navidrome source.

Subsonic is the de-facto self-hosted music server protocol. Navidrome,
Airsonic, Funkwhale, gonic, and a few others all implement it; once a
user has any of these running and pointed at their music library, tide
talks to them with the same REST surface and streams the actual audio
directly from the user's server.

Auth — two flavors, both supported here:
  - **salt+token** (default): the client picks a salt, sends
    ``t=md5(password + salt)`` along with ``s=salt``. Server hashes
    its own copy of the password the same way and compares. Safe to
    use over plain HTTP because the password never traverses the wire.
  - **plain** (HTTPS only): passes the password via ``p=``. Required
    by some Navidrome installs that store passwords hashed at rest.

Streaming — Subsonic's ``stream`` endpoint returns the raw audio bytes
behind a query-string-authed URL, so ``resolve_stream`` just hands mpv
the URL and the existing playback path takes over.

Capabilities: ``{library, albums, artists, home, radio, rating}``. No
lyrics from the server itself; LRClib fallback covers it.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable

from .. import cache
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


SOURCE_SLUG = "subsonic"

# Subsonic API version we advertise. 1.16 is broadly supported across
# Navidrome / Subsonic / Airsonic and unlocks search3 + getArtists.
API_VERSION = "1.16.1"
CLIENT_NAME = "tide"
TIMEOUT_SECONDS = 10.0
# Cap the response body a (possibly plain-HTTP, least-trusted) Subsonic
# server can push at us, so a malicious/MITM'd server can't exhaust memory
# with a multi-GB body. Generous enough for any real library JSON page.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _seconds_to_hms(secs: int) -> str:
    if secs <= 0:
        return ""
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@dataclass
class SubsonicConfig:
    url: str = ""
    user: str = ""
    password: str = ""
    auth_style: str = "salt"   # "salt" | "plain"

    def is_complete(self) -> bool:
        return bool(self.url and self.user and self.password)


def _normalize_url(raw: str) -> str:
    """Coerce a user-typed URL into something we can append /rest/ to.

    Accept ``http://host``, ``host:port``, bare ``host``. Strip trailing
    slashes so concatenation is predictable. Default to https when the
    user didn't pick a scheme — modern Navidrome installs run behind a
    reverse proxy and HTTPS is the better safer default.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    return raw


class SubsonicSource(MusicSource):
    slug = SOURCE_SLUG
    name = "subsonic"
    icon = "subsonic"
    needs_auth = True
    backend_slug = "mpv"
    short_tag = "SS"
    capabilities = frozenset({
        "library", "albums", "artists", "home", "radio", "rating",
    })

    def __init__(self, config: SubsonicConfig) -> None:
        self._cfg = config
        self._url = _normalize_url(config.url)
        # Older tide versions persisted credentialed stream URLs to the
        # shared disk cache (auth token in the query string, world-readable,
        # surviving sign-out). We never write them anymore — see
        # resolve_stream — so purge whatever an earlier version left behind.
        try:
            cache.clear_source(SOURCE_SLUG)
        except Exception:
            pass
        # Quick probe state — populated by the first call to a method
        # that does any IO. status_text() consumes it to render the
        # "signed in as X" / "can't reach server" line in the source
        # panel without doing extra network round-trips per repaint.
        self._reachable: bool | None = None
        self._reachable_error: str = ""

    def set_config(self, config: SubsonicConfig) -> None:
        """Swap in new credentials at runtime — used by the source-panel
        gear dialog when the user re-enters their server URL or password.
        Resets the reachability probe so the next is_authenticated() call
        re-tests against the new server."""
        self._cfg = config
        self._url = _normalize_url(config.url)
        self._reachable = None
        self._reachable_error = ""
        # Any cached URLs were minted with the old server/credentials —
        # invalid at best, a leaked credential at worst (sign_out routes
        # through here with an empty config).
        try:
            cache.clear_source(SOURCE_SLUG)
        except Exception:
            pass

    def sign_out(self) -> None:
        """Wipe credentials in-memory. The settings file still holds them
        until the gear dialog persists the cleared values."""
        self.set_config(SubsonicConfig())

    # ---------- auth surface ----------

    def is_authenticated(self) -> bool:
        # Cheap. Returns True iff the config has the three required
        # fields and the most recent probe (if any) didn't fail. We
        # deliberately do NOT do a network round-trip here — the source
        # panel calls this on construction, and a blocking probe per row
        # at panel-mount would freeze the GUI for the connect timeout
        # (10s) on an unreachable server. probe() runs the actual ping
        # off-thread and updates _reachable for subsequent calls.
        if not self._cfg.is_complete():
            return False
        return self._reachable is not False

    def probe(self) -> bool:
        """Hit the server's `ping.view` endpoint synchronously and cache
        the result. Safe to call from a background thread; the GUI uses
        ``is_authenticated()`` + ``status_text()`` after the probe writes
        ``_reachable``. Returns True iff the server replied ok.
        """
        if not self._cfg.is_complete():
            self._reachable = False
            return False
        try:
            self._call("ping")
            self._reachable = True
            self._reachable_error = ""
        except Exception as exc:
            self._reachable = False
            self._reachable_error = str(exc)
        return bool(self._reachable)

    def status_text(self) -> str:
        if not self._cfg.is_complete():
            return "needs server url + credentials"
        if self._reachable is False:
            return f"can't reach {self._cfg.url} — check settings"
        if self._reachable is None:
            # Config is set, no probe yet — be honest about it instead of
            # claiming "signed in as ..." before we've talked to the server.
            return f"{self._cfg.user} @ {self._cfg.url} · checking…"
        return f"signed in as {self._cfg.user}"

    # ---------- REST helper ----------

    def _auth_params(self) -> dict[str, str]:
        params: dict[str, str] = {
            "u": self._cfg.user,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        # Plain auth (?p=<cleartext>) is only safe over TLS — otherwise the
        # account password crosses the wire (and lands in server/proxy access
        # logs) in the clear. The "plain (https only)" label was never
        # enforced, so refuse to honor it over http: fall back to salt+token,
        # which authenticates against any Subsonic server without ever
        # transmitting the cleartext password. Over https, plain is respected.
        use_plain = self._cfg.auth_style == "plain" and self._url.lower().startswith("https://")
        if use_plain:
            params["p"] = self._cfg.password
        else:
            salt = secrets.token_hex(8)
            token = hashlib.md5(
                (self._cfg.password + salt).encode("utf-8")
            ).hexdigest()
            params["t"] = token
            params["s"] = salt
        return params

    def _call(self, endpoint: str, params: dict | None = None) -> dict:
        if not self._url:
            raise RuntimeError("subsonic: no server url configured")
        merged = {**self._auth_params(), **({k: v for k, v in (params or {}).items() if v is not None})}
        url = f"{self._url}/rest/{endpoint}.view?" + urllib.parse.urlencode(merged)
        req = urllib.request.Request(url, headers={"User-Agent": "tide/1.2.1"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except Exception as exc:
            # Transport failure — the server is gone as far as the UI cares.
            # Record it so status_text()/is_authenticated() stop claiming
            # "signed in as X"; the next successful call heals the state.
            self._reachable = False
            self._reachable_error = str(exc)
            raise RuntimeError(f"subsonic: can't reach server: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._reachable = False
            self._reachable_error = f"non-json response from {endpoint}"
            raise RuntimeError(f"subsonic: non-json response from {endpoint}: {exc}")
        # Well-formed JSON of the wrong *shape* (a list/number/string at the
        # top level, or a non-dict envelope) would otherwise raise an
        # AttributeError out of the worker thread on the .get() calls below.
        # A malicious/MITM'd server can send that trivially, so treat any
        # unexpected shape as a protocol error rather than crashing.
        if not isinstance(data, dict):
            raise RuntimeError(f"subsonic: malformed response from {endpoint}")
        envelope = data.get("subsonic-response")
        if not isinstance(envelope, dict):
            raise RuntimeError(f"subsonic: malformed response from {endpoint}")
        status = envelope.get("status")
        if status != "ok":
            err = envelope.get("error") or {}
            msg = err.get("message") or f"endpoint {endpoint} returned {status!r}"
            # Codes 40/41 are credential rejections — those poison the whole
            # source. Anything else (unsupported endpoint, bad id, …) is a
            # per-request problem and must NOT mark the server unhealthy:
            # get_artist probes optional endpoints that many servers lack.
            if str(err.get("code")) in ("40", "41"):
                self._reachable = False
                self._reachable_error = msg
            raise RuntimeError(f"subsonic: {msg}")
        self._reachable = True
        self._reachable_error = ""
        return envelope

    def _stream_url(self, song_id: str) -> str:
        params = {**self._auth_params(), "id": song_id, "format": "raw"}
        return f"{self._url}/rest/stream.view?" + urllib.parse.urlencode(params)

    def _cover_url(self, art_id: str, size: int = 320) -> str:
        if not art_id:
            return ""
        params = {**self._auth_params(), "id": art_id, "size": size}
        return f"{self._url}/rest/getCoverArt.view?" + urllib.parse.urlencode(params)

    # ---------- conversion helpers ----------

    def _to_track(self, item: dict | None) -> Track | None:
        # Reject non-dict input up front. Subsonic sometimes returns a bare
        # object where a list is expected (or a malicious server sends the
        # wrong shape entirely); the callers iterate `.get("song")` blindly,
        # and iterating a dict/str yields keys/chars. Guarding here means
        # every one of those loops degrades to "skip" instead of raising an
        # AttributeError out of the worker thread.
        if not isinstance(item, dict):
            return None
        tid = item.get("id") or ""
        if not tid:
            return None
        secs = safe_int(item.get("duration"))
        return Track(
            video_id=tid,
            title=item.get("title", "") or "",
            artists=item.get("artist", "") or "",
            album=item.get("album", "") or "",
            duration=_seconds_to_hms(secs),
            duration_seconds=secs,
            thumbnail=self._cover_url(item.get("coverArt", "")),
            source=SOURCE_SLUG,
            extras={"raw": item},
        )

    def _to_album_entry(self, item: dict) -> AlbumEntry | None:
        if not isinstance(item, dict):
            return None
        aid = item.get("id") or ""
        if not aid:
            return None
        year = str(item.get("year") or "")
        return AlbumEntry(
            browse_id=aid,
            title=item.get("name") or item.get("title", "") or "",
            artists=item.get("artist", "") or "",
            year=year,
            thumbnail=self._cover_url(item.get("coverArt") or aid),
            playlist_id="",
        )

    def _to_artist_entry(self, item: dict) -> ArtistEntry | None:
        if not isinstance(item, dict):
            return None
        cid = item.get("id") or ""
        if not cid:
            return None
        return ArtistEntry(
            channel_id=cid,
            name=item.get("name", "") or "",
            thumbnail=self._cover_url(item.get("coverArt") or cid),
            subscribers=str(item.get("albumCount") or "") + " albums" if item.get("albumCount") else "",
        )

    # ---------- required ----------

    def search_songs(self, query: str, limit: int = 20) -> list[Track]:
        if not query.strip():
            return []
        # Errors propagate: the search worker turns them into a visible
        # "search failed" instead of a silent, lying "no results". _call
        # has already flipped the health state for status_text().
        res = self._call("search3", {"query": query, "songCount": limit, "albumCount": 0, "artistCount": 0})
        items = ((res.get("searchResult3") or {}).get("song")) or []
        out: list[Track] = []
        for item in items:
            tr = self._to_track(item)
            if tr:
                out.append(tr)
        return out

    def resolve_stream(self, track: Track) -> StreamRef:
        # Built fresh every time, never cached: the URL is pure local
        # computation (md5 + urlencode, no network), and it embeds the auth
        # token — in "plain" mode the actual password. Persisting it to the
        # shared stream cache wrote credentials to disk where they survived
        # sign-out. Nothing is gained by caching a zero-cost computation.
        return StreamRef(backend="mpv", payload=self._stream_url(track.video_id))

    # ---------- search filters ----------

    def search_albums(self, query: str, limit: int = 20) -> list[AlbumEntry]:
        if not query.strip():
            return []
        res = self._call("search3", {"query": query, "albumCount": limit, "songCount": 0, "artistCount": 0})
        items = ((res.get("searchResult3") or {}).get("album")) or []
        out: list[AlbumEntry] = []
        for item in items:
            ent = self._to_album_entry(item)
            if ent:
                out.append(ent)
        return out

    def search_artists(self, query: str, limit: int = 20) -> list[ArtistEntry]:
        if not query.strip():
            return []
        res = self._call("search3", {"query": query, "artistCount": limit, "songCount": 0, "albumCount": 0})
        items = ((res.get("searchResult3") or {}).get("artist")) or []
        out: list[ArtistEntry] = []
        for item in items:
            ent = self._to_artist_entry(item)
            if ent:
                out.append(ent)
        return out

    # ---------- library + playlists ----------

    def get_library_playlists(self, limit: int = 100) -> list[PlaylistEntry]:
        # Propagates: the library worker shows "library load failed" rather
        # than rendering an empty library over a dead connection.
        res = self._call("getPlaylists")
        items = ((res.get("playlists") or {}).get("playlist")) or []
        out: list[PlaylistEntry] = []
        for item in items[:limit]:
            pid = item.get("id") or ""
            if not pid:
                continue
            out.append(PlaylistEntry(
                playlist_id=pid,
                title=item.get("name", "") or "",
                description=(item.get("comment") or "").strip(),
                thumbnail=self._cover_url(item.get("coverArt", "")),
            ))
        return out

    def get_playlist(self, playlist_id: str, limit: int = 500) -> PlaylistDetail:
        res = self._call("getPlaylist", {"id": playlist_id})
        pl = res.get("playlist") or {}
        tracks: list[Track] = []
        for item in (pl.get("entry") or [])[:limit]:
            tr = self._to_track(item)
            if tr:
                tracks.append(tr)
        return PlaylistDetail(
            playlist_id=playlist_id,
            title=pl.get("name", "") or "",
            description=(pl.get("comment") or "").strip(),
            track_count=int(pl.get("songCount") or len(tracks)),
            thumbnail=self._cover_url(pl.get("coverArt", "")),
            tracks=tracks,
        )

    # ---------- album / artist detail ----------

    def get_album(self, browse_id: str) -> AlbumDetail | None:
        if not browse_id:
            return None
        try:
            res = self._call("getAlbum", {"id": browse_id})
        except Exception:
            return None
        album = res.get("album") or {}
        cover = self._cover_url(album.get("coverArt") or browse_id)
        tracks: list[Track] = []
        for item in album.get("song", []) or []:
            tr = self._to_track(item)
            if tr is None:
                continue
            if not tr.album:
                tr.album = album.get("name", "") or ""
            if not tr.thumbnail:
                tr.thumbnail = cover
            tracks.append(tr)
        secs = safe_int(album.get("duration"))
        return AlbumDetail(
            browse_id=browse_id,
            title=album.get("name", "") or "",
            artists=album.get("artist", "") or "",
            year=str(album.get("year") or ""),
            duration=_seconds_to_hms(secs),
            track_count=int(album.get("songCount") or len(tracks)),
            thumbnail=cover,
            description="",
            tracks=tracks,
        )

    def get_artist(self, channel_id: str) -> ArtistDetail | None:
        if not channel_id:
            return None
        try:
            res = self._call("getArtist", {"id": channel_id})
        except Exception:
            return None
        artist = res.get("artist") or {}
        albums: list[AlbumEntry] = []
        singles: list[AlbumEntry] = []
        for a in artist.get("album", []) or []:
            ent = self._to_album_entry(a)
            if ent is None:
                continue
            # Heuristic: 1-2 track releases get filed under "singles",
            # everything else under "albums". Matches the YT Music shape.
            if int(a.get("songCount") or 0) <= 2:
                singles.append(ent)
            else:
                albums.append(ent)
        # Top songs — Subsonic exposes "getTopSongs" which some servers
        # implement, some don't. Best-effort, then fall back to empty.
        top_songs: list[Track] = []
        try:
            top = self._call("getTopSongs", {"artist": artist.get("name", "")})
            for item in ((top.get("topSongs") or {}).get("song") or [])[:10]:
                tr = self._to_track(item)
                if tr:
                    top_songs.append(tr)
        except Exception:
            pass
        # Related artists via getSimilarArtists2 when available.
        related: list[ArtistEntry] = []
        try:
            sim = self._call("getArtistInfo2", {"id": channel_id})
            for item in ((sim.get("artistInfo2") or {}).get("similarArtist") or [])[:10]:
                ent = self._to_artist_entry(item)
                if ent:
                    related.append(ent)
        except Exception:
            pass
        return ArtistDetail(
            channel_id=channel_id,
            name=artist.get("name", "") or "",
            description="",
            subscribers=f"{artist.get('albumCount') or 0} albums",
            monthly_listeners="",
            thumbnail=self._cover_url(artist.get("coverArt") or channel_id),
            top_songs=top_songs,
            albums=albums,
            singles=singles,
            related=related,
        )

    # ---------- home shelves ----------

    def get_home(self, limit: int = 5) -> list[Shelf]:
        shelves: list[Shelf] = []

        def _albums_shelf(title: str, kind: str, size: int = 12) -> Shelf | None:
            try:
                res = self._call("getAlbumList2", {"type": kind, "size": size})
            except Exception:
                return None
            items = ((res.get("albumList2") or {}).get("album")) or []
            out: list[ShelfItem] = []
            for item in items:
                ent = self._to_album_entry(item)
                if ent is None:
                    continue
                out.append(ShelfItem(
                    kind="album", title=ent.title, subtitle=ent.artists,
                    thumbnail=ent.thumbnail, album=ent,
                ))
            if not out:
                return None
            return Shelf(title=title, items=out)

        # Each kind = Subsonic's getAlbumList2 type. "newest" = recently
        # added (i.e. just imported / ripped), "frequent" = play-count
        # ordered, "starred" = user-starred albums, "random" =
        # surprise-me. Together they give the "what's new + what I like"
        # picture that a home view wants.
        for title, kind in (
            ("recently added", "newest"),
            ("most played", "frequent"),
            ("starred", "starred"),
            ("random", "random"),
        ):
            shelf = _albums_shelf(title, kind)
            if shelf is not None:
                shelves.append(shelf)
                if len(shelves) >= limit:
                    break
        return shelves

    # ---------- radio ----------

    def get_radio(self, video_id: str, exclude: set[str] | None = None,
                  depth: int = 0) -> list[Track]:
        """getSimilarSongs2 returns "more like this" for a given song.
        Falls through to getRandomSongs if the server doesn't implement
        the similar endpoint — better an honest fallback than dead air."""
        if not video_id:
            return []
        excluded = set(exclude or ())
        excluded.add(video_id)
        out: list[Track] = []
        try:
            res = self._call("getSimilarSongs2", {"id": video_id, "count": 20})
            for item in ((res.get("similarSongs2") or {}).get("song") or []):
                tr = self._to_track(item)
                if tr and tr.video_id not in excluded:
                    excluded.add(tr.video_id)
                    out.append(tr)
        except Exception:
            pass
        if len(out) >= 8:
            return out
        try:
            res = self._call("getRandomSongs", {"size": 20})
            for item in ((res.get("randomSongs") or {}).get("song") or []):
                tr = self._to_track(item)
                if tr and tr.video_id not in excluded:
                    excluded.add(tr.video_id)
                    out.append(tr)
        except Exception:
            pass
        return out

    # ---------- like / star ----------

    def rate_song(self, video_id: str, liked: bool) -> None:
        if not video_id:
            return
        try:
            self._call("star" if liked else "unstar", {"id": video_id})
        except Exception:
            pass

    def is_liked(self, video_id: str) -> bool | None:
        if not video_id:
            return None
        try:
            res = self._call("getSong", {"id": video_id})
        except Exception:
            return None
        item = res.get("song") or {}
        return bool(item.get("starred"))

    # ---------- lyrics ----------

    def get_lyrics_for(self, video_id: str) -> str | None:
        # Subsonic exposes a getLyrics endpoint but most servers don't
        # populate it from the tag data Navidrome reads. Skip; tide's
        # LRClib fallback in get_lyrics_for_track will give the user a
        # synced result if one exists.
        return None

    def get_lyrics_for_track(self, track: Track):
        from ..lyrics_provider import fetch_lrclib
        return fetch_lrclib(
            title=track.title or "",
            artist=track.artists or "",
            album=track.album or "",
            duration_seconds=int(track.duration_seconds or 0),
        )
