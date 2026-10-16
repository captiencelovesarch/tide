"""The karaoke's sung line: words light up as they're sung, the light
sweeping across each word through its own [start, end].

Painted by hand (a QLabel can only colour whole spans): words are laid
out in centred rows that wrap to the width, drawn in the body colour, and
the sung part is drawn over in the accent. Both personalities sweep:

  * **modern** — a soft edge: the word is drawn with a gradient pen whose
    accent → body crossover sits at the sweep point, feathered over
    ~0.4 em, so the light flows through the word.
  * **brutalist** — a hard edge that advances a whole letter at a time
    (the sweep is quantised to character cells, like the waveform's 2 px
    cells), no blending.

Motion OFF turns the modern edge into the brutalist letter steps; "word"
style lights each word whole at its start instead of sweeping.

Timing: the lyrics view only hears the playhead four times a second, so
the line keeps its own clock — the last position plus wall time at the
rate the position has been moving — and repaints at frame rate while a
word is being sung. A playhead that stops moving (pause) stops the clock.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import lyric_words, theming
from ..lyric_words import WordLine
from . import legibility, motion as motion_module

STYLES: tuple[tuple[str, str], ...] = (
    ("sweep", "sweep · the light runs through each word as it's sung"),
    ("word", "word · each word lights up whole"),
)
DEFAULT_STYLE = "sweep"
FRAME_MS = 16
FEATHER_EM = 0.4
# how long past the last playhead report the clock keeps running on its
# own; position reports arrive every ~0.25 s while playing
MAX_COAST_S = 0.75

_style = DEFAULT_STYLE


def set_style(name: str) -> None:
    global _style
    name = str(name or "").strip().lower()
    _style = name if name in dict(STYLES) else DEFAULT_STYLE


def style() -> str:
    return _style


def style_choices() -> tuple[tuple[str, str], ...]:
    return STYLES


class PlayClock:
    """Media time between sparse position reports."""

    def __init__(self) -> None:
        self.pos = 0.0
        self.wall: float | None = None
        self.rate = 0.0

    def report(self, pos: float, wall: float | None = None) -> None:
        wall = time.monotonic() if wall is None else wall
        if self.wall is None:
            self.rate = 1.0
            self.pos, self.wall = float(pos), wall
            return
        dt, dp = wall - self.wall, pos - self.pos
        if 0.0 <= dt <= 0.05 and abs(dp) < 0.5:
            # reports at frame rate (the visualizer tile): too close to
            # measure a rate from; keep the one we have
            if self.rate <= 0.05 and dp > 0:
                self.rate = 1.0
        elif 0.05 < dt < 2.0 and 0.0 <= dp < 4.0 * dt:
            inst = dp / dt
            if inst < 0.05:
                self.rate = 0.0                       # paused in place
            elif self.rate <= 0.05:
                self.rate = inst
            else:
                self.rate = 0.6 * self.rate + 0.4 * inst
        else:
            # a seek or a long gap: assume playing until the next report
            self.rate = 1.0
        self.pos, self.wall = float(pos), wall

    def now(self, wall: float | None = None) -> float:
        wall = time.monotonic() if wall is None else wall
        if self.wall is None:
            return self.pos
        coast = min(max(0.0, wall - self.wall), MAX_COAST_S)
        return self.pos + coast * self.rate

    def running(self, wall: float | None = None) -> bool:
        """Moving, and heard from recently (mpv stops reporting while
        paused, so silence means stopped)."""
        wall = time.monotonic() if wall is None else wall
        return (self.wall is not None and self.rate > 0.05
                and wall - self.wall < MAX_COAST_S)


class SweepLine(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._line: WordLine | None = None
        self.clock = PlayClock()
        self._fg = QColor("#e6e6e6")
        self._accent = QColor("#d4b95e")
        self._modern = False
        self._pt = 24
        self._layout_key: tuple | None = None
        self._rows: list[list[tuple[int, float, float]]] = []   # (word idx, x, width)
        self._row_h = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._last_state: tuple | None = None
        self._left = False
        self._apply_font()

    # ---------- configuration ----------

    def set_look(self, *, fg: QColor, accent: QColor, modern: bool, pt: int) -> None:
        self._fg, self._accent, self._modern = QColor(fg), QColor(accent), bool(modern)
        if pt != self._pt:
            self._pt = int(pt)
            self._apply_font()
        self._last_state = None
        self.update()

    def set_left_aligned(self, left: bool) -> None:
        """Left-aligned and snug (the lyrics list's active line) instead of
        centred with breathing room (karaoke's big line)."""
        self._left = bool(left)
        self._layout_key = None
        self.updateGeometry()
        self.update()

    def _apply_font(self) -> None:
        f = QFont(self.font())
        f.setPointSize(max(1, self._pt))
        f.setWeight(QFont.Bold)
        f.setLetterSpacing(QFont.PercentageSpacing, 102)
        self.setFont(f)
        self._layout_key = None
        self.updateGeometry()

    def set_line(self, line: WordLine | None) -> None:
        if line == self._line:
            return
        self._line = line
        self._layout_key = None
        self._last_state = None
        self.updateGeometry()
        self.update()
        self._sync_timer()

    def line(self) -> WordLine | None:
        return self._line

    def report_position(self, secs: float) -> None:
        self.clock.report(float(secs))
        self._sync_timer()
        self._tick()

    # ---------- layout ----------

    def _words(self) -> list[str]:
        if self._line is None:
            return []
        if self._line.words:
            return [w.text for w in self._line.words]
        return lyric_words.split_words(self._line.text)

    def _relayout(self, width: float) -> None:
        key = (id(self._line), self._line.text if self._line else "", round(width), self.font().key())
        if key == self._layout_key:
            return
        self._layout_key = key
        fm = QFontMetricsF(self.font())
        space = fm.horizontalAdvance(" ")
        self._row_h = fm.height() * 1.12
        rows: list[list[tuple[int, float, float]]] = []
        row: list[tuple[int, float, float]] = []
        x = 0.0
        m = self.contentsMargins()
        avail = max(40.0, width - 8.0 - m.left() - m.right())
        for i, w in enumerate(self._words()):
            ww = fm.horizontalAdvance(w)
            if row and x + ww > avail:
                rows.append(row)
                row, x = [], 0.0
            row.append((i, x, ww))
            x += ww + space
        if row:
            rows.append(row)
        # centre each row (or start it at the left margin)
        m = self.contentsMargins()
        placed = []
        for r in rows:
            total = r[-1][1] + r[-1][2]
            off = float(m.left()) if self._left else (width - total) / 2.0
            placed.append([(i, off + x0, ww) for i, x0, ww in r])
        self._rows = placed

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        self._relayout(float(width))
        fm = QFontMetricsF(self.font())
        rows = max(1, len(self._rows))
        m = self.contentsMargins()
        air = 0.0 if self._left else fm.height() * 0.5
        return int(rows * self._row_h + air + m.top() + m.bottom() + 0.999)

    def sizeHint(self) -> QSize:
        w = max(200, self.width())
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        return QSize(80, self.heightForWidth(max(200, self.width())))

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._layout_key = None
        self.updateGeometry()

    # ---------- timing ----------

    def _progress(self, t: float) -> tuple[int, float]:
        line = self._line
        if line is None or not line.words:
            return -1, 0.0
        idx, frac = lyric_words.word_progress(line, t)
        if t < line.words[0].start:
            return -1, 0.0
        return idx, frac

    def _stepped(self) -> bool:
        return (not self._modern) or motion_module.intensity() == motion_module.Intensity.OFF

    def _sync_timer(self) -> None:
        want = (self.isVisible() and self._line is not None and bool(self._line.words)
                and self.clock.running() and _style == "sweep")
        if want and not self._timer.isActive():
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        idx, frac = self._progress(self.clock.now())
        if _style == "word" or idx < 0:
            state = (idx, 0 if frac <= 0.0 else 1)
        elif self._stepped():
            n = max(1, len(self._words()[idx]) if idx < len(self._words()) else 1)
            state = (idx, int(frac * n + 1e-6))
        else:
            state = (idx, round(frac * 240))
        if state != self._last_state:
            self._last_state = state
            self.update()
        if not self.clock.running():
            self._sync_timer()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._sync_timer()

    def hideEvent(self, ev) -> None:
        super().hideEvent(ev)
        self._timer.stop()

    # ---------- painting ----------

    def paintEvent(self, _ev) -> None:
        words = self._words()
        if not words:
            return
        self._relayout(float(self.width()))
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(self.font())
        fm = QFontMetricsF(self.font())
        total_h = len(self._rows) * self._row_h
        m = self.contentsMargins()
        inner_h = self.height() - m.top() - m.bottom()
        top = m.top() + (0.0 if self._left else (inner_h - total_h) / 2.0)
        fg = legibility.ink(self, self._fg)
        accent = legibility.ink(self, self._accent)
        idx, frac = self._progress(self.clock.now())
        stepped = self._stepped()
        whole = _style == "word"
        feather = max(2.0, fm.height() * FEATHER_EM)
        for r, row in enumerate(self._rows):
            base_y = top + r * self._row_h + fm.ascent() + (self._row_h - fm.height()) / 2.0
            for i, x, ww in row:
                text = words[i]
                at = QPointF(x, base_y)
                if i < idx or (i == idx and (frac >= 1.0 or (whole and frac > 0.0))):
                    p.setPen(accent)
                    p.drawText(at, text)
                    continue
                if i > idx or frac <= 0.0:
                    p.setPen(fg)
                    p.drawText(at, text)
                    continue
                # the word being sung
                if stepped:
                    lit = int(frac * len(text) + 1e-6)
                    edge = x + fm.horizontalAdvance(text[:lit])
                    p.setPen(fg)
                    p.drawText(at, text)
                    if lit > 0:
                        p.save()
                        p.setClipRect(QRectF(x - 2.0, 0.0, edge - x + 2.0, float(self.height())))
                        p.setPen(accent)
                        p.drawText(at, text)
                        p.restore()
                else:
                    edge = x + ww * frac
                    grad = QLinearGradient(edge - feather / 2.0, 0.0, edge + feather / 2.0, 0.0)
                    grad.setColorAt(0.0, accent)
                    grad.setColorAt(1.0, fg)
                    p.setPen(QPen(grad, 1.0))
                    p.drawText(at, text)
        p.end()
