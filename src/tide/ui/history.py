"""History view: recently-played tracks, newest first."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
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

from .. import api, history, theming
from .track_row import TrackRowDelegate
from .widgets import BracketButton


def _line_heading(label: str, total: int = 60) -> str:
    styled = theming.styled_case(label)
    line = "─" * max(4, total - len(styled) - 6)
    return f"── {styled} {line}"


class HistoryView(QWidget):
    play_now_requested = Signal(object, bool)   # Track, seed_radio
    queue_add_requested = Signal(object)
    radio_requested = Signal(object)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._build_ui()

    def _build_ui(self) -> None:
        self.heading = QLabel(_line_heading("history"))
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.clicked.connect(self.reload)
        self.clear_btn = BracketButton("clear")
        self.clear_btn.clicked.connect(self._on_clear)

        actions = QHBoxLayout()
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.clear_btn)
        actions.addStretch(1)

        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_menu)
        self.list.itemActivated.connect(self._on_activated)
        self._delegate = TrackRowDelegate(self)
        self._delegate.attach(self.list)
        self.list.setItemDelegate(self._delegate)

        from . import scale as _scale
        col = QVBoxLayout(self)
        col.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        col.setSpacing(_scale.px(8))
        col.addWidget(self.heading)
        col.addLayout(actions)
        col.addWidget(self.list, stretch=1)

    def reload(self) -> None:
        entries = history.read_recent()
        self.heading.setText(_line_heading(f"history · {len(entries)}"))
        self.list.clear()
        marker = self._list_marker()
        for e in entries:
            artist = theming.styled_case(e.artists or "")
            title = theming.styled_case(e.title or "")
            dur = e.duration or ""
            label = f"{marker}{artist} — {title}"
            if dur:
                gap = max(2, 60 - len(label) - len(dur))
                label = f"{label}{' ' * gap}{dur}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, e.to_track())
            self.list.addItem(item)

    def _on_activated(self, item: QListWidgetItem) -> None:
        tr: api.Track = item.data(Qt.UserRole)
        if tr:
            self.play_now_requested.emit(tr, True)

    def _on_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if not item:
            return
        tr: api.Track = item.data(Qt.UserRole)
        if not tr:
            return
        menu = QMenu(self.list)
        a_play = QAction("play now", menu)
        a_add  = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        for a in (a_play, a_add, a_radio):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _on_clear(self) -> None:
        history.clear()
        self.reload()
        self.status_message.emit("history cleared")

    def _on_theme(self, theme) -> None:
        self._theme = theme

    def _list_marker(self) -> str:
        return str(self._theme.t("layout", "list_marker", "> ")) if self._theme else "> "
