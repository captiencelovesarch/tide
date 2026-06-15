"""Main window: search + results + queue + now-playing strip."""
from __future__ import annotations

from PySide6.QtCore import (
    QObject,
    QThread,
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
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming
from ..player import PlayState, Player
from ..queue import Queue, Role
from .library import LibraryView
from .lyrics import LyricsView
from .widgets import AlbumArt, BracketButton, MonoProgress, MonoVolume, NowPlayingLabel


# ---------- background workers ----------


class _SearchWorker(QObject):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api, query: str) -> None:
        super().__init__()
        self.api = api_obj
        self.query = query

    def run(self) -> None:
        try:
            self.done.emit(self.api.search_songs(self.query))
        except Exception as exc:
            self.failed.emit(str(exc))


class _ResolveWorker(QObject):
    resolved = Signal(str, str)
    failed = Signal(str, str)

    def __init__(self, video_id: str) -> None:
        super().__init__()
        self.video_id = video_id

    def run(self) -> None:
        try:
            url = api.resolve_stream_url(self.video_id)
            self.resolved.emit(self.video_id, url)
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


# ---------- main window ----------


class MainWindow(QMainWindow):
    def __init__(self, api_obj: api.Api, player: Player) -> None:
        super().__init__()
        self.setWindowTitle("tide")
        self.resize(1100, 720)
        self.api = api_obj
        self.player = player
        self.queue = Queue(self)

        # thread / worker refs (hold to prevent GC during run())
        self._search_thread: QThread | None = None
        self._search_worker: _SearchWorker | None = None
        self._resolve_thread: QThread | None = None
        self._resolve_worker: _ResolveWorker | None = None
        self._radio_thread: QThread | None = None
        self._radio_worker: _RadioWorker | None = None

        self._current: api.Track | None = None
        self._auto_radio_on_play = True   # play-now seeds a radio by default
        self._last_position: float = 0.0

        self._net = QNetworkAccessManager(self)
        self._art_for_video_id: str | None = None

        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme_changed)

        self._build_ui()
        self._wire_player()
        self._wire_queue()
        self._wire_shortcuts()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        # ----- nav rail -----
        self.nav_search_btn = BracketButton("search")
        self.nav_library_btn = BracketButton("library")
        self.nav_queue_btn = BracketButton("queue")
        self.nav_lyrics_btn = BracketButton("lyrics")
        self.nav_settings_btn = BracketButton("settings")
        self.nav_search_btn.clicked.connect(lambda: self._switch_view("search"))
        self.nav_library_btn.clicked.connect(lambda: self._switch_view("library"))
        self.nav_queue_btn.clicked.connect(lambda: self._switch_view("queue"))
        self.nav_lyrics_btn.clicked.connect(lambda: self._switch_view("lyrics"))
        self.nav_settings_btn.clicked.connect(self.open_settings)

        nav_col = QVBoxLayout()
        nav_col.setContentsMargins(10, 14, 10, 14)
        nav_col.setSpacing(2)
        nav_col.addWidget(self.nav_search_btn)
        nav_col.addWidget(self.nav_library_btn)
        nav_col.addWidget(self.nav_queue_btn)
        nav_col.addWidget(self.nav_lyrics_btn)
        nav_col.addStretch(1)
        nav_col.addWidget(self.nav_settings_btn)
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setLayout(nav_col)
        nav.setFixedWidth(140)

        # ----- search view -----
        self.search = QLineEdit()
        self.search.setPlaceholderText("search youtube music…")
        self.search.returnPressed.connect(self._on_search)
        self.search.setClearButtonEnabled(True)

        self.heading = QLabel(self._line_heading("results"))
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.results = QListWidget()
        self.results.itemActivated.connect(self._on_result_activated)
        self.results.setUniformItemSizes(True)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_results_menu)

        search_col = QVBoxLayout()
        search_col.setContentsMargins(16, 14, 16, 8)
        search_col.setSpacing(8)
        search_col.addWidget(self.search)
        search_col.addWidget(self.heading)
        search_col.addWidget(self.results, stretch=1)
        search_view = QWidget()
        search_view.setLayout(search_col)

        # ----- queue view -----
        self.queue_heading = QLabel(self._line_heading("queue"))
        self.queue_heading.setProperty("class", "dim")

        self.queue_view = QListView()
        self.queue_view.setModel(self.queue)
        self.queue_view.setUniformItemSizes(True)
        self.queue_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_view.customContextMenuRequested.connect(self._on_queue_menu)
        self.queue_view.doubleClicked.connect(self._on_queue_double)

        self.radio_btn = BracketButton("radio: off")
        self.radio_btn.clicked.connect(self._on_radio_toggle)
        self.clear_btn = BracketButton("clear queue")
        self.clear_btn.clicked.connect(self.queue.clear)

        queue_actions = QHBoxLayout()
        queue_actions.addWidget(self.radio_btn)
        queue_actions.addWidget(self.clear_btn)
        queue_actions.addStretch(1)

        queue_col = QVBoxLayout()
        queue_col.setContentsMargins(16, 14, 16, 8)
        queue_col.setSpacing(8)
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

        # ----- stack -----
        self.stack = QStackedWidget()
        self.stack.addWidget(search_view)        # 0
        self.stack.addWidget(self.library_view)  # 1
        self.stack.addWidget(queue_view)         # 2
        self.stack.addWidget(self.lyrics_view)   # 3

        upper = QHBoxLayout()
        upper.setContentsMargins(0, 0, 0, 0)
        upper.setSpacing(0)
        upper.addWidget(nav)
        upper.addWidget(self.stack, stretch=1)
        upper_wrap = QWidget()
        upper_wrap.setLayout(upper)

        # ----- now-playing strip -----
        self.art = AlbumArt(96)
        self.now_label = NowPlayingLabel()

        self.prev_btn = BracketButton("prev", glyph="◂◂")
        self.play_btn = BracketButton("play", glyph="▶")
        self.next_btn = BracketButton("next", glyph="▸▸")
        self.prev_btn.clicked.connect(self._on_prev_clicked)
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.next_btn.clicked.connect(self._on_next_clicked)
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.play_btn.setEnabled(False)

        self.progress = MonoProgress()
        self.progress.seek_requested.connect(self.player.seek)

        self.volume = MonoVolume()
        self.volume.volume_changed.connect(self._on_volume_changed)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("class", "dim")
        self.time_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        self.time_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(2)
        controls_row.addWidget(self.prev_btn)
        controls_row.addWidget(self.play_btn)
        controls_row.addWidget(self.next_btn)
        controls_row.addStretch(1)
        controls_row.addWidget(self.volume)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.time_label)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(6)
        right_col.addWidget(self.now_label, stretch=1)
        right_col.addLayout(progress_row)
        right_col.addLayout(controls_row)

        strip_layout = QHBoxLayout()
        strip_layout.setContentsMargins(16, 12, 16, 12)
        strip_layout.setSpacing(14)
        strip_layout.addWidget(self.art)
        strip_layout.addLayout(right_col, stretch=1)
        strip = QFrame()
        strip.setObjectName("now_playing")
        strip.setLayout(strip_layout)
        strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # ----- assemble -----
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(upper_wrap, stretch=1)
        root.addWidget(strip)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("ready")

    def _line_heading(self, label: str, total: int = 60) -> str:
        line = "─" * max(4, total - len(label) - 6)
        return f"── {label.lower()} {line}"

    def _set_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    # ---------- nav ----------

    def _switch_view(self, name: str) -> None:
        if name == "search":
            self.stack.setCurrentIndex(0)
            self.search.setFocus()
        elif name == "library":
            self.stack.setCurrentIndex(1)
            if self.library_view.playlists_list.count() == 0:
                self.library_view.reload_playlists()
        elif name == "queue":
            self.stack.setCurrentIndex(2)
        elif name == "lyrics":
            self.stack.setCurrentIndex(3)
            self.lyrics_view.show_for(self._current)

    # ---------- search ----------

    def _on_search(self) -> None:
        q = self.search.text().strip()
        if not q:
            return
        self.heading.setText(self._line_heading(f"searching “{q}”"))
        self.results.clear()
        self.statusBar().showMessage(f"searching: {q}")

        thread = QThread(self)
        worker = _SearchWorker(self.api, q)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_results)
        worker.failed.connect(self._on_search_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._search_thread = thread
        self._search_worker = worker
        thread.start()

    def _on_results(self, tracks: list[api.Track]) -> None:
        marker = self._list_marker()
        if not tracks:
            self.heading.setText(self._line_heading("no results"))
            self.statusBar().showMessage("no results")
            return
        self.heading.setText(self._line_heading(f"results · {len(tracks)}"))
        self.statusBar().showMessage(f"{len(tracks)} results")
        for tr in tracks:
            artist = (tr.artists or "").lower()
            title = (tr.title or "").lower()
            dur = tr.duration or ""
            label = f"{marker}{artist} — {title}"
            if dur:
                gap = max(2, 60 - len(label) - len(dur))
                label = f"{label}{' ' * gap}{dur}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, tr)
            self.results.addItem(item)

    def _on_search_failed(self, msg: str) -> None:
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
        for a in (a_play, a_next, a_add, a_radio):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self._play_now(tr, seed_radio=False))
        a_next.triggered.connect(lambda: self._queue_next(tr))
        a_add.triggered.connect(lambda: self._queue_add(tr))
        a_radio.triggered.connect(lambda: self._start_radio(tr))
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

    def _play_now(self, track: api.Track, *, seed_radio: bool) -> None:
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
        self._current = track
        self.now_label.setTrack(track.artists, track.title, track.album)
        self.now_label.setStatus("loading")
        self.progress.reset()
        self.time_label.setText("0:00 / 0:00")
        self.statusBar().showMessage("resolving stream…")
        self._fetch_art(track)

        thread = QThread(self)
        worker = _ResolveWorker(track.video_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resolved.connect(self._on_resolved)
        worker.failed.connect(self._on_resolve_failed)
        worker.resolved.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._resolve_thread = thread
        self._resolve_worker = worker
        thread.start()

    def _on_resolved(self, video_id: str, url: str) -> None:
        if not self._current or self._current.video_id != video_id:
            return
        self.player.load_url(url)
        self.now_label.setStatus("")
        self.statusBar().showMessage("playing")
        self.play_btn.setEnabled(True)
        self._refresh_nav_buttons()
        # If lyrics is the active view, refresh it for the new track.
        if self.stack.currentIndex() == 3:
            self.lyrics_view.show_for(self._current)

    def _on_resolve_failed(self, video_id: str, msg: str) -> None:
        self.statusBar().showMessage(f"couldn't resolve: {msg}")
        self.now_label.setStatus("error")
        QMessageBox.warning(self, "tide", f"couldn't get audio:\n\n{msg}")

    def _on_play_clicked(self) -> None:
        self.player.toggle()

    def _on_next_clicked(self) -> None:
        tr = self.queue.advance()
        if tr:
            self._play_track(tr)

    def _on_prev_clicked(self) -> None:
        # If we're more than 3s into the song, restart it. Else go back.
        if self.player.duration > 0 and self._last_position > 3:
            self.player.seek(0)
            return
        tr = self.queue.back()
        if tr:
            self._play_track(tr)

    def _refresh_nav_buttons(self) -> None:
        self.next_btn.setEnabled(self.queue.can_advance() or self.queue.radio_enabled)
        self.prev_btn.setEnabled(self.queue.can_go_back() or self.player.duration > 0)

    # ---------- queue / radio plumbing ----------

    def _wire_queue(self) -> None:
        self.queue.current_changed.connect(self._on_queue_current_changed)
        self.queue.refill_requested.connect(self._on_radio_refill_requested)
        self.queue.radio_state_changed.connect(self._on_radio_state_changed)
        self.queue.rowsInserted.connect(self._on_queue_size_changed)
        self.queue.rowsRemoved.connect(self._on_queue_size_changed)
        self.queue.modelReset.connect(lambda: self._on_queue_size_changed(None, 0, 0))

    def _on_queue_size_changed(self, *_args) -> None:
        self.queue_heading.setText(
            self._line_heading(f"queue · {self.queue.rowCount()}")
        )
        self._refresh_nav_buttons()

    def _on_queue_current_changed(self, _track) -> None:
        self._refresh_nav_buttons()

    def _on_radio_state_changed(self, enabled: bool) -> None:
        self.radio_btn.setLabel("radio: on" if enabled else "radio: off")
        self._refresh_nav_buttons()

    def _on_radio_refill_requested(self, seed_video_id: str, exclude: list) -> None:
        thread = QThread(self)
        worker = _RadioWorker(self.api, seed_video_id, list(exclude))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_radio_done)
        worker.failed.connect(self._on_radio_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._radio_thread = thread
        self._radio_worker = worker
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
        """
        self.art.setImage(None)
        if not track.thumbnail:
            self._art_for_video_id = None
            return
        self._art_for_video_id = track.video_id

        req = QNetworkRequest(QUrl(track.thumbnail))
        reply = self._net.get(req)
        target_video_id = track.video_id

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
            img = QImage()
            if img.loadFromData(data):
                self.art.setImage(img)

        reply.finished.connect(on_finished)

    # ---------- player state ----------

    def _wire_player(self) -> None:
        self.player.state_changed.connect(self._on_state)
        self.player.position_changed.connect(self._on_position)
        self.player.duration_changed.connect(self._on_duration)
        self.player.ended.connect(self._on_track_ended)
        self.player.error.connect(self._on_player_error)

    def _on_state(self, s: PlayState) -> None:
        if s == PlayState.PLAYING:
            self.play_btn.setLabel("pause")
            self.play_btn.setGlyph("⏸")
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

    def _on_duration(self, secs: float) -> None:
        self.progress.setDuration(secs)
        self._update_time_label(0.0, secs)

    def _update_time_label(self, pos: float, dur: float) -> None:
        self.time_label.setText(f"{_mmss(pos)} / {_mmss(dur)}")

    def _on_track_ended(self) -> None:
        tr = self.queue.advance()
        if tr:
            self._play_track(tr)
        else:
            self.now_label.setStatus("queue empty")
            self.statusBar().showMessage("queue empty")

    def _on_player_error(self, msg: str) -> None:
        self.statusBar().showMessage(f"player error: {msg}")

    # ---------- theme + shortcuts ----------

    def _on_theme_changed(self, theme) -> None:
        self._theme = theme
        self.heading.setText(self._line_heading("results"))
        self.queue_heading.setText(self._line_heading(f"queue · {self.queue.rowCount()}"))

    def _list_marker(self) -> str:
        return str(self._theme.t("layout", "list_marker", "> ")) if self._theme else "> "

    def _wire_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+L"), self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+F"), self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._switch_view("search"))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._switch_view("library"))
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self._switch_view("queue"))
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self._switch_view("lyrics"))
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        QShortcut(QKeySequence("Space"), self, self.player.toggle)
        QShortcut(QKeySequence("Ctrl+Right"), self, self._on_next_clicked)
        QShortcut(QKeySequence("Ctrl+Left"), self, self._on_prev_clicked)
        QShortcut(QKeySequence("Ctrl+Up"), self, lambda: self.volume.setVolume(self.volume.volume() + 5))
        QShortcut(QKeySequence("Ctrl+Down"), self, lambda: self.volume.setVolume(self.volume.volume() - 5))

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

    def open_settings(self) -> None:
        from .settings import SettingsDialog
        current = getattr(self, "_settings", None)
        if current is None:
            # Settings injection from app.py hasn't happened (e.g. tests).
            from .. import settings as settings_module
            current = settings_module.load()
        dlg = SettingsDialog(current, parent=self)
        result = dlg.exec()
        if result != dlg.DialogCode.Accepted:
            return
        new = dlg.updated_settings()
        self._settings = new
        # Push discord changes to the live presence client if it's running.
        discord = getattr(self, "_discord", None)
        if discord is not None:
            discord.configure(new.discord_app_id, new.discord_enabled)

    # ---------- lifecycle ----------

    def closeEvent(self, event) -> None:
        self.player.shutdown()
        super().closeEvent(event)


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}"
