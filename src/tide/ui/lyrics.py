"""Lyrics panel — plain text + timed (synced) + karaoke modes.

Three render paths:

  - **untimed**: render the full block in a scrollable label, like before.
  - **timed (default)**: render each line as its own widget so we can
    highlight the active one (bolder + larger) and scroll it into view as
    playback advances. The active line is sung word by word: a SweepLine
    takes its label's place in the list while it's active.
  - **karaoke**: toggleable from a checkbox at the top of this view.
    Big-center current line whose words light up as they're sung, the
    light sweeping through each word (ui/karaoke_line); prev/next lines
    small + dim above and below.

Word timing (2.3) comes from lyric_timing's shared service: a
word-synced source (LRClib lyricsfile, NetEase YRC) matched onto the
video's lines, optionally the local aligner, else line mode — words
spread across each line's window by length. The karaoke checkbox names
which one is in force. ``offset_ms`` (the "lyrics offset" setting) moves
every surface's lyrics against the playhead.
"""
from __future__ import annotations

import weakref
from bisect import bisect_right

from PySide6.QtCore import (
    QObject, QThread, Qt, QVariantAnimation, Signal, QTimer,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics
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

from .. import api, lyric_timing, lyric_words, qthreads, theming
from . import legibility, motion as motion_module, scale as _scale
from .karaoke_line import SweepLine
from .headings import Heading, line_heading as _line_heading


# Lyric type is sized against the app's body text (the theme's size, or
# the user's override, times the ui scale), never a fixed point size: at
# "huge" the old fixed 10pt lines came out smaller than the checkbox above
# them. Multipliers per step: (timed line, active line, karaoke side
# line, karaoke center line).
SIZES: dict[str, tuple[float, float, float, float]] = {
    "small": (1.0, 1.3, 1.2, 2.8),
    "medium": (1.3, 1.6, 1.3, 3.0),
    "large": (1.6, 2.0, 1.45, 3.2),
    "huge": (2.1, 2.6, 1.7, 3.6),
}
SIZE_BLURBS: dict[str, str] = {
    "small": "small · the size of the text around it",
    "medium": "medium",
    "large": "large",
    "huge": "huge · for across the room",
}
DEFAULT_SIZE = "large"

_size: str = DEFAULT_SIZE
_views: "weakref.WeakSet[LyricsView]" = weakref.WeakSet()


def set_size(name: str) -> None:
    """Pick a lyric size and restyle every open lyrics view (the panel,
    the mini's, fullscreen's). Unknown names fall back to the default."""
    global _size
    name = str(name or "").strip().lower()
    _size = name if name in SIZES else DEFAULT_SIZE
    for view in list(_views):
        try:
            view.refresh_type()
        except RuntimeError:
            pass    # C++ side already gone


def size() -> str:
    return _size


_offset_s: float = 0.0
OFFSET_RANGE_MS = 2000


def set_offset_ms(ms) -> None:
    """Move every surface's lyrics against the playhead: positive shows
    them later (for a sheet that runs ahead of the singer)."""
    global _offset_s
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        ms = 0
    ms = max(-OFFSET_RANGE_MS, min(OFFSET_RANGE_MS, ms))
    _offset_s = -ms / 1000.0
    lyric_timing.set_offset_s(_offset_s)
    for view in list(_views):
        try:
            view.update_position(view._raw_position)
        except RuntimeError:
            pass


_list_sweep_on: bool = True


def set_list_sweep(on: bool) -> None:
    """Whether the lyrics list's active line sweeps word by word (off =
    the whole line lights at once). Karaoke is not affected."""
    global _list_sweep_on
    _list_sweep_on = bool(on)
    for view in list(_views):
        try:
            view.sweep_check.blockSignals(True)
            view.sweep_check.setChecked(_list_sweep_on)
            view.sweep_check.blockSignals(False)
            view._unplace_sweep()
            view._restyle_lines()
            view._place_sweep()
            view._list_sweep.report_position(view._last_position)
        except RuntimeError:
            pass


def list_sweep() -> bool:
    return _list_sweep_on


def offset_ms() -> int:
    return int(round(-_offset_s * 1000))


def refresh_timing() -> None:
    """Re-ask the word-timing service for every open view's sheet (its
    settings changed). Cheap when nothing did: the answer is cached."""
    for view in list(_views):
        try:
            view.refresh_timing()
        except RuntimeError:
            pass


TIMING_TAGS: dict[str, str] = {
    "source": "synced words",
    "aligned": "aligned words",
    "line": "estimated words",
}


def timing_of(sheet) -> str:
    """The sheet's best word timing: "source" > "aligned" > "line"."""
    kinds = {l.timing for l in sheet if l.words}
    for k in ("source", "aligned"):
        if k in kinds:
            return k
    return "line"


def size_choices() -> tuple[tuple[str, str], ...]:
    return tuple((k, SIZE_BLURBS[k]) for k in SIZES)


def _body_pt() -> float:
    mgr = theming.manager()
    theme = mgr.current_effective()
    base = 0.0
    try:
        base = float(mgr.user_font_size() or 0)
    except Exception:
        base = 0.0
    if base <= 0:
        base = float(theme.t("typography", "size_pt", 10) if theme else 10)
    return base * _scale.factor()


def type_pt(role: str, font_scale: float = 1.0) -> int:
    """Point size for one lyric role ("line", "active", "side", "center")
    at the current size step, body text and ui scale."""
    idx = ("line", "active", "side", "center").index(role)
    return max(1, round(_body_pt() * SIZES[_size][idx] * font_scale))


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


class _LineLabel(legibility.InkLabel):
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


def _set_line_sheet(lbl: QLabel, sheet: str) -> None:
    if getattr(lbl, "_line_sheet", None) != sheet:
        lbl._line_sheet = sheet
        lbl.setStyleSheet(sheet)


class _KaraokeWidget(QWidget):
    """Big-center karaoke rendering. Stack of three lines (prev, current,
    next): the current one big and centred, its words lit as they're
    sung by the SweepLine (ui/karaoke_line), the neighbours small + dim.

    Fed a word sheet (lyric_words.WordLine list): word timing from a
    source, the aligner, or — line mode — spread across the line's window
    by length. The sheet's own line starts pick the current line, so a
    re-timed sheet re-times the karaoke too."""

    def __init__(self, parent: QWidget | None = None, *,
                 font_scale: float = 1.0) -> None:
        super().__init__(parent)
        self._sheet: list = []
        self._starts: list[float] = []
        # Effective, not base: built mid-song (mini panel, fullscreen)
        # the accent must be the adaptive one already in force.
        self._theme = theming.manager().current_effective()
        self._font_scale = float(font_scale)
        self._active_idx: int = -1
        self._current_secs: float = 0.0

        # InkLabel: rich, selectable text paints past the style proxy.
        self.prev_label = legibility.InkLabel(" ")
        self.next_label = legibility.InkLabel(" ")
        self.current_line = SweepLine()

        for lbl in (self.prev_label, self.next_label):
            lbl.setWordWrap(True)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            # remote text: never rendered as markup
            lbl.setTextFormat(Qt.PlainText)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(18)
        col.addStretch(1)
        col.addWidget(self.prev_label)
        col.addWidget(self.current_line)
        col.addWidget(self.next_label)
        col.addStretch(1)

        self._apply_styles()

    def set_lines(self, lines: list[tuple[float, str]]) -> None:
        self.set_sheet(lyric_words.line_sheet(list(lines)))

    def set_sheet(self, sheet: list) -> None:
        self._sheet = list(sheet)
        self._starts = [l.start for l in self._sheet]
        self._active_idx = -2
        self.set_position(self._current_secs)

    def sheet(self) -> list:
        return list(self._sheet)

    def set_position(self, secs: float) -> None:
        self._current_secs = float(secs)
        idx = bisect_right(self._starts, self._current_secs) - 1
        if idx != self._active_idx:
            self._active_idx = idx
            self._render()
        self.current_line.report_position(self._current_secs)

    def set_active_index(self, idx: int, current_secs: float) -> None:
        """Kept for callers that pick the line themselves; the sheet's
        own timing wins."""
        self.set_position(current_secs)

    def set_theme(self, theme) -> None:
        self._theme = theme
        self._apply_styles()
        self._render()

    # ---------- internals ----------

    def _apply_styles(self) -> None:
        theme = self._theme
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        side_pt = type_pt("side", self._font_scale)
        cur_pt = type_pt("center", self._font_scale)
        self.prev_label.setStyleSheet(
            f"color: {dim}; background: transparent; font-size: {side_pt}pt; padding: 0;"
        )
        self.next_label.setStyleSheet(
            f"color: {dim}; background: transparent; font-size: {side_pt}pt; padding: 0;"
        )
        modern = getattr(theme, "aesthetic", "") == "modern"
        self.current_line.set_look(fg=QColor(fg), accent=QColor(accent),
                                   modern=modern, pt=cur_pt)
        self.current_line.setContentsMargins(0, 12, 0, 12)

    def _render(self) -> None:
        idx = self._active_idx
        n = len(self._sheet)
        prev_text = self._sheet[idx - 1].text if 0 < idx < n else ""
        next_text = self._sheet[idx + 1].text if 0 <= idx < n - 1 else ""
        if idx < 0 and n:
            # the intro: what's coming up sits underneath
            next_text = self._sheet[0].text
        self.prev_label.setText(prev_text or " ")
        self.next_label.setText(next_text or " ")
        self.current_line.set_line(self._sheet[idx] if 0 <= idx < n else None)


class LyricsView(QWidget):
    # Emitted whenever the user toggles the [mute lyrics] button. The
    # owning MainWindow searches for an instrumental version of the
    # given track and drives the swap. `want_instrumental` is True when
    # the button is now checked (swap to instrumental); False when the
    # user toggled it off (swap back to vocal).
    toggle_instrumental_requested = Signal(object, bool)   # track, want_instrumental
    # The tab's [sweep words] checkbox: MainWindow persists it as
    # Settings.lyrics_list_sweep and applies it (set_list_sweep).
    list_sweep_toggled = Signal(bool)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None, *,
                 font_scale: float = 1.0) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current_effective()
        # Type multiplier for embeds that are read from farther away (the
        # fullscreen mode). 1.0 = the classic panel sizes, unchanged.
        self._font_scale = float(font_scale)
        theming.manager().theme_changed.connect(self._on_theme)
        _views.add(self)

        self._current_video_id: str | None = None
        self._current_track = None
        self._current_key: str | None = None
        self._result = None
        # Position as reported, before the lyrics offset.
        self._raw_position: float = 0.0
        lyric_timing.service().updated.connect(self._on_sheet)
        self._thread: QThread | None = None
        self._worker: _LyricsWorker | None = None

        # Timed state
        self._timed_lines: list[tuple[float, str]] = []
        self._timed_starts: list[float] = []
        self._line_widgets: list[_LineLabel] = []
        self._active_line_index: int = -1
        self._lines_behind: bool = False
        self._karaoke_mode: bool = False
        # Line-advance motion (glide scroll + accent fade-in). One of
        # each at most; stopped whenever the line list is rebuilt.
        self._scroll_anim: QVariantAnimation | None = None
        self._active_anim: QVariantAnimation | None = None
        # Last known playback position, used so karaoke can re-render
        # smoothly when the user toggles in mid-line.
        self._last_position: float = 0.0

        self.heading = Heading("lyrics")
        self.heading.setProperty("class", "dim")
        # Heading embeds the remote track title (see show_for); plain-text it.
        self.heading.setTextFormat(Qt.PlainText)

        # Toggles bar — karaoke mode + (v1.2.1 second-half) mute-lyrics
        # instrumental-swap button. Lives in this view per the spec, NOT
        # on the main playback control bar.
        self.karaoke_check = QCheckBox(theming.styled_case("karaoke mode"))
        self.karaoke_check.toggled.connect(self._on_karaoke_toggled)
        # Word-by-word sweep in the live list (karaoke always sweeps).
        # Part of the tab's chrome: it follows karaoke_check's visibility,
        # so embeds that hide the chrome (mini, fullscreen, the visualizer
        # tile) hide this too.
        self.sweep_check = QCheckBox(theming.styled_case("sweep words"))
        self.sweep_check.setChecked(_list_sweep_on)
        self.sweep_check.setToolTip(theming.styled_case(
            "light the line being sung word by word. off lights the whole line."))
        self.sweep_check.toggled.connect(self.list_sweep_toggled.emit)
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
        toggles_row.addWidget(self.sweep_check)
        toggles_row.addWidget(self.mute_btn)
        toggles_row.addStretch(1)

        # Container we swap between the plain QLabel and the timed line list.
        self._plain_label = legibility.InkLabel("── no track ──")
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
        # The list's active line, sung word by word: this sweep line takes
        # the active label's place in the list (the label hides) and moves
        # down as the song does. Only for lines with words to sweep.
        self._sheet: list = []
        self._list_sweep = SweepLine(self._timed_host)
        self._list_sweep.set_left_aligned(True)
        self._list_sweep.setContentsMargins(0, 4, 0, 4)
        self._list_sweep.hide()
        self._sweep_at = -1

        self._karaoke_widget = _KaraokeWidget(font_scale=self._font_scale)
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
        self.refresh_type()

    # ---------- public ----------

    def show_for(self, track) -> None:
        if track is None:
            self._current_video_id = None
            self._current_track = None
            self._current_key = None
            self._result = None
            self.heading.set_label("lyrics")
            self._show_plain("── no track ──")
            self._clear_timed()
            self.mute_btn.setEnabled(False)
            return
        if self._current_video_id == track.video_id:
            return
        self._current_video_id = track.video_id
        self._current_track = track
        self._current_key = lyric_timing.track_key(track)
        self._result = None
        self.heading.set_label(f"lyrics · {(track.title or '').lower()}")
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
        self._raw_position = float(seconds)
        seconds = float(seconds) + _offset_s
        self._last_position = seconds
        if not self._timed_lines:
            return
        # Latest line whose timestamp <= seconds.
        idx = bisect_right(self._timed_starts, seconds) - 1
        # Karaoke keeps its own clock between reports; every one re-syncs it.
        if self._karaoke_mode:
            self._karaoke_widget.set_position(seconds)
        elif self._sweep_at >= 0:
            self._list_sweep.report_position(seconds)
        if idx == self._active_line_index:
            return
        self._active_line_index = idx
        if self._karaoke_mode:
            return
        self._restyle_lines()
        self._place_sweep()
        if self._sweep_at >= 0:
            self._list_sweep.report_position(seconds)
        if not self.isVisible():
            # Nobody is looking: skip the colour fade (a stylesheet per
            # frame) and the scroll glide; showEvent parks the list.
            self._lines_behind = True
            return
        self._animate_active_line()
        self._glide_to_active()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.sweep_check.setVisible(not self.karaoke_check.isHidden())
        if self._lines_behind:
            self._lines_behind = False
            if not self._karaoke_mode and self._line_widgets:
                self._glide_to_active()

    # ---------- async result handling ----------

    def _on_done(self, video_id: str, result) -> None:
        if video_id != self._current_video_id:
            return
        from ..lyrics_provider import LyricsResult
        if not isinstance(result, LyricsResult):
            self._show_plain("── no lyrics for this track ──")
            return
        self._result = result
        # Word timing: the best sheet known now; the service upgrades it
        # in the background (_on_sheet).
        sheet = lyric_timing.service().sheet_for(self._current_track, result)
        if sheet:
            self._show_sheet(sheet)
        elif result.plain_text:
            self._show_plain(result.plain_text)
        else:
            self._show_plain("── no lyrics for this track ──")

    def refresh_timing(self) -> None:
        if self._result is None or self._current_track is None:
            return
        sheet = lyric_timing.service().sheet_for(self._current_track, self._result)
        if sheet:
            self._show_sheet(sheet)
        self._karaoke_widget.current_line.update()

    def _on_sheet(self, key: str, sheet) -> None:
        if key != self._current_key or self._current_key is None or not sheet:
            return
        self._show_sheet(sheet)

    def _on_failed(self, video_id: str, _msg: str) -> None:
        if video_id != self._current_video_id:
            return
        self._show_plain("── lyrics unavailable ──")

    # ---------- render modes ----------

    def _show_sheet(self, sheet) -> None:
        """Show a word sheet. A sheet with the same lines as the one up
        (a timing upgrade) only re-times: the line widgets stay."""
        lines = [(l.start, l.text) for l in sheet]
        self._sheet = list(sheet)
        same = (len(lines) == len(self._timed_lines) and self._line_widgets
                and all(a[1] == b[1] for a, b in zip(lines, self._timed_lines)))
        if same:
            self._timed_lines = lines
            self._timed_starts = [t for t, _l in lines]
            self._karaoke_widget.set_sheet(sheet)
            self._unplace_sweep()       # re-placed below with the new words
        else:
            self._show_timed(lines)
            self._karaoke_widget.set_sheet(sheet)
        tag = TIMING_TAGS[timing_of(sheet)]
        self.karaoke_check.setText(theming.styled_case(f"karaoke mode · {tag}"))
        self._active_line_index = -2
        self.update_position(self._raw_position)

    def _show_plain(self, text: str) -> None:
        self._sheet = []
        self._unplace_sweep()
        self._timed_lines = []
        self._timed_starts = []
        self.karaoke_check.setText(theming.styled_case("karaoke mode"))
        self._active_line_index = -1
        self._plain_label.setText(text)
        self._scroll.takeWidget()
        self._scroll.setWidget(self._plain_label)

    def _show_timed(self, lines: list[tuple[float, str]]) -> None:
        self._timed_lines = lines
        self._timed_starts = [t for t, _l in lines]
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
        self._stop_line_motion()
        self._unplace_sweep()
        for w in self._line_widgets:
            w.deleteLater()
        self._line_widgets = []

    def _unplace_sweep(self) -> None:
        at = self._sweep_at
        self._sweep_at = -1
        self._timed_layout.removeWidget(self._list_sweep)
        self._list_sweep.hide()
        self._list_sweep.set_line(None)
        if 0 <= at < len(self._line_widgets):
            try:
                self._line_widgets[at].show()
            except RuntimeError:
                pass

    def _place_sweep(self) -> None:
        """Put the sweep line where the active label is (and the label
        back where the sweep was)."""
        idx = self._active_line_index
        line = self._sheet[idx] if 0 <= idx < len(self._sheet) else None
        ok = (_list_sweep_on and line is not None and bool(line.words) and idx < len(self._line_widgets)
              and line.text == self._timed_lines[idx][1] and bool(line.text.strip()))
        if self._sweep_at == idx and ok:
            self._list_sweep.set_line(line)
            return
        self._unplace_sweep()
        if not ok:
            return
        self._apply_sweep_look()
        # start the text where the labels start theirs: QLabel indents by
        # half an "x" when it has a frame (and the list labels do)
        lbl = self._line_widgets[idx]
        indent = lbl.indent()
        if indent < 0:
            indent = lbl.fontMetrics().horizontalAdvance("x") // 2 if lbl.frameWidth() > 0 else 0
        self._list_sweep.setContentsMargins(lbl.contentsRect().left() + indent, 4, 0, 4)
        self._list_sweep.set_line(line)
        self._timed_layout.insertWidget(idx, self._list_sweep)
        self._line_widgets[idx].hide()
        self._list_sweep.show()
        self._sweep_at = idx

    def _apply_sweep_look(self) -> None:
        theme = self._theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        self._list_sweep.set_look(
            fg=QColor(fg), accent=QColor(accent),
            modern=getattr(theme, "aesthetic", "") == "modern",
            pt=type_pt("active", self._font_scale))

    def _stop_line_motion(self) -> None:
        for anim in (self._scroll_anim, self._active_anim):
            if anim is not None:
                anim.stop()
        self._scroll_anim = None
        self._active_anim = None

    def _animate_active_line(self) -> None:
        """Fade the newly-active line from body color into the accent.
        The restyle above already snapped the bold + size (so layout
        settles in one step); the color glide is what makes the handoff
        read as motion instead of a blink."""
        if self._active_anim is not None:
            self._active_anim.stop()
            self._active_anim = None
        idx = self._active_line_index
        if not (0 <= idx < len(self._line_widgets)):
            return
        if idx == self._sweep_at:
            return   # the sweep line is up; the label is hidden
        if motion_module.intensity() == motion_module.Intensity.OFF:
            return   # _restyle_lines already painted the accent
        theme = self._theme
        fg = QColor(theme.token("fg", "#e6e6e6") if theme else "#e6e6e6")
        accent = QColor(
            theme.token("accent", "#d4b95e") if theme else "#d4b95e")
        lbl = self._line_widgets[idx]
        pt = type_pt("active", self._font_scale)

        def _apply(c: QColor, lbl=lbl, pt=pt) -> None:
            try:
                _set_line_sheet(lbl,
                    f"color: {c.name()}; background: transparent; "
                    f"font-weight: 700; font-size: {pt}pt; padding: 4px 0;"
                )
            except RuntimeError:
                pass   # line list rebuilt mid-fade; the stop is coming

        self._active_anim = motion_module.color_lerp(
            fg, accent,
            on_update=_apply,
            owner=self,
            kind="color/lyric_active",
        )

    def _glide_to_active(self) -> None:
        """Teleprompter scroll: park the active line around a third from
        the top of the viewport. ensureWidgetVisible only moved when a
        line left the view, so the list sat still and then jumped a
        page; gliding a little on every line keeps the eye anchored."""
        idx = self._active_line_index
        if not (0 <= idx < len(self._line_widgets)):
            return
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
            self._scroll_anim = None
        target = self._list_sweep if idx == self._sweep_at else self._line_widgets[idx]
        # Sizes just changed in _restyle_lines; settle geometry before
        # reading positions or the goal is one restyle stale.
        host_layout = self._timed_host.layout()
        if host_layout is not None:
            host_layout.activate()
        if motion_module.intensity() == motion_module.Intensity.OFF:
            self._scroll.ensureWidgetVisible(
                target, 0, max(80, self._scroll.viewport().height() // 3),
            )
            return
        bar = self._scroll.verticalScrollBar()
        goal = target.pos().y() - int(self._scroll.viewport().height() * 0.38)
        goal = max(0, min(int(goal), bar.maximum()))
        if goal == bar.value():
            return
        self._scroll_anim = motion_module.value_lerp(
            float(bar.value()), float(goal),
            on_update=lambda v, bar=bar: bar.setValue(int(v)),
            dur=motion_module.dur("med"),
            easing=motion_module.ease("out_strong"),
            owner=self,
            kind="value/lyric_scroll",
        )

    def _restyle_lines(self) -> None:
        theme = self._theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        active_pt = type_pt("active", self._font_scale)
        rest_pt = type_pt("line", self._font_scale)
        # Only the lines whose sheet actually changes are touched: each
        # setStyleSheet is a repolish, and a song has dozens of lines of
        # which two change state per step.
        for i, lbl in enumerate(self._line_widgets):
            if i == self._active_line_index:
                _set_line_sheet(lbl,
                    f"color: {accent}; background: transparent; "
                    f"font-weight: 700; font-size: {active_pt}pt; padding: 4px 0;"
                )
            elif i < self._active_line_index:
                _set_line_sheet(lbl,
                    f"color: {dim}; background: transparent; "
                    f"font-size: {rest_pt}pt; padding: 2px 0;"
                )
            else:
                _set_line_sheet(lbl,
                    f"color: {fg}; background: transparent; "
                    f"font-size: {rest_pt}pt; padding: 2px 0;"
                )

    # ---------- karaoke + swap ----------

    def _on_karaoke_toggled(self, on: bool) -> None:
        self._karaoke_mode = bool(on)
        self.sweep_check.setEnabled(not self._karaoke_mode)   # karaoke always sweeps
        # No timed data yet → toggle is harmless visual-state-only.
        if not self._timed_lines:
            return
        self._scroll.takeWidget()
        if self._karaoke_mode:
            # Re-sync the karaoke widget to the current playback position
            # so the user doesn't see the "active line" pop back to -1
            # for a beat after toggling on.
            self._karaoke_widget.set_position(self._last_position)
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
        self.refresh_type()

    def refresh_type(self) -> None:
        """Re-derive every lyric size: theme, scale or the size step
        changed."""
        self._plain_label.setStyleSheet(
            f"font-size: {type_pt('line', self._font_scale)}pt;")
        if self._line_widgets:
            self._restyle_lines()
        self._apply_sweep_look()
        self._karaoke_widget.set_theme(self._theme)
