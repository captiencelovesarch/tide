"""modern cards lift on hover through the motion profile; brutalist keeps
its instant halo.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_card_hover.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.sources.base import ShelfItem
from tide.ui import motion
from tide.ui.card import Card
from tide.ui.home import patterns


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Track:      # duck-typed like tide's Track
    pass


_Track.__name__ = "Track"


class HoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()

    def setUp(self) -> None:
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()
        self._intensity = motion.intensity()
        self._profile = motion.bound_profile()
        motion._reduced_motion = False
        self._widgets = []

    def tearDown(self) -> None:
        motion.set_intensity(self._intensity)
        motion.set_profile(None)
        motion.bind_preset(self._profile)
        for w in self._widgets:
            w.deleteLater()
        QTest.qWait(5)
        self._sheet.stop()

    def _card(self, kind="song"):
        c = Card("t", "s", "", ShelfItem(kind=kind, title="t"))
        self._widgets.append(c)
        return c

    def test_springy_hover_animates_to_one_and_back(self) -> None:
        theming.manager().apply("nord")
        motion.set_intensity("full")
        motion.set_profile("springy")
        c = self._card()
        c._set_hover(True)
        self.assertLess(c._hover_t, 1.0)             # mid-flight
        QTest.qWait(motion.dur("micro") + 120)
        self.assertAlmostEqual(c._hover_t, 1.0, places=2)
        c._set_hover(False)
        QTest.qWait(motion.dur("micro") + 120)
        self.assertAlmostEqual(c._hover_t, 0.0, places=2)

    def test_motion_off_snaps(self) -> None:
        theming.manager().apply("nord")
        motion.set_intensity("off")
        c = self._card()
        c._set_hover(True)
        self.assertEqual(c._hover_t, 1.0)

    def test_brutalist_never_animates(self) -> None:
        theming.manager().apply("brutalist-mono")
        motion.set_intensity("full")
        c = self._card()
        c._set_hover(True)
        self.assertEqual(c._hover_t, 1.0)
        self.assertNotIn("hover", getattr(c, "_motion_anims", {}))

    def test_playable_is_songs_videos_and_tracks(self) -> None:
        self.assertTrue(self._card("song")._playable())
        self.assertTrue(self._card("video")._playable())
        self.assertFalse(self._card("album")._playable())
        self.assertFalse(self._card("artist")._playable())
        c = Card("t", "s", "", _Track())
        self._widgets.append(c)
        self.assertTrue(c._playable())

    def test_hover_changes_the_paint_in_modern_only(self) -> None:
        for slug, expect_change in (("nord", True), ("brutalist-mono", False)):
            with self.subTest(theme=slug):
                theming.manager().apply(slug)
                motion.set_intensity("off")
                c = self._card()
                c.show()
                QTest.qWait(5)
                # offscreen spawns the widget under the cursor, so it may
                # already be "hovered": pin the baseline, and repaint before
                # each grab (grab alone hands back the stale store)
                c._hover_t = 0.0
                c.repaint()
                before = c.grab().toImage()
                c._hover_t = 1.0
                c.repaint()
                after = c.grab().toImage()
                # brutalist paints its halo from underMouse(), not _hover_t
                self.assertEqual(before != after, expect_change)

    def test_compact_row_hover_paints(self) -> None:
        theming.manager().apply("nord")
        motion.set_intensity("off")
        r = patterns.CompactTrackRow("t", "s", "", ShelfItem(kind="song", title="t"))
        self._widgets.append(r)
        r.show()
        QTest.qWait(5)
        r._hover_t = 0.0
        r.repaint()
        before = r.grab().toImage()
        r._set_hover(True)
        self.assertEqual(r._hover_t, 1.0)
        r.repaint()
        self.assertNotEqual(before, r.grab().toImage())


if __name__ == "__main__":
    unittest.main()
