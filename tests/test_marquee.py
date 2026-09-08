"""A title too long for its box must not just lose its tail.

The reported bug: song titles rendering as ``i'll take care of you
(feat. y…``. The fix is two-layered — the text scrolls when it overflows,
and the full string is always on the tooltip underneath, which is what
covers motion OFF and the moment the marquee is parked at the head.

Contracts pinned here: the scroll is motion-gated (OFF and reduced-motion
never create a timer at all), ``set_metrics`` is idempotent so a
per-frame scramble can't pin the cycle at frame zero, the offset stays
inside the overflow, and text that fits costs nothing.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_marquee.py
"""
import sys
import unittest

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide.ui import motion
from tide.ui.marquee import Marquee
from tide.ui.widgets import NowPlayingLabel


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _MarqueeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self._prev = motion._user_intensity
        self._prev_reduced = motion._reduced_motion
        self.addCleanup(motion.set_intensity, self._prev)
        self.addCleanup(setattr, motion, "_reduced_motion", self._prev_reduced)
        motion._reduced_motion = False
        motion.set_intensity(motion.Intensity.LITE)
        self.mq = Marquee()
        self.addCleanup(self.mq.stop)


class GateTests(_MarqueeCase):
    def test_text_that_fits_never_scrolls(self) -> None:
        self.mq.set_metrics(80.0, 200.0)
        self.assertFalse(self.mq.active())
        self.assertEqual(self.mq.offset(), 0.0)
        self.assertEqual(self.mq.overflow(), 0.0)

    def test_overflow_starts_the_scroll(self) -> None:
        self.mq.set_metrics(400.0, 200.0)
        self.assertTrue(self.mq.active())
        self.assertEqual(self.mq.overflow(), 200.0)

    def test_a_sliver_of_overflow_is_not_worth_animating(self) -> None:
        self.mq.set_metrics(202.0, 200.0)
        self.assertFalse(self.mq.active())

    def test_motion_off_never_creates_a_timer(self) -> None:
        motion.set_intensity(motion.Intensity.OFF)
        self.mq.set_metrics(400.0, 200.0)
        self.assertFalse(self.mq.active())
        self.assertEqual(self.mq.offset(), 0.0)
        self.assertIsNone(self.mq._timer)

    def test_reduced_motion_never_creates_a_timer(self) -> None:
        # A marquee runs forever, so reduced-motion is the one signal that
        # most clearly asks us to drop it — LITE is not a free pass here.
        motion._reduced_motion = True
        motion.set_intensity(motion.Intensity.LITE)
        self.mq.set_metrics(400.0, 200.0)
        self.assertFalse(self.mq.active())

    def test_growing_the_box_stops_the_scroll(self) -> None:
        self.mq.set_metrics(400.0, 200.0)
        self.assertTrue(self.mq.active())
        self.mq.set_metrics(400.0, 500.0)
        self.assertFalse(self.mq.active())

    def test_stop_is_idempotent(self) -> None:
        self.mq.set_metrics(400.0, 200.0)
        self.mq.stop()
        self.mq.stop()
        self.assertFalse(self.mq.active())


class CycleTests(_MarqueeCase):
    def test_identical_metrics_do_not_restart_the_cycle(self) -> None:
        # The scramble cascade rewrites the title every frame with the
        # same target; if that reset the clock the scroll would never
        # leave the head of the string.
        self.mq.set_metrics(400.0, 200.0)
        QTest.qWait(250)
        for _ in range(20):
            self.mq.set_metrics(400.0, 200.0)
        QTest.qWait(2200)
        moved = self.mq.offset()
        self.assertGreater(moved, 0.0,
                           "repeat set_metrics pinned the scroll at zero")

    def test_offset_never_exceeds_the_overflow(self) -> None:
        self.mq.set_metrics(320.0, 200.0)
        for _ in range(60):
            QTest.qWait(60)
            self.assertGreaterEqual(self.mq.offset(), 0.0)
            self.assertLessEqual(self.mq.offset(), self.mq.overflow() + 0.01)

    def test_it_dwells_at_the_head_before_setting_off(self) -> None:
        self.mq.set_metrics(400.0, 200.0)
        QTest.qWait(200)
        self.assertEqual(self.mq.offset(), 0.0)

    def test_it_actually_travels(self) -> None:
        self.mq.set_metrics(320.0, 200.0)
        seen = set()
        for _ in range(90):
            QTest.qWait(60)
            seen.add(round(self.mq.offset()))
        self.assertGreater(len(seen), 5, "the marquee never moved")
        self.assertGreater(max(seen), 20)

    def test_turning_motion_off_mid_scroll_parks_it(self) -> None:
        self.mq.set_metrics(400.0, 200.0)
        QTest.qWait(1800)
        motion.set_intensity(motion.Intensity.OFF)
        QTest.qWait(120)
        self.assertFalse(self.mq.active())
        self.assertEqual(self.mq.offset(), 0.0)


class LabelTests(_MarqueeCase):
    """The strip label is the host that matters — it is the thing that
    names the track."""

    def _label(self, width: int = 200) -> NowPlayingLabel:
        lbl = NowPlayingLabel()
        lbl.resize(width, 44)
        self.addCleanup(lbl.deleteLater)
        return lbl

    def test_tooltip_carries_the_full_text(self) -> None:
        lbl = self._label()
        lbl.setTrack("someone with a long name",
                     "i'll take care of you (feat. yebba)", "an album")
        self.assertIn("i'll take care of you (feat. yebba)", lbl.toolTip())
        self.assertNotIn("…", lbl.toolTip())

    def test_tooltip_is_there_even_at_motion_off(self) -> None:
        motion.set_intensity(motion.Intensity.OFF)
        lbl = self._label()
        lbl.setTrack("a", "i'll take care of you (feat. yebba)")
        lbl.grab()
        self.assertFalse(lbl._marquee.active())
        self.assertIn("i'll take care of you (feat. yebba)", lbl.toolTip())

    def test_the_animated_path_updates_the_tooltip_too(self) -> None:
        # setTrackAnimated is what the window actually calls on a track
        # change. It commits the target values and hands the label to the
        # scramble; refreshing the tooltip only in setTrack left it
        # showing the PREVIOUS song for the whole life of the new one.
        motion.set_intensity(motion.Intensity.FULL)
        lbl = self._label()
        lbl.setTrackAnimated("artist one", "first song")
        self.assertIn("first song", lbl.toolTip())
        lbl.setTrackAnimated("someone with a long name",
                             "i'll take care of you (feat. yebba)")
        self.assertIn("i'll take care of you (feat. yebba)", lbl.toolTip())
        self.assertNotIn("first song", lbl.toolTip())

    def test_painting_a_long_title_starts_the_scroll(self) -> None:
        lbl = self._label()
        lbl.setTrack("someone with a long name",
                     "i'll take care of you (feat. yebba)")
        lbl.grab()      # the measure happens in paintEvent
        self.assertTrue(lbl._marquee.active())

    def test_a_short_title_leaves_the_marquee_parked(self) -> None:
        lbl = self._label(600)
        lbl.setTrack("a", "b")
        lbl.grab()
        self.assertFalse(lbl._marquee.active())

    def test_clear_parks_the_scroll(self) -> None:
        lbl = self._label()
        lbl.setTrack("someone with a long name",
                     "i'll take care of you (feat. yebba)")
        lbl.grab()
        lbl.clear()
        self.assertFalse(lbl._marquee.active())
        self.assertEqual(lbl.toolTip(), "")

    def test_hiding_parks_the_scroll(self) -> None:
        # Qt only delivers a hide event to a widget that was actually
        # shown, so this has to go on screen first to exercise the path.
        lbl = self._label()
        lbl.show()
        QTest.qWaitForWindowExposed(lbl)
        lbl.setTrack("someone with a long name",
                     "i'll take care of you (feat. yebba)")
        lbl.grab()
        self.assertTrue(lbl._marquee.active())
        lbl.hide()
        self.assertFalse(lbl._marquee.active())

    def test_every_now_label_variant_paints_and_tooltips(self) -> None:
        from tide.ui.variants import make_now_label
        for slug in ("stacked", "inline", "centered", "headline"):
            with self.subTest(variant=slug):
                lbl = make_now_label(slug)
                self.addCleanup(lbl.deleteLater)
                lbl.resize(200, 44)
                lbl.setTrack("someone with a long name",
                             "i'll take care of you (feat. yebba)", "album")
                lbl.grab()      # must not raise
                self.assertIn("yebba)", lbl.toolTip())


if __name__ == "__main__":
    unittest.main()
