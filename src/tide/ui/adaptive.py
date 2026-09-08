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
# acting as a confidence term only after a color has enough presence — and
# "no accent at all" is a first-class outcome: a grayscale cover must fall
# through to the theme baseline instead of wearing whatever whisper of
# chroma survived JPEG compression.
_MIN_HUEFULNESS = 0.020
_MIN_HUE_GROUP_FREQ = 0.055
# A winning hue family must be confidently colored, not merely non-grey.
# Uniform scanner/JPEG toning on a b/w cover forms a "hue family" that
# spans the whole image, but its chroma never rises above a whisper: even
# a +8-per-channel cast tops out around 0.031 mean huefulness, while
# genuinely muted album art (saturation ~0.12–0.2) sits at 0.045 and up.
# This gate is what separates "toned scan" from "faded pastel sleeve".
_MIN_GROUP_HUEFULNESS = 0.036
# ...and its colored mass (huefulness-weighted share of ALL pixels, not
# just the family's) must be a real slice of the image. This is the
# relative check: a few-percent smear of chroma noise on an otherwise
# neutral cover used to win simply by being the only family standing. An
# 8%-area solid logo (huefulness ~0.4) still clears this many times over.
_MIN_CHROMA_MASS = 0.012
# The vivid escape hatch: a SMALL but saturated element — the red glyph on
# a b/w sleeve, a neon sticker — is real color the eye latches onto, and
# such covers kept their accent for years (fake bucket chroma used to
# inflate those families past the area gate; the true-mean rework took
# that crutch away). A family this confidently colored passes on a couple
# percent of area alone. Toning and JPEG grain never get near this bar:
# their mean huefulness stays under ~0.05. The floor is set a hair under
# the nominal 2%: the 64×64 downscale bleeds a small glyph's edge pixels
# into the background, so its counted area lands below its printed area.
_VIVID_MIN_FREQ = 0.015
_VIVID_HUEFULNESS = 0.22
# And the muted counterpart: a dusty-rose or olive panel at huefulness
# ~0.1 would need ~12% of the cover to clear the mass gate alone. A real
# tenth of the image with clearly-non-grey color is not noise, so there
# area stands in for mass (again a hair under nominal for downscale bleed).
_MUTED_PANEL_FREQ = 0.085
_MUTED_PANEL_HUEFULNESS = 0.08
# The dark path: huefulness scales chroma DOWN with value, which is right
# for telling toned scans from pastel sleeves but wrong for near-black
# covers — a clearly-navy cover at HSV value 15 tops out around 0.028 mean
# huefulness and used to read as "toned grey". Saturation (chroma over
# value) is what actually separates the two down there: the navy stays at
# 0.2+ while a uniform +12 cast on a mid-grey gradient reaches only ~0.10
# and grayscale sensor noise ~0.13 with almost no chroma at all. So dark
# families get a second door: saturated enough, carrying real (if small)
# chroma, over most of the cover. The chroma floor is what keeps per-pixel
# noise on a near-black cover out — its bucket means stay under 0.008.
_DARK_MIN_FREQ = 0.20
_DARK_MIN_SAT = 0.16
_DARK_MIN_CHROMA = 0.010
# Per-bucket membership ramps for the dark path (see _dominant_hue_groups):
# buckets fade in as their chroma clears the noise floor and their
# saturation clears the cast/grain band, mirroring the huefulness ramp.
_DARK_CHROMA_RAMP = (0.008, 0.008)
_DARK_SAT_RAMP = (0.14, 0.14)
_HUE_BIN_DEGREES = 24.0
_ALT_MIN_WEIGHT_RATIO = 0.32
_ALT_MIN_HUE_SEP = 32.0


# ---------- palette extraction ----------


def extract_palette(image: QImage) -> list[tuple[QColor, int]]:
    """Return up to 32 dominant colors and their pixel counts via a cheap
    4-bit-per-channel histogram on a 64×64 downscale. Each bucket reports the
    TRUE mean color of the pixels that landed in it, not the bucket center:
    reading centers back moves each channel by up to 16, so a neutral pixel
    whose channels straddle a bucket edge (e.g. 127,127,133) came back with
    chroma 17/255 — fake color that let grayscale covers pass the hue gates.
    Runs on whatever thread calls it.
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
    # Voting: quantize to a 4-bits-per-channel cube (4096 buckets), but
    # accumulate the real channel sums per bucket so the readback below is
    # the mean of what's actually there.
    buckets: dict[tuple[int, int, int], list[int]] = {}
    raw = bytes(bits[: bytes_per_line * h])
    for y in range(h):
        row_start = y * bytes_per_line
        for x in range(0, w * 3, 3):
            r = raw[row_start + x]
            g = raw[row_start + x + 1]
            b = raw[row_start + x + 2]
            acc = buckets.setdefault((r >> 4, g >> 4, b >> 4), [0, 0, 0, 0])
            acc[0] += 1
            acc[1] += r
            acc[2] += g
            acc[3] += b
    if not buckets:
        return []
    top = sorted(buckets.values(), key=lambda acc: acc[0], reverse=True)[:32]
    return [(QColor(rs // n, gs // n, bs // n), n) for n, rs, gs, bs in top]


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


def _smooth_ramp(v: float, start: float, span: float) -> float:
    """Smoothstep membership: 0 at ``start``, 1 past ``start + span``."""
    t = (v - start) / span
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


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

    A family only makes it out at all when it is confidently colored: the
    body path wants enough area (_MIN_HUE_GROUP_FREQ), enough chroma on
    average (_MIN_GROUP_HUEFULNESS), and enough colored mass relative to
    the whole image (_MIN_CHROMA_MASS, with area standing in for mass on
    big muted panels); the vivid path lets a small saturated element — the
    red glyph on a b/w sleeve — through on confidence alone; the dark path
    lets a near-black but clearly saturated cover — a navy sleeve at HSV
    value 15 — through on saturation where huefulness goes blind. An empty
    result means "this cover has no real color" and the pickers pass
    through to the theme baseline.
    """
    total = sum(n for _, n in palette) or 1
    bins = max(1, round(360.0 / _HUE_BIN_DEGREES))
    groups: dict[int, dict[str, object]] = {}
    for color, count in palette:
        huefulness = _huefulness(color)
        # Soft membership near the grey line instead of a hard cut. A hard
        # threshold made the gates flip with 4-bit bucket boundaries as a
        # dark cover darkens (tint kept at L 0.10 and 0.12, lost at 0.11 —
        # whichever greyish buckets happened to straddle the cut swung the
        # group means). Near-grey buckets now fade in over a band around
        # _MIN_HUEFULNESS, so no single bucket can flip the outcome.
        member = _smooth_ramp(
            huefulness, 0.5 * _MIN_HUEFULNESS, _MIN_HUEFULNESS)
        # Dark-door membership (the _DARK_* block up top): down near black
        # saturation, not huefulness, is what separates real color from a
        # toned grey, so buckets fade in as their chroma clears the noise
        # floor and their saturation clears the cast/grain band.
        r, g, b = color.redF(), color.greenF(), color.blueF()
        value = max(r, g, b)
        chroma = value - min(r, g, b)
        saturation = chroma / value if value > 0.0 else 0.0
        dark_member = (_smooth_ramp(chroma, *_DARK_CHROMA_RAMP)
                       * _smooth_ramp(saturation, *_DARK_SAT_RAMP))
        if member <= 0.0 and dark_member <= 0.0:
            continue
        hue = _hue_deg(color)
        idx = int((hue + _HUE_BIN_DEGREES / 2.0) // _HUE_BIN_DEGREES) % bins
        group = groups.setdefault(
            idx,
            {
                "weight": 0.0, "count": 0.0, "huef_mass": 0.0, "colors": [],
                "dark_weight": 0.0, "dark_count": 0.0, "dark_sat_mass": 0.0,
                "dark_chroma_mass": 0.0, "dark_colors": [],
            },
        )
        if member > 0.0:
            eff = float(count) * member
            confidence = 0.50 + 0.50 * min(1.0, huefulness / 0.24)
            group["weight"] = float(group["weight"]) + eff * confidence
            group["count"] = float(group["count"]) + eff
            group["huef_mass"] = float(group["huef_mass"]) + huefulness * eff
            group["colors"].append((color, eff * (0.65 + 0.35 * confidence)))
        if dark_member > 0.0:
            # Mirror of the body accumulation with saturation standing in
            # for huefulness as the confidence axis — fully confident where
            # the sat ramp tops out.
            dark_eff = float(count) * dark_member
            dark_conf = 0.50 + 0.50 * min(
                1.0, saturation / (_DARK_SAT_RAMP[0] + _DARK_SAT_RAMP[1]))
            group["dark_weight"] = (
                float(group["dark_weight"]) + dark_eff * dark_conf)
            group["dark_count"] = float(group["dark_count"]) + dark_eff
            group["dark_sat_mass"] = (
                float(group["dark_sat_mass"]) + saturation * dark_eff)
            group["dark_chroma_mass"] = (
                float(group["dark_chroma_mass"]) + chroma * dark_eff)
            group["dark_colors"].append(
                (color, dark_eff * (0.65 + 0.35 * dark_conf)))
    result: list[tuple[float, float, QColor]] = []
    for group in groups.values():
        count = float(group["count"])
        body = vivid = False
        freq = 0.0
        if count > 0.0:
            freq = count / total
            huef_mass = float(group["huef_mass"])
            mean_huef = huef_mass / count
            # Three ways past the gates. The body path: enough area,
            # confidently colored on average, and enough colored mass
            # relative to the whole image — toning/noise families cover
            # area but carry no real chroma, muted album bodies clear all
            # three comfortably; a real tenth of the cover in muted color
            # passes on area standing in for mass. The vivid path: a small
            # saturated glyph or sticker on an otherwise-grey sleeve,
            # confident enough to carry the accent on a couple percent of
            # area alone.
            body = (
                freq >= _MIN_HUE_GROUP_FREQ
                and mean_huef >= _MIN_GROUP_HUEFULNESS
                and (
                    huef_mass / total >= _MIN_CHROMA_MASS
                    or (freq >= _MUTED_PANEL_FREQ
                        and mean_huef >= _MUTED_PANEL_HUEFULNESS)
                )
            )
            vivid = freq >= _VIVID_MIN_FREQ and mean_huef >= _VIVID_HUEFULNESS
        if body or vivid:
            representative = _weighted_average(group["colors"])
            if representative.isValid():
                result.append((float(group["weight"]), freq, representative))
            continue
        # ...and the dark door: a near-black cover whose color is real but
        # whose huefulness can't show it — saturated enough on average,
        # carrying real (if small) chroma, over most of the cover. Built
        # from the dark-member buckets, not the huefulness ones: the
        # buckets that matter down here may carry no huefulness weight
        # at all.
        dark_count = float(group["dark_count"])
        if dark_count <= 0.0:
            continue
        dark_freq = dark_count / total
        mean_sat = float(group["dark_sat_mass"]) / dark_count
        mean_chroma = float(group["dark_chroma_mass"]) / dark_count
        if (dark_freq < _DARK_MIN_FREQ or mean_sat < _DARK_MIN_SAT
                or mean_chroma < _DARK_MIN_CHROMA):
            continue
        representative = _weighted_average(group["dark_colors"])
        if representative.isValid():
            result.append(
                (float(group["dark_weight"]), dark_freq, representative))
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


def neutral_tint(palette: list[tuple[QColor, int]]) -> QColor:
    """A pure grey at the cover's mean lightness, for covers that carry no
    confident colour. The backdrop reads the lightness to sit a darker
    sleeve behind a dimmer field and a white one behind a brighter one."""
    total = sum(max(0, int(n)) for _, n in palette) or 1
    lum = sum(c.lightnessF() * max(0, int(n)) for c, n in palette) / total
    v = int(round(max(0.0, min(1.0, lum)) * 255))
    return QColor(v, v, v)


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

    # Full-res cover for consumers that draw the art itself (the liquid
    # backdrop), emitted with the same generation guarding as the palette:
    # QImage on fetch, None when nothing is playing or the fetch failed.
    art_ready = Signal(object)

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
        # Monotonic per-track-change id. Every async stage (art fetch →
        # palette worker → apply) carries the gen it was started for and is
        # discarded when a newer track change happened meanwhile. The old
        # URL-equality guard couldn't tell "stale result for the same art"
        # from "fresh result", and worse, it had no answer for *failures* —
        # see _apply_palette.
        self._gen = 0

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
        self._gen += 1
        if not self.is_enabled():
            return
        if track is None or not track.thumbnail:
            theming.manager().clear_accent_override()
            self.art_ready.emit(None)
            self._current_url = None
            return
        self._current_url = track.thumbnail
        gen = self._gen
        # Need a QImage. Try cache first.
        url = track.thumbnail
        img = art_cache.cache().request(
            url, lambda image, gen=gen: self._on_art_ready(gen, image)
        )
        if img is not None:
            self._on_art_ready(gen, img)

    def _on_art_ready(self, gen: int, img: QImage | None) -> None:
        if gen != self._gen:
            return
        if img is None or img.isNull():
            # The fetch failed for the track that's actually playing. The
            # old code returned here, which meant the PREVIOUS track's
            # palette stayed on screen for the whole song. Baseline theme
            # colors are the correct fallback.
            theming.manager().clear_accent_override()
            self.art_ready.emit(None)
            return
        self.art_ready.emit(img)
        # Extract in worker.
        worker = _PaletteWorker(img)
        self._palette_jobs.add(worker)
        worker.signals.done.connect(
            lambda palette, worker=worker, gen=gen: self._on_palette_done_from_worker(
                worker, gen, palette
            )
        )
        QThreadPool.globalInstance().start(worker)

    def _on_palette_done_from_worker(
        self, worker: _PaletteWorker, gen: int, palette: list
    ) -> None:
        try:
            worker.signals.done.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._palette_jobs.discard(worker)
        if gen != self._gen:
            return
        self._apply_palette(palette)

    def _apply_palette(self, palette: list) -> None:
        """Apply this track's palette, replacing the previous track's.

        The dynamic override layer is REPLACED wholesale every time, never
        merged into. ``override_tokens`` merges, so the old flow of pushing
        only the keys this cover produced left the rest of the previous
        song's palette standing — a grayscale cover with a usable tint kept
        the last song's accent, and a cover that produced nothing kept
        everything. Both read as "the backdrop is stuck on the previous
        song". An empty replacement returns the theme baseline.
        """
        theme = theming.manager().current()
        if theme is None:
            return
        overrides: dict[str, str] = {}
        if palette:
            bg = QColor(theme.token("bg", "#0b0b0b"))
            new_accent = pick_accent(palette, bg)
            wants_bg = self._wants_ambient_bg()
            new_ambient_bg = pick_bg_tint(palette) if wants_bg else None
            if new_accent is not None:
                overrides["accent"] = new_accent.name()
                # Pick a second color for accent_alt (used by neon-grid
                # visualizer + a few QSS spots), but only from colors
                # actually in the cover.
                new_accent_alt = pick_accent_alt(palette, new_accent, bg)
                if new_accent_alt is not None:
                    overrides["accent_alt"] = new_accent_alt.name()
            if new_ambient_bg is not None:
                overrides["ambient_bg"] = new_ambient_bg.name()
            elif wants_bg and new_accent is None:
                # A cover with no confident colour. The UI accent stays the
                # theme's (that fallback is deliberate), but the backdrop
                # should not: a black-and-white sleeve behind a blue glow
                # reads as the wrong song. Push a neutral at the cover's
                # brightness and flag it, so the backdrop paints greys
                # instead of falling back to the theme accent.
                grey = neutral_tint(palette)
                overrides["ambient_bg"] = grey.name()
                overrides["ambient_neutral"] = "1"
        theming.manager().replace_dynamic_tokens(overrides)
