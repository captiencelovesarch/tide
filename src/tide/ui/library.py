"""Library view: your playlists + liked songs.

Has two internal pages:
  - Index: list of your playlists (LM 'Liked Music' first, then user playlists).
  - Detail: tracks of the currently-opened playlist with the standard right-
    click menu (play now / play next / add to queue / start radio).

All actions bubble up to MainWindow via signals so playback state stays in
one place.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming
from .widgets import BracketButton


class _PlaylistsWorker(QObject):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api) -> None:
        super().__init__()
        self.api = api_obj

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_library_playlists())
        except Exception as exc:
            self.failed.emit(str(exc))


class _PlaylistDetailWorker(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api, playlist_id: str) -> None:
        super().__init__()
        self.api = api_obj
        self.playlist_id = playlist_id

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_playlist(self.playlist_id))
        except Exception as exc:
            self.failed.emit(str(exc))


def _line_heading(label: str, total: int = 60) -> str:
    line = "─" * max(4, total - len(label) - 6)
    return f"── {label.lower()} {line}"


class LibraryView(QWidget):
    play_now_requested = Signal(object, bool)   # Track, seed_radio
    queue_add_requested = Signal(object)        # Track
    queue_next_requested = Signal(object)       # Track
    radio_requested = Signal(object)            # Track
    play_all_requested = Signal(list)           # tracks (first plays, rest queued)
    status_message = Signal(str)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

        self._pls_thread: QThread | None = None
        self._pls_worker: _PlaylistsWorker | None = None
        self._detail_thread: QThread | None = None
        self._detail_worker: _PlaylistDetailWorker | None = None

        self._current_detail: api.PlaylistDetail | None = None

        self._build_ui()
        # Lazy-load: only fetch playlists when the view becomes visible.

    def _build_ui(self) -> None:
        self.stack = QStackedWidget()

        # ----- index page -----
        self.index_heading = QLabel(_line_heading("your library"))
        self.index_heading.setProperty("class", "dim")

        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.clicked.connect(self.reload_playlists)

        actions_row = QHBoxLayout()
        actions_row.addWidget(self.refresh_btn)
        actions_row.addStretch(1)

        self.playlists_list = QListWidget()
        self.playlists_list.setUniformItemSizes(True)
        self.playlists_list.itemActivated.connect(self._on_playlist_activated)

        idx_col = QVBoxLayout()
        idx_col.setContentsMargins(16, 14, 16, 8)
        idx_col.setSpacing(8)
        idx_col.addWidget(self.index_heading)
        idx_col.addLayout(actions_row)
        idx_col.addWidget(self.playlists_list, stretch=1)
        idx_page = QWidget()
        idx_page.setLayout(idx_col)

        # ----- detail page -----
        self.back_btn = BracketButton("back")
        self.back_btn.clicked.connect(self._show_index)
        self.detail_heading = QLabel(_line_heading("playlist"))
        self.detail_heading.setProperty("class", "dim")
        self.detail_heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.play_all_btn = BracketButton("play all")
        self.play_all_btn.clicked.connect(self._on_play_all)
        self.shuffle_play_btn = BracketButton("shuffle")
        self.shuffle_play_btn.clicked.connect(self._on_shuffle_play)

        detail_top = QHBoxLayout()
        detail_top.addWidget(self.back_btn)
        detail_top.addWidget(self.detail_heading, stretch=1)

        detail_actions = QHBoxLayout()
        detail_actions.addWidget(self.play_all_btn)
        detail_actions.addWidget(self.shuffle_play_btn)
        detail_actions.addStretch(1)

        self.tracks_list = QListWidget()
        self.tracks_list.setUniformItemSizes(True)
        self.tracks_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tracks_list.customContextMenuRequested.connect(self._on_track_menu)
        self.tracks_list.itemActivated.connect(self._on_track_activated)

        det_col = QVBoxLayout()
        det_col.setContentsMargins(16, 14, 16, 8)
        det_col.setSpacing(8)
        det_col.addLayout(detail_top)
        det_col.addLayout(detail_actions)
        det_col.addWidget(self.tracks_list, stretch=1)
        det_page = QWidget()
        det_page.setLayout(det_col)

        self.stack.addWidget(idx_page)
        self.stack.addWidget(det_page)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack)

    # ---------- index ----------

    def reload_playlists(self) -> None:
        self.playlists_list.clear()
        self.index_heading.setText(_line_heading("loading…"))
        thread = QThread(self)
        worker = _PlaylistsWorker(self.api)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_playlists)
        worker.failed.connect(self._on_playlists_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._pls_thread = thread
        self._pls_worker = worker
        thread.start()

    def _on_playlists(self, items: list[api.PlaylistEntry]) -> None:
        marker = self._list_marker()
        self.index_heading.setText(_line_heading(f"your library · {len(items)}"))
        self.playlists_list.clear()
        for p in items:
            label = f"{marker}{(p.title or '').lower()}"
            if p.description:
                label += f"    {p.description.lower()}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, p)
            self.playlists_list.addItem(item)

    def _on_playlists_failed(self, msg: str) -> None:
        self.index_heading.setText(_line_heading("library load failed"))
        self.status_message.emit(f"library: {msg}")

    def _on_playlist_activated(self, item: QListWidgetItem) -> None:
        p: api.PlaylistEntry = item.data(Qt.UserRole)
        if not p:
            return
        self.open_playlist(p)

    # ---------- detail ----------

    def open_playlist(self, entry: api.PlaylistEntry) -> None:
        self.tracks_list.clear()
        self.detail_heading.setText(_line_heading(f"{entry.title} · loading…"))
        self.stack.setCurrentIndex(1)
        self.status_message.emit(f"loading {entry.title.lower()}…")

        thread = QThread(self)
        worker = _PlaylistDetailWorker(self.api, entry.playlist_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_detail)
        worker.failed.connect(self._on_detail_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._detail_thread = thread
        self._detail_worker = worker
        thread.start()

    def _on_detail(self, detail: api.PlaylistDetail) -> None:
        self._current_detail = detail
        marker = self._list_marker()
        self.detail_heading.setText(_line_heading(f"{detail.title} · {len(detail.tracks)}"))
        self.tracks_list.clear()
        for tr in detail.tracks:
            artist = (tr.artists or "").lower()
            title = (tr.title or "").lower()
            dur = tr.duration or ""
            label = f"{marker}{artist} — {title}"
            if dur:
                gap = max(2, 60 - len(label) - len(dur))
                label = f"{label}{' ' * gap}{dur}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, tr)
            self.tracks_list.addItem(item)
        self.status_message.emit(f"{detail.title.lower()} · {len(detail.tracks)} tracks")

    def _on_detail_failed(self, msg: str) -> None:
        self.detail_heading.setText(_line_heading("playlist load failed"))
        self.status_message.emit(f"playlist: {msg}")

    def _show_index(self) -> None:
        self.stack.setCurrentIndex(0)

    # ---------- track interactions ----------

    def _on_track_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            # double-click = play now without seeding radio (the playlist is the queue)
            self.play_now_requested.emit(tr, False)

    def _on_track_menu(self, pos) -> None:
        item = self.tracks_list.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.tracks_list)
        a_play = QAction("play now", menu)
        a_next = QAction("play next", menu)
        a_add  = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        a_play_from = QAction("play playlist from here", menu)
        for a in (a_play, a_next, a_add, a_radio, a_play_from):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_next.triggered.connect(lambda: self.queue_next_requested.emit(tr))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        a_play_from.triggered.connect(lambda: self._play_from_track(tr))
        menu.exec(self.tracks_list.viewport().mapToGlobal(pos))

    def _play_from_track(self, track: api.Track) -> None:
        if not self._current_detail:
            return
        tracks = list(self._current_detail.tracks)
        try:
            i = next(j for j, t in enumerate(tracks) if t.video_id == track.video_id)
        except StopIteration:
            return
        self.play_all_requested.emit(tracks[i:])

    def _on_play_all(self) -> None:
        if self._current_detail and self._current_detail.tracks:
            self.play_all_requested.emit(list(self._current_detail.tracks))

    def _on_shuffle_play(self) -> None:
        if not self._current_detail or not self._current_detail.tracks:
            return
        import random
        shuffled = list(self._current_detail.tracks)
        random.shuffle(shuffled)
        self.play_all_requested.emit(shuffled)

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme

    def _list_marker(self) -> str:
        return str(self._theme.t("layout", "list_marker", "> ")) if self._theme else "> "
