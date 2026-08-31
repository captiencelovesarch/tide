"""Backdrop style renderer coverage.

Every style — the two gradient originals, the liquid cover, the reworked
scene styles and the three new ones — must render clean across the whole
support matrix: window shapes from huge to tiny, idle and pounding pulse,
all three motion modes, dark / light / translucent themes. On top of the
no-crash sweep, dark opaque frames must sit in the content-legible
brightness band (neither a dead black rectangle nor a blown-out wall),
and every style must fit the per-frame time budget with room left for the
rest of the UI at 24 fps.

The renderer is exercised directly through _render_buffer — that is the
exact entry point paintEvent uses, minus the upscale blit that Qt owns.
Widget tone fields are assigned directly instead of going through the
theming manager so the matrix is deterministic and theme-order
independent.
"""
import os
import sys
import tempfile
import time
import unittest

import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from tide.ui.central_bg import CentralBg, _bg_tone


ALL_SLUGS = [
    "field", "band", "vbeam", "horizon", "lightning", "depths",
    "rimlight", "liquid", "aurora", "smoke", "caustics",
]

SIZES = [(1280, 800), (800, 1280), (300, 200), (40, 30)]
PULSES = [0.0, 0.9]
MOTIONS = ["off", "lite", "full"]

# (name, bg, tone lightness/saturation triples matching _on_theme's bands)
DARK_BG = QColor("#0b0b0b")
LIGHT_BG = QColor("#f2f2f2")
GLASS_BG = QColor(12, 12, 16, 180)

# Frame budget at the biggest size. The animation timer runs at ~24 fps
# (~42 ms); staying under 10 ms leaves the rest of the tick for the UI.
BUDGET_MS = 10.0


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _tones_for(bg: QColor) -> tuple[QColor, QColor, QColor]:
    """Album-ish tones placed the way _on_theme places them: a step
    lighter than a dark bg, a step darker than a light one."""
    if bg.lightnessF() > 0.5:
        return (_bg_tone(QColor("#3a4a5a"), 0.78, 0.22),
                _bg_tone(QColor("#e08030"), 0.70, 0.28),
                _bg_tone(QColor("#ff5060"), 0.62, 0.32))
    return (_bg_tone(QColor("#3a4a5a"), 0.22, 0.34),
            _bg_tone(QColor("#e08030"), 0.29, 0.38),
            _bg_tone(QColor("#ff5060"), 0.34, 0.42))


def _synth_cover() -> QImage:
    """A stand-in album cover for the liquid style: a dark ground with a
    few big color masses, which is all the style's blur keeps anyway."""
    img = QImage(300, 300, QImage.Format_RGB32)
    img.fill(QColor("#1c2733"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#d4713a"))
    p.drawEllipse(30, 40, 160, 160)
    p.setBrush(QColor("#3a8fa0"))
    p.drawEllipse(140, 120, 150, 170)
    p.setBrush(QColor("#e8c95e"))
    p.drawEllipse(90, 200, 110, 90)
    p.end()
    return img


def _make_bg(slug: str, motion: str, bg: QColor, pulse: float) -> CentralBg:
    w = CentralBg(QWidget())
    w._enabled = True
    w.set_style(slug)
    assert w._style == slug, f"set_style rejected {slug!r}"
    w.set_motion(motion)
    ta, tb, tc = _tones_for(bg)
    w._bg = QColor(bg)
    # Displayed tones AND targets, so a later _snap_tones can't drift them.
    w._tone_a = QColor(ta)
    w._tone_b = QColor(tb)
    w._tone_c = QColor(tc)
    w._tone_ta = QColor(ta)
    w._tone_tb = QColor(tb)
    w._tone_tc = QColor(tc)
    w._pulse = pulse
    w._pulse_shown = pulse
    if slug == "liquid":
        w.set_art(_synth_cover())
    return w


def _mean_and_max(img: QImage) -> tuple[float, int]:
    im = img.convertToFormat(QImage.Format_RGB888)
    raw = bytes(im.constBits()[: im.bytesPerLine() * im.height()])
    arr = (np.frombuffer(raw, np.uint8)
           .reshape(im.height(), im.bytesPerLine())[:, : im.width() * 3])
    return float(arr.mean()), int(arr.max())


class BackdropStyleMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_all_styles_render_across_the_matrix(self) -> None:
        for slug in ALL_SLUGS:
            for bg in (DARK_BG, LIGHT_BG, GLASS_BG):
                for motion in MOTIONS:
                    for pulse in PULSES:
                        w = _make_bg(slug, motion, bg, pulse)
                        for size in SIZES:
                            with self.subTest(slug=slug, size=size,
                                              pulse=pulse, motion=motion,
                                              bg=bg.name(QColor.HexArgb)):
                                img = w._render_buffer(*size)
                                self.assertIsNotNone(img)
                                self.assertFalse(img.isNull())

    def test_dark_frames_stay_in_the_legible_band(self) -> None:
        # Dark opaque theme: the scene must be visible (not the bare bg,
        # not black) and must never wash out the content sitting on it.
        for slug in ALL_SLUGS:
            for pulse in PULSES:
                w = _make_bg(slug, "full", DARK_BG, pulse)
                # Let a little scene time pass so time-born features
                # (motes, curtains) exist.
                w._fx_phase = 2.0
                w._liq_phase = 2.0
                w._t0 -= 2.0
                img = w._render_buffer(1280, 800)
                mean, peak = _mean_and_max(img)
                with self.subTest(slug=slug, pulse=pulse):
                    self.assertGreaterEqual(
                        mean, 4.0, f"{slug} renders near-black")
                    self.assertLessEqual(
                        mean, 130.0, f"{slug} is blown out")
                    self.assertGreaterEqual(
                        peak, 20, f"{slug} painted nothing over the bg")

    def test_frame_budget_at_full_size(self) -> None:
        for slug in ALL_SLUGS:
            w = _make_bg(slug, "full", DARK_BG, 0.5)
            for _ in range(3):                     # warm caches / grids
                w._render_buffer(1280, 800)
            t0 = time.perf_counter()
            frames = 30
            for _ in range(frames):
                w._render_buffer(1280, 800)
            per_frame_ms = (time.perf_counter() - t0) / frames * 1000.0
            with self.subTest(slug=slug):
                self.assertLess(
                    per_frame_ms, BUDGET_MS,
                    f"{slug} spends {per_frame_ms:.2f} ms per frame")

    def test_save_style_previews(self) -> None:
        # One PNG per style so a human can eyeball the scenes. Not an
        # assertion of beauty — just that a frame with motion history and
        # a live pulse encodes and lands on disk.
        out_dir = os.environ.get("TIDE_BACKDROP_PNG_DIR") or os.path.join(
            tempfile.gettempdir(), "tide-backdrop-styles")
        os.makedirs(out_dir, exist_ok=True)
        for slug in ALL_SLUGS:
            w = _make_bg(slug, "full", DARK_BG, 0.6)
            w._fx_phase = 4.0
            w._liq_phase = 4.0
            w._t0 -= 4.0
            if slug == "lightning":
                # Freeze a strike mid-flash so the preview shows the bolt.
                w._bolt_t0 = (time.monotonic() - w._t0) - 0.06
                w._next_auto_strike = float("inf")
            img = w._render_buffer(640, 400)
            big = img.scaled(640, 400, Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
            path = os.path.join(out_dir, f"{slug}.png")
            with self.subTest(slug=slug):
                self.assertTrue(big.save(path), f"could not save {path}")


if __name__ == "__main__":
    unittest.main()
