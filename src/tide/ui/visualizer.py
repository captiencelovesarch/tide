"""Audio visualizer view.

One ``VisualizerView`` widget. Multiple ``Renderer`` strategies. The
active strategy is chosen by the current theme's ``[layout].visualizer``
token (``bars-mono`` | ``bars-filled`` | ``oscilloscope`` | ``neon-grid``).

Each strategy reads palette tokens at paint time so theme swaps repaint
correctly.
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import audio_capture, settings as settings_module, theming
from .widgets import BracketButton


def _color(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))


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
        p.fillRect(rect, bg)
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
        p.fillRect(rect, bg)
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
        p.fillRect(rect, bg)
        if waveform is None or waveform.shape[0] < 2:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        n = waveform.shape[0]
        cx = rect.left()
        cy = rect.top() + rect.height() // 2
        # Pre-build the path so the stroke is one call.
        path = QPainterPath()
        # Center-amplitude scale; clamp to a fraction of the height.
        scale = (rect.height() / 2) * 0.85
        step = rect.width() / float(n - 1)
        path.moveTo(cx, cy - waveform[0] * scale)
        for i in range(1, n):
            x = cx + i * step
            y = cy - float(waveform[i]) * scale
            path.lineTo(x, y)
        # Soft halo behind the main stroke.
        halo = QColor(accent)
        halo.setAlpha(70)
        pen = QPen(halo, 4.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(path)
        main = QPen(accent, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(main)
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
        p.fillRect(rect, bg)
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
        p.fillRect(rect, bg)
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
        p.fillRect(rect, bg)
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
        p.fillRect(rect, bg)
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
        p.fillRect(rect, QColor(bg.red(), bg.green(), bg.blue(), 60))
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
        p.fillRect(rect, bg)
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
        theming.manager().theme_changed.connect(self._on_theme)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

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
        self.update()

    def update_waveform(self, wave: np.ndarray) -> None:
        self._waveform = wave
        # Oscilloscope wants per-waveform repaint; bars already update via bands_updated.
        if isinstance(self._renderer, OscilloscopeRenderer):
            self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        self._renderer.paint(p, self.rect(), self._theme, self._bands, self._waveform)


class VisualizerView(QWidget):
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._feed = audio_capture.feed()
        self._feed.bands_updated.connect(self._on_bands)
        self._feed.waveform_updated.connect(self._on_waveform)
        self._feed.error.connect(self._on_error)

        self._canvas = _Canvas(self)
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._renderer_override: str | None = None
        self._audio_source_override: str | None = None

        self._heading = QLabel(self._heading_text())
        self._heading.setProperty("class", "dim")
        self._heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._toggle_btn = BracketButton("start" if not self._feed.running else "stop")
        self._toggle_btn.clicked.connect(self._on_toggle)

        self._fullscreen_btn = BracketButton("fullscreen")
        self._fullscreen_btn.clicked.connect(self._toggle_fullscreen)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self._heading, stretch=1)
        bar.addWidget(self._toggle_btn)
        bar.addWidget(self._fullscreen_btn)

        from . import scale as _scale
        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(8))
        root.addLayout(bar)
        root.addWidget(self._canvas, stretch=1)

        # Floating cog button on top-right of the canvas.
        self._cog = QToolButton(self._canvas)
        self._cog.setText("⚙")
        self._cog.setAutoRaise(True)
        self._cog.setPopupMode(QToolButton.InstantPopup)
        self._cog.setStyleSheet(
            "QToolButton { background: rgba(0,0,0,140); color: white; "
            "border: 1px solid rgba(255,255,255,80); border-radius: 4px; "
            "padding: 4px 8px; font-size: 14pt; }"
            "QToolButton:hover { background: rgba(0,0,0,200); }"
            "QToolButton::menu-indicator { image: none; width: 0; }"
        )
        self._cog.setMenu(self._build_cog_menu())
        # Position deferred to first resize.
        self._cog.show()
        self._cog.raise_()

        self._fullscreen = False
        # Apply saved audio device override at construction time so the first
        # start() uses the user's choice.
        try:
            saved = settings_module.load()
            if saved.audio_device:
                self._audio_source_override = saved.audio_device
        except Exception:
            pass

    # ---------- cog menu ----------

    def _build_cog_menu(self) -> QMenu:
        menu = QMenu(self)

        # Renderer submenu
        rmenu = menu.addMenu("renderer")
        r_group = QActionGroup(rmenu)
        r_group.setExclusive(True)
        a_theme = QAction("from theme", rmenu, checkable=True)
        a_theme.setChecked(self._renderer_override is None)
        a_theme.triggered.connect(lambda: self._set_renderer_override(None))
        r_group.addAction(a_theme)
        rmenu.addAction(a_theme)
        rmenu.addSeparator()
        for slug in renderer_slugs():
            a = QAction(slug, rmenu, checkable=True)
            a.setChecked(self._renderer_override == slug)
            a.triggered.connect(lambda _=False, s=slug: self._set_renderer_override(s))
            r_group.addAction(a)
            rmenu.addAction(a)

        # Audio device submenu
        amenu = menu.addMenu("audio source")
        a_group = QActionGroup(amenu)
        a_group.setExclusive(True)
        a_auto = QAction("auto (default sink monitor)", amenu, checkable=True)
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

        menu.addSeparator()
        a_fs = QAction("fullscreen", menu)
        a_fs.triggered.connect(self._toggle_fullscreen)
        menu.addAction(a_fs)

        a_stop = QAction("stop / restart", menu)
        a_stop.triggered.connect(self._on_toggle)
        menu.addAction(a_stop)

        return menu

    def _refresh_cog_menu(self) -> None:
        # Rebuild so device list re-enumerates fresh.
        self._cog.setMenu(self._build_cog_menu())

    def _set_renderer_override(self, slug: str | None) -> None:
        self._renderer_override = slug
        self._canvas.set_renderer_override(slug)
        self._heading.setText(self._heading_text())
        self._refresh_cog_menu()
        self.status_message.emit(theming.styled_case(
            f"visualizer · {slug}" if slug else "visualizer · theme default"
        ))

    def _set_audio_source(self, name: str | None) -> None:
        self._audio_source_override = name
        # Persist so the picker remembers across launches.
        try:
            s = settings_module.load()
            s.audio_device = name or ""
            settings_module.save(s)
        except Exception:
            pass
        # Restart the feed if it's already running so the change takes effect.
        if self._feed.running:
            self._feed.stop()
            self._toggle_btn.setLabel("start")
            self._start_capture()
        self._refresh_cog_menu()

    def _position_cog(self) -> None:
        if self._cog and self._canvas:
            margin = 8
            x = self._canvas.width() - self._cog.width() - margin
            y = margin
            self._cog.move(max(0, x), max(0, y))

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._position_cog()

    # ---------- visibility-driven start/stop ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._feed.running:
            self._start_capture()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        # Don't tear down audio if the visualizer is just behind another view —
        # keep capturing so revealing it is instant. Users who want it off use
        # the [stop] button or the settings toggle.

    def teardown(self) -> None:
        self._feed.stop()
        self._toggle_btn.setLabel("start")

    # ---------- actions ----------

    def _start_capture(self) -> None:
        ok = self._feed.start(source=self._audio_source_override)
        if ok:
            self._toggle_btn.setLabel("stop")
            self.status_message.emit(theming.styled_case(f"visualizer · capturing from {self._feed.device}"))
        else:
            self._toggle_btn.setLabel("start")
            self.status_message.emit(theming.styled_case("visualizer · no audio device"))

    def _on_toggle(self) -> None:
        if self._feed.running:
            self._feed.stop()
            self._toggle_btn.setLabel("start")
            self.status_message.emit(theming.styled_case("visualizer · stopped"))
        else:
            self._start_capture()

    def _toggle_fullscreen(self) -> None:
        if not self._fullscreen:
            self._was_geometry = self.window().saveGeometry()
            self.window().showFullScreen()
            self._fullscreen = True
            self._fullscreen_btn.setLabel("exit fullscreen")
        else:
            self.window().showNormal()
            try:
                self.window().restoreGeometry(self._was_geometry)
            except Exception:
                pass
            self._fullscreen = False
            self._fullscreen_btn.setLabel("fullscreen")

    # ---------- feed signals (GUI thread) ----------

    def _on_bands(self, bands: np.ndarray) -> None:
        if self._canvas.isVisible():
            self._canvas.update_bands(bands)

    def _on_waveform(self, wave: np.ndarray) -> None:
        if self._canvas.isVisible():
            self._canvas.update_waveform(wave)

    def _on_error(self, msg: str) -> None:
        self.status_message.emit(f"visualizer: {msg}")

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self._heading.setText(self._heading_text())

    def _heading_text(self) -> str:
        r = renderer_for_theme(self._theme)
        line = "─" * 40
        label = theming.styled_case(f"visualizer · {r.slug}")
        return f"── {label} {line}"
