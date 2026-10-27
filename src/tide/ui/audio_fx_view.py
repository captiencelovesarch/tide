"""The [audio fx] page (reworked in 3.1).

Top to bottom:
  1. "sound", the on switch, what's playing through it ("bass · edited"),
     and [hold to compare]: the rack is bypassed while it's held, at the
     same loudness (the chain is level-matched, see audio_fx).
  2. Sounds: one tap sets the whole rack (audio_fx.SOUNDS), then your
     saved ones, then [save this]. Right-click a saved one to delete it.
  3. The eq as one curve (``EqCurve``): ten band points plus the bass and
     treble shelves as the two end points. The line is the real response
     of the filters, so overlapping bands show as they sound.
  4. Effect tiles in four groups (space, motion, tone, listening). Tap a
     tile to turn it on; its one amount slider shows while it's on.

The view owns a single ``AudioFxState`` and emits ``state_changed``
whenever any control flips. ``app.py`` wires that signal to the playback
router + a debounced settings save. ``compare_changed`` and
the sound's speed go to the window (slowed + reverb asks for a slower
speed; the rack itself has no speed, see window._apply_sound_speed).

Two faces (v2.0): the amount sliders sit in ``_FxControl`` wrappers
hosting a ``_ShelfSlider`` (bracket — brutalist) or a
``_SpringShelfSlider`` (modern). The face lands on show (``showEvent`` →
``_ensure_face``) because the view is built before app.py attaches
``window._settings`` — construction always starts bracket; a
mid-session flip lands the next time the page is shown.

Debounce discipline: one mpv filter-chain push costs 34-62 ms ON THE GUI
THREAD and restarts the reverb tail, so drags must NEVER push per-tick.
Live changes ride ``state_changed`` into the window's trailing-edge
debounce (~120 ms push / ~250 ms save); a commit SHORTENS that pending
work via ``_commit_fx_debounce`` — never bypasses it, because commits
arrive per wheel notch and key auto-repeat once motion is off. The curve
commits on release under the modern face only, like the spring sliders.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from ..audio_fx import (
    AudioFxState,
    EQ_BAND_COUNT,
    EQ_FREQUENCIES_HZ,
    EQ_GAIN_MAX_DB,
    EQ_GAIN_MIN_DB,
    REVERB_PRESETS,
    SOUNDS,
    eq_response_db,
)
from ..theming import styled_case
from .flow import FlowLayout
from .headings import Heading
from .spring_slider import SpringSlider
from .widgets import BracketButton




def _format_hz(hz: int) -> str:
    if hz >= 1000:
        return f"{hz // 1000}k" if hz % 1000 == 0 else f"{hz / 1000:.1f}k"
    return str(hz)


def _format_db(value: float) -> str:
    if abs(value) < 0.05:
        return "0"
    sign = "+" if value > 0 else "−"
    return f"{sign}{abs(value):.0f}"


def _format_suffixed(v: float, suffix: str) -> str:
    """Shared readout format — both faces must print values identically."""
    if suffix == " dB":
        return f"{_format_db(v)}{suffix}"
    if suffix == "%":
        return f"{int(round(v * 100))}%"
    if suffix == "x":
        return f"{v:.2f}×"
    return f"{v:.1f}{suffix}"


def _active_face(widget: QWidget) -> str:
    """``"spring"`` under the modern personality, else ``"bracket"`` —
    the speed popover's exact rule; unknown/absent settings → bracket."""
    settings = getattr(widget.window(), "_settings", None)
    preset = str(getattr(settings, "preset", "") or "")
    return "spring" if preset == "modern" else "bracket"


# Commit → drain delay: half the push debounce, long enough to swallow a
# burst (X11 auto-repeat ~33 ms; each drain costs a 34-62 ms mpv rebuild).
COMMIT_DRAIN_MS = 60

_FX_DEBOUNCE_TIMERS = (
    ("_audio_fx_push_timer", "_flush_audio_fx_push"),
    ("_audio_fx_save_timer", "_flush_audio_fx_state"),
)


def _fx_debounce_host(widget: QWidget):
    """The MainWindow-ish object holding the fx debounce timers, or None —
    walks parent() past Qt.Popup windows; duck-typed (tests find nothing)."""
    w = widget.window()
    while w is not None and not hasattr(w, "_flush_audio_fx_push"):
        w = w.parent()
    return w


def _drain_fx_debounce(host) -> None:
    """Cut short what the debounce still owes. Idle timers are skipped —
    nothing pending means a flush would just redo the same work."""
    for timer_attr, flush_attr in _FX_DEBOUNCE_TIMERS:
        try:
            timer = getattr(host, timer_attr, None)
            if timer is not None and timer.isActive():
                timer.stop()
                getattr(host, flush_attr)()
        except Exception:
            pass


def _commit_fx_debounce(widget: QWidget) -> None:
    """Ask the pending debounced fx work (~120 ms push, ~250 ms save) to
    land now-ish — via a ``COMMIT_DRAIN_MS`` single-shot, never an inline
    flush: with motion off every wheel notch and key auto-repeat commits,
    and an inline flush meant a synchronous 34-62 ms ``af set`` plus a
    TOML write per notch. During a burst nothing pushes; one drain lands
    the final value. Nothing pending → nothing scheduled."""
    host = _fx_debounce_host(widget)
    if host is None:
        return
    try:
        pending = any(
            getattr(host, attr, None) is not None
            and getattr(host, attr).isActive()
            for attr, _flush in _FX_DEBOUNCE_TIMERS
        )
    except RuntimeError:
        return
    if not pending:
        return
    timer = getattr(host, "_audio_fx_commit_timer", None)
    if timer is None:
        timer = QTimer(host)    # child of the host: dies with the window
        timer.setSingleShot(True)
        timer.setInterval(COMMIT_DRAIN_MS)
        timer.timeout.connect(lambda t=timer: _drain_fx_debounce(t.parent()))
        host._audio_fx_commit_timer = timer
    timer.start()




class _ShelfSlider(QWidget):
    """Horizontal labeled slider with a live readout. Used for bass +
    treble + reverb wet + stereo width."""

    value_changed = Signal(float)

    def __init__(self, label: str, vmin: float, vmax: float, step: float = 0.1,
                 suffix: str = " dB", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vmin = vmin
        self._vmax = vmax
        self._step = step
        self._suffix = suffix
        self._silent = False
        self._scale = int(round(1.0 / step)) if step > 0 else 1

        self._label = QLabel(styled_case(label))
        self._label.setMinimumWidth(110)
        self._label.setVisible(bool(label))   # tiles carry their own name
        self._readout = QLabel("0")
        self._readout.setMinimumWidth(56)
        self._readout.setVisible(bool(label))  # a tile shows the value itself
        self._readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(int(round(vmin * self._scale)))
        self._slider.setMaximum(int(round(vmax * self._scale)))
        self._slider.setSingleStep(1)
        self._slider.setPageStep(self._scale)
        self._slider.valueChanged.connect(self._on_slider)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._label)
        row.addWidget(self._slider, stretch=1)
        row.addWidget(self._readout)

    def value(self) -> float:
        return self._slider.value() / self._scale

    def set_value(self, v: float) -> None:
        clamped = max(self._vmin, min(self._vmax, float(v)))
        target = int(round(clamped * self._scale))
        if target == self._slider.value():
            self._readout.setText(self._format(clamped))
            return
        self._silent = True
        try:
            self._slider.setValue(target)
        finally:
            self._silent = False
        self._readout.setText(self._format(clamped))

    def _format(self, v: float) -> str:
        return _format_suffixed(v, self._suffix)

    def _on_slider(self, value: int) -> None:
        v = value / self._scale
        self._readout.setText(self._format(v))
        if not self._silent:
            self.value_changed.emit(v)


class _SpringShelfSlider(QWidget):
    """Modern face for a continuous rack control: same row shape and
    formats as ``_ShelfSlider``, a SpringSlider in the groove seat, one
    detent on the control's stock value. ``value_changed`` fires live,
    ``value_committed`` on landing, ``set_value`` never emits."""

    value_changed = Signal(float)
    value_committed = Signal(float)

    def __init__(self, label: str, vmin: float, vmax: float, step: float = 0.1,
                 suffix: str = " dB", detents: tuple = (),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._suffix = suffix

        self._label = QLabel(styled_case(label))
        self._label.setMinimumWidth(110)
        self._label.setVisible(bool(label))   # tiles carry their own name
        self._readout = QLabel("0")
        self._readout.setMinimumWidth(56)
        self._readout.setVisible(bool(label))  # a tile shows the value itself
        self._readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._slider = SpringSlider()
        self._slider.set_range(vmin, vmax, step)
        self._slider.set_detents(detents)
        self._slider.set_formatter(self._format)
        self._slider.value_changed.connect(self._on_changed)
        self._slider.value_committed.connect(self.value_committed)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._label)
        row.addWidget(self._slider, stretch=1)
        row.addWidget(self._readout)

        self._readout.setText(self._format(self._slider.value()))

    def value(self) -> float:
        return self._slider.value()

    def set_value(self, v: float) -> None:
        clamped = max(self._vmin, min(self._vmax, float(v)))
        self._slider.set_value(clamped)     # programmatic — never emits
        self._readout.setText(self._format(self._slider.value()))

    def _format(self, v: float) -> str:
        return _format_suffixed(v, self._suffix)

    def _on_changed(self, v: float) -> None:
        self._readout.setText(self._format(v))
        self.value_changed.emit(v)


class _FxControl(QWidget):
    """Hosts one continuous control and swaps its face in place — the
    wrapper is what the view wires and the layouts hold, so a swap never
    touches layout positions or external connections. ``value_committed``
    only ever fires from the spring face (bracket has no commit notion)."""

    value_changed = Signal(float)
    value_committed = Signal(float)

    def __init__(self, label: str, vmin: float, vmax: float, step: float = 0.1,
                 suffix: str = " dB", detents: tuple = (),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = (label, vmin, vmax, step, suffix)
        self._detents = tuple(detents)
        self._face = ""
        self._inner: QWidget | None = None
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)
        self.set_face("bracket")

    def face(self) -> str:
        return self._face

    def set_face(self, face: str) -> None:
        face = "spring" if face == "spring" else "bracket"
        if face == self._face:
            return
        old = self._inner
        carried = old.value() if old is not None else None
        label, vmin, vmax, step, suffix = self._spec
        if face == "spring":
            inner = _SpringShelfSlider(label, vmin, vmax, step, suffix=suffix,
                                       detents=self._detents)
            inner.value_committed.connect(self.value_committed)
        else:
            inner = _ShelfSlider(label, vmin, vmax, step, suffix=suffix)
        inner.value_changed.connect(self.value_changed)
        self._face = face
        self._inner = inner
        self._box.addWidget(inner)
        if old is not None:
            old.hide()
            self._box.removeWidget(old)
            old.deleteLater()
            if carried is not None:
                inner.set_value(carried)    # silent on both faces

    def value(self) -> float:
        return self._inner.value()

    def set_value(self, v: float) -> None:
        self._inner.set_value(v)

    def readout_text(self) -> str:
        return self._inner._readout.text()




def _tok(key: str, default: str) -> QColor:
    t = theming.manager().current_effective()
    return QColor(t.token(key, default) if t is not None else default)


def _modern() -> bool:
    t = theming.manager().current_effective()
    return getattr(t, "aesthetic", "") == "modern"


# ---------------------------------------------------------------------------
# the eq curve
# ---------------------------------------------------------------------------

LOW_SHELF = EQ_BAND_COUNT          # handle index of the bass shelf
HIGH_SHELF = EQ_BAND_COUNT + 1     # and the treble shelf
_F_MIN, _F_MAX = 20.0, 20000.0
_DB_RANGE = 15.0                   # the plot shows ±15 dB
_CURVE_POINTS = 180


class EqCurve(QWidget):
    """Ten band points and two shelf points on the real response curve.
    Drag a point up or down (0.5 dB steps), double-click to zero it, or
    use the wheel over it."""

    band_changed = Signal(int, float)   # handle index, dB
    committed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: AudioFxState | None = None
        self._drag: int | None = None
        self._hover: int | None = None
        self._cache_key = None
        self._cache: list[float] = []
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.SizeVerCursor)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, *_args) -> None:
        self.update()

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(640, 190)

    def set_state(self, state: AudioFxState) -> None:
        self._state = state
        self.update()

    # ---- geometry ----

    def _plot(self) -> QRectF:
        return QRectF(48, 12, max(10, self.width() - 62), max(10, self.height() - 38))

    def _x(self, f: float) -> float:
        r = self._plot()
        t = math.log(f / _F_MIN) / math.log(_F_MAX / _F_MIN)
        return r.left() + t * r.width()

    def _y(self, db: float) -> float:
        r = self._plot()
        db = max(-_DB_RANGE, min(_DB_RANGE, db))
        return r.center().y() - db / _DB_RANGE * (r.height() / 2)

    def _db_at(self, y: float) -> float:
        r = self._plot()
        db = (r.center().y() - y) / (r.height() / 2) * _DB_RANGE
        db = max(EQ_GAIN_MIN_DB, min(EQ_GAIN_MAX_DB, db))
        return round(db * 2) / 2

    def _handle_freq(self, i: int) -> float:
        if i == LOW_SHELF:
            return _F_MIN
        if i == HIGH_SHELF:
            return _F_MAX
        return float(EQ_FREQUENCIES_HZ[i])

    def handle_db(self, i: int) -> float:
        s = self._state
        if s is None:
            return 0.0
        if i == LOW_SHELF:
            return float(s.bass_db)
        if i == HIGH_SHELF:
            return float(s.treble_db)
        return float(s.eq_bands[i])

    def _nearest(self, x: float) -> int:
        return min(range(HIGH_SHELF + 1), key=lambda i: abs(self._x(self._handle_freq(i)) - x))

    # ---- input ----

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() != Qt.LeftButton or self._state is None:
            return
        self._drag = self._nearest(ev.position().x())
        self._set(self._drag, self._db_at(ev.position().y()))

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if self._drag is not None:
            self._set(self._drag, self._db_at(ev.position().y()))
        else:
            hover = self._nearest(ev.position().x())
            if hover != self._hover:
                self._hover = hover
                self.update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.update()
            self.committed.emit()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        if self._state is None:
            return
        self._set(self._nearest(ev.position().x()), 0.0)
        self.committed.emit()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = None
        self.update()
        super().leaveEvent(ev)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if self._state is None:
            ev.ignore()
            return
        steps = ev.angleDelta().y() / 120.0
        if not steps:
            ev.ignore()
            return
        i = self._nearest(ev.position().x())
        db = max(EQ_GAIN_MIN_DB, min(EQ_GAIN_MAX_DB, self.handle_db(i) + 0.5 * steps))
        self._set(i, round(db * 2) / 2)
        self.committed.emit()
        ev.accept()

    def _set(self, i: int, db: float) -> None:
        if abs(db - self.handle_db(i)) < 1e-6:
            return
        self.band_changed.emit(i, db)
        self.update()

    # ---- paint ----

    def _response(self) -> list[float]:
        s = self._state
        key = (tuple(s.eq_bands), s.bass_db, s.treble_db, s.sub_bass, s.sub_amount)
        if key != self._cache_key:
            freqs = [_F_MIN * (_F_MAX / _F_MIN) ** (k / (_CURVE_POINTS - 1))
                     for k in range(_CURVE_POINTS)]
            self._cache = list(zip(freqs, eq_response_db(s, freqs)))
            self._cache_key = key
        return self._cache

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        modern = _modern()
        accent = _tok("accent", "#d4b95e")
        fg = _tok("fg", "#dddddd")
        dim = _tok("dim", "#888888")
        surface = _tok("surface_1", "#202020")
        line = _tok("border_dim", "#333333")
        radius = 12.0 if modern else 0.0
        outer = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(line, 1))
        p.setBrush(surface)
        p.drawRoundedRect(outer, radius, radius)

        r = self._plot()
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.85))
        p.setFont(small)
        # grid: 0 dB in dim, ±6 / ±12 dashed and fainter, labelled left
        faint = QColor(dim)
        faint.setAlpha(90)
        for db in (-12, -6, 0, 6, 12):
            pen = QPen(faint if db else dim, 1)
            if db:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            y = self._y(db)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(dim)
            p.drawText(QRectF(4, y - 8, r.left() - 16, 16),
                       Qt.AlignRight | Qt.AlignVCenter,
                       "0" if not db else f"{db:+d}".replace("-", "−"))
        p.setPen(dim)
        for f in EQ_FREQUENCIES_HZ:
            x = self._x(f)
            p.drawText(QRectF(x - 20, r.bottom() + 6, 40, 16), Qt.AlignCenter, _format_hz(f))

        if self._state is None:
            p.end()
            return
        pts = [QPointF(self._x(f), self._y(db)) for f, db in self._response()]
        curve = QPainterPath(pts[0])
        for pt in pts[1:]:
            curve.lineTo(pt)
        fill = QPainterPath(curve)
        fill.lineTo(QPointF(pts[-1].x(), self._y(0)))
        fill.lineTo(QPointF(pts[0].x(), self._y(0)))
        fill.closeSubpath()
        wash = QColor(accent)
        wash.setAlpha(46)
        p.setPen(Qt.NoPen)
        p.setBrush(wash)
        p.drawPath(fill)
        p.setPen(QPen(accent, 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(curve)

        active = self._drag if self._drag is not None else self._hover
        for i in range(HIGH_SHELF + 1):
            c = QPointF(self._x(self._handle_freq(i)), self._y(self.handle_db(i)))
            p.setPen(QPen(accent, 2.0))
            p.setBrush(accent if i == active else surface)
            rad = 6.0 if i == active else 4.5
            if i in (LOW_SHELF, HIGH_SHELF):
                diamond = QPainterPath()
                diamond.moveTo(c.x(), c.y() - rad - 1)
                diamond.lineTo(c.x() + rad + 1, c.y())
                diamond.lineTo(c.x(), c.y() + rad + 1)
                diamond.lineTo(c.x() - rad - 1, c.y())
                diamond.closeSubpath()
                p.drawPath(diamond)
            elif modern:
                p.drawEllipse(c, rad, rad)
            else:
                p.drawRect(QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad))
        if active is not None:
            db = self.handle_db(active)
            name = {LOW_SHELF: "bass shelf", HIGH_SHELF: "treble shelf"}.get(
                active, f"{_format_hz(EQ_FREQUENCIES_HZ[active])} hz"
                if active < EQ_BAND_COUNT else "")
            text = styled_case(f"{name}  {_format_db(db)} db")
            x = self._x(self._handle_freq(active))
            box = QRectF(x - 60, r.top(), 120, 16)
            box.moveLeft(max(r.left(), min(r.right() - 120, box.left())))
            p.setPen(fg)
            p.drawText(box, Qt.AlignCenter, text)
        p.end()


# ---------------------------------------------------------------------------
# effect tiles
# ---------------------------------------------------------------------------

# key, name, one line, the state switch, and the amount slider:
# (state attr, lo, hi, step, suffix, detent, scale) — the state holds
# slider value × scale. None = no slider. Width and reverb are special
# cases in the view (no plain bool behind them).
TILE_GROUPS: tuple[tuple[str, tuple], ...] = (
    ("space", (
        ("reverb", "reverb", "a room round the song", None,
         ("reverb_wet", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
        ("eightd", "8d", "the song circles your head", "eightd",
         ("eightd_speed", 0.0, 1.0, 0.05, "%", 0.4, 1.0)),
    )),
    ("motion", (
        ("chorus", "chorus", "doubled and wider", "chorus",
         ("chorus_amount", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
        ("flanger", "flanger", "jet sweep", "flanger",
         ("flanger_amount", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
        ("phaser", "phaser", "slow swirl", "phaser",
         ("phaser_amount", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
        ("tremolo", "tremolo", "volume wobble", "tremolo",
         ("tremolo_speed", 0.5, 10.0, 0.5, " Hz", 5.0, 1.0)),
    )),
    ("tone", (
        ("sub_bass", "sub bass", "low end you feel", "sub_bass",
         ("sub_amount", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
        ("exciter", "exciter", "a clearer top end", "exciter",
         ("exciter_amount", 0.0, 1.0, 0.05, "%", 0.4, 5.0)),
        ("lofi", "lofi", "tape wobble and crunch", "lofi",
         ("lofi_amount", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
        ("compressor", "compressor", "quiet parts up, loud parts down", "compressor",
         ("compressor_amount", 0.0, 1.0, 0.05, "%", 0.5, 1.0)),
    )),
    ("listening", (
        ("loudness", "even volume", "every song at about the same level",
         "loudness_norm", None),
        ("crossfeed", "crossfeed", "less hard left and right on headphones",
         "crossfeed", ("crossfeed_strength", 0.0, 1.0, 0.05, "%", 0.4, 1.0)),
        ("width", "width", "narrower or wider stereo", None,
         ("stereo_width", 0.0, 2.0, 0.1, "x", 1.0, 1.0)),
        ("mono", "mono", "both speakers play the same", "mono", None),
    )),
)


class FxTile(QFrame):
    """One effect: name, a line on what it does, and its amount slider
    while it's on. Clicking anywhere but the slider toggles it."""

    toggled = Signal(str, bool)

    def __init__(self, key: str, name: str, blurb: str, control,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self._name = name
        self._on = False
        self.setObjectName("fxTile")
        self.setProperty("on", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.name = QLabel()
        self.name.setObjectName("fxTileName")
        self.value = QLabel()
        self.value.setObjectName("fxTileValue")
        self.value.setProperty("class", "dim")
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value.hide()
        self.blurb = QLabel(styled_case(blurb))
        self.blurb.setObjectName("fxTileBlurb")
        self.blurb.setProperty("class", "dim")
        self.blurb.setWordWrap(True)
        self.control = control
        self.extra: QWidget | None = None
        self._col = QVBoxLayout(self)
        self._col.setContentsMargins(12, 10, 12, 10)
        self._col.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.name, stretch=1)
        top.addWidget(self.value)
        self._col.addLayout(top)
        self._col.addWidget(self.blurb)
        if control is not None:
            self._col.addWidget(control)
            control.hide()
            control.value_changed.connect(lambda _v: self.refresh_value())
        self._refresh_name()

    def add_extra(self, w: QWidget) -> None:
        self.extra = w
        self._col.insertWidget(2, w)
        w.hide()

    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        """Silent: the view calls this while syncing."""
        on = bool(on)
        self._on = on
        self.setProperty("on", on)
        self.style().unpolish(self)
        self.style().polish(self)
        if self.control is not None:
            self.control.setVisible(on)
            self.value.setVisible(on)
            self.refresh_value()
        if self.extra is not None:
            self.extra.setVisible(on)
        self._refresh_name()

    def refresh_value(self) -> None:
        if self.control is not None:
            self.value.setText(self.control.readout_text())

    def _refresh_name(self) -> None:
        name = styled_case(self._name)
        if not _modern():
            name = f"[{'x' if self._on else ' '}] {name}"
        self.name.setText(name)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.set_on(not self._on)
            self.toggled.emit(self.key, self._on)
        super().mouseReleaseEvent(ev)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

class AudioFxView(QWidget):
    """The full [audio fx] page. The single source of truth for the
    rack lives in ``self._state``; UI controls reflect it and call back
    to mutate it."""

    state_changed = Signal(object)    # AudioFxState
    compare_changed = Signal(bool)    # held: play without the rack

    def __init__(self, state: AudioFxState | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state if state is not None else AudioFxState()
        # Construction always builds bracket — window._settings doesn't
        # exist yet at window-build time; showEvent lands the real face.
        self._face = "bracket"
        self._last_width = 1.4
        self._last_room = "hall"

        # 1. header
        heading = Heading("sound", 40)
        self._master_box = QCheckBox(styled_case("on"))
        self._master_box.setChecked(self._state.master_enabled)
        self._master_box.toggled.connect(self._on_master_toggle)
        self._readout = QLabel("")
        self._readout.setObjectName("audioFxActivePreset")
        self._readout.setProperty("class", "dim")
        self._compare_btn = BracketButton("hold to compare")
        self._compare_btn.setRole("pill")
        self._compare_btn.setToolTip(styled_case(
            "plays without the rack while you hold it, at the same loudness"))
        self._compare_btn.pressed.connect(lambda: self.compare_changed.emit(True))
        self._compare_btn.released.connect(lambda: self.compare_changed.emit(False))
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(heading)
        head.addWidget(self._master_box)
        head.addWidget(self._readout, stretch=1)
        head.addWidget(self._compare_btn)

        # 2. sounds
        self._sound_flow = FlowLayout(h_gap=6, v_gap=6)
        self._sound_box = QWidget()
        self._sound_box.setLayout(self._sound_flow)
        self._sound_chips: dict[str, BracketButton] = {}

        # 3. eq
        self._curve = EqCurve()
        self._curve.band_changed.connect(self._on_curve_changed)
        self._curve.committed.connect(self._on_curve_committed)

        # 4. tiles
        self._tiles: dict[str, FxTile] = {}
        self._controls: dict[str, _FxControl] = {}
        self._specs: dict[str, tuple] = {}
        # one grid for every group, so the columns line up down the page
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        row = 0
        for group, tiles in TILE_GROUPS:
            grid.addWidget(self._section(group), row, 0, 1, 4)
            row += 1
            for i, (key, name, blurb, switch, amount) in enumerate(tiles):
                ctrl = None
                if amount is not None:
                    attr, lo, hi, step, suffix, detent, scale = amount
                    ctrl = _FxControl("", lo, hi, step, suffix=suffix, detents=(detent,))
                    ctrl.value_changed.connect(lambda v, k=key: self._on_amount(k, v))
                    ctrl.value_committed.connect(self._on_fx_commit)
                    self._controls[key] = ctrl
                tile = FxTile(key, name, blurb, ctrl)
                tile.toggled.connect(self._on_tile)
                self._tiles[key] = tile
                self._specs[key] = (switch, amount)
                grid.addWidget(tile, row + i // 4, i % 4, Qt.AlignTop)
            row += (len(tiles) + 3) // 4
        self._room_combo = QComboBox()
        for name in REVERB_PRESETS:
            if name != "off":
                self._room_combo.addItem(styled_case(name), name)
        self._room_combo.currentIndexChanged.connect(self._on_room)
        self._tiles["reverb"].add_extra(self._room_combo)
        # the names the tests and the popover know these by
        self._reverb_wet = self._controls["reverb"]
        self._stereo_slider = self._controls["width"]

        col = QVBoxLayout()
        col.setContentsMargins(28, 24, 28, 24)
        col.setSpacing(10)
        col.addLayout(head)
        col.addWidget(self._section("sounds"))
        col.addWidget(self._sound_box)
        col.addWidget(self._section("eq · drag a point, double-click to zero it"))
        col.addWidget(self._curve)
        col.addLayout(grid)
        col.addStretch(1)

        scroll_inner = QWidget()
        scroll_inner.setLayout(col)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_inner)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        theming.manager().theme_changed.connect(self._on_theme)
        self._restyle()
        self._rebuild_sounds()
        # Initial sync from state — sets all the controls without emitting.
        self.sync_from_state()

    def _section(self, text: str) -> QLabel:
        lab = QLabel(styled_case(text))
        lab.setObjectName("audioFxSectionLabel")
        lab.setProperty("class", "dim")
        return lab

    # ---------- public ----------

    def face(self) -> str:
        return self._face

    def state(self) -> AudioFxState:
        return self._state

    def set_state(self, state: AudioFxState) -> None:
        self._state = state
        self._rebuild_sounds()
        self.sync_from_state()

    def sync_from_state(self) -> None:
        """Push self._state to every control. Signals are gated so this
        does NOT emit ``state_changed`` — used on first show and after
        ``set_state``."""
        s = self._state
        self._master_box.blockSignals(True)
        self._master_box.setChecked(s.master_enabled)
        self._master_box.blockSignals(False)
        self._curve.set_state(s)
        if s.reverb_preset != "off":
            self._last_room = s.reverb_preset
        if abs(s.stereo_width - 1.0) >= 0.01:
            self._last_width = s.stereo_width
        self._room_combo.blockSignals(True)
        self._room_combo.setCurrentIndex(max(0, self._room_combo.findData(self._last_room)))
        self._room_combo.blockSignals(False)
        for key, tile in self._tiles.items():
            tile.set_on(self._is_on(key))
            ctrl = self._controls.get(key)
            if ctrl is not None:
                attr, *_rest, scale = self._specs[key][1]
                if key == "width":
                    ctrl.set_value(self._last_width)
                else:
                    ctrl.set_value(float(getattr(s, attr)) / scale)
                tile.refresh_value()
        self._refresh_readout()

    # ---------- sounds ----------

    def _rebuild_sounds(self) -> None:
        while self._sound_flow.count():
            item = self._sound_flow.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        self._sound_chips = {}
        names = list(SOUNDS) + [x.name for x in self._state.saved_sounds]
        for name in names:
            b = BracketButton(name)
            b.setRole("pill")
            b.clicked.connect(lambda _c=False, n=name: self._on_sound(n))
            if name not in SOUNDS:
                b.setContextMenuPolicy(Qt.CustomContextMenu)
                b.customContextMenuRequested.connect(
                    lambda _pos, n=name, w=b: self._sound_menu(n, w))
                b.setToolTip(styled_case("your sound. right-click to delete it."))
            self._sound_chips[name] = b
            self._sound_flow.addWidget(b)
        save = BracketButton("save this")
        save.setRole("pill")
        save.setMuted(True)
        # a modal opened inside the click handler segfaults on PySide6 +
        # py3.14; defer it a turn
        save.clicked.connect(lambda: QTimer.singleShot(0, self._ask_save))
        self._sound_flow.addWidget(save)
        self._save_btn = save
        self._refresh_readout()

    def _on_sound(self, name: str) -> None:
        if not self._state.apply_sound(name):
            return
        if name != "flat":
            self._state.master_enabled = True
        self._ui_sound("toggle_on")
        self.sync_from_state()
        self._emit()

    def _ask_save(self) -> None:
        name, ok = QInputDialog.getText(self, styled_case("save this sound"),
                                        styled_case("name:"))
        if not ok or not name.strip():
            return
        self._state.save_sound(name)
        self._rebuild_sounds()
        self._emit()

    def _sound_menu(self, name: str, w: QWidget) -> None:
        menu = QMenu(w)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        act = QAction(styled_case(f"delete {name}"), menu)
        act.triggered.connect(lambda: self._delete_sound(name))
        menu.addAction(act)
        menu.exec(w.mapToGlobal(w.rect().bottomLeft()))

    def _delete_sound(self, name: str) -> None:
        self._state.delete_sound(name)
        QTimer.singleShot(0, self._rebuild_sounds)
        self._emit()

    def _refresh_readout(self) -> None:
        label = self._state.sound_label()
        current = label.split(" · ")[0]
        for name, chip in self._sound_chips.items():
            chip.setActiveState(name == current and self._state.master_enabled)
        if not self._state.master_enabled:
            text = "off"
        else:
            text = label or "your own mix"
        self._readout.setText(styled_case(text))

    # ---------- face + theme ----------

    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._ensure_face()

    def _fx_controls(self) -> tuple:
        return tuple(self._controls.values())

    def _ensure_face(self) -> None:
        """Land the personality's face. Runs every show, so a mid-session
        preset flip lands the next time the page is opened — never mid-view."""
        face = _active_face(self)
        if face == self._face:
            return
        self._face = face
        for ctrl in self._fx_controls():
            ctrl.set_face(face)
        # set_face carried each control's value; this also refreshes
        # readouts and the rest, without emitting.
        self.sync_from_state()

    def _on_theme(self, *_args) -> None:
        self._restyle()
        for tile in self._tiles.values():
            tile._refresh_name()

    def _restyle(self) -> None:
        modern = _modern()
        radius = 12 if modern else 0
        sc = _tok("surface_1", "#202020")
        surface = f"rgba({sc.red()}, {sc.green()}, {sc.blue()}, {sc.alpha()})"
        line = _tok("border_dim", "#333333").name()
        accent = _tok("accent", "#d4b95e").name()
        bold = "font-weight: 600;" if modern else ""
        self.setStyleSheet(
            f"#fxTile {{ background: {surface}; border: 1px solid {line};"
            f" border-radius: {radius}px; }}"
            f"#fxTile[on=\"true\"] {{ border: 1px solid {accent}; }}"
            f"#fxTileName {{ {bold} }}"
        )

    # ---------- handlers ----------

    def _ui_sound(self, key: str) -> None:
        w = self.window()
        player = getattr(w, "ui_sounds", None) if w is not None else None
        if player is not None:
            try:
                player.play(key)
            except Exception:
                pass

    def _on_master_toggle(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.master_enabled = bool(on)
        self._emit()

    def _on_curve_changed(self, idx: int, db: float) -> None:
        s = self._state
        if idx == LOW_SHELF:
            s.bass_db = float(db)
        elif idx == HIGH_SHELF:
            s.treble_db = float(db)
        elif 0 <= idx < len(s.eq_bands):
            s.eq_bands[idx] = float(db)
        else:
            return
        self._emit()

    def _on_curve_committed(self) -> None:
        # modern face only: brutalist keeps the plain debounce timing
        if self._face == "spring":
            _commit_fx_debounce(self)

    def _on_fx_commit(self, _value: float) -> None:
        # Spring-face interaction landed — see _commit_fx_debounce.
        _commit_fx_debounce(self)

    def _is_on(self, key: str) -> bool:
        s = self._state
        if key == "reverb":
            return s.reverb_preset != "off" and s.reverb_wet > 0.0
        if key == "width":
            return abs(s.stereo_width - 1.0) >= 0.01
        return bool(getattr(s, self._specs[key][0]))

    def _on_tile(self, key: str, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        s = self._state
        if key == "reverb":
            s.reverb_preset = self._last_room if on else "off"
            if on and s.reverb_wet <= 0.0:
                s.reverb_wet = 0.5
                self._reverb_wet.set_value(0.5)
        elif key == "width":
            s.stereo_width = self._last_width if on else 1.0
        else:
            setattr(s, self._specs[key][0], on)
        if on and not s.master_enabled:
            s.master_enabled = True
            self._master_box.blockSignals(True)
            self._master_box.setChecked(True)
            self._master_box.blockSignals(False)
        self._emit()

    def _on_amount(self, key: str, value: float) -> None:
        attr, *_rest, scale = self._specs[key][1]
        if key == "width":
            self._last_width = float(value)
            if not self._tiles["width"].is_on():
                return
        setattr(self._state, attr, float(value) * scale)
        self._emit()

    def _on_room(self, _idx: int) -> None:
        room = self._room_combo.currentData() or "hall"
        self._last_room = room
        if self._state.reverb_preset != "off":
            self._state.reverb_preset = room
            self._emit()

    def _emit(self) -> None:
        self._curve.update()
        self._refresh_readout()
        self.state_changed.emit(self._state)
