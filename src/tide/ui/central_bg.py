"""Adaptive app backdrop.

A wrapper widget that sits behind Tide's main app surface. By itself it
paints whatever the theme says ``bg`` is; when
``adaptive_background`` is on, it paints layered album-tinted fields over
that base color. The adaptive driver supplies ``ambient_bg`` / ``accent_alt``
via the theming manager's runtime overrides, so the background shifts with
album art automatically without the wrapper needing to know anything about
palette extraction.

Corners obey ``corner_style`` (sharp / soft / rounded). The radius is
applied to both the gradient draw and the clipping mask, so the gradient
stops *inside* the rounded shape — the window's bg shows through the
corners cleanly.

Child widgets keep their own QSS-defined backgrounds. Structural containers
are made transparent by theming._CONTENT_BACKDROP_QSS so the app has one
coherent backdrop, while real controls keep their own surfaces.
"""
from __future__ import annotations

import colorsys
import math
import time

import random

import numpy as np

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QHBoxLayout, QWidget

from .. import backdrops, contrast, theming


# Maps the corner_style setting to a pixel radius. Kept here so the dialog
# and the painter share one source of truth.
CORNER_RADII: dict[str, int] = {
    "sharp": 0,
    "soft": 6,
    "rounded": 12,
}


def corner_radius(style: str) -> int:
    return CORNER_RADII.get(style or "sharp", 0)


# Animation tuning. The drift oscillators use mutually-prime-ish periods so
# the composite motion never obviously loops.
# How long the backdrop takes to cross from one album's palette to the
# next. Long enough to read as a scene change, short enough that the new
# song owns the room before its first chorus.
_TONE_FADE_MS = 1400.0

_ANIM_INTERVAL_MS = 42          # idle drift stays inexpensive
# The two gradient looks drift over 30-50 s periods through soft fields, so
# a quarter of a pixel per frame at 24 fps; 15 fps reads the same. Every
# frame repaints the whole window on top of the backdrop, so this is the
# idle cost of the app.
_GRADIENT_IDLE_MS = 66
_GRADIENT_STYLES = frozenset({"band", "field"})
_PULSE_INTERVAL_MS = 16         # transients need a display-rate cadence
_PERIOD_FLOW_S = 43.0
_PERIOD_FIELD_A_S = 29.0
_PERIOD_FIELD_B_S = 37.0
_PERIOD_FIELD_C_S = 53.0
_BASE_ANGLE = math.radians(56)  # diagonal, top-left → bottom-right
# the source already shapes the attack. follow it immediately, easing only
# the release in wall time so timer cadence cannot smear adjacent hits.
_PULSE_RELEASE_S = 0.030
# Offscreen buffer cap (long side, px). The gradient is smooth so a small
# buffer upscaled bilinearly is visually identical to a full-res fill, but
# caps the fill cost regardless of window size / desktop scaling.
_BUF_CAP = 384

# Liquid-cover tuning. The style samples the actual album art: stretched to
# the window's aspect, blurred down to its primary color masses, then
# domain-warped per frame so the colors slide around each other like paint.
# The warp math runs in numpy at this cap and the result rides the same
# smooth upscale as every other style, so window size never matters.
_LIQ_CAP = 220
# Cover → this many cells per side before upsampling. This IS the blur: at
# 10×10 nothing survives but the art's main color fields.
_LIQ_TINY = 10
# Spread the blurred colors apart a little — heavy averaging mutes them, and
# the whole point of the style is that you can still tell whose colors these
# are.
_LIQ_CHROMA = 1.35
# Where the field's average brightness should sit, as luminance 0..1. Kept in
# the same band as the other styles' tones so content stays readable on top.
_LIQ_MEAN_L_DARK = 0.20
_LIQ_MEAN_L_LIGHT = 0.74

# Procedural-field tuning. The scene styles (everything except the two
# original gradient looks and liquid) render the liquid way: a small numpy
# grid at the _LIQ_CAP long side, vectorized per-frame math, bilinear
# upscale in paintEvent. Their shared ingredient is a seeded stack of value-
# noise lattices; _fbm() blends octave pairs over time so the field *churns*
# instead of merely scrolling — that churn is what makes the scenes read as
# haze / water / terrain rather than stacked gradient primitives.
_FX_LATTICE = 48                 # lattice cells per side, wrap-sampled
_FX_LATTICE_COUNT = 10           # planes in the stack; styles pick pairs
_FX_SEED = 20260827              # fixed so a style always looks like itself

# The styles that go through the procedural-field renderer.
_FX_STYLES = frozenset({
    "vbeam", "horizon", "lightning", "ripples", "stage", "rimlight",
    "contours", "vinyl",
})

# Ripples: how many rings can be on the water at once, and how long one
# lasts before it has faded to nothing.
_DROP_MAX = 8
_DROP_LIFE_S = 3.4
_DROP_TILT = 0.58

# Text legibility (ui/legibility.py) reads the backdrop through a small
# luminance grid: the last frame scaled to _INK_GRID on its long side, as
# APCA screen luminance per cell. It eases toward each new frame over
# _INK_EASE_S so text follows the drift of the scene but not the bass:
# a kick that brightens the field for 100 ms must not strobe the text.
_INK_GRID = 160
_INK_EASE_S = 0.5
# The grid is rebuilt at most this often. It eases over half a second
# anyway, and between rebuilds every text run on screen can reuse its last
# answer (legibility memoises on ink_version) instead of probing again.
_INK_REBUILD_S = 0.1
_INK_LUT = tuple(np.array(t, dtype=np.float32) for t in contrast.APCA_LUT)

# set_style's whitelist, from the backdrop registry so pickers and the
# renderer share one list. The assert makes a slug added to backdrops.py
# without a paint branch here fail at import, not render as "field".
_STYLES = frozenset(backdrops.SLUGS)
assert _STYLES == _FX_STYLES | {"field", "band", "liquid"}, (
    "backdrop registry drifted from central_bg's renderer styles"
)

_fx_lattice_stack: np.ndarray | None = None


def _fx_lattices() -> np.ndarray:
    """The shared value-noise stack, built once per process. float32
    (count, L, L) in 0..1; every CentralBg instance samples the same
    planes, so the mini player's backdrop churns in sync with the main
    window's for free."""
    global _fx_lattice_stack
    if _fx_lattice_stack is None:
        rng = np.random.default_rng(_FX_SEED)
        _fx_lattice_stack = rng.random(
            (_FX_LATTICE_COUNT, _FX_LATTICE, _FX_LATTICE)).astype(np.float32)
    return _fx_lattice_stack


def _contour_segments(level: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Marching squares over ``level`` at every integer: (N, 4) float32
    segments as x0, y0, x1, y1 in grid pixels, and an (N,) bool that
    marks the index lines (every fifth level). One vectorised pass per
    level; the field is smooth, so saddle cells are rare enough to pair
    by edge order."""
    v00 = level[:-1, :-1]
    v10 = level[:-1, 1:]
    v11 = level[1:, 1:]
    v01 = level[1:, :-1]
    lo = np.minimum(np.minimum(v00, v10), np.minimum(v01, v11))
    hi = np.maximum(np.maximum(v00, v10), np.maximum(v01, v11))
    out: list[np.ndarray] = []
    flags: list[np.ndarray] = []
    for k in range(int(math.floor(float(lo.min()))) + 1,
                   int(math.floor(float(hi.max()))) + 1):
        jj, ii = np.nonzero((lo < k) & (hi >= k))
        if jj.size == 0:
            continue
        a = v00[jj, ii]
        b = v10[jj, ii]
        c = v11[jj, ii]
        d = v01[jj, ii]
        fi = ii.astype(np.float32)
        fj = jj.astype(np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            # top, right, bottom, left edge crossings
            pts = np.stack([
                np.stack([fi + (k - a) / (b - a), fj], 1),
                np.stack([fi + 1.0, fj + (k - b) / (c - b)], 1),
                np.stack([fi + (k - d) / (c - d), fj + 1.0], 1),
                np.stack([fi, fj + (k - a) / (d - a)], 1),
            ], 1)
        crossed = np.stack([(a < k) != (b < k), (b < k) != (c < k),
                            (d < k) != (c < k), (a < k) != (d < k)], 1)
        order = np.argsort(~crossed, axis=1, kind="stable")
        rows = np.arange(jj.size)
        first = pts[rows, order[:, 0]]
        second = pts[rows, order[:, 1]]
        segs = [np.concatenate([first, second], 1)]
        saddle = crossed.all(axis=1)
        if saddle.any():
            segs.append(np.concatenate([pts[saddle, 2], pts[saddle, 3]], 1))
        seg = np.concatenate(segs, 0)
        out.append(seg)
        flags.append(np.full(seg.shape[0], k % 5 == 0))
    if not out:
        return np.zeros((0, 4), np.float32), np.zeros(0, bool)
    return (np.nan_to_num(np.concatenate(out, 0)).astype(np.float32),
            np.concatenate(flags, 0))


# Vinyl grooves per unit of window height, and how fast the label (the
# album cover) turns at full motion: a turn every 9 s.
_GROOVES_PER_UNIT = 80.0
_LABEL_DEG_PER_S = 40.0


def _lerp(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
    )


def _alpha(c: QColor, a: int) -> QColor:
    out = QColor(c)
    out.setAlpha(max(0, min(255, int(a))))
    return out


def _bg_tone(c: QColor, l: float, s: float) -> QColor:
    """Take the *hue* of ``c`` and place it at a fixed lightness/saturation.
    Used to turn a vivid album accent into a background tone that's dark
    enough to keep content legible but light enough to actually be seen
    against the theme bg (the previous 'deepen' approach was so dark it was
    invisible)."""
    h, _, base_s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    target_s = max(0.0, min(1.0, s if base_s >= 0.035 else 0.0))
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), target_s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def _hls_saturation(c: QColor) -> float:
    return colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())[2]


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    """Plain RGB lerp. The tones being blended are all dark, low-chroma
    neighbors, so RGB is fine — no hue-wheel shortcuts needed."""
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


class CentralBg(QWidget):
    """Wraps the main app surface. When enabled, paints a slowly morphing
    album-palette field that also swells on bass.

    The colors come from the theme tokens ``bg`` / ``ambient_bg`` /
    ``accent_alt`` — the adaptive driver overrides ambient_bg + accent_alt
    from album art and the theming manager re-emits ``theme_changed``, so
    this widget tracks album color with no extra wiring. The bass pulse is
    fed in via ``set_pulse`` from the ambient controller and is a *local*
    paint effect — it never touches the theme/QSS, so a per-frame pulse costs
    one small buffer fill + a scaled blit, not an app-wide restyle.
    """

    def __init__(self, child: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # WA_StyledBackground=False so QSS doesn't override our paintEvent
        # (the brutalist theme sets `QWidget { background: @bg }` globally).
        self.setAttribute(Qt.WA_StyledBackground, False)
        # We DO want a backing buffer so children compose against our paint
        # rather than the window's bg, which prevents flicker on resize.
        self.setAutoFillBackground(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(child)

        self._enabled: bool = False
        self._radius: int = 0
        # One of backdrops.SLUGS.
        self._style: str = "field"
        self._motion: str = "lite"          # "off" freezes the drift
        self._bg = QColor("#0b0b0b")
        self._tone_a = QColor("#141414")
        self._tone_b = QColor("#141414")
        self._tone_c = QColor("#141414")
        # Tone crossfade: _tone_a/b/c are what the painters read (the
        # displayed colors); on a palette change they lerp toward the
        # targets below over _TONE_FADE_MS instead of snapping. blend == 1
        # means settled. bg itself always snaps — it only moves on a real
        # theme switch, and the QSS restyle around it snaps anyway.
        self._tone_ta = QColor(self._tone_a)
        self._tone_tb = QColor(self._tone_b)
        self._tone_tc = QColor(self._tone_c)
        self._tone_fa = QColor(self._tone_a)
        self._tone_fb = QColor(self._tone_b)
        self._tone_fc = QColor(self._tone_c)
        self._tone_blend: float = 1.0
        self._pulse: float = 0.0            # target from the audio feed
        self._pulse_peak: float = 0.0       # preserve attacks between paints
        self._pulse_shown: float = 0.0      # smoothed value actually painted
        self._last_tick: float = 0.0        # when _tick last painted
        self._t0 = time.monotonic()
        # Playback speed (see set_speed) and the song clock it drives.
        self._speed: float = 1.0
        self._song_t: float = 0.0
        self._song_last: float | None = None
        # Lightning-style strike state. Seed regenerates per strike so each
        # bolt has its own shape; t0 drives the flash/fade envelope.
        self._bolt_seed: int = 1
        self._bolt_t0: float = -10.0
        self._strike_prev: float = 0.0
        self._next_auto_strike: float = 3.0
        # Ripples state: live drops as (x 0..1, y 0..1, born, strength) in
        # the same wall clock as the strikes.
        self._drops: list[tuple[float, float, float, float]] = []
        self._drop_seed: int = 7
        self._drop_prev: float = 0.0
        self._last_drop: float = -10.0
        self._next_auto_drop: float = 0.6
        # Vector strokes for the current frame (contours, vinyl), set by the
        # renderer and drawn over the upscaled field at window resolution.
        self._vec: tuple | None = None
        self._groove_img: QImage | None = None
        self._groove_key: tuple | None = None
        self._label_img: QImage | None = None
        self._label_key: tuple | None = None
        self._buf: QImage | None = None
        # The last rendered frame, reused for every paint until a tick (or
        # a style/theme/size change) marks it stale. Without this, every
        # child repaint (a progress tick, a hover, a text fade) re-rendered
        # the whole backdrop under it.
        self._frame: QImage | None = None
        self._frame_size: tuple[int, int] = (0, 0)
        self._frame_stale: bool = True
        # Liquid-cover state. The full-res art is cached whatever the current
        # style is (it arrives whenever the adaptive pipeline fetches it, and
        # holding a reference is free), so switching to liquid mid-song works.
        # The derived color fields are only built while liquid actually paints.
        self._art: QImage | None = None
        self._liq_from: np.ndarray | None = None   # previous track's field
        self._liq_from_mean: float = 0.0
        self._liq_to: np.ndarray | None = None     # current track's field
        self._liq_to_mean: float = 0.0
        self._liq_blend: float = 1.0               # 1 == settled on _liq_to
        self._liq_size: tuple[int, int] = (0, 0)
        self._liq_grid: tuple[np.ndarray, np.ndarray] | None = None
        # Swirl time accumulates scaled by the motion mode, so "off" freezes
        # the liquify mid-pour instead of snapping it flat, and mode changes
        # never jump the phase.
        self._liq_phase: float = 0.0
        self._liq_last: float | None = None
        # Procedural-field state, shared by every _FX_STYLES style: the
        # cached coordinate grid at the current render size and the same
        # phase-accumulation clock the liquid style uses, so motion "off"
        # freezes a scene mid-churn instead of snapping it flat.
        self._fx_size: tuple[int, int] = (0, 0)
        self._fx_grid: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._fx_phase: float = 0.0
        self._fx_last: float | None = None

        # Legibility probe state (ink_range): the frame last painted, a
        # counter that moves on every paint, and the eased grid built from
        # it on demand. The vinyl label is drawn over the field at window
        # res, so its placement rides along for the grid to include.
        self._ink_src: QImage | None = None
        self._ink_frame: int = 0
        self._ink_seen: int = -1
        self._ink_grid: np.ndarray | None = None
        self._ink_t: float | None = None
        self._ink_version: int = 0
        self._ink_spans: dict[tuple[int, int, int, int], tuple[float, float]] = {}
        self._ink_label: tuple[float, float, float, float] | None = None
        self._ink_ref: float = contrast.apca_y(self._bg)

        self._anim = QTimer(self)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.setInterval(_ANIM_INTERVAL_MS)
        self._anim.timeout.connect(self._tick)

        theming.manager().theme_changed.connect(self._on_theme)
        # Effective, not base: a backdrop built mid-song (the fullscreen
        # window, the first mini open) must anchor to the album palette
        # already in force — the driver's re-push for the same song is a
        # no-op that never re-emits, so there is no later catch-up.
        self._on_theme(theming.manager().current_effective())

    # ---------- public API ----------

    def _invalidate(self) -> None:
        self._frame_stale = True
        self.update()

    def set_enabled(self, on: bool) -> None:
        if on == self._enabled:
            return
        self._enabled = on
        self._sync_timer()
        self._invalidate()

    def set_radius(self, radius: int) -> None:
        r = max(0, int(radius))
        if r == self._radius:
            return
        self._radius = r
        self._invalidate()

    def set_style(self, style: str) -> None:
        new_style = style if style in _STYLES else "field"
        if new_style == self._style:
            return
        self._style = new_style
        if new_style != "vinyl":
            self._groove_img = self._groove_key = None
            self._label_img = self._label_key = None
        self._invalidate()

    def set_art(self, image: QImage | None) -> None:
        """Feed the current track's full-res cover (or None when nothing is
        playing / the fetch failed). Only the liquid style draws it; everyone
        else ignores the stored reference. Fed by the adaptive driver for the
        main window and by the mini player's own art fetch, so it tracks the
        same art the rest of the UI shows."""
        if image is not None and image.isNull():
            image = None
        self._art = image
        if image is None:
            # Nothing playing (or the fetch failed): drop the fields so the
            # renderer falls back to the living-fields look, whose tones are
            # already fading home because the adaptive driver cleared its
            # overrides on the same event.
            self._liq_from = None
            self._liq_to = None
            self._liq_blend = 1.0
            if self._enabled and self._style == "liquid":
                self._invalidate()
            return
        if self._style != "liquid":
            # Not painting it — just remember the art and drop stale fields.
            self._liq_from = None
            self._liq_to = None
            self._liq_blend = 1.0
            return
        # Crossfade from whatever is on screen right now, same contract as
        # the tone fade: mid-fade track skips re-anchor and chain smoothly.
        if self._liq_to is not None and self._enabled and self._motion != "off" \
                and self.isVisible():
            t = self._liq_blend
            t = t * t * (3.0 - 2.0 * t)
            if self._liq_from is not None and t < 1.0:
                self._liq_from = self._liq_from + (self._liq_to - self._liq_from) * t
                self._liq_from_mean = (
                    self._liq_from_mean
                    + (self._liq_to_mean - self._liq_from_mean) * t
                )
            else:
                self._liq_from = self._liq_to
                self._liq_from_mean = self._liq_to_mean
            self._liq_blend = 0.0
        else:
            self._liq_from = None
            self._liq_blend = 1.0
        self._liq_to = None      # rebuilt from the new art at render size
        if self._enabled:
            self._sync_timer()
            self._invalidate()

    def set_motion(self, motion: str) -> None:
        new_motion = motion or "lite"
        if new_motion == self._motion:
            return
        self._motion = new_motion
        if new_motion == "off":
            self._snap_tones()
        self._sync_timer()
        self._invalidate()

    def set_speed(self, rate: float) -> None:
        """The song's playback speed. Every scene's clock runs at it, so a
        slowed song drifts slower and a sped-up one faster. Phases
        accumulate, so a change never jumps the picture, only its pace.
        Palette crossfades and lightning's flash stay on real time."""
        self._speed = max(0.25, min(4.0, float(rate or 1.0)))

    def _song_time(self) -> float:
        """Wall time at the playback speed, for the scenes that move with
        the song rather than with the motion setting (the gradient drift,
        ripples). Starts from the wall clock so a fresh widget matches
        the old behaviour at 1x; gaps (hidden, stalled) count as 0.25 s
        at most."""
        now = time.monotonic()
        if self._song_last is None:
            self._song_t = now - self._t0
        else:
            self._song_t += min(0.25, max(0.0, now - self._song_last)) * self._speed
        self._song_last = now
        return self._song_t

    def set_pulse(self, level: float) -> None:
        """Feed the current bass-energy envelope (0..1). Normally stored only
        — the animation timer paints it, so this can be called at audio rate
        without exceeding the repaint cap. A sharp *onset* paints right away
        (still rate-limited to the timer interval) so the swell lands on the
        beat instead of up to a frame later."""
        self._pulse = max(0.0, min(1.0, float(level)))
        self._pulse_peak = (max(self._pulse_peak, self._pulse)
                            if self._enabled and self.isVisible() else self._pulse)
        self._sync_timer()
        if (
            self._pulse - self._pulse_shown > 0.08
            and self._anim.isActive()
            and (time.monotonic() - self._last_tick) * 1000.0 >= _PULSE_INTERVAL_MS
        ):
            self._tick()
            self._anim.start()

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._pulse_peak = 0.0
        self._anim.stop()

    def _sync_timer(self) -> None:
        # Run only when there's something to animate and we're on screen.
        active = self._enabled and self.isVisible() and (
            self._motion != "off"
            or self._pulse > 0.001
            or self._pulse_shown > 0.001
            or self._pulse_peak > 0.001
            or self._tone_blend < 1.0
            or self._liq_blend < 1.0
        )
        if max(self._pulse, self._pulse_peak, self._pulse_shown) > 0.001:
            interval = _PULSE_INTERVAL_MS
        elif self._style in _GRADIENT_STYLES and self._tone_blend >= 1.0:
            interval = _GRADIENT_IDLE_MS
        else:
            interval = _ANIM_INTERVAL_MS
        if self._anim.interval() != interval:
            self._anim.setInterval(interval)
        if active and not self._anim.isActive():
            self._last_tick = time.monotonic()
            self._anim.start()
        elif not active and self._anim.isActive():
            self._anim.stop()

    def _tick(self) -> None:
        if not self._enabled or not self.isVisible():
            self._anim.stop()
            return
        now = time.monotonic()
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        changed = self._motion != "off"
        target = max(self._pulse, self._pulse_peak)
        self._pulse_peak = 0.0
        delta = target - self._pulse_shown
        if abs(delta) > 0.003:
            rate = 1.0 if delta > 0 else -math.expm1(-dt / _PULSE_RELEASE_S)
            self._pulse_shown += delta * rate
            changed = True
        else:
            changed = changed or self._pulse_shown != target
            self._pulse_shown = target
        if self._tone_blend < 1.0:
            self._tone_blend = min(
                1.0, self._tone_blend + dt * 1000.0 / _TONE_FADE_MS)
            # Smoothstep: gentle in, gentle out.
            t = self._tone_blend * self._tone_blend * (3.0 - 2.0 * self._tone_blend)
            self._tone_a = _lerp_color(self._tone_fa, self._tone_ta, t)
            self._tone_b = _lerp_color(self._tone_fb, self._tone_tb, t)
            self._tone_c = _lerp_color(self._tone_fc, self._tone_tc, t)
            changed = True
        if self._liq_blend < 1.0:
            self._liq_blend = min(
                1.0, self._liq_blend + dt * 1000.0 / _TONE_FADE_MS)
            changed = True
        if changed:
            handle = self.window().windowHandle()
            if handle is not None and not handle.isExposed():
                # Minimized or otherwise off screen: nothing would paint.
                # The stale frame is rendered when an expose repaints us.
                self._frame_stale = True
            else:
                self._invalidate()
        self._sync_timer()

    def _snap_tones(self) -> None:
        self._tone_a = QColor(self._tone_ta)
        self._tone_b = QColor(self._tone_tb)
        self._tone_c = QColor(self._tone_tc)
        self._tone_blend = 1.0

    # ---------- theme tracking ----------

    def _on_theme(self, theme) -> None:
        if theme is None:
            return
        # The adaptive driver pushes ambient_bg + accent_alt as dynamic
        # overrides;
        # the theming manager re-emits theme_changed when that happens, so we
        # re-derive the tones with no additional wiring.
        self._bg = QColor(theme.token("bg", "#0b0b0b"))
        self._ink_ref = contrast.apca_y(self._bg)
        surface = QColor(theme.token("bg_alt", self._bg.name()))
        if not surface.isValid():
            surface = QColor(self._bg)
        ambient_bg = QColor(
            theme.token("ambient_bg", theme.token("bg_alt", self._bg.name()))
        )
        if not ambient_bg.isValid():
            ambient_bg = QColor(self._bg)
        accent = QColor(theme.token("accent", "#d4b95e"))
        if not accent.isValid():
            accent = QColor(ambient_bg)
        accent_alt = QColor(theme.token("accent_alt", accent.name()))
        if not accent_alt.isValid():
            accent_alt = QColor(accent)

        # The adaptive driver flags a cover with no confident colour: the
        # field goes neutral (greys at the cover's brightness) instead of
        # borrowing the theme accent, which put a blue glow behind every
        # black-and-white sleeve.
        neutral = str(theme.token("ambient_neutral", "")).strip() == "1"
        if neutral:
            grey_l = ambient_bg.lightnessF() if ambient_bg.isValid() else 0.5
            k = 0.7 + 0.6 * grey_l           # darker sleeve, dimmer field
            neutral_c = QColor(128, 128, 128)
            body = accent = accent_alt = neutral_c
        else:
            k = 1.0
            body_has_hue = _hls_saturation(ambient_bg) >= 0.04
            body = ambient_bg if body_has_hue else surface
            if not body_has_hue or _hls_saturation(accent) < 0.04:
                accent = QColor(body)
            if not body_has_hue or _hls_saturation(accent_alt) < 0.04:
                accent_alt = QColor(accent)

        # Album-derived hues placed in a visible band around the theme bg:
        # for a dark theme, tones sit a clear step *lighter* than bg (so the
        # gradient reads against black); for a light theme, a step darker.
        # Content stays legible because these are still well away from fg.
        if self._bg.lightnessF() > 0.5:
            new_a = _bg_tone(body, min(0.92, 0.78 * (2.0 - k)), 0.22)
            new_b = _bg_tone(accent, min(0.90, 0.70 * (2.0 - k)), 0.28)
            new_c = _bg_tone(accent_alt, min(0.88, 0.62 * (2.0 - k)), 0.32)
        else:
            new_a = _bg_tone(body, 0.22 * k, 0.34)
            new_b = _bg_tone(accent, 0.29 * k, 0.38)
            new_c = _bg_tone(accent_alt, 0.34 * k, 0.42)

        # Same targets as the fade already in flight (theme_changed re-fires
        # for scale changes and override re-emits): leave the blend alone.
        if (new_a.rgb() == self._tone_ta.rgb()
                and new_b.rgb() == self._tone_tb.rgb()
                and new_c.rgb() == self._tone_tc.rgb()):
            self._invalidate()
            return
        self._tone_ta, self._tone_tb, self._tone_tc = new_a, new_b, new_c
        if self._motion == "off" or not self._enabled or not self.isVisible():
            # No animation budget (or nobody watching): keep the old snap.
            self._snap_tones()
        else:
            # Crossfade from whatever is on screen right now — mid-fade
            # palette changes re-anchor, so fast skips chain smoothly.
            self._tone_fa = QColor(self._tone_a)
            self._tone_fb = QColor(self._tone_b)
            self._tone_fc = QColor(self._tone_c)
            self._tone_blend = 0.0
            self._sync_timer()
        self._invalidate()

    # ---------- liquid cover ----------

    def _field_dims(self, w: int, h: int) -> tuple[int, int]:
        # Shared by the liquid style and the procedural-field styles: both
        # do their math on a grid capped at _LIQ_CAP on the long side and
        # ride the smooth upscale in paintEvent.
        if w >= h:
            rw = min(w, _LIQ_CAP)
            rh = max(2, round(rw * h / max(1, w)))
        else:
            rh = min(h, _LIQ_CAP)
            rw = max(2, round(rh * w / max(1, h)))
        return rw, rh

    def _build_liquid_field(self, art: QImage, rw: int, rh: int
                            ) -> tuple[np.ndarray, float]:
        """Blur the cover down to its primary color masses, stretched to the
        render aspect. Returns (float32 (rh, rw, 3) field, mean luminance on
        the 0..255 scale). Downscaling to a handful of cells and smoothly
        upsampling in two steps approximates an enormous gaussian blur
        without ever paying for one."""
        tiny = art.scaled(_LIQ_TINY, _LIQ_TINY,
                          Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        mid = tiny.scaled(max(2, rw // 3), max(2, rh // 3),
                          Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = mid.scaled(rw, rh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = big.convertToFormat(QImage.Format_RGB888)
        stride = big.bytesPerLine()
        raw = bytes(big.constBits()[: stride * rh])
        field = (
            np.frombuffer(raw, dtype=np.uint8)
            .reshape(rh, stride)[:, : rw * 3]
            .reshape(rh, rw, 3)
            .astype(np.float32)
        )
        # All that averaging washes the colors toward each other; spread them
        # back apart so the field still reads as THIS album's colors.
        grey = field.mean(axis=2, keepdims=True)
        field = grey + (field - grey) * _LIQ_CHROMA
        lum = (0.2126 * field[..., 0] + 0.7152 * field[..., 1]
               + 0.0722 * field[..., 2])
        return field, float(lum.mean())

    def _render_liquid(self, w: int, h: int) -> QImage | None:
        """The album cover as a slow pour of paint. Blur field from
        _build_liquid_field, displaced per frame by two octaves of traveling
        sines (the liquify), sampled bilinearly. Bass fattens the
        displacement, pumps a slight zoom and lifts the brightness, so a kick
        makes the whole pour swell. Returns None when there's no art to draw
        — the caller falls back to the living-fields look."""
        rw, rh = self._field_dims(max(1, w), max(1, h))
        if (rw, rh) != self._liq_size:
            # Window aspect changed: restretch at the new shape. A mid-fade
            # resize snaps to the current track — the previous track's field
            # only exists at the old aspect.
            self._liq_size = (rw, rh)
            self._liq_grid = None
            self._liq_to = None
            self._liq_from = None
            self._liq_blend = 1.0
        if self._liq_to is None and self._art is not None:
            self._liq_to, self._liq_to_mean = self._build_liquid_field(
                self._art, rw, rh)
        if self._liq_to is None:
            return None

        src = self._liq_to
        mean = self._liq_to_mean
        tb = self._liq_blend
        if self._liq_from is not None and tb < 1.0:
            tb = tb * tb * (3.0 - 2.0 * tb)
            src = self._liq_from + (src - self._liq_from) * tb
            mean = self._liq_from_mean + (mean - self._liq_from_mean) * tb

        # Swirl clock — advances scaled by the motion mode, so "off" freezes
        # the pour mid-swirl (never flattens it) and lite just moves slower.
        now = time.monotonic() - self._t0
        if self._liq_last is None:
            self._liq_last = now
        dt = min(0.25, max(0.0, now - self._liq_last))
        self._liq_last = now
        motion = 0.0
        if self._motion != "off":
            motion = 1.0 if self._motion == "full" else 0.58
        self._liq_phase += dt * 0.9 * motion * self._speed
        tt = self._liq_phase
        pulse = math.pow(max(0.0, min(1.0, self._pulse_shown)), 1.12)
        dark = self._bg.lightnessF() <= 0.5

        if self._liq_grid is None:
            xs = np.linspace(0.0, 1.0, rw, dtype=np.float32)
            ys = np.linspace(0.0, 1.0, rh, dtype=np.float32)
            gy, gx = np.meshgrid(ys, xs, indexing="ij")
            self._liq_grid = (gx, gy)
        gx, gy = self._liq_grid

        two_pi = 2.0 * math.pi
        amp = 0.052 + 0.058 * pulse
        w1 = tt * (two_pi / 19.0)
        w2 = tt * (two_pi / 31.0)
        w3 = tt * (two_pi / 13.0)
        dx = amp * np.sin(two_pi * (0.9 * gy + 0.4 * gx) + w1) \
            + 0.6 * amp * np.sin(two_pi * (1.7 * gx - 1.2 * gy) - w2 + 1.3)
        dy = amp * np.cos(two_pi * (0.8 * gx - 0.5 * gy) - w3 + 0.7) \
            + 0.6 * amp * np.cos(two_pi * (1.4 * gy + 1.1 * gx) + w1 + 2.1)
        zoom = 1.0 - 0.06 * pulse
        sx = np.clip((gx - 0.5) * zoom + 0.5 + dx, 0.0, 1.0) * (rw - 1)
        sy = np.clip((gy - 0.5) * zoom + 0.5 + dy, 0.0, 1.0) * (rh - 1)

        x0 = sx.astype(np.int32)
        y0 = sy.astype(np.int32)
        x1 = np.minimum(x0 + 1, rw - 1)
        y1 = np.minimum(y0 + 1, rh - 1)
        fx = (sx - x0)[..., None]
        fy = (sy - y0)[..., None]
        top = src[y0, x0] * (1.0 - fx) + src[y0, x1] * fx
        bot = src[y1, x0] * (1.0 - fx) + src[y1, x1] * fx
        out = top * (1.0 - fy) + bot * fy

        # Normalize brightness into the band the other styles live in, then
        # let bass lift it (dark themes) or press it (light themes) — the
        # same direction reactive() pushes everywhere else.
        target = (_LIQ_MEAN_L_DARK if dark else _LIQ_MEAN_L_LIGHT) * 255.0
        gain = max(0.25, min(3.4, target / max(mean, 1.0)))
        gain *= (1.0 + 0.40 * pulse) if dark else (1.0 - 0.16 * pulse)
        # A whiff of the theme bg keeps the pour sitting in the room instead
        # of pasted over it.
        out *= gain * 0.88
        out[..., 0] += 0.12 * self._bg.red()
        out[..., 1] += 0.12 * self._bg.green()
        out[..., 2] += 0.12 * self._bg.blue()

        out8 = np.ascontiguousarray(np.clip(out, 0.0, 255.0).astype(np.uint8))
        img = QImage(out8.data, rw, rh, rw * 3, QImage.Format_RGB888).copy()
        if self._bg.alpha() < 255:
            # Translucent theme: keep the glass — composite the pour at
            # partial opacity instead of replacing the window tint.
            over = QImage(rw, rh, QImage.Format_ARGB32_Premultiplied)
            over.fill(QColor(0, 0, 0, 0))
            op = QPainter(over)
            op.setOpacity(0.78)
            op.drawImage(0, 0, img)
            op.end()
            img = over

        vp = QPainter(img)
        vignette = QRadialGradient(rw * 0.52, rh * 0.46, max(rw, rh) * 0.86)
        vignette.setColorAt(0.00, _alpha(self._bg, 0))
        vignette.setColorAt(0.68, _alpha(self._bg, 0))
        vignette.setColorAt(1.00, _alpha(self._bg, 88 if dark else 70))
        vp.fillRect(QRectF(0, 0, rw, rh), QBrush(vignette))
        vp.end()
        return img

    # ---------- procedural fields ----------

    def _fx_time(self) -> float:
        """Scene clock for the field styles. Accumulates scaled by the
        motion mode (same contract as the liquid swirl clock): "off"
        freezes every scene mid-churn, "lite" just moves slower, and mode
        changes never jump the phase."""
        now = time.monotonic() - self._t0
        if self._fx_last is None:
            self._fx_last = now
        dt = min(0.25, max(0.0, now - self._fx_last))
        self._fx_last = now
        motion = 0.0
        if self._motion != "off":
            motion = 1.0 if self._motion == "full" else 0.58
        self._fx_phase += dt * motion * self._speed
        return self._fx_phase

    @staticmethod
    def _lat_sample(lat: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Smooth wrap-sampled read of one noise lattice at fractional
        coordinates (lattice units). Bilinear with a smoothstep on the
        fractions — value noise, cheap and outline-free by nature."""
        side = lat.shape[0]
        x0 = np.floor(x)
        y0 = np.floor(y)
        fx = x - x0
        fy = y - y0
        fx = fx * fx * (3.0 - 2.0 * fx)
        fy = fy * fy * (3.0 - 2.0 * fy)
        xi = x0.astype(np.int32) % side
        yi = y0.astype(np.int32) % side
        xj = (xi + 1) % side
        yj = (yi + 1) % side
        top = lat[yi, xi] * (1.0 - fx) + lat[yi, xj] * fx
        bot = lat[yj, xi] * (1.0 - fx) + lat[yj, xj] * fx
        return top + (bot - top) * fy

    def _fbm(self, x: np.ndarray, y: np.ndarray, t: float, *,
             first: int = 0, octaves: int = 3, gain: float = 0.55,
             lac: float = 1.9, drift: float = 0.06,
             morph: float = 0.13) -> np.ndarray:
        """2–3 octave fbm over the seeded lattice stack, roughly 0..1.

        Time evolution is two-fold and that's the trick: each octave
        *drifts* (coordinates slide, opposite directions per octave so the
        composite shears instead of scrolling) and *morphs* (each octave
        blends between two lattice planes on its own slow cosine), so the
        field boils like a medium rather than panning like a texture.
        ``first`` picks where in the stack this field's plane pairs start,
        so two fields in one scene never share structure."""
        lat = _fx_lattices()
        count = lat.shape[0]
        total: np.ndarray | None = None
        norm = 0.0
        amp = 1.0
        freq = 1.0
        for o in range(octaves):
            ia = (first + 2 * o) % count
            ib = (first + 2 * o + 1) % count
            ddx = drift * t * freq * (1.0 if o % 2 == 0 else -0.7)
            ddy = drift * t * freq * (0.6 if o % 2 == 0 else -1.0)
            m = 0.5 - 0.5 * math.cos(t * morph * (1.0 + 0.7 * o) + o * 2.1)
            # Morph the two planes BEFORE sampling: the lattice is 48²
            # while the render grid is ~30k px, so blending up front
            # halves the per-pixel gather work for an identical result.
            plane = lat[ia] + (lat[ib] - lat[ia]) * np.float32(m)
            v = self._lat_sample(plane,
                                 x * freq + ddx + o * 7.3,
                                 y * freq + ddy + o * 3.1)
            total = v * amp if total is None else total + v * amp
            norm += amp
            amp *= gain
            freq *= lac
        return total / norm

    def _fx_compose(self, rw: int, rh: int,
                    layers: list[tuple[np.ndarray, QColor]]) -> QImage:
        """Composite intensity fields into the frame. Each layer is
        (intensity 0..~1, tone); opaque themes get bg + Σ i·(tone − bg),
        which brightens dark themes and darkens light ones with the same
        math — the light-theme inversion falls out of the tones already
        sitting on the other side of bg. Translucent themes composite the
        accumulated light over clear so the glass tint beneath stays a
        single layer. Every contour is a smooth field, so the no-outline
        invariant holds by construction."""
        bgc = self._bg
        if bgc.alpha() == 255:
            out = np.empty((rh, rw, 3), np.float32)
            out[..., 0] = bgc.red()
            out[..., 1] = bgc.green()
            out[..., 2] = bgc.blue()
            for inten, col in layers:
                delta = np.array([col.red() - bgc.red(),
                                  col.green() - bgc.green(),
                                  col.blue() - bgc.blue()], np.float32)
                out += inten[..., None] * delta
            out8 = np.ascontiguousarray(
                np.clip(out, 0.0, 255.0).astype(np.uint8))
            return QImage(out8.data, rw, rh, rw * 3,
                          QImage.Format_RGB888).copy()
        acc = np.zeros((rh, rw, 3), np.float32)
        asum = np.zeros((rh, rw), np.float32)
        for inten, col in layers:
            ic = np.clip(inten, 0.0, 1.5).astype(np.float32)
            acc += ic[..., None] * np.array(
                [col.red(), col.green(), col.blue()], np.float32)
            asum += ic
        alpha = np.clip(asum, 0.0, 1.0) * 0.82
        denom = np.maximum(asum, 1e-4)[..., None]
        prem = np.clip(acc / denom, 0.0, 255.0) * alpha[..., None]
        buf = np.empty((rh, rw, 4), np.uint8)
        p8 = prem.astype(np.uint8)
        buf[..., 0] = p8[..., 2]                       # BGRA byte order
        buf[..., 1] = p8[..., 1]
        buf[..., 2] = p8[..., 0]
        buf[..., 3] = (alpha * 255.0).astype(np.uint8)
        return QImage(np.ascontiguousarray(buf).data, rw, rh, rw * 4,
                      QImage.Format_ARGB32_Premultiplied).copy()

    def _render_fx(self, w: int, h: int) -> QImage:
        """The field-based scene styles. Coordinates: ``gy`` spans 0..1
        down the frame, ``ax`` is x in the same height units (0..aspect),
        so distances are isotropic at any window shape. All color still
        comes from the three adaptive tones + bg; bass reaches every scene
        both through rtone (brightness) and through geometry (growth)."""
        rw, rh = self._field_dims(max(1, w), max(1, h))
        if self._fx_grid is None or self._fx_size != (rw, rh):
            self._fx_size = (rw, rh)
            xs = np.linspace(0.0, 1.0, rw, dtype=np.float32)
            ys = np.linspace(0.0, 1.0, rh, dtype=np.float32)
            gy, gx = np.meshgrid(ys, xs, indexing="ij")
            self._fx_grid = (gx, gy, gx * (rw / rh))
        gx, gy, ax = self._fx_grid
        # 1-D views of the same coordinates. Most scene shapes are outer
        # products of x- and y-profiles (gaussians, edge falloffs), so the
        # expensive transcendentals run on a row and a column and only the
        # final multiply touches the full grid.
        xs = ax[0]
        ys = gy[:, 0]
        aspect = rw / rh
        t = self._fx_time()
        pulse = math.pow(max(0.0, min(1.0, self._pulse_shown)), 1.12)
        dark = self._bg.lightnessF() <= 0.5

        def rtone(c: QColor, mix: float) -> QColor:
            # Same contract as reactive() on the gradient path: tones sit
            # partway toward bg; bass lifts them on dark themes, presses
            # them on light ones.
            out = _lerp(self._bg, c, min(1.0, mix + 0.08 * pulse))
            if pulse <= 0.0:
                return out
            factor = int(100 + 42 * pulse)
            return out.lighter(factor) if dark else out.darker(factor)

        qa = rtone(self._tone_a, 0.74)
        qb = rtone(self._tone_b, 0.70)
        qc = rtone(self._tone_c, 0.66)
        layers: list[tuple[np.ndarray, QColor]] = []

        if self._style == "vbeam":
            # Hill of light seen through haze. The arch is a gaussian
            # mound anchored below the bottom edge, but the fbm haze
            # multiplies straight into its body, so what reaches the eye
            # is broken, airborne light — never the ellipse underneath.
            # Bass raises the arch and gathers a crest at the apex.
            sway = 0.020 * math.sin(t * 0.31)
            bx = (0.50 + sway) * aspect
            arch_h = 0.42 + 0.05 * math.sin(t * 0.17) + 0.46 * pulse
            half_w = aspect * (0.40 + 0.14 * pulse)
            # Squashed-and-clipped fbm: patchy, with real gaps between the
            # bright shreds — this is what sells "light through haze".
            haze = np.clip(
                (self._fbm(ax * 2.6, gy * 2.6 - t * 0.05, t, first=0)
                 - 0.28) * 1.6, 0.0, None)

            def dome(kx: float, ky: float, scale: float) -> np.ndarray:
                # exp(-(x² + y²)) separates into an x-profile × y-profile
                # outer product — same gaussian, a fraction of the exps.
                ex = np.exp(-(((xs - bx) / kx) ** 2) * scale)
                ey = np.exp(-(((ys - 1.06) / ky) ** 2) * scale)
                return ey[:, None] * ex[None, :]

            body = dome(half_w, arch_h, 1.5) * (0.35 + 1.00 * haze)
            core = dome(half_w, arch_h, 3.4) * (0.50 + 0.85 * haze)
            ux = np.exp(-((xs - bx) / (aspect * 0.85)) ** 2)
            uy = np.exp(-((ys - 1.10) / 0.45) ** 2)
            layers.append((uy[:, None] * ux[None, :]
                           * (0.35 + 0.25 * pulse), qa))
            layers.append((body * (0.60 + 0.40 * pulse), qb))
            layers.append((core * (0.50 + 0.45 * pulse), qb))
            if pulse > 0.01:
                apy = 1.06 - arch_h * 0.92
                crx = np.exp(-(((xs - bx) / (aspect * 0.30)) ** 2) * 1.6)
                cry = np.exp(-(((ys - apy) / 0.22) ** 2) * 1.6)
                crest = cry[:, None] * crx[None, :]
                layers.append(
                    (crest * (0.60 * pulse) * (0.5 + 0.8 * haze), qc))
            return self._fx_compose(rw, rh, layers)

        if self._style == "horizon":
            # Sunset over water. The sky is a noise-banded glow deepening
            # toward the horizon line, the sun a soft two-scale bloom
            # resting on it, the water a dimmer mirror whose reflection
            # column glints through stretched noise. The sky/water masks
            # blend across the line — no hard band anywhere, and the sun
            # bloom deliberately straddles it.
            hy = 0.60 + 0.02 * math.sin(t * 0.13)
            sunx = (0.50 + 0.14 * math.sin(t * 0.09)) * aspect
            bn = self._fbm(ax * 2.2, gy * 1.8 + t * 0.02, t, first=2)
            # Vertical profiles are pure functions of y — computed on the
            # column and broadcast, like the gaussians in the other styles.
            msky = np.clip((hy - ys) / 0.04, 0.0, 1.0)[:, None]
            mwat = 1.0 - msky
            skyw = np.clip(ys / hy, 0.0, 1.0)       # 0 at top → 1 at horizon
            layers.append((((1.0 - skyw)[:, None] * msky) * 0.22
                           * (0.5 + 0.6 * bn), qa))
            layers.append(((skyw ** 1.7)[:, None] * msky
                           * (0.50 + 0.60 * bn) * (0.55 + 0.30 * pulse), qb))
            depth = np.clip((ys - hy) / max(1e-3, 1.0 - hy), 0.0, 1.0)
            shn = self._fbm(ax * 9.0 + t * 0.16, (gy - hy) * 26.0, t,
                            first=4, octaves=2)
            glint = np.clip(shn - 0.50, 0.0, 1.0) * 2.2
            wat_body = ((1.0 - depth) ** 1.6)[:, None] * (0.55 + 0.50 * bn)
            layers.append((mwat * wat_body * (0.28 + 0.22 * pulse), qb))
            refl = (np.exp(-((xs - sunx)
                             / (aspect * (0.15 + 0.07 * pulse))) ** 2)[None, :]
                    * np.exp(-depth * 2.2)[:, None])
            layers.append(
                (mwat * refl * (0.38 + 0.50 * pulse) * (0.35 + 1.3 * glint),
                 qc))
            halo = (np.exp(-((ys - hy) / 0.42) ** 2)[:, None]
                    * np.exp(-((xs - sunx) / 0.85) ** 2)[None, :])
            sun = (np.exp(-((ys - hy) / (0.09 + 0.10 * pulse)) ** 2)[:, None]
                   * np.exp(-((xs - sunx)
                              / (0.26 + 0.16 * pulse)) ** 2)[None, :])
            # A tighter hot heart inside the bloom so the sun reads as a
            # body, not just a warm patch of sky.
            heart = (np.exp(-((ys - hy) / (0.045 + 0.05 * pulse)) ** 2)[:, None]
                     * np.exp(-((xs - sunx)
                                / (0.10 + 0.07 * pulse)) ** 2)[None, :])
            layers.append((halo * (0.28 + 0.22 * pulse), qb))
            layers.append((sun * (0.85 + 0.55 * pulse), qc))
            layers.append((heart * (0.55 + 0.45 * pulse), qc))
            return self._fx_compose(rw, rh, layers)

        if self._style == "ripples":
            # Rain on dark water. Each kick drops a ring that runs out
            # across the surface and fades, with a fainter ring trailing
            # it; between kicks (and on songs the pulse can't read) a drop
            # falls every couple of seconds on its own. Rings live on the
            # song clock (wall time at the playback speed), not the motion
            # clock, so a ring already falling finishes even with motion
            # frozen, and runs out faster on a sped-up song.
            tw = self._song_time()
            prev = self._drop_prev
            self._drop_prev = pulse
            onset = (pulse - prev > 0.10 and pulse > 0.30
                     and tw - self._last_drop > 0.16)
            if onset or (self._motion != "off"
                         and tw >= self._next_auto_drop):
                rng = random.Random(self._drop_seed)
                self._drop_seed = (self._drop_seed * 69069
                                   + int(tw * 997.0) + 1) & 0xFFFFFF
                strength = (0.55 + 0.45 * pulse) if onset else 0.45
                self._drops.append((rng.uniform(0.10, 0.90),
                                    rng.uniform(0.12, 0.88), tw, strength))
                self._drops = self._drops[-_DROP_MAX:]
                self._last_drop = tw
                self._next_auto_drop = tw + 1.4 + 2.0 * rng.random()

            swell = self._fbm(ax * 1.7, gy * 1.7, t, first=4, octaves=2)
            layers.append(((0.24 + 0.34 * swell) * (0.9 + 0.3 * pulse), qa))
            # A slow sheen across the surface, lit from the top.
            sheen = (np.exp(-((ys - 0.18) / 0.55) ** 2)[:, None]
                     * np.clip(swell - 0.35, 0.0, None) * 1.4)
            layers.append((sheen * (0.30 + 0.20 * pulse), qb))
            rings = np.zeros((rh, rw), np.float32)
            crest = np.zeros((rh, rw), np.float32)
            alive = []
            for fx_, fy_, t0_, strength in self._drops:
                age = tw - t0_
                if age < 0.0 or age > _DROP_LIFE_S:
                    continue
                alive.append((fx_, fy_, t0_, strength))
                rad = 0.04 + 0.34 * age
                wid = 0.022 + 0.018 * age
                fade = strength * math.exp(-age / 1.15)
                # Squashed vertically: a water plane seen at an angle, so
                # the rings read as ripples on a surface, not bubbles. The
                # swell bends them a little.
                d = np.sqrt((((ys - fy_) / _DROP_TILT) ** 2)[:, None]
                            + ((xs - fx_ * aspect) ** 2)[None, :]) \
                    + 0.03 * (swell - 0.5)
                u = (d - rad) / wid
                lead = np.exp(-u * u)
                u2 = (d - rad + 0.075) / (wid * 1.4)
                rings += (lead + 0.45 * np.exp(-u2 * u2)) * fade
                crest += lead * lead * fade
            self._drops = alive
            layers.append((rings * 0.60, qb))
            layers.append((crest * 0.55, qc))
            return self._fx_compose(rw, rh, layers)

        if self._style == "stage":
            # Stage lights. Three lamps above the frame throw cones that
            # sweep side to side at their own pace; the haze they cut
            # through decides what lights up, and the light pools on the
            # floor. Bass widens and brightens the cones.
            haze = self._fbm(ax * 2.2, gy * 2.2 - t * 0.05, t, first=3)
            lit = 0.30 + 1.00 * haze
            layers.append((haze * 0.12 + 0.04, qa))
            wid = 0.075 + 0.055 * pulse
            for bx, tone, ph, spd in ((0.18, qb, 0.0, 0.31),
                                      (0.50, qc, 2.1, 0.23),
                                      (0.82, qb, 4.2, 0.27)):
                ox = bx * aspect
                aim = 0.40 * math.sin(t * spd + ph) + (0.5 - bx) * 0.55
                dx = (xs - ox)[None, :]
                dy = (ys + 0.12)[:, None]
                off = np.arctan2(dx, dy) - aim
                fall = np.exp(-np.sqrt(dx * dx + dy * dy) * 0.55)
                cone = np.exp(-(off / wid) ** 2)
                core = np.exp(-(off / (wid * 0.35)) ** 2)
                layers.append((cone * fall * lit * (0.55 + 0.60 * pulse),
                               tone))
                layers.append((core * fall * lit * (0.22 + 0.35 * pulse),
                               tone))
            floor = (np.exp(-((ys - 1.04) / 0.24) ** 2)[:, None]
                     * (0.6 + 0.6 * haze))
            layers.append((floor * (0.22 + 0.30 * pulse), qa))
            return self._fx_compose(rw, rh, layers)

        if self._style == "rimlight":
            # Rim light. The edge falloffs breathe through a slow fbm
            # field, so the frame reads as lit air pooling at the borders
            # rather than a vignette stamp; corner blooms stay the light
            # sources. Deliberately near-static — the noise drifts,
            # nothing travels.
            n = self._fbm(ax * 3.6, gy * 3.6, t * 0.5, first=3)
            wv = 0.14 + 0.20 * pulse           # of height, like the old style
            wh_ = 0.10 + 0.14 * pulse          # of width
            ux = gx[0]                          # plain 0..1 across the width
            exl = ux / wh_
            eyt = ys / wv
            # The whole frame profile is a sum of 1-D edge falloffs — the
            # exps run on a row and a column, broadcast to 2-D at the add.
            fx1 = np.exp(-exl) + np.exp(-(1.0 / wh_ - exl))
            fy1 = np.exp(-eyt) + np.exp(-(1.0 / wv - eyt))
            frame = fy1[:, None] + fx1[None, :]
            breathe = 0.45 + 1.10 * n
            layers.append((frame * breathe * (0.32 + 0.40 * pulse), qb))
            fx2 = np.exp(-exl * 0.45) + np.exp(-(1.0 / wh_ - exl) * 0.45)
            fy2 = np.exp(-eyt * 0.45) + np.exp(-(1.0 / wv - eyt) * 0.45)
            layers.append(((fy2[:, None] + fx2[None, :]) * breathe
                           * (0.06 + 0.09 * pulse), qa))
            cr = 0.20 + 0.10 * pulse
            crx0 = np.exp(-(xs ** 2) / (cr * cr))
            crx1 = np.exp(-((xs - aspect) ** 2) / (cr * cr))
            cry0 = np.exp(-(ys ** 2) / (cr * cr))
            cry1 = np.exp(-((ys - 1.0) ** 2) / (cr * cr))
            blooms = ((cry0[:, None] + cry1[:, None])
                      * (crx0[None, :] + crx1[None, :]))
            layers.append((blooms * (0.28 + 0.42 * pulse)
                           * (0.5 + 0.8 * n), qc))
            return self._fx_compose(rw, rh, layers)

        if self._style == "lightning":
            # Storm cell. The deck is a turbulent fbm cloud mass now, lit
            # from inside by the flash; strikes keep their wall-clock
            # envelope so a flash decays in real time even while motion is
            # frozen. The bolt stays the one deliberate line in the whole
            # module — fractal midpoint displacement, width tapering
            # toward the ground, layered soft passes so nothing reads as a
            # crisp UI stroke.
            tw = time.monotonic() - self._t0
            prev = self._strike_prev
            self._strike_prev = pulse
            age = tw - self._bolt_t0
            if ((pulse - prev > 0.10 and pulse > 0.30 and age > 0.28)
                    or (self._motion != "off"
                        and tw >= self._next_auto_strike)):
                self._bolt_seed = (self._bolt_seed * 69069
                                   + int(tw * 997.0) + 1) & 0xFFFFFF
                self._bolt_t0 = tw
                age = 0.0
                self._next_auto_strike = tw + 7.0 + 9.0 * random.Random(
                    self._bolt_seed).random()
            flash = math.exp(-age / 0.10)        # scene illumination
            vis = math.exp(-age / 0.16) * (0.75 + 0.25 * math.cos(age * 90.0))
            if age > 0.55:
                flash = 0.0
                vis = 0.0

            dn = self._fbm(ax * 2.6 + t * 0.04, gy * 3.2, t, first=5)
            topw = (np.clip(1.30 - ys * 2.1, 0.0, 1.0) ** 1.3)[:, None]
            deck = topw * (0.30 + 0.90 * dn)
            billow = np.clip(dn - 0.52, 0.0, 1.0) * 2.2 * topw
            layers.append((deck * (0.42 + 0.30 * pulse + 0.55 * flash), qa))
            layers.append(
                (billow * (0.34 + 0.30 * pulse + 0.60 * flash), qb))
            if flash > 0.003:
                layers.append(
                    (np.full((rh, rw), 0.12 * flash, np.float32), qc))

            rng = random.Random(self._bolt_seed)
            x_top = (0.25 + 0.50 * rng.random()) * aspect
            x_hit = x_top + aspect * rng.uniform(-0.16, 0.16)
            y_hit = 0.62 + 0.28 * rng.random()
            if vis > 0.02:
                # Impact glow goes into the field pass so it composites as
                # softly as the clouds do.
                igx = np.exp(-((xs - x_hit) / (0.14 + 0.10 * flash)) ** 2)
                igy = np.exp(-((ys - y_hit) / (0.09 + 0.07 * flash)) ** 2)
                layers.append((igy[:, None] * igx[None, :] * 0.55 * vis, qc))
            img = self._fx_compose(rw, rh, layers)
            if vis > 0.02:
                core_tone = (_lerp(qc, QColor(255, 255, 255), 0.85) if dark
                             else _lerp(qc, QColor(0, 0, 0), 0.55))

                def displace(p0: tuple[float, float], p1: tuple[float, float],
                             disp: float, depth: int
                             ) -> list[tuple[float, float]]:
                    # Fractal midpoint displacement, perpendicular to each
                    # segment, halving roughly per level — the classic
                    # bolt: big wander up high, fine jitter near the tip.
                    pts = [p0, p1]
                    d = disp
                    for _ in range(depth):
                        nxt = [pts[0]]
                        for a_, b_ in zip(pts, pts[1:]):
                            mx = (a_[0] + b_[0]) * 0.5
                            my = (a_[1] + b_[1]) * 0.5
                            dx_ = b_[0] - a_[0]
                            dy_ = b_[1] - a_[1]
                            ln = math.hypot(dx_, dy_) or 1.0
                            off = rng.uniform(-d, d)
                            nxt.append((mx - dy_ / ln * off,
                                        my + dx_ / ln * off))
                            nxt.append(b_)
                        pts = nxt
                        d *= 0.52
                    return pts

                pp = QPainter(img)
                pp.setRenderHint(QPainter.Antialiasing, True)

                def stroke(pts_u: list[tuple[float, float]],
                           passes: list[tuple[float, float, QColor, int]]
                           ) -> None:
                    # Tapered layered strokes: width shrinks toward the
                    # ground per pass, every pass a soft translucent pen.
                    n_seg = len(pts_u) - 1
                    for w0, w1, col, alpha in passes:
                        for i in range(n_seg):
                            f = i / max(1, n_seg)
                            wpx = max(1.0, (w0 + (w1 - w0) * f) * rh)
                            pp.setPen(QPen(_alpha(col, alpha), wpx,
                                           Qt.SolidLine, Qt.RoundCap,
                                           Qt.RoundJoin))
                            ax0, ay0 = pts_u[i]
                            ax1, ay1 = pts_u[i + 1]
                            pp.drawLine(QLineF(ax0 * rh, ay0 * rh,
                                               ax1 * rh, ay1 * rh))

                main = displace((x_top, -0.02), (x_hit, y_hit), 0.09, 5)
                stroke(main, [
                    (0.050, 0.018, qc, int(50 * vis)),
                    (0.020, 0.007, qc, int(110 * vis)),
                    (0.009, 0.003, core_tone, int(235 * vis)),
                ])
                # 1–2 branches forking from the upper half of the bolt.
                for _ in range(1 + rng.randint(0, 1)):
                    bx_, by_ = main[rng.randint(4, len(main) // 2)]
                    fx_ = bx_ + aspect * rng.uniform(-0.20, 0.20)
                    fy_ = by_ + (y_hit - by_) * rng.uniform(0.35, 0.65)
                    stroke(displace((bx_, by_), (fx_, fy_), 0.05, 4), [
                        (0.018, 0.006, qc, int(46 * vis)),
                        (0.005, 0.002, core_tone, int(150 * vis)),
                    ])
                pp.end()
            return img

        if self._style == "contours":
            # A topographic map. One slow fbm is the terrain; its contour
            # lines drift uphill over time and bass pushes them further,
            # every fifth one heavier like a survey map's index lines. The
            # field only carries the terrain shading and a faint bloom
            # under each line: the lines themselves are traced from it
            # (marching squares) and stroked at full resolution in
            # paintEvent. Drawn into the ~220 px field and stretched to the
            # window, they came out as smeared bands.
            h = self._fbm(ax * 1.4, gy * 1.4, t * 0.45, first=2,
                          drift=0.02)
            level = h * 14.0 - t * 0.12 - 0.9 * pulse
            nearest = np.round(level)
            gy_l, gx_l = np.gradient(level)
            slope = np.sqrt(gx_l * gx_l + gy_l * gy_l) + 1e-3
            dist = np.abs(level - nearest) / slope
            bloom = np.exp(-(dist / 2.6) ** 2)
            major = (np.mod(nearest, 5.0) == 0.0).astype(np.float32)
            layers.append(((0.12 + 0.32 * h) * (0.85 + 0.30 * pulse), qa))
            layers.append((bloom * (0.10 + 0.08 * pulse)
                           * (1.0 + 1.5 * major), qb))
            # Traced on every other grid point: the terrain is smooth, the
            # lines land in the same place, and there are half as many
            # segments to stroke.
            segs, majors = _contour_segments(level[::2, ::2])
            self._vec = ("contours", rw, rh, segs * 2.0, majors,
                         QColor(qb), QColor(qc), pulse)
            return self._fx_compose(rw, rh, layers)

        # vinyl — the last fx style. A record much bigger than the window,
        # its center just past the bottom-right corner, so the grooves
        # cross the frame as arcs. A wedge of reflected light swings back
        # and forth over the quarter of the disc in view (a full turn left
        # the frame dark half the time); the smooth gaps between songs
        # ring the disc; the label peeks in at the corner. Bass lifts the
        # sheen and pumps a ring out from the label. The field is the
        # disc and its soft light; the grooves and the label's edge are
        # vector strokes laid on in paintEvent (see contours).
        cx = aspect + 0.16
        cy = 1.10
        dx = (xs - cx)[None, :]
        dy = (ys - cy)[:, None]
        r = np.sqrt(dx * dx + dy * dy)
        ang = np.arctan2(dy, dx)
        gaps = np.abs(np.mod(r * 2.3, 1.0) - 0.5)
        tracks = 1.0 - 0.60 * np.exp(-(gaps / 0.035) ** 2)
        label_r = 0.34
        disc = np.clip((r - label_r) / 0.02, 0.0, 1.0)
        aim = -0.75 * math.pi + 0.50 * math.sin(t * 0.21)
        d1 = np.mod(ang - aim + math.pi, 2.0 * math.pi) - math.pi
        sw = 0.34 + 0.12 * pulse
        wedge = np.exp(-(d1 / sw) ** 2)
        layers.append((0.20 * tracks * disc * (0.9 + 0.3 * pulse), qa))
        layers.append((wedge * 0.75 * tracks * disc
                       * (0.45 + 0.45 * pulse), qb))
        if pulse > 0.01:
            kick = np.exp(-((r - (label_r + 0.05 + 0.55 * pulse)) / 0.05) ** 2)
            layers.append((kick * disc * 0.45 * pulse, qc))
        # The label is the album cover, turning with the record (scene
        # time, so motion "off" stops it and lite slows it).
        self._vec = ("vinyl", cx / aspect, cy, label_r, QColor(qc),
                     (t * _LABEL_DEG_PER_S) % 360.0)
        return self._fx_compose(rw, rh, layers)

    # ---------- paint ----------

    def _render_buffer(self, w: int, h: int) -> QImage:
        self._vec = None
        if self._style == "liquid":
            liq = self._render_liquid(w, h)
            if liq is not None:
                return liq
            # No art to melt (nothing playing, fetch failed) — fall through
            # to the living fields so the backdrop isn't a dead rectangle.
        elif self._style in _FX_STYLES:
            return self._render_fx(w, h)
        if w >= h:
            bw = min(w, _BUF_CAP)
            bh = max(1, round(bw * h / max(1, w)))
        else:
            bh = min(h, _BUF_CAP)
            bw = max(1, round(bh * w / max(1, h)))
        # Opaque themes render into RGB32; translucent (#AARRGGBB bg) themes
        # need a real alpha channel — an RGB32 buffer can't hold the "clear"
        # base (a SourceOver fill at alpha 0 is a no-op on it), so the frame
        # came out opaque, seeded with uninitialized memory. Same recipe as
        # the fx/liquid renderers' glass paths.
        fmt = (
            QImage.Format_RGB32
            if self._bg.alpha() == 255
            else QImage.Format_ARGB32_Premultiplied
        )
        if (
            self._buf is None
            or self._buf.width() != bw
            or self._buf.height() != bh
            or self._buf.format() != fmt
        ):
            self._buf = QImage(bw, bh, fmt)
        img = self._buf

        t = self._song_time()
        motion = 0.0
        if self._motion != "off":
            motion = 1.0 if self._motion == "full" else 0.58
        pulse = math.pow(max(0.0, min(1.0, self._pulse_shown)), 1.12)
        dark = self._bg.lightnessF() <= 0.5

        def wave(period: float, phase: float = 0.0) -> float:
            return math.sin((2 * math.pi * t / period) + phase)

        def reactive(color: QColor, mix: float) -> QColor:
            out = _lerp(self._bg, color, min(1.0, mix + 0.08 * pulse))
            if pulse <= 0.0:
                return out
            factor = int(100 + 42 * pulse)
            return out.lighter(factor) if dark else out.darker(factor)

        tone_a = reactive(self._tone_a, 0.74)
        tone_b = reactive(self._tone_b, 0.70)
        tone_c = reactive(self._tone_c, 0.66)
        clear = _alpha(self._bg, 0)
        max_side = max(bw, bh)

        # Translucent themes (#AARRGGBB bg tokens) get their tint from the
        # styled window beneath; filling it again here would stack alpha and
        # over-darken the glass, so their base is genuinely clear. Opaque
        # themes keep the solid base. fill() writes pixels directly (no
        # composition), so the clear actually lands on the ARGB buffer.
        if self._bg.alpha() == 255:
            img.fill(self._bg)
        else:
            img.fill(QColor(0, 0, 0, 0))
        pp = QPainter(img)
        pp.setRenderHint(QPainter.Antialiasing, True)

        if self._style == "band":
            angle = (
                _BASE_ANGLE
                + motion * 0.50 * wave(_PERIOD_FLOW_S, 0.2)
                + pulse * 0.05
            )
            extent = 0.70 + motion * 0.14 * wave(_PERIOD_FIELD_A_S, 1.0)
            extent *= 1.0 + 0.22 * pulse
            ox = motion * 0.09 * wave(_PERIOD_FIELD_B_S, 0.3)
            oy = motion * 0.07 * wave(_PERIOD_FIELD_C_S, 1.4)

            halo = QRadialGradient(
                bw * (0.50 + motion * 0.22 * wave(_PERIOD_FIELD_B_S, 2.0)),
                bh * (0.50 + motion * 0.18 * wave(_PERIOD_FIELD_C_S, 3.0)),
                0.62 * max_side * (1.0 + 0.18 * pulse),
            )
            halo.setColorAt(0.0, _alpha(tone_b, 34 + int(52 * pulse)))
            halo.setColorAt(0.48, _alpha(tone_a, 22 + int(32 * pulse)))
            halo.setColorAt(1.0, clear)
            pp.fillRect(img.rect(), QBrush(halo))

            cx, cy = bw * (0.5 + ox), bh * (0.5 + oy)
            dx, dy = math.cos(angle), math.sin(angle)
            half = 0.5 * extent * math.hypot(bw, bh)
            band = QLinearGradient(cx - dx * half, cy - dy * half,
                                   cx + dx * half, cy + dy * half)
            band_alpha = 112 + int(62 * pulse)
            band.setColorAt(0.00, clear)
            band.setColorAt(0.18, clear)
            band.setColorAt(0.40, _alpha(tone_a, band_alpha))
            band.setColorAt(0.58, _alpha(tone_b, min(220, band_alpha + 24)))
            band.setColorAt(0.82, clear)
            band.setColorAt(1.00, clear)
            pp.fillRect(img.rect(), QBrush(band))
            pp.end()
            return img

        def draw_field(
            color: QColor,
            base_x: float,
            base_y: float,
            radius: float,
            alpha: int,
            period: float,
            phase: float,
        ) -> None:
            drift_x = motion * 0.10 * wave(period, phase)
            drift_y = motion * 0.08 * wave(period * 1.19, phase + 1.7)
            kick_x = pulse * 0.035 * math.cos(phase + 0.8)
            kick_y = pulse * 0.030 * math.sin(phase + 0.4)
            x = bw * (base_x + drift_x + kick_x)
            y = bh * (base_y + drift_y + kick_y)
            r = max_side * radius * (1.0 + 0.16 * pulse)
            grad = QRadialGradient(x, y, r)
            grad.setColorAt(0.00, _alpha(color, alpha + int(34 * pulse)))
            grad.setColorAt(0.42, _alpha(color, int(alpha * 0.48) + int(22 * pulse)))
            grad.setColorAt(1.00, clear)
            pp.fillRect(img.rect(), QBrush(grad))

        draw_field(tone_a, 0.22, 0.22, 0.74, 76, _PERIOD_FIELD_A_S, 0.0)
        draw_field(tone_b, 0.82, 0.30, 0.70, 68, _PERIOD_FIELD_B_S, 2.2)
        draw_field(tone_c, 0.48, 0.86, 0.82, 58, _PERIOD_FIELD_C_S, 4.1)

        flow_angle = (
            _BASE_ANGLE
            + motion * 0.28 * wave(_PERIOD_FLOW_S, 0.5)
            + pulse * 0.07
        )
        flow_offset = motion * 0.10 * wave(_PERIOD_FLOW_S * 0.73, 2.0)
        cx = bw * (0.50 + flow_offset)
        cy = bh * (0.50 - flow_offset * 0.55)
        dx, dy = math.cos(flow_angle), math.sin(flow_angle)
        half = 0.78 * math.hypot(bw, bh) * (1.0 + 0.10 * pulse)
        wash = QLinearGradient(cx - dx * half, cy - dy * half,
                               cx + dx * half, cy + dy * half)
        wash_alpha = 34 + int(28 * pulse)
        wash.setColorAt(0.00, clear)
        wash.setColorAt(0.24, _alpha(tone_a, int(wash_alpha * 0.45)))
        wash.setColorAt(0.48, _alpha(tone_b, wash_alpha))
        wash.setColorAt(0.72, _alpha(tone_c, int(wash_alpha * 0.60)))
        wash.setColorAt(1.00, clear)
        pp.fillRect(img.rect(), QBrush(wash))

        vignette = QRadialGradient(bw * 0.52, bh * 0.46, max_side * 0.86)
        vignette.setColorAt(0.00, clear)
        vignette.setColorAt(0.68, clear)
        vignette.setColorAt(1.00, _alpha(self._bg, 88 if dark else 70))
        pp.fillRect(img.rect(), QBrush(vignette))
        pp.end()
        return img

    def _paint_vec(self, p: QPainter, rect) -> None:
        """Stroke the frame's vector layer at window resolution."""
        vec = self._vec
        W, H = float(rect.width()), float(rect.height())
        unit = max(1.0, min(W, H) / 640.0)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(rect.left(), rect.top())
        if vec[0] == "contours":
            _, rw, rh, segs, majors, minor_c, major_c, pulse = vec
            if len(segs):
                sx, sy = W / rw, H / rh
                # +0.5: grid values sit at pixel centres of the field image
                # drawImage stretched over the rect.
                pts = (segs + 0.5) * np.array([sx, sy, sx, sy], np.float32)
                # Minor lines are hairlines (Qt's cheap 1px path); the
                # index lines scale with the window.
                for mask, col, width, alpha in (
                        (~majors, _lerp(minor_c, major_c, 0.25), 1.0,
                         0.75 + 0.25 * pulse),
                        (majors, major_c, 1.8 * unit, 0.85 + 0.15 * pulse)):
                    chosen = pts[mask]
                    if not len(chosen):
                        continue
                    pen = QPen(_alpha(col, int(255 * min(1.0, alpha))),
                               width)
                    # Flat, not round: round caps on ~2k short segments
                    # cost four times the rest of the paint.
                    pen.setCapStyle(Qt.FlatCap)
                    p.setPen(pen)
                    p.drawLines([QLineF(*row) for row in chosen.tolist()])
        else:
            _, cxu, cyu, label_r, qc, spin = vec
            cx, cy = cxu * W, cyu * H
            p.drawImage(0, 0, self._groove_layer(int(W), int(H), cx, cy,
                                                 label_r))
            lr = label_r * H
            self._ink_label = (cx, cy, lr, spin)
            cover = self._label_cover(int(round(2 * lr)))
            if cover is not None:
                p.save()
                p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                p.translate(cx, cy)
                p.rotate(spin)
                p.drawImage(QPointF(-cover.width() / 2.0,
                                    -cover.height() / 2.0), cover)
                p.restore()
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(_alpha(qc, 170), 1.5 * unit))
            else:
                # No cover (nothing playing, the fetch failed): a plain
                # label in the album's colour, with a crisp edge.
                p.setPen(Qt.NoPen)
                p.setBrush(_alpha(qc, 235))
            p.drawEllipse(QRectF(cx - lr, cy - lr, 2 * lr, 2 * lr))
        p.restore()

    def _groove_layer(self, W: int, H: int, cx: float, cy: float,
                      label_r: float) -> QImage:
        """The record's grooves at window resolution, drawn once per size
        and theme: they never move, only the light over them does, and
        restroking ~200k pixels of hairline every frame cost ~10 ms.

        Each groove is a hairline in the theme's own bg, a fine cut back
        to the background: on the lit wedge of the disc they read as
        grooves catching the light, on the dark part they all but
        vanish, which is what the sheen on a real record does."""
        key = (W, H, round(cx, 2), round(cy, 2), label_r, self._bg.rgb())
        if self._groove_key == key and self._groove_img is not None:
            return self._groove_img
        img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            cut = QColor(self._bg.rgb())
            pen = QPen(_alpha(cut, 130), 1.0)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            r_max = math.hypot(cx, cy) / H + 0.02
            r = label_r + 0.04
            step = 1.0 / _GROOVES_PER_UNIT
            while r < r_max:
                # The smooth gaps between songs stay bare.
                if abs(((r * 2.3) % 1.0) - 0.5) > 0.04:
                    rp = r * H
                    # Only the quarter facing the window: the centre sits
                    # past its bottom-right corner.
                    p.drawArc(QRectF(cx - rp, cy - rp, 2 * rp, 2 * rp),
                              90 * 16, 90 * 16)
                r += step
        finally:
            p.end()
        self._groove_key = key
        self._groove_img = img
        return img

    def _label_cover(self, diameter: int) -> QImage | None:
        """The current cover cut to a round label ``diameter`` px across,
        its rim shaded into the disc. Cut once per cover and size; each
        frame only rotates it."""
        art = self._art
        if art is None or art.isNull() or diameter < 4:
            return None
        key = (art.cacheKey(), diameter, self._bg.rgb())
        if self._label_key == key and self._label_img is not None:
            return self._label_img
        scaled = art.scaled(diameter, diameter, Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation)
        img = QImage(diameter, diameter, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            path.addEllipse(QRectF(0, 0, diameter, diameter))
            p.setClipPath(path)
            p.drawImage(QPointF((diameter - scaled.width()) / 2.0,
                                (diameter - scaled.height()) / 2.0), scaled)
            rim = QRadialGradient(diameter / 2.0, diameter / 2.0,
                                  diameter / 2.0)
            rim.setColorAt(0.0, _alpha(self._bg, 0))
            rim.setColorAt(0.78, _alpha(self._bg, 0))
            rim.setColorAt(1.0, _alpha(self._bg, 110))
            p.fillRect(QRectF(0, 0, diameter, diameter), QBrush(rim))
        finally:
            p.end()
        self._label_key = key
        self._label_img = img
        return img

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        try:
            rect = self.rect()
            # Round the corners only when the pixels outside the arc can
            # actually be transparent. On an opaque top-level window the
            # "corner" is the window's own flat QSS bg showing through — a
            # near-black bite that reads as a triangle against a bright
            # backdrop (the liquid style made it obvious). Painting square
            # to the edge is the correct degradation; @radius still rounds
            # inputs and scrollbars so the setting keeps meaning.
            if self._radius > 0 and self.window().testAttribute(
                    Qt.WA_TranslucentBackground):
                # Rounded clip so the fill stops inside the corners, leaving
                # the (transparent) window bg to show through them.
                path = QPainterPath()
                path.addRoundedRect(
                    float(rect.left()), float(rect.top()),
                    float(rect.width()), float(rect.height()),
                    float(self._radius), float(self._radius),
                )
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setClipPath(path)

            if self._enabled:
                size = (max(1, rect.width()), max(1, rect.height()))
                fresh = (self._frame_stale or self._frame is None
                         or self._frame_size != size)
                if fresh:
                    self._frame = self._render_buffer(*size)
                    self._frame_size = size
                    self._frame_stale = False
                img = self._frame
                p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                p.drawImage(rect, img)
                self._ink_label = None
                if self._vec is not None:
                    self._paint_vec(p, rect)
                if fresh:
                    self._ink_src = img
                    self._ink_frame += 1
            elif self._bg.alpha() == 255:
                self._ink_src = None
                p.fillRect(rect, self._bg)
            else:
                # Translucent theme: the styled window is the base; paint
                # nothing so its alpha shows through once, not twice.
                self._ink_src = None
        finally:
            p.end()

    # ---------- legibility probe ----------

    def ink_reference(self) -> float:
        """APCA luminance of the theme's own bg: what every ink was designed
        against, so how far a patch strays from it is what moves an ink."""
        return self._ink_ref

    def ink_version(self) -> int:
        """Moves whenever ink_range answers may have changed. Asking brings
        the grid up to date first, so a memo keyed on it is never stale."""
        self._ink_luma()
        return self._ink_version

    def ink_range(self, rect) -> tuple[float, float] | None:
        """Darkest and brightest luminance of the backdrop under ``rect``
        (a QRect in this widget's coordinates), or None when there is no
        backdrop to read: the fill is the theme's flat bg, which the token
        pass in contrast.py already covers."""
        grid = self._ink_luma()
        if grid is None:
            return None
        gh, gw = grid.shape
        W, H = max(1, self.width()), max(1, self.height())
        x0 = max(0, min(gw - 1, int(rect.left() * gw / W)))
        y0 = max(0, min(gh - 1, int(rect.top() * gh / H)))
        x1 = max(x0 + 1, min(gw, math.ceil((rect.right() + 1) * gw / W)))
        y1 = max(y0 + 1, min(gh, math.ceil((rect.bottom() + 1) * gh / H)))
        key = (x0, y0, x1, y1)
        span = self._ink_spans.get(key)
        if span is None:
            cell = grid[y0:y1, x0:x1]
            span = (float(cell.min()), float(cell.max()))
            self._ink_spans[key] = span
        return span

    def _ink_luma(self) -> np.ndarray | None:
        """The eased luminance grid, rebuilt at most once per painted frame
        and only when something asks (text paints right after us)."""
        if not self._enabled or self._ink_src is None:
            return None
        if self._ink_grid is not None and (
                self._ink_seen == self._ink_frame
                or time.monotonic() - (self._ink_t or 0.0) < _INK_REBUILD_S):
            return self._ink_grid
        src = self._ink_src
        sw, sh = max(1, src.width()), max(1, src.height())
        if sw >= sh:
            gw = min(sw, _INK_GRID)
            gh = max(1, round(gw * sh / sw))
        else:
            gh = min(sh, _INK_GRID)
            gw = max(1, round(gh * sw / sh))
        # Opaque RGB at grid size. A translucent theme's frame is glass
        # over the desktop; the theme bg under it is the best stand-in.
        small = QImage(gw, gh, QImage.Format_RGB888)
        small.fill(QColor(self._bg.red(), self._bg.green(), self._bg.blue()))
        sp = QPainter(small)
        sp.setRenderHint(QPainter.SmoothPixmapTransform, True)
        sp.drawImage(QRectF(0, 0, gw, gh), src)
        if self._ink_label is not None and self._art is not None:
            # The vinyl label: the cover, spinning, at grid scale.
            cx, cy, lr, spin = self._ink_label
            kx, ky = gw / max(1, self.width()), gh / max(1, self.height())
            path = QPainterPath()
            path.addEllipse(QPointF(cx * kx, cy * ky), lr * kx, lr * ky)
            sp.setClipPath(path)
            sp.translate(cx * kx, cy * ky)
            sp.rotate(spin)
            sp.drawImage(QRectF(-lr * kx, -lr * ky, 2 * lr * kx, 2 * lr * ky),
                         self._art)
        sp.end()
        stride = small.bytesPerLine()
        raw = bytes(small.constBits()[: stride * gh])
        rgb = (np.frombuffer(raw, dtype=np.uint8)
               .reshape(gh, stride)[:, : gw * 3].reshape(gh, gw, 3))
        lr_, lg_, lb_ = _INK_LUT
        y = lr_[rgb[..., 0]] + lg_[rgb[..., 1]] + lb_[rgb[..., 2]]

        now = time.monotonic()
        if (self._ink_grid is None or self._ink_t is None
                or self._ink_grid.shape != y.shape):
            self._ink_grid = y
        else:
            k = 1.0 - math.exp(-max(0.0, now - self._ink_t) / _INK_EASE_S)
            self._ink_grid = self._ink_grid + (y - self._ink_grid) * k
        self._ink_t = now
        self._ink_seen = self._ink_frame
        self._ink_version += 1
        self._ink_spans.clear()
        return self._ink_grid
