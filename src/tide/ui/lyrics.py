"""Lyrics panel — plain text + optional timed (karaoke) mode.

Two paths:

  - **untimed**: render the full block in a scrollable label, like before.
  - **timed**: render each line as its own widget so we can highlight the
    active one (bolder + larger + accent color) and scroll it into view
    as playback advances.

LRClib is the timed-lyrics fallback when YT Music has none.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal, QTimer
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, theming


class _LyricsWorker(QObject):
    done = Signal(str, object)        # video_id, LyricsResult | None
    failed = Signal(str, str)

    def __init__(self, api_obj: api.Api, track: api.Track) -> None:
        super().__init__()
        self.api = api_obj
        self.track = track

    def run(self) -> None:
        try:
            self.done.emit(self.track.video_id, self.api.get_lyrics_for_track(self.track))
        except Exception as exc:
            self.failed.emit(self.track.video_id, str(exc))


def _line_heading(label: str, total: int = 60) -> str:
    styled = theming.styled_case(label)
    line = "─" * max(4, total - len(styled) - 6)
    return f"── {styled} {line}"


class _LineLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setAlignment(Qt.AlignLeft)


class LyricsView(QWidget):
    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

        self._current_video_id: str | None = None
        self._thread: QThread | None = None
        self._worker: _LyricsWorker | None = None

        # Timed state
        self._timed_lines: list[tuple[float, str]] = []
        self._line_widgets: list[_LineLabel] = []
        self._active_line_index: int = -1

        self.heading = QLabel(_line_heading("lyrics"))
        self.heading.setProperty("class", "dim")

        # Container we swap between the plain QLabel and the timed line list.
        self._plain_label = QLabel("── no track ──")
        self._plain_label.setWordWrap(True)
        self._plain_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._plain_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._plain_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._plain_label.setContentsMargins(0, 6, 0, 6)

        self._timed_host = QWidget()
        self._timed_layout = QVBoxLayout(self._timed_host)
        self._timed_layout.setContentsMargins(0, 6, 0, 6)
        self._timed_layout.setSpacing(4)
        self._timed_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setWidget(self._plain_label)

        from . import scale as _scale
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        layout.setSpacing(_scale.px(8))
        layout.addWidget(self.heading)
        layout.addWidget(self._scroll, stretch=1)

    # ---------- public ----------

    def show_for(self, track) -> None:
        if track is None:
            self._current_video_id = None
            self.heading.setText(_line_heading("lyrics"))
            self._show_plain("── no track ──")
            self._clear_timed()
            return
        if self._current_video_id == track.video_id:
            return
        self._current_video_id = track.video_id
        self.heading.setText(_line_heading(f"lyrics · {(track.title or '').lower()}"))
        self._show_plain("── loading ──")
        self._clear_timed()

        thread = QThread(self)
        worker = _LyricsWorker(self.api, track)
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

    def update_position(self, seconds: float) -> None:
        if not self._timed_lines:
            return
        # Find latest line whose timestamp <= seconds.
        idx = -1
        for i, (t, _line) in enumerate(self._timed_lines):
            if t <= seconds:
                idx = i
            else:
                break
        if idx == self._active_line_index:
            return
        self._active_line_index = idx
        self._restyle_lines()
        if 0 <= idx < len(self._line_widgets):
            target = self._line_widgets[idx]
            self._scroll.ensureWidgetVisible(target, 0, max(80, self._scroll.viewport().height() // 3))

    # ---------- async result handling ----------

    def _on_done(self, video_id: str, result) -> None:
        if video_id != self._current_video_id:
            return
        from ..lyrics_provider import LyricsResult
        if not isinstance(result, LyricsResult):
            self._show_plain("── no lyrics for this track ──")
            return
        if result.has_timed:
            self._show_timed(result.timed_lines)
        elif result.plain_text:
            self._show_plain(result.plain_text)
        else:
            self._show_plain("── no lyrics for this track ──")

    def _on_failed(self, video_id: str, _msg: str) -> None:
        if video_id != self._current_video_id:
            return
        self._show_plain("── lyrics unavailable ──")

    # ---------- render modes ----------

    def _show_plain(self, text: str) -> None:
        self._timed_lines = []
        self._active_line_index = -1
        self._plain_label.setText(text)
        self._scroll.takeWidget()
        self._scroll.setWidget(self._plain_label)

    def _show_timed(self, lines: list[tuple[float, str]]) -> None:
        self._timed_lines = lines
        self._active_line_index = -1
        self._clear_timed()
        for _t, text in lines:
            lbl = _LineLabel(text or " ", self._timed_host)
            self._line_widgets.append(lbl)
            self._timed_layout.insertWidget(self._timed_layout.count() - 1, lbl)
        self._restyle_lines()
        self._scroll.takeWidget()
        self._scroll.setWidget(self._timed_host)

    def _clear_timed(self) -> None:
        for w in self._line_widgets:
            w.deleteLater()
        self._line_widgets = []

    def _restyle_lines(self) -> None:
        theme = self._theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        for i, lbl in enumerate(self._line_widgets):
            if i == self._active_line_index:
                lbl.setStyleSheet(
                    f"color: {accent}; background: transparent; "
                    f"font-weight: 700; font-size: 13pt; padding: 4px 0;"
                )
            elif i < self._active_line_index:
                lbl.setStyleSheet(
                    f"color: {dim}; background: transparent; "
                    f"font-size: 10pt; padding: 2px 0;"
                )
            else:
                lbl.setStyleSheet(
                    f"color: {fg}; background: transparent; "
                    f"font-size: 10pt; padding: 2px 0;"
                )

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme
        if self._line_widgets:
            self._restyle_lines()
