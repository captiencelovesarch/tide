"""the 2.3 visualizer: a live scene over the track's beat deck.

the old tab drew one 32-band spectrum ten ways and knew nothing about
the song. this one has two layers:

* the scene: the live capture (spectrum, mirror or scope; or one of the
  classic renderers), with peak caps, kick flashes and, in the glow
  look, bloom and shockwave rings.
* the deck: the beat map the ambient controller already holds for the
  backdrop pulse, scrolling under a playhead. the swell is the landscape,
  beats are ticks sliding in from the right, downbeats carry bar numbers,
  and a strip at the bottom is the whole song (click it to seek). you see
  the drop coming.

two looks, one per personality: ``blocks`` (brutalist: quantized cells,
hard edges, an inverse flash on the downbeat) and ``glow`` (modern:
soft bars, palette-tinted bloom, rings off every kick). ``auto`` follows
the theme's aesthetic, the same rule ``headings.Heading`` uses.

cost stays flat: graphics render into a buffer capped at
``RENDER_CAP`` px on the long side, the scene is a handful of numpy ops
plus one draw call per bar, the song strip is cached per map, and the
canvas stops its timer once paused and settled. text is drawn at native
resolution on top so it never goes soft.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
import logging
import math
import time

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import theming
from ..beat_map import CONFIDENCE_FLOOR, FLOOR_FROM_SWELL


_log = logging.getLogger(__name__)
_logged: set[str] = set()


def _log_once(where: str, exc: BaseException) -> None:
    """a paint or tick that throws is logged the first time only: at 60 fps
    a repeating error would flood the log."""
    key = f"{where}:{type(exc).__name__}:{exc}"
    if key not in _logged:
        _logged.add(key)
        _log.warning("visualizer %s failed: %r", where, exc, exc_info=exc)


LOOKS = ("blocks", "glow")
SCENES = ("spectrum", "mirror", "scope")
FLASH_GAIN = {"off": 0.0, "soft": 0.5, "hard": 1.0}
SPANS = (8, 16, 32)
BAR_COUNTS = (16, 32, 48, 64)
FPS_CHOICES = (30, 60)

RENDER_CAP = 1280            # buffer long side, px
# 60 fps backs off to 30 while a frame costs more than this (a 4k window,
# a slow machine) and comes back once it is cheap again
SLOW_PAINT_MS = 9.0
FAST_PAINT_MS = 5.0
PLAYHEAD_AT = 0.25           # playhead sits a quarter in: most of the deck is ahead
RING_S = 0.7                 # a shockwave ring lives this long (wall seconds)
FLASH_S = 0.14               # downbeat flash decay constant
FLASH_REACH_S = 0.45
HARD_HIT_MIN = 0.8           # a hit this strong flashes like a downbeat
LIVE_EDGE = 0.6              # live pulse rising through this spawns a ring
LIVE_EDGE_REARM = 0.35
SETTLED = 0.003


def resolve_look(setting: str, theme) -> str:
    if setting in LOOKS:
        return setting
    return "glow" if getattr(theme, "aesthetic", "") == "modern" else "blocks"


def format_time(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "–:––"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _mix(a: QColor, b: QColor, t: float, alpha: int | None = None) -> QColor:
    t = max(0.0, min(1.0, t))
    c = QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )
    c.setAlpha(a.alpha() if alpha is None else max(0, min(255, int(alpha))))
    return c


def _alpha(c: QColor, alpha: float) -> QColor:
    out = QColor(c)
    out.setAlpha(max(0, min(255, int(alpha))))
    return out


@dataclass(frozen=True)
class Palette:
    bg: QColor
    fg: QColor
    dim: QColor
    accent: QColor
    accent_alt: QColor

    @classmethod
    def from_theme(cls, theme) -> Palette:
        def tok(key: str, default: str) -> QColor:
            if theme is None:
                return QColor(default)
            c = QColor(theme.token(key, default))
            return c if c.isValid() else QColor(default)
        accent = tok("accent", "#d4b95e")
        raw_alt = theme.token("accent_alt", "") if theme is not None else ""
        alt = QColor(raw_alt) if raw_alt else QColor()
        if not alt.isValid():
            alt = accent.lighter(135)
        return cls(tok("bg", "#0b0b0b"), tok("fg", "#e6e6e6"), tok("dim", "#6f6f6f"),
                   accent, alt)


# ---------- the cover's colours ----------

def art_colors(img: QImage) -> tuple[QColor, QColor, QColor] | None:
    """(primary, secondary, shadow) from a cover: the strongest saturated
    hue, a second one at least a third of the wheel away (or a lighter
    primary), and the cover's darks. lifted so they read on a dark tile."""
    if img is None or img.isNull():
        return None
    small = img.convertToFormat(QImage.Format_RGB32).scaled(
        24, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    ptr = small.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8, count=small.sizeInBytes()).reshape(
        small.height(), small.bytesPerLine() // 4, 4)[:, : small.width(), :3][..., ::-1]
    rgb = arr.reshape(-1, 3).astype(float) / 255.0
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    hue = np.zeros(len(rgb))
    d = np.maximum(mx - mn, 1e-6)
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    hue = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) / 6.0
    weight = sat * mx
    bins = (hue * 12).astype(int) % 12
    totals = np.bincount(bins, weights=weight, minlength=12)

    def mean_of(mask) -> np.ndarray:
        w = weight[mask]
        return (rgb[mask] * w[:, None]).sum(axis=0) / max(w.sum(), 1e-6)

    def lifted(c: np.ndarray) -> QColor:
        q = QColor.fromRgbF(*np.clip(c, 0, 1))
        h, s_, v, _ = q.getHsvF()
        return QColor.fromHsvF(max(h, 0.0), min(1.0, max(s_, 0.35)), max(v, 0.75))

    shadow_mask = mx <= np.percentile(mx, 25)
    shadow = QColor.fromRgbF(*np.clip(rgb[shadow_mask].mean(axis=0), 0, 1))
    if totals.max() < 0.5:                       # a grey cover: no hue to take
        return None
    first = int(np.argmax(totals))
    primary = lifted(mean_of(bins == first))
    order = np.argsort(totals)[::-1]
    second = next((int(k) for k in order
                   if min(abs(k - first), 12 - abs(k - first)) >= 3
                   and totals[k] >= totals[first] * 0.15), None)
    secondary = lifted(mean_of(bins == second)) if second is not None else primary.lighter(140)
    return primary, secondary, shadow


# ---------- the live spectrum ----------

class Spectrum:
    """bands resampled to the bar count, eased toward each new frame (the
    feed's 43 fps would step visibly at 60), with falling peak caps."""

    ATTACK_S = 0.02
    RELEASE_S = 0.12
    HOLD_S = 0.35
    GRAVITY = 2.4            # peak fall acceleration, full heights / s²

    def __init__(self, n: int = 48) -> None:
        self.resize(n)

    def resize(self, n: int) -> None:
        self.n = max(4, int(n))
        self.level = np.zeros(self.n)
        self.peak = np.zeros(self.n)
        self._target = np.zeros(self.n)
        self._hold = np.zeros(self.n)
        self._vel = np.zeros(self.n)

    def push(self, bands: np.ndarray) -> None:
        bands = np.asarray(bands, dtype=float)
        if bands.ndim != 1 or bands.shape[0] < 2:
            return
        bands = np.nan_to_num(np.clip(bands, 0.0, 1.0))
        if bands.shape[0] == self.n:
            self._target = bands.copy()
        else:
            x = np.linspace(0.0, bands.shape[0] - 1.0, self.n)
            self._target = np.interp(x, np.arange(bands.shape[0]), bands)

    def silence(self) -> None:
        self._target = np.zeros(self.n)

    def step(self, dt: float, now: float) -> None:
        dt = max(0.0, min(0.1, dt))
        up = self._target > self.level
        k = np.where(up, 1.0 - math.exp(-dt / self.ATTACK_S),
                     1.0 - math.exp(-dt / self.RELEASE_S))
        self.level += (self._target - self.level) * k
        rising = self.level >= self.peak
        self.peak[rising] = self.level[rising]
        self._hold[rising] = now + self.HOLD_S
        self._vel[rising] = 0.0
        falling = ~rising & (now > self._hold)
        self._vel[falling] += self.GRAVITY * dt
        self.peak[falling] -= self._vel[falling] * dt
        np.maximum(self.peak, self.level, out=self.peak)

    def settled(self) -> bool:
        return (float(self._target.max(initial=0.0)) < SETTLED
                and float(self.peak.max(initial=0.0)) < SETTLED)


# ---------- the beat map, as the deck reads it ----------

@dataclass(frozen=True)
class BeatInfo:
    bpm: float | None
    phase: int | None        # 0..3 of the beat just played, 0 = the one
    bar: int | None          # 1-based
    trusted: bool


class MapView:
    """numpy columns over one BeatMap, built once per map."""

    def __init__(self, beat_map) -> None:
        self.map = beat_map
        beats = beat_map.beats
        self.times = np.array([b.time for b in beats], dtype=float)
        self.strengths = np.array([b.strength for b in beats], dtype=float)
        self.phases = np.array([b.phase for b in beats], dtype=int)
        self.periods = np.array([b.period for b in beats], dtype=float)
        self.pulse = np.array([b.pulse for b in beats], dtype=bool)
        self.bars = np.cumsum(self.phases == 0) if len(beats) else np.zeros(0, int)
        self.hit_times = np.array([h.time for h in beat_map.hits], dtype=float)
        self.hit_strengths = np.array([h.strength for h in beat_map.hits], dtype=float)
        self.swell = np.array(beat_map.swell, dtype=float)
        self.swell_rate = float(beat_map.swell_rate)
        # the full mix's level at 16 Hz (beat_map.loudness). a map cached
        # before it existed has none: the swell stands in, at its 4 Hz
        loud = tuple(getattr(beat_map, "loudness", ()) or ())
        if loud:
            self.loud = np.array(loud, dtype=float)
            self.loud_rate = float(getattr(beat_map, "loudness_rate", 16.0))
        else:
            self.loud = self.swell
            self.loud_rate = self.swell_rate
        self.trusted = bool(len(beats)) and beat_map.confidence >= CONFIDENCE_FLOOR
        # downbeats and hits strong enough to flash, merged and sorted
        accents = [(t, s) for t, s, ph, pu in zip(self.times, self.strengths,
                                                   self.phases, self.pulse)
                   if pu and ph == 0]
        accents += [(t, s) for t, s in zip(self.hit_times, self.hit_strengths)
                    if s >= HARD_HIT_MIN]
        accents.sort()
        self.accent_times = [t for t, _s in accents]
        self.accent_strengths = [s for _t, s in accents]

    @property
    def duration(self) -> float:
        return float(self.map.duration)

    def info(self, position: float) -> BeatInfo:
        if not self.trusted:
            return BeatInfo(None, None, None, False)
        i = int(np.searchsorted(self.times, position, side="right")) - 1
        if i < 0:
            j = 0
            return BeatInfo(60.0 / float(self.periods[j]), None, None, True)
        period = float(self.periods[i])
        bar = int(self.bars[i]) if self.bars[i] > 0 else 0
        return BeatInfo(60.0 / period if period > 0 else None,
                        int(self.phases[i]), max(1, bar), True)

    def kick(self, position: float, speed: float = 1.0) -> float:
        """the map's own shaped pulse with the swell floor taken out: how
        hard the beat is hitting right now, 0..1."""
        v = self.map.value_at(position, speed)
        if v is None:
            return 0.0
        floor = FLOOR_FROM_SWELL * self.map.swell_at(position)
        return max(0.0, min(1.0, (v - floor) / max(1e-6, 1.0 - floor)))

    def flash(self, position: float, speed: float = 1.0) -> float:
        """downbeats and big hits only: what may flash the whole scene."""
        i = bisect_right(self.accent_times, position) - 1
        if i < 0:
            return 0.0
        age = (position - self.accent_times[i]) / max(1e-6, speed)
        if age > FLASH_REACH_S:
            return 0.0
        return float(self.accent_strengths[i]) * math.exp(-age / FLASH_S)

    def rings(self, position: float, speed: float = 1.0) -> list[tuple[float, float]]:
        """(age in wall seconds, strength) of every pulse beat and hit
        inside a ring's lifetime. derived from the map, so no state."""
        lo = position - RING_S * speed
        out: list[tuple[float, float]] = []
        a = int(np.searchsorted(self.times, lo, side="left"))
        b = int(np.searchsorted(self.times, position, side="right"))
        for k in range(a, b):
            if self.pulse[k]:
                out.append(((position - self.times[k]) / speed, float(self.strengths[k])))
        a = int(np.searchsorted(self.hit_times, lo, side="left"))
        b = int(np.searchsorted(self.hit_times, position, side="right"))
        for k in range(a, b):
            out.append(((position - self.hit_times[k]) / speed, float(self.hit_strengths[k])))
        return out

    def loud_between(self, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
        """(times, values) of the loudness samples covering [lo, hi]."""
        if self.loud.size == 0:
            return np.zeros(0), np.zeros(0)
        r = self.loud_rate
        i0 = max(0, int(math.floor(lo * r)))
        i1 = min(self.loud.size - 1, int(math.ceil(hi * r)))
        if i1 < i0:
            return np.zeros(0), np.zeros(0)
        idx = np.arange(i0, i1 + 1)
        return idx / r, self.loud[idx]

    def beats_between(self, lo: float, hi: float) -> range:
        return range(int(np.searchsorted(self.times, lo, side="left")),
                     int(np.searchsorted(self.times, hi, side="right")))

    def hits_between(self, lo: float, hi: float) -> range:
        return range(int(np.searchsorted(self.hit_times, lo, side="left")),
                     int(np.searchsorted(self.hit_times, hi, side="right")))

    def swell_between(self, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
        """(times, values) of the swell samples covering [lo, hi]."""
        if self.swell.size == 0:
            return np.zeros(0), np.zeros(0)
        r = self.swell_rate
        i0 = max(0, int(math.floor(lo * r)))
        i1 = min(self.swell.size - 1, int(math.ceil(hi * r)))
        if i1 < i0:
            return np.zeros(0), np.zeros(0)
        idx = np.arange(i0, i1 + 1)
        return idx / r, self.swell[idx]


class LiveTrace:
    """the capture's pulse over the last stretch of wall time, for a deck
    with no beat map to draw (spotify without a cached map, live radio,
    the first seconds of an analysis). rising edges spawn rings.

    the feed pulses ~43 times a second; samples are kept as BUCKET-wide
    maxima so a frame walks a few hundred points, not thousands."""

    BUCKET = 0.125

    def __init__(self, seconds: float = 32.0) -> None:
        self.seconds = seconds
        # (bucket start, loudest level in it), oldest first
        self.samples: deque[tuple[float, float]] = deque()
        self.edges: deque[tuple[float, float]] = deque(maxlen=16)
        self._armed = True
        self.level = 0.0
        self.last_at = -math.inf

    def push(self, now: float, level: float) -> None:
        if not math.isfinite(level):
            return
        level = max(0.0, min(1.0, level))
        self.level = level
        self.last_at = now
        start = math.floor(now / self.BUCKET) * self.BUCKET
        if self.samples and self.samples[-1][0] == start:
            self.samples[-1] = (start, max(self.samples[-1][1], level))
        else:
            self.samples.append((start, level))
        while self.samples and now - self.samples[0][0] > self.seconds:
            self.samples.popleft()
        if self._armed and level >= LIVE_EDGE:
            self.edges.append((now, level))
            self._armed = False
        elif level < LIVE_EDGE_REARM:
            self._armed = True

    def decay(self, now: float) -> float:
        age = now - self.last_at
        if not math.isfinite(age):
            return 0.0
        return self.level * math.exp(-max(0.0, age) / 0.12)

    def rings(self, now: float) -> list[tuple[float, float]]:
        return [(now - t, s) for t, s in self.edges if 0 <= now - t < RING_S]

    def flash(self, now: float) -> float:
        if not self.edges:
            return 0.0
        t, s = self.edges[-1]
        age = now - t
        if 0 <= age <= FLASH_REACH_S and s >= HARD_HIT_MIN:
            return s * math.exp(-age / FLASH_S)
        return 0.0

    def clear(self) -> None:
        self.samples.clear()
        self.edges.clear()
        self._armed = True
        self.level = 0.0
        self.last_at = -math.inf


# ---------- what one frame draws from ----------

@dataclass
class Frame:
    look: str
    pal: Palette
    kick: float = 0.0        # 0..1, the beat hitting now
    flash: float = 0.0       # 0..1, downbeat/big hit flash (already gain-scaled)
    swell: float = 0.0
    rings: tuple = ()
    gain: float = 0.5
    # strobe trails: (alpha 0..1, bar levels at the kick, waveform at the kick)
    ghosts: tuple = ()
    # outline ripple: (age in wall seconds, strength) per recent kick
    ripples: tuple = ()


# ---------- scenes ----------

def _cells(h: float) -> int:
    return int(max(10, min(32, round(h / 14.0))))


def paint_spectrum(p: QPainter, w: float, h: float, spec: Spectrum, f: Frame,
                   mirror: bool = False) -> None:
    n = spec.n
    gap = max(1.0, w / n * (0.22 if f.look == "blocks" else 0.3))
    bw = max(1.0, (w - gap * (n - 1)) / n)
    xs = np.arange(n) * (bw + gap)
    if f.look == "blocks":
        _spectrum_blocks(p, w, h, xs, bw, spec, f, mirror)
    else:
        _spectrum_glow(p, w, h, xs, bw, spec, f, mirror)


def _spectrum_blocks(p, w, h, xs, bw, spec, f, mirror) -> None:
    pal = f.pal
    rows = _cells(h)
    if mirror:
        rows = max(6, rows // 2)
        half = h / 2.0
        cell = half / rows
        base = half
    else:
        cell = h / rows
        base = h
    lit = np.ceil(np.clip(spec.level, 0, 1) * rows - 0.15).clip(0, rows)
    peaks = np.floor(np.clip(spec.peak, 0, 1) * rows).clip(0, rows - 1)
    inv = f.flash
    body = _mix(pal.fg, pal.bg, inv) if inv > 0.02 else QColor(pal.fg)
    if f.kick > 0.02 and f.gain > 0:
        body = _mix(body, pal.accent, f.kick * f.gain * 0.6)
    p.setPen(Qt.NoPen)
    # the unlit grid, then the lit columns over it
    p.setBrush(_alpha(pal.dim, 46))
    for x in xs:
        p.drawRect(QRectF(x, 0, bw, h))
    # strobe trails: what the bars stood at on recent kicks, fading. drawn
    # before the cell cuts so the ghosts are cells too
    for alpha, levels, _wave in f.ghosts:
        if levels is None or len(levels) != len(xs):
            continue
        p.setBrush(_alpha(pal.accent, 150 * alpha))
        glit = np.ceil(np.clip(levels, 0, 1) * rows - 0.15).clip(0, rows)
        for x, k in zip(xs, glit):
            if k <= 0:
                continue
            hh = k * cell
            p.drawRect(QRectF(x, base - hh, bw, 2 * hh if mirror else hh))
    p.setBrush(body)
    for x, k in zip(xs, lit):
        if k <= 0:
            continue
        hh = k * cell
        if mirror:
            p.drawRect(QRectF(x, base - hh, bw, 2 * hh))
        else:
            p.drawRect(QRectF(x, base - hh, bw, hh))
    # cut the columns into cells
    stripe = max(1.0, cell * 0.24)
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    if mirror:
        for j in range(1, rows + 1):
            p.drawRect(QRectF(0, base - j * cell, w, stripe))
            p.drawRect(QRectF(0, base + j * cell - stripe, w, stripe))
        p.drawRect(QRectF(0, base - stripe / 2, w, stripe))
    else:
        for j in range(1, rows + 1):
            p.drawRect(QRectF(0, base - j * cell, w, stripe))
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    # peak caps: one accent cell, floating
    p.setBrush(pal.accent if inv < 0.5 else pal.bg)
    for x, k, pk in zip(xs, lit, peaks):
        if pk + 1 <= k or pk < 1:
            continue
        top = base - (pk + 1) * cell + stripe
        p.drawRect(QRectF(x, top, bw, cell - stripe))
        if mirror:
            p.drawRect(QRectF(x, base + pk * cell, bw, cell - stripe))
    if f.gain > 0 and f.kick > 0.05:
        # the baseline rule takes the kick
        p.setBrush(_alpha(pal.accent, 255 * f.kick * f.gain))
        p.drawRect(QRectF(0, base - 1, w, 2))
    if inv > 0.02:
        # the inverse flash goes in under the marks
        p.setCompositionMode(QPainter.CompositionMode_DestinationOver)
        p.setBrush(_alpha(pal.fg, 235 * inv))
        p.drawRect(QRectF(0, 0, w, h))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)


def _spectrum_glow(p, w, h, xs, bw, spec, f, mirror) -> None:
    pal = f.pal
    p.setRenderHint(QPainter.Antialiasing, True)
    base = h * (0.5 if mirror else 0.8)
    room = base - 6
    if f.gain > 0 or f.swell > 0:
        # bloom off the kick, from where the bars stand
        r = max(w, h) * (0.45 + 0.25 * f.kick)
        g = QRadialGradient(QPointF(w / 2, base), r)
        g.setColorAt(0.0, _alpha(pal.accent, 30 + 120 * f.kick * f.gain + 40 * f.flash))
        g.setColorAt(1.0, _alpha(pal.accent, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRect(QRectF(0, 0, w, h))
    levels = np.clip(spec.level, 0, 1)
    peaks = np.clip(spec.peak, 0, 1)
    rad0 = min(bw / 2.0, 6.0)
    p.setPen(Qt.NoPen)
    for alpha, glevels, _wave in f.ghosts:
        if glevels is None or len(glevels) != len(xs):
            continue
        p.setBrush(_alpha(pal.accent_alt, 110 * alpha))
        # plain rects: a ghost fades too fast for round tops to show, and
        # they're half the price
        for x, v in zip(xs, np.clip(glevels, 0, 1)):
            hh = max(rad0 * 2, v * room)
            if mirror:
                p.drawRect(QRectF(x, base - hh, bw, 2 * hh))
            else:
                p.drawRect(QRectF(x, base - hh, bw, hh))
    grad = QLinearGradient(QPointF(0, base), QPointF(0, base - room))
    grad.setColorAt(0.0, _alpha(pal.accent, 235))
    grad.setColorAt(1.0, _alpha(pal.accent_alt, 235))
    # one rounded rect per bar: a single many-shape path rasterizes ~10x
    # slower under antialiasing
    rad = min(bw / 2.0, 6.0)
    rects = []
    for x, v in zip(xs, levels):
        hh = max(rad * 2, v * room)
        if mirror:
            rects.append(QRectF(x, base - hh, bw, 2 * hh))
        else:
            rects.append(QRectF(x, base - hh, bw, hh + rad))
    p.setPen(Qt.NoPen)
    if not mirror:
        # a reflection on the floor
        p.save()
        p.setClipRect(QRectF(0, base, w, h - base))
        p.translate(0, 2 * base)
        p.scale(1, -1)
        refl = QLinearGradient(QPointF(0, base), QPointF(0, base - (h - base)))
        refl.setColorAt(0.0, _alpha(pal.accent, 70))
        refl.setColorAt(1.0, _alpha(pal.accent, 0))
        p.setBrush(refl)
        for r in rects:
            if r.top() < 2 * base - h:
                r = QRectF(r.left(), 2 * base - h, r.width(), r.bottom() - (2 * base - h))
            p.drawRoundedRect(r, rad, rad)
        p.restore()
        p.setClipRect(QRectF(0, 0, w, base))
    p.setBrush(grad)
    for r in rects:
        p.drawRoundedRect(r, rad, rad)
    p.setClipping(False)
    # peak caps: thin pills that float down
    cap_h = max(2.0, min(4.0, bw * 0.3))
    p.setBrush(_alpha(pal.fg, 210))
    for x, v, pk in zip(xs, levels, peaks):
        if pk - v < 0.02:
            continue
        y = base - pk * room - cap_h - 2
        p.drawRoundedRect(QRectF(x, y, bw, cap_h), cap_h / 2, cap_h / 2)
        if mirror:
            p.drawRoundedRect(QRectF(x, base + pk * room + 2, bw, cap_h), cap_h / 2, cap_h / 2)
    _rings(p, w, h, QPointF(w / 2, base), f)


def _rings(p: QPainter, w: float, h: float, center: QPointF, f: Frame) -> None:
    if not f.rings or f.gain <= 0:
        return
    reach = math.hypot(w / 2, h) * 0.9
    p.setBrush(Qt.NoBrush)
    for age, strength in f.rings:
        t = max(0.0, min(1.0, age / RING_S))
        ease = 1.0 - (1.0 - t) ** 3
        r = 12 + ease * reach * (0.55 + 0.45 * strength)
        a = (1.0 - t) ** 1.6 * 200 * strength * f.gain
        if a < 3:
            continue
        p.setPen(QPen(_alpha(f.pal.accent_alt, a), 1.5 + 3.0 * (1.0 - t) * strength))
        p.drawEllipse(center, r, r * 0.8)


def paint_scope(p: QPainter, w: float, h: float, wave: np.ndarray | None,
                f: Frame) -> None:
    pal = f.pal
    cy = h / 2.0
    if f.look == "blocks":
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(pal.dim, 90))
        p.drawRect(QRectF(0, cy - 0.5, w, 1))
    if wave is None or wave.shape[0] < 2:
        return
    if f.look == "blocks":
        rows = _cells(h)
        cell = h / rows
        cols = int(max(32, min(160, w / 8)))
        idx = (np.arange(cols) * (wave.shape[0] / cols)).astype(np.intp)
        ys = np.round(np.clip(wave[idx], -1, 1) * 0.9 * (rows / 2)) * cell
        cw = w / cols
        pts = []
        for i, y in enumerate(ys):
            pts.append(QPointF(i * cw, cy - y))
            pts.append(QPointF((i + 1) * cw, cy - y))
        color = _mix(pal.fg, pal.accent, f.kick * f.gain)
        for alpha, _levels, gwave in f.ghosts:
            if gwave is None or gwave.shape[0] < 2:
                continue
            gi = (np.arange(cols) * (gwave.shape[0] / cols)).astype(np.intp)
            gys = np.round(np.clip(gwave[gi], -1, 1) * 0.9 * (rows / 2)) * cell
            gpts = []
            for i, y in enumerate(gys):
                gpts.append(QPointF(i * cw, cy - y))
                gpts.append(QPointF((i + 1) * cw, cy - y))
            p.setRenderHint(QPainter.Antialiasing, False)
            p.setPen(QPen(_alpha(pal.accent, 170 * alpha), max(2.0, cell * 0.35)))
            p.drawPolyline(QPolygonF(gpts))
        if f.flash > 0.02:
            p.setPen(Qt.NoPen)
            p.setBrush(_alpha(pal.fg, 235 * f.flash))
            p.drawRect(QRectF(0, 0, w, h))
            color = _mix(color, pal.bg, f.flash)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(QPen(color, max(2.0, cell * 0.35), Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.drawPolyline(QPolygonF(pts))
        return
    p.setRenderHint(QPainter.Antialiasing, True)
    cols = int(max(64, min(240, w / 5)))
    for alpha, _levels, gwave in f.ghosts:
        if gwave is None or gwave.shape[0] < 2:
            continue
        gi = (np.arange(cols) * (gwave.shape[0] / cols)).astype(np.intp)
        gys = np.clip(gwave[gi], -1, 1) * h * 0.42
        gstep = w / (cols - 1)
        p.setPen(QPen(_alpha(pal.accent_alt, 150 * alpha), 1.6))
        p.drawPolyline(QPolygonF([QPointF(i * gstep, cy - float(y)) for i, y in enumerate(gys)]))
    # column means, not strided picks: the line reads smoother and the
    # antialiased stroke (the frame's priciest call) has fewer hairpins
    k = max(1, wave.shape[0] // cols)
    cols = max(2, wave.shape[0] // k)
    ys = (np.clip(wave[: cols * k].reshape(cols, k).mean(axis=1), -1, 1)
          * h * 0.42 * (1.0 + 0.25 * f.kick))
    step = w / (cols - 1)
    path = QPainterPath()
    path.moveTo(0, cy - float(ys[0]))
    for i in range(1, cols):
        path.lineTo(i * step, cy - float(ys[i]))
    # one stroke: the bloom pass is the halo (a second wide antialiased
    # stroke cost more than the rest of the frame together)
    width = 2.0 + 2.5 * f.kick * f.gain
    p.setPen(QPen(_mix(pal.accent, pal.fg, 0.35 * f.kick), width, Qt.SolidLine,
                  Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    _rings(p, w, h, QPointF(w / 2, cy), f)


def bloom(layer: QImage, strength: float) -> None:
    """a cheap glow: the layer shrunk (area-averaged) and laid back under
    itself, stretched. two scales, so it stays soft without a blur pass."""
    if strength <= 0 or layer.width() < 16 or layer.height() < 16:
        return
    small = layer.scaled(max(1, layer.width() // 8), max(1, layer.height() // 8),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    p = QPainter(layer)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setCompositionMode(QPainter.CompositionMode_DestinationOver)
    p.setOpacity(max(0.0, min(1.0, strength)))
    p.drawImage(QRectF(0, 0, layer.width(), layer.height()), small)
    p.end()


# ---------- the deck ----------

@dataclass
class DeckText:
    """labels the graphics pass wants drawn crisp, after the blit."""
    items: list

    def add(self, x: float, y: float, text: str, color: QColor, *,
            align: str = "left", size: float = 1.0, bold: bool = False) -> None:
        self.items.append((x, y, text, QColor(color), align, size, bold))


def _overview(mv: MapView, w: int, h: int, color: QColor, look: str) -> QImage:
    img = QImage(max(1, w), max(1, h), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    if mv.swell.size == 0 or mv.duration <= 0:
        return img
    p = QPainter(img)
    cols = max(1, w // (3 if look == "blocks" else 1))
    x = np.linspace(0, mv.swell.size - 1, cols)
    vals = np.interp(x, np.arange(mv.swell.size), mv.swell)
    # the strip spans the analyzed part; an incomplete map's tail stays empty
    cw = w / cols
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    for i, v in enumerate(vals):
        hh = max(1.0, (0.25 + 0.75 * float(v)) * h)
        if look == "blocks":
            hh = max(1.0, round(hh / 2.0) * 2.0)
            p.drawRect(QRectF(i * cw, h - hh, max(1.0, cw - 1), hh))
        else:
            p.drawRect(QRectF(i * cw, (h - hh) / 2, cw + 0.5, hh))
    p.end()
    return img


def paint_deck(p: QPainter, r: QRectF, f: Frame, mv: MapView | None,
               position: float | None, span: float, live: LiveTrace,
               now: float, text: DeckText, overview: tuple[QImage, QImage] | None,
               track_duration: float) -> QRectF | None:
    """draw the tape and the song strip into ``r``. returns the strip's
    rect (the seek target) when there is one."""
    pal = f.pal
    blocks = f.look == "blocks"
    strip_h = 6.0 if not blocks else 8.0
    tape = QRectF(r.left(), r.top(), r.width(), r.height() - strip_h - 8)
    strip = QRectF(r.left(), r.bottom() - strip_h, r.width(), strip_h)
    p.setPen(Qt.NoPen)
    if blocks:
        # brutalist frame: a hairline above and below the tape
        p.setBrush(_alpha(pal.dim, 140))
        p.drawRect(QRectF(tape.left(), tape.top(), tape.width(), 1))
        p.drawRect(QRectF(tape.left(), tape.bottom() - 1, tape.width(), 1))
    else:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(_alpha(pal.bg, 70))
        p.drawRoundedRect(tape, 10, 10)

    if mv is None or position is None:
        _deck_live(p, tape, f, live, now)
        return None

    lo = position - span * PLAYHEAD_AT
    hi = lo + span
    sx = tape.width() / span

    def X(t: float) -> float:
        return tape.left() + (t - lo) * sx

    inner_top = tape.top() + 6
    inner_h = tape.height() - 12
    base = inner_top + inner_h
    head_x = X(position)

    # everything on the tape stays on the tape
    p.save()
    p.setClipRect(tape)
    # swell: the song's loudness as a landscape
    st, sv = mv.swell_between(max(0.0, lo), min(mv.duration, hi))
    if st.size:
        if blocks:
            cw = sx / mv.swell_rate
            past = []
            future = []
            for t, v in zip(st, sv):
                q = max(1.0, round(float(v) * 8.0)) / 8.0
                hh = q * inner_h * 0.55
                rect = QRectF(X(t), base - hh, max(1.0, cw - 2), hh)
                (past if t < position else future).append(rect)
            p.setBrush(_alpha(pal.fg, 34))
            for rect in past:
                p.drawRect(rect)
            p.setBrush(_alpha(pal.dim, 56))
            for rect in future:
                p.drawRect(rect)
        else:
            path = QPainterPath()
            path.moveTo(X(float(st[0])), base)
            for t, v in zip(st, sv):
                path.lineTo(X(float(t)), base - float(v) * inner_h * 0.6)
            path.lineTo(X(float(st[-1])), base)
            path.closeSubpath()
            g = QLinearGradient(QPointF(0, base - inner_h * 0.6), QPointF(0, base))
            g.setColorAt(0.0, _alpha(pal.accent, 110))
            g.setColorAt(1.0, _alpha(pal.accent, 8))
            p.save()
            p.setClipRect(QRectF(head_x, tape.top(), tape.right() - head_x, tape.height()))
            g2 = QLinearGradient(g)
            g2.setColorAt(0.0, _alpha(pal.fg, 50))
            g2.setColorAt(1.0, _alpha(pal.fg, 4))
            p.setBrush(g2)
            p.drawPath(path)
            p.setClipRect(QRectF(tape.left(), tape.top(), head_x - tape.left(), tape.height()))
            p.setBrush(g)
            p.drawPath(path)
            p.restore()

    # not analyzed yet: the decode is still running ahead of us
    if not mv.map.complete and mv.duration < hi:
        x0 = max(tape.left(), X(mv.duration))
        if x0 < tape.right():
            p.save()
            p.setClipRect(QRectF(x0, tape.top(), tape.right() - x0, tape.height()))
            if blocks:
                p.setPen(QPen(_alpha(pal.dim, 90), 1))
                step = 8.0
                x = x0 - tape.height()
                while x < tape.right():
                    p.drawLine(QPointF(x, tape.bottom()), QPointF(x + tape.height(), tape.top()))
                    x += step
            else:
                g = QLinearGradient(QPointF(x0, 0), QPointF(x0 + 80, 0))
                g.setColorAt(0.0, _alpha(pal.fg, 26))
                g.setColorAt(1.0, _alpha(pal.fg, 0))
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawRect(QRectF(x0, tape.top(), tape.right() - x0, tape.height()))
            p.restore()
            if x0 + 8 < tape.right() - 40:
                text.add(x0 + 8, tape.top() + 14, theming.styled_case("analyzing…"),
                         pal.dim, size=0.85)

    # beats: ticks sliding in toward the playhead
    for k in mv.beats_between(lo, hi):
        t = float(mv.times[k])
        x = X(t)
        ph = int(mv.phases[k])
        s = float(mv.strengths[k])
        pulse = bool(mv.pulse[k])
        frac = 1.0 if ph == 0 else (0.68 if ph == 2 else 0.45)
        frac *= 0.55 + 0.45 * s
        if not mv.trusted:
            frac *= 0.6
        hh = inner_h * frac
        age = (position - t)
        near = max(0.0, 1.0 - abs(age) / 0.12)
        if blocks:
            wpx = 3.0 if ph == 0 else 2.0
            col = pal.fg if t >= position else _alpha(pal.dim, 200)
            if not pulse:
                col = _alpha(pal.dim, 140)
            if near > 0:
                col = _mix(col, pal.accent, near)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRect(QRectF(x - wpx / 2, base - hh, wpx, hh))
        else:
            a = (220 if t >= position else 110) * (1.0 if pulse else 0.45)
            col = _mix(_alpha(pal.accent_alt, a), pal.fg, near, alpha=a + 35 * near)
            p.setPen(QPen(col, 3.0 if ph == 0 else 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x, base - 2), QPointF(x, base - hh))
            if near > 0:
                p.setPen(Qt.NoPen)
                p.setBrush(_alpha(pal.accent_alt, 90 * near))
                p.drawEllipse(QPointF(x, base - hh), 7 * near + 2, 7 * near + 2)
        if (ph == 0 and mv.trusted and tape.left() + 4 < x < tape.right() - 24
                and not -22 < x - head_x < 4):
            bar = int(mv.bars[k])
            if bar % (4 if span >= 32 else 1) == 0:
                text.add(x + 4, tape.top() + 13, str(bar),
                         pal.fg if t >= position else pal.dim, size=0.8)

    # off-grid hits: little marks between the ticks
    p.setPen(Qt.NoPen)
    for k in mv.hits_between(lo, hi):
        t = float(mv.hit_times[k])
        x = X(t)
        s = float(mv.hit_strengths[k])
        y = base - inner_h * (0.25 + 0.35 * s)
        if blocks:
            p.setBrush(pal.accent if t >= position else _alpha(pal.dim, 200))
            p.drawRect(QRectF(x - 3, y - 3, 6, 6))
        else:
            p.setBrush(_alpha(pal.accent, 200 if t >= position else 90))
            p.drawEllipse(QPointF(x, y), 3.2, 3.2)

    # the playhead
    if blocks:
        if f.kick > 0.05 and f.gain > 0:
            p.setBrush(_alpha(pal.accent, 200 * f.kick))
            p.drawRect(QRectF(head_x - 6, tape.top() + 1, 12, tape.height() - 2))
        p.setBrush(pal.fg)
        p.drawRect(QRectF(head_x - 1, tape.top(), 2, tape.height()))
        tri = QPolygonF([QPointF(head_x - 5, tape.bottom()), QPointF(head_x + 5, tape.bottom()),
                         QPointF(head_x, tape.bottom() - 6)])
        p.drawPolygon(tri)
    else:
        g = QLinearGradient(QPointF(head_x - 14, 0), QPointF(head_x + 14, 0))
        glow = _alpha(pal.accent, 40 + 120 * f.kick * max(0.3, f.gain))
        g.setColorAt(0.0, _alpha(pal.accent, 0))
        g.setColorAt(0.5, glow)
        g.setColorAt(1.0, _alpha(pal.accent, 0))
        p.setBrush(g)
        p.drawRect(QRectF(head_x - 14, tape.top() + 2, 28, tape.height() - 4))
        p.setBrush(_alpha(pal.fg, 230))
        p.drawRoundedRect(QRectF(head_x - 1, tape.top() + 4, 2, tape.height() - 8), 1, 1)

    p.restore()
    p.setPen(Qt.NoPen)

    # the whole song, with the played part lit; seeking rides on it
    if overview is not None:
        dim_img, lit_img = overview
        dur = track_duration if track_duration > 0 else mv.duration
        if dur > 0:
            analyzed_w = strip.width() * min(1.0, mv.duration / dur)
            target = QRectF(strip.left(), strip.top(), analyzed_w, strip.height())
            p.drawImage(target, dim_img)
            played = max(0.0, min(1.0, position / dur)) * strip.width()
            if played > 0:
                p.save()
                p.setClipRect(QRectF(strip.left(), strip.top(), played, strip.height()))
                p.drawImage(target, lit_img)
                p.restore()
            p.setBrush(pal.fg)
            x = strip.left() + played
            if blocks:
                p.drawRect(QRectF(x - 1, strip.top() - 3, 2, strip.height() + 6))
            else:
                p.drawEllipse(QPointF(x, strip.center().y()), 4, 4)
            return strip
    return None


def _deck_live(p: QPainter, tape: QRectF, f: Frame, live: LiveTrace, now: float) -> None:
    """no beat map: the capture's pulse, a seismograph scrolling left."""
    pal = f.pal
    if not live.samples:
        return
    span = live.seconds
    inner_h = tape.height() - 12
    base = tape.bottom() - 6
    sx = tape.width() / span
    p.save()
    p.setClipRect(tape)
    _deck_live_marks(p, tape, f, live, now, sx, inner_h, base)
    p.restore()


def _deck_live_marks(p, tape, f, live, now, sx, inner_h, base) -> None:
    pal = f.pal
    if f.look == "blocks":
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(pal.fg, 150))
        # one column per bucket, its loudest moment, in eighths
        cw = live.BUCKET * sx
        gap = 2.0 if cw >= 6 else 1.0
        for t, v in live.samples:
            q = max(1.0, round(v * 8.0)) / 8.0
            hh = q * inner_h
            x = tape.right() - (now - t) * sx
            p.drawRect(QRectF(x, base - hh, max(1.0, cw - gap), hh))
    else:
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        first = True
        for t, v in live.samples:
            x = tape.right() - (now - t) * sx
            y = base - v * inner_h
            if first:
                path.moveTo(x, base)
                first = False
            path.lineTo(x, y)
        path.lineTo(tape.right(), base)
        path.closeSubpath()
        g = QLinearGradient(QPointF(0, base - inner_h), QPointF(0, base))
        g.setColorAt(0.0, _alpha(pal.accent, 150))
        g.setColorAt(1.0, _alpha(pal.accent, 10))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(path)


def paint_beat_counter(p: QPainter, x: float, cy: float, size: float,
                       info: BeatInfo, f: Frame) -> float:
    """four cells, the current one lit. returns the width used."""
    pal = f.pal
    gap = size * 0.6
    for i in range(4):
        on = info.phase == i
        r = QRectF(x + i * (size + gap), cy - size / 2, size, size)
        if f.look == "blocks":
            p.setPen(QPen(pal.fg if on else pal.dim, 1))
            p.setBrush(pal.accent if on and f.kick > 0.3 else (pal.fg if on else Qt.NoBrush))
            p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        else:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            if on:
                halo = size * 0.5 * (1.25 + 0.6 * f.kick)
                p.setBrush(_alpha(pal.accent, 70 + 90 * f.kick))
                p.drawEllipse(r.center(), halo, halo)
                p.setBrush(pal.fg)
            else:
                p.setBrush(_alpha(pal.fg, 60))
            p.drawEllipse(r.center(), size / 2, size / 2)
    return 4 * size + 3 * gap


# ---------- the engine: one clock, one frame, every tile ----------

RIPPLE_S = 0.9               # an outline ripple runs half the perimeter in this long
TRAIL_S = 0.5                # a strobe ghost fades over this long
MAX_GHOSTS = 3
RIPPLE_MIN = 0.45            # weaker kicks don't ripple the outlines


class VisEngine(QObject):
    """the visualizer's shared state and clock. the view pushes capture
    data and the beat-clock provider in; once per tick the engine eases
    the spectrum, reads the beat map, collects new kicks (for the strobe
    trails and the outline ripple) and builds one ``Frame``; then every
    tile repaints from it. one timer, so Qt folds all the tiles into a
    single repaint pass."""

    seek_requested = Signal(float)
    fullscreen_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.theme = theming.manager().current_effective()
        self.pal = Palette.from_theme(self.theme)
        self.settings = None
        self._look_setting = "auto"
        self.look = resolve_look("auto", self.theme)
        self.classic_name = "theme"
        self.classic = None
        self.flash_name = "soft"
        self.trails = True
        self.ripple = True
        self.span = 16.0
        self.fps = 60
        self.caption = True
        self.spec = Spectrum(48)
        self.bands: np.ndarray | None = None
        self.wave: np.ndarray | None = None
        self.live = LiveTrace()
        self._clock_provider = None
        self.mv: MapView | None = None
        self.position: float | None = None
        self.speed = 1.0
        self.info: BeatInfo | None = None
        self._last_pos: float | None = None
        self.player_pos: float | None = None
        self.duration = 0.0
        self.playing = False
        self.title = ""
        self.artist = ""
        self.track = None
        # the cover, fetched only once a tile asks for it (want_art)
        self.art: QImage | None = None
        self.art_cols: tuple[QColor, QColor, QColor] | None = None
        self.art_version = 0
        self._art_url: str | None = None
        self._art_asked = False
        self.frame = Frame(self.look, self.pal)
        self.now = time.monotonic()
        self._kicks: deque[tuple[float, float]] = deque(maxlen=12)
        self._ghosts: deque[tuple[float, np.ndarray, np.ndarray | None]] = deque(maxlen=MAX_GHOSTS)
        self._scan_pos: float | None = None
        self._scan_map = None
        self._edge_seen = -math.inf
        self.kicks_now: list[float] = []    # strengths of kicks that landed this tick
        self.tiles: list = []
        self._active = False
        self._last_t = time.monotonic()
        self._paint_ms = 0.0
        self._tick_ms = 0.0
        self._throttled = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(1000 // self.fps)
        self._timer.timeout.connect(self.tick)
        theming.manager().theme_changed.connect(self._on_theme)

    # ---------- configuration ----------

    def setting(self, name: str, default):
        s = self.settings
        return getattr(s, name, default) if s is not None else default

    def apply_settings(self, s) -> None:
        from .visualizer import get_renderer, renderer_for_theme
        self.settings = s
        classic = str(getattr(s, "visualizer_classic", "theme"))
        self.classic_name = classic
        self.classic = (renderer_for_theme(self.theme) if classic == "theme"
                        else get_renderer(classic) or renderer_for_theme(self.theme))
        self._look_setting = str(getattr(s, "visualizer_look", "auto"))
        self.look = resolve_look(self._look_setting, self.theme)
        flash = str(getattr(s, "visualizer_flash", "soft"))
        self.flash_name = flash if flash in FLASH_GAIN else "soft"
        self.trails = bool(getattr(s, "visualizer_trails", True))
        self.ripple = bool(getattr(s, "visualizer_ripple", True))
        span = getattr(s, "visualizer_deck_span", 16)
        self.span = float(span if span in SPANS else 16)
        bars = int(getattr(s, "visualizer_bars", 48))
        bars = bars if bars in BAR_COUNTS else 48
        if bars != self.spec.n:
            self.spec.resize(bars)
            self._ghosts.clear()
            if self.bands is not None:
                self.spec.push(self.bands)
        fps = int(getattr(s, "visualizer_fps", 60))
        self.fps = fps if fps in FPS_CHOICES else 60
        self._throttled = False
        self._timer.setInterval(1000 // self.fps)
        self.caption = bool(getattr(s, "visualizer_caption", True))
        for tile in self.tiles:
            tile.settings_changed()
        self.wake()

    def _on_theme(self, theme) -> None:
        self.theme = theme
        self.pal = Palette.from_theme(theme)
        self.look = resolve_look(self._look_setting, theme)
        if self.classic_name == "theme":
            from .visualizer import renderer_for_theme
            self.classic = renderer_for_theme(theme)
        for tile in self.tiles:
            tile.theme_changed()
            tile.update()

    def register(self, tile) -> None:
        if tile not in self.tiles:
            self.tiles.append(tile)

    def unregister(self, tile) -> None:
        if tile in self.tiles:
            self.tiles.remove(tile)

    def set_clock_provider(self, provider) -> None:
        self._clock_provider = provider

    # ---------- data in ----------

    def set_now_playing(self, track) -> None:
        self.track = track
        self.title = (getattr(track, "title", "") or "") if track is not None else ""
        self.artist = (getattr(track, "artists", "") or "") if track is not None else ""
        url = (getattr(track, "thumbnail", "") or "") if track is not None else ""
        if url != self._art_url:
            self._art_url = url or None
            self._art_asked = False
            self.art = None
            self.art_cols = None
            self.art_version += 1
        self._last_pos = None
        self.player_pos = None
        self._scan_pos = None
        self.live.clear()
        self._ghosts.clear()
        self._kicks.clear()
        for tile in self.tiles:
            tile.track_changed(track)
        self.wake()

    def want_art(self) -> None:
        """fetch the cover (once per track) for the tiles that draw it or
        take their colours from it."""
        if self._art_asked or not self._art_url:
            return
        self._art_asked = True
        url = self._art_url
        try:
            from . import art_cache
            img = art_cache.cache().request(url, lambda image, url=url: self.set_art(url, image))
        except Exception as exc:
            _log_once("art", exc)
            return
        if img is not None:
            self.set_art(url, img)

    def set_art(self, url: str | None, image: QImage | None) -> None:
        if url != self._art_url or image is None or image.isNull():
            return
        self.art = image
        try:
            self.art_cols = art_colors(image)
        except Exception as exc:
            _log_once("art colours", exc)
            self.art_cols = None
        self.art_version += 1
        for tile in self.tiles:
            tile.update()

    def colors(self) -> tuple[QColor, QColor, QColor]:
        """(primary, secondary, shadow) to paint music with: the cover's in
        the glow look, the theme's in blocks (brutalist stays its palette)."""
        if self.look == "glow" and self.art_cols is not None:
            return self.art_cols
        return self.pal.accent, self.pal.accent_alt, self.pal.accent.darker(260)

    def set_position(self, seconds: float) -> None:
        if math.isfinite(seconds):
            self.player_pos = float(seconds)
            for tile in self.tiles:
                tile.position_changed(self.player_pos)
            if not self.playing:
                self.wake()

    def set_duration(self, seconds: float) -> None:
        self.duration = float(seconds) if math.isfinite(seconds) and seconds > 0 else 0.0

    def set_playing(self, playing: bool) -> None:
        self.playing = bool(playing)
        if not self.playing:
            self.spec.silence()
        self.wake()

    def update_bands(self, bands: np.ndarray) -> None:
        bands = np.asarray(bands, dtype=float)
        if bands.ndim != 1 or bands.shape[0] < 2:
            return
        # the classic renderers turn levels into ints: a NaN would throw
        # in their paint, every frame
        self.bands = np.clip(np.nan_to_num(bands, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
        self.spec.push(self.bands)
        self.wake()

    def update_waveform(self, wave: np.ndarray) -> None:
        wave = np.asarray(wave)
        if wave.ndim != 1:
            return
        if wave.size and not np.isfinite(wave).all():
            wave = np.nan_to_num(wave, nan=0.0, posinf=0.0, neginf=0.0)
        self.wave = wave

    def update_pulse(self, level: float) -> None:
        self.live.push(time.monotonic(), level)

    # ---------- timing ----------

    def set_active(self, on: bool) -> None:
        """the view is on screen. off stops the clock outright."""
        self._active = bool(on)
        if self._active:
            self.wake()
        else:
            self._timer.stop()

    def wake(self) -> None:
        if self._active and not self._timer.isActive():
            self._last_t = time.monotonic()
            self._timer.start()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def effective_fps(self) -> int:
        return 30 if self._throttled else self.fps

    def account(self, ms: float) -> None:
        """tiles report what their paint cost; summed per tick."""
        self._tick_ms += ms

    def _settle_paint_cost(self) -> None:
        ms, self._tick_ms = self._tick_ms, 0.0
        if ms <= 0:
            return
        self._paint_ms += (ms - self._paint_ms) * 0.1
        if self.fps > 30:
            throttle = (self._paint_ms > SLOW_PAINT_MS if not self._throttled
                        else self._paint_ms > FAST_PAINT_MS)
            if throttle != self._throttled:
                self._throttled = throttle
                self._timer.setInterval(1000 // self.effective_fps())

    def _clock(self):
        if self._clock_provider is None:
            return None
        try:
            return self._clock_provider()
        except Exception:
            return None

    def tick(self) -> None:
        self._settle_paint_cost()
        now = time.monotonic()
        dt = now - self._last_t
        self._last_t = now
        self.now = now
        self.spec.step(dt, now)
        try:
            self.frame = self._build_frame(now)
        except Exception as exc:   # keep painting with the last frame
            _log_once("frame", exc)
        for tile in list(self.tiles):
            try:
                if tile.isVisible():
                    tile.on_frame(now, dt)
            except RuntimeError:
                self.unregister(tile)          # its C++ half is gone
            except Exception as exc:
                # one tile's bad frame must not starve the rest of theirs
                _log_once(f"{getattr(tile, 'kind', '?')} frame", exc)
        if (not self.playing and self.spec.settled() and not self.frame.ghosts
                and not self.frame.ripples and not self.frame.rings
                and not any(t.wants_frames() for t in self.tiles if t.isVisible())):
            self._timer.stop()

    def _new_kicks(self, now: float, mv: MapView | None, position: float | None,
                   speed: float) -> list[float]:
        """strengths of the kicks that landed since the last tick."""
        out: list[float] = []
        if mv is not None and position is not None and mv.map.covers(position):
            prev = self._scan_pos
            if (self._scan_map is not mv.map or prev is None
                    or position < prev or position - prev > 0.5 * max(speed, 0.25)):
                prev = position          # seek or a new map: no backlog
            self._scan_map = mv.map
            self._scan_pos = position
            if position > prev:
                a = int(np.searchsorted(mv.times, prev, side="right"))
                b = int(np.searchsorted(mv.times, position, side="right"))
                out += [float(mv.strengths[k]) for k in range(a, b) if mv.pulse[k]]
                a = int(np.searchsorted(mv.hit_times, prev, side="right"))
                b = int(np.searchsorted(mv.hit_times, position, side="right"))
                out += [float(mv.hit_strengths[k]) for k in range(a, b)]
        else:
            self._scan_pos = None
            for t, strength in self.live.edges:
                if t > self._edge_seen:
                    out.append(strength)
            if self.live.edges:
                self._edge_seen = max(self._edge_seen, self.live.edges[-1][0])
        return out

    def _build_frame(self, now: float) -> Frame:
        clock = self._clock()
        f = Frame(self.look, self.pal, gain=FLASH_GAIN[self.flash_name])
        position = None
        speed = 1.0
        if clock is not None:
            if self.mv is None or self.mv.map is not clock.beat_map:
                self.mv = MapView(clock.beat_map)
            speed = clock.speed if math.isfinite(clock.speed) and clock.speed > 0 else 1.0
            position = clock.position
            if position is not None and not math.isfinite(position):
                position = None
            if position is None:
                position = self.player_pos if self.player_pos is not None else self._last_pos
        else:
            self.mv = None
        if position is not None:
            self._last_pos = position
        mv = self.mv
        hard = 0.0
        if mv is not None and position is not None and mv.map.covers(position):
            if self.playing:
                f.kick = mv.kick(position, speed)
                hard = mv.flash(position, speed)
                f.rings = tuple(mv.rings(position, speed)) if self.look == "glow" else ()
            f.swell = mv.map.swell_at(position)
        elif self.playing:
            f.kick = self.live.decay(now)
            hard = self.live.flash(now)
            f.rings = tuple(self.live.rings(now)) if self.look == "glow" else ()
        f.flash = hard if self.flash_name == "hard" else 0.0
        self.kicks_now = self._new_kicks(now, mv, position, speed) if self.playing else []
        if self.playing:
            for strength in self.kicks_now:
                if strength >= RIPPLE_MIN:
                    self._kicks.append((now, strength))
                if self.trails:
                    wave = None
                    if self.wave is not None and self.wave.shape[0] >= 2:
                        idx = (np.arange(256) * (self.wave.shape[0] / 256)).astype(np.intp)
                        wave = np.array(self.wave[idx], dtype=float)
                    self._ghosts.append((now, self.spec.level.copy(), wave))
        if self.trails:
            f.ghosts = tuple(((1.0 - (now - t) / TRAIL_S) * 0.9, lv, wv)
                             for t, lv, wv in self._ghosts if 0 <= now - t < TRAIL_S)
        if self.ripple:
            f.ripples = tuple((now - t, s) for t, s in self._kicks if 0 <= now - t < RIPPLE_S)
        self.position = position
        self.speed = speed
        self.info = mv.info(position) if (mv is not None and position is not None) else None
        return f


# ---------- tiles ----------

def _perimeter(rect: QRectF) -> float:
    return 2.0 * (rect.width() + rect.height())


def _perim_point(rect: QRectF, s: float) -> QPointF:
    """the point ``s`` px along the rect's outline, clockwise from the top left."""
    w, h = rect.width(), rect.height()
    s %= _perimeter(rect)
    if s < w:
        return QPointF(rect.left() + s, rect.top())
    s -= w
    if s < h:
        return QPointF(rect.right(), rect.top() + s)
    s -= h
    if s < w:
        return QPointF(rect.right() - s, rect.bottom())
    s -= w
    return QPointF(rect.left(), rect.bottom() - s)


def _perim_param(rect: QRectF, pt: QPointF) -> float:
    """where on the outline a point (projected onto the nearest edge) sits."""
    w, h = rect.width(), rect.height()
    x = min(max(pt.x(), rect.left()), rect.right()) - rect.left()
    y = min(max(pt.y(), rect.top()), rect.bottom()) - rect.top()
    d = {"top": y, "right": w - x, "bottom": h - y, "left": x}
    edge = min(d, key=d.get)
    if edge == "top":
        return x
    if edge == "right":
        return w + y
    if edge == "bottom":
        return w + h + (w - x)
    return 2 * w + h + (h - y)


def _perim_polyline(rect: QRectF, a: float, b: float) -> QPolygonF:
    """the outline from ``a`` to ``b`` (a <= b, either may wrap), with the
    corners it passes as real vertices."""
    w, h = rect.width(), rect.height()
    L = _perimeter(rect)
    corners = (0.0, w, w + h, 2 * w + h)
    params = [a]
    k0 = math.floor(a / L) if L > 0 else 0
    for k in (k0, k0 + 1):
        for c in corners:
            cp = c + k * L
            if a < cp < b:
                params.append(cp)
    params.append(b)
    params.sort()
    return QPolygonF([_perim_point(rect, x) for x in params])


def paint_outline(p: QPainter, rect: QRectF, origin: QPointF, f: Frame) -> None:
    """the tile's frame, and a light running round it from ``origin`` on
    every kick: two comets that leave the corner nearest the middle of
    the grid and meet on the far side."""
    pal = f.pal
    glow = f.look == "glow"
    radius = 12.0 if glow else 0.0
    p.save()
    p.setBrush(Qt.NoBrush)
    if glow:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(_alpha(pal.fg, 30), 1.0))
        p.drawRoundedRect(rect, radius, radius)
    else:
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(QPen(_alpha(pal.dim, 120), 1.0))
        p.drawRect(rect)
    if f.ripples:
        L = _perimeter(rect)
        s0 = _perim_param(rect, origin)
        tail = 0.22 * L
        chunks = 4
        for age, strength in f.ripples[-2:]:
            t = max(0.0, min(1.0, age / RIPPLE_S))
            d = (1.0 - (1.0 - t) ** 2) * L / 2.0
            fade = (1.0 - t) ** 1.3 * strength
            if fade < 0.02:
                continue
            seg = min(tail, d) / chunks
            for sign in (1.0, -1.0):
                head = s0 + sign * d
                for i in range(chunks):
                    # chunk i runs from the head back toward the origin
                    a0 = head - sign * seg * i
                    a1 = head - sign * seg * (i + 1)
                    pts = _perim_polyline(rect, min(a0, a1), max(a0, a1))
                    k = 1.0 - i / chunks
                    alpha = 255 * fade * k
                    if glow:
                        p.setPen(QPen(_alpha(pal.accent_alt, alpha * 0.35), 7.0 * k + 1,
                                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                        p.drawPolyline(pts)
                        p.setPen(QPen(_mix(pal.accent_alt, pal.fg, 0.4 * k, alpha=alpha),
                                      2.0 * k + 0.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    else:
                        p.setPen(QPen(_alpha(pal.fg if i == 0 else pal.accent, alpha),
                                      3.0 if i < 2 else 2.0, Qt.SolidLine, Qt.SquareCap,
                                      Qt.MiterJoin))
                    p.drawPolyline(pts)
    p.restore()


class TileMixin:
    """what the grid and the engine ask of every tile."""

    kind = ""
    KNOBS: tuple[str, ...] = ()     # settings keys this tile reads (its menu)
    solo = False                    # alone in the grid: ripple from the bottom
    caption_here = False

    def ripple_origin(self) -> QPointF:
        r = self.rect()
        if self.solo or self.parentWidget() is None:
            return QPointF(r.center().x(), r.bottom())
        c = self.parentWidget().rect().center()
        return QPointF(self.mapFrom(self.parentWidget(), c))

    def outline_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

    # engine hooks (no-ops by default)
    def on_frame(self, now: float, dt: float) -> None:
        self.update()

    def wants_frames(self) -> bool:
        return False

    def settings_changed(self) -> None:
        pass

    def theme_changed(self) -> None:
        pass

    def track_changed(self, track) -> None:
        pass

    def position_changed(self, seconds: float) -> None:
        pass

    def release(self) -> None:
        """leaving the grid."""
        self.engine.unregister(self)


class CanvasTile(TileMixin, QWidget):
    """a tile that paints. graphics render into a buffer capped at
    RENDER_CAP; the outline and any text go on top at native res."""

    PAD = 14.0

    def __init__(self, engine: VisEngine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._buf: QImage | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setMinimumSize(120, 90)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        engine.register(self)

    def paint_graphics(self, p: QPainter, w: float, h: float, f: Frame) -> None:
        pass

    def buffer_scale(self, f: Frame) -> float:
        """render the graphics at this fraction of the tile's resolution.
        soft, bloomed tiles lose nothing at 0.6 and cost a third."""
        return 1.0

    def paint_overlay(self, p: QPainter, w: float, h: float, f: Frame) -> None:
        pass

    def buffer(self) -> QImage | None:
        return self._buf

    def paintEvent(self, _event) -> None:
        started = time.perf_counter()
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        f = self.engine.frame
        dpr = self.devicePixelRatioF()
        s = min(dpr, RENDER_CAP / max(w, h)) * self.buffer_scale(f)
        bw, bh = max(1, int(round(w * s))), max(1, int(round(h * s)))
        if self._buf is None or self._buf.width() != bw or self._buf.height() != bh:
            self._buf = QImage(bw, bh, QImage.Format_ARGB32_Premultiplied)
        self._buf.fill(Qt.transparent)
        # a throw mid-paint must still end its painter: one left open on
        # the buffer makes every later frame fail to begin, and the tile
        # froze for good
        bp = QPainter(self._buf)
        try:
            bp.scale(bw / w, bh / h)
            self.paint_graphics(bp, float(w), float(h), f)
        except Exception as exc:
            _log_once(f"{self.kind} graphics", exc)
        finally:
            bp.end()
        try:
            self.post_graphics(self._buf, f)
        except Exception as exc:
            _log_once(f"{self.kind} post", exc)
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(QRectF(0, 0, w, h), self._buf)
            paint_outline(p, self.outline_rect(), self.ripple_origin(), f)
            p.setRenderHint(QPainter.TextAntialiasing, True)
            p.setRenderHint(QPainter.Antialiasing, True)
            self.paint_overlay(p, float(w), float(h), f)
        except Exception as exc:
            _log_once(f"{self.kind} overlay", exc)
        finally:
            p.end()
        self.engine.account((time.perf_counter() - started) * 1000.0)

    def post_graphics(self, buf: QImage, f: Frame) -> None:
        pass

    def font_at(self, scale: float = 1.0, bold: bool = False) -> QFont:
        font = QFont(self.font())
        size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        font.setPointSizeF(max(6.0, size * scale))
        font.setBold(bold)
        return font

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.engine.fullscreen_requested.emit()
            return
        super().mouseDoubleClickEvent(event)


class SceneTile(CanvasTile):
    """the live capture: spectrum, mirror or scope (``kind``), or the
    classic renderer picked in settings ("classic")."""

    kind = "spectrum"

    def __init__(self, engine, kind: str = "spectrum", parent=None) -> None:
        super().__init__(engine, parent)
        self.kind = kind if kind in SCENES or kind == "classic" else "spectrum"

    def paint_graphics(self, p, w, h, f) -> None:
        e = self.engine
        if self.kind == "classic":
            if e.classic is not None:
                p.setRenderHint(QPainter.TextAntialiasing, True)
                e.classic.paint(p, QRect(0, 0, int(w), int(h)), e.theme, e.bands, e.wave)
            return
        top = 54.0 if self.caption_here and e.caption and (e.title or e.artist) else self.PAD
        p.translate(self.PAD, top)
        iw, ih = max(1.0, w - 2 * self.PAD), max(1.0, h - top - self.PAD)
        if self.kind == "scope":
            paint_scope(p, iw, ih, e.wave if e.playing else None, f)
        else:
            paint_spectrum(p, iw, ih, e.spec, f, mirror=self.kind == "mirror")

    def post_graphics(self, buf, f) -> None:
        if f.look == "glow" and self.kind != "classic":
            bloom(buf, 0.55 + 0.35 * f.kick * f.gain)

    def paint_overlay(self, p, w, h, f) -> None:
        e = self.engine
        if not (self.caption_here and e.caption and (e.title or e.artist)):
            return
        modern = f.look == "glow"
        tf = self.font_at(1.7 if modern else 1.25, bold=modern)
        p.setFont(tf)
        fm = QFontMetricsF(tf)
        p.setPen(f.pal.fg)
        avail = max(10.0, w - 2 * self.PAD)
        p.drawText(QPointF(self.PAD, 10 + fm.ascent()),
                   fm.elidedText(e.title, Qt.ElideRight, avail))
        af = self.font_at(1.0)
        p.setFont(af)
        afm = QFontMetricsF(af)
        p.setPen(_alpha(f.pal.fg, 165) if modern else f.pal.dim)
        p.drawText(QPointF(self.PAD, 12 + fm.height() + afm.ascent()),
                   afm.elidedText(e.artist, Qt.ElideRight, avail))


class DeckTile(CanvasTile):
    """the beat map under a playhead, an info row, the song strip."""

    kind = "deck"
    INFO_H = 30.0

    def __init__(self, engine, parent=None) -> None:
        super().__init__(engine, parent)
        self._text = DeckText([])
        self._overview_key = None
        self._overview: tuple[QImage, QImage] | None = None
        self.strip_rect: QRectF | None = None

    def _deck_rect(self, w: float, h: float) -> QRectF:
        return QRectF(self.PAD, self.INFO_H + 6, max(1.0, w - 2 * self.PAD),
                      max(1.0, h - self.INFO_H - 6 - self.PAD + 4))

    def _overview_images(self, mv: MapView, w: int, h: int) -> tuple[QImage, QImage]:
        pal, look = self.engine.pal, self.engine.look
        key = (id(mv.map), w, h, look, pal.fg.rgba(), pal.accent.rgba())
        if self._overview_key != key or self._overview is None:
            dim = _alpha(pal.dim, 150)
            lit = pal.accent if look == "glow" else pal.fg
            self._overview = (_overview(mv, w, h, dim, look), _overview(mv, w, h, lit, look))
            self._overview_key = key
        return self._overview

    def paint_graphics(self, p, w, h, f) -> None:
        e = self.engine
        self._text = DeckText([])
        dr = self._deck_rect(w, h)
        overview = None
        if e.mv is not None and e.position is not None:
            s = (self._buf.width() / w) if self._buf is not None else 1.0
            overview = self._overview_images(
                e.mv, max(1, int(dr.width() * s)),
                max(1, int((8 if f.look == "blocks" else 6) * s)))
        self.strip_rect = paint_deck(p, dr, f, e.mv, e.position, e.span, e.live,
                                     e.now, self._text, overview, e.duration)

    def paint_overlay(self, p, w, h, f) -> None:
        e = self.engine
        pal = f.pal
        for x, y, s, color, align, size, bold in self._text.items:
            font = self.font_at(size, bold)
            p.setFont(font)
            p.setPen(color)
            if align == "right":
                x -= QFontMetricsF(font).horizontalAdvance(s)
            p.drawText(QPointF(x, y), s)
        font = self.font_at(0.95)
        p.setFont(font)
        fm = QFontMetricsF(font)
        cy = self.INFO_H / 2 + 4
        y = cy + (fm.ascent() - fm.descent()) / 2
        info = e.info
        mv = e.mv
        p.setPen(pal.fg)
        if info is not None and info.trusted and info.bpm:
            label = theming.styled_case(f"{info.bpm:.0f} bpm")
            p.drawText(QPointF(self.PAD, y), label)
            x = self.PAD + fm.horizontalAdvance(label) + 14
            cell = max(6.0, fm.ascent() * 0.7)
            x += paint_beat_counter(p, x, cy, cell, info, f) + 14
            if info.bar is not None:
                p.setPen(pal.dim)
                p.drawText(QPointF(x, y), theming.styled_case(f"bar {info.bar}"))
        elif mv is not None and e.position is not None:
            p.setPen(pal.dim)
            p.drawText(QPointF(self.PAD, y), theming.styled_case("free time · hits only"))
        else:
            p.setPen(pal.dim)
            label = "live" if e.playing else ("paused" if e.title else "nothing playing")
            p.drawText(QPointF(self.PAD, y), theming.styled_case(label))
        pos = e.position if e.position is not None else e.player_pos
        dur = e.duration or (mv.duration if mv is not None and mv.map.complete else 0.0)
        if pos is not None:
            right = f"{format_time(pos)} / {format_time(dur)}"
            p.setPen(pal.dim)
            p.drawText(QPointF(w - self.PAD - fm.horizontalAdvance(right), y), right)

    def mousePressEvent(self, event) -> None:
        r = self.strip_rect
        pos = event.position()
        if (event.button() == Qt.LeftButton and r is not None
                and r.adjusted(0, -8, 0, 8).contains(pos) and r.width() > 0):
            e = self.engine
            dur = e.duration or (e.mv.duration if e.mv else 0.0)
            if dur > 0:
                frac = max(0.0, min(1.0, (pos.x() - r.left()) / r.width()))
                e.seek_requested.emit(frac * dur)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        r = self.strip_rect
        over = r is not None and r.adjusted(0, -8, 0, 8).contains(event.position())
        self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
        super().mouseMoveEvent(event)
