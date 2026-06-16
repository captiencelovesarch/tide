"""Explore view — YT Music's home as a stack of horizontal card shelves."""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming
from .card import Card, ShelfRow
from .widgets import BracketButton


def _line_heading(label: str, total: int = 60) -> str:
    styled = theming.styled_case(label)
    line = "─" * max(4, total - len(styled) - 6)
    return f"── {styled} {line}"


class _HomeWorker(QObject):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, api_obj: api.Api) -> None:
        super().__init__()
        self.api = api_obj

    def run(self) -> None:
        try:
            self.done.emit(self.api.get_home(limit=6))
        except Exception as exc:
            self.failed.emit(str(exc))


class ExploreView(QWidget):
    play_now_requested = Signal(object, bool)
    queue_add_requested = Signal(object)
    radio_requested = Signal(object)
    album_requested = Signal(object)          # AlbumEntry
    artist_requested = Signal(object)         # ArtistEntry
    playlist_requested = Signal(object)       # PlaylistEntry
    status_message = Signal(str)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._thread: QThread | None = None
        self._worker: _HomeWorker | None = None
        self._loaded = False
        self._build_ui()

    def _build_ui(self) -> None:
        self.heading = QLabel(_line_heading("explore"))
        self.heading.setProperty("class", "dim")
        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.clicked.connect(self.reload)

        top = QHBoxLayout()
        top.addWidget(self.heading, stretch=1)
        top.addWidget(self.refresh_btn)

        self._content = QWidget()
        self._content_col = QVBoxLayout(self._content)
        self._content_col.setContentsMargins(0, 0, 0, 0)
        self._content_col.setSpacing(10)
        self._content_col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self._content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        from . import scale as _scale
        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(10))
        root.addLayout(top)
        root.addWidget(scroll, stretch=1)

    def reload(self) -> None:
        self._loaded = False
        self._clear_content()
        if hasattr(self.api, "supports") and not self.api.supports("home"):
            src_name = theming.styled_case(getattr(self.api, "name", "this source"))
            self.heading.setText(_line_heading(f"explore · not available for {src_name}"))
            placeholder = QLabel(theming.styled_case(
                "  switch active source to one that surfaces a home shelf — "
                "youtube music or local files."
            ))
            placeholder.setWordWrap(True)
            # _content_col holds a stretch at the end; insert before it.
            self._content_col.insertWidget(self._content_col.count() - 1, placeholder)
            self._loaded = True
            return
        self.heading.setText(_line_heading("explore · loading…"))
        self.status_message.emit(theming.styled_case("loading explore…"))

        thread = QThread(self)
        worker = _HomeWorker(self.api)
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

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.reload()

    def _on_done(self, shelves: list[api.Shelf]) -> None:
        self._loaded = True
        self._clear_content()
        if not shelves:
            self.heading.setText(_line_heading("explore · nothing here"))
            return
        self.heading.setText(_line_heading(f"explore · {len(shelves)} shelves"))
        for s in shelves:
            self._add_shelf(s)
        self._content_col.addStretch(1)
        self.status_message.emit(theming.styled_case(f"explore · {len(shelves)} shelves"))

    def _on_failed(self, msg: str) -> None:
        self.heading.setText(_line_heading("explore · load failed"))
        self.status_message.emit(f"explore: {msg}")

    def _add_shelf(self, shelf: api.Shelf) -> None:
        label = QLabel(_line_heading(shelf.title))
        label.setProperty("class", "dim")
        self._content_col.addWidget(label)

        row = ShelfRow()
        for it in shelf.items:
            circular = (it.kind == "artist")
            c = Card(it.title, it.subtitle, it.thumbnail, it, circular=circular)
            c.clicked.connect(lambda item=it: self._dispatch_item(item))
            row.add_card(c)
        row.end_with_stretch()
        self._content_col.addWidget(row)

    def _dispatch_item(self, item: api.ShelfItem) -> None:
        if item.kind in ("song", "video") and item.track is not None:
            self.play_now_requested.emit(item.track, True)
        elif item.kind == "album" and item.album is not None:
            self.album_requested.emit(item.album)
        elif item.kind == "artist" and item.artist is not None:
            self.artist_requested.emit(item.artist)
        elif item.kind == "playlist" and item.playlist is not None:
            self.playlist_requested.emit(item.playlist)

    def _clear_content(self) -> None:
        while self._content_col.count():
            it = self._content_col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    def _on_theme(self, theme) -> None:
        self._theme = theme
