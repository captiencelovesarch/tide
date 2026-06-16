"""Album detail view — cover header + track list.

Reachable from:
  - clicking an album card on Explore
  - clicking an album in the [albums] search tab
  - opening from an Artist page's discography
  - right-click → "view album" on any track that has one

Signals bubble up to MainWindow for playback actions, same shape as the
LibraryView / HistoryView pattern.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QSize, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming
from . import art_cache
from .track_row import TrackRowDelegate
from .widgets import BracketButton


COVER_SIZE = 200


def _line_heading(label: str, total: int = 60) -> str:
    styled = theming.styled_case(label)
    line = "─" * max(4, total - len(styled) - 6)
    return f"── {styled} {line}"


class _AlbumWorker(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api, browse_id: str) -> None:
        super().__init__()
        self.api = api_obj
        self.browse_id = browse_id

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_album(self.browse_id))
        except Exception as exc:
            self.failed.emit(str(exc))


class AlbumView(QWidget):
    back_requested = Signal()
    play_now_requested = Signal(object, bool)
    queue_add_requested = Signal(object)
    queue_next_requested = Signal(object)
    radio_requested = Signal(object)
    play_all_requested = Signal(list)
    artist_requested = Signal(str)               # channel_id
    status_message = Signal(str)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._thread: QThread | None = None
        self._worker: _AlbumWorker | None = None
        self._current: api.AlbumDetail | None = None
        self._current_browse_id: str | None = None
        self._art_for_browse_id: str | None = None
        self._net = QNetworkAccessManager(self)
        self._build_ui()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        self.back_btn = BracketButton("back")
        self.back_btn.clicked.connect(self.back_requested.emit)

        self.heading = QLabel(_line_heading("album"))
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.back_btn)
        top_bar.addWidget(self.heading, stretch=1)

        # ---- header ----
        from . import scale as _scale
        self.cover = QLabel()
        self._cover_size = _scale.px(COVER_SIZE)
        self.cover.setFixedSize(self._cover_size, self._cover_size)
        self.cover.setStyleSheet("QLabel { border: 1px solid palette(mid); }")
        self.cover.setAlignment(Qt.AlignCenter)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 14pt;")
        self.title_label.setWordWrap(True)

        self.artist_label = QLabel("")
        self.artist_label.setProperty("class", "dim")
        self.artist_label.setCursor(Qt.PointingHandCursor)
        self.artist_label.mousePressEvent = self._on_artist_click   # type: ignore[assignment]

        self.meta_label = QLabel("")
        self.meta_label.setProperty("class", "dim")

        self.description_label = QLabel("")
        self.description_label.setProperty("class", "dim")
        self.description_label.setWordWrap(True)
        self.description_label.setMaximumHeight(96)

        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 0, 0, 0)
        meta_col.setSpacing(4)
        meta_col.addWidget(self.title_label)
        meta_col.addWidget(self.artist_label)
        meta_col.addWidget(self.meta_label)
        meta_col.addSpacing(8)
        meta_col.addWidget(self.description_label)
        meta_col.addStretch(1)

        header_row = QHBoxLayout()
        header_row.setSpacing(18)
        header_row.addWidget(self.cover, alignment=Qt.AlignTop)
        header_row.addLayout(meta_col, stretch=1)

        # ---- actions ----
        self.play_all_btn = BracketButton("play all")
        self.shuffle_btn = BracketButton("shuffle")
        self.play_all_btn.clicked.connect(self._on_play_all)
        self.shuffle_btn.clicked.connect(self._on_shuffle)

        actions_row = QHBoxLayout()
        actions_row.addWidget(self.play_all_btn)
        actions_row.addWidget(self.shuffle_btn)
        actions_row.addStretch(1)

        # ---- track list ----
        self.tracks = QListWidget()
        self.tracks.setUniformItemSizes(True)
        self.tracks.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tracks.customContextMenuRequested.connect(self._on_track_menu)
        self.tracks.itemActivated.connect(self._on_track_activated)
        self._delegate = TrackRowDelegate(self)
        self._delegate.attach(self.tracks)
        self.tracks.setItemDelegate(self._delegate)

        # ---- assemble ----
        from . import scale as _scale
        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(10))
        root.addLayout(top_bar)
        root.addLayout(header_row)
        root.addLayout(actions_row)
        root.addWidget(self.tracks, stretch=1)

    # ---------- loading ----------

    def open_album(self, browse_id: str, *, title_hint: str = "", thumbnail_hint: str = "") -> None:
        if not browse_id:
            return
        self._current = None
        self._current_browse_id = browse_id
        self._art_for_browse_id = browse_id
        self.heading.setText(_line_heading(f"album · loading…"))
        self.title_label.setText(theming.styled_case(title_hint or "loading…"))
        self.artist_label.setText("")
        self.meta_label.setText("")
        self.description_label.setText("")
        self.tracks.clear()
        if thumbnail_hint:
            self._fetch_cover(browse_id, thumbnail_hint)
        else:
            self._set_cover_placeholder()
        self.status_message.emit(theming.styled_case(f"loading album…"))

        thread = QThread(self)
        worker = _AlbumWorker(self.api, browse_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_done(self, detail: api.AlbumDetail | None) -> None:
        if detail is None:
            self._on_failed("album not found")
            return
        if self._current_browse_id != detail.browse_id:
            return  # user already navigated away
        self._current = detail

        self.heading.setText(_line_heading(f"album · {len(detail.tracks)}"))
        self.title_label.setText(theming.styled_case(detail.title))
        self.artist_label.setText(theming.styled_case(detail.artists or ""))
        meta_parts = [p for p in (detail.year, detail.duration, f"{detail.track_count} tracks") if p]
        self.meta_label.setText(theming.styled_case("  ·  ".join(meta_parts)))
        self.description_label.setText(theming.styled_case(detail.description or ""))
        self.description_label.setVisible(bool(detail.description))

        if detail.thumbnail:
            self._fetch_cover(detail.browse_id, detail.thumbnail)

        self.tracks.clear()
        for tr in detail.tracks:
            item = QListWidgetItem(theming.styled_case(f"{tr.artists or ''} — {tr.title or ''}"))
            item.setData(Qt.UserRole, tr)
            self.tracks.addItem(item)
        self.status_message.emit(theming.styled_case(f"{detail.title} · {len(detail.tracks)} tracks"))

    def _on_failed(self, msg: str) -> None:
        self.heading.setText(_line_heading("album load failed"))
        self.status_message.emit(f"album: {msg}")

    # ---------- cover art ----------

    def _set_cover_placeholder(self) -> None:
        self.cover.setPixmap(QPixmap())
        self.cover.setText(theming.styled_case("[ art ]"))

    def _fetch_cover(self, browse_id: str, url: str) -> None:
        if not url:
            self._set_cover_placeholder()
            return
        img = art_cache.cache().get(url)
        if img is not None:
            self._show_cover(img)
            return

        def on_loaded(loaded: QImage | None) -> None:
            if self._art_for_browse_id != browse_id:
                return
            if loaded is not None:
                self._show_cover(loaded)
        art_cache.cache().request(url, on_loaded)
        self._set_cover_placeholder()

    def _show_cover(self, img: QImage) -> None:
        # Scale per the active ui_scale rather than the base constant — the
        # cover label was sized to match at construction.
        pix = QPixmap.fromImage(img).scaled(
            self._cover_size, self._cover_size,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        self.cover.setText("")
        self.cover.setPixmap(pix)

    # ---------- track interactions ----------

    def _on_track_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            self.play_now_requested.emit(tr, False)

    def _on_track_menu(self, pos) -> None:
        item = self.tracks.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.tracks)
        a_play = QAction("play now", menu)
        a_next = QAction("play next", menu)
        a_add = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        a_play_from = QAction("play album from here", menu)
        for a in (a_play, a_next, a_add, a_radio, a_play_from):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_next.triggered.connect(lambda: self.queue_next_requested.emit(tr))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        a_play_from.triggered.connect(lambda: self._play_from_track(tr))
        menu.exec(self.tracks.viewport().mapToGlobal(pos))

    def _play_from_track(self, track: api.Track) -> None:
        if not self._current:
            return
        tracks = list(self._current.tracks)
        try:
            i = next(j for j, t in enumerate(tracks) if t.video_id == track.video_id)
        except StopIteration:
            return
        self.play_all_requested.emit(tracks[i:])

    def _on_play_all(self) -> None:
        if self._current and self._current.tracks:
            self.play_all_requested.emit(list(self._current.tracks))

    def _on_shuffle(self) -> None:
        if not self._current or not self._current.tracks:
            return
        import random
        shuffled = list(self._current.tracks)
        random.shuffle(shuffled)
        self.play_all_requested.emit(shuffled)

    def _on_artist_click(self, _ev) -> None:
        # Album response doesn't carry the artist's channelId, so we have to
        # look it up via search. The window owns navigation, so it'll dispatch.
        if not self._current or not self._current.artists:
            return
        self.artist_requested.emit(self._current.artists)

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme
