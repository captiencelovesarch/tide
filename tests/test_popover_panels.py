"""Spring popovers paint an opaque panel; a drag owns its handle; the
rail fold animates.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_popover_panels.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tide import material, theming
from tide.ui import motion, scale
from tide.ui.spring_slider import SpringSlider


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()

    def test_composite_flattens_alpha_onto_the_ground(self) -> None:
        c = material.composite("#000000", "#80ffffff")
        self.assertEqual((c.red(), c.green(), c.blue(), c.alpha()), (128, 128, 128, 255))
        self.assertEqual(material.composite("#123456", "not a colour").name(), "#123456")

    def test_spring_popovers_paint_opaque_panels(self) -> None:
        from tide.ui.speed import SpringSpeedPopover
        from tide.ui.audio_fx_popover import SpringAudioFxPopover
        theming.manager().apply("blackwater")           # bg #000: the hardest ground
        host = QWidget()
        host.show()
        self.addCleanup(host.deleteLater)
        fill, _line = material.panel_colors(theming.manager().current())
        self.assertGreater(fill.lightnessF(), 0.02, "panel must lift off pure black")
        for cls in (SpringSpeedPopover, SpringAudioFxPopover):
            with self.subTest(popover=cls.__name__):
                pop = cls(host)
                self.addCleanup(pop.deleteLater)
                pop.show()
                QTest.qWait(30)
                pop.repaint()
                img = pop.grab().toImage()
                px = img.pixelColor(pop.width() // 2, 4)
                self.assertEqual(px.alpha(), 255)
                self.assertEqual(px.name(), fill.name())
                pop.hide()


class DragOwnsHandleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        self._intensity = motion.intensity()
        motion.set_intensity("off")
        self.s = SpringSlider()
        self.s.set_range(0.0, 100.0, 1.0)
        self.s.set_detents([50.0])
        self.s.resize(400, 40)
        self.s.show()
        QTest.qWait(10)

    def tearDown(self) -> None:
        motion.set_intensity(self._intensity)
        self.s.deleteLater()
        QTest.qWait(5)

    def _press(self, x):
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, 20), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        self.s.mousePressEvent(ev)

    def _move(self, x):
        ev = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, 20), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        self.s.mouseMoveEvent(ev)

    def test_set_value_during_a_drag_leaves_the_display_alone(self) -> None:
        self._press(self.s._x_for_value(20.0))
        self._move(self.s._x_for_value(30.4))          # between steps: display is raw
        shown = self.s._display
        self.assertAlmostEqual(shown, 30.4, places=1)
        self.assertEqual(self.s.value(), 30.0)
        self.s.set_value(30.0)                          # a host echo mid-drag
        self.assertEqual(self.s._display, shown, "echo yanked the handle")
        self.s.set_value(31.0, animate=True)
        self.assertEqual(self.s._display, shown)
        self.assertEqual(self.s.value(), 31.0)
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(self.s._x_for_value(30.4), 20), Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        self.s.mouseReleaseEvent(ev)
        self.assertEqual(self.s._display, 31.0)         # release settles to the value

    def test_set_value_outside_a_drag_still_moves_the_display(self) -> None:
        self.s.set_value(70.0)
        self.assertEqual(self.s._display, 70.0)


class RailFoldAnimationTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()
        theming.manager().refresh()
        theming.manager().apply("nord")
        motion._reduced_motion = False
        self._intensity = motion.intensity()
        self._profile = motion.bound_profile()
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.settings import Settings
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        s = Settings()
        s.nav_icon_set = "svg"
        self.w._settings = s
        self.w.apply_nav_icons("svg")
        self.w.show()
        QTest.qWait(30)

    def tearDown(self) -> None:
        motion.set_intensity(self._intensity)
        motion.set_profile(None)
        motion.bind_preset(self._profile)
        self.w._settings = None
        self.w.close()
        self._sheet.stop()
        QTest.qWait(20)

    def test_fold_eases_the_width_and_unfold_brings_labels_back_last(self) -> None:
        motion.set_intensity("full")
        motion.set_profile("springy")
        wide, narrow = scale.px(176), scale.px(56)
        self.assertEqual(self.w._nav_frame.width(), wide)
        with mock.patch("tide.settings.save_fields"):
            self.w.toggle_nav_rail()
        # labels go first, width is still on its way
        self.assertEqual(self.w.nav_home_btn.text(), "")
        self.assertGreater(self.w._nav_frame.width(), narrow)
        QTest.qWait(motion.dur("short") + 150)
        self.assertEqual(self.w._nav_frame.width(), narrow)
        with mock.patch("tide.settings.save_fields"):
            self.w.toggle_nav_rail()
        # width opens first, labels return when it lands
        self.assertEqual(self.w.nav_home_btn.text(), "")
        QTest.qWait(motion.dur("short") + 150)
        self.assertEqual(self.w._nav_frame.width(), wide)
        self.assertEqual(self.w.nav_home_btn.text(), "home")

    def test_motion_off_folds_instantly(self) -> None:
        motion.set_intensity("off")
        with mock.patch("tide.settings.save_fields"):
            self.w.toggle_nav_rail()
        self.assertEqual(self.w._nav_frame.width(), scale.px(56))
        with mock.patch("tide.settings.save_fields"):
            self.w.toggle_nav_rail()
        self.assertEqual(self.w._nav_frame.width(), scale.px(176))
        self.assertEqual(self.w.nav_home_btn.text(), "home")


if __name__ == "__main__":
    unittest.main()
