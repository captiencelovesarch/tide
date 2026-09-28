"""Album-art transitions (ui/art_fx.py) and the AlbumArt plumbing.

Covers the style registry and its fallbacks, that every style composes a
frame the size of the incoming cover at every point of the curve, the
motion OFF / style off snaps, AlbumArt landing on a fresh render after a
transition, the circle and polaroid variants transitioning between the
shapes they actually draw, a second cover mid-flight, the empty state
cancelling a transition, and Tween's stall tolerance (the reason art
transitions don't ride a wall-clock animation).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import time
import unittest

from PySide6.QtCore import QEasingCurve, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide.settings import Settings
from tide.ui import art_fx, motion
from tide.ui.widgets import AlbumArt


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _image(color: str, size: int = 64) -> QImage:
    img = QImage(size, size, QImage.Format_RGB32)
    img.fill(QColor(color))
    return img


def _pix(color: str, size: int = 48) -> QPixmap:
    return QPixmap.fromImage(_image(color, size))


class _MotionCase(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._style0 = art_fx.style()
        self._intensity0 = motion.intensity()
        motion.set_intensity("lite")

    def tearDown(self) -> None:
        art_fx.set_style(self._style0)
        motion.set_intensity(self._intensity0)


class RegistryTest(_MotionCase):
    def test_flip_is_the_default(self) -> None:
        self.assertEqual(art_fx.DEFAULT_STYLE, "flip")
        self.assertEqual(Settings().art_transition, "flip")

    def test_unknown_style_falls_back(self) -> None:
        art_fx.set_style("wobble")
        self.assertEqual(art_fx.style(), art_fx.DEFAULT_STYLE)

    def test_motion_off_means_instant(self) -> None:
        art_fx.set_style("blocks")
        motion.set_intensity("off")
        self.assertEqual(art_fx.effective_style(), "off")

    def test_choices_cover_every_style(self) -> None:
        self.assertEqual([slug for slug, _ in art_fx.choices()],
                         list(art_fx.STYLES))


class FrameTest(_MotionCase):
    def test_every_style_composes_at_every_step(self) -> None:
        old, new = _pix("#204060"), _pix("#c04030")
        for name in art_fx.STYLES:
            for step in range(11):
                prog = step / 10.0
                with self.subTest(style=name, prog=prog):
                    out = art_fx.frame(name, old, new, prog)
                    self.assertFalse(out.isNull())
                    self.assertEqual(out.size(), new.size())

    def test_the_last_frame_is_the_new_cover(self) -> None:
        old, new = _pix("#204060"), _pix("#c04030")
        for name in art_fx.STYLES:
            with self.subTest(style=name):
                self.assertIs(art_fx.frame(name, old, new, 1.0), new)

    def test_flip_shows_the_old_face_first_and_the_new_face_last(self) -> None:
        motion.set_profile("springy")
        try:
            old, new = _pix("#0000ff"), _pix("#ff0000")
            early = art_fx.frame("flip", old, new, 0.1).toImage()
            late = art_fx.frame("flip", old, new, 0.95).toImage()
            c = early.width() // 2
            self.assertGreater(early.pixelColor(c, c).blue(),
                               early.pixelColor(c, c).red())
            self.assertGreater(late.pixelColor(c, c).red(),
                               late.pixelColor(c, c).blue())
        finally:
            motion.set_profile(None)

    def test_blocks_swap_at_the_coarsest_rung(self) -> None:
        old, new = _pix("#0000ff"), _pix("#ff0000")
        mid_old = art_fx.frame("blocks", old, new, 0.45).toImage()
        mid_new = art_fx.frame("blocks", old, new, 0.55).toImage()
        self.assertEqual(mid_old.pixelColor(5, 5).blue(), 255)
        self.assertEqual(mid_new.pixelColor(5, 5).red(), 255)


class AlbumArtTest(_MotionCase):
    def _wait_for_landing(self, art: AlbumArt) -> None:
        deadline = time.monotonic() + 3.0
        while art_fx.running(art) and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertFalse(art_fx.running(art), "transition never landed")

    def test_first_cover_snaps(self) -> None:
        art = AlbumArt(64)
        art.setImage(_image("#336699"))
        self.assertFalse(art_fx.running(art))
        self.assertFalse(art.pixmap().isNull())

    def test_style_off_snaps(self) -> None:
        art_fx.set_style("off")
        art = AlbumArt(64)
        art.setImage(_image("#336699"))
        art.setImage(_image("#993322"))
        self.assertFalse(art_fx.running(art))

    def test_motion_off_snaps(self) -> None:
        art_fx.set_style("flip")
        motion.set_intensity("off")
        art = AlbumArt(64)
        art.setImage(_image("#336699"))
        art.setImage(_image("#993322"))
        self.assertFalse(art_fx.running(art))
        c = art.width() // 2
        self.assertEqual(art.pixmap().toImage().pixelColor(c, c).name(),
                         "#993322")

    def test_every_style_lands_on_the_new_cover(self) -> None:
        for name in ("flip", "slide", "pop", "blocks", "fade"):
            with self.subTest(style=name):
                art_fx.set_style(name)
                art = AlbumArt(64)
                art.setImage(_image("#336699"))
                art.setImage(_image("#993322"))
                self.assertTrue(art_fx.running(art))
                self._wait_for_landing(art)
                c = art.width() // 2
                self.assertEqual(
                    art.pixmap().toImage().pixelColor(c, c).name(), "#993322")

    def test_a_second_cover_mid_flight_takes_over(self) -> None:
        art_fx.set_style("flip")
        art = AlbumArt(64)
        art.setImage(_image("#336699"))
        art.setImage(_image("#993322"))
        QTest.qWait(40)
        art.setImage(_image("#22aa44"))
        self._wait_for_landing(art)
        c = art.width() // 2
        self.assertEqual(art.pixmap().toImage().pixelColor(c, c).name(),
                         "#22aa44")

    def test_clearing_the_art_cancels_the_transition(self) -> None:
        art_fx.set_style("fade")
        art = AlbumArt(64)
        art.setImage(_image("#336699"))
        art.setImage(_image("#993322"))
        art.setImage(None)
        self.assertFalse(art_fx.running(art))
        QTest.qWait(80)
        self.assertTrue(art.pixmap().isNull(),
                        "a cancelled frame painted over the empty state")

    def test_circle_variant_stays_round(self) -> None:
        # Used to crossfade to (and settle on) a square cover until the
        # next theme change re-rendered it through the circle path.
        from tide.ui.variants import CircleAlbumArt
        art_fx.set_style("fade")
        art = CircleAlbumArt(64)
        art.setImage(_image("#336699"))
        art.setImage(_image("#993322"))
        self._wait_for_landing(art)
        corner = art.pixmap().toImage().pixelColor(1, 1)
        self.assertEqual(corner.alpha(), 0, "circle art came out square")

    def test_unframed_art_has_no_ground(self) -> None:
        # The mini and fullscreen covers turn over a backdrop; a bg fill
        # behind them showed as a slab mid-flip.
        art = AlbumArt(64)
        art.set_framed(False)
        self.assertIn("background: transparent", art.styleSheet())


class ArtSlotSwapTest(_MotionCase):
    def test_swapping_the_art_style_keeps_the_cover(self) -> None:
        from tide import settings as settings_module
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.ui.variants import CircleAlbumArt
        from tide.ui.window import MainWindow
        real_save = settings_module.save
        settings_module.save = lambda s: None
        try:
            router = PlaybackRouter()
            router.register(MpvBackend())
            w = MainWindow(LocalSource(), router)
            w.art.setImage(_image("#993322"))
            w._swap_album_art("circle")
            self.assertIsInstance(w.art, CircleAlbumArt)
            self.assertFalse(w.art.pixmap().isNull(),
                             "the new tile came up on 'no art'")
            w.close()
        finally:
            settings_module.save = real_save


class TweenTest(_MotionCase):
    def test_a_stall_pauses_instead_of_skipping(self) -> None:
        seen: list[float] = []
        tw = motion.tween(0.0, 1.0, on_update=seen.append, dur=400,
                          easing=QEasingCurve(QEasingCurve.Linear))
        self.assertIsNotNone(tw)
        # Pretend the GUI thread just froze for 300 ms.
        tw._last = time.monotonic() - 0.3
        tw._tick()
        self.assertLessEqual(seen[-1], 0.1 + 1e-6,
                             "a 300 ms stall jumped the tween ahead")
        tw.stop()

    def test_off_snaps_to_the_end(self) -> None:
        motion.set_intensity("off")
        seen: list[float] = []
        done: list[bool] = []
        tw = motion.tween(0.0, 1.0, on_update=seen.append,
                          on_done=lambda: done.append(True))
        self.assertIsNone(tw)
        self.assertEqual(seen, [1.0])
        self.assertEqual(done, [True])


if __name__ == "__main__":
    unittest.main()
