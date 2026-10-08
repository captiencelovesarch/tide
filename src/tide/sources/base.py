"""Source abstraction layer.

A `MusicSource` produces `Track` records and resolves them to a `StreamRef`
that its declared playback backend understands. The queue is source-agnostic
— each Track carries the slug of the source that produced it, and the
playback router uses that to dispatch.

For v1.1 there was only one implicit source (YouTube Music) and tracks
flowed straight to mpv. v1.2 introduces the abstraction; existing tracks
default to ``source="ytmusic"`` so sessions written under v1.1 keep playing.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional


class NotSupportedError(Exception):
    """A source declined a capability call. Caller should treat as graceful."""


_COUNT_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_count(text: object) -> int:
    """Best-effort '84.2M views' / '1,204' / '3.4K' → int. 0 when hopeless.

    YT hands counts back in whatever shape the surface felt like — raw
    strings with separators from ``videoDetails``, abbreviated display
    strings from search rows. Sources normalize through here so the UI
    only ever sees ints.
    """
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return safe_int(text)
    s = str(text).strip().lower().replace(",", "").replace(" ", "")
    if not s:
        return 0
    m = re.match(r"^([\d.]+)\s*([kmb])?", s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    return int(num * _COUNT_SUFFIXES.get(m.group(2) or "", 1))


def human_count(n: int) -> str:
    """Int → the dim-line form: 84_200_000 → '84.2m'. Empty string for 0
    so callers can join-filter absent stats without special-casing."""
    if n <= 0:
        return ""
    for div, suffix in ((1_000_000_000, "b"), (1_000_000, "m"), (1_000, "k")):
        if n >= div:
            v = n / div
            return (f"{v:.1f}" if v < 100 else f"{v:.0f}").rstrip("0").rstrip(".") + suffix
    return str(n)


def safe_int(value: object, default: int = 0) -> int:
    """Coerce a remote/untrusted value to int, never raising.

    Guards the ``int(server_json.get("duration") or 0)`` pattern: a
    non-numeric string raises ``ValueError``, and since Python 3.11 so does a
    numeric string longer than ~4300 digits (int/str conversion limit). A
    malicious or MITM'd source could send either and crash the worker thread
    parsing the response, so every duration/count coercion routes through
    here and falls back to ``default`` instead.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        # Last resort for numeric-ish floats/strings like "123.0".
        try:
            return int(float(value))
        except (ValueError, TypeError, OverflowError):
            return default


# ---------- shared dataclasses ----------

@dataclass
class Track:
    # `video_id` is the primary key per source. For YT Music it's the actual
    # videoId; for SoundCloud/Bandcamp/Mixcloud it's the canonical permalink
    # URL; for local files it's the absolute path.
    video_id: str
    title: str
    artists: str
    album: str = ""
    duration: str = ""
    duration_seconds: int = 0
    thumbnail: str = ""
    source: str = "ytmusic"
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


@dataclass
class AlbumEntry:
    browse_id: str
    title: str
    artists: str = ""
    year: str = ""
    thumbnail: str = ""
    playlist_id: str = ""


@dataclass
class ArtistEntry:
    channel_id: str
    name: str
    thumbnail: str = ""
    subscribers: str = ""


@dataclass
class AlbumDetail:
    browse_id: str
    title: str
    artists: str = ""
    year: str = ""
    duration: str = ""
    track_count: int = 0
    thumbnail: str = ""
    description: str = ""
    tracks: list = field(default_factory=list)
    # The album's audio playlist id (YT's OLAK5uy_…) — what add-to-library
    # and album radio want. Empty for sources without the concept.
    playlist_id: str = ""


@dataclass
class ArtistDetail:
    channel_id: str
    name: str
    description: str = ""
    subscribers: str = ""
    monthly_listeners: str = ""
    thumbnail: str = ""
    top_songs: list = field(default_factory=list)
    albums: list = field(default_factory=list)
    singles: list = field(default_factory=list)
    related: list = field(default_factory=list)
    # None = the source doesn't know (button hidden); bool = current state.
    subscribed: bool | None = None


@dataclass
class SongInsights:
    """Community numbers for one track — the data every major client shows
    and every API hands out in fragments. Assembled from whichever fragments
    a source has; zero/empty means unknown, and the UI renders nothing for
    unknowns (no '0 likes' lies).
    """
    video_id: str
    views: int = 0
    likes: int = 0
    comment_count: int = 0
    year: str = ""              # release/upload year, e.g. "2016"
    channel: str = ""


@dataclass
class Comment:
    """One public comment. ``parent_id`` is empty for top-level comments and
    holds the parent's ``comment_id`` for replies — the source returns a
    flat list and the view groups."""
    comment_id: str
    author: str
    text: str
    likes: int = 0
    time_text: str = ""         # relative, e.g. "2y"
    parent_id: str = ""
    pinned: bool = False
    hearted: bool = False       # creator-hearted
    by_uploader: bool = False
    author_thumbnail: str = ""


@dataclass
class CreditSection:
    """One block of song credits: 'performed by' → [names]."""
    title: str
    names: list = field(default_factory=list)


@dataclass
class ChartEntry:
    """One ranked row on a chart. ``trend`` is "up" | "down" | "neutral" |
    "new" | "" (unknown). The payload rides in a ShelfItem so chart rows
    dispatch (play / open artist) exactly like shelf cards."""
    rank: int
    trend: str
    item: "ShelfItem"


@dataclass
class MoodCategory:
    """One moods-&-genres chip: display title + the opaque params blob the
    source wants back to enumerate that category's playlists."""
    title: str
    params: str


@dataclass
class ShelfItem:
    kind: str   # "song" | "video" | "album" | "playlist" | "artist"
    title: str
    subtitle: str = ""
    thumbnail: str = ""
    track: Track | None = None
    album: AlbumEntry | None = None
    artist: ArtistEntry | None = None
    playlist: PlaylistEntry | None = None


@dataclass
class Shelf:
    title: str
    items: list = field(default_factory=list)


# ---------- StreamRef: how a source hands a Track off to a backend ----------

@dataclass
class StreamRef:
    """What `resolve_stream` returns. ``backend`` tells the router which
    playback backend handles ``payload``.

    - mpv backend: payload is a URL or absolute file path.
    - librespot backend (v1.2.1): payload is a ``spotify:track:<id>`` URI.
    - musickit backend (v1.2.2): payload is an Apple Music catalog id.
    """
    backend: str
    payload: str
    headers: dict | None = None


# ---------- MusicSource ABC ----------

class MusicSource(ABC):
    """Minimum surface every source must implement, plus optional capability
    methods that default to ``NotSupportedError`` so callers can probe.

    ``capabilities`` declares which optional methods are wired. Views check
    ``source.supports("library")`` (etc.) and render a clean empty state
    when the active source doesn't expose the feature, instead of catching
    NotSupportedError after a wasted thread spin.
    """

    slug: str = ""
    name: str = ""
    icon: str = ""
    needs_auth: bool = False
    backend_slug: str = "mpv"
    short_tag: str = ""        # 2-char badge for federated search rows
    # True when ``begin_auth()`` is implemented, i.e. the source can sign in
    # from inside the generic source dialog. Sources with bespoke setup flows
    # (Spotify OAuth, Subsonic server form) leave this False — they handle
    # sign-in via their own gear dialogs, not the generic [sign in] button.
    supports_in_app_auth: bool = False

    # Known capability keys: "library", "albums", "artists", "videos",
    # "home", "radio", "lyrics", "rating". Required surface (search_songs +
    # resolve_stream) is implicit.
    #
    # v1.5 community/depth keys: "insights" (view/like counts), "comments",
    # "credits", "related" (per-song related shelves), "history_sync"
    # (report_play writes to the source's own history so its recommender
    # learns from tide), "explore" (new releases / top songs / moods bundle),
    # "charts" (ranked artists + chart playlists), "moods" (mood/genre
    # playlist browsing).
    capabilities: frozenset = frozenset()

    def supports(self, cap: str) -> bool:
        return cap in self.capabilities

    # ---------- auth ----------

    def is_authenticated(self) -> bool:
        return not self.needs_auth

    def begin_auth(self, parent_widget) -> bool:
        raise NotSupportedError(f"{self.slug} has no in-app auth")

    def sign_out(self) -> None:
        return None

    def status_text(self) -> str:
        """One-line human label for the Source panel."""
        return "ok" if self.is_authenticated() else "not signed in"

    # ---------- required capabilities ----------

    @abstractmethod
    def search_songs(self, query: str, limit: int = 20) -> list[Track]: ...

    @abstractmethod
    def resolve_stream(self, track: Track) -> StreamRef: ...

    # ---------- optional capabilities ----------

    def search_albums(self, query: str, limit: int = 20) -> list[AlbumEntry]:
        raise NotSupportedError(f"{self.slug}: search_albums")

    def search_artists(self, query: str, limit: int = 20) -> list[ArtistEntry]:
        raise NotSupportedError(f"{self.slug}: search_artists")

    def search_videos(self, query: str, limit: int = 20) -> list[Track]:
        raise NotSupportedError(f"{self.slug}: search_videos")

    def get_library_playlists(self, limit: int = 100) -> list[PlaylistEntry]:
        raise NotSupportedError(f"{self.slug}: get_library_playlists")

    def get_playlist(self, playlist_id: str, limit: int = 500) -> PlaylistDetail:
        raise NotSupportedError(f"{self.slug}: get_playlist")

    def get_album(self, browse_id: str) -> AlbumDetail | None:
        raise NotSupportedError(f"{self.slug}: get_album")

    def get_artist(self, channel_id: str) -> ArtistDetail | None:
        raise NotSupportedError(f"{self.slug}: get_artist")

    def get_home(self, limit: int = 5) -> list[Shelf]:
        raise NotSupportedError(f"{self.slug}: get_home")

    def get_radio(self, video_id: str, exclude: set[str] | None = None,
                  depth: int = 0, mix: str = "balanced") -> list[Track]:
        """More tracks like ``video_id``. ``depth`` counts the refills
        already taken from this seed's radio; a source that can page its
        radio should read further down it rather than start over. Sources
        that can't are free to ignore it. ``mix`` is Settings.radio_mix
        (balanced / familiar / discover / all); a source with one kind of
        radio ignores it too."""
        raise NotSupportedError(f"{self.slug}: get_radio")

    def get_lyrics_for(self, video_id: str) -> str | None:
        return None

    def get_lyrics_for_track(self, track: Track):
        return None

    def rate_song(self, video_id: str, liked: bool) -> None:
        raise NotSupportedError(f"{self.slug}: rate_song")

    def dislike_song(self, video_id: str) -> None:
        """Thumbs-down / 'less of this'. Distinct from ``rate_song(False)``,
        which clears back to neutral — a dislike is negative signal the
        source's recommender should learn from."""
        raise NotSupportedError(f"{self.slug}: dislike_song")

    def is_liked(self, video_id: str) -> bool | None:
        return None

    # ---------- v1.5 community / depth ----------

    def get_song_insights(self, video_id: str) -> "SongInsights | None":
        """Community numbers for a track; None when unknown. Cheap-or-cached
        expected: views may lag reality by the cache TTL and that's fine."""
        return None

    def report_play(self, track: Track) -> bool:
        """Tell the source a play happened, so *its* history/recommender
        learns from tide. True when the source accepted it. Only called when
        the user opted in (settings.report_plays) — sources never self-arm.
        """
        return False

    def get_comments(self, video_id: str, *, sort: str = "top",
                     limit: int = 60) -> list:
        """Flat list of ``Comment`` (replies carry ``parent_id``).
        ``sort`` is "top" | "new". Expected slow (seconds) — callers own the
        worker thread and the spinner."""
        raise NotSupportedError(f"{self.slug}: get_comments")

    def get_related_for(self, track: Track) -> list:
        """Related-content shelves for a track ("you might also like",
        "other performances", …) as the same ``Shelf`` records home uses."""
        raise NotSupportedError(f"{self.slug}: get_related_for")

    def get_credits_for(self, track: Track) -> list:
        """Song credits as ``CreditSection`` blocks. Empty list = the source
        supports credits but has none for this track."""
        raise NotSupportedError(f"{self.slug}: get_credits_for")

    # ---------- v1.5 browse (home engine feeds) ----------

    def get_explore_data(self) -> dict:
        """Editorial browse bundle. Keys (all optional): "new_releases"
        [AlbumEntry], "top_songs" [ChartEntry], "new_videos" [Track],
        "moods" [(section_title, [MoodCategory])]."""
        raise NotSupportedError(f"{self.slug}: get_explore_data")

    def get_charts_data(self, country: str = "ZZ") -> dict:
        """Charts bundle. Keys (all optional): "artists" [ChartEntry],
        "playlists" [PlaylistEntry], "selected" (country label)."""
        raise NotSupportedError(f"{self.slug}: get_charts_data")

    def get_mood_playlists_list(self, params: str) -> list:
        """Playlists for one MoodCategory, as PlaylistEntry records."""
        raise NotSupportedError(f"{self.slug}: get_mood_playlists_list")

    # ---------- v1.5 library parity ----------
    # Capability keys: "library_full" (songs/albums/artists/subscriptions
    # tabs), "playlist_edit" (create/add/remove/rename/delete),
    # "subscribe" (follow artists), "remote_history" (the source's own
    # play history, readable + per-row removable), "playlist_search".

    def get_library_songs_list(self, limit: int = 200,
                               order: str | None = None) -> list:
        """The user's saved songs as Track records. ``order`` is
        "a_to_z" | "z_to_a" | "recently_added" | None (source default)."""
        raise NotSupportedError(f"{self.slug}: get_library_songs_list")

    def get_library_albums_list(self, limit: int = 100,
                                order: str | None = None) -> list:
        raise NotSupportedError(f"{self.slug}: get_library_albums_list")

    def get_library_artists_list(self, limit: int = 100,
                                 order: str | None = None) -> list:
        raise NotSupportedError(f"{self.slug}: get_library_artists_list")

    def get_library_subscriptions_list(self, limit: int = 100) -> list:
        """Artists the user follows, as ArtistEntry records."""
        raise NotSupportedError(f"{self.slug}: get_library_subscriptions_list")

    def create_playlist_remote(self, title: str, description: str = "",
                               video_ids: list | None = None) -> str:
        """Create a playlist on the source; returns its id ('' on refusal)."""
        raise NotSupportedError(f"{self.slug}: create_playlist_remote")

    def add_to_playlist(self, playlist_id: str, video_ids: list) -> bool:
        raise NotSupportedError(f"{self.slug}: add_to_playlist")

    def remove_from_playlist(self, playlist_id: str, tracks: list) -> bool:
        """Remove Track records from a playlist. Sources that need more
        than a video id (YT wants setVideoId) read it from track.extras."""
        raise NotSupportedError(f"{self.slug}: remove_from_playlist")

    def edit_playlist_remote(self, playlist_id: str, *, title: str | None = None,
                             description: str | None = None,
                             privacy: str | None = None) -> bool:
        raise NotSupportedError(f"{self.slug}: edit_playlist_remote")

    def delete_playlist_remote(self, playlist_id: str) -> bool:
        raise NotSupportedError(f"{self.slug}: delete_playlist_remote")

    def set_artist_subscribed(self, channel_id: str, subscribed: bool) -> bool:
        raise NotSupportedError(f"{self.slug}: set_artist_subscribed")

    def add_album_to_library(self, album: "AlbumDetail | AlbumEntry") -> bool:
        raise NotSupportedError(f"{self.slug}: add_album_to_library")

    def get_remote_history(self, limit: int = 200) -> list:
        """The source's own play history (newest first) as Track records.
        Removal tokens, when the source has them, ride in track.extras."""
        raise NotSupportedError(f"{self.slug}: get_remote_history")

    def remove_remote_history(self, tracks: list) -> bool:
        raise NotSupportedError(f"{self.slug}: remove_remote_history")

    def search_playlists(self, query: str, limit: int = 20) -> list:
        """Community playlist search, as PlaylistEntry records."""
        raise NotSupportedError(f"{self.slug}: search_playlists")

    def get_search_suggestions_list(self, query: str) -> list:
        """Typeahead strings for a partial query (capability "suggest")."""
        raise NotSupportedError(f"{self.slug}: get_search_suggestions_list")

    def get_taste_profile(self) -> list:
        """Artists the source's recommender can be tuned with, as plain
        name strings (capability "taste")."""
        raise NotSupportedError(f"{self.slug}: get_taste_profile")

    def set_taste_profile(self, artists: list) -> bool:
        """Select these artists as the user's taste seeds."""
        raise NotSupportedError(f"{self.slug}: set_taste_profile")
