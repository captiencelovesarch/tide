"""Main window: search + results + queue + now-playing strip."""
from __future__ import annotations

import sys
import time

from PySide6.QtCore import (
    QObject,
    QThread,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QImage,
    QKeySequence,
    QShortcut,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import api, history as history_module, layout as layout_module, qthreads, session as session_module, theming
from ..player import PlayState, Player
from ..playback import PlaybackRouter
from ..playback.prefetch import StreamPrefetch
from ..sources import StreamRef, registry as source_registry
from ..queue import Queue, Role
from .album import AlbumView
from .artist import ArtistView
from .explore import ExploreView
from .history import HistoryView
from .library import LibraryView
from .loading_indicator import LoadingIndicator
from .lyrics import LyricsView
from .track_row import TrackRowDelegate
from .variants import (
    make_album_art,
    make_controls,
    make_now_label,
    make_progress,
    make_volume,
)
from .visualizer import VisualizerView
from .widgets import AlbumArt, BracketButton, MonoProgress, MonoVolume, NowPlayingLabel


# ---------- background workers ----------


class _SearchWorker(QObject):
    done = Signal(int, str, list)   # request gen, filter, results
    failed = Signal(int, str)       # request gen, error

    def __init__(self, api_obj: api.Api, query: str, filter_: str, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.query = query
        self.filter = filter_
        self.gen = gen

    def run(self) -> None:
        try:
            supports = getattr(self.api, "supports", lambda _c: True)
            if self.filter == "albums":
                if not supports("albums"):
                    self.done.emit(self.gen, self.filter, [])
                    return
                out = self.api.search_albums(self.query)
            elif self.filter == "artists":
                if not supports("artists"):
                    self.done.emit(self.gen, self.filter, [])
                    return
                out = self.api.search_artists(self.query)
            elif self.filter == "videos":
                if not supports("videos"):
                    self.done.emit(self.gen, self.filter, [])
                    return
                out = self.api.search_videos(self.query)
            else:
                out = self.api.search_songs(self.query)
            self.done.emit(self.gen, self.filter, out)
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _FederatedSearchWorker(QObject):
    """Fan-out search across all enabled sources, merge as each returns.

    Songs-only — albums/artists/videos vary too much per source to merge
    meaningfully in v1.2.0 (and several sources don't expose them at all).
    """

    partial = Signal(str, list)         # source slug, tracks
    done = Signal(int, str, list)        # request gen, filter, all_tracks
    failed = Signal(int, str)            # request gen, error

    def __init__(self, query: str, gen: int) -> None:
        super().__init__()
        self.query = query
        self.gen = gen
        self._collected: list = []
        self._remaining: int = 0
        self._lock_remaining = False

    def run(self) -> None:
        from PySide6.QtCore import QRunnable, QThreadPool
        from ..sources import registry as _registry
        sources = _registry().enabled_sources()
        if not sources:
            self.done.emit(self.gen, "songs", [])
            return
        self._remaining = len(sources)

        outer = self

        class _One(QRunnable):
            def __init__(self_inner, source):
                super().__init__()
                self_inner.source = source

            def run(self_inner):
                try:
                    tracks = self_inner.source.search_songs(outer.query, limit=15)
                except Exception:
                    tracks = []
                outer.partial.emit(self_inner.source.slug, tracks)

        # Connect partial → accumulator BEFORE dispatch so we don't miss
        # fast returns.
        self.partial.connect(self._on_partial)
        pool = QThreadPool.globalInstance()
        for s in sources:
            pool.start(_One(s))

    def _on_partial(self, slug: str, tracks: list) -> None:
        self._collected.extend(tracks)
        self._remaining -= 1
        if self._remaining <= 0:
            self.done.emit(self.gen, "songs", list(self._collected))


class _ResolveWorker(QObject):
    # video_id, StreamRef (or its mpv-payload URL for back-compat consumers)
    resolved = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, track: api.Track) -> None:
        super().__init__()
        self.track = track
        self.video_id = track.video_id

    def run(self) -> None:
        try:
            source = source_registry().get(self.track.source or "ytmusic")
            if source is None:
                raise RuntimeError(f"no source registered for {self.track.source!r}")
            ref = source.resolve_stream(self.track)
            self.resolved.emit(self.video_id, ref)
        except Exception as exc:
            self.failed.emit(self.video_id, str(exc))


class _RadioWorker(QObject):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api, video_id: str, exclude: list[str]) -> None:
        super().__init__()
        self.api = api_obj
        self.video_id = video_id
        self.exclude = set(exclude)

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_radio(self.video_id, exclude=self.exclude))
        except Exception as exc:
            self.failed.emit(str(exc))


class _RateWorker(QObject):
    done = Signal(str, bool)        # video_id, new_liked_state
    failed = Signal(str, str)       # video_id, msg

    def __init__(self, api_obj: api.Api, video_id: str, liked: bool) -> None:
        super().__init__()
        self.api = api_obj
        self.video_id = video_id
        self.liked = liked

    def run(self) -> None:
        try:
            self.api.rate_song(self.video_id, self.liked)
            self.done.emit(self.video_id, self.liked)
        except Exception as exc:
            self.failed.emit(self.video_id, str(exc))


class _InstrumentalSearchWorker(QObject):
    """Off-main-thread instrumental hunter for the karaoke mute toggle.

    The search method is a synchronous loop over enabled sources; running
    it on the GUI thread would freeze the UI for the round-trips. Same
    QThread + worker lifetime pattern as the other workers in this file.
    """
    done = Signal(object, object)        # vocal_track, InstrumentalMatch|None
    failed = Signal(object, str)         # vocal_track, msg

    def __init__(self, vocal_track: api.Track) -> None:
        super().__init__()
        self.vocal_track = vocal_track

    def run(self) -> None:
        try:
            from .. import instrumental as _inst
            match = _inst.find_instrumental(self.vocal_track)
            self.done.emit(self.vocal_track, match)
        except Exception as exc:
            self.failed.emit(self.vocal_track, str(exc))


# ---------- main window ----------


class MainWindow(QMainWindow):
    # How far [prev] can walk back across queue replacements.
    PLAY_HISTORY_MAX = 100
    # How often to re-check that the active source's session still authenticates.
    AUTH_HEARTBEAT_MS = 10 * 60 * 1000
    # How often to compare the recorded cookie expiry against the clock.
    EXPIRY_WATCH_MS = 30 * 60 * 1000
    # Warn this far ahead of the recorded expiry.
    EXPIRY_WARN_SECONDS = 3 * 24 * 3600

    def __init__(self, api_obj: api.Api, player: PlaybackRouter | Player) -> None:
        super().__init__()
        self.setWindowTitle("tide")
        # Translucency must be set BEFORE the first show — app.py applies the
        # theme before constructing the window, so the flag is known here.
        current_theme = theming.manager().current()
        if current_theme is not None:
            self._apply_window_translucency(current_theme)
        self.resize(1100, 720)
        self.api = api_obj
        self.player = player
        self.queue = Queue(self)

        # thread / worker refs (hold to prevent GC during run())
        self._search_thread: QThread | None = None
        self._search_worker: _SearchWorker | None = None
        # Monotonic id for search requests. Results/failures carry the id of
        # the request that produced them; anything not matching the latest id
        # is a straggler from an abandoned query and gets dropped — otherwise
        # a slow "abba" search lands after a fast "beatles" one and appends
        # its rows into the beatles result list.
        self._search_gen = 0
        self._resolve_thread: QThread | None = None
        self._resolve_worker: _ResolveWorker | None = None
        self._radio_thread: QThread | None = None
        self._radio_worker: _RadioWorker | None = None
        self._rate_thread: QThread | None = None
        self._rate_worker: _RateWorker | None = None
        self._liked_current: bool = False
        self._mini_mode: bool = False
        self._mini = None                   # lazy MiniPlayer window
        self._upper_wrap_widget = None

        # Stream-URL prefetch — kicks off while the current track is finishing
        # so the next _play_track sees a warm cache and skips the resolve
        # worker. Best-effort; on miss the normal resolve path runs.
        self._prefetch = StreamPrefetch(self)
        # In-flight join: a click on a track whose prefetch is mid-resolve
        # waits for THAT resolve instead of racing a duplicate yt-dlp round
        # trip. The fallback timer hardens against a prefetch worker that
        # never reports back (hung network call) — after it fires we resolve
        # ourselves like the old code always did.
        self._awaiting_prefetch_vid: str | None = None
        self._await_fallback_timer = QTimer(self)
        self._await_fallback_timer.setSingleShot(True)
        self._await_fallback_timer.setInterval(10_000)
        self._await_fallback_timer.timeout.connect(self._on_prefetch_join_timeout)
        self._prefetch.resolved.connect(self._on_prefetch_join_resolved)
        self._prefetch.failed.connect(self._on_prefetch_join_failed)
        # Click-to-audio timing for the always-on per-play summary line.
        self._perf_t0: float | None = None
        self._perf_vid: str = ""
        self._perf_path: str = "cold"
        self._perf_resolve_ms: float | None = None
        # Position-prefetch trigger threshold (seconds remaining). When the
        # current track's tail crosses this, we request prefetch for the
        # next queued track. Tuned to comfortably exceed a slow yt-dlp call.
        self._prefetch_lead_secs = 15.0
        # Track-scoped guard: a single video_id we've already requested for
        # the current playback. Reset when the playing track changes.
        self._prefetch_armed_for: str | None = None

        # Sleep timer state
        self._sleep_mode = None              # SleepMode or None
        self._sleep_deadline: float | None = None
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setInterval(1000)
        self._sleep_timer.timeout.connect(self._on_sleep_tick)

        self._current: api.Track | None = None
        self._auto_radio_on_play = True   # play-now seeds a radio by default
        self._last_position: float = 0.0
        self._restoring_session: bool = False
        self._session_dirty: bool = False
        # Cross-queue play history for [prev]. queue.back() only walks the
        # current queue array, and _play_now() clears that array on every
        # pick, so this is the only thing that survives a queue replacement.
        self._play_history: list[api.Track] = []
        self._navigating_back: bool = False

        # Debounced session save — fires ~2s after the last change.
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.setInterval(2000)
        self._session_save_timer.timeout.connect(self._save_session_now)

        self._net = QNetworkAccessManager(self)
        self._art_for_video_id: str | None = None

        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme_changed)

        self._build_ui()
        self._wire_player()
        self._wire_queue()
        self._wire_shortcuts()
        # Has to come AFTER _build_ui because hover-prefetch needs all
        # the track-bearing list views to exist on self / its children.
        self._wire_hover_prefetch()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        # ----- nav rail -----
        # The search view doubles as "home" — search bar + explore shelves
        # in one surface. Per-tab [explore] disappears as a separate nav
        # entry; clicking [home] goes there.
        self.nav_home_btn = BracketButton("home")
        self.nav_library_btn = BracketButton("library")
        self.nav_queue_btn = BracketButton("queue")
        self.nav_lyrics_btn = BracketButton("lyrics")
        self.nav_history_btn = BracketButton("history")
        self.nav_visualizer_btn = BracketButton("visualizer")
        self.nav_source_btn = BracketButton("source")
        self.nav_settings_btn = BracketButton("settings")
        # Slot map used by _apply_nav_icons to walk both ways (button → slot
        # for picking the icon, slot → button for hot-swap).
        self._nav_buttons: dict[str, "BracketButton"] = {
            "home": self.nav_home_btn,
            "library": self.nav_library_btn,
            "queue": self.nav_queue_btn,
            "lyrics": self.nav_lyrics_btn,
            "history": self.nav_history_btn,
            "visualizer": self.nav_visualizer_btn,
            "source": self.nav_source_btn,
            "settings": self.nav_settings_btn,
        }
        self.nav_home_btn.clicked.connect(lambda: self._switch_view("home"))
        self.nav_library_btn.clicked.connect(lambda: self._switch_view("library"))
        self.nav_queue_btn.clicked.connect(lambda: self._switch_view("queue"))
        self.nav_lyrics_btn.clicked.connect(lambda: self._switch_view("lyrics"))
        self.nav_history_btn.clicked.connect(lambda: self._switch_view("history"))
        self.nav_visualizer_btn.clicked.connect(lambda: self._switch_view("visualizer"))
        self.nav_source_btn.clicked.connect(lambda: self._switch_view("source"))
        self.nav_settings_btn.clicked.connect(self.open_settings)

        nav_col = QVBoxLayout()
        nav_col.setContentsMargins(10, 14, 10, 14)
        nav_col.setSpacing(2)
        nav_col.addWidget(self.nav_home_btn)
        nav_col.addWidget(self.nav_library_btn)
        nav_col.addWidget(self.nav_queue_btn)
        nav_col.addWidget(self.nav_lyrics_btn)
        nav_col.addWidget(self.nav_history_btn)
        nav_col.addWidget(self.nav_visualizer_btn)
        nav_col.addWidget(self.nav_source_btn)
        nav_col.addStretch(1)
        nav_col.addWidget(self.nav_settings_btn)
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setLayout(nav_col)
        nav.setFixedWidth(140)

        # ----- search view -----
        self.search = QLineEdit()
        self.search.returnPressed.connect(self._on_search)
        self.search.setClearButtonEnabled(True)
        self._refresh_search_placeholder()

        self.heading = QLabel(self._line_heading("results"))
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Search filter tabs (songs/videos/albums/artists).
        self.search_tab_songs = BracketButton("songs")
        self.search_tab_videos = BracketButton("videos")
        self.search_tab_albums = BracketButton("albums")
        self.search_tab_artists = BracketButton("artists")
        self._search_filter = "songs"
        for btn, name in (
            (self.search_tab_songs, "songs"),
            (self.search_tab_videos, "videos"),
            (self.search_tab_albums, "albums"),
            (self.search_tab_artists, "artists"),
        ):
            btn.clicked.connect(lambda _=False, n=name: self._set_search_filter(n))

        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(2)
        tabs_row.addWidget(self.search_tab_songs)
        tabs_row.addWidget(self.search_tab_videos)
        tabs_row.addWidget(self.search_tab_albums)
        tabs_row.addWidget(self.search_tab_artists)
        tabs_row.addStretch(1)

        self.results = QListWidget()
        self.results.itemActivated.connect(self._on_result_activated)
        self.results.setUniformItemSizes(True)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_results_menu)
        self._track_delegate = TrackRowDelegate(self)
        self._track_delegate.attach(self.results)
        self.results.setItemDelegate(self._track_delegate)

        # Card grid used by [albums] and [artists] tabs.
        from .card import CardGrid
        self.results_cards = CardGrid()
        self.results_cards.setVisible(False)

        results_scroll = QScrollArea()
        results_scroll.setWidget(self.results_cards)
        results_scroll.setWidgetResizable(True)
        results_scroll.setFrameShape(QScrollArea.NoFrame)
        results_scroll.setVisible(False)
        self._results_card_scroll = results_scroll

        # The search view doubles as "home" — when the search bar is empty,
        # the explore shelves render below it (YT Music site shape). The
        # explore_view widget is constructed further down; we add it to the
        # layout via a placeholder slot and parent it in after it exists.
        self._home_explore_slot = QVBoxLayout()
        self._home_explore_slot.setContentsMargins(0, 0, 0, 0)
        self._home_explore_slot.setSpacing(0)

        # Tabs row stays hidden until the user types something — empty-state
        # home view just shows shelves.
        self._tabs_row_widget = QWidget()
        self._tabs_row_widget.setLayout(tabs_row)
        self._tabs_row_widget.setVisible(False)
        self.heading.setVisible(False)
        self.results.setVisible(False)
        results_scroll.setVisible(False)

        from . import scale as _scale
        search_col = QVBoxLayout()
        search_col.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        search_col.setSpacing(_scale.px(8))
        search_col.addWidget(self.search)
        search_col.addWidget(self._tabs_row_widget)
        search_col.addWidget(self.heading)
        search_col.addWidget(self.results, stretch=1)
        search_col.addWidget(results_scroll, stretch=1)
        search_col.addLayout(self._home_explore_slot, stretch=1)
        search_view = QWidget()
        search_view.setLayout(search_col)
        # Hook the search bar's textChanged so clearing returns to home view.
        self.search.textChanged.connect(self._on_search_text_changed)

        # ----- queue view -----
        self.queue_heading = QLabel(self._line_heading("queue"))
        self.queue_heading.setProperty("class", "dim")

        self.queue_view = QListView()
        self.queue_view.setModel(self.queue)
        self.queue_view.setUniformItemSizes(True)
        self.queue_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_view.customContextMenuRequested.connect(self._on_queue_menu)
        self.queue_view.doubleClicked.connect(self._on_queue_double)
        self.queue_view.setDragDropMode(QListView.InternalMove)
        self.queue_view.setDefaultDropAction(Qt.MoveAction)
        self.queue_view.setSelectionMode(QListView.SingleSelection)
        self.queue_view.setMovement(QListView.Snap)
        self.queue_view.setDragEnabled(True)
        self.queue_view.setAcceptDrops(True)
        self.queue_view.setDropIndicatorShown(True)
        self._track_delegate.attach(self.queue_view)
        self.queue_view.setItemDelegate(self._track_delegate)

        self.radio_btn = BracketButton("radio: off")
        self.radio_btn.clicked.connect(self._on_radio_toggle)
        self.clear_btn = BracketButton("clear queue")
        self.clear_btn.clicked.connect(self.queue.clear)

        queue_actions = QHBoxLayout()
        queue_actions.addWidget(self.radio_btn)
        queue_actions.addWidget(self.clear_btn)
        queue_actions.addStretch(1)

        queue_col = QVBoxLayout()
        queue_col.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        queue_col.setSpacing(_scale.px(8))
        queue_col.addWidget(self.queue_heading)
        queue_col.addLayout(queue_actions)
        queue_col.addWidget(self.queue_view, stretch=1)
        queue_view = QWidget()
        queue_view.setLayout(queue_col)

        # ----- library view -----
        self.library_view = LibraryView(self.api)
        self.library_view.play_now_requested.connect(self._play_now)
        self.library_view.queue_add_requested.connect(self._queue_add)
        self.library_view.queue_next_requested.connect(self._queue_next)
        self.library_view.radio_requested.connect(self._start_radio)
        self.library_view.play_all_requested.connect(self._play_all)
        self.library_view.status_message.connect(self._set_status)

        # ----- lyrics view -----
        self.lyrics_view = LyricsView(self.api)
        # "mute lyrics" instrumental-swap. The view emits when the user
        # toggles the button; MainWindow owns the player + source
        # registry so the actual hunt + swap lands here.
        self.lyrics_view.toggle_instrumental_requested.connect(
            self._on_instrumental_swap_requested
        )
        # Swap state — remembers what to switch back to + the position
        # at the moment of swap so toggling off resumes mid-song.
        self._instrumental_swap_thread: QThread | None = None
        self._instrumental_swap_worker: QObject | None = None
        self._instrumental_vocal_track = None
        self._instrumental_swap_position: float = 0.0
        self._instrumental_active: bool = False
        # Used by the karaoke "mute lyrics" swap + by any future feature
        # that needs to load a track and snap to a non-zero start point
        # on first PLAYING (e.g. session-restore mid-track).
        self._pending_seek: float = 0.0

        # ----- history view -----
        self.history_view = HistoryView()
        self.history_view.play_now_requested.connect(self._play_now)
        self.history_view.queue_add_requested.connect(self._queue_add)
        self.history_view.radio_requested.connect(self._start_radio)
        self.history_view.status_message.connect(self._set_status)

        # ----- explore + album + artist views -----
        self.explore_view = ExploreView(self.api)
        self.explore_view.play_now_requested.connect(self._play_now)
        self.explore_view.queue_add_requested.connect(self._queue_add)
        self.explore_view.radio_requested.connect(self._start_radio)
        self.explore_view.album_requested.connect(self._open_album_entry)
        self.explore_view.artist_requested.connect(self._open_artist_entry)
        self.explore_view.playlist_requested.connect(self._open_playlist_entry)
        self.explore_view.status_message.connect(self._set_status)
        # Mount explore as the home-view bottom half, below the search bar.
        self._home_explore_slot.addWidget(self.explore_view, stretch=1)

        self.album_view = AlbumView(self.api)
        self.album_view.back_requested.connect(self._go_back)
        self.album_view.play_now_requested.connect(self._play_now)
        self.album_view.queue_add_requested.connect(self._queue_add)
        self.album_view.queue_next_requested.connect(self._queue_next)
        self.album_view.radio_requested.connect(self._start_radio)
        self.album_view.play_all_requested.connect(self._play_all)
        self.album_view.artist_requested.connect(self._open_artist_by_name)
        self.album_view.status_message.connect(self._set_status)

        self.artist_view = ArtistView(self.api)
        self.artist_view.back_requested.connect(self._go_back)
        self.artist_view.play_now_requested.connect(self._play_now)
        self.artist_view.queue_add_requested.connect(self._queue_add)
        self.artist_view.queue_next_requested.connect(self._queue_next)
        self.artist_view.radio_requested.connect(self._start_radio)
        self.artist_view.play_all_requested.connect(self._play_all)
        self.artist_view.album_requested.connect(self._open_album_entry)
        self.artist_view.artist_requested.connect(self._open_artist_entry)
        self.artist_view.status_message.connect(self._set_status)

        # ----- visualizer view -----
        self.visualizer_view = VisualizerView()
        self.visualizer_view.status_message.connect(self._set_status)

        # ----- source panel -----
        from .source_panel import SourcePanel
        # _settings is attached by app.py after the window is built. To avoid
        # a chicken-and-egg, fall back to a fresh Settings instance — but the
        # panel is always re-created against the real one when it's set.
        from ..settings import Settings as _Settings
        initial_settings = getattr(self, "_settings", None) or _Settings()
        self.source_view = SourcePanel(initial_settings)
        self.source_view.active_changed.connect(self._on_active_source_changed)
        self.source_view.enabled_changed.connect(self._on_source_enabled_changed)
        self.source_view.settings_changed.connect(self._persist_settings)
        self.source_view.settings_changed.connect(self._refresh_search_placeholder)
        self.source_view.local_dir_changed.connect(self._on_local_dir_changed)
        # Session-death notifications (e.g. imported YT Music cookies started
        # 401ing). Sources report from worker threads; AutoConnection queues
        # delivery onto this (GUI) thread, so the slot may build widgets.
        self._auth_expired_toasted: set[str] = set()
        source_registry().auth_expired.connect(self._on_source_auth_expired)

        # Auth heartbeat. Cookie death used to surface only when the user
        # happened to touch the API — i.e. mid-session, as songs quietly
        # started resolving to the wrong thing. Poll a cheap authenticated
        # endpoint on a timer so expiry announces itself instead.
        self._auth_heartbeat = QTimer(self)
        self._auth_heartbeat.setInterval(self.AUTH_HEARTBEAT_MS)
        self._auth_heartbeat.timeout.connect(self._run_auth_heartbeat)
        self._auth_heartbeat.start()
        # And warn *before* the recorded cookie expiry lands, so a refresh can
        # happen at a moment of the user's choosing rather than mid-song.
        self._expiry_warned = False
        self._expiry_watch = QTimer(self)
        self._expiry_watch.setInterval(self.EXPIRY_WATCH_MS)
        self._expiry_watch.timeout.connect(self._check_session_expiry)
        self._expiry_watch.start()
        QTimer.singleShot(8000, self._check_session_expiry)

        # ----- audio FX panel -----
        from .audio_fx_view import AudioFxView
        self.audio_fx_view = AudioFxView()
        self.audio_fx_view.state_changed.connect(self._on_audio_fx_state_changed)

        # ----- stack -----
        # search_view contains both the search bar AND explore shelves, so
        # there's no separate explore index. The old idx 5 slot is held by
        # a hidden placeholder so the existing _switch_view branches that
        # reference idx 5 keep working — they're rerouted to "home" below.
        self.stack = QStackedWidget()
        # Named so the adaptive-background QSS can transparentize content
        # containers and QScrollArea viewports. See theming._CONTENT_BACKDROP_QSS.
        self.stack.setObjectName("contentStack")
        self.stack.addWidget(search_view)            # 0 — home (search + explore)
        self.stack.addWidget(self.library_view)      # 1
        self.stack.addWidget(queue_view)             # 2
        self.stack.addWidget(self.lyrics_view)       # 3
        self.stack.addWidget(self.history_view)      # 4
        self.stack.addWidget(QWidget())              # 5 — unused placeholder
        self.stack.addWidget(self.album_view)        # 6
        self.stack.addWidget(self.artist_view)       # 7
        self.stack.addWidget(self.visualizer_view)   # 8
        self.stack.addWidget(self.source_view)       # 9
        self.stack.addWidget(self.audio_fx_view)     # 10 — v1.2.2 audio FX rack

        # Simple back stack of previous indices so AlbumView/ArtistView can pop.
        self._view_history: list[int] = []

        upper = QHBoxLayout()
        upper.setContentsMargins(0, 0, 0, 0)
        upper.setSpacing(0)
        upper.addWidget(nav)
        upper.addWidget(self.stack, stretch=1)
        upper_wrap = QWidget()
        upper_wrap.setObjectName("appUpper")
        upper_wrap.setLayout(upper)
        self._upper_wrap_widget = upper_wrap

        # ----- now-playing strip -----
        # Slot variants come from the active layout. Falls back to v1 defaults
        # if no layout has been applied yet.
        layout = layout_module.manager().current()
        self._slot_album_art = layout.slots.get("album_art", "square")
        self._slot_now_label = layout.slots.get("now_label", "stacked")
        self._slot_progress = layout.slots.get("progress", "blocks")
        self._slot_volume = layout.slots.get("volume", "blocks")
        self._slot_controls = layout.slots.get("controls", "bracket")

        self.art = make_album_art(self._slot_album_art, 96)
        self._wire_art_click(self.art)
        self.now_label = make_now_label(self._slot_now_label)
        self.up_next = QLabel("")
        self.up_next.setProperty("class", "dim")
        self.up_next.setVisible(False)
        self.up_next.setContentsMargins(0, 0, 0, 2)
        # Shows remote artist — title of the next track; plain-text so it
        # can't render as HTML (AutoText default).
        self.up_next.setTextFormat(Qt.PlainText)

        self._controls_bundle = make_controls(self._slot_controls)
        self.prev_btn = self._controls_bundle.prev_btn
        self.play_btn = self._controls_bundle.play_btn
        self.next_btn = self._controls_bundle.next_btn
        self.like_btn = self._controls_bundle.like_btn
        self.prev_btn.clicked.connect(self._on_prev_clicked)
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.next_btn.clicked.connect(self._on_next_clicked)
        self.like_btn.clicked.connect(self._on_like_clicked)
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.play_btn.setEnabled(False)
        self.like_btn.setEnabled(False)

        self.progress = make_progress(self._slot_progress)
        self.progress.seek_requested.connect(self.player.seek)

        self.volume = make_volume(self._slot_volume)
        self.volume.volume_changed.connect(self._on_volume_changed)

        # Playback-speed indicator + popover. Shows current speed (e.g.
        # [1.0×]); click to open the popover, right-click to reset. Wired
        # to the player + settings below.
        from .speed import SpeedButton
        self.speed_btn = SpeedButton()
        self.speed_btn.speed_changed.connect(self._on_speed_changed)

        # Audio FX rack quick-access button — opens the small popover with
        # preset / reverb / bass / treble. Right-click toggles the master
        # rack on/off. The full panel (Ctrl+9) shares the same state
        # object via app.py.
        from .audio_fx_popover import AudioFxButton
        self.audio_fx_btn = AudioFxButton()
        self.audio_fx_btn.state_changed.connect(self._on_audio_fx_state_changed)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("class", "dim")
        self.time_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        self.time_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

        strip_layout = self._build_classic_strip_layout()
        strip = QFrame()
        strip.setObjectName("now_playing")
        strip.setLayout(strip_layout)
        strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.strip = strip

        # ----- assemble -----
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(upper_wrap, stretch=1)
        root.addWidget(strip)
        central = QWidget()
        central.setObjectName("appSurface")
        central.setLayout(root)

        # Paint the adaptive backdrop behind the whole app surface, not just
        # the content stack. Structural panes are transparentized in
        # theming._CONTENT_BACKDROP_QSS, so nav/content/now-playing read as
        # one clean surface while controls keep their own QSS backgrounds.
        from .central_bg import CentralBg
        self.central_bg = CentralBg(central)
        self.setCentralWidget(self.central_bg)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("ready")
        # Loading indicator — drives the status bar with a progress bar while
        # a track resolves + buffers. Style is read from settings at start
        # time so the user can change it without restarting.
        self._loading = LoadingIndicator(self)
        self._loading.updated.connect(self.statusBar().showMessage)

    def _line_heading(self, label: str, total: int = 60) -> str:
        styled = theming.styled_case(label)
        line = "─" * max(4, total - len(styled) - 6)
        return f"── {styled} {line}"

    def _set_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    # ---------- multi-source (v1.2) ----------

    def _on_active_source_changed(self, slug: str) -> None:
        """Retarget Search / Library / Explore at the new active source."""
        reg = source_registry()
        new_source = reg.get(slug)
        if new_source is None:
            return
        self.api = new_source
        # Cascade to views that hold their own api ref.
        for view in (self.library_view, self.lyrics_view, self.explore_view,
                     self.album_view, self.artist_view):
            try:
                view.api = new_source
            except Exception:
                pass
        # Clear search results — they're source-specific.
        try:
            self.results.clear()
            self.results_cards.clear()
        except Exception:
            pass
        self._refresh_search_placeholder()
        self.statusBar().showMessage(f"active source: {new_source.name}")

    def _enter_home_mode(self) -> None:
        """Empty-query home: shelves visible, results hidden."""
        self._tabs_row_widget.setVisible(False)
        self.heading.setVisible(False)
        self.results.setVisible(False)
        self._results_card_scroll.setVisible(False)
        self.explore_view.setVisible(True)

    def _enter_results_mode(self) -> None:
        """Active query: shelves hidden, results area shown."""
        self.explore_view.setVisible(False)
        self._tabs_row_widget.setVisible(True)
        self.heading.setVisible(True)
        # Default to the list view while loading — _on_results will swap to
        # the card grid for albums/artists filters.
        self.results.setVisible(True)
        self._results_card_scroll.setVisible(False)

    def _on_search_text_changed(self, txt: str) -> None:
        if not txt.strip():
            self._enter_home_mode()

    def _refresh_search_placeholder(self) -> None:
        federated = (
            getattr(self, "_settings", None) is not None
            and bool(self._settings.federated_search)
        )
        if federated:
            self.search.setPlaceholderText("search all sources…")
            return
        src = getattr(self, "api", None)
        name = getattr(src, "name", "") or "youtube music"
        self.search.setPlaceholderText(f"search {name}…")

    def _run_auth_heartbeat(self) -> None:
        """Fire one cheap authenticated round-trip off-thread.

        The source's own sentinel classifies the result: an auth-shaped
        failure flips its expired flag and emits through the registry, which
        lands on _on_source_auth_expired. Network blips raise non-auth errors
        and are swallowed — a dead wifi link must never look like expiry."""
        from PySide6.QtCore import QRunnable, QThreadPool
        reg = source_registry()
        for slug in ("ytmusic",):
            source = reg.get(slug)
            if source is None or not reg.is_enabled(slug):
                continue
            probe = getattr(source, "probe_auth", None)
            if probe is None:
                continue
            try:
                if not source.is_authenticated():
                    continue          # already known-dead; don't re-probe
            except Exception:
                continue

            class _Probe(QRunnable):
                # Bind through __init__, not the enclosing scope: a closure
                # over the loop variable would resolve at run() time, so
                # every probe would hit the last source in the loop.
                def __init__(self_inner, fn) -> None:
                    super().__init__()
                    self_inner._fn = fn

                def run(self_inner) -> None:
                    try:
                        self_inner._fn()
                    except Exception:
                        pass

            QThreadPool.globalInstance().start(_Probe(probe))

    def _check_session_expiry(self) -> None:
        """Warn ahead of the recorded YT Music cookie expiry.

        Unknown expiry (None) means we simply have no data — an older import,
        or session-scoped cookies — and must NOT be read as 'expiring'."""
        if self._expiry_warned:
            return
        from .. import auth as auth_module
        try:
            remaining = auth_module.seconds_until_expiry()
        except Exception:
            return
        if remaining is None or remaining > self.EXPIRY_WARN_SECONDS:
            return
        reg = source_registry()
        if not reg.is_enabled("ytmusic"):
            return
        if "ytmusic" in self._auth_expired_toasted:
            return      # already shouting about a dead session; don't pile on
        self._expiry_warned = True
        from .toast import show_toast
        if remaining <= 0:
            text = "youtube music: token has expired"
        else:
            days = int(remaining // 86400)
            hours = int(remaining // 3600)
            when = f"{days}d" if days >= 1 else f"{hours}h"
            text = f"youtube music: token expires in {when}"
        show_toast(
            self.toast_host(),
            text,
            action_label="refresh token",
            on_action=lambda: self._begin_source_reauth("ytmusic"),
        )

    def _on_source_auth_expired(self, slug: str) -> None:
        """A source's saved session stopped authenticating (expired cookies).

        Raise ONE sticky toast with a [sign in] action instead of letting
        search / library / home silently degrade into empty views — that
        silence was the old behavior and it made expiry look like random
        breakage."""
        if slug in self._auth_expired_toasted:
            return
        self._auth_expired_toasted.add(slug)
        source = source_registry().get(slug)
        name = getattr(source, "name", slug) or slug
        from .toast import show_toast
        # Short on purpose. The old three-sentence version explained the
        # whole failure mode in a 420px box, and "sign in" undersold what
        # actually happens — the browser is nearly always still signed in,
        # so one click re-imports the cookies with no interaction at all.
        show_toast(
            self.toast_host(),
            f"{name}: token expired",
            action_label="refresh token",
            on_action=lambda: self._begin_source_reauth(slug),
        )
        # Keep the Sources tab honest too (dot → warn, status → expired).
        try:
            self.source_view.refresh_statuses()
            self.source_view._refresh_dot_for(slug)
        except Exception:
            pass

    def _begin_source_reauth(self, slug: str) -> None:
        """Toast-action handler for [refresh token].

        For YT Music, try a silent cookie re-import first — the browser is
        usually still signed in, so the whole thing resolves in one click with
        no dialog. Only when no browser holds a live session do we fall back
        to the wizard, which is the case where the user genuinely has to go
        log in again."""
        if slug == "ytmusic":
            from .wizard import refresh_token_async
            self.statusBar().showMessage("refreshing youtube music token…")
            self._refresh_slug = slug
            refresh_token_async(self._on_token_refreshed, self._on_token_refresh_failed)
            return
        self._open_source_reauth(slug)

    def _on_token_refreshed(self, profile_label: str) -> None:
        """Bound method (never a lambda) — worker signals deliver into the
        emitting thread when connected to lambdas."""
        slug = getattr(self, "_refresh_slug", "ytmusic")
        if not profile_label:
            # No browser had a live session: the user really is signed out.
            self.statusBar().showMessage("no signed-in browser found")
            self._open_source_reauth(slug)
            return
        source = source_registry().get(slug)
        # Rebuild the source's client against the cookies we just wrote.
        try:
            rebuilt = bool(source.reload_client())
        except Exception:
            rebuilt = False
        self._auth_expired_toasted.discard(slug)
        self._expiry_warned = False      # fresh cookies → warn again next time
        from .toast import show_toast
        if not rebuilt:
            show_toast(self.toast_host(), "token refreshed — restart tide to use it")
            return
        self.statusBar().showMessage(f"token refreshed from {profile_label}")
        show_toast(self.toast_host(), f"token refreshed from {profile_label}")
        self._refresh_after_reauth(slug)

    def _on_token_refresh_failed(self, message: str) -> None:
        slug = getattr(self, "_refresh_slug", "ytmusic")
        self.statusBar().showMessage(f"token refresh failed: {message}")
        self._open_source_reauth(slug)

    def _refresh_after_reauth(self, slug: str) -> None:
        if source_registry().active_slug != slug:
            return
        # Reload the views that went stale/empty under the dead session.
        try:
            self.explore_view.reload()
        except Exception:
            pass
        try:
            self.library_view.reload_playlists()
        except Exception:
            pass
        try:
            self.source_view.refresh_statuses()
        except Exception:
            pass

    def _open_source_reauth(self, slug: str) -> None:
        """Fallback path: open the source's own sign-in flow. The modal must
        NOT open inside the click handler (PySide6 + py3.14 segfault) — defer
        a tick, then run the source panel's shared re-auth flow."""
        def _open() -> None:
            try:
                ok = self.source_view.reauth_source(slug)
            except Exception:
                ok = False
            # Either way, allow a future expiry to re-notify: on success the
            # source's flag was reset; on cancel the user said "not now" and
            # the Sources tab keeps showing the expired state.
            self._auth_expired_toasted.discard(slug)
            if not ok:
                return
            source = source_registry().get(slug)
            from .toast import show_toast
            show_toast(self.toast_host(), f"{getattr(source, 'name', slug)}: signed back in")
            self._refresh_after_reauth(slug)
        QTimer.singleShot(0, _open)

    def _on_source_enabled_changed(self, slug: str, enabled: bool) -> None:
        if slug == "local" and enabled:
            reg = source_registry()
            local = reg.get("local")
            if local is not None:
                self._rescan_local_in_background(local)

    def _on_local_dir_changed(self, new_dir: str) -> None:
        reg = source_registry()
        local = reg.get("local")
        if local is None:
            return
        self._rescan_local_in_background(local)

    def _rescan_local_in_background(self, local) -> None:
        from PySide6.QtCore import QRunnable, QThreadPool
        panel = self.source_view

        class _Job(QRunnable):
            def run(self_inner):
                try:
                    local.rescan()
                    local.start_watcher()
                except Exception:
                    pass

        QThreadPool.globalInstance().start(_Job())
        QTimer.singleShot(800, panel.refresh_statuses)
        QTimer.singleShot(3000, panel.refresh_statuses)

    def _persist_settings(self) -> None:
        if not hasattr(self, "_settings") or self._settings is None:
            return
        try:
            from .. import settings as _settings_module
            _settings_module.save(self._settings)
        except Exception:
            pass

    # ---------- nav ----------

    def _ui_sound(self, key: str) -> None:
        """Forward to the optional UiSoundPlayer attached by app.py. No-op
        in headless/test contexts where it was never bound, or when the
        master toggle / music-playing mute is in effect (the player's
        own guards handle those)."""
        player = getattr(self, "ui_sounds", None)
        if player is not None:
            try:
                player.play(key)
            except Exception:
                pass

    def _set_stack_index(self, target: int) -> None:
        """Switch the central stack with a motion-aware crossfade. The
        motion module short-circuits to a synchronous index swap when
        intensity is 'off', so this is one line for all three settings."""
        if self.stack.currentIndex() == target:
            return
        from . import motion as motion_module
        try:
            motion_module.crossfade_stack(
                self.stack, target, dur=motion_module.DUR_SHORT,
            )
        except Exception:
            self.stack.setCurrentIndex(target)

    def _switch_view(self, name: str) -> None:
        # Recording the previous root view for the back-stack — never push
        # transient detail pages.
        prev = self.stack.currentIndex()
        self._ui_sound("nav")
        if name in ("home", "search", "explore"):
            self._set_stack_index(0)
            self.explore_view.ensure_loaded()
            if name == "search":
                self.search.setFocus()
        elif name == "library":
            self._set_stack_index(1)
            if self.library_view.playlists_list.count() == 0:
                self.library_view.reload_playlists()
        elif name == "queue":
            self._set_stack_index(2)
        elif name == "lyrics":
            self._set_stack_index(3)
            self.lyrics_view.show_for(self._current)
        elif name == "history":
            self._set_stack_index(4)
            self.history_view.reload()
        elif name == "visualizer":
            self._set_stack_index(8)
        elif name == "source":
            self._set_stack_index(9)
            self.source_view.refresh_statuses()
        elif name == "audio_fx":
            self._set_stack_index(10)
        # Reset back-stack on root-level navigation so [back] doesn't
        # bounce between top-level views.
        if prev in (6, 7) and self.stack.currentIndex() not in (6, 7):
            self._view_history.clear()

    def _push_view(self, target_index: int) -> None:
        if self.stack.currentIndex() != target_index:
            self._view_history.append(self.stack.currentIndex())
            self._set_stack_index(target_index)

    def _go_back(self) -> None:
        self._ui_sound("back")
        if self._view_history:
            self._set_stack_index(self._view_history.pop())
        else:
            # Fallback: go to search.
            self._set_stack_index(0)

    # ---------- search ----------

    def _on_search(self) -> None:
        # Invalidate any in-flight search first — even on the empty-query
        # path, so a straggler can't paint results over the home screen.
        self._search_gen += 1
        gen = self._search_gen
        q = self.search.text().strip()
        if not q:
            self._enter_home_mode()
            return
        self._enter_results_mode()
        self.heading.setText(self._line_heading(f"searching “{q}”"))
        self.results.clear()
        self.results_cards.clear()

        # Federated mode: songs filter only, fan out to every enabled source.
        federated = (
            getattr(self, "_settings", None) is not None
            and bool(self._settings.federated_search)
            and self._search_filter == "songs"
        )

        if federated:
            self.statusBar().showMessage(f"federated search: {q}")
            worker = _FederatedSearchWorker(q, gen)
            # Federated worker uses QThreadPool internally — no QThread needed.
            worker.done.connect(self._on_results)
            worker.failed.connect(self._on_search_failed)
            self._search_worker = worker
            worker.run()
            return

        self.statusBar().showMessage(f"searching {self._search_filter}: {q}")
        thread = QThread()
        worker = _SearchWorker(self.api, q, self._search_filter, gen)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_results)
        worker.failed.connect(self._on_search_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._search_thread = thread
        self._search_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _set_search_filter(self, name: str) -> None:
        if name == self._search_filter:
            return
        self._search_filter = name
        # Re-run with the new filter if there's an active query.
        if self.search.text().strip():
            self._on_search()

    def _on_results(self, gen: int, filter_: str, items: list) -> None:
        if gen != self._search_gen:
            return   # straggler from an abandoned query
        # Filter could have changed since this query started — discard stale.
        # (Still needed alongside the gen: switching filters with an empty
        # search box doesn't start a new search, so it doesn't bump the gen.)
        if filter_ != self._search_filter:
            return
        if not items:
            self.heading.setText(self._line_heading("no results"))
            self.statusBar().showMessage("no results")
            return
        self.heading.setText(self._line_heading(f"results · {len(items)}"))
        self.statusBar().showMessage(f"{len(items)} results")

        is_cards = filter_ in ("albums", "artists")
        self.results.setVisible(not is_cards)
        self._results_card_scroll.setVisible(is_cards)

        if not is_cards:
            marker = self._list_marker()
            federated = (
                getattr(self, "_settings", None) is not None
                and bool(self._settings.federated_search)
                and filter_ == "songs"
            )
            reg = source_registry()
            for tr in items:
                artist = theming.styled_case(tr.artists or "")
                title = theming.styled_case(tr.title or "")
                dur = tr.duration or ""
                tag = ""
                if federated:
                    src = reg.get(getattr(tr, "source", "") or "")
                    if src is not None and src.short_tag:
                        tag = f"[{src.short_tag}] "
                label = f"{marker}{tag}{artist} — {title}"
                if dur:
                    gap = max(2, 60 - len(label) - len(dur))
                    label = f"{label}{' ' * gap}{dur}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, tr)
                self.results.addItem(item)
            n = int(getattr(getattr(self, "_settings", None),
                            "prefetch_warm_results", 3) or 0)
            if n > 0:
                try:
                    self._prefetch.warm(items, limit=n)
                except Exception:
                    pass
            return

        # Cards (albums or artists).
        from .card import Card
        for entry in items:
            if filter_ == "albums":
                c = Card(entry.title, entry.artists, entry.thumbnail, entry)
                c.clicked.connect(self._open_album_entry)
            else:
                c = Card(entry.name, "artist", entry.thumbnail, entry, circular=True)
                c.clicked.connect(self._open_artist_entry)
            self.results_cards.add_card(c)

    def _on_search_failed(self, gen: int, msg: str) -> None:
        if gen != self._search_gen:
            return   # a newer search is running/done — don't clobber it
        self.heading.setText(self._line_heading("search failed"))
        self.statusBar().showMessage(f"search failed: {msg}")

    # ---------- result interactions ----------

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            self._play_now(tr, seed_radio=self._auto_radio_on_play)

    def _on_results_menu(self, pos) -> None:
        item = self.results.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.results)
        a_play = QAction("play now", menu)
        a_next = QAction("play next", menu)
        a_add  = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        a_artist = QAction("view artist", menu)
        for a in (a_play, a_next, a_add, a_radio, a_artist):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self._play_now(tr, seed_radio=False))
        a_next.triggered.connect(lambda: self._queue_next(tr))
        a_add.triggered.connect(lambda: self._queue_add(tr))
        a_radio.triggered.connect(lambda: self._start_radio(tr))
        a_artist.triggered.connect(lambda: self._open_artist_by_name(tr.artists))
        menu.exec(self.results.viewport().mapToGlobal(pos))

    # ---------- queue interactions ----------

    def _on_queue_double(self, index) -> None:
        if not index.isValid():
            return
        self._play_index(index.row())

    def _on_queue_menu(self, pos) -> None:
        idx = self.queue_view.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        tr: api.Track | None = self.queue.data(idx, Role.Track)
        if not tr:
            return
        menu = QMenu(self.queue_view)
        a_play = QAction("play now", menu)
        a_radio = QAction("start radio from here", menu)
        a_remove = QAction("remove", menu)
        for a in (a_play, a_radio, a_remove):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self._play_index(row))
        a_radio.triggered.connect(lambda: self._start_radio(tr))
        a_remove.triggered.connect(lambda: self.queue.remove(row))
        menu.exec(self.queue_view.viewport().mapToGlobal(pos))

    def _on_radio_toggle(self) -> None:
        if self.queue.radio_enabled:
            self.queue.disable_radio()
        else:
            seed = self._current.video_id if self._current else None
            if not seed and self.queue.current:
                seed = self.queue.current.video_id
            self.queue.enable_radio(seed)

    # ---------- queue actions ----------

    def _play_now(self, track: api.Track, seed_radio: bool = False) -> None:
        # Replace queue with just this track, set current, play it. If
        # seed_radio is true, also turn radio on so the queue refills.
        self.queue.blockSignals(True)
        self.queue.clear()
        self.queue.blockSignals(False)
        self.queue.add(track)
        self.queue.set_current(0)
        if seed_radio:
            self.queue.enable_radio(track.video_id)
        self._play_track(track)

    def _queue_add(self, track: api.Track) -> None:
        self.queue.add(track)
        self.statusBar().showMessage(f"added to queue · {self.queue.upcoming_count} upcoming")
        if self.queue.current is None:
            self.queue.set_current(self.queue.rowCount() - 1)
            self._play_track(track)

    def _queue_next(self, track: api.Track) -> None:
        self.queue.add_next(track)
        self.statusBar().showMessage(f"queued next · {self.queue.upcoming_count} upcoming")
        if self.queue.current is None:
            self.queue.set_current(0)
            self._play_track(self.queue.current)

    def _start_radio(self, track: api.Track) -> None:
        self._play_now(track, seed_radio=True)
        self.statusBar().showMessage("radio started")

    def _toggle_visualizer_fullscreen(self) -> None:
        # F11 only meaningful when the visualizer is the active view.
        if self.stack.currentIndex() == 8:
            self.visualizer_view._toggle_fullscreen()

    # ---------- discovery navigation ----------

    def _open_album_entry(self, entry: api.AlbumEntry) -> None:
        if not entry:
            return
        self.album_view.open_album(entry.browse_id, title_hint=entry.title, thumbnail_hint=entry.thumbnail)
        self._push_view(6)

    def _open_album_browse_id(self, browse_id: str) -> None:
        if not browse_id:
            return
        self.album_view.open_album(browse_id)
        self._push_view(6)

    def _open_artist_entry(self, entry: api.ArtistEntry) -> None:
        if not entry:
            return
        self.artist_view.open_artist(entry.channel_id, name_hint=entry.name, thumbnail_hint=entry.thumbnail)
        self._push_view(7)

    def _open_artist_by_id(self, channel_id: str) -> None:
        if not channel_id:
            return
        self.artist_view.open_artist(channel_id)
        self._push_view(7)

    def _open_artist_by_name(self, name: str) -> None:
        """Album view fires this with an artist name string. Look up the top
        match in the background and open the first hit."""
        if not name:
            return
        from PySide6.QtCore import QObject as _QO, QThread as _QT, Signal as _QSig
        class _LookupWorker(_QO):
            found = _QSig(object)
            failed = _QSig(str)
            def __init__(self, api_obj, name):
                super().__init__()
                self.api = api_obj
                self.name = name
            def run(self):
                try:
                    arts = self.api.search_artists(self.name, limit=1)
                    self.found.emit(arts[0] if arts else None)
                except Exception as e:
                    self.failed.emit(str(e))
        thread = _QT()
        worker = _LookupWorker(self.api, name)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        def on_found(entry):
            if entry is not None:
                self._open_artist_entry(entry)
            else:
                self.statusBar().showMessage(f"no artist found for {name!r}")
        worker.found.connect(on_found)
        worker.failed.connect(lambda m: self.statusBar().showMessage(f"artist lookup failed: {m}"))
        worker.found.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        # Hold refs so they don't GC.
        self._artist_lookup_thread = thread
        self._artist_lookup_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _open_playlist_entry(self, entry: api.PlaylistEntry) -> None:
        # Hand off to the library view's detail page, then switch to it.
        if not entry:
            return
        self.library_view.open_playlist(entry)
        # Keep library nav highlighted; LibraryView handles internal detail.
        self.stack.setCurrentIndex(1)

    def _play_all(self, tracks: list[api.Track]) -> None:
        """Replace queue with `tracks`, start the first one. No radio seed —
        the playlist itself is the timeline."""
        if not tracks:
            return
        # Rebuild queue from scratch.
        self.queue.disable_radio()
        self.queue.blockSignals(True)
        self.queue.clear()
        self.queue.blockSignals(False)
        self.queue.add_many(tracks)
        first = self.queue.set_current(0)
        if first:
            self._play_track(first)
        self.statusBar().showMessage(f"playing {len(tracks)} tracks")

    def _play_index(self, row: int) -> None:
        tr = self.queue.set_current(row)
        if tr:
            self._play_track(tr)

    # ---------- engine ----------

    def _play_track(self, track: api.Track) -> None:
        if track is None:
            return
        # Remember what we're leaving so [prev] can come back to it even when
        # the queue got replaced underneath us. Skipped while navigating back
        # (that would just re-push what we popped) and while restoring a
        # session (nothing was actually "played" yet).
        if (self._current is not None
                and not self._navigating_back
                and not self._restoring_session
                and self._current.video_id != track.video_id):
            self._push_play_history(self._current)
        # Any new play supersedes a pending in-flight join.
        self._awaiting_prefetch_vid = None
        self._await_fallback_timer.stop()
        self._perf_t0 = time.monotonic()
        self._perf_vid = track.video_id
        self._perf_path = "cold"
        self._perf_resolve_ms = None
        # Stop the previous track immediately. Otherwise its audio keeps
        # playing for the 1–3s the resolve worker takes, which feels broken
        # on a manual skip.
        self.player.stop()
        # If a karaoke swap is active and the user picked an unrelated
        # track (not the swap target, not the cached vocal), clear the
        # swap state so the mute-lyrics button doesn't get stuck on a
        # song it's not swapped from. _swapping_now guards the play
        # calls coming FROM the swap orchestration itself.
        if (self._instrumental_active
                and self._instrumental_vocal_track is not None
                and not getattr(self, "_swapping_now", False)):
            vocal_id = self._instrumental_vocal_track.video_id
            if track.video_id != vocal_id:
                self._instrumental_active = False
                self._instrumental_vocal_track = None
                try:
                    self.lyrics_view.mute_btn.setChecked(False)
                    self.lyrics_view.swap_status.setText("")
                except Exception:
                    pass
        self._current = track
        self.now_label.setTrackAnimated(track.artists, track.title, track.album)
        self.now_label.setStatus("loading")
        self.progress.reset()
        self.time_label.setText("0:00 / 0:00")
        style = getattr(self._settings, "loading_indicator_style", "blocks") \
            if hasattr(self, "_settings") and self._settings is not None else "blocks"
        self._loading.set_style(style)
        self._loading.start("resolving stream")
        self._fetch_art(track)
        # Log to history. Skip if we're restoring a session — that's a resume,
        # not a fresh play.
        if not self._restoring_session:
            try:
                history_module.append(track)
            except Exception:
                pass

        # The next-track prefetch armer is keyed on what's playing; new track
        # → fresh window to arm for whatever comes after it.
        self._prefetch_armed_for = None

        # Cache hit? Skip the resolve worker entirely — load straight into
        # the player. This is the happy path for "user pressed next while the
        # current track was nearly done."
        cached = self._prefetch.lookup(track.video_id)
        if cached is not None:
            self._perf_path = "prefetch-hit"
            self._on_resolved(track.video_id, cached)
            return

        if self._prefetch.is_inflight(track.video_id):
            # A hover/press/warm prefetch is already resolving this exact
            # track — join it instead of racing a duplicate yt-dlp round
            # trip. prefetch.resolved/failed land on the GUI thread and
            # continue in _on_prefetch_join_*.
            self._perf_path = "inflight-join"
            self._awaiting_prefetch_vid = track.video_id
            self._await_fallback_timer.start()
            return

        self._spawn_resolve_worker(track)

    def _spawn_resolve_worker(self, track: api.Track) -> None:
        thread = QThread()
        worker = _ResolveWorker(track)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resolved.connect(self._on_resolved)
        worker.failed.connect(self._on_resolve_failed)
        worker.resolved.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._resolve_thread = thread
        self._resolve_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    # ---------- in-flight prefetch join ----------

    def _on_prefetch_join_resolved(self, video_id: str) -> None:
        if video_id != self._awaiting_prefetch_vid:
            return
        self._awaiting_prefetch_vid = None
        self._await_fallback_timer.stop()
        if not self._current or self._current.video_id != video_id:
            return
        ref = self._prefetch.lookup(video_id)
        if ref is not None:
            self._on_resolved(video_id, ref)
        else:
            # Resolved but already expired/invalidated (edge) — do it ourselves.
            self._spawn_resolve_worker(self._current)

    def _on_prefetch_join_failed(self, video_id: str, _msg: str) -> None:
        if video_id != self._awaiting_prefetch_vid:
            return
        self._awaiting_prefetch_vid = None
        self._await_fallback_timer.stop()
        if self._current and self._current.video_id == video_id:
            # One explicit retry — parity with the duplicate worker the old
            # code effectively raced here. A second failure surfaces through
            # _on_resolve_failed as before.
            self._spawn_resolve_worker(self._current)

    def _on_prefetch_join_timeout(self) -> None:
        vid = self._awaiting_prefetch_vid
        self._awaiting_prefetch_vid = None
        if vid and self._current and self._current.video_id == vid:
            self._spawn_resolve_worker(self._current)

    def _on_resolved(self, video_id: str, ref: object) -> None:
        if not self._current or self._current.video_id != video_id:
            return
        if self._perf_t0 is not None and self._perf_vid == video_id:
            self._perf_resolve_ms = (time.monotonic() - self._perf_t0) * 1000.0
        if isinstance(ref, StreamRef):
            self.player.load_ref(ref)
        else:
            # Defensive: handle a bare URL if some path still emits one.
            self.player.load_url(str(ref))
        self.now_label.setStatus("")
        # Resolve done — switch the indicator's leading text. PLAYING state
        # (which fires when mpv actually starts audio) finishes the indicator.
        self._loading.update_message("buffering")
        self.play_btn.setEnabled(True)
        # Like only when the track's source supports rating. Same for radio.
        cur_source = source_registry().get(self._current.source or "ytmusic") if self._current else None
        can_like = bool(cur_source and cur_source.supports("rating"))
        can_radio = bool(cur_source and cur_source.supports("radio"))
        self.like_btn.setEnabled(can_like)
        if self._mini is not None:
            self._mini.set_like_enabled(can_like)
        self.radio_btn.setEnabled(can_radio)
        # Best-effort like-state lookup in the background; UI defaults to ♡.
        self._liked_current = False
        self._refresh_like_button()
        self._refresh_nav_buttons()
        # If lyrics is the active view, refresh it for the new track.
        if self.stack.currentIndex() == 3:
            self.lyrics_view.show_for(self._current)

    def _on_resolve_failed(self, video_id: str, msg: str) -> None:
        self._loading.cancel()
        self.statusBar().showMessage(f"couldn't resolve: {msg}")
        self.now_label.setStatus("error")
        from .toast import show_toast
        show_toast(self.toast_host(), f"couldn't get audio · {msg[:80]}")

    def _on_play_clicked(self) -> None:
        self.player.toggle()

    def _on_like_clicked(self) -> None:
        if not self._current:
            return
        target = not self._liked_current
        # Optimistic UI flip.
        self._liked_current = target
        self._refresh_like_button()
        self.like_btn.setEnabled(False)
        if self._mini is not None:
            self._mini.set_like_enabled(False)

        thread = QThread()
        worker = _RateWorker(self.api, self._current.video_id, target)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_rate_done)
        worker.failed.connect(self._on_rate_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._rate_thread = thread
        self._rate_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _on_rate_done(self, video_id: str, liked: bool) -> None:
        if self._current and self._current.video_id == video_id:
            self.statusBar().showMessage("liked" if liked else "removed like")
            self.like_btn.setEnabled(True)
            if self._mini is not None:
                self._mini.set_like_enabled(True)

    def _on_rate_failed(self, video_id: str, msg: str) -> None:
        # Revert optimistic flip.
        if self._current and self._current.video_id == video_id:
            self._liked_current = not self._liked_current
            self._refresh_like_button()
            self.like_btn.setEnabled(True)
            if self._mini is not None:
                self._mini.set_like_enabled(True)
        self.statusBar().showMessage(f"couldn't update like: {msg}")

    def _refresh_like_button(self) -> None:
        glyph = "♥" if self._liked_current else "♡"
        self.like_btn.setLabel(glyph)
        self.like_btn.setGlyph(glyph)
        if self._mini is not None:
            self._mini.set_liked(self._liked_current)

    def _on_next_clicked(self) -> None:
        tr = self.queue.advance()
        if tr:
            self._play_track(tr)

    def _on_prev_clicked(self) -> None:
        # If we're more than 3s into the song, restart it. Else go back.
        if self.player.duration > 0 and self._last_position > 3:
            self.player.seek(0)
            # Reset eagerly instead of waiting for mpv's next position tick.
            # Without this, a quick second press re-read the stale pre-seek
            # position and restarted the track again instead of going back.
            self._last_position = 0.0
            self.progress.setPosition(0)
            return
        tr = self.queue.back()
        if tr:
            self._play_track(tr)
            return
        # Nothing before us *in the queue* — but "play now" replaces the queue
        # wholesale, so picking tracks one at a time leaves current at row 0
        # forever and queue.back() never has anywhere to go. Fall back to the
        # cross-queue play history, which is what "previous track" means to
        # anyone who isn't thinking about tide's queue model.
        prev = self._pop_play_history()
        if prev is not None:
            self._play_from_history(prev)

    # ---------- cross-queue play history ----------

    def _push_play_history(self, track: api.Track) -> None:
        if track is None:
            return
        if self._play_history and self._play_history[-1].video_id == track.video_id:
            return
        self._play_history.append(track)
        del self._play_history[:-self.PLAY_HISTORY_MAX]

    def _pop_play_history(self) -> api.Track | None:
        return self._play_history.pop() if self._play_history else None

    def _play_from_history(self, track: api.Track) -> None:
        """Play a track pulled off the history stack without re-recording it
        (that would make prev/next ping-pong between two songs forever)."""
        # Keep the queue's idea of "current" in step, or the queue view stays
        # highlighting the track we just left and up-next reads wrong.
        row = next(
            (i for i, t in enumerate(self.queue.tracks) if t.video_id == track.video_id),
            -1,
        )
        if row < 0:
            self.queue.add_prev(track)
            row = max(0, self.queue.current_index - 1)
        self._navigating_back = True
        try:
            self.queue.set_current(row)
            self._play_track(track)
        finally:
            self._navigating_back = False

    def _refresh_nav_buttons(self) -> None:
        self.next_btn.setEnabled(self.queue.can_advance() or self.queue.radio_enabled)
        self.prev_btn.setEnabled(
            self.queue.can_go_back()
            or bool(self._play_history)
            or self.player.duration > 0
        )
        if self._mini is not None:
            self._mini.set_nav_enabled(self.prev_btn.isEnabled(),
                                       self.next_btn.isEnabled())

    # ---------- queue / radio plumbing ----------

    def _wire_queue(self) -> None:
        self.queue.current_changed.connect(self._on_queue_current_changed)
        self.queue.current_removed.connect(self._on_queue_current_removed)
        self.queue.refill_requested.connect(self._on_radio_refill_requested)
        self.queue.radio_state_changed.connect(self._on_radio_state_changed)
        self.queue.rowsInserted.connect(self._on_queue_size_changed)
        self.queue.rowsRemoved.connect(self._on_queue_size_changed)
        self.queue.modelReset.connect(lambda: self._on_queue_size_changed(None, 0, 0))
        # Persist session on any meaningful queue change.
        self.queue.current_changed.connect(lambda _t: self._schedule_session_save())
        self.queue.rowsInserted.connect(lambda *_a: self._schedule_session_save())
        self.queue.rowsRemoved.connect(lambda *_a: self._schedule_session_save())
        self.queue.modelReset.connect(self._schedule_session_save)
        self.queue.radio_state_changed.connect(lambda _e: self._schedule_session_save())

    def _on_queue_size_changed(self, *_args) -> None:
        self.queue_heading.setText(
            self._line_heading(f"queue · {self.queue.rowCount()}")
        )
        self._refresh_nav_buttons()
        self._refresh_up_next()

    def _on_queue_current_removed(self, track) -> None:
        """The playing row was removed from the queue. The model already
        moved the current pointer; here we reconcile the *audio*. If a track
        took the removed slot, skip to it (only when the removed row was the
        one actually playing — otherwise a stopped/paused queue shouldn't
        spontaneously start). If nothing shifted in, stop."""
        was_playing = self.player.state in (PlayState.PLAYING, PlayState.LOADING, PlayState.PAUSED)
        if track is not None:
            if was_playing:
                self._play_track(track)
        else:
            # Nothing to advance to — halt playback and drop the now-stale
            # now-playing track ref.
            try:
                self.player.stop()
            except Exception:
                pass
            self._current = None

    def _on_queue_current_changed(self, _track) -> None:
        self._refresh_nav_buttons()
        self._refresh_up_next()
        # Neighborhood prefetch — keep the next two and the previous one
        # warm on every queue transition so back/next/queue-jump feel
        # instant. The position-tick prefetch only arms one track at a
        # time, near end-of-current; this catches the "user jumps to a
        # random queue slot mid-song" case the tick-based logic misses.
        self._arm_neighborhood_prefetch()

    # ---------- instrumental swap ("mute lyrics") ----------

    def _on_instrumental_swap_requested(self, vocal_track, want_instrumental: bool) -> None:
        """Driven by the Lyrics view's [mute lyrics] toggle. When
        switching ON, we kick a background instrumental hunt; on
        success the player swaps stream + restores position. When
        switching OFF, we restore the previously-cached vocal track."""
        if want_instrumental:
            if vocal_track is None:
                self.lyrics_view.mute_btn.setChecked(False)
                return
            # Remember where to come back to.
            self._instrumental_vocal_track = vocal_track
            try:
                self._instrumental_swap_position = float(self.player.position if hasattr(self.player, "position") else 0.0)
            except Exception:
                self._instrumental_swap_position = 0.0
            self._spawn_instrumental_search(vocal_track)
            return
        # Toggling OFF — switch back to the vocal version we cached
        # when the swap was triggered.
        if not self._instrumental_active or self._instrumental_vocal_track is None:
            self.lyrics_view.swap_status.setText("")
            return
        vocal = self._instrumental_vocal_track
        # Capture current position so the swap-back lands where the
        # user was singing (instead of restarting the vocal track).
        try:
            current_pos = float(self.player.position if hasattr(self.player, "position") else 0.0)
        except Exception:
            current_pos = self._instrumental_swap_position
        self._instrumental_active = False
        self._instrumental_vocal_track = None
        self.lyrics_view.swap_status.setText("")
        self._instrumental_swap_position = current_pos
        # Reuse the standard play path; once it loads, _seek_after_load
        # snaps to the saved position.
        self._play_track_then_seek(vocal, current_pos)

    def _spawn_instrumental_search(self, vocal_track) -> None:
        # Cancel any in-flight hunt.
        old = self._instrumental_swap_thread
        if old is not None:
            try:
                old.quit()
            except Exception:
                pass
        thread = QThread()
        worker = _InstrumentalSearchWorker(vocal_track)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_instrumental_found)
        worker.failed.connect(self._on_instrumental_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._instrumental_swap_thread = thread
        self._instrumental_swap_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _on_instrumental_found(self, vocal_track, match) -> None:
        # Sanity check — did the user toggle off / change tracks while
        # the search was running?
        if (vocal_track is None
                or self._current is None
                or vocal_track.video_id != self._current.video_id
                or not self.lyrics_view.mute_btn.isChecked()):
            return
        if match is None:
            self.lyrics_view.mute_btn.setChecked(False)
            self.lyrics_view.swap_status.setText(theming.styled_case(
                "no instrumental version found across enabled sources"
            ))
            self._instrumental_vocal_track = None
            return
        self._instrumental_active = True
        self.lyrics_view.swap_status.setText(theming.styled_case(
            f"playing instrumental · source: {match.source_name}"
        ))
        # Swap. The instrumental's track carries its own source slug, so
        # the existing playback router picks the right backend without
        # any special-casing here.
        self._play_track_then_seek(match.track, self._instrumental_swap_position)

    def _on_instrumental_failed(self, vocal_track, msg: str) -> None:
        if vocal_track is None or self._current is None:
            return
        if vocal_track.video_id != self._current.video_id:
            return
        self.lyrics_view.mute_btn.setChecked(False)
        self.lyrics_view.swap_status.setText(theming.styled_case(
            f"instrumental search failed: {msg}"
        ))
        self._instrumental_vocal_track = None

    def _play_track_then_seek(self, track, seek_secs: float) -> None:
        """Play ``track`` and snap to ``seek_secs`` the moment the
        stream is live. Used by the karaoke swap so the swap-in / swap-
        out feel like a crossfade-in-place rather than a track restart.
        """
        self._pending_seek = max(0.0, float(seek_secs or 0.0))
        # Tell _play_track this is a swap dispatch, not a user pick,
        # so it doesn't clear the karaoke swap state during the
        # transition.
        self._swapping_now = True
        try:
            self._play_track(track)
        finally:
            self._swapping_now = False
        # The seek itself fires in _on_state_changed when state goes to
        # PLAYING — see _maybe_apply_pending_seek.

    def _maybe_apply_pending_seek(self, state) -> None:
        """Companion to _play_track_then_seek — fires on first PLAYING
        after the seek was requested, then clears the pending value."""
        from ..player import PlayState
        if getattr(self, "_pending_seek", 0.0) <= 0.0:
            return
        if state != PlayState.PLAYING:
            return
        try:
            self.player.seek(float(self._pending_seek))
        except Exception:
            pass
        self._pending_seek = 0.0

    def _wire_hover_prefetch(self) -> None:
        """Hover-prefetch every track-bearing view in the app. Mouseover
        a row in any of these → its URL resolves in the background after
        a 300ms debounce, so the next click is a cache hit."""
        views: list = [self.results, self.queue_view]
        for child_view, attr in (
            (getattr(self, "library_view", None), "tracks_list"),
            (getattr(self, "history_view", None), "list"),
            (getattr(self, "album_view", None), "tracks"),
            (getattr(self, "artist_view", None), "songs"),
        ):
            if child_view is None:
                continue
            v = getattr(child_view, attr, None)
            if v is not None:
                views.append(v)
        for v in views:
            try:
                self._prefetch.attach_hover(v)
                self._prefetch.attach_press(v)
            except Exception:
                pass

    def _arm_neighborhood_prefetch(self) -> None:
        idx = self.queue.current_index
        tracks = self.queue.tracks
        seen: set[str] = set()
        for offset in (1, 2, -1):
            i = idx + offset
            if i < 0 or i >= len(tracks):
                continue
            tr = tracks[i]
            if not tr or not tr.video_id or tr.video_id in seen:
                continue
            seen.add(tr.video_id)
            try:
                self._prefetch.request(tr)
            except Exception:
                pass

    def _refresh_up_next(self) -> None:
        nxt_idx = self.queue.current_index + 1
        if 0 < nxt_idx < self.queue.rowCount():
            tr = self.queue.tracks[nxt_idx]
            artist = theming.styled_case(tr.artists or "")
            title = theming.styled_case(tr.title or "")
            self.up_next.setText(f"{theming.styled_case('next')}:  {artist} — {title}")
            self.up_next.setVisible(True)
        elif self.queue.radio_enabled:
            self.up_next.setText(theming.styled_case("next:  radio"))
            self.up_next.setVisible(True)
        else:
            self.up_next.setVisible(False)

    def _on_radio_state_changed(self, enabled: bool) -> None:
        self.radio_btn.setLabel("radio: on" if enabled else "radio: off")
        self._refresh_nav_buttons()
        self._refresh_up_next()

    def _on_radio_refill_requested(self, seed_video_id: str, exclude: list) -> None:
        # Sources without the "radio" capability can't refill — most
        # commonly Spotify in Dev Mode, whose recommendations + artist-
        # top-tracks endpoints were locked behind Extended Quota in Feb
        # 2026. Skip silently here so the queue doesn't enter a tight
        # 403-loop trying to refill from an unsupported source.
        if not self.api.supports("radio"):
            self.queue.disable_radio()
            return
        thread = QThread()
        worker = _RadioWorker(self.api, seed_video_id, list(exclude))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_radio_done)
        worker.failed.connect(self._on_radio_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._radio_thread = thread
        self._radio_worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _on_radio_done(self, tracks: list) -> None:
        added = self.queue.absorb_radio(tracks)
        if added:
            self.statusBar().showMessage(f"radio added {added} tracks")

    def _on_radio_failed(self, msg: str) -> None:
        self.queue.absorb_radio([])
        self.statusBar().showMessage(f"radio refill failed: {msg}")

    # ---------- album art ----------

    def _fetch_art(self, track: api.Track) -> None:
        """Stale-tolerant art fetch.

        We don't try to manage the lifecycle of in-flight QNetworkReplies —
        they're owned by Qt and get deleted as soon as they finish. Instead
        we track `_art_for_video_id` and discard any reply that doesn't match
        the currently-playing track when it finishes.

        We deliberately do NOT clear the existing art at the top: keeping
        the prior cover visible until the new one lands gives AlbumArt's
        crossfade something to fade from. Only when the new track has no
        thumbnail at all do we wipe to the empty state.
        """
        from .art_cache import (
            ART_TRANSFER_TIMEOUT_MS,
            MAX_ART_BYTES,
            _is_fetchable_art_url,
        )

        # Same guard as the shared art cache: a remote-supplied thumbnail
        # URL is untrusted, so reject non-http(s) (file:// / data:) before it
        # reaches QNAM, and treat a bad URL as "no art".
        if not track.thumbnail or not _is_fetchable_art_url(track.thumbnail):
            self.art.setImage(None)
            self._art_for_video_id = None
            return
        self._art_for_video_id = track.video_id

        req = QNetworkRequest(QUrl(track.thumbnail))
        req.setTransferTimeout(ART_TRANSFER_TIMEOUT_MS)
        reply = self._net.get(req)
        target_video_id = track.video_id

        def on_progress(received: int, _total: int) -> None:
            if received > MAX_ART_BYTES:
                reply.abort()

        def on_finished():
            try:
                err = reply.error()
            except RuntimeError:
                return  # reply was already deleted
            if err != QNetworkReply.NoError:
                reply.deleteLater()
                return
            data = bytes(reply.readAll().data())
            reply.deleteLater()
            if self._art_for_video_id != target_video_id:
                return
            if len(data) > MAX_ART_BYTES:
                return
            img = QImage()
            if img.loadFromData(data):
                self.art.setImage(img)

        reply.downloadProgress.connect(on_progress)
        reply.finished.connect(on_finished)

    # ---------- player state ----------

    def _wire_player(self) -> None:
        self.player.state_changed.connect(self._on_state)
        self.player.position_changed.connect(self._on_position)
        self.player.duration_changed.connect(self._on_duration)
        self.player.ended.connect(self._on_track_ended)
        self.player.error.connect(self._on_player_error)

    def _on_state(self, s: PlayState) -> None:
        # First chance to apply a pending karaoke-swap seek — the player
        # ignores seek() in LOADING because there's no demuxer yet, so
        # we wait for PLAYING.
        self._maybe_apply_pending_seek(s)
        if s == PlayState.PLAYING:
            self.play_btn.setLabel("pause")
            self.play_btn.setGlyph("⏸")
            # Audio actually started — stop the loading indicator.
            self._loading.finish("playing")
            # One summary line per play (not per pause/resume — t0 clears).
            # This is the ground truth for tuning instant-play behavior.
            if self._perf_t0 is not None:
                total_ms = (time.monotonic() - self._perf_t0) * 1000.0
                resolve = (
                    f"{self._perf_resolve_ms:.0f}"
                    if self._perf_resolve_ms is not None else "-"
                )
                audio_ms = total_ms - (self._perf_resolve_ms or 0.0)
                print(
                    f"tide: play {self._perf_vid} path={self._perf_path} "
                    f"resolve={resolve}ms audio={audio_ms:.0f}ms "
                    f"total={total_ms:.0f}ms",
                    file=sys.stderr,
                )
                self._perf_t0 = None
        elif s == PlayState.PAUSED:
            self.play_btn.setLabel("play")
            self.play_btn.setGlyph("▶")
        elif s == PlayState.LOADING:
            self.play_btn.setLabel("…")
            self.play_btn.setGlyph("…")
        else:
            self.play_btn.setLabel("play")
            self.play_btn.setGlyph("▶")

    def _on_position(self, secs: float) -> None:
        self._last_position = secs
        self.progress.setPosition(secs)
        self._update_time_label(secs, self.player.duration)
        # Throttle the heavier side-effects: lyrics line lookup + session
        # dirty marking don't need to run at mpv's ~60Hz position rate.
        last = getattr(self, "_last_heavy_position", -10.0)
        if abs(secs - last) >= 0.25:
            self._last_heavy_position = secs
            try:
                self.lyrics_view.update_position(secs)
            except Exception:
                pass
            if not self._restoring_session:
                self._session_dirty = True
                if not self._session_save_timer.isActive():
                    self._session_save_timer.start()
        # Prefetch the next track once the tail of the current one is within
        # the lead window. Idempotent (StreamPrefetch.request dedupes), but
        # the armed-for guard keeps us from hitting it every position tick.
        self._maybe_arm_prefetch(secs)

    def _maybe_arm_prefetch(self, position_secs: float) -> None:
        duration = float(self.player.duration or 0.0)
        if duration <= 0.0:
            return
        if duration - position_secs > self._prefetch_lead_secs:
            return
        nxt = self.queue.peek_next()
        if nxt is None or not nxt.video_id:
            return
        if self._prefetch_armed_for == nxt.video_id:
            return
        self._prefetch_armed_for = nxt.video_id
        self._prefetch.request(nxt)

    def _on_duration(self, secs: float) -> None:
        self.progress.setDuration(secs)
        self._update_time_label(0.0, secs)

    def _update_time_label(self, pos: float, dur: float) -> None:
        self.time_label.setText(f"{_mmss(pos)} / {_mmss(dur)}")

    def _on_track_ended(self) -> None:
        from .sleep_timer import SleepMode
        if self._sleep_mode == SleepMode.AFTER_SONG:
            self.player.pause()
            self._sleep_cancel(silent=True)
            self.statusBar().showMessage("sleep: paused after current song")
            return
        tr = self.queue.advance()
        if tr:
            self._play_track(tr)
        else:
            self.now_label.setStatus("queue empty")
            self.statusBar().showMessage("queue empty")
            if self._sleep_mode == SleepMode.AFTER_QUEUE:
                self._sleep_cancel(silent=True)
                self.statusBar().showMessage("sleep: paused (queue ended)")

    def _on_player_error(self, msg: str) -> None:
        self._loading.cancel()
        # The failing URL may have come from the prefetch cache (stale CDN
        # signature, expired session). Drop it so retry/next-attempt does a
        # fresh resolve instead of replaying the same dead URL until TTL.
        cur = self._current
        if cur is not None and getattr(cur, "video_id", ""):
            self._prefetch.invalidate(cur.video_id)
        self.statusBar().showMessage(f"player error: {msg}")

    # ---------- theme + shortcuts ----------

    def _apply_window_translucency(self, theme) -> None:
        """Honor a theme's `[layout] window_translucent` flag (seaglass).

        The attribute only takes effect on map, so a live theme switch that
        flips it needs one hide/show — visually covered by the restyle that
        lands the same instant. No-ops when the flag already matches."""
        try:
            want = bool(theme.t("layout", "window_translucent", False))
        except Exception:
            want = False
        if want == self.testAttribute(Qt.WA_TranslucentBackground):
            return
        self.setAttribute(Qt.WA_TranslucentBackground, want)
        if self.isVisible():
            self.hide()
            self.show()

    def _on_theme_changed(self, theme) -> None:
        prior = self._theme
        self._theme = theme
        self._apply_window_translucency(theme)
        self.heading.setText(self._line_heading("results"))
        self.queue_heading.setText(self._line_heading(f"queue · {self.queue.rowCount()}"))
        # If the theme's aesthetic flipped (brutalist ↔ modern), stale slot
        # overrides from the previous aesthetic should reset to the new
        # theme's [slots] prefs so e.g. a "blocks" progress bar from
        # brutalist-mono doesn't leak into ambient. Only run on actual slug
        # changes (theming.override_tokens re-emits theme_changed too).
        try:
            new_slug = getattr(theme, "slug", None)
            old_slug = getattr(prior, "slug", None)
            if new_slug and new_slug != old_slug:
                self._maybe_apply_theme_slot_prefs(theme, prior)
        except Exception:
            pass

    def _maybe_apply_theme_slot_prefs(self, new_theme, prior_theme) -> None:
        new_aes = getattr(new_theme, "aesthetic", None)
        old_aes = getattr(prior_theme, "aesthetic", None) if prior_theme is not None else None
        slot_prefs = getattr(new_theme, "slots", None) or {}
        if not slot_prefs:
            return
        settings = getattr(self, "_settings", None)
        # On aesthetic FLIP (or first-ever apply), reset user overrides to
        # the new theme's prefs entirely — anything the user picked on the
        # prior aesthetic almost certainly doesn't translate.
        # On same-aesthetic theme swap, keep user overrides (their picks
        # still fit the new theme's vibe).
        if old_aes is not None and new_aes == old_aes:
            return
        if settings is not None:
            settings.layout_overrides = dict(slot_prefs)
            try:
                from .. import settings as settings_module
                settings_module.save(settings)
            except Exception:
                pass
        # Push to the layout manager + re-build the strip so new variants
        # take effect immediately.
        try:
            effective = layout_module.manager().update_overrides(dict(slot_prefs))
            if effective is not None and hasattr(self, "apply_layout"):
                self.apply_layout(effective)
        except Exception:
            pass

    def _list_marker(self) -> str:
        return str(self._theme.t("layout", "list_marker", "> ")) if self._theme else "> "

    def _wire_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+L"), self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+F"), self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._switch_view("home"))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._switch_view("library"))
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self._switch_view("queue"))
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self._switch_view("lyrics"))
        QShortcut(QKeySequence("Ctrl+5"), self, lambda: self._switch_view("history"))
        QShortcut(QKeySequence("Ctrl+6"), self, lambda: self._switch_view("home"))
        QShortcut(QKeySequence("Ctrl+7"), self, lambda: self._switch_view("visualizer"))
        QShortcut(QKeySequence("Ctrl+8"), self, lambda: self._switch_view("source"))
        QShortcut(QKeySequence("Ctrl+9"), self, lambda: self._switch_view("audio_fx"))
        QShortcut(QKeySequence("F11"), self, self._toggle_visualizer_fullscreen)
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        QShortcut(QKeySequence("Space"), self, self.player.toggle)
        QShortcut(QKeySequence("Ctrl+Right"), self, self._on_next_clicked)
        QShortcut(QKeySequence("Ctrl+Left"), self, self._on_prev_clicked)
        QShortcut(QKeySequence("Ctrl+Up"), self, lambda: self.volume.setVolume(self.volume.volume() + 5))
        QShortcut(QKeySequence("Ctrl+Down"), self, lambda: self.volume.setVolume(self.volume.volume() - 5))
        QShortcut(QKeySequence("Ctrl+H"), self, self._on_like_clicked)
        QShortcut(QKeySequence("Ctrl+M"), self, self.toggle_mini_mode)
        QShortcut(QKeySequence("Ctrl+I"), self, self.open_sleep_timer)
        # Playback speed shortcuts: [ slower, ] faster, \ reset to 1.0×.
        # Mirrors the popover's −/+ and reset; the SpeedButton's set_speed
        # handles clamping + persistence.
        from .speed import SPEED_STEP
        QShortcut(QKeySequence("["), self,
                  lambda: self.speed_btn.set_speed(self.speed_btn.speed() - SPEED_STEP))
        QShortcut(QKeySequence("]"), self,
                  lambda: self.speed_btn.set_speed(self.speed_btn.speed() + SPEED_STEP))
        QShortcut(QKeySequence("\\"), self, self.speed_btn.reset)

    def apply_nav_icons(self, set_name: str) -> None:
        """Update every nav button's icon based on the named set. Called at
        startup (from app.py) and on settings save. The "svg" set renders
        bundled brutalist SVG icons recolored to match the theme; other
        sets render unicode glyphs inline before the label."""
        from . import nav_icons
        for slot, btn in self._nav_buttons.items():
            if set_name == "svg":
                btn.setSvgIcon(nav_icons.svg_text_for(slot))
            else:
                btn.setSvgIcon(None)
                btn.setIconGlyph(nav_icons.icon_for(set_name, slot))

    def _on_volume_changed(self, value: int) -> None:
        self.player.set_volume(value)
        # Persist on every change (cheap — small toml). Falls back gracefully
        # if settings injection didn't happen.
        current = getattr(self, "_settings", None)
        if current is None:
            return
        if current.volume == value:
            return
        current.volume = value
        try:
            from .. import settings as settings_module
            settings_module.save(current)
        except Exception:
            pass

    def apply_initial_volume(self, value: int) -> None:
        """Called once at startup so the widget + mpv start in sync without
        triggering a re-save."""
        self.volume.setVolume(value, emit=False)
        self.player.set_volume(value)

    def _on_speed_changed(self, value: float) -> None:
        # Push to the playback router → mpv. Backends that don't support
        # variable speed (future Librespot/MusicKit) silently no-op.
        self.player.set_speed(value)
        # Persist. Same lazy-save pattern as volume — cheap, gracefully
        # skipped if settings hasn't been attached yet (e.g. mid-startup).
        current = getattr(self, "_settings", None)
        if current is None:
            return
        if abs(current.playback_speed - value) < 1e-4:
            return
        current.playback_speed = float(value)
        try:
            from .. import settings as settings_module
            settings_module.save(current)
        except Exception:
            pass

    def _on_audio_fx_state_changed(self, state) -> None:
        """Fan a state change from either FX widget out to: (a) the
        playback router (which pushes the rebuilt filter chain into mpv),
        (b) the OTHER FX widget so its controls reflect the same state,
        (c) the persisted Settings.audio_fx_state JSON (debounced to
        avoid a TOML write on every EQ-slider tick)."""
        from ..audio_fx import build_filter_chain
        # 1. apply
        try:
            self.player.set_audio_filter_chain(build_filter_chain(state))
        except Exception:
            pass
        # 2. mirror — the two widgets share the dataclass instance, but
        # their bound widgets still need to repaint to reflect mutations
        # the OTHER widget made. blockSignals inside sync_from_state /
        # sync prevents a re-emit loop.
        sender = self.sender()
        if sender is not self.audio_fx_view:
            self.audio_fx_view.sync_from_state()
        if sender is not self.audio_fx_btn:
            self.audio_fx_btn.set_state(state, emit=False)
        else:
            # The button forwards from its popover — refresh its own label
            # in case the master toggled.
            self.audio_fx_btn._refresh_label()
        # 3. persist (debounced).
        self._schedule_audio_fx_save(state)

    def _schedule_audio_fx_save(self, state) -> None:
        # Lazy-init the QTimer so we don't pay the construction cost on
        # every state change. 250 ms debounce — feels instant to the user
        # and means dragging an EQ slider writes the TOML twice instead
        # of 60 times a second.
        from PySide6.QtCore import QTimer as _QT
        timer = getattr(self, "_audio_fx_save_timer", None)
        if timer is None:
            timer = _QT(self)
            timer.setInterval(250)
            timer.setSingleShot(True)
            timer.timeout.connect(self._flush_audio_fx_state)
            self._audio_fx_save_timer = timer
        self._pending_audio_fx_state = state
        timer.start()

    def _flush_audio_fx_state(self) -> None:
        state = getattr(self, "_pending_audio_fx_state", None)
        current = getattr(self, "_settings", None)
        if state is None or current is None:
            return
        try:
            current.audio_fx_state = state.to_json()
        except Exception:
            return
        try:
            from .. import settings as settings_module
            settings_module.save(current)
        except Exception:
            pass

    # ---------- sleep timer ----------

    def open_sleep_timer(self) -> None:
        from .sleep_timer import SleepTimerDialog, SleepMode
        default_minutes = 30
        settings = getattr(self, "_settings", None)
        if settings is not None and getattr(settings, "sleep_preset_minutes", None):
            default_minutes = int(settings.sleep_preset_minutes)
        dlg = SleepTimerDialog(default_minutes=default_minutes,
                               active_mode=self._sleep_mode, parent=self)
        dlg.started.connect(self._sleep_start)
        dlg.cancelled.connect(self._sleep_cancel)
        self._ui_sound("modal_open")
        dlg.exec()
        self._ui_sound("modal_close")

    def _sleep_start(self, mode, minutes: int) -> None:
        from .sleep_timer import SleepMode
        import time as _t
        self._sleep_cancel(silent=True)
        self._sleep_mode = mode
        if mode == SleepMode.MINUTES:
            self._sleep_deadline = _t.time() + minutes * 60
            self._sleep_timer.start()
            self.statusBar().showMessage(f"sleep: pausing in {minutes} min")
            if hasattr(self, "_settings") and self._settings is not None:
                self._settings.sleep_preset_minutes = minutes
                try:
                    from .. import settings as settings_module
                    settings_module.save(self._settings)
                except Exception:
                    pass
        elif mode == SleepMode.AFTER_SONG:
            self.statusBar().showMessage("sleep: will pause after current song")
        elif mode == SleepMode.AFTER_QUEUE:
            self.statusBar().showMessage("sleep: will pause after current queue")

    def _sleep_cancel(self, silent: bool = False) -> None:
        was_active = self._sleep_mode is not None
        self._sleep_mode = None
        self._sleep_deadline = None
        self._sleep_timer.stop()
        if was_active and not silent:
            self.statusBar().showMessage("sleep timer cancelled")

    def _on_sleep_tick(self) -> None:
        if self._sleep_deadline is None:
            return
        import time as _t
        remaining = self._sleep_deadline - _t.time()
        if remaining <= 0:
            self.statusBar().showMessage("sleep: paused")
            self.player.pause()
            self._sleep_cancel(silent=True)
            return
        mins, secs = divmod(int(remaining), 60)
        self.statusBar().showMessage(f"sleep: {mins}:{secs:02d}")

    # ---------- strip layout builders ----------

    def _build_classic_strip_layout(self) -> QHBoxLayout:
        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(2)
        controls_row.addWidget(self.prev_btn)
        controls_row.addWidget(self.play_btn)
        controls_row.addWidget(self.next_btn)
        controls_row.addWidget(self.like_btn)
        controls_row.addStretch(1)
        controls_row.addWidget(self.audio_fx_btn)
        controls_row.addWidget(self.speed_btn)
        controls_row.addWidget(self.volume)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.time_label)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(6)
        right_col.addWidget(self.up_next)
        right_col.addWidget(self.now_label, stretch=1)
        right_col.addLayout(progress_row)
        right_col.addLayout(controls_row)

        strip_layout = QHBoxLayout()
        strip_layout.setContentsMargins(16, 12, 16, 12)
        strip_layout.setSpacing(14)
        strip_layout.addWidget(self.art)
        strip_layout.addLayout(right_col, stretch=1)
        return strip_layout

    def _build_compact_strip_layout(self) -> QVBoxLayout:
        """Vertical phone-style stack — art centered up top, controls below."""
        # Wrap art in an h-box to center horizontally.
        art_row = QHBoxLayout()
        art_row.addStretch(1)
        art_row.addWidget(self.art)
        art_row.addStretch(1)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.time_label)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(4)
        controls_row.addStretch(1)
        controls_row.addWidget(self.prev_btn)
        controls_row.addWidget(self.play_btn)
        controls_row.addWidget(self.next_btn)
        controls_row.addWidget(self.like_btn)
        controls_row.addStretch(1)

        volume_row = QHBoxLayout()
        volume_row.setContentsMargins(0, 0, 0, 0)
        volume_row.addStretch(1)
        volume_row.addWidget(self.audio_fx_btn)
        volume_row.addWidget(self.speed_btn)
        volume_row.addWidget(self.volume)
        volume_row.addStretch(1)

        strip_layout = QVBoxLayout()
        strip_layout.setContentsMargins(28, 24, 28, 24)
        strip_layout.setSpacing(14)
        strip_layout.addStretch(1)
        strip_layout.addLayout(art_row)
        strip_layout.addWidget(self.up_next, alignment=Qt.AlignHCenter)
        strip_layout.addWidget(self.now_label, alignment=Qt.AlignHCenter)
        strip_layout.addLayout(progress_row)
        strip_layout.addLayout(controls_row)
        strip_layout.addLayout(volume_row)
        strip_layout.addStretch(1)
        return strip_layout

    def _rebuild_strip(self, mode: str) -> None:
        """Replace the strip's layout. Re-parents existing widgets so refs survive.

        Widgets to keep:
            self.art, self.up_next, self.now_label, self.progress,
            self.time_label, self.prev_btn, self.play_btn, self.next_btn,
            self.like_btn, self.volume
        """
        keep = [self.art, self.up_next, self.now_label, self.progress,
                self.time_label, self.prev_btn, self.play_btn, self.next_btn,
                self.like_btn, self.volume]
        # Pull our widgets out of the old layout so the layout deletion
        # doesn't take them with it.
        for w in keep:
            if w is None:
                continue
            try:
                w.setParent(None)
                w.hide()
            except RuntimeError:
                pass
        # The Qt idiom for swapping a layout on a widget: transfer the old
        # layout to a temp QWidget which then GCs.
        old_layout = self.strip.layout()
        if old_layout is not None:
            temp = QWidget()
            temp.setLayout(old_layout)
            temp.deleteLater()
        # Build + install the new layout.
        if mode == "compact":
            new_layout = self._build_compact_strip_layout()
            self.strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        else:
            new_layout = self._build_classic_strip_layout()
            self.strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.strip.setLayout(new_layout)
        # Re-parent each widget to the strip and show.
        for w in keep:
            if w is None:
                continue
            try:
                w.setParent(self.strip)
                w.show()
            except RuntimeError:
                pass

    # ---------- layout ----------

    def apply_layout(self, layout) -> None:
        """Apply a new layout: swap slot variants in the now-playing strip,
        toggle nav/status visibility, resize window per layout's window_default.

        Structural mode changes (classic/compact/stage) are partially handled
        — compact triggers mini-mode style hiding; stage is currently treated
        like classic (TODO: side-by-side art + lyrics).
        """
        # Slot swap — rebuild whichever widgets changed.
        new_progress = layout.slots.get("progress", "blocks")
        new_volume   = layout.slots.get("volume", "blocks")
        new_art      = layout.slots.get("album_art", "square")
        new_controls = layout.slots.get("controls", "bracket")
        new_label    = layout.slots.get("now_label", "stacked")

        if new_progress != self._slot_progress:
            self._slot_progress = new_progress
            self._swap_progress(new_progress)
        if new_volume != self._slot_volume:
            self._slot_volume = new_volume
            self._swap_volume(new_volume)
        if new_art != self._slot_album_art:
            self._slot_album_art = new_art
            self._swap_album_art(new_art)
        if new_controls != self._slot_controls:
            self._slot_controls = new_controls
            self._swap_controls(new_controls)
        if new_label != self._slot_now_label:
            self._slot_now_label = new_label
            self._swap_now_label(new_label)

        # Visibility
        if hasattr(self, "_upper_wrap_widget") and self._upper_wrap_widget is not None:
            self._upper_wrap_widget.setVisible(layout.visibility.get("nav_rail", True))
        self.statusBar().setVisible(layout.visibility.get("status_bar", True))
        # Show/hide nav buttons for views the layout doesn't want.
        self.nav_queue_btn.setVisible(layout.visibility.get("queue_view", True))
        self.nav_lyrics_btn.setVisible(layout.visibility.get("lyrics", True))
        self.nav_history_btn.setVisible(layout.visibility.get("history", True))
        self.nav_visualizer_btn.setVisible(layout.visibility.get("visualizer", True))

        # Mode-based structural rebuild + window size.
        new_mode = layout.mode
        prev_mode = getattr(self, "_layout_mode", "classic")
        self._layout_mode = new_mode
        if new_mode != prev_mode:
            # Chrome hiding is fully covered by the visibility flags above;
            # compact layouts only need the strip re-oriented. (They used to
            # also call set_mini_mode, which now opens a separate window —
            # and whose old behavior was redundant here anyway.)
            self._rebuild_strip("compact" if new_mode == "compact" else "classic")
        self.resize(*layout.window_default)

        self.statusBar().showMessage(
            theming.styled_case(f"layout · {layout.name}")
        )

    def _swap_progress(self, slug: str) -> None:
        new = make_progress(slug)
        new.seek_requested.connect(self.player.seek)
        if self.player.duration > 0:
            new.setDuration(self.player.duration)
            new.setPosition(self._last_position)
        self._replace_in_layout(self.progress, new)
        self.progress = new

    def _swap_volume(self, slug: str) -> None:
        new = make_volume(slug)
        new.volume_changed.connect(self._on_volume_changed)
        try:
            new.setVolume(self.volume.volume(), emit=False)
        except Exception:
            pass
        self._replace_in_layout(self.volume, new)
        self.volume = new

    def _swap_album_art(self, slug: str) -> None:
        new = make_album_art(slug, 96)
        self._wire_art_click(new)
        self._replace_in_layout(self.art, new)
        self.art = new

    def _swap_controls(self, slug: str) -> None:
        new_bundle = make_controls(slug)
        # Snapshot prev state defensively — buttons may be in any state.
        def _safe_enabled(btn) -> bool:
            try:
                return btn.isEnabled()
            except (RuntimeError, AttributeError):
                return False
        prev_enabled = _safe_enabled(self.prev_btn)
        play_enabled = _safe_enabled(self.play_btn)
        next_enabled = _safe_enabled(self.next_btn)
        like_enabled = _safe_enabled(self.like_btn)
        new_bundle.prev_btn.setEnabled(prev_enabled)
        new_bundle.play_btn.setEnabled(play_enabled)
        new_bundle.next_btn.setEnabled(next_enabled)
        new_bundle.like_btn.setEnabled(like_enabled)
        new_bundle.prev_btn.clicked.connect(self._on_prev_clicked)
        new_bundle.play_btn.clicked.connect(self._on_play_clicked)
        new_bundle.next_btn.clicked.connect(self._on_next_clicked)
        new_bundle.like_btn.clicked.connect(self._on_like_clicked)
        # Swap each button in its layout slot.
        self._replace_in_layout(self.prev_btn, new_bundle.prev_btn)
        self._replace_in_layout(self.play_btn, new_bundle.play_btn)
        self._replace_in_layout(self.next_btn, new_bundle.next_btn)
        self._replace_in_layout(self.like_btn, new_bundle.like_btn)
        self.prev_btn = new_bundle.prev_btn
        self.play_btn = new_bundle.play_btn
        self.next_btn = new_bundle.next_btn
        self.like_btn = new_bundle.like_btn
        self._controls_bundle = new_bundle

    def _swap_now_label(self, slug: str) -> None:
        new = make_now_label(slug)
        if self._current is not None:
            new.setTrack(self._current.artists, self._current.title, self._current.album)
        self._replace_in_layout(self.now_label, new)
        self.now_label = new

    def _replace_in_layout(self, old, new) -> None:
        """Recursively find ``old``'s containing layout (even when nested
        several levels deep) and swap it for ``new`` at the same index.

        Defensively tolerates already-deleted Qt objects; in that case the
        new widget is just left orphan (Python ref keeps it alive).
        """
        try:
            parent = old.parentWidget()
        except RuntimeError:
            new.deleteLater()
            return
        if parent is None:
            new.deleteLater()
            return
        try:
            top_layout = parent.layout()
        except RuntimeError:
            new.deleteLater()
            return
        if top_layout is None:
            new.deleteLater()
            return

        containing_layout, idx = self._find_widget_in_layout_tree(top_layout, old)
        if containing_layout is None:
            new.deleteLater()
            return

        try:
            containing_layout.removeWidget(old)
            old.hide()
            old.setParent(None)
            old.deleteLater()
        except RuntimeError:
            pass
        # Reparent new to the same QWidget that owned old's containing layout.
        owner = containing_layout.parentWidget() or parent
        new.setParent(owner)
        try:
            containing_layout.insertWidget(idx, new)
        except Exception:
            containing_layout.addWidget(new)
        new.show()

    @staticmethod
    def _find_widget_in_layout_tree(layout, target):
        """DFS through nested layouts. Returns (containing_layout, index) or (None, -1)."""
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item is None:
                continue
            try:
                w = item.widget()
            except RuntimeError:
                continue
            if w is target:
                return layout, i
            sub = item.layout()
            if sub is not None:
                found_layout, found_idx = MainWindow._find_widget_in_layout_tree(sub, target)
                if found_layout is not None:
                    return found_layout, found_idx
        return None, -1

    # ---------- mini-mode ----------

    def _wire_art_click(self, art) -> None:
        """Single click on the now-playing art opens the mini player."""
        art.clicked.connect(self.toggle_mini_mode)
        art.setCursor(Qt.PointingHandCursor)
        art.setToolTip("mini player")

    def toggle_mini_mode(self) -> None:
        # Deferred: reached from mouse handlers (art click) and shortcuts —
        # window show/hide inside the emission is the PySide6 + py3.14 crash
        # pattern (see open_settings below).
        QTimer.singleShot(0, self._toggle_mini_now)

    def _toggle_mini_now(self) -> None:
        self.set_mini_mode(not self._mini_mode)

    def exit_mini_mode(self) -> None:
        self.set_mini_mode(False)

    def set_mini_mode(self, on: bool) -> None:
        """Swap between the main window and the dedicated mini player.

        The mini is a separate frameless top-level (ui/mini.py) — the old
        shrink-the-main-window mini died in v1.2.7's redo.
        """
        if on == self._mini_mode:
            return
        self._mini_mode = on
        adaptive = getattr(self, "_adaptive", None)
        ambient = getattr(self, "_ambient", None)
        if on:
            if self._mini is None:
                from .mini import MiniPlayer
                self._mini = MiniPlayer(self)
            if adaptive is not None:
                adaptive.set_mini_active(True)
            if ambient is not None:
                # The mini itself is the target — it fans the envelope into
                # its backdrop and (optionally) the bass-resize breathing.
                ambient.add_target(self._mini)
            self._mini.sync_now(
                self._current,
                self.player.duration,
                self._last_position,
                self.player.state,
                self._liked_current,
            )
            self._mini.show()
            self._mini.raise_()
            self._mini.activateWindow()
            self.hide()
        else:
            if self._mini is not None:
                if ambient is not None:
                    ambient.remove_target(self._mini)
                self._mini.hide()
            if adaptive is not None:
                adaptive.set_mini_active(False)
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _apply_mini_backdrop(self) -> None:
        """Live-apply mini_* settings to an open mini (settings-dialog save)."""
        if self._mini is not None and self._mini_mode:
            self._mini.apply_settings()

    # ---------- multi-window helpers (tray / MPRIS) ----------

    def active_app_window(self) -> QWidget:
        """The window the user currently interacts with — mini or main."""
        if self._mini_mode and self._mini is not None:
            return self._mini
        return self

    def present_active(self) -> None:
        w = self.active_app_window()
        if w is self:
            self.showNormal()
        else:
            w.show()
        w.raise_()
        w.activateWindow()

    def toast_host(self) -> QWidget:
        """Where toasts should render — the hidden main window can't show
        them while the mini is up."""
        if self._mini_mode and self._mini is not None and self._mini.isVisible():
            return self._mini
        return self

    def open_settings(self) -> None:
        # Defer the modal past the click handler — opening a QDialog directly
        # inside the button's clicked emission segfaults on PySide6 + py3.14
        # (see [[feedback-pyside-modal]]). singleShot(0) with self as receiver
        # marshals onto this (GUI) thread's event loop.
        QTimer.singleShot(0, self._do_open_settings)

    def _do_open_settings(self) -> None:
        from .settings import SettingsDialog
        current = getattr(self, "_settings", None)
        if current is None:
            # Settings injection from app.py hasn't happened (e.g. tests).
            from .. import settings as settings_module
            current = settings_module.load()
        dlg = SettingsDialog(current, parent=self)
        self._ui_sound("modal_open")
        try:
            result = dlg.exec()
        finally:
            self._ui_sound("modal_close")
        if result != dlg.DialogCode.Accepted:
            dlg.deleteLater()
            return
        new = dlg.updated_settings()
        dlg.deleteLater()
        self._settings = new
        # Hot-swap the UI sounds master toggle.
        ui_sounds = getattr(self, "ui_sounds", None)
        if ui_sounds is not None:
            ui_sounds.set_enabled(bool(new.ui_sounds_enabled))
        # Hover/press prefetch is checked at fire time, so flipping the
        # attr is the whole live apply. warm_results is read per-search.
        self._prefetch.hover_enabled = bool(new.prefetch_hover)
        # Push discord changes to the live presence client if it's running.
        discord = getattr(self, "_discord", None)
        if discord is not None:
            discord.configure(new.discord_app_id, new.discord_enabled)
        # Presence lyric feed follows both toggles; disabling emits None,
        # which clears any lyric already sitting on the profile.
        lyric_tracker = getattr(self, "_lyric_tracker", None)
        if lyric_tracker is not None:
            lyric_tracker.set_enabled(
                new.discord_enabled and new.discord_lyrics_enabled
            )
        # Apply audio device override to the visualizer feed (restart if running).
        try:
            self.visualizer_view._set_audio_source(new.audio_device or None)
        except Exception:
            pass
        # Apply listenbrainz settings to the live scrobbler.
        scrobbler = getattr(self, "_scrobbler", None)
        if scrobbler is not None:
            scrobbler.configure(new.listenbrainz_token, new.listenbrainz_enabled)
        # Apply adaptive accent toggle.
        adaptive = getattr(self, "_adaptive", None)
        if adaptive is not None:
            adaptive.set_enabled(new.adaptive_accent)
            adaptive.set_background_enabled(new.adaptive_background)
        # Central-area gradient + corner radius. The CentralBg widget owns
        # the paint; the theming manager owns the @radius token so other
        # widgets (inputs, scrollbars, etc.) match the chosen softness.
        from .central_bg import corner_radius as _corner_radius
        if hasattr(self, "central_bg"):
            self.central_bg.set_enabled(new.adaptive_background)
            self.central_bg.set_style(new.adaptive_background_style or "field")
            self.central_bg.set_motion(new.motion or "lite")
            self.central_bg.set_radius(_corner_radius(new.corner_style))
        # Ambient bass-pulse toggle.
        ambient = getattr(self, "_ambient", None)
        if ambient is not None:
            ambient.set_pulse_enabled(new.adaptive_pulse and new.adaptive_background)
        # Live-apply mini player prefs if the mini is currently up.
        self._apply_mini_backdrop()
        radius_px = _corner_radius(new.corner_style)
        theming.manager().set_user_override(
            "radius", f"{radius_px}px" if radius_px > 0 else None
        )
        # Hot-swap nav icons.
        self.apply_nav_icons(new.nav_icon_set or "off")
        # Hot-swap font family override. The theming manager re-applies the
        # current theme so the new font lands on every widget that listens
        # to theme_changed.
        theming.manager().set_user_font(new.font_family_override or "")
        theming.manager().set_user_font_size(new.font_size_override_pt or 0)
        # Push new loading-indicator style to any currently-running indicator.
        if hasattr(self, "_loading"):
            self._loading.set_style(new.loading_indicator_style)
        # Hot-swap motion intensity. Helpers consult the cached value every
        # call, so animations queued after this point pick up the new level.
        from . import motion as motion_module
        motion_module.set_intensity(new.motion)
        # Hot-swap UI scale. Re-apply the active theme so the QApplication
        # font + QSS pick up the new size_pt, and any widget that listens to
        # theme_changed (track row delegate, AlbumArt, MonoProgress, etc.)
        # re-derives its scaled pixel sizes in the same beat.
        from . import scale as scale_module
        if scale_module.current().value != new.ui_scale:
            scale_module.set_factor(new.ui_scale)
            current_theme = theming.manager().current()
            if current_theme is not None:
                theming.manager().apply(current_theme.slug)
        # Hot-swap pitch correction. This re-applies the scaletempo filter
        # immediately so the user hears the change without restarting mpv.
        try:
            self.player.set_pitch_correction(bool(new.preserve_pitch))
        except Exception:
            pass

    # ---------- session persistence ----------

    def _schedule_session_save(self) -> None:
        if self._restoring_session:
            return
        self._session_dirty = True
        self._session_save_timer.start()

    def _save_session_now(self) -> None:
        if self._restoring_session:
            return
        try:
            snap = session_module.snapshot_from(
                self.queue, self.player.state, self._last_position
            )
            session_module.save(snap)
            self._session_dirty = False
        except Exception:
            pass

    def restore_session(self, snapshot: "session_module.Snapshot") -> None:
        """Re-populate queue + start current track paused at saved position.

        Called from app.py after the window is constructed but before show().
        """
        tracks = session_module.tracks_from_snapshot(snapshot)
        if not tracks or snapshot.current_index < 0 or snapshot.current_index >= len(tracks):
            return

        self._restoring_session = True
        try:
            self.queue.clear()
            self.queue.add_many(tracks)
            if snapshot.radio_enabled:
                seed = tracks[snapshot.current_index].video_id
                self.queue.enable_radio(seed)
            current = self.queue.set_current(snapshot.current_index)
            if current is None:
                return
            # Pre-fill the now-playing strip so the user sees state immediately.
            self._current = current
            self.now_label.setTrack(current.artists, current.title, current.album)
            self.now_label.setStatus("paused")
            self.statusBar().showMessage(f"restored session · paused at {_mmss(snapshot.position_seconds)}")
            self._fetch_art(current)

            # Resolve + load, then seek + pause. Failures fall back to "just loaded".
            thread = QThread()
            worker = _ResolveWorker(current)
            worker.moveToThread(thread)
            saved_pos = snapshot.position_seconds

            def _on_resolved(video_id: str, ref: object) -> None:
                if not self._current or self._current.video_id != video_id:
                    return
                if isinstance(ref, StreamRef):
                    self.player.load_ref(ref)
                else:
                    self.player.load_url(str(ref))
                self.player.pause()
                if saved_pos > 1.0:
                    QTimer.singleShot(300, lambda: self.player.seek(saved_pos))
                self.play_btn.setEnabled(True)
                self._refresh_nav_buttons()

            def _on_failed(_vid: str, msg: str) -> None:
                self.statusBar().showMessage(f"couldn't restore stream: {msg}")

            thread.started.connect(worker.run)
            worker.resolved.connect(_on_resolved)
            worker.failed.connect(_on_failed)
            worker.resolved.connect(thread.quit)
            worker.failed.connect(thread.quit)
            thread.finished.connect(thread.deleteLater)
            self._resolve_thread = thread
            self._resolve_worker = worker
            qthreads.retain(thread, worker)
            thread.start()
        finally:
            self._restoring_session = False

    # ---------- lifecycle ----------

    def request_quit(self) -> None:
        """Mark this close as a real quit (vs. a hide-to-tray)."""
        self._wants_quit = True
        self.close()

    def closeEvent(self, event) -> None:
        wants_quit = getattr(self, "_wants_quit", False)
        tray = getattr(self, "_tray", None)
        # If we have a tray and the user didn't explicitly request quit,
        # hide-to-tray instead of closing.
        if tray is not None and not wants_quit:
            event.ignore()
            self.hide()
            return
        # Real quit: the mini is a separate top-level and would outlive
        # super().closeEvent, leaving the app idling headless with a dead
        # player. Commit to quitting (the mini's own closeEvent reroutes
        # compositor closes to exit-mini unless _wants_quit says otherwise),
        # then close it before the player goes down.
        self._wants_quit = True
        if self._mini is not None:
            try:
                self._mini.close()
            except RuntimeError:
                pass
        if self._session_dirty:
            self._save_session_now()
        else:
            try:
                snap = session_module.snapshot_from(
                    self.queue, self.player.state, self._last_position
                )
                session_module.save(snap)
            except Exception:
                pass
        self.player.shutdown()
        super().closeEvent(event)


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}"
