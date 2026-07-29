"""Adaptive accent driver — shift the active theme's accent toward the
dominant color of whatever's playing.

Pipeline:
    track changes → fetch art (art_cache) → extract palette (frequency-
    counted 4-bit-per-channel histogram on a 64×64 downscale, in a worker)
    → group dominant hue families → normalize selected hues
    into readable theme tokens → push one patched stylesheet per palette.

Operates on top of any theme. When the active theme's ``[layout].adaptive``
flag is true, the driver also supplies ``ambient_bg`` for the custom-painted
app backdrop. It deliberately does not animate ``bg_alt``: that token is used
by ordinary controls and panel chrome, and album-art colors there make the UI
look muddy instead of clean.
"""
from __future__ import annotations

import colorsys
from collections import Counter

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage

from .. import theming
from . import art_cache


# Picker tuning. Album colors should read as the album, not as the loudest
# tiny detail. Hue families are selected mostly by pixel mass, with chroma
# acting as a confidence term only after a color has enough presence.
_MIN_HUEFULNESS = 0.020
_MIN_HUE_GROUP_FREQ = 0.055
_HUE_BIN_DEGREES = 24.0
_ALT_MIN_WEIGHT_RATIO = 0.32
_ALT_MIN_HUE_SEP = 32.0


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


def _huefulness(c: QColor) -> float:
    """How confidently this bucket carries a hue. Greys return near zero;
    muted album body colors still pass when they occupy meaningful area."""
    r, g, b = c.redF(), c.greenF(), c.blueF()
    chroma = max(r, g, b) - min(r, g, b)
    value = max(r, g, b)
    return chroma * (0.45 + 0.55 * value)


def _hue_deg(c: QColor) -> float:
    return colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())[0] * 360.0


def _hue_distance(a: QColor, b: QColor) -> float:
    ah = _hue_deg(a)
    bh = _hue_deg(b)
    d = abs(ah - bh)
    return min(d, 360.0 - d)


def _weighted_average(colors: list[tuple[QColor, float]]) -> QColor:
    if not colors:
        return QColor()
    total = sum(weight for _, weight in colors) or 1.0
    r = sum(color.redF() * weight for color, weight in colors) / total
    g = sum(color.greenF() * weight for color, weight in colors) / total
    b = sum(color.blueF() * weight for color, weight in colors) / total
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def _dominant_hue_groups(
    palette: list[tuple[QColor, int]],
) -> list[tuple[float, float, QColor]]:
    """Return hue groups as (weight, frequency, representative color).

    Buckets are grouped into broad hue families. The weight is deliberately
    dominated by count, with huefulness only nudging confidence; this keeps
    a green/grey cover from turning yellow or pink because of small bright
    text or stickers in the art.
    """
    total = sum(n for _, n in palette) or 1
    bins = max(1, round(360.0 / _HUE_BIN_DEGREES))
    groups: dict[int, dict[str, object]] = {}
    for color, count in palette:
        huefulness = _huefulness(color)
        if huefulness < _MIN_HUEFULNESS:
            continue
        hue = _hue_deg(color)
        idx = int((hue + _HUE_BIN_DEGREES / 2.0) // _HUE_BIN_DEGREES) % bins
        confidence = 0.50 + 0.50 * min(1.0, huefulness / 0.24)
        weight = float(count) * confidence
        avg_weight = float(count) * (0.65 + 0.35 * confidence)
        group = groups.setdefault(idx, {"weight": 0.0, "count": 0, "colors": []})
        group["weight"] = float(group["weight"]) + weight
        group["count"] = int(group["count"]) + count
        group["colors"].append((color, avg_weight))
    result: list[tuple[float, float, QColor]] = []
    for group in groups.values():
        weight = float(group["weight"])
        freq = int(group["count"]) / total
        if freq < _MIN_HUE_GROUP_FREQ:
            continue
        representative = _weighted_average(group["colors"])
        if representative.isValid():
            result.append((weight, freq, representative))
    result.sort(key=lambda item: item[0], reverse=True)
    return result


def _normalize_accent(c: QColor, bg_lum: float) -> QColor:
    """Keep the hue, clamp lightness/saturation into a readable accent band
    against the theme's bg. Otherwise a near-black album cover yields a
    near-black accent that's invisible on the (also dark) theme.
    """
    h, l, s = colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())
    if bg_lum < 0.5:
        # Dark theme: readable but not candy-saturated. Muted album hues get
        # enough chroma to read; already-vivid hues are capped.
        l = min(max(l, 0.50), 0.72)
        s = min(max(s, 0.34), 0.70)
    else:
        # Light theme: deeper accent, same faithful saturation cap.
        l = min(max(l, 0.30), 0.48)
        s = min(max(s, 0.36), 0.68)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


# ---------- pickers ----------


def pick_accent(
    palette: list[tuple[QColor, int]], theme_bg: QColor
) -> QColor | None:
    """Pick the most prominent hue family in ``palette`` and normalize it
    into a readable accent against ``theme_bg``. Returns None when no bucket
    has a usable hue (e.g. fully grayscale cover) — caller should keep the
    base theme accent.
    """
    if not palette:
        return None
    bg_lum = _luminance(theme_bg)
    groups = _dominant_hue_groups(palette)
    if not groups:
        return None
    return _normalize_accent(groups[0][2], bg_lum)


def pick_accent_alt(
    palette: list[tuple[QColor, int]], accent: QColor, theme_bg: QColor
) -> QColor | None:
    """Pick a secondary accent with a distinct hue from the primary.

    Used by the visualizer's neon-grid renderer (which paints with both
    ``accent`` and ``accent_alt``) so the whole reactive look adapts.
    """
    bg_lum = _luminance(theme_bg)
    if not palette:
        return QColor(accent)
    groups = _dominant_hue_groups(palette)
    if not groups:
        return QColor(accent)
    primary_weight = groups[0][0]
    for weight, _freq, color in groups:
        if _hue_distance(color, accent) < _ALT_MIN_HUE_SEP:
            continue
        if weight < primary_weight * _ALT_MIN_WEIGHT_RATIO:
            continue
        return _normalize_accent(color, bg_lum)
    return QColor(accent)


def pick_bg_tint(palette: list[tuple[QColor, int]]) -> QColor | None:
    """Pick a deeply-muted version of the album's dominant body color for the
    custom backdrop tint. Unlike the accent picker this weights raw frequency
    — the tint should feel like the cover's main mass, not a small splash.
    """
    if not palette:
        return None
    groups = _dominant_hue_groups(palette)
    if not groups:
        return None
    base = groups[0][2]
    h, l, s = colorsys.rgb_to_hls(base.redF(), base.greenF(), base.blueF())
    s = min(max(s, 0.08), 0.28)
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
        # PySide can segfault if Qt auto-deletes the QRunnable while a queued
        # Python signal from its child QObject is still being delivered.
        # AdaptiveDriver retains each worker until the done signal returns.
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            colors = extract_palette(self._image)
        except Exception:
            colors = []
        self.signals.done.emit(colors)


# ---------- driver ----------


class AdaptiveDriver(QObject):
    """Owns adaptive theme overrides for the active session."""

    def __init__(self, queue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue = queue
        self._enabled = False
        # When True, push ambient_bg overrides for the app backdrop.
        # Independent of the accent shift; the user can have either, both,
        # or neither.
        self._background_enabled = False
        # The mini player paints an adaptive backdrop unconditionally — that
        # look IS the feature — so it counts as an ambient consumer for as
        # long as it is on screen, regardless of the app-wide toggles above.
        self._mini_active = False
        self._current_url: str | None = None

        self._palette_jobs: set[_PaletteWorker] = set()

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
            theming.manager().clear_accent_override()

    def set_background_enabled(self, on: bool) -> None:
        """Toggle the album-tint extraction path used by the app backdrop
        gradient. Independent of ``set_enabled`` (the accent shift) — the
        user can pick either, both, or neither in settings."""
        if on == self._background_enabled:
            return
        self._background_enabled = on
        if self.is_enabled():
            # Re-fire for the current track so ambient_bg is computed (or
            # cleared) immediately rather than waiting for the next track.
            self._on_track_changed(self._queue.current)
        elif not on:
            # Background turned off and accent is off too — clear any
            # remaining ambient override so the theme baseline returns.
            theming.manager().clear_accent_override()

    def set_mini_active(self, on: bool) -> None:
        """Mark the mini player as an active consumer of the album palette.

        Keeps extraction running (and ``ambient_bg`` flowing) while the mini
        window is up even when both app-wide adaptive toggles are off. The
        accent override rides along, as it already does for the
        background-only case; the main window is hidden while mini is open,
        so nothing else is looking at it until we clear it on the way out.
        """
        on = bool(on)
        if on == self._mini_active:
            return
        self._mini_active = on
        if on:
            self._on_track_changed(self._queue.current)
        elif not self.is_enabled():
            theming.manager().clear_accent_override()

    def is_enabled(self) -> bool:
        return (
            self._enabled
            or self._background_enabled
            or self._mini_active
            or self._theme_demands_adaptive()
        )

    def _wants_ambient_bg(self) -> bool:
        return (
            self._background_enabled
            or self._mini_active
            or self._theme_demands_adaptive()
        )

    def _theme_demands_adaptive(self) -> bool:
        t = theming.manager().current()
        if t is None:
            return False
        return bool(t.t("layout", "adaptive", False))

    # ---------- signal handlers ----------

    def _on_theme_changed(self, theme) -> None:
        # Same base theme (just an override push, layout swap, etc.) — do
        # NOT re-anchor or re-extract. Doing so caused settings-open lag
        # spikes (each picker setCurrentIndex re-fires theme_changed, which
        # spawned a palette worker, which pushed overrides, which re-emitted, …).
        new_slug = getattr(theme, "slug", None)
        last_slug = getattr(self, "_last_base_slug", None)
        if new_slug == last_slug:
            return
        self._last_base_slug = new_slug
        # Real theme change: re-extract against the new base palette.
        if self.is_enabled():
            self._on_track_changed(self._queue.current)

    def _on_track_changed(self, track) -> None:
        if not self.is_enabled():
            return
        if track is None or not track.thumbnail:
            theming.manager().clear_accent_override()
            self._current_url = None
            return
        self._current_url = track.thumbnail
        # Need a QImage. Try cache first.
        url = track.thumbnail
        img = art_cache.cache().request(
            url, lambda image, url=url: self._on_art_ready(url, image)
        )
        if img is not None:
            self._on_art_ready(url, img)

    def _on_art_ready(self, url: str, img: QImage | None) -> None:
        if img is None or img.isNull():
            return
        if url != self._current_url:
            return
        # Extract in worker.
        worker = _PaletteWorker(img)
        self._palette_jobs.add(worker)
        worker.signals.done.connect(
            lambda palette, worker=worker, url=url: self._on_palette_done_from_worker(
                worker, url, palette
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _on_palette_done_from_worker(
        self, worker: _PaletteWorker, url: str, palette: list
    ) -> None:
        try:
            worker.signals.done.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._palette_jobs.discard(worker)
        if url != self._current_url:
            return
        self._on_palette_done(palette)

    def _on_palette_done(self, palette: list) -> None:
        if not palette:
            return
        theme = theming.manager().current()
        if theme is None:
            return
        bg = QColor(theme.token("bg", "#0b0b0b"))
        new_accent = pick_accent(palette, bg)
        new_ambient_bg = pick_bg_tint(palette) if self._wants_ambient_bg() else None

        overrides: dict[str, str] = {}
        if new_accent is not None:
            overrides["accent"] = new_accent.name()
            # Pick a second color for accent_alt (used by neon-grid visualizer
            # + a few QSS spots), but only from colors actually in the cover.
            new_accent_alt = pick_accent_alt(palette, new_accent, bg)
            if new_accent_alt is not None:
                overrides["accent_alt"] = new_accent_alt.name()
        if new_ambient_bg is not None:
            overrides["ambient_bg"] = new_ambient_bg.name()
        if not overrides:
            return
        theming.manager().override_tokens(overrides)
