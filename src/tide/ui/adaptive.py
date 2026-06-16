"""Adaptive accent driver — shift the active theme's accent toward the
dominant color of whatever's playing.

Pipeline:
    track changes → fetch art (art_cache) → extract palette (frequency-
    counted 4-bit-per-channel histogram on a 64×64 downscale, in a worker)
    → score candidates by vibrancy × √frequency → normalize the winning
    hue into a readable accent for the current theme → animate from
    current to target over ~1.5s → push patched stylesheet each frame.

Operates on top of any theme. When the active theme's ``[layout].adaptive``
flag is true, the driver also animates ``bg_alt`` for a stronger reactive
look.
"""
from __future__ import annotations

import colorsys
import math
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

# Picker tuning. _MIN_VIBRANCY is a chroma×brightness floor below which a
# bucket is treated as grey/black/white and can't supply a usable hue.
_MIN_VIBRANCY = 0.15
# Min hue separation (degrees) between accent and accent_alt.
_MIN_HUE_SEP = 35.0


def _qcolor_lerp(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
    )


# ---------- palette extraction ----------


def extract_palette(image: QImage) -> list[tuple[QColor, int]]:
    """Return up to 32 dominant colors and their pixel counts via a cheap
    4-bit-per-channel histogram on a 64×64 downscale. Runs on whatever thread
    calls it.
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
    # ×17 maps 4-bit (0..15) back to 8-bit (0..255).
    return [(QColor(r * 17, g * 17, b * 17), n) for (r, g, b), n in counts.most_common(32)]


# ---------- color helpers ----------


def _luminance(c: QColor) -> float:
    r, g, b = c.redF(), c.greenF(), c.blueF()
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _vibrancy(c: QColor) -> float:
    """Visual colorfulness in 0..1. HSV chroma weighted by brightness so
    near-black colors (e.g. rgb(20,10,10), which HLS happily calls 100%
    saturated) don't score as vibrant.
    """
    r, g, b = c.redF(), c.greenF(), c.blueF()
    chroma = max(r, g, b) - min(r, g, b)
    value = max(r, g, b)
    return chroma * (0.35 + 0.65 * value)


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


def _normalize_accent(c: QColor, bg_lum: float) -> QColor:
    """Keep the hue, clamp lightness/saturation into a readable accent band
    against the theme's bg. Otherwise a near-black album cover yields a
    near-black accent that's invisible on the (also dark) theme.
    """
    h, l, s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    if bg_lum < 0.5:
        # Dark theme: bright accent.
        l = min(max(l, 0.55), 0.78)
        s = max(s, 0.58)
    else:
        # Light theme: deep accent.
        l = min(max(l, 0.28), 0.48)
        s = max(s, 0.62)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


# ---------- pickers ----------


def pick_accent(
    palette: list[tuple[QColor, int]], theme_bg: QColor
) -> QColor | None:
    """Pick the most prominent vibrant hue in ``palette`` and normalize it
    into a readable accent against ``theme_bg``. Returns None when no bucket
    has a usable hue (e.g. fully grayscale cover) — caller should keep the
    base theme accent.
    """
    if not palette:
        return None
    total = sum(n for _, n in palette) or 1
    bg_lum = _luminance(theme_bg)
    candidates: list[tuple[float, QColor]] = []
    for color, count in palette:
        vib = _vibrancy(color)
        if vib < _MIN_VIBRANCY:
            continue
        freq = count / total
        # √freq lets a moderate-frequency vibrant color beat a 1-pixel splash,
        # without letting a single dominant muted color shadow a smaller
        # genuinely-vibrant one. Without this term the picker was choosing
        # tiny bright outliers as accents.
        score = vib * math.sqrt(freq)
        candidates.append((score, color))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return _normalize_accent(candidates[0][1], bg_lum)


def pick_accent_alt(
    palette: list[tuple[QColor, int]], accent: QColor, theme_bg: QColor
) -> QColor | None:
    """Pick a secondary accent with a distinct hue from the primary.

    Used by the visualizer's neon-grid renderer (which paints with both
    ``accent`` and ``accent_alt``) so the whole reactive look adapts.
    """
    bg_lum = _luminance(theme_bg)
    if not palette:
        return _normalize_accent(_complementary(accent), bg_lum)
    total = sum(n for _, n in palette) or 1
    candidates: list[tuple[float, QColor]] = []
    for color, count in palette:
        vib = _vibrancy(color)
        if vib < _MIN_VIBRANCY:
            continue
        hd = _hue_distance(color, accent)
        if hd < _MIN_HUE_SEP:
            continue
        freq = count / total
        # Reward bigger hue separation so we land on a true contrasting tone
        # instead of a near-neighbor.
        sep_bonus = min(hd / 180.0, 1.0)
        score = vib * math.sqrt(freq) * (0.6 + 0.6 * sep_bonus)
        candidates.append((score, color))
    if not candidates:
        return _normalize_accent(_complementary(accent), bg_lum)
    candidates.sort(key=lambda x: x[0], reverse=True)
    return _normalize_accent(candidates[0][1], bg_lum)


def pick_bg_tint(palette: list[tuple[QColor, int]]) -> QColor | None:
    """Pick a deeply-muted version of the album's dominant body color for the
    ``bg_alt`` tint. Unlike the accent picker this weights raw frequency —
    the tint should feel like the cover's main mass, not a small splash.
    """
    if not palette:
        return None
    total = sum(n for _, n in palette) or 1
    candidates: list[tuple[float, QColor]] = []
    for color, count in palette:
        vib = _vibrancy(color)
        if vib < 0.08:  # looser than accent — even subtle hues make a fine tint
            continue
        freq = count / total
        score = vib * freq
        candidates.append((score, color))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    base = candidates[0][1]
    h, l, s = colorsys.rgb_to_hls(base.redF(), base.greenF(), base.blueF())
    s = min(s, 0.32)
    l = min(l, 0.10)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


# ---------- worker ----------


class _PaletteWorker(QRunnable):
    """Runs ``extract_palette`` on the QThreadPool to keep the GUI thread free."""

    class _Sig(QObject):
        done = Signal(object)        # list[tuple[QColor, int]]

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
        # When True, push bg_alt overrides even if the theme doesn't ask for
        # them — used by the CentralBg gradient setting. Independent of the
        # accent shift; the user can have either, both, or neither.
        self._background_enabled = False
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

    def set_background_enabled(self, on: bool) -> None:
        """Toggle the bg_alt extraction path used by the central-area
        gradient. Independent of ``set_enabled`` (the accent shift) — the
        user can pick either, both, or neither in settings."""
        if on == self._background_enabled:
            return
        self._background_enabled = on
        if self.is_enabled():
            # Re-fire for the current track so bg_alt is computed (or
            # cleared) immediately rather than waiting for the next track.
            self._on_track_changed(self._queue.current)
        elif not on:
            # Background turned off and accent is off too — clear any
            # remaining bg_alt override so the theme baseline returns.
            theming.manager().clear_accent_override()

    def is_enabled(self) -> bool:
        return (
            self._enabled
            or self._background_enabled
            or self._theme_demands_adaptive()
        )

    def _wants_bg_alt(self) -> bool:
        return self._background_enabled or self._theme_demands_adaptive()

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
        new_bg_alt = pick_bg_tint(palette) if self._wants_bg_alt() else None

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
