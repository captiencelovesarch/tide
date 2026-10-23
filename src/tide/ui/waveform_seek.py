"""The waveform seek bar (2.3): the song's loudness drawn over the
progress bar, in three moments.

  * **loading** — while the beat-map decode runs, the part decoded so far
    shows as bars and a line marks the frontier, moving right as the
    stream loads (each partial map's ``duration`` is the frontier).
  * **loaded** — once the map is complete and playback has begun, the
    whole song shows for a few seconds, then fades back to the plain bar.
  * **seeking** — hover or drag on the bar and it comes back, with a line
    and the time at the pointer's preview position.

No second decode: the data is the beat map the ambient controller
already keeps (``AmbientController.set_watching`` holds it for us even
with the backdrop pulse off; ``beat_clock()`` hands it over). The
envelope is the map's full-band ``loudness`` when it carries one, else
its ``swell`` (the kick band — coarser, but always there).

The waveform stands ON the progress bar: its bars grow up from just
above the bar's top edge, into room the strip reserves while the
waveform is enabled (``ROOM_PX``), and the bar itself stays as it is —
every variant and both personalities keep their own. brutalist draws
hard 2px cells and a 1px rule; modern rounded bars and a soft line. The
played colour feathers into the unplayed one across a band centred on
the playhead (``feather``), never a hard switch. Motion OFF snaps
instead of fading. The overlay never takes the mouse: seeking stays the
progress bar's own.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .. import theming
from . import scale

MODES: tuple[str, ...] = ("on", "hover", "off")
LOADED_HOLD_MS = 3000     # the whole song on screen after it loads
POLL_MS = 200             # partial maps arrive a few times a second
FADE_IN = "short"
FADE_OUT = "med"
WAVE_PX = 20              # the waveform's height above the bar
ROOM_PX = 14              # what the strip reserves for it (+ its row gap)
FEATHER_PX = 36           # played → unplayed blend band at the playhead


def blend(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t)


def feather(x: float, play_x: float, width: float) -> float:
    """0 = fully played, 1 = fully unplayed, smoothstepped across a band
    ``width`` wide centred on ``play_x``."""
    if width <= 0:
        return 0.0 if x < play_x else 1.0
    t = (x - (play_x - width / 2.0)) / width
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def envelope(beat_map) -> tuple[tuple[float, ...], float]:
    """(values 0..1, samples per second) from a beat map — the full-band
    loudness when the map has it, else the kick-band swell."""
    if beat_map is None:
        return (), 0.0
    loud = getattr(beat_map, "loudness", None)
    rate = float(getattr(beat_map, "loudness_rate", 0.0) or 0.0)
    if loud and rate > 0:
        return tuple(loud), rate
    swell = getattr(beat_map, "swell", None) or ()
    return tuple(swell), float(getattr(beat_map, "swell_rate", 0.0) or 0.0)


def bars_for(values, rate: float, total: float, frontier: float,
             count: int) -> list[float | None]:
    """``count`` bar heights (0..1) across ``total`` seconds; None for a
    bar past the decode ``frontier``. Each bar is mostly its slice's mean
    with a quarter of its peak: pure peaks flattened long songs (a bar
    spans seconds, and most seconds hold one near-max frame — 75% of the
    bars of a 6-minute song sat at 1.0), pure means hide short hits."""
    if count <= 0:
        return []
    if not values or rate <= 0 or total <= 0:
        return [None] * count
    out: list[float | None] = []
    n = len(values)
    span = total / count
    for i in range(count):
        t0, t1 = i * span, (i + 1) * span
        if t0 >= frontier:
            out.append(None)
            continue
        a = max(0, int(math.floor(t0 * rate)))
        b = min(n, max(a + 1, int(math.ceil(min(t1, frontier) * rate))))
        if a >= n:
            out.append(None)
            continue
        chunk = values[a:b]
        level = 0.75 * (sum(chunk) / len(chunk)) + 0.25 * max(chunk)
        out.append(max(0.0, min(1.0, level)))
    return out


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class WaveformOverlay(QWidget):
    """Paints the waveform; owned and driven by :class:`WaveformSeek`."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.alpha = 0.0
        self.values: tuple[float, ...] = ()
        self.rate = 0.0
        self.total = 0.0
        self.frontier = 0.0
        self.position = 0.0
        self.loading = False
        self.hover_x: float | None = None      # in our coords
        self.hide()

    def bar_metrics(self) -> tuple[int, int]:
        """(bar width, gap) in px for the active personality."""
        modern = getattr(theming.manager().current_effective(), "aesthetic", "") == "modern"
        return (scale.px(3), scale.px(2)) if modern else (scale.px(2), scale.px(1))

    def bar_count(self) -> int:
        bw, gap = self.bar_metrics()
        return max(1, (self.width() + gap) // (bw + gap))

    def paintEvent(self, _ev) -> None:
        if self.alpha <= 0.001:
            return
        theme = theming.manager().current_effective()
        modern = getattr(theme, "aesthetic", "") == "modern"
        tok = (lambda k, d: QColor(theme.token(k, d))) if theme is not None \
            else (lambda _k, d: QColor(d))
        fg, dim, accent = tok("fg", "#e6e6e6"), tok("dim", "#666"), tok("accent", "#d4b95e")
        played = accent if modern else fg
        unplayed = QColor(fg if modern else dim)
        if modern:
            unplayed.setAlpha(110)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, modern)
        p.setOpacity(self.alpha)
        bw, gap = self.bar_metrics()
        count = self.bar_count()
        bars = bars_for(self.values, self.rate, self.total, self.frontier, count)
        h = float(self.height())
        base = h - 1.0                 # bars stand on the bar below us
        min_h = 2.0 if modern else 1.0
        play_x = (self.position / self.total * self.width()) if self.total > 0 else -1
        band = float(scale.px(FEATHER_PX))
        p.setPen(Qt.NoPen)
        for i, v in enumerate(bars):
            if v is None:
                continue
            x = i * (bw + gap)
            bh = max(min_h, v * (h - 2))
            p.setBrush(blend(played, unplayed, feather(x + bw / 2.0, play_x, band)))
            r = QRectF(x, base - bh, bw, bh)
            if modern:
                p.drawRoundedRect(r, bw / 2.0, bw / 2.0)
            else:
                p.drawRect(r)

        def vline(x: float, color: QColor, width: float) -> None:
            pen = QPen(color, width)
            pen.setCapStyle(Qt.RoundCap if modern else Qt.FlatCap)
            p.setPen(pen)
            p.drawLine(QPointF(x, 1.0), QPointF(x, h - 1.0))

        if self.loading and self.total > 0 and self.frontier < self.total:
            vline(self.frontier / self.total * self.width(), fg, 1.5 if modern else 1.0)
        if self.hover_x is not None:
            x = max(0.0, min(float(self.width()), self.hover_x))
            vline(x, accent, 2.0 if modern else 1.0)
            if self.total > 0:
                text = _mmss(x / max(1, self.width()) * self.total)
                fm = QFontMetrics(self.font())
                tw = fm.horizontalAdvance(text) + 8
                tx = min(max(0.0, x + 4), self.width() - tw)
                tr = QRectF(tx, 0, tw, fm.height())
                bg = tok("bg", "#000")
                bg.setAlpha(200 if modern else 255)
                p.setPen(Qt.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(tr, 3, 3) if modern else p.drawRect(tr)
                p.setPen(accent)
                p.drawText(tr, Qt.AlignCenter, text)
        p.end()


class WaveformSeek(QObject):
    """The state machine around the overlay. The window feeds it the
    player state, position and duration; it pulls maps itself."""

    def __init__(self, map_provider, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._map_provider = map_provider        # () -> BeatMap | None
        self._mode = "on"
        self._progress: QWidget | None = None
        self.overlay: WaveformOverlay | None = None
        self._map = None
        self._playing = False
        self._loaded_shown = False               # the post-load reveal ran
        self._hovering = False
        self._pressed = False
        self._alpha = 0.0
        self._anim: QVariantAnimation | None = None
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._settle)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._pull_map)

    # ------------------------------------------------------------ wiring

    def attach(self, progress: QWidget) -> None:
        """Ride ``progress`` (re-call after the layout swaps the variant)."""
        if self._progress is not None:
            try:
                self._progress.removeEventFilter(self)
            except RuntimeError:
                pass
        if self.overlay is not None:
            try:
                self.overlay.deleteLater()
            except RuntimeError:
                pass
            self.overlay = None
        self._progress = progress
        if progress is None:
            return
        progress.setMouseTracking(True)
        progress.installEventFilter(self)
        host = progress.parentWidget() or progress
        self.overlay = WaveformOverlay(host)
        self._place()
        self._apply_alpha(self._alpha)

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in MODES else "on"
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "off":
            self._poll.stop()
            self._hold.stop()
            self._fade_to(0.0, snap=True)
        else:
            self._poll.start()
            self._settle()

    def mode(self) -> str:
        return self._mode

    def track_changed(self) -> None:
        self._map = None
        self._loaded_shown = False
        self._hold.stop()
        if self.overlay is not None:
            self.overlay.values, self.overlay.rate = (), 0.0
            self.overlay.frontier = 0.0
            self.overlay.position = 0.0
        if self._mode != "off":
            self._poll.start()
        self._settle()

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        self._maybe_reveal_loaded()

    def set_duration(self, seconds: float) -> None:
        if self.overlay is not None:
            self.overlay.total = max(0.0, float(seconds or 0.0))
            self.overlay.update()

    def set_position(self, seconds: float) -> None:
        ov = self.overlay
        if ov is None or self._alpha <= 0.0:
            if ov is not None:
                ov.position = float(seconds or 0.0)
            return
        ov.position = float(seconds or 0.0)
        ov.update()

    # ------------------------------------------------------------ state

    def state(self) -> str:
        """"seeking" | "loading" | "loaded" | "idle" (what's on screen)."""
        if self._mode == "off":
            return "idle"
        if self._hovering or self._pressed:
            return "seeking"
        if self._mode == "on" and self._map is not None \
                and not getattr(self._map, "complete", True):
            return "loading"
        if self._mode == "on" and self._hold.isActive():
            return "loaded"
        return "idle"

    def _pull_map(self) -> None:
        try:
            beat_map = self._map_provider() if self._map_provider else None
        except Exception:
            beat_map = None
        if beat_map is self._map:
            return
        self._map = beat_map
        ov = self.overlay
        if ov is not None:
            ov.values, ov.rate = envelope(beat_map)
            dur = float(getattr(beat_map, "duration", 0.0) or 0.0)
            ov.frontier = dur if beat_map is not None else 0.0
            if ov.total <= 0 and getattr(beat_map, "complete", False):
                ov.total = dur
            if getattr(beat_map, "complete", False) and ov.total > 0:
                ov.frontier = max(ov.frontier, ov.total)
            ov.update()
        self._maybe_reveal_loaded()
        self._settle()

    def _maybe_reveal_loaded(self) -> None:
        if (self._mode == "on" and not self._loaded_shown and self._playing
                and self._map is not None and getattr(self._map, "complete", False)):
            self._loaded_shown = True
            self._hold.start(LOADED_HOLD_MS)
            self._settle()

    def _settle(self) -> None:
        st = self.state()
        if self.overlay is not None:
            self.overlay.loading = st == "loading"
            if st != "seeking":
                self.overlay.hover_x = None
            self.overlay.update()
        if st == "idle":
            self._fade_to(0.0, out=True)
        else:
            self._fade_to(1.0)
        # keep polling while the decode can still move the frontier
        if self._mode != "off" and (self._map is None
                                    or not getattr(self._map, "complete", False)):
            if not self._poll.isActive():
                self._poll.start()
        elif self._poll.isActive():
            self._poll.stop()

    # ------------------------------------------------------------ mouse

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 (Qt)
        if obj is not self._progress:
            return False
        t = ev.type()
        if t in (QEvent.Resize, QEvent.Move, QEvent.Show):
            self._place()
        elif t == QEvent.Hide and self.overlay is not None:
            self.overlay.hide()
        if self._mode == "off":
            return False
        if t == QEvent.Enter:
            self._hovering = True
            self._settle()
        elif t in (QEvent.MouseMove, QEvent.HoverMove):
            self._hovering = True
            if self.overlay is not None:
                self.overlay.hover_x = ev.position().x() - self._span()[0]
            self._settle()
        elif t == QEvent.MouseButtonPress:
            self._pressed = True
            if self.overlay is not None:
                self.overlay.hover_x = ev.position().x() - self._span()[0]
            self._settle()
        elif t == QEvent.MouseButtonRelease:
            self._pressed = False
            self._settle()
        elif t == QEvent.Leave:
            self._hovering = False
            self._pressed = False
            self._settle()
        return False

    # ------------------------------------------------------------ look

    def _span(self) -> tuple[int, int]:
        """(x, width) of the seekable part of the bar, in its own coords.
        Most variants seek across their whole width; brutalist's text bar
        only spans its cells (``seek_span``)."""
        pr = self._progress
        if pr is None:
            return 0, 0
        span = getattr(pr, "seek_span", None)
        return span() if span is not None else (0, pr.width())

    def _place(self) -> None:
        pr, ov = self._progress, self.overlay
        if pr is None or ov is None:
            return
        try:
            g = pr.geometry()
            x0, w = self._span()
        except RuntimeError:
            return
        # Above the bar, standing on its top edge.
        h = scale.px(WAVE_PX)
        ov.setGeometry(g.x() + x0, g.top() - h - scale.px(1), w, h)
        if self._alpha > 0:
            ov.show()
            ov.raise_()

    def _fade_to(self, target: float, *, out: bool = False, snap: bool = False) -> None:
        from . import motion as motion_module
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if abs(target - self._alpha) < 1e-3:
            return
        if snap or motion_module.intensity() == motion_module.Intensity.OFF \
                or self.overlay is None:
            self._apply_alpha(target)
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(self._alpha)
        anim.setEndValue(target)
        anim.setDuration(motion_module.dur(FADE_OUT if out else FADE_IN))
        anim.setEasingCurve(motion_module.ease("out"))
        anim.valueChanged.connect(lambda v: self._apply_alpha(float(v)))
        anim.finished.connect(lambda t=target: self._apply_alpha(t))
        self._anim = anim
        anim.start()

    def _apply_alpha(self, a: float) -> None:
        self._alpha = max(0.0, min(1.0, a))
        ov, pr = self.overlay, self._progress
        if ov is not None:
            try:
                ov.alpha = self._alpha
                if self._alpha > 0:
                    self._place()
                    ov.show()
                    ov.raise_()
                else:
                    ov.hide()
                ov.update()
            except RuntimeError:
                pass

    def alpha(self) -> float:
        return self._alpha
