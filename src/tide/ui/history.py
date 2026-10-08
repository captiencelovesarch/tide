"""History view: recently-played tracks, newest first.

v1.5 makes it double-sided. [tide] is the local jsonl — every play this
app ever made, source-agnostic. [youtube] is the account's own history
from the active source (``remote_history`` capability): plays from the
phone, the web, everywhere — with per-row removal, since YT hands back a
feedbackToken per item. With play reporting on, the two sides converge;
that's the point.
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
    QVBoxLayout,
    QWidget,
)

from .. import api, history, qthreads, theming
from .headings import Heading, line_heading as _line_heading
from .track_row import TrackRowDelegate
from .widgets import BracketButton


class _RemoteHistoryWorker(QObject):
    done = Signal(int, list)
    failed = Signal(int, str)

    def __init__(self, api_obj, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.api.get_remote_history())
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _RemoveRemoteWorker(QObject):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, api_obj, track) -> None:
        super().__init__()
        self.api = api_obj
        self.track = track

    def run(self) -> None:
        try:
            if self.api.remove_remote_history([self.track]):
                self.done.emit(self.track.title or "item")
            else:
                self.failed.emit("source refused the removal")
        except Exception as exc:
            self.failed.emit(str(exc))


class HistoryView(QWidget):
    play_now_requested = Signal(object, bool)   # Track, seed_radio
    queue_add_requested = Signal(object)
    radio_requested = Signal(object)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        # The remote side needs a source; window attaches it after
        # construction (same late-bind pattern as _settings).
        self.api = None
        self._side = "tide"
        self._remote_gen = 0
        self._build_ui()

    def _build_ui(self) -> None:
        self.heading = Heading("history")
        self.heading.setProperty("class", "dim")
        self.heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.tab_tide = BracketButton("tide")
        self.tab_remote = BracketButton("youtube")
        self.tab_tide.clicked.connect(lambda: self._set_side("tide"))
        self.tab_remote.clicked.connect(lambda: self._set_side("remote"))
        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.setIconKey("refresh")
        self.refresh_btn.clicked.connect(self.reload)
        self.clear_btn = BracketButton("clear")
        self.clear_btn.clicked.connect(self._on_clear)

        actions = QHBoxLayout()
        actions.addWidget(self.tab_tide)
        actions.addWidget(self.tab_remote)
        actions.addSpacing(12)
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
        self._refresh_tab_buttons()

    # ---------- sides ----------

    def _remote_supported(self) -> bool:
        return bool(self.api is not None
                    and hasattr(self.api, "supports")
                    and self.api.supports("remote_history"))

    def _refresh_tab_buttons(self) -> None:
        remote_ok = self._remote_supported()
        self.tab_remote.setVisible(remote_ok)
        self.tab_tide.setVisible(remote_ok)   # solo tab = no tabs at all
        self.tab_tide.setActiveState(self._side == "tide")
        self.tab_remote.setActiveState(self._side == "remote")
        # Local clear only applies to the local side.
        self.clear_btn.setVisible(self._side == "tide")

    def _set_side(self, side: str) -> None:
        if side == "remote" and not self._remote_supported():
            side = "tide"
        if side == self._side:
            return
        self._side = side
        self._refresh_tab_buttons()
        self.reload()

    # ---------- loading ----------

    def reload(self) -> None:
        self._refresh_tab_buttons()
        if self._side == "remote":
            self._reload_remote()
            return
        entries = history.read_recent()
        self.heading.set_label(f"history · {len(entries)}")
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

    def _reload_remote(self) -> None:
        self._remote_gen += 1
        self.list.clear()
        self.heading.set_label("history · youtube · loading…")
        thread = QThread()
        worker = _RemoteHistoryWorker(self.api, self._remote_gen)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_remote)
        worker.failed.connect(self._on_remote_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    def _on_remote(self, gen: int, tracks: list) -> None:
        if gen != self._remote_gen or self._side != "remote":
            return
        self.heading.set_label(f"history · youtube · {len(tracks)}")
        self.list.clear()
        marker = self._list_marker()
        for tr in tracks:
            artist = theming.styled_case(tr.artists or "")
            title = theming.styled_case(tr.title or "")
            # get_history rows carry a period label ("Today", "March 2026").
            period = str((tr.extras or {}).get("played") or "")
            label = f"{marker}{artist} — {title}"
            if period:
                gap = max(2, 60 - len(label) - len(period))
                label = f"{label}{' ' * gap}{theming.styled_case(period)}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, tr)
            self.list.addItem(item)

    def _on_remote_failed(self, gen: int, msg: str) -> None:
        if gen != self._remote_gen or self._side != "remote":
            return
        self.heading.set_label("history · youtube · failed")
        self.status_message.emit(f"youtube history: {msg}")

    # ---------- interactions ----------

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
        menu.setAttribute(Qt.WA_DeleteOnClose)
        a_play = QAction("play now", menu)
        a_add  = QAction("add to queue", menu)
        a_radio = QAction("start radio from here", menu)
        for a in (a_play, a_add, a_radio):
            menu.addAction(a)
        a_play.triggered.connect(lambda: self.play_now_requested.emit(tr, False))
        a_add.triggered.connect(lambda: self.queue_add_requested.emit(tr))
        a_radio.triggered.connect(lambda: self.radio_requested.emit(tr))
        if (self._side == "remote" and self._remote_supported()
                and (tr.extras or {}).get("feedbackToken")):
            menu.addSeparator()
            a_remove = QAction("remove from youtube history", menu)
            menu.addAction(a_remove)
            a_remove.triggered.connect(lambda: self._remove_remote(tr, item))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _remove_remote(self, tr, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        thread = QThread()
        worker = _RemoveRemoteWorker(self.api, tr)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(lambda title: self._on_removed(title, row))
        worker.failed.connect(
            lambda msg: self.status_message.emit(f"couldn't remove: {msg}"))
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    def _on_removed(self, title: str, row: int) -> None:
        if self._side == "remote" and 0 <= row < self.list.count():
            self.list.takeItem(row)
        self.status_message.emit(
            theming.styled_case(f"removed {title} from youtube history"))

    def _on_clear(self) -> None:
        history.clear()
        self.reload()
        self.status_message.emit("history cleared")

    def _on_theme(self, theme) -> None:
        self._theme = theme

    def _list_marker(self) -> str:
        return str(self._theme.t("layout", "list_marker", "> ")) if self._theme else "> "
