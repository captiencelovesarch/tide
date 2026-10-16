"""the visualizer's tile grid, and the tiles beyond the scenes and the deck.

up to four tiles share the visualizer tab (``LAYOUTS``), each picked per
slot from ``TILE_KINDS``: the live scenes and the beat deck (vis_deck.py),
the lyrics, and:

* ``river``: the song's loudness (the beat map's 16 Hz curve) as a
  mirrored silhouette flowing past a playhead, so quiet bits and drops
  show before they arrive.
* ``spectrogram``: the live spectrum over the last few seconds as a heat
  map, bass at the bottom, the beats running down it.
* ``halo``: the cover in the middle, the spectrum radiating round it,
  breathing with the swell and punching on the kick.
* ``tapper``: a rhythm game on the beat map. click (or space) on the
  beat; it says how many ms off you were, keeps a streak and tells you
  your average offset.

in the glow look the river, spectrogram and halo take their colours from
the cover (VisEngine.colors); blocks keeps the theme's palette.

each tile's knobs are ordinary settings (settings_schema, "visualizer
tiles"), shown in the settings dialog and in the tile's own menu: the
[edit] button that shows while you hover a tile, or a right-click.
all of them run off the engine's one clock and stop asking for frames
once they've settled.
"""
from __future__ import annotations

from collections import deque
import math
import time

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPen, QPolygonF, QRegion
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import theming
from .vis_deck import (
    CanvasTile, DeckTile, Frame, SceneTile, TileMixin, VisEngine, _alpha, _mix,
    bloom, paint_outline,
)


TILE_KINDS: tuple[tuple[str, str], ...] = (
    ("spectrum", "spectrum"),
    ("mirror", "mirror"),
    ("scope", "scope"),
    ("deck", "beat deck"),
    ("lyrics", "lyrics"),
    ("river", "loudness river"),
    ("spectrogram", "spectrogram"),
    ("halo", "art halo"),
    ("tapper", "beat tapper"),
    ("classic", "classic renderer"),
)
KIND_LABELS = dict(TILE_KINDS)

# (row, col, rowspan, colspan) per slot, and the grid's (row, col) stretch
LAYOUTS: dict[str, tuple[tuple[int, int, int, int], ...]] = {
    "single": ((0, 0, 1, 1),),
    "split": ((0, 0, 1, 1), (0, 1, 1, 1)),
    "stack": ((0, 0, 1, 1), (1, 0, 1, 1)),
    "trio": ((0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)),
    "quad": ((0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)),
}
STRETCH: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "single": ((1,), (1,)),
    "split": ((1,), (1, 1)),
    "stack": ((2, 1), (1,)),
    "trio": ((1, 1), (3, 2)),
    "quad": ((1, 1), (1, 1)),
}
LAYOUT_LABELS: tuple[tuple[str, str], ...] = (
    ("single", "one tile"),
    ("split", "two, side by side"),
    ("stack", "two, stacked"),
    ("trio", "three: one big, two small"),
    ("quad", "four: a 2×2 grid"),
)
SLOT_NAMES: dict[str, tuple[str, ...]] = {
    "single": ("the tile",),
    "split": ("left", "right"),
    "stack": ("top", "bottom"),
    "trio": ("big", "top right", "bottom right"),
    "quad": ("top left", "top right", "bottom left", "bottom right"),
}
SLOT_KEYS = ("visualizer_tile_1", "visualizer_tile_2", "visualizer_tile_3",
             "visualizer_tile_4")

_SCENE_KINDS = ("spectrum", "mirror", "scope", "classic")


def _rgba_image(arr: np.ndarray) -> QImage:
    """an (h, w, 4) uint8 rgba array as an owned QImage."""
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


def _rgb(c: QColor) -> np.ndarray:
    return np.array([c.red(), c.green(), c.blue()], dtype=float)


def _cut_cells(p: QPainter, rect: QRectF, rows: int, cols: int) -> None:
    """blocks look: clear thin gutters so a scaled-up grid reads as cells."""
    cw, ch = rect.width() / cols, rect.height() / rows
    if cw < 5 or ch < 5:
        return
    g = max(1.0, min(cw, ch) * 0.18)
    p.save()
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for i in range(1, cols):
        p.drawRect(QRectF(rect.left() + i * cw - g / 2, rect.top(), g, rect.height()))
    for j in range(1, rows):
        p.drawRect(QRectF(rect.left(), rect.top() + j * ch - g / 2, rect.width(), g))
    p.restore()


class PlayTile(CanvasTile):
    """a tile you click into: double-click must not leave for fullscreen."""

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def knob(self, name: str, default):
        return self.engine.setting(name, default)

    HINT_H = 18.0

    def inner(self, w: float, h: float) -> QRectF:
        """the play area: the tile less its padding and the hint line."""
        return QRectF(self.PAD, self.PAD, max(1.0, w - 2 * self.PAD),
                      max(1.0, h - 2 * self.PAD - self.HINT_H))

    def hint(self, p: QPainter, w: float, h: float, f: Frame, text: str) -> None:
        font = self.font_at(0.85)
        p.setFont(font)
        p.setPen(_alpha(f.pal.dim, 200))
        fm = QFontMetricsF(font)
        p.drawText(QPointF(w - self.PAD - fm.horizontalAdvance(text), h - self.PAD + 2 - fm.descent()), text)


# ---------- loudness river ----------

RIVER_VIEWS = {"near": 16.0, "far": 48.0, "whole": 0.0}   # seconds across; 0 = the whole song


class RiverTile(PlayTile):
    """the song's loudness (the beat map's 16 Hz curve) as a mirrored
    silhouette flowing past a playhead: what's been, and what's coming."""

    kind = "river"
    KNOBS = ("visualizer_river_view",)
    HEAD_AT = 0.3

    def __init__(self, engine, parent=None) -> None:
        super().__init__(engine, parent)
        self._whole_key = None
        self._whole: QImage | None = None

    def buffer_scale(self, f) -> float:
        return 0.6 if f.look == "glow" else 1.0

    def mouseDoubleClickEvent(self, event) -> None:
        CanvasTile.mouseDoubleClickEvent(self, event)    # a view, not a toy: lean back

    def on_frame(self, now, dt) -> None:
        if self.engine.look == "glow":
            self.engine.want_art()
        self.update()

    def _window(self, mv) -> tuple[float, float, float]:
        """(lo, hi, playhead fraction) in media seconds."""
        e = self.engine
        span = RIVER_VIEWS.get(self.knob("visualizer_river_view", "near"), 16.0)
        pos = e.position or 0.0
        if span <= 0:
            dur = e.duration if e.duration > 0 else mv.duration
            dur = max(dur, 1.0)
            return 0.0, dur, min(1.0, pos / dur)
        lo = pos - span * self.HEAD_AT
        return lo, lo + span, self.HEAD_AT

    def paint_graphics(self, p, w, h, f) -> None:
        e = self.engine
        r = self.inner(w, h)
        p.setClipRect(r)
        mv = e.mv
        c1, c2, shadow = e.colors()
        pal = f.pal
        glow = f.look == "glow"
        cy = r.center().y()
        amp = r.height() * 0.46
        if mv is None or e.position is None:
            self._live(p, r, f, c1)
            return
        lo, hi, head = self._window(mv)
        span = max(hi - lo, 1e-6)
        sx = r.width() / span
        head_x = r.left() + (e.position - lo) * sx
        t, v = mv.loud_between(max(0.0, lo), min(mv.duration, hi))
        if t.size >= 2:
            if not glow:
                # columns of cells, an eighth of a second (or a pixel) wide
                cw = max(3.0, r.width() / 160)
                cols = int(r.width() / cw)
                ct = lo + (np.arange(cols) + 0.5) * cw / sx
                cv = np.interp(ct, t, v, left=0.0, right=0.0)
                q = np.round(cv * 10) / 10
                cell = amp / 10
                p.setPen(Qt.NoPen)
                for i, (tt, vv) in enumerate(zip(ct, q)):
                    if vv <= 0:
                        continue
                    x = r.left() + i * cw
                    past = tt < e.position
                    p.setBrush(_alpha(pal.fg, 70) if past else pal.fg)
                    p.drawRect(QRectF(x, cy - vv * amp, cw - 1, 2 * vv * amp))
                _cut_cells_h(p, QRectF(r.left(), cy - amp, r.width(), 2 * amp), 20)
            else:
                # no more points than ~3 px apart: "far" and "whole" would
                # otherwise hand the stroker thousands
                limit = max(32, int(r.width() / 3))
                if t.size > limit:
                    k = int(math.ceil(t.size / limit))
                    n = (t.size // k) * k
                    t = t[:n].reshape(-1, k).mean(axis=1)
                    v = v[:n].reshape(-1, k).max(axis=1)
                xs = r.left() + (t - lo) * sx
                top = QPolygonF([QPointF(float(x), float(cy - y * amp)) for x, y in zip(xs, v)])
                bottom = QPolygonF([QPointF(float(x), float(cy + y * amp))
                                    for x, y in zip(xs[::-1], v[::-1])])
                from PySide6.QtGui import QLinearGradient, QPainterPath
                path = QPainterPath()
                path.addPolygon(top + bottom)
                path.closeSubpath()
                p.setRenderHint(QPainter.Antialiasing, True)
                g = QLinearGradient(QPointF(0, cy - amp), QPointF(0, cy + amp))
                g.setColorAt(0.0, _alpha(c2, 200))
                g.setColorAt(0.5, _alpha(c1, 235))
                g.setColorAt(1.0, _alpha(c2, 200))
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawPath(path)
                # what's been played goes quiet: one rect, only where the
                # river is (source-atop), instead of a second clipped fill
                if head_x > r.left():
                    p.save()
                    p.setCompositionMode(QPainter.CompositionMode_SourceAtop)
                    p.fillRect(QRectF(r.left(), r.top(), head_x - r.left(), r.height()),
                               _alpha(shadow, 170))
                    p.restore()
                # the rim catches the kick where the playhead is
                p.setPen(QPen(_alpha(pal.fg, 60 + 120 * f.kick), 1.2))
                p.setBrush(Qt.NoBrush)
                p.drawPolyline(top)
        # beats along the centre line, downbeats longer: one call per kind
        p.setPen(Qt.NoPen)
        rng_ = mv.beats_between(lo, hi)
        if len(rng_):
            ts = mv.times[rng_.start:rng_.stop]
            down = mv.phases[rng_.start:rng_.stop] == 0
            xs_b = r.left() + (ts - lo) * sx
            tick = pal.bg if not glow else pal.fg
            for mask, hh, a in ((down, 7.0, 200), (~down, 3.0, 120)):
                rects = [QRectF(float(x) - 0.75, cy - hh, 1.5, 2 * hh) for x in xs_b[mask]]
                if rects:
                    p.setBrush(_alpha(tick, a))
                    p.drawRects(rects)
        # the playhead
        p.setRenderHint(QPainter.Antialiasing, glow)
        if glow:
            from PySide6.QtGui import QLinearGradient
            g = QLinearGradient(QPointF(head_x - 18, 0), QPointF(head_x + 18, 0))
            g.setColorAt(0.0, _alpha(c1, 0))
            g.setColorAt(0.5, _alpha(c1, 60 + 150 * f.kick))
            g.setColorAt(1.0, _alpha(c1, 0))
            p.setBrush(g)
            p.drawRect(QRectF(head_x - 18, r.top(), 36, r.height()))
            p.setBrush(_alpha(pal.fg, 230))
            p.drawRect(QRectF(head_x - 1, r.top(), 2, r.height()))
        else:
            p.setBrush(pal.accent if f.kick > 0.3 else pal.fg)
            p.drawRect(QRectF(head_x - 1.5, r.top(), 3, r.height()))
        # an analysis still running: the river ends where the map does
        if not mv.map.complete and mv.duration < hi:
            x0 = r.left() + (mv.duration - lo) * sx
            if x0 < r.right():
                p.setBrush(_alpha(pal.dim, 40))
                p.drawRect(QRectF(x0, cy - 1, r.right() - x0, 2))

    def _live(self, p, r, f, c1) -> None:
        """no map: the capture's bass, mirrored, flowing in from the right."""
        live = self.engine.live
        if not live.samples:
            return
        now = self.engine.now
        sx = r.width() / live.seconds
        cy = r.center().y()
        amp = r.height() * 0.46
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(c1 if f.look == "glow" else f.pal.fg, 150))
        cw = max(2.0, live.BUCKET * sx - 1)
        for t, v in live.samples:
            x = r.right() - (now - t) * sx
            p.drawRect(QRectF(x, cy - v * amp, cw, 2 * v * amp))

    def post_graphics(self, buf, f) -> None:
        if f.look == "glow":
            bloom(buf, 0.35 + 0.3 * f.kick * max(f.gain, 0.3))

    def paint_overlay(self, p, w, h, f) -> None:
        e = self.engine
        if e.mv is None:
            self.hint(p, w, h, f, theming.styled_case("live · the river fills in once the song's analyzed"))
        else:
            view = self.knob("visualizer_river_view", "near")
            self.hint(p, w, h, f, theming.styled_case(
                "the whole song" if view == "whole" else "what's coming →"))


def _cut_cells_h(p: QPainter, rect: QRectF, rows: int) -> None:
    """blocks: horizontal gutters only (the columns bring their own gaps)."""
    ch = rect.height() / rows
    if ch < 4:
        return
    g = max(1.0, ch * 0.2)
    p.save()
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for j in range(1, rows):
        p.drawRect(QRectF(rect.left(), rect.top() + j * ch - g / 2, rect.width(), g))
    p.restore()


# ---------- spectrogram ----------

SPECTRO_HISTORY = {6: 6.0, 12: 12.0, 24: 24.0}
SPECTRO_CONTRAST = {"soft": 1.5, "normal": 1.0, "punchy": 0.65}


class SpectrogramTile(PlayTile):
    """the live spectrum over the last few seconds as a heat map, bass at
    the bottom, with the beats running down it. coloured through a ramp
    from the cover (glow) or the theme (blocks)."""

    kind = "spectrogram"
    KNOBS = ("visualizer_spectrogram_history", "visualizer_spectrogram_contrast")
    ROWS = 64

    def __init__(self, engine, parent=None) -> None:
        super().__init__(engine, parent)
        self._cols = 0
        self._hist = np.zeros((0, self.ROWS))
        self._beats = np.zeros(0)
        self._head = 0
        self._carry = 0.0
        self._lut_key = None
        self._lut = np.zeros((256, 4), dtype=np.uint8)

    def buffer_scale(self, f) -> float:
        return 0.6 if f.look == "glow" else 1.0

    def mouseDoubleClickEvent(self, event) -> None:
        CanvasTile.mouseDoubleClickEvent(self, event)

    def _rate(self) -> float:
        return 10.0 if self.engine.look == "blocks" else 30.0     # columns per second

    def _ensure(self) -> None:
        seconds = SPECTRO_HISTORY.get(int(self.knob("visualizer_spectrogram_history", 12)), 12.0)
        rows = 32 if self.engine.look == "blocks" else self.ROWS
        cols = int(seconds * self._rate())
        if self._hist.shape != (cols, rows):
            self._hist = np.zeros((cols, rows))
            self._beats = np.zeros(cols)
            self._head = 0
            self._cols = cols

    def settings_changed(self) -> None:
        self._ensure()

    def theme_changed(self) -> None:
        self._lut_key = None

    def track_changed(self, track) -> None:
        self._hist[:] = 0
        self._beats[:] = 0

    def on_frame(self, now, dt) -> None:
        e = self.engine
        self._ensure()
        if e.look == "glow":
            e.want_art()
        if e.playing:
            self._carry += dt * self._rate()
            n = min(int(self._carry), self._cols)
            self._carry -= int(self._carry)
            if n:
                level = e.spec.level
                rows = self._hist.shape[1]
                col = np.interp(np.linspace(0, level.size - 1, rows), np.arange(level.size), level)
                beat = max(e.kicks_now) if e.kicks_now else 0.0
                downbeat = e.info is not None and e.info.phase == 0 and beat > 0
                for i in range(n):
                    self._hist[self._head] = col
                    self._beats[self._head] = (2.0 if downbeat else 1.0) * beat if i == n - 1 else 0.0
                    self._head = (self._head + 1) % self._cols
        self.update()

    def _ramp(self) -> np.ndarray:
        e = self.engine
        c1, c2, shadow = e.colors()
        pal = e.pal
        key = (e.look, c1.rgba(), c2.rgba(), shadow.rgba(), pal.fg.rgba(), pal.dim.rgba())
        if key != self._lut_key:
            if e.look == "blocks":
                stops = [(0.0, pal.dim, 0), (0.25, pal.dim, 140), (0.55, pal.fg, 200),
                         (0.8, pal.fg, 255), (1.0, pal.accent, 255)]
            else:
                stops = [(0.0, shadow, 0), (0.25, shadow.lighter(200), 150), (0.5, c1, 220),
                         (0.78, c2, 245), (1.0, pal.fg, 255)]
            xs = np.linspace(0, 1, 256)
            lut = np.zeros((256, 4))
            pos = [s_[0] for s_ in stops]
            for ch, get in enumerate((QColor.red, QColor.green, QColor.blue)):
                lut[:, ch] = np.interp(xs, pos, [get(s_[1]) for s_ in stops])
            lut[:, 3] = np.interp(xs, pos, [s_[2] for s_ in stops])
            if e.look == "blocks":
                lut = lut[(np.floor(xs * 5).clip(0, 4) / 4 * 255).astype(int)]
            self._lut = lut.astype(np.uint8)
            self._lut_key = key
        return self._lut

    def paint_graphics(self, p, w, h, f) -> None:
        self._ensure()
        r = self.inner(w, h)
        gamma = SPECTRO_CONTRAST.get(self.knob("visualizer_spectrogram_contrast", "normal"), 1.0)
        # oldest on the left: unroll the ring from the write head
        hist = np.roll(self._hist, -self._head, axis=0)
        vals = np.clip(hist, 0, 1) ** gamma
        idx = (vals * 255).astype(np.uint8).T[::-1]           # rows: bass at the bottom
        img = _rgba_image(self._ramp()[idx])
        p.setRenderHint(QPainter.SmoothPixmapTransform, f.look == "glow")
        p.drawImage(r, img)
        if f.look == "blocks":
            _cut_cells(p, r, img.height(), img.width())
        # the beats, running down the history
        beats = np.roll(self._beats, -self._head)
        cw = r.width() / max(1, self._cols)
        p.setPen(Qt.NoPen)
        for i in np.nonzero(beats)[0]:
            strong = beats[i] >= 1.5
            x = r.left() + (i + 0.5) * cw
            p.setBrush(_alpha(f.pal.fg, 150 if strong else 60))
            p.drawRect(QRectF(x - 0.5, r.top(), 1.0 if not strong else 1.5, r.height()))

    def post_graphics(self, buf, f) -> None:
        if f.look == "glow":
            bloom(buf, 0.3)

    def paint_overlay(self, p, w, h, f) -> None:
        secs = int(self.knob("visualizer_spectrogram_history", 12))
        self.hint(p, w, h, f, theming.styled_case(f"last {secs} s · bass at the bottom"))


# ---------- beat tapper ----------

TAP_WINDOWS = {"tight": (25.0, 50.0, 90.0), "normal": (35.0, 75.0, 130.0),
               "loose": (50.0, 100.0, 180.0)}
GRADES = ("perfect", "good", "ok", "miss")
TAP_ACTIVE_S = 4.0           # misses only count while you're playing along


class TapperTile(PlayTile):
    kind = "tapper"
    KNOBS = ("visualizer_tapper_target", "visualizer_tapper_window",
             "visualizer_tapper_offset")
    SPAN = 6.0

    def __init__(self, engine, parent=None) -> None:
        super().__init__(engine, parent)
        self.setFocusPolicy(Qt.ClickFocus)
        self._targets_key = None
        self._targets = np.zeros(0)
        self.reset()

    def reset(self) -> None:
        self.streak = 0
        self.best = 0
        self.taps = 0
        self.good = 0
        self.offsets: deque[float] = deque(maxlen=32)
        self.marks: deque[tuple[float, str]] = deque(maxlen=64)   # (target time, grade)
        self.history: deque[str] = deque(maxlen=16)
        self.last: tuple[str, float | None, float] | None = None  # grade, ms, wall
        self._hit: set[float] = set()
        self._last_tap_wall = -math.inf
        self._judged_to: float | None = None

    def settings_changed(self) -> None:
        self._targets_key = None

    def track_changed(self, track) -> None:
        self.reset()
        self._targets_key = None

    def targets(self) -> np.ndarray:
        mv = self.engine.mv
        if mv is None:
            return np.zeros(0)
        which = self.knob("visualizer_tapper_target", "beats")
        key = (id(mv.map), which)
        if key != self._targets_key:
            if which == "downbeats":
                t = mv.times[mv.phases == 0]
            elif which == "kicks":
                t = np.sort(np.concatenate([mv.times[mv.pulse], mv.hit_times]))
            else:
                t = mv.times
            self._targets = np.asarray(t, dtype=float)
            self._targets_key = key
        return self._targets

    def _window(self) -> tuple[float, float, float]:
        return TAP_WINDOWS.get(self.knob("visualizer_tapper_window", "normal"),
                               TAP_WINDOWS["normal"])

    def grade(self, delta_ms: float) -> str:
        perfect, good, ok = self._window()
        a = abs(delta_ms)
        return "perfect" if a <= perfect else "good" if a <= good else "ok" if a <= ok else "miss"

    def tap(self, wall: float | None = None) -> str | None:
        e = self.engine
        wall = time.monotonic() if wall is None else wall
        targets = self.targets()
        if e.position is None or not e.playing or targets.size == 0:
            self.last = ("waiting", None, wall)
            self.update()
            return None
        offset = float(self.knob("visualizer_tapper_offset", 0)) / 1000.0
        t = e.position - offset * e.speed
        i = int(np.searchsorted(targets, t))
        cands = [k for k in (i - 1, i) if 0 <= k < targets.size]
        k = min(cands, key=lambda c: abs(targets[c] - t))
        target = float(targets[k])
        delta = (t - target) / max(e.speed, 1e-6) * 1000.0
        g = self.grade(delta)
        if target in self._hit and g != "miss":
            g = "miss"                           # double-tapped one beat
        self.taps += 1
        self._last_tap_wall = wall
        if g == "miss":
            self.streak = 0
        else:
            self._hit.add(target)
            self.streak += 1
            self.best = max(self.best, self.streak)
            if g in ("perfect", "good"):
                self.good += 1
            self.offsets.append(delta)
        self.marks.append((target, g))
        self.history.append(g)
        self.last = (g, delta, wall)
        e.wake()
        self.update()
        return g

    def on_frame(self, now, dt) -> None:
        e = self.engine
        targets = self.targets()
        if e.position is not None and e.playing and targets.size:
            ok_s = self._window()[2] / 1000.0 * e.speed
            upto = e.position - ok_s
            start = self._judged_to
            if start is None or upto < start or upto - start > 2.0:
                start = upto
            if now - self._last_tap_wall <= TAP_ACTIVE_S:
                a = int(np.searchsorted(targets, start, side="right"))
                b = int(np.searchsorted(targets, upto, side="right"))
                for k in range(a, b):
                    t = float(targets[k])
                    if t not in self._hit:
                        self.streak = 0
                        self.marks.append((t, "miss"))
                        self.history.append("miss")
            self._judged_to = upto
        self.update()

    def wants_frames(self) -> bool:
        return self.last is not None and time.monotonic() - self.last[2] < 0.8

    def _grade_color(self, g: str, f: Frame) -> QColor:
        pal = f.pal
        return {"perfect": pal.accent, "good": pal.fg, "ok": _alpha(pal.fg, 150),
                "miss": _alpha(pal.dim, 220)}.get(g, pal.dim)

    def paint_graphics(self, p, w, h, f) -> None:
        e = self.engine
        pal = f.pal
        blocks = f.look == "blocks"
        lane = QRectF(self.PAD, h * 0.42, w - 2 * self.PAD, h * 0.22)
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(pal.dim, 40) if blocks else _alpha(pal.bg, 70))
        if blocks:
            p.drawRect(lane)
        else:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.drawRoundedRect(lane, lane.height() / 2, lane.height() / 2)
        head_x = lane.left() + lane.width() * 0.3
        pos = e.position
        targets = self.targets()
        if pos is not None and targets.size:
            lo = pos - self.SPAN * 0.3
            hi = lo + self.SPAN
            sx = lane.width() / self.SPAN
            graded = dict(self.marks)
            a = int(np.searchsorted(targets, lo))
            b = int(np.searchsorted(targets, hi))
            for k in range(a, b):
                t = float(targets[k])
                x = lane.left() + (t - lo) * sx
                g = graded.get(t)
                color = self._grade_color(g, f) if g else (pal.fg if t >= pos else pal.dim)
                near = max(0.0, 1.0 - abs(t - pos) / 0.1)
                size = lane.height() * (0.55 + 0.35 * near)
                if blocks:
                    p.setBrush(color)
                    p.drawRect(QRectF(x - 3, lane.center().y() - size / 2, 6, size))
                else:
                    p.setBrush(color)
                    p.drawEllipse(QPointF(x, lane.center().y()), size * 0.22, size * 0.22)
        # the hit line
        p.setBrush(pal.accent if f.kick > 0.4 else pal.fg)
        if blocks:
            p.drawRect(QRectF(head_x - 1.5, lane.top() - 10, 3, lane.height() + 20))
        else:
            p.drawRoundedRect(QRectF(head_x - 1.5, lane.top() - 10, 3, lane.height() + 20), 1.5, 1.5)
        # the last 16 judgements
        n = len(self.history)
        cell = min(18.0, (w - 2 * self.PAD) / 18)
        y = h - self.PAD - cell - 18
        for i, g in enumerate(self.history):
            p.setBrush(self._grade_color(g, f))
            x = self.PAD + (16 - n + i) * (cell + 3)
            if blocks:
                p.drawRect(QRectF(x, y, cell, cell))
            else:
                p.drawEllipse(QRectF(x, y, cell, cell))

    def paint_overlay(self, p, w, h, f) -> None:
        pal = f.pal
        e = self.engine
        small = self.font_at(0.95)
        p.setFont(small)
        fm = QFontMetricsF(small)
        acc = f"{round(100 * self.good / self.taps)}%" if self.taps else "–"
        top = theming.styled_case(f"streak {self.streak} · best {self.best} · on beat {acc}")
        p.setPen(pal.fg)
        p.drawText(QPointF(self.PAD, self.PAD + fm.ascent()), top)
        if len(self.offsets) >= 8:
            mean = sum(self.offsets) / len(self.offsets)
            if abs(mean) >= 15:
                tip = (f"you tap {abs(mean):.0f} ms {'late' if mean > 0 else 'early'} · "
                       f"latency knob: {int(self.knob('visualizer_tapper_offset', 0) + round(mean / 30) * 30):+d} ms")
            else:
                tip = f"average {mean:+.0f} ms"
            p.setPen(pal.dim)
            p.drawText(QPointF(self.PAD, self.PAD + fm.height() + fm.ascent()),
                       theming.styled_case(tip))
        if self.last is not None:
            g, delta, wall = self.last
            age = time.monotonic() - wall
            if age < 0.8:
                fade = 1.0 - age / 0.8
                big = self.font_at(2.2, bold=f.look == "glow")
                p.setFont(big)
                bfm = QFontMetricsF(big)
                if g == "waiting":
                    word = "press play" if not e.playing else "waiting for the beat map"
                    big = self.font_at(1.2)
                    p.setFont(big)
                    bfm = QFontMetricsF(big)
                else:
                    word = g
                text = theming.styled_case(word)
                color = self._grade_color(g, f) if g != "waiting" else pal.dim
                p.setPen(_alpha(color, 255 * fade))
                y = h * 0.34
                p.drawText(QPointF((w - bfm.horizontalAdvance(text)) / 2, y), text)
                if delta is not None:
                    ms = f"{delta:+.0f} ms"
                    p.setFont(small)
                    p.setPen(_alpha(pal.fg, 220 * fade))
                    p.drawText(QPointF((w - fm.horizontalAdvance(ms)) / 2, y + fm.height() + 2), ms)
        if self.engine.mv is None:
            self.hint(p, w, h, f, theming.styled_case("needs the beat map · waiting for analysis"))
        else:
            self.hint(p, w, h, f, theming.styled_case("click or space on the beat"))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            self.tap()
            return
        super().mousePressEvent(event)

    _TAP_KEYS = (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter, Qt.Key_J, Qt.Key_K)

    def event(self, ev) -> bool:
        # while focused, space taps instead of play/pause
        if ev.type() == QEvent.ShortcutOverride and ev.key() in self._TAP_KEYS:
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, event) -> None:
        if event.key() in self._TAP_KEYS and not event.isAutoRepeat():
            self.tap()
            return
        super().keyPressEvent(event)


# ---------- art halo ----------

HALO_SPOKES = {48: 48, 96: 96, 144: 144}
HALO_PUNCH = {"off": 0.0, "gentle": 0.035, "hard": 0.08}


class HaloTile(PlayTile):
    """the cover in the middle, the spectrum radiating round it. the
    cover breathes with the swell and punches on the kick; the halo
    takes its colours from the cover."""

    kind = "halo"
    KNOBS = ("visualizer_halo_spokes", "visualizer_halo_punch")

    def __init__(self, engine, parent=None) -> None:
        super().__init__(engine, parent)
        self._cover_key = None
        self._cover: QImage | None = None

    def buffer_scale(self, f) -> float:
        return 0.6 if f.look == "glow" else 1.0

    def mouseDoubleClickEvent(self, event) -> None:
        CanvasTile.mouseDoubleClickEvent(self, event)

    def on_frame(self, now, dt) -> None:
        self.engine.want_art()
        self.update()

    def _cover_image(self, size: int, round_: bool) -> QImage | None:
        e = self.engine
        art = e.art
        if art is None or size < 4:
            return None
        key = (e.art_version, size, round_)
        if key != self._cover_key:
            img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            cp = QPainter(img)
            cp.setRenderHint(QPainter.Antialiasing, True)
            cp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if round_:
                from PySide6.QtGui import QPainterPath
                clip = QPainterPath()
                clip.addEllipse(QRectF(0, 0, size, size))
                cp.setClipPath(clip)
            # centre-crop to square
            side = min(art.width(), art.height())
            src = QRectF((art.width() - side) / 2, (art.height() - side) / 2, side, side)
            cp.drawImage(QRectF(0, 0, size, size), art, src)
            cp.end()
            self._cover = img
            self._cover_key = key
        return self._cover

    def paint_graphics(self, p, w, h, f) -> None:
        e = self.engine
        pal = f.pal
        glow = f.look == "glow"
        c1, c2, _shadow = e.colors()
        r = self.inner(w, h)
        cx, cy = r.center().x(), r.center().y()
        reach = min(r.width(), r.height()) / 2
        punch = HALO_PUNCH.get(self.knob("visualizer_halo_punch", "gentle"), 0.035)
        breathe = 1.0 + 0.04 * f.swell + punch * f.kick
        art_r = reach * 0.42 * breathe
        inner = art_r + reach * 0.05
        outer = reach * 0.98
        spokes = HALO_SPOKES.get(int(self.knob("visualizer_halo_spokes", 96)), 96)
        lv = e.spec.level
        pk = e.spec.peak
        half = spokes // 2
        idx = np.linspace(0, lv.size - 1, half)
        levels = np.interp(idx, np.arange(lv.size), lv)
        peaks = np.interp(idx, np.arange(pk.size), pk)
        # mirrored: bass at the top, meeting again at the bottom
        levels = np.concatenate([levels, levels[::-1]])
        peaks = np.concatenate([peaks, peaks[::-1]])
        angles = np.linspace(0, 2 * math.pi, spokes, endpoint=False) - math.pi / 2
        cos, sin = np.cos(angles), np.sin(angles)
        length = inner + np.clip(levels, 0, 1) * (outer - inner)
        if glow:
            p.setRenderHint(QPainter.Antialiasing, True)
            from PySide6.QtGui import QRadialGradient
            g = QRadialGradient(QPointF(cx, cy), outer)
            g.setColorAt(art_r / outer, _alpha(c1, 50 + 90 * f.kick * max(f.gain, 0.3)))
            g.setColorAt(1.0, _alpha(c1, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), outer, outer)
            width = max(1.5, 2 * math.pi * inner / spokes * 0.55)
            # colour runs c1 → c2 round the ring; eight buckets, one
            # drawLines each, rather than a pen and a call per spoke
            from PySide6.QtCore import QLineF
            tone = np.abs(np.sin(angles / 2 + math.pi / 4))
            bucket = np.minimum((tone * 8).astype(int), 7)
            x0, y0 = cx + cos * inner, cy + sin * inner
            x1, y1 = cx + cos * length, cy + sin * length
            for b in range(8):
                sel = np.nonzero(bucket == b)[0]
                if sel.size == 0:
                    continue
                # flat caps: round ones tripled the stroke cost, and the
                # bloom softens the ends anyway
                p.setPen(QPen(_mix(c1, c2, (b + 0.5) / 8, alpha=230), width,
                              Qt.SolidLine, Qt.FlatCap))
                p.drawLines([QLineF(x0[i], y0[i], x1[i], y1[i]) for i in sel])
            # peak dots: one drawPoints with a round pen
            dots = np.nonzero(peaks - levels > 0.03)[0]
            if dots.size:
                rr = inner + peaks[dots] * (outer - inner) + width
                p.setPen(QPen(_alpha(pal.fg, 200), width * 0.9, Qt.SolidLine, Qt.RoundCap))
                p.drawPoints(QPolygonF([QPointF(float(cx + cos[i] * d), float(cy + sin[i] * d))
                                        for i, d in zip(dots, rr)]))
        else:
            # radial cells: a dashed pen cuts each spoke into blocks
            cell = max(3.0, (outer - inner) / 14)
            width = max(2.0, 2 * math.pi * inner / spokes * 0.6)
            pen = QPen(pal.fg, width, Qt.CustomDashLine, Qt.FlatCap)
            pen.setDashPattern([cell * 0.72 / width, cell * 0.28 / width])
            p.setRenderHint(QPainter.Antialiasing, False)
            from PySide6.QtCore import QLineF
            dim = QPen(pen)
            dim.setColor(_alpha(pal.dim, 60))
            x0, y0 = cx + cos * inner, cy + sin * inner
            p.setPen(dim)
            p.drawLines([QLineF(x0[i], y0[i], cx + cos[i] * outer, cy + sin[i] * outer)
                         for i in range(spokes)])
            pen.setColor(_mix(pal.fg, pal.accent, f.kick * f.gain * 0.7))
            p.setPen(pen)
            lit = [QLineF(x0[i], y0[i], cx + cos[i] * length[i], cy + sin[i] * length[i])
                   for i in range(spokes) if levels[i] > 0.02]
            if lit:
                p.drawLines(lit)
            p.setPen(Qt.NoPen)
            p.setBrush(pal.accent)
            for i in range(spokes):
                if peaks[i] - levels[i] > 0.04:
                    rr = inner + peaks[i] * (outer - inner)
                    p.drawRect(QRectF(cx + cos[i] * rr - width / 2, cy + sin[i] * rr - width / 2,
                                      width, width))
        # the cover
        size = int(round(art_r * 2))
        cover = self._cover_image(size, glow)
        target = QRectF(cx - art_r, cy - art_r, 2 * art_r, 2 * art_r)
        if cover is not None:
            p.drawImage(target, cover)
        else:
            p.setPen(QPen(_alpha(pal.dim, 160), 1))
            p.setBrush(_alpha(pal.bg, 160))
            if glow:
                p.drawEllipse(target)
            else:
                p.drawRect(target)
        if f.flash > 0.02 or (not glow and f.kick > 0.3 and f.gain > 0):
            p.setPen(QPen(_alpha(pal.accent, 255 * max(f.flash, f.kick * f.gain)), 2.0))
            p.setBrush(Qt.NoBrush)
            if glow:
                p.drawEllipse(target.adjusted(-3, -3, 3, 3))
            else:
                p.drawRect(target.adjusted(-3, -3, 3, 3))
        _rings_around(p, QPointF(cx, cy), art_r, outer, f, c2)

    def post_graphics(self, buf, f) -> None:
        if f.look == "glow":
            bloom(buf, 0.45 + 0.3 * f.kick * max(f.gain, 0.3))

    def paint_overlay(self, p, w, h, f) -> None:
        e = self.engine
        if e.art is None:
            self.hint(p, w, h, f, theming.styled_case("no cover for this one"))


def _rings_around(p: QPainter, c: QPointF, start: float, end: float, f: Frame,
                  color: QColor) -> None:
    """glow: a ring leaves the cover on every kick (the map's own rings)."""
    if f.look != "glow" or not f.rings or f.gain <= 0:
        return
    from .vis_deck import RING_S
    p.setBrush(Qt.NoBrush)
    for age, strength in f.rings:
        t = max(0.0, min(1.0, age / RING_S))
        rr = start + (1.0 - (1.0 - t) ** 3) * (end - start)
        a = (1.0 - t) ** 1.5 * 200 * strength * f.gain
        if a < 3:
            continue
        p.setPen(QPen(_alpha(color, a), 1.5 + 2.5 * (1.0 - t)))
        p.drawEllipse(c, rr, rr)


# ---------- lyrics ----------

class LyricsTile(TileMixin, QWidget):
    """the synced lyrics, in a tile. same embedding as the fullscreen
    player: a LyricsView with its in-app chrome hidden."""

    kind = "lyrics"
    _QSS = """
    QWidget#visLyricsTile, QWidget#visLyricsTile .QWidget,
    QWidget#visLyricsTile LyricsView, QWidget#visLyricsTile QScrollArea,
    QWidget#visLyricsTile QScrollArea > QWidget,
    QWidget#visLyricsTile QScrollArea > QWidget > QWidget,
    QWidget#visLyricsTile #lyricsKaraoke { background: transparent; }
    """

    def __init__(self, engine: VisEngine, api_provider=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._api_provider = api_provider
        api = None
        try:
            api = api_provider() if api_provider is not None else None
        except Exception:
            api = None
        self.setObjectName("visLyricsTile")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setStyleSheet(self._QSS)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(120, 90)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self.view = None
        if api is not None:
            from .lyrics import LyricsView
            self.view = LyricsView(api, font_scale=1.15)
            for chrome in ("heading", "karaoke_check", "mute_btn", "swap_status"):
                w = getattr(self.view, chrome, None)
                if w is not None:
                    w.hide()
            lay.addWidget(self.view)
        else:
            label = QLabel(theming.styled_case("lyrics show up here"))
            label.setAlignment(Qt.AlignCenter)
            label.setProperty("class", "dim")
            lay.addWidget(label)
        self._track = None
        self._shown_for = object()
        self._last_pos = -math.inf
        self._rippling = False
        engine.register(self)

    def _sync(self) -> None:
        if self.view is None or not self.isVisible():
            return
        try:
            api = self._api_provider() if self._api_provider is not None else None
        except Exception:
            api = None
        if api is not None and getattr(self.view, "api", None) is not api:
            self.view.api = api              # the active source changed
            self._shown_for = object()
        if self._shown_for is not self.engine.track:
            self._shown_for = self.engine.track
            self.view.show_for(self.engine.track)
            self._last_pos = -math.inf
        pos = self.engine.player_pos
        if pos is not None:
            self.view.update_position(pos)
            self._last_pos = pos

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync()

    def track_changed(self, track) -> None:
        self._sync()

    def position_changed(self, seconds: float) -> None:
        if self.view is None or not self.isVisible():
            return
        if self._shown_for is not self.engine.track:
            self._sync()
            return
        # karaoke interpolates the active word per tick; lines need ~4 Hz
        if getattr(self.view, "_karaoke_mode", False) or abs(seconds - self._last_pos) >= 0.25:
            self._last_pos = seconds
            self.view.update_position(seconds)

    def on_frame(self, now, dt) -> None:
        ripples = bool(self.engine.frame.ripples)
        if ripples or self._rippling:
            # repaint only the frame's edges: the lyrics underneath stay put
            r = self.rect()
            band = 10
            region = QRegion(0, 0, r.width(), band)
            region += QRegion(0, r.height() - band, r.width(), band)
            region += QRegion(0, 0, band, r.height())
            region += QRegion(r.width() - band, 0, band, r.height())
            self.update(region)
        self._rippling = ripples

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        paint_outline(p, self.outline_rect(), self.ripple_origin(), self.engine.frame)
        p.end()


# ---------- the grid ----------

class _EditPlate(QWidget):
    """the [edit] button's backing, so a busy tile can't show through it."""

    def __init__(self, engine: VisEngine, parent: QWidget) -> None:
        super().__init__(parent)
        self._engine = engine
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    def paintEvent(self, _event) -> None:
        pal = self._engine.pal
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(pal.bg, 235))
        if self._engine.look == "glow":
            p.setRenderHint(QPainter.Antialiasing, True)
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        else:
            p.drawRect(self.rect())
        p.end()


def make_tile(kind: str, engine: VisEngine, api_provider=None) -> QWidget:
    if kind in _SCENE_KINDS:
        return SceneTile(engine, kind)
    if kind == "deck":
        return DeckTile(engine)
    if kind == "lyrics":
        return LyricsTile(engine, api_provider)
    if kind == "river":
        return RiverTile(engine)
    if kind == "spectrogram":
        return SpectrogramTile(engine)
    if kind == "halo":
        return HaloTile(engine)
    if kind == "tapper":
        return TapperTile(engine)
    return SceneTile(engine, "spectrum")


class TileGrid(QWidget):
    """lays the tiles out. a tile keeps its state across a re-layout as
    long as its slot keeps its kind (the tapper's streak, the spectrogram's history)."""

    menu_requested = Signal(int, object)    # slot, global QPoint

    def __init__(self, engine: VisEngine, api_provider=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._api_provider = api_provider
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self.tiles: list[QWidget] = []
        self.layout_name = ""

    def configure(self, layout: str, kinds: list[str]) -> None:
        if layout not in LAYOUTS:
            layout = "stack"
        slots = LAYOUTS[layout]
        kinds = [k if k in KIND_LABELS else "spectrum" for k in kinds[:len(slots)]]
        while len(kinds) < len(slots):
            kinds.append("spectrum")
        if layout == self.layout_name and [t.kind for t in self.tiles] == kinds:
            return
        for tile in self.tiles:
            self._grid.removeWidget(tile)
        keep: list[QWidget] = []
        for slot, kind in enumerate(kinds):
            old = self.tiles[slot] if slot < len(self.tiles) else None
            if old is not None and old.kind == kind:
                keep.append(old)
                continue
            if old is not None:
                self._drop(old)
            tile = make_tile(kind, self.engine, self._api_provider)
            tile.setParent(self)
            tile.customContextMenuRequested.connect(
                lambda pos, t=tile: self._on_menu(t, pos))
            self._attach_edit(tile)
            keep.append(tile)
        for old in self.tiles[len(kinds):]:
            self._drop(old)
        self.tiles = keep
        for i in range(4):
            self._grid.setRowStretch(i, 0)
            self._grid.setColumnStretch(i, 0)
        rows, cols = STRETCH[layout]
        for i, s in enumerate(rows):
            self._grid.setRowStretch(i, s)
        for i, s in enumerate(cols):
            self._grid.setColumnStretch(i, s)
        caption_given = False
        for tile, (r, c, rs, cs) in zip(self.tiles, slots):
            self._grid.addWidget(tile, r, c, rs, cs)
            tile.solo = len(slots) == 1
            tile.caption_here = (not caption_given and tile.kind in ("spectrum", "mirror", "scope"))
            caption_given = caption_given or tile.caption_here
            tile.show()
        self.layout_name = layout
        self.engine.wake()

    # ---------- the edit button ----------
    # right-click isn't discoverable (and a tile may want the button), so each
    # tile also gets an [edit] button that shows while the mouse is over
    # it and opens the same menu.

    def _attach_edit(self, tile: QWidget) -> None:
        from .widgets import BracketButton
        plate = _EditPlate(self.engine, tile)
        btn = BracketButton("edit", parent=plate)
        btn.setToolTip(theming.styled_case("change this tile and its knobs"))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False, t=tile, b=btn: self._edit_clicked(t, b))
        lay = QVBoxLayout(plate)
        lay.setContentsMargins(3, 2, 3, 2)
        lay.addWidget(btn)
        plate.hide()
        tile._edit_btn = btn
        tile._edit_plate = plate
        tile.installEventFilter(self)

    def _place_edit(self, tile: QWidget) -> None:
        plate = getattr(tile, "_edit_plate", None)
        if plate is None:
            return
        plate.adjustSize()
        plate.move(max(0, tile.width() - plate.width() - 8), 8)

    def _edit_clicked(self, tile: QWidget, btn: QWidget) -> None:
        if tile in self.tiles:
            self.menu_requested.emit(self.tiles.index(tile),
                                     btn.mapToGlobal(btn.rect().bottomLeft()))

    def eventFilter(self, obj, event) -> bool:
        plate = getattr(obj, "_edit_plate", None)
        if plate is not None:
            kind = event.type()
            if kind == QEvent.Enter:
                self._place_edit(obj)
                plate.show()
                plate.raise_()
            elif kind == QEvent.Leave:
                plate.hide()
            elif kind == QEvent.Resize and plate.isVisible():
                self._place_edit(obj)
        return super().eventFilter(obj, event)

    def _drop(self, tile: QWidget) -> None:
        tile.removeEventFilter(self)
        try:
            tile.release()
        except Exception:
            pass
        tile.hide()
        tile.setParent(None)
        tile.deleteLater()

    def _on_menu(self, tile: QWidget, pos) -> None:
        if tile in self.tiles:
            self.menu_requested.emit(self.tiles.index(tile), tile.mapToGlobal(pos))

    def teardown(self) -> None:
        for tile in self.tiles:
            self._drop(tile)
        self.tiles = []
        self.layout_name = ""
