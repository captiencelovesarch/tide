"""Artist detail view — header + Top songs + Albums + Singles + Related."""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming
from . import art_cache
from .card import Card, ShelfRow
from .track_row import TrackRowDelegate
from .widgets import BracketButton


HEADER_THUMB = 160


def _line_heading(label: str, total: int = 60) -> str:
    styled = theming.styled_case(label)
    line = "─" * max(4, total - len(styled) - 6)
    return f"── {styled} {line}"


class _ArtistWorker(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api, channel_id: str) -> None:
        super().__init__()
        self.api = api_obj
        self.channel_id = channel_id

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_artist(self.channel_id))
        except Exception as exc:
            self.failed.emit(str(exc))


class ArtistView(QWidget):
    back_requested = Signal()
    play_now_requested = Signal(object, bool)
    queue_add_requested = Signal(object)
    queue_next_requested = Signal(object)
    radio_requested = Signal(object)
    play_all_requested = Signal(list)
    album_requested = Signal(object)        # AlbumEntry
    artist_requested = Signal(object)       # ArtistEntry (related)
    status_message = Signal(str)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._thread: QThread | None = None
        self._worker: _ArtistWorker | None = None
        self._current: api.ArtistDetail | None = None
        self._current_cid: str | None = None
        self._art_for_cid: str | None = None
        self._build_ui()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        self.back_btn = BracketButton("back")
        self.back_btn.clicked.connect(self.back_requested.emit)
        self.heading = QLabel(_line_heading("artist"))
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.back_btn)
        top_bar.addWidget(self.heading, stretch=1)

        # ---- header ----
        from . import scale as _scale
        self.avatar = QLabel()
        self._avatar_size = _scale.px(HEADER_THUMB)
        self.avatar.setFixedSize(self._avatar_size, self._avatar_size)
        self.avatar.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); border-radius: %dpx; }"
            % (self._avatar_size // 2)
        )
        self.avatar.setAlignment(Qt.AlignCenter)

        self.name_label = QLabel("")
        self.name_label.setStyleSheet("font-weight: 600; font-size: 16pt;")
        self.name_label.setWordWrap(True)

        self.subs_label = QLabel("")
        self.subs_label.setProperty("class", "dim")

        self.description_label = QLabel("")
        self.description_label.setProperty("class", "dim")
        self.description_label.setWordWrap(True)
        self.description_label.setMaximumHeight(96)

        meta = QVBoxLayout()
        meta.setSpacing(4)
        meta.addWidget(self.name_label)
        meta.addWidget(self.subs_label)
        meta.addSpacing(6)
        meta.addWidget(self.description_label)
        meta.addStretch(1)

        header_row = QHBoxLayout()
        header_row.setSpacing(18)
        header_row.addWidget(self.avatar, alignment=Qt.AlignTop)
        header_row.addLayout(meta, stretch=1)

        # ---- top songs ----
        self.songs_heading = QLabel(_line_heading("top songs"))
        self.songs_heading.setProperty("class", "dim")
        self.songs = QListWidget()
        self.songs.setUniformItemSizes(True)
        self.songs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.songs.customContextMenuRequested.connect(self._on_song_menu)
        self.songs.itemActivated.connect(self._on_song_activated)
        self._delegate = TrackRowDelegate(self)
        self._delegate.attach(self.songs)
        self.songs.setItemDelegate(self._delegate)
        self.songs.setMaximumHeight(280)

        # ---- albums / singles / related (shelves) ----
        self.albums_heading = QLabel(_line_heading("albums"))
        self.albums_heading.setProperty("class", "dim")
        self.albums_row = ShelfRow()

        self.singles_heading = QLabel(_line_heading("singles"))
        self.singles_heading.setProperty("class", "dim")
        self.singles_row = ShelfRow()

        self.related_heading = QLabel(_line_heading("related artists"))
        self.related_heading.setProperty("class", "dim")
        self.related_row = ShelfRow()

        # ---- assemble (scrollable since content can be long) ----
        content = QWidget()
        content_col = QVBoxLayout(content)
        content_col.setContentsMargins(0, 0, 0, 0)
        content_col.setSpacing(8)
        content_col.addLayout(header_row)
        content_col.addSpacing(8)
        content_col.addWidget(self.songs_heading)
        content_col.addWidget(self.songs)
        content_col.addWidget(self.albums_heading)
        content_col.addWidget(self.albums_row)
        content_col.addWidget(self.singles_heading)
        content_col.addWidget(self.singles_row)
        content_col.addWidget(self.related_heading)
        content_col.addWidget(self.related_row)
        content_col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        from . import scale as _scale
        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(10))
        root.addLayout(top_bar)
        root.addWidget(scroll, stretch=1)

    # ---------- loading ----------

    def open_artist(self, channel_id: str, *, name_hint: str = "", thumbnail_hint: str = "") -> None:
        if not channel_id:
            return
        self._current = None
        self._current_cid = channel_id
        self._art_for_cid = channel_id
        self.heading.setText(_line_heading("artist · loading…"))
        self.name_label.setText(theming.styled_case(name_hint or "loading…"))
        self.subs_label.setText("")
        self.description_label.setText("")
        self.songs.clear()
        self.albums_row.clear()
        self.singles_row.clear()
        self.related_row.clear()
        if thumbnail_hint:
            self._fetch_avatar(channel_id, thumbnail_hint)
        else:
            self._set_avatar_placeholder()
        self.status_message.emit(theming.styled_case("loading artist…"))

        thread = QThread(self)
        worker = _ArtistWorker(self.api, channel_id)
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

    def _on_done(self, detail: api.ArtistDetail | None) -> None:
        if detail is None:
            self._on_failed("artist not found")
            return
        if self._current_cid != detail.channel_id:
            return
        self._current = detail

        self.heading.setText(_line_heading(f"artist · {detail.name}"))
        self.name_label.setText(theming.styled_case(detail.name))
        meta_parts = [p for p in (
            f"{detail.subscribers} subscribers" if detail.subscribers else "",
            f"{detail.monthly_listeners} monthly listeners" if detail.monthly_listeners else "",
        ) if p]
        self.subs_label.setText(theming.styled_case("  ·  ".join(meta_parts)))
        self.description_label.setText(theming.styled_case(detail.description or ""))
        self.description_label.setVisible(bool(detail.description))

        if detail.thumbnail:
            self._fetch_avatar(detail.channel_id, detail.thumbnail)

        # Top songs
        for tr in detail.top_songs:
            item = QListWidgetItem(theming.styled_case(f"{tr.artists or ''} — {tr.title or ''}"))
            item.setData(Qt.UserRole, tr)
            self.songs.addItem(item)

        # Albums + singles + related as card shelves
        for ent in detail.albums:
            self._add_card(self.albums_row, ent.title, ent.year or "album",
                           ent.thumbnail, ent, on_click=self.album_requested.emit)
        self.albums_row.end_with_stretch()

        for ent in detail.singles:
            self._add_card(self.singles_row, ent.title, ent.year or "single",
                           ent.thumbnail, ent, on_click=self.album_requested.emit)
        self.singles_row.end_with_stretch()

        for art in detail.related:
            self._add_card(self.related_row, art.name, "artist",
                           art.thumbnail, art, on_click=self.artist_requested.emit,
                           circular=True)
        self.related_row.end_with_stretch()

        self.status_message.emit(theming.styled_case(f"{detail.name}"))

    def _on_failed(self, msg: str) -> None:
        self.heading.setText(_line_heading("artist load failed"))
        self.status_message.emit(f"artist: {msg}")

    def _add_card(self, row: ShelfRow, title: str, subtitle: str, thumb: str, payload, *, on_click, circular: bool = False) -> None:
        c = Card(title, subtitle, thumb, payload, circular=circular)
        c.clicked.connect(on_click)
        row.add_card(c)

    # ---------- avatar ----------

    def _set_avatar_placeholder(self) -> None:
        self.avatar.setPixmap(QPixmap())
        self.avatar.setText(theming.styled_case("·"))

    def _fetch_avatar(self, cid: str, url: str) -> None:
        if not url:
            self._set_avatar_placeholder()
            return
        img = art_cache.cache().get(url)
        if img is not None:
            self._show_avatar(img)
            return

        def on_loaded(loaded: QImage | None) -> None:
            if self._art_for_cid != cid:
                return
            if loaded is not None:
                self._show_avatar(loaded)
        art_cache.cache().request(url, on_loaded)
        self._set_avatar_placeholder()

    def _show_avatar(self, img: QImage) -> None:
        # Scale per the active ui_scale to match the avatar QLabel's size.
        pix = QPixmap.fromImage(img).scaled(
            self._avatar_size, self._avatar_size,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        self.avatar.setText("")
        self.avatar.setPixmap(pix)

    # ---------- track interactions ----------

    def _on_song_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            self.play_now_requested.emit(tr, True)

    def _on_song_menu(self, pos) -> None:
        item = self.songs.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.songs)
        a_play = QAction("play now", menu)
        a_next = QAction("play next", menu)
        a_add = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        for a in (a_play, a_next, a_add, a_radio):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_next.triggered.connect(lambda: self.queue_next_requested.emit(tr))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        menu.exec(self.songs.viewport().mapToGlobal(pos))

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme
