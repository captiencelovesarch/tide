"""Lyrics panel — plain text + timed (synced) + karaoke modes.

Three render paths:

  - **untimed**: render the full block in a scrollable label, like before.
  - **timed (default)**: render each line as its own widget so we can
    highlight the active one (bolder + larger + accent color) and scroll
    it into view as playback advances.
  - **karaoke** (v1.2.1): toggleable from a checkbox at the top of this
    view. Big-center current line with per-word highlight that
    advances interpolated against the line's duration; prev/next lines
    shown small + dim above and below.

LRClib is the timed-lyrics fallback when YT Music has none. Word-level
LRC annotations (``<mm:ss.cc>word``) are rare on LRClib in practice, so
karaoke mode interpolates word position from the line's duration window
— good enough to feel right at speech tempo.
"""
from __future__ import annotations

import html

from PySide6.QtCore import QObject, QThread, Qt, Signal, QTimer
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, qthreads, theming


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
        # Lyric text is remote (LRClib / YT Music). QLabel's default AutoText
        # would render a line like "<b><font size=40>…" as HTML; force
        # PlainText so lines always display literally. (The karaoke path
        # builds its own escaped rich text separately.)
        self.setTextFormat(Qt.PlainText)


class _KaraokeWidget(QWidget):
    """Big-center karaoke rendering. Stack of three lines (prev, current,
    next) with the current one bigger, centered, and per-word
    accent-highlighted as time advances through its duration window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lines: list[tuple[float, str]] = []
        self._theme = theming.manager().current()
        self._active_idx: int = -1
        self._current_secs: float = 0.0

        self.prev_label = QLabel(" ")
        self.current_label = QLabel(" ")
        self.next_label = QLabel(" ")

        for lbl in (self.prev_label, self.current_label, self.next_label):
            lbl.setWordWrap(True)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setTextFormat(Qt.RichText)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(18)
        col.addStretch(1)
        col.addWidget(self.prev_label)
        col.addWidget(self.current_label)
        col.addWidget(self.next_label)
        col.addStretch(1)

        self._apply_styles()

    def set_lines(self, lines: list[tuple[float, str]]) -> None:
        self._lines = list(lines)
        self._active_idx = -1
        self._render()

    def set_active_index(self, idx: int, current_secs: float) -> None:
        if idx == self._active_idx and abs(current_secs - self._current_secs) < 0.05:
            return
        self._active_idx = idx
        self._current_secs = current_secs
        self._render()

    def set_theme(self, theme) -> None:
        self._theme = theme
        self._apply_styles()
        self._render()

    # ---------- internals ----------

    def _apply_styles(self) -> None:
        theme = self._theme
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        self.prev_label.setStyleSheet(
            f"color: {dim}; background: transparent; font-size: 12pt; padding: 0;"
        )
        self.next_label.setStyleSheet(
            f"color: {dim}; background: transparent; font-size: 12pt; padding: 0;"
        )
        # The current line's styling is set per-render because we paint
        # the active word in accent and the rest in fg via rich-text spans.
        self.current_label.setStyleSheet(
            f"color: {fg}; background: transparent; font-size: 28pt; "
            f"font-weight: 700; padding: 12px 0; letter-spacing: 0.02em;"
        )

    def _render(self) -> None:
        idx = self._active_idx
        n = len(self._lines)
        prev_text = self._lines[idx - 1][1] if 0 < idx < n else ""
        cur_text = self._lines[idx][1] if 0 <= idx < n else ""
        next_text = self._lines[idx + 1][1] if 0 <= idx < n - 1 else ""

        self.prev_label.setText(html.escape(prev_text or " "))
        self.next_label.setText(html.escape(next_text or " "))

        # Active-word interpolation: find which word within the current
        # line should be highlighted given how far we are through the
        # line's duration window. Two refinements on top of the naive
        # "split duration / word count":
        #   1) Character-weighted distribution. "Mississippi" and "is"
        #      shouldn't get the same time slice — they don't get sung
        #      for the same length. We weight each word's time share
        #      by its character count, which approximates syllable
        #      count well enough for English-language pop.
        #   2) Clamped sing window. LRC lines don't have an end time;
        #      we only know when the NEXT line starts. If there's a
        #      long instrumental break between lines, the naive math
        #      drags the highlight across each word for seconds at a
        #      time. Cap the effective duration at 4.5s OR a per-char
        #      budget of 0.35s, whichever is larger.
        #   3) Forward bias of 120ms — LRClib timestamps tend to land
        #      slightly *after* the singer's actual word, biasing the
        #      perceived timing as "too late". Nudging the highlight
        #      forward by a small fraction matches what feels natural
        #      on most tracks.
        if not cur_text.strip() or idx < 0 or idx >= n:
            self.current_label.setText(html.escape(cur_text or " "))
            return
        line_start = float(self._lines[idx][0])
        line_end = float(self._lines[idx + 1][0]) if (idx + 1) < n else line_start + 4.0

        words = cur_text.split()
        if not words:
            self.current_label.setText(html.escape(cur_text or " "))
            return

        char_budget = max(2, sum(len(w) for w in words))
        max_sing_window = max(4.5, 0.35 * char_budget)
        line_dur = max(0.4, min(line_end - line_start, max_sing_window))

        elapsed = max(0.0, self._current_secs - line_start + 0.12)

        # Build a cumulative character-weighted timeline: word i becomes
        # active when elapsed crosses `cumulative_chars[i] / total_chars *
        # line_dur`. This is much closer to how words are actually sung
        # than uniform time slicing.
        active = 0
        accum = 0
        for i, w in enumerate(words):
            accum += max(1, len(w))
            threshold = (accum / char_budget) * line_dur
            if elapsed >= threshold:
                active = i + 1
        active = max(0, min(len(words) - 1, active))

        theme = self._theme
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        parts: list[str] = []
        for i, w in enumerate(words):
            esc = html.escape(w)
            if i == active:
                parts.append(f"<span style='color:{accent};'>{esc}</span>")
            elif i < active:
                # past words: slight dim so the "wave" of advance is visible
                parts.append(f"<span style='opacity:0.75;'>{esc}</span>")
            else:
                parts.append(esc)
        self.current_label.setText(" ".join(parts))


class LyricsView(QWidget):
    # Emitted whenever the user toggles the [mute lyrics] button. The
    # owning MainWindow searches for an instrumental version of the
    # given track and drives the swap. `want_instrumental` is True when
    # the button is now checked (swap to instrumental); False when the
    # user toggled it off (swap back to vocal).
    toggle_instrumental_requested = Signal(object, bool)   # track, want_instrumental

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

        self._current_video_id: str | None = None
        self._current_track = None
        self._thread: QThread | None = None
        self._worker: _LyricsWorker | None = None

        # Timed state
        self._timed_lines: list[tuple[float, str]] = []
        self._line_widgets: list[_LineLabel] = []
        self._active_line_index: int = -1
        self._karaoke_mode: bool = False
        # Last known playback position, used so karaoke can re-render
        # smoothly when the user toggles in mid-line.
        self._last_position: float = 0.0

        self.heading = QLabel(_line_heading("lyrics"))
        self.heading.setProperty("class", "dim")
        # Heading embeds the remote track title (see show_for); plain-text it.
        self.heading.setTextFormat(Qt.PlainText)

        # Toggles bar — karaoke mode + (v1.2.1 second-half) mute-lyrics
        # instrumental-swap button. Lives in this view per the spec, NOT
        # on the main playback control bar.
        self.karaoke_check = QCheckBox(theming.styled_case("karaoke mode"))
        self.karaoke_check.toggled.connect(self._on_karaoke_toggled)
        # "mute lyrics" — swap to instrumental version of current track.
        # Stays disabled until a track is loaded and the instrumental
        # search infrastructure (P3.5) is wired in.
        self.mute_btn = QPushButton(theming.styled_case("[mute lyrics]"))
        self.mute_btn.setCheckable(True)
        self.mute_btn.setEnabled(False)
        self.mute_btn.clicked.connect(self._on_mute_lyrics_clicked)
        self.swap_status = QLabel("")
        self.swap_status.setProperty("class", "dim")
        self.swap_status.setWordWrap(True)

        toggles_row = QHBoxLayout()
        toggles_row.setContentsMargins(0, 0, 0, 0)
        toggles_row.setSpacing(12)
        toggles_row.addWidget(self.karaoke_check)
        toggles_row.addWidget(self.mute_btn)
        toggles_row.addStretch(1)

        # Container we swap between the plain QLabel and the timed line list.
        self._plain_label = QLabel("── no track ──")
        self._plain_label.setWordWrap(True)
        self._plain_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._plain_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._plain_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._plain_label.setContentsMargins(0, 6, 0, 6)
        # Untimed lyrics are a remote text block — plain-text it so markup in
        # the lyrics can't render as HTML.
        self._plain_label.setTextFormat(Qt.PlainText)

        self._timed_host = QWidget()
        self._timed_layout = QVBoxLayout(self._timed_host)
        self._timed_layout.setContentsMargins(0, 6, 0, 6)
        self._timed_layout.setSpacing(4)
        self._timed_layout.addStretch(1)

        self._karaoke_widget = _KaraokeWidget()
        # Named so the adaptive-background QSS transparentizes this container
        # (a QWidget subclass, so it isn't caught by the `.QWidget` rule) and
        # the gradient shows behind the karaoke lines. See theming._CONTENT_BACKDROP_QSS.
        self._karaoke_widget.setObjectName("lyricsKaraoke")

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setWidget(self._plain_label)

        from . import scale as _scale
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        layout.setSpacing(_scale.px(8))
        layout.addWidget(self.heading)
        layout.addLayout(toggles_row)
        layout.addWidget(self.swap_status)
        layout.addWidget(self._scroll, stretch=1)

    # ---------- public ----------

    def show_for(self, track) -> None:
        if track is None:
            self._current_video_id = None
            self._current_track = None
            self.heading.setText(_line_heading("lyrics"))
            self._show_plain("── no track ──")
            self._clear_timed()
            self.mute_btn.setEnabled(False)
            return
        if self._current_video_id == track.video_id:
            return
        self._current_video_id = track.video_id
        self._current_track = track
        self.heading.setText(_line_heading(f"lyrics · {(track.title or '').lower()}"))
        self._show_plain("── loading ──")
        self._clear_timed()
        # Enable the swap toggle now that there's a real track. The
        # button stays a no-op until the instrumental searcher wires
        # in — clicking it surfaces a "not wired yet" status until then.
        self.mute_btn.setEnabled(True)

        thread = QThread()
        worker = _LyricsWorker(self.api, track)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def update_position(self, seconds: float) -> None:
        self._last_position = float(seconds)
        if not self._timed_lines:
            return
        # Find latest line whose timestamp <= seconds.
        idx = -1
        for i, (t, _line) in enumerate(self._timed_lines):
            if t <= seconds:
                idx = i
            else:
                break
        # Karaoke wants per-tick word interpolation even when the line
        # didn't change, so it gets fed every position update.
        if self._karaoke_mode:
            self._karaoke_widget.set_active_index(idx, float(seconds))
        if idx == self._active_line_index:
            return
        self._active_line_index = idx
        if not self._karaoke_mode:
            self._restyle_lines()
            if 0 <= idx < len(self._line_widgets):
                target = self._line_widgets[idx]
                self._scroll.ensureWidgetVisible(
                    target, 0, max(80, self._scroll.viewport().height() // 3),
                )

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
        # Keep both renderings ready; show the one matching the toggle.
        self._karaoke_widget.set_lines(lines)
        self._scroll.takeWidget()
        if self._karaoke_mode:
            self._scroll.setWidget(self._karaoke_widget)
        else:
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

    # ---------- karaoke + swap ----------

    def _on_karaoke_toggled(self, on: bool) -> None:
        self._karaoke_mode = bool(on)
        # No timed data yet → toggle is harmless visual-state-only.
        if not self._timed_lines:
            return
        self._scroll.takeWidget()
        if self._karaoke_mode:
            # Re-sync the karaoke widget to the current playback position
            # so the user doesn't see the "active line" pop back to -1
            # for a beat after toggling on.
            idx = -1
            for i, (t, _line) in enumerate(self._timed_lines):
                if t <= self._last_position:
                    idx = i
                else:
                    break
            self._karaoke_widget.set_active_index(idx, self._last_position)
            self._scroll.setWidget(self._karaoke_widget)
        else:
            self._scroll.setWidget(self._timed_host)

    def _on_mute_lyrics_clicked(self) -> None:
        # Surfaced by the lyrics view; the actual swap (search for an
        # instrumental version, cross-fade, preserve position) is
        # orchestrated by MainWindow because it needs the player +
        # source registry. We just emit so MainWindow can drive it.
        want_instrumental = self.mute_btn.isChecked()
        if want_instrumental:
            self.swap_status.setText(theming.styled_case(
                "searching for instrumental version…"
            ))
        else:
            self.swap_status.setText("")
        self.toggle_instrumental_requested.emit(self._current_track, want_instrumental)

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme
        if self._line_widgets:
            self._restyle_lines()
        self._karaoke_widget.set_theme(theme)
