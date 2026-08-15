"""The fx panel gets a rail tab and the sleep timer gets a face.

Regressions being pinned: the audio FX panel was a full stack view
reachable only via hotkey (never on the nav rail), the sleep timer's only
entry point was Ctrl+I with no visible surface anywhere, and Ctrl+6 was a
ghost duplicate of home left over from the explore→home merge — so the
digit row visibly skipped a number relative to the rail.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.playback import MpvBackend, PlaybackRouter
from tide.sources.local import LocalSource
from tide.ui import nav_icons


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _window():
    _app()
    theming.manager().refresh()
    theming.manager().apply("nord")
    from tide.ui.window import MainWindow
    router = PlaybackRouter()
    router.register(MpvBackend())
    return MainWindow(LocalSource(), router)


class FxNavTabTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = _window()

    def tearDown(self) -> None:
        self.w.close()
        QTest.qWait(20)

    def test_fx_button_is_on_the_rail_and_opens_the_panel(self) -> None:
        btn = self.w.nav_fx_btn
        self.assertIsNotNone(btn.parentWidget())
        btn.click()
        QTest.qWait(20)
        self.assertIs(self.w.stack.currentWidget(), self.w.audio_fx_view)

    def test_digit_row_mirrors_the_rail_order(self) -> None:
        self.w.show()
        QTest.qWait(30)
        for key, attr in ((Qt.Key_6, "visualizer_view"),
                          (Qt.Key_7, "source_view"),
                          (Qt.Key_8, "audio_fx_view")):
            QTest.keyClick(self.w, key, Qt.ControlModifier)
            QTest.qWait(20)
            self.assertIs(self.w.stack.currentWidget(),
                          getattr(self.w, attr),
                          f"Ctrl+{key - Qt.Key_0} landed on the wrong view")

    def test_every_nav_slot_has_an_svg_icon(self) -> None:
        for slot in self.w._nav_buttons:
            self.assertIsNotNone(nav_icons.svg_text_for(slot),
                                 f"no svg icon for nav slot {slot!r}")


class SleepButtonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = _window()

    def tearDown(self) -> None:
        self.w.close()
        QTest.qWait(20)

    def test_sleep_button_lives_in_the_strip(self) -> None:
        self.assertIsNotNone(self.w.sleep_btn.parentWidget())

    def test_label_tracks_the_armed_state(self) -> None:
        from tide.ui.sleep_timer import SleepMode
        w = self.w
        w._sleep_start(SleepMode.MINUTES, 5)
        self.assertIn("5m", w.sleep_btn.text())
        w._sleep_cancel(silent=True)
        self.assertNotIn("5m", w.sleep_btn.text())
        w._sleep_start(SleepMode.AFTER_SONG, 0)
        self.assertIn("song", w.sleep_btn.text())
        w._sleep_cancel(silent=True)

    def test_label_survives_a_strip_rebuild(self) -> None:
        """The v1.3.1 keep-list bug class: a layout-mode switch must not
        orphan the sleep button (or its armed label)."""
        from tide.ui.sleep_timer import SleepMode
        w = self.w
        w._sleep_start(SleepMode.MINUTES, 12)
        w._rebuild_strip("compact")
        QTest.qWait(20)
        self.assertIsNotNone(w.sleep_btn.parentWidget())
        self.assertIn("12m", w.sleep_btn.text())
        w._rebuild_strip("classic")
        w._sleep_cancel(silent=True)


if __name__ == "__main__":
    unittest.main()
