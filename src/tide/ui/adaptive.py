"""Adaptive accent driver — shift the active theme's accent toward the
dominant color of whatever's playing.

Pipeline:
    track changes → fetch art (art_cache) → extract palette (median-cut on
    a 64×64 downscale, in a worker) → pick best accent → animate from
    current to target over ~1.5s → push patched stylesheet each frame.

Operates on top of any theme. When the active theme's ``[layout].adaptive``
flag is true, the driver also animates ``bg_alt`` for a stronger reactive
look.
"""
from __future__ import annotations

import colorsys
from collections import Counter
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    QVariantAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage

from .. import theming
from . import art_cache


# Animation tuning
ANIM_DURATION_MS = 1500
ANIM_EASING = QEasingCurve.OutCubic


def _qcolor_lerp(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
    )


# ---------- palette extraction ----------


def extract_palette(image: QImage, max_samples: int = 4096) -> list[QColor]:
    """Return up to ~5 dominant colors from ``image`` via a cheap histogram +
    median-cut style binning. Runs on whatever thread calls it.
    """
    if image is None or image.isNull():
        return []
    # Downscale aggressively — color voting is dominated by relative areas, not detail.
    small = image.scaled(64, 64, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    small = small.convertToFormat(QImage.Format_RGB888)
    bits = small.constBits()
    w = small.width()
    h = small.height()
    bytes_per_line = small.bytesPerLine()
    # Voting: quantize to a 4-bits-per-channel cube (4096 buckets).
    counts: Counter = Counter()
    raw = bytes(bits[: bytes_per_line * h])
    for y in range(h):
        row_start = y * bytes_per_line
        for x in range(0, w * 3, 3):
            r = raw[row_start + x] >> 4
            g = raw[row_start + x + 1] >> 4
            b = raw[row_start + x + 2] >> 4
            counts[(r, g, b)] += 1
    if not counts:
        return []
    # Top buckets become candidate colors.
    top = counts.most_common(24)
    palette: list[QColor] = []
    for (r, g, b), _ in top:
        col = QColor(r * 17, g * 17, b * 17)  # ×17 = back from 4-bit to 8-bit
        palette.append(col)
    return palette


def _saturation(c: QColor) -> float:
    r, g, b = c.redF(), c.greenF(), c.blueF()
    _h, l, s = colorsys.rgb_to_hls(r, g, b)
    # Combine saturation + how far from pure white/black (avoid washed-out picks).
    return s * (1.0 - abs(l - 0.5) * 0.4)


def _luminance(c: QColor) -> float:
    r, g, b = c.redF(), c.greenF(), c.blueF()
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def pick_accent(palette: list[QColor], theme_bg: QColor) -> QColor | None:
    """Pick the most saturated color from ``palette`` that still has enough
    contrast against the theme's background. Returns None if the palette is
    all washed out — caller should keep the base theme accent.
    """
    if not palette:
        return None
    bg_lum = _luminance(theme_bg)
    candidates = []
    for c in palette:
        sat = _saturation(c)
        if sat < 0.20:
            continue
        # Want adequate contrast: prefer light accents on dark bg and vice versa.
        target_diff = 0.35
        contrast = abs(_luminance(c) - bg_lum)
        if contrast < target_diff:
            # Boost saturation but reduce confidence.
            sat *= 0.5
        candidates.append((sat, c))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def pick_accent_alt(palette: list[QColor], accent: QColor, theme_bg: QColor) -> QColor | None:
    """Pick a secondary accent that contrasts well with the primary accent.

    Used for the visualizer's neon-grid renderer (which paints with both
    ``accent`` and ``accent_alt``) so the whole reactive look adapts.
    """
    if not palette:
        return _complementary(accent)
    bg_lum = _luminance(theme_bg)
    candidates = []
    for c in palette:
        sat = _saturation(c)
        if sat < 0.20:
            continue
        # Want a hue at least 30° away from the primary accent.
        if _hue_distance(c, accent) < 30:
            continue
        contrast = abs(_luminance(c) - bg_lum)
        score = sat * (0.6 + 0.4 * min(contrast / 0.5, 1.0))
        candidates.append((score, c))
    if not candidates:
        return _complementary(accent)
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _hue_distance(a: QColor, b: QColor) -> float:
    ah = colorsys.rgb_to_hls(a.redF(), a.greenF(), a.blueF())[0] * 360.0
    bh = colorsys.rgb_to_hls(b.redF(), b.greenF(), b.blueF())[0] * 360.0
    d = abs(ah - bh)
    return min(d, 360.0 - d)


def _complementary(c: QColor) -> QColor:
    h, l, s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    h = (h + 0.5) % 1.0
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def pick_bg_tint(palette: list[QColor]) -> QColor | None:
    """Pick a desaturated, slightly darker color for the bg_alt tint."""
    if not palette:
        return None
    # Use the brightest sufficiently-saturated color, then desaturate + darken.
    palette_sorted = sorted(palette, key=_saturation, reverse=True)
    base = palette_sorted[0]
    r, g, b = base.redF(), base.greenF(), base.blueF()
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    # Pull toward a calmer version.
    s = min(s, 0.35)
    l = min(l, 0.12)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


# ---------- worker ----------


class _PaletteWorker(QRunnable):
    """Runs ``extract_palette`` on the QThreadPool to keep the GUI thread free."""

    class _Sig(QObject):
        done = Signal(object)        # list[QColor]

    def __init__(self, image: QImage) -> None:
        super().__init__()
        self.signals = self._Sig()
        self._image = image
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            colors = extract_palette(self._image)
        except Exception:
            colors = []
        self.signals.done.emit(colors)


# ---------- driver ----------


class AdaptiveDriver(QObject):
    """Owns the animation + theme overrides for the active session."""

    def __init__(self, queue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue = queue
        self._enabled = False
        self._current_url: str | None = None

        self._target_accent: QColor | None = None
        self._target_accent_alt: QColor | None = None
        self._target_bg_alt: QColor | None = None
        self._current_accent: QColor | None = None
        self._current_accent_alt: QColor | None = None
        self._current_bg_alt: QColor | None = None
        self._suppress_theme_handler: bool = False

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(ANIM_DURATION_MS)
        self._anim.setEasingCurve(ANIM_EASING)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_anim_frame)

        queue.current_changed.connect(self._on_track_changed)
        theming.manager().theme_changed.connect(self._on_theme_changed)

    def set_enabled(self, on: bool) -> None:
        if on == self._enabled:
            return
        self._enabled = on
        if on:
            # Trigger immediately for current track.
            self._on_track_changed(self._queue.current)
        else:
            self._anim.stop()
            theming.manager().clear_accent_override()
            self._current_accent = None
            self._target_accent = None

    def is_enabled(self) -> bool:
        return self._enabled or self._theme_demands_adaptive()

    def _theme_demands_adaptive(self) -> bool:
        t = theming.manager().current()
        if t is None:
            return False
        return bool(t.t("layout", "adaptive", False))

    # ---------- signal handlers ----------

    def _on_theme_changed(self, theme) -> None:
        # theming.override_tokens() re-emits theme_changed on every animation
        # frame. Ignore those.
        if self._anim.state() == QVariantAnimation.Running:
            return
        if self._suppress_theme_handler:
            return
        # Same base theme (just an override push, layout swap, etc.) — do
        # NOT re-anchor or re-extract. Doing so caused settings-open lag
        # spikes (each picker setCurrentIndex re-fires theme_changed, which
        # spawned a palette worker, which animated, which re-emitted, …).
        new_slug = getattr(theme, "slug", None)
        last_slug = getattr(self, "_last_base_slug", None)
        if new_slug == last_slug:
            return
        self._last_base_slug = new_slug
        # Real theme change: re-anchor to the new base palette.
        self._current_accent = None
        self._current_accent_alt = None
        self._current_bg_alt = None
        if self.is_enabled():
            self._on_track_changed(self._queue.current)

    def _on_track_changed(self, track) -> None:
        if not self.is_enabled():
            return
        if track is None or not track.thumbnail:
            self._anim.stop()
            theming.manager().clear_accent_override()
            self._current_url = None
            return
        self._current_url = track.thumbnail
        # Need a QImage. Try cache first.
        img = art_cache.cache().request(track.thumbnail, self._on_art_ready)
        if img is not None:
            self._on_art_ready(img)

    def _on_art_ready(self, img: QImage | None) -> None:
        if img is None or img.isNull():
            return
        if self._current_url is None:
            return
        # Extract in worker.
        worker = _PaletteWorker(img)
        worker.signals.done.connect(self._on_palette_done)
        QThreadPool.globalInstance().start(worker)

    def _on_palette_done(self, palette: list) -> None:
        if not palette:
            return
        theme = theming.manager().current()
        if theme is None:
            return
        bg = QColor(theme.token("bg", "#0b0b0b"))
        new_accent = pick_accent(palette, bg)
        if new_accent is None:
            return
        # Pick a contrasting second color for accent_alt (used by neon-grid
        # visualizer + a few QSS spots). Falls back to a complementary hue.
        new_accent_alt = pick_accent_alt(palette, new_accent, bg)
        new_bg_alt = pick_bg_tint(palette) if self._theme_demands_adaptive() else None

        # Start animation: from current → target.
        self._target_accent = new_accent
        self._target_accent_alt = new_accent_alt
        self._target_bg_alt = new_bg_alt
        if self._current_accent is None:
            self._current_accent = QColor(theme.token("accent", "#d4b95e"))
        if self._current_accent_alt is None and new_accent_alt is not None:
            self._current_accent_alt = QColor(theme.token("accent_alt",
                                                          theme.token("accent", "#d4b95e")))
        if self._current_bg_alt is None and new_bg_alt is not None:
            self._current_bg_alt = QColor(theme.token("bg_alt", "#141414"))
        self._anim.stop()
        self._anim.start()

    def _on_anim_frame(self, t: float) -> None:
        if self._target_accent is None or self._current_accent is None:
            return
        accent = _qcolor_lerp(self._current_accent, self._target_accent, t)
        kwargs = {"accent": accent.name()}
        if self._target_accent_alt is not None and self._current_accent_alt is not None:
            alt = _qcolor_lerp(self._current_accent_alt, self._target_accent_alt, t)
            kwargs["accent_alt"] = alt.name()
        if self._target_bg_alt is not None and self._current_bg_alt is not None:
            bg_alt = _qcolor_lerp(self._current_bg_alt, self._target_bg_alt, t)
            kwargs["bg_alt"] = bg_alt.name()
        theming.manager().override_tokens(kwargs)
        if t >= 1.0:
            self._current_accent = self._target_accent
            if self._target_accent_alt is not None:
                self._current_accent_alt = self._target_accent_alt
            if self._target_bg_alt is not None:
                self._current_bg_alt = self._target_bg_alt
