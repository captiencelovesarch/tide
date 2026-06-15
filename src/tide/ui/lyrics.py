"""Lyrics panel.

Plain (non-timed) lyrics from ytmusicapi. Auto-fetches when the current
track changes. Empty state and error state both render as ``── no lyrics ──``
or ``── no lyrics for this track ──`` so the panel never feels broken.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming


class _LyricsWorker(QObject):
    done = Signal(str, object)        # video_id, text or None
    failed = Signal(str, str)         # video_id, msg

    def __init__(self, api_obj: api.Api, video_id: str) -> None:
        super().__init__()
        self.api = api_obj
        self.video_id = video_id

    def run(self) -> None:
        try:
            text = self.api.get_lyrics_for(self.video_id)
            self.done.emit(self.video_id, text)
        except Exception as exc:
            self.failed.emit(self.video_id, str(exc))


def _line_heading(label: str, total: int = 60) -> str:
    line = "─" * max(4, total - len(label) - 6)
    return f"── {label.lower()} {line}"


class LyricsView(QWidget):
    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

        self._current_video_id: str | None = None
        self._thread: QThread | None = None
        self._worker: _LyricsWorker | None = None

        self.heading = QLabel(_line_heading("lyrics"))
        self.heading.setProperty("class", "dim")

        self.body = QLabel("── no track ──")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.body.setContentsMargins(0, 6, 0, 6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 8)
        layout.setSpacing(8)
        layout.addWidget(self.heading)
        layout.addWidget(scroll, stretch=1)

    # ---------- public ----------

    def show_for(self, track) -> None:
        if track is None:
            self._current_video_id = None
            self.heading.setText(_line_heading("lyrics"))
            self.body.setText("── no track ──")
            return
        # Skip refetch if it's the same track.
        if self._current_video_id == track.video_id:
            return
        self._current_video_id = track.video_id
        self.heading.setText(_line_heading(f"lyrics · {(track.title or '').lower()}"))
        self.body.setText("── loading ──")

        thread = QThread(self)
        worker = _LyricsWorker(self.api, track.video_id)
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

    def _on_done(self, video_id: str, text) -> None:
        if video_id != self._current_video_id:
            return
        if not text:
            self.body.setText("── no lyrics for this track ──")
            return
        self.body.setText(text)

    def _on_failed(self, video_id: str, _msg: str) -> None:
        if video_id != self._current_video_id:
            return
        self.body.setText("── lyrics unavailable ──")

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme
