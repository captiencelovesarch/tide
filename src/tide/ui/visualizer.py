"""Audio visualizer view.

2.3: ``VisualizerView`` hosts the remastered ``vis_deck.DeckCanvas`` — a
live scene over the track's beat deck (see that module). The classic
``Renderer`` strategies below stay: they are the "classic · …" scenes,
the theme's own pick (``[layout].visualizer``: ``bars-mono`` |
``bars-filled`` | ``oscilloscope`` | ``neon-grid`` | …), and what the
mini player's art-flip canvas (``_Canvas``) draws.

Each strategy reads palette tokens at paint time so theme swaps repaint
correctly.
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import audio_capture, settings as settings_module, theming
from .widgets import BracketButton


def _color(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))


def _fill_backdrop(_p: QPainter, _rect: QRect, _color: QColor) -> None:
    # The app-level CentralBg owns the real backdrop. Visualizer renderers
    # paint marks on top of it instead of laying down an opaque black panel.
    return


# Physical-pixel cap for the render surface (long side). Above this the canvas
# renders into a smaller offscreen buffer and upscales — keeps raster cost flat
# regardless of window size / desktop scaling, which is what made the
# oscilloscope tank on a large / HiDPI window.
_RENDER_CAP = 1280
# How long the visualizer keeps its capture while hidden (see hideEvent).
_RELEASE_HIDDEN_MS = 20_000
# Watchdog (VisualizerView._check_capture): no bands for this long while
# playing = the capture is stuck; retries are spaced at least this far.
_STALL_S = 2.5
_RETRY_S = 3.0

# Repaint cap. The audio feed pushes ~43 frames/s; there's no need to repaint
# faster than this, and coalescing bursts halves the paint load.
_RENDER_INTERVAL_MS = 33


def _decimate(wave: np.ndarray, target: int) -> np.ndarray:
    """Down-sample a waveform to ``target`` points by striding (preserves the
    instantaneous excursions a scope wants, unlike block-averaging)."""
    n = wave.shape[0]
    if target >= n:
        return wave
    idx = (np.arange(target) * (n / target)).astype(np.intp)
    return wave[idx]


# ---------- renderer strategies ----------


class Renderer(ABC):
    slug: str = ""

    @abstractmethod
    def paint(self, p: QPainter, rect: QRect, theme,
              bands: np.ndarray | None, waveform: np.ndarray | None) -> None:
        ...


class BarsMonoRenderer(Renderer):
    """Text-block bars in monospace. Reads `▁▂▃▄▅▆▇█` ramp from cells."""

    slug = "bars-mono"
    RAMP = " ▁▂▃▄▅▆▇█"

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        dim = _color(theme, "dim", "#444")
        _fill_backdrop(p, rect, bg)
        if bands is None:
            return
        n = bands.shape[0]
        # Compute font size so the ramp fills the height.
        font = QFont(theme.t("typography", "family", "monospace") if theme else "monospace")
        font.setPointSizeF(max(8.0, rect.height() / 22.0))
        p.setFont(font)
        fm = QFontMetrics(font)
        cell_w = max(1, rect.width() // n)
        ramp_len = len(self.RAMP) - 1
        rows = max(8, rect.height() // fm.height())
        # We paint columns from bottom up.
        for i in range(n):
            mag = float(bands[i])
            filled_rows = int(round(mag * rows))
            x = rect.left() + i * cell_w + cell_w // 2
            base_y = rect.bottom() - 2
            for r in range(rows):
                cell_y = base_y - r * fm.height()
                if r < filled_rows:
                    # Use the top of the ramp to make the bar "full".
                    if r == filled_rows - 1:
                        # Top character uses the partial ramp slot for a smoother peak.
                        frac = mag * rows - filled_rows + 1
                        idx = max(1, min(ramp_len, int(round(frac * ramp_len))))
                    else:
                        idx = ramp_len
                    p.setPen(accent)
                    p.drawText(x - fm.horizontalAdvance(self.RAMP[idx]) // 2, cell_y,
                               self.RAMP[idx])
                else:
                    p.setPen(dim)
                    p.drawText(x - fm.horizontalAdvance("·") // 2, cell_y, "·")


class BarsFilledRenderer(Renderer):
    """Smooth filled rectangles. Reads accent + gradient toward fg."""

    slug = "bars-filled"

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        fg = _color(theme, "fg", "#e6e6e6")
        _fill_backdrop(p, rect, bg)
        if bands is None:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        n = bands.shape[0]
        gap = 2
        total_w = rect.width() - gap * (n + 1)
        bar_w = max(2, total_w // n)
        baseline = rect.bottom() - 4
        for i in range(n):
            mag = float(bands[i])
            h = int(mag * (rect.height() - 12))
            x = rect.left() + gap + i * (bar_w + gap)
            r = QRectF(x, baseline - h, bar_w, h)
            grad = QLinearGradient(r.topLeft(), r.bottomLeft())
            grad.setColorAt(0.0, fg)
            grad.setColorAt(1.0, accent)
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(r, 1.5, 1.5)


class OscilloscopeRenderer(Renderer):
    """Time-domain waveform line, accent-colored, soft glow."""

    slug = "oscilloscope"

    def paint(self, p, rect, theme, _bands, waveform):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        _fill_backdrop(p, rect, bg)
        if waveform is None or waveform.shape[0] < 2:
            return
        # Never build more path points than there are output pixels — bounds
        # both path construction and stroke-rasterization work. (Combined with
        # the canvas's capped render surface, ``rect`` is itself bounded.)
        target = int(max(64, min(waveform.shape[0], rect.width() // 2)))
        wave = _decimate(waveform, target)
        n = wave.shape[0]
        p.setRenderHint(QPainter.Antialiasing, True)
        cx = rect.left()
        cy = rect.top() + rect.height() / 2.0
        scale = (rect.height() / 2) * 0.85
        step = rect.width() / float(n - 1)
        path = QPainterPath()
        path.moveTo(cx, cy - float(wave[0]) * scale)
        for i in range(1, n):
            path.lineTo(cx + i * step, cy - float(wave[i]) * scale)
        # One soft halo + one crisp line (was two full-width strokes over the
        # raw 1024-point path — the halo pen is now thinner, too).
        halo = QColor(accent)
        halo.setAlpha(60)
        p.setPen(QPen(halo, 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.setPen(QPen(accent, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)


class EnvelopeRenderer(Renderer):
    """Filled min/max waveform envelope. Cost is O(columns) regardless of the
    sample count or window size, so it stays cheap at any scale."""

    slug = "waveform-envelope"
    COLS = 256

    def paint(self, p, rect, theme, _bands, waveform):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        fg = _color(theme, "fg", "#e6e6e6")
        _fill_backdrop(p, rect, bg)
        if waveform is None or waveform.shape[0] < 2:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        cols = min(self.COLS, waveform.shape[0])
        k = max(1, waveform.shape[0] // cols)
        cols = waveform.shape[0] // k
        # Vectorized per-column min/max over contiguous sample groups.
        block = waveform[: cols * k].reshape(cols, k)
        maxs = block.max(axis=1)
        mins = block.min(axis=1)
        cy = rect.top() + rect.height() / 2.0
        scale = (rect.height() / 2) * 0.9
        col_w = rect.width() / float(cols)
        path = QPainterPath()
        path.moveTo(rect.left(), cy - float(maxs[0]) * scale)
        for i in range(1, cols):
            path.lineTo(rect.left() + i * col_w, cy - float(maxs[i]) * scale)
        for i in range(cols - 1, -1, -1):
            path.lineTo(rect.left() + i * col_w, cy - float(mins[i]) * scale)
        path.closeSubpath()
        grad = QLinearGradient(QPointF(rect.left(), rect.top()),
                               QPointF(rect.left(), rect.bottom()))
        grad.setColorAt(0.0, fg)
        grad.setColorAt(0.5, accent)
        grad.setColorAt(1.0, fg)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPath(path)


class CircleBurstRenderer(Renderer):
    """Radial spectrum bursting from the center. Bars rotate around 360°."""

    slug = "circle-burst"

    def paint(self, p, rect, theme, bands, _wave):
        import math
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        fg = _color(theme, "fg", "#e6e6e6")
        dim = _color(theme, "dim", "#444")
        _fill_backdrop(p, rect, bg)
        if bands is None:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        cx = rect.left() + rect.width() // 2
        cy = rect.top() + rect.height() // 2
        radius_inner = min(rect.width(), rect.height()) // 8
        radius_max = min(rect.width(), rect.height()) // 2 - 8
        n = bands.shape[0]
        # Mirror left/right of center for a smoother circle.
        full = np.concatenate([bands, bands[::-1]])
        m = full.shape[0]
        # Inner ring.
        p.setPen(QPen(dim, 1.0))
        p.drawEllipse(QPointF(cx, cy), radius_inner, radius_inner)
        for i in range(m):
            angle = (i / m) * 2 * math.pi - math.pi / 2
            mag = float(full[i])
            length = radius_inner + mag * (radius_max - radius_inner)
            x1 = cx + math.cos(angle) * radius_inner
            y1 = cy + math.sin(angle) * radius_inner
            x2 = cx + math.cos(angle) * length
            y2 = cy + math.sin(angle) * length
            grad = QLinearGradient(QPointF(x1, y1), QPointF(x2, y2))
            grad.setColorAt(0.0, fg)
            grad.setColorAt(1.0, accent)
            pen = QPen(accent, 2.4, Qt.SolidLine, Qt.RoundCap)
            pen.setBrush(grad)
            p.setPen(pen)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


class MirrorBarsRenderer(Renderer):
    """Symmetric bars from a center horizontal line. Like a VU spectrum."""

    slug = "mirror-bars"

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        dim = _color(theme, "dim", "#444")
        _fill_backdrop(p, rect, bg)
        if bands is None:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        n = bands.shape[0]
        cy = rect.top() + rect.height() // 2
        gap = 2
        bar_w = max(2, (rect.width() - gap * (n + 1)) // n)
        half_h = (rect.height() - 12) // 2
        # Faint center line.
        pen = QPen(dim, 1.0)
        p.setPen(pen)
        p.drawLine(rect.left() + 4, cy, rect.right() - 4, cy)
        for i in range(n):
            mag = float(bands[i])
            h = int(mag * half_h)
            x = rect.left() + gap + i * (bar_w + gap)
            top = QRectF(x, cy - h, bar_w, h)
            bot = QRectF(x, cy, bar_w, h)
            grad_top = QLinearGradient(top.topLeft(), top.bottomLeft())
            grad_top.setColorAt(0.0, accent)
            grad_top.setColorAt(1.0, accent.lighter(130))
            grad_bot = QLinearGradient(bot.topLeft(), bot.bottomLeft())
            grad_bot.setColorAt(0.0, accent.lighter(130))
            grad_bot.setColorAt(1.0, accent)
            p.setPen(Qt.NoPen)
            p.setBrush(grad_top)
            p.drawRoundedRect(top, 1.0, 1.0)
            p.setBrush(grad_bot)
            p.drawRoundedRect(bot, 1.0, 1.0)


class DotMatrixRenderer(Renderer):
    """Pixelated dot grid that lights up reactively. Pure brutalist."""

    slug = "dot-matrix"

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        dim = _color(theme, "dim", "#222")
        _fill_backdrop(p, rect, bg)
        if bands is None:
            return
        cols = bands.shape[0]
        rows = 16
        cell_w = rect.width() // cols
        cell_h = rect.height() // rows
        radius = min(cell_w, cell_h) // 3
        radius = max(2, radius)
        for c in range(cols):
            mag = float(bands[c])
            on_rows = int(round(mag * rows))
            cx = rect.left() + c * cell_w + cell_w // 2
            for r in range(rows):
                cy = rect.bottom() - r * cell_h - cell_h // 2
                if r < on_rows:
                    p.setBrush(accent)
                    p.setPen(Qt.NoPen)
                else:
                    p.setBrush(dim)
                    p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(cx, cy), radius, radius)


class StarfieldRenderer(Renderer):
    """Reactive starfield — density follows bass, color drifts with accent."""

    slug = "starfield"

    _seed_state = None

    def __init__(self) -> None:
        # Seeded RNG so the field stays coherent frame-to-frame rather than
        # flickering. We advance positions each frame using a smooth update.
        import numpy as np
        self._rng = np.random.default_rng(seed=2026)
        self._stars = self._rng.uniform(low=[-1.0, -1.0, 0.2], high=[1.0, 1.0, 1.0], size=(180, 3))
        self._last_t = time.monotonic()

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#0b0b0b")
        accent = _color(theme, "accent", "#d4b95e")
        fg = _color(theme, "fg", "#e6e6e6")
        _fill_backdrop(p, rect, bg)
        if bands is None:
            return
        import numpy as np
        # Bass-driven speed: low bands move stars toward camera faster.
        bass = float(bands[:5].mean()) if bands.shape[0] >= 5 else 0.0
        treble = float(bands[-8:].mean()) if bands.shape[0] >= 8 else 0.0
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._last_t))
        self._last_t = now
        speed = 0.35 + bass * 2.0
        self._stars[:, 2] -= speed * dt
        # Respawn stars that went past camera.
        passed = self._stars[:, 2] <= 0.0
        if np.any(passed):
            cnt = int(passed.sum())
            self._stars[passed, 0:2] = self._rng.uniform(-1.0, 1.0, size=(cnt, 2))
            self._stars[passed, 2] = 1.0
        cx = rect.left() + rect.width() / 2.0
        cy = rect.top() + rect.height() / 2.0
        half_w = rect.width() / 2.0
        half_h = rect.height() / 2.0
        p.setRenderHint(QPainter.Antialiasing, True)
        for x, y, z in self._stars:
            # Perspective project.
            sx = cx + (x / z) * half_w
            sy = cy + (y / z) * half_h
            if not (rect.left() <= sx <= rect.right() and rect.top() <= sy <= rect.bottom()):
                continue
            size = max(1.0, (1.0 - z) * 4.0 + treble * 3.0)
            # Tint between fg (far) and accent (near).
            t = float(np.clip(1.0 - z, 0.0, 1.0))
            col = QColor(
                int(fg.red()   * (1 - t) + accent.red()   * t),
                int(fg.green() * (1 - t) + accent.green() * t),
                int(fg.blue()  * (1 - t) + accent.blue()  * t),
            )
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(sx, sy), size, size)


class MatrixRainRenderer(Renderer):
    """Cascading character columns. Speed + brightness react to spectrum."""

    slug = "matrix-rain"
    CHARS = "01アイウエオカキクケコサシスセソタチツテトナニヌネノ╳━┃░▒▓"

    def __init__(self) -> None:
        self._columns: list[list[float]] = []   # per-column y positions of trails
        self._last_w = -1
        self._last_t = time.monotonic()
        import numpy as np
        self._rng = np.random.default_rng(seed=42)

    def _ensure_columns(self, width: int) -> None:
        col_w = 14
        n = max(1, width // col_w)
        if n == self._last_w:
            return
        self._last_w = n
        self._columns = [[float(self._rng.uniform(0, 1)) for _ in range(3)] for _ in range(n)]

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#000000")
        accent = _color(theme, "accent", "#00ff66")
        fg = _color(theme, "fg", "#00cc55")
        dim = _color(theme, "dim", "#007733")
        # Trail fade: paint a translucent bg rect over the previous frame for
        # the persistence effect.
        p.fillRect(rect, QColor(bg.red(), bg.green(), bg.blue(), 28))
        if bands is None:
            return
        font = QFont(theme.t("typography", "family", "monospace") if theme else "monospace")
        font.setPointSizeF(11.0)
        p.setFont(font)
        fm = QFontMetrics(font)
        col_w = 14
        self._ensure_columns(rect.width())
        n_cols = len(self._columns)
        # Bass drives the speed; treble drives the per-column brightness.
        bass = float(bands[:5].mean()) if bands.shape[0] >= 5 else 0.0
        treble = float(bands[-10:].mean()) if bands.shape[0] >= 10 else 0.0
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._last_t))
        self._last_t = now
        speed = 60 + 280 * bass
        for ci, trails in enumerate(self._columns):
            cx = rect.left() + ci * col_w + col_w // 2
            for ti in range(len(trails)):
                trails[ti] += speed * dt
                if trails[ti] > rect.bottom() + 40:
                    trails[ti] = rect.top() - float(self._rng.integers(40, 200))
                head_y = int(trails[ti])
                # Draw trail of N chars upward, fading.
                for k in range(0, 16):
                    y = head_y - k * fm.height()
                    if y < rect.top() or y > rect.bottom():
                        continue
                    ch = self.CHARS[int(self._rng.integers(0, len(self.CHARS)))]
                    if k == 0:
                        col = QColor(accent)
                        col.setAlpha(int(180 + 75 * treble))
                    else:
                        col = QColor(fg)
                        col.setAlpha(max(20, 255 - k * 18))
                    p.setPen(col)
                    p.drawText(cx - fm.horizontalAdvance(ch) // 2, y, ch)


class NeonGridRenderer(Renderer):
    """Synthwave-style perspective grid + reactive spectrum bars."""

    slug = "neon-grid"

    def paint(self, p, rect, theme, bands, _wave):
        bg = _color(theme, "bg", "#1a103d")
        accent = _color(theme, "accent", "#ff5dff")
        accent_alt = _color(theme, "accent_alt", "#5dfdff")
        fg = _color(theme, "fg", "#f0e8ff")
        _fill_backdrop(p, rect, bg)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Horizon at ~55% down — gives the grid the right perspective feel.
        horizon_y = rect.top() + int(rect.height() * 0.55)
        center_x = rect.left() + rect.width() // 2

        # Vertical grid lines converging on the horizon.
        v_lines = 14
        p.setPen(QPen(accent_alt, 1.2))
        for i in range(-v_lines, v_lines + 1):
            x = center_x + i * (rect.width() // (v_lines + 2))
            p.drawLine(x, rect.bottom(), center_x, horizon_y)

        # Horizontal lines spaced with exponential gap toward the horizon.
        for i in range(1, 11):
            t = i / 10.0
            y = horizon_y + int((rect.bottom() - horizon_y) * (t ** 1.8))
            pen = QPen(accent_alt if i % 2 == 0 else accent, 1.0)
            pen.setColor(QColor(accent_alt))
            p.setPen(pen)
            p.drawLine(rect.left(), y, rect.right(), y)

        # Sun above the horizon.
        sun_r = min(rect.width(), rect.height()) // 8
        sun_cx = center_x
        sun_cy = horizon_y - sun_r - 4
        grad = QLinearGradient(sun_cx, sun_cy - sun_r, sun_cx, sun_cy + sun_r)
        grad.setColorAt(0.0, QColor(accent_alt))
        grad.setColorAt(1.0, QColor(accent))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(QPointF(sun_cx, sun_cy), sun_r, sun_r)
        # Sun stripes — slice the disc with the bg color.
        p.setBrush(bg)
        stripes = 4
        stripe_h = max(2, sun_r // 4)
        for i in range(stripes):
            y = sun_cy + i * stripe_h + 1
            if y >= sun_cy + sun_r - 2:
                break
            p.drawRect(QRectF(sun_cx - sun_r, y, 2 * sun_r, stripe_h * 0.5))

        if bands is None:
            return
        # Reactive spectrum bars rise from the bottom of the screen along
        # the floor of the grid, so the sun stays clean above the horizon.
        n = bands.shape[0]
        gap = 2
        bar_w = max(2, (rect.width() - gap * (n + 1)) // n)
        baseline = rect.bottom() - 4
        max_h = max(24, baseline - horizon_y - 6)
        for i in range(n):
            mag = float(bands[i])
            h = int(mag * max_h)
            x = rect.left() + gap + i * (bar_w + gap)
            r = QRectF(x, baseline - h, bar_w, h)
            grad = QLinearGradient(r.topLeft(), r.bottomLeft())
            grad.setColorAt(0.0, accent_alt)
            grad.setColorAt(1.0, accent)
            p.setBrush(grad)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 1.5, 1.5)


# Registry. Used by the view to look up renderers by slug.
_RENDERERS: dict[str, Renderer] = {
    BarsMonoRenderer.slug: BarsMonoRenderer(),
    BarsFilledRenderer.slug: BarsFilledRenderer(),
    OscilloscopeRenderer.slug: OscilloscopeRenderer(),
    EnvelopeRenderer.slug: EnvelopeRenderer(),
    NeonGridRenderer.slug: NeonGridRenderer(),
    CircleBurstRenderer.slug: CircleBurstRenderer(),
    MirrorBarsRenderer.slug: MirrorBarsRenderer(),
    DotMatrixRenderer.slug: DotMatrixRenderer(),
    StarfieldRenderer.slug: StarfieldRenderer(),
    MatrixRainRenderer.slug: MatrixRainRenderer(),
}


def renderer_slugs() -> list[str]:
    return list(_RENDERERS.keys())


def get_renderer(slug: str) -> Renderer | None:
    return _RENDERERS.get(slug)


def renderer_for_theme(theme) -> Renderer:
    if theme is None:
        return _RENDERERS[BarsFilledRenderer.slug]
    slug = str(theme.t("layout", "visualizer", ""))
    if slug and slug in _RENDERERS:
        return _RENDERERS[slug]
    # fallback: mono fonts -> bars-mono, else bars-filled
    if bool(theme.t("typography", "mono", False)):
        return _RENDERERS[BarsMonoRenderer.slug]
    return _RENDERERS[BarsFilledRenderer.slug]


# ---------- the view ----------


class _Canvas(QWidget):
    """Inner widget that does the actual painting. Separated so the parent
    view can host a header bar above it without interfering with paint."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theming.manager().current()
        self._override_slug: str | None = None
        self._renderer = renderer_for_theme(self._theme)
        self._bands: np.ndarray | None = None
        self._waveform: np.ndarray | None = None
        self._dirty = False
        self._buf: QImage | None = None
        # Repaints are driven by this timer, not by every audio callback, so
        # the ~43 fps feed is coalesced to a bounded repaint rate.
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._on_render_tick)
        theming.manager().theme_changed.connect(self._on_theme)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._render_timer.isActive():
            self._render_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._render_timer.stop()

    def _on_render_tick(self) -> None:
        if self._dirty and self.isVisible():
            self._dirty = False
            self.update()

    def _on_theme(self, theme) -> None:
        self._theme = theme
        if self._override_slug is None:
            self._renderer = renderer_for_theme(theme)
        self.update()

    def set_renderer_override(self, slug: str | None) -> None:
        self._override_slug = slug
        if slug is None:
            self._renderer = renderer_for_theme(self._theme)
        else:
            r = get_renderer(slug)
            if r is not None:
                self._renderer = r
        self.update()

    def current_renderer_slug(self) -> str:
        return self._renderer.slug

    def update_bands(self, bands: np.ndarray) -> None:
        self._bands = bands
        self._dirty = True

    def update_waveform(self, wave: np.ndarray) -> None:
        self._waveform = wave
        self._dirty = True

    def paintEvent(self, _ev) -> None:
        rect = self.rect()
        dpr = self.devicePixelRatioF()
        long_px = max(rect.width(), rect.height()) * dpr
        if long_px > _RENDER_CAP:
            # Render into a capped offscreen buffer, then upscale-blit. Bounds
            # the renderer's working resolution on large / HiDPI surfaces.
            s = _RENDER_CAP / long_px
            bw = max(1, int(round(rect.width() * dpr * s)))
            bh = max(1, int(round(rect.height() * dpr * s)))
            if self._buf is None or self._buf.width() != bw or self._buf.height() != bh:
                self._buf = QImage(bw, bh, QImage.Format_ARGB32_Premultiplied)
            self._buf.fill(Qt.transparent)
            bp = QPainter(self._buf)
            bp.setRenderHint(QPainter.TextAntialiasing, True)
            self._renderer.paint(bp, QRect(0, 0, bw, bh), self._theme,
                                 self._bands, self._waveform)
            bp.end()
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(rect, self._buf)
            p.end()
        else:
            p = QPainter(self)
            p.setRenderHint(QPainter.TextAntialiasing, True)
            self._renderer.paint(p, rect, self._theme, self._bands, self._waveform)
            p.end()


class VisualizerView(QWidget):
    """the visualizer tab: a header and up to four tiles (vis_play.TileGrid)
    driven by one engine (vis_deck.VisEngine). owns the "visualizer"
    consumer on the shared capture and, while on screen, watches the
    ambient controller's beat clock (which keeps the track's beat map
    alive even when the backdrop pulse is off)."""

    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .headings import Heading
        from .vis_deck import VisEngine
        from .vis_play import TileGrid
        self._feed = audio_capture.feed()
        self._feed.bands_updated.connect(self._on_bands)
        self._feed.waveform_updated.connect(self._on_waveform)
        self._feed.pulse_updated.connect(self._on_pulse)
        self._feed.error.connect(self._on_error)
        # Local view-capturing flag. The feed is refcounted and may also be
        # held by the ambient bass-pulse, so ``feed.running`` no longer maps
        # 1:1 to "this view is displaying" — track our own membership.
        self._capturing = False
        self._watching = False
        self._playing = False
        self._last_bands_at = time.monotonic()
        self._last_retry = -math.inf
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_capture)
        self._player = None
        self._queue = None
        self._api_provider = None
        self._settings_provider = None
        self._ambient_provider = None
        self._fallback_settings = None
        self._audio_source_override: str | None = None
        # a plugin's tile came or went: lay the grid out again
        from . import vis_play
        vis_play.registry_signal.changed.connect(self._on_tiles_changed)

        self.engine = VisEngine(self)
        self.engine.set_clock_provider(self._beat_clock)
        self.engine.seek_requested.connect(self._on_seek)
        self.engine.fullscreen_requested.connect(self._toggle_fullscreen)
        self._grid = TileGrid(self.engine, api_provider=self._api)
        self._grid.menu_requested.connect(self._on_tile_menu)
        self._grid.setFocusPolicy(Qt.ClickFocus)

        self._heading = Heading("visualizer")
        self._heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._style_btn = BracketButton("style")
        self._style_btn.setToolTip("layout, tiles, look and audio source. "
                                   "hover a tile and hit [edit] for its own knobs")
        self._style_btn.clicked.connect(self._open_style_menu)
        self._fullscreen_btn = BracketButton("fullscreen")
        self._fullscreen_btn.clicked.connect(self._toggle_fullscreen)

        from . import scale as _scale
        self._header = QWidget(self)
        bar = QHBoxLayout(self._header)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(_scale.px(6))
        bar.addWidget(self._heading, stretch=1)
        bar.addWidget(self._style_btn)
        bar.addWidget(self._fullscreen_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(8))
        root.addWidget(self._header)
        root.addWidget(self._grid, stretch=1)

        self._fullscreen = False
        self._was_geometry = None
        # The first start() uses the saved device; apply_settings keeps it
        # current once the window attaches its live Settings.
        self.apply_settings(self._settings())

    # ---------- wiring ----------

    def attach(self, *, player=None, queue=None, settings=None, ambient=None,
               api=None) -> None:
        """hand over the player (position, seek, state), the queue (now
        playing), the live Settings provider, the ambient-controller
        provider (the beat clock) and the api provider (lyrics). each is
        optional: a bare view still draws the live capture."""
        self._player = player
        self._queue = queue
        self._settings_provider = settings
        self._ambient_provider = ambient
        self._api_provider = api
        if player is not None:
            player.state_changed.connect(self._on_state)
            player.position_changed.connect(self.engine.set_position)
            player.duration_changed.connect(self.engine.set_duration)
            self._on_state(player.state)
        if queue is not None:
            queue.current_changed.connect(self._on_track)
            self._on_track(getattr(queue, "current", None))
        if api is not None:
            # lyrics tiles built before the api arrived get rebuilt
            self._grid.teardown()
        self.apply_settings(self._settings())

    def _settings(self):
        s = self._settings_provider() if self._settings_provider is not None else None
        if s is None:
            if self._fallback_settings is None:
                try:
                    self._fallback_settings = settings_module.load()
                except Exception:
                    self._fallback_settings = settings_module.Settings()
            s = self._fallback_settings
        return s

    def _ambient(self):
        try:
            return self._ambient_provider() if self._ambient_provider is not None else None
        except Exception:
            return None

    def _api(self):
        try:
            return self._api_provider() if self._api_provider is not None else None
        except Exception:
            return None

    def _beat_clock(self):
        ambient = self._ambient()
        return ambient.beat_clock() if ambient is not None else None

    def tiles(self) -> list:
        return list(self._grid.tiles)

    def apply_settings(self, s) -> None:
        if s is None:
            return
        self._audio_source_override = s.audio_device or None
        self.engine.apply_settings(s)
        from .vis_play import SLOT_KEYS
        self._grid.configure(s.visualizer_layout,
                             [str(getattr(s, k, "spectrum")) for k in SLOT_KEYS])
        self._reconcile_watch()

    def _on_tiles_changed(self) -> None:
        try:
            self.apply_settings(self._settings())
        except RuntimeError:
            pass        # the view is going

    def _set_setting(self, name: str, value) -> None:
        """the menus' write path: the live Settings, field-scoped save (a
        whole-object save would revert other savers), re-apply."""
        s = self._settings()
        setattr(s, name, value)
        try:
            settings_module.save_fields(s, name)
        except Exception:
            pass
        self.apply_settings(s)

    # ---------- menus ----------

    def _radio(self, sub: QMenu, rows, current, name: str) -> None:
        group = QActionGroup(sub)
        group.setExclusive(True)
        for value, label in rows:
            a = QAction(theming.styled_case(str(label)), sub, checkable=True)
            a.setChecked(value == current)
            a.triggered.connect(lambda _=False, v=value: self._set_setting(name, v))
            group.addAction(a)
            sub.addAction(a)

    def _toggle(self, menu: QMenu, label: str, name: str) -> None:
        a = QAction(theming.styled_case(label), menu, checkable=True)
        a.setChecked(bool(getattr(self._settings(), name)))
        a.toggled.connect(lambda on: self._set_setting(name, bool(on)))
        menu.addAction(a)

    def _knob_menu(self, menu: QMenu, keys) -> None:
        """one submenu per knob, straight from the settings descriptors."""
        from . import settings_schema
        s = self._settings()
        for key in keys:
            desc = settings_schema.by_key().get(key)
            if desc is None or not isinstance(desc.choices, tuple):
                continue
            label = desc.label.split("· ", 1)[-1]
            sub = menu.addMenu(theming.styled_case(label))
            self._radio(sub, desc.choices, getattr(s, key), key)

    def _plugin_knob_menu(self, menu: QMenu, entry) -> None:
        for key in entry.knobs:
            try:
                desc = entry.settings.desc(key)
            except KeyError:
                continue
            if not isinstance(desc.choices, tuple):
                continue
            sub = menu.addMenu(theming.styled_case(desc.label))
            group = QActionGroup(sub)
            group.setExclusive(True)
            for value, label in desc.choices:
                a = QAction(theming.styled_case(str(label)), sub, checkable=True)
                a.setChecked(value == entry.settings[key])
                a.triggered.connect(lambda _=False, k=key, v=value: self._set_plugin_knob(entry, k, v))
                group.addAction(a)
                sub.addAction(a)

    def _set_plugin_knob(self, entry, key: str, value) -> None:
        entry.set_knob(key, value)
        for tile in self._grid.tiles:
            if tile.kind == entry.kind:
                tile.settings_changed()
        self.engine.wake()

    def _slot_menu(self, menu: QMenu, slot: int) -> None:
        """what slot ``slot`` shows, and that tile's knobs."""
        from .vis_play import SLOT_KEYS, plugin_tile, tile_choices
        s = self._settings()
        key = SLOT_KEYS[slot]
        kmenu = menu.addMenu(theming.styled_case("show"))
        self._radio(kmenu, tile_choices(), getattr(s, key), key)
        tile = self._grid.tiles[slot] if slot < len(self._grid.tiles) else None
        entry = plugin_tile(tile.kind) if tile is not None else None
        if entry is not None:
            # a plugin's tile: its knobs are the plugin's settings, and it
            # may bring actions of its own (the tapper's reset score)
            if entry.knobs:
                menu.addSeparator()
                self._plugin_knob_menu(menu, entry)
            extra = getattr(tile, "menu_actions", None)
            for label, fn in (extra() if callable(extra) else ()):
                a = QAction(theming.styled_case(label), menu)
                a.triggered.connect(lambda _=False, f=fn: f())
                menu.addAction(a)
            return
        knobs = getattr(tile, "KNOBS", ())
        if tile is not None and tile.kind == "deck":
            knobs = ("visualizer_deck_span",)
        elif tile is not None and tile.kind == "classic":
            knobs = ("visualizer_classic",)
        elif tile is not None and tile.kind in ("spectrum", "mirror"):
            knobs = ("visualizer_bars",)
        if knobs:
            menu.addSeparator()
            self._knob_menu(menu, knobs)

    def _build_style_menu(self) -> QMenu:
        from . import settings_schema
        from .vis_play import LAYOUT_LABELS, SLOT_NAMES, LAYOUTS
        s = self._settings()
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)

        lmenu = menu.addMenu(theming.styled_case("layout"))
        self._radio(lmenu, LAYOUT_LABELS, s.visualizer_layout, "visualizer_layout")
        tmenu = menu.addMenu(theming.styled_case("tiles"))
        layout = s.visualizer_layout if s.visualizer_layout in LAYOUTS else "stack"
        for slot, name in enumerate(SLOT_NAMES[layout]):
            self._slot_menu(tmenu.addMenu(theming.styled_case(name)), slot)
        menu.addSeparator()
        look = menu.addMenu(theming.styled_case("look"))
        self._radio(look, (("auto", "auto"), ("blocks", "blocks"), ("glow", "glow")),
                    s.visualizer_look, "visualizer_look")
        self._knob_menu(menu, ("visualizer_flash",))
        self._toggle(menu, "strobe trails", "visualizer_trails")
        self._toggle(menu, "outline ripple", "visualizer_ripple")
        self._toggle(menu, "title over the scene", "visualizer_caption")
        menu.addSeparator()
        self._knob_menu(menu, ("visualizer_bars", "visualizer_deck_span",
                               "visualizer_classic", "visualizer_fps"))
        menu.addSeparator()

        # Audio device submenu
        amenu = menu.addMenu(theming.styled_case("audio source"))
        a_group = QActionGroup(amenu)
        a_group.setExclusive(True)
        a_auto = QAction("auto (tide's audio only)", amenu, checkable=True)
        a_auto.setChecked(self._audio_source_override is None)
        a_auto.triggered.connect(lambda: self._set_audio_source(None))
        a_group.addAction(a_auto)
        amenu.addAction(a_auto)
        amenu.addSeparator()
        sources = audio_capture.list_monitor_sources()
        if sources:
            for name, label in sources:
                a = QAction(label, amenu, checkable=True)
                a.setChecked(self._audio_source_override == name)
                a.triggered.connect(lambda _=False, n=name: self._set_audio_source(n))
                a_group.addAction(a)
                amenu.addAction(a)
        else:
            a = QAction("(no monitor sources found — check pactl)", amenu)
            a.setEnabled(False)
            amenu.addAction(a)
        return menu

    def _build_tile_menu(self, slot: int) -> QMenu:
        from .vis_play import LAYOUT_LABELS
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        self._slot_menu(menu, slot)
        menu.addSeparator()
        lmenu = menu.addMenu(theming.styled_case("layout"))
        self._radio(lmenu, LAYOUT_LABELS, self._settings().visualizer_layout,
                    "visualizer_layout")
        return menu

    def _open_style_menu(self) -> None:
        # Rebuilt per open so the device list re-enumerates fresh. Deferred
        # a tick: a modal exec straight from a click handler can crash on
        # PySide6 + Python 3.14.
        QTimer.singleShot(0, self, self._show_style_menu)

    def _show_style_menu(self) -> None:
        menu = self._build_style_menu()
        menu.popup(self._style_btn.mapToGlobal(self._style_btn.rect().bottomLeft()))

    def _on_tile_menu(self, slot: int, global_pos) -> None:
        QTimer.singleShot(0, self, lambda: self._build_tile_menu(slot).popup(global_pos))

    def _set_audio_source(self, name: str | None) -> None:
        self._audio_source_override = name
        s = self._settings()
        if (s.audio_device or None) != name:
            s.audio_device = name or ""
            try:
                settings_module.save_fields(s, "audio_device")
            except Exception:
                pass
        # Restart the shared capture on the new device if it's live (this
        # keeps all consumers, e.g. the ambient pulse, attached).
        if self._feed.running:
            self._feed.set_source(name or None)

    # ---------- visibility-driven start/stop ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._capturing:
            self._start_capture()
        self._reconcile_watch()
        self._last_bands_at = time.monotonic()
        self._watchdog.start()
        self.engine.set_active(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if getattr(self, "engine", None) is None:
            return      # Qt hides children while tearing the wrapper down
        self._watchdog.stop()
        self.engine.set_active(False)
        # Don't tear down audio the moment the visualizer goes behind another
        # view: flicking back to it stays instant. Left hidden for a while,
        # it lets go, so leaving the page doesn't keep the spectrum FFT (and
        # parec, when nothing else holds it) running for the whole session.
        # showEvent picks capture back up.
        QTimer.singleShot(_RELEASE_HIDDEN_MS, self, self._release_if_hidden)

    def _release_if_hidden(self) -> None:
        if self.isVisible():
            return
        if self._capturing:
            self._feed.remove_consumer("visualizer")
            self._capturing = False
        self._reconcile_watch()

    def _reconcile_watch(self) -> None:
        """watch the beat clock while on screen (the deck, the kicks, the
        trails, the ripple and the play tiles all ride the map). let go
        when hidden past the release delay."""
        want = self.isVisible() or (self._watching and self._capturing)
        ambient = self._ambient()
        if ambient is None or not hasattr(ambient, "set_watching"):
            self._watching = False
            return
        if want != self._watching:
            ambient.set_watching("visualizer", want)
            self._watching = want

    def teardown(self) -> None:
        self._feed.remove_consumer("visualizer")
        self._capturing = False
        self._watchdog.stop()
        self.engine.set_active(False)
        ambient = self._ambient()
        if ambient is not None and self._watching and hasattr(ambient, "set_watching"):
            ambient.set_watching("visualizer", False)
        self._watching = False

    # ---------- capture ----------

    def _start_capture(self, quiet: bool = False) -> None:
        ok = self._feed.add_consumer("visualizer", source=self._audio_source_override)
        # registered either way: if this start failed, the next one (ours
        # from the watchdog, or the ambient pulse's) runs the spectrum for us
        self._capturing = True
        self._last_retry = time.monotonic()
        if quiet:
            return
        if ok:
            self.status_message.emit(theming.styled_case(f"visualizer · capturing from {self._feed.device}"))
        else:
            self.status_message.emit(theming.styled_case("visualizer · no audio device"))

    def _check_capture(self) -> None:
        """the watchdog (1 Hz while on screen and playing). the shared
        capture can stop under us: its worker gives up after parec keeps
        exiting, or it sits on a stream tide has left (a song change can
        leave the old one corked, which blocks parec without an EOF). the
        scenes then froze until the tab was re-opened. restart a stopped
        feed; retarget one that's gone quiet."""
        if not self.isVisible() or not self._playing:
            self._last_bands_at = time.monotonic()
            return
        now = time.monotonic()
        if not self._feed.running:
            if now - self._last_retry >= _RETRY_S:
                self._start_capture(quiet=True)
                self._last_bands_at = now
            return
        if not self._capturing:
            self._start_capture(quiet=True)
            return
        if now - self._last_bands_at >= _STALL_S and now - self._last_retry >= _RETRY_S:
            self._last_retry = now
            self._last_bands_at = now
            try:
                self._feed.retarget()
            except Exception:
                pass

    # ---------- actions ----------

    def _on_seek(self, seconds: float) -> None:
        if self._player is not None:
            try:
                self._player.seek(float(seconds))
            except Exception:
                pass

    def _toggle_fullscreen(self) -> None:
        if not self._fullscreen:
            self._was_geometry = self.window().saveGeometry()
            self.window().showFullScreen()
            self._fullscreen = True
            self._fullscreen_btn.setLabel("exit fullscreen")
            # lean back: the tiles get the whole view; esc or a double-click
            # brings the header back (focus on the grid so esc reaches
            # keyPressEvent wherever focus was before)
            self._header.hide()
            self._grid.setFocus(Qt.OtherFocusReason)
        else:
            self.window().showNormal()
            try:
                if self._was_geometry is not None:
                    self.window().restoreGeometry(self._was_geometry)
            except Exception:
                pass
            self._fullscreen = False
            self._fullscreen_btn.setLabel("fullscreen")
            self._header.show()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and self._fullscreen:
            self._toggle_fullscreen()
            return
        super().keyPressEvent(event)

    # ---------- player / queue ----------

    def _on_state(self, state) -> None:
        from ..player import PlayState
        playing = state == PlayState.PLAYING
        if playing and not self._playing:
            # a fresh start (or a new song) gets the full grace period
            self._last_bands_at = time.monotonic()
        self._playing = playing
        self.engine.set_playing(playing)

    def _on_track(self, track) -> None:
        self.engine.set_now_playing(track)

    # ---------- feed signals (GUI thread) ----------

    def _on_bands(self, bands: np.ndarray) -> None:
        # Only when this view is actively capturing (not merely because the
        # ambient pulse is holding the shared feed) and on screen.
        if self._capturing:
            self._last_bands_at = time.monotonic()
            if self.isVisible():
                self.engine.update_bands(bands)

    def _on_waveform(self, wave: np.ndarray) -> None:
        if self._capturing and self.isVisible():
            self.engine.update_waveform(wave)

    def _on_pulse(self, level: float) -> None:
        if self._capturing and self.isVisible():
            self.engine.update_pulse(level)

    def _on_error(self, msg: str) -> None:
        self.status_message.emit(f"visualizer: {msg}")
