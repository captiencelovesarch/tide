"""2.3 speed bubble — the pill grows a speech bubble whose stem IS the pill.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_speed_bubble.py
"""
import sys
import unittest
from types import SimpleNamespace

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

from tide import theming
from tide.ui import motion
from tide.ui.speed import SpeedButton
from tide.ui.speed_bubble import SpeedBubble, bubble_path


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class PathTests(unittest.TestCase):
    def test_path_is_one_shape_covering_panel_and_stem(self) -> None:
        panel = QRectF(0, 0, 300, 100)
        stem = QRectF(120, 106, 60, 28)
        path = bubble_path(panel, stem, radius=12, stem_radius=14, fillet=10)
        for pt in (QPointF(150, 50), QPointF(150, 120), QPointF(150, 103)):
            self.assertTrue(path.contains(pt), pt)
        # the fillet fills the concave corner beside the neck...
        self.assertTrue(path.contains(QPointF(117, 101)))
        # ...but not far out under the panel
        self.assertFalse(path.contains(QPointF(60, 120)))

    def test_brutalist_has_no_fillet(self) -> None:
        panel = QRectF(0, 0, 300, 100)
        stem = QRectF(120, 106, 60, 28)
        path = bubble_path(panel, stem, radius=0, stem_radius=0, fillet=0)
        self.assertFalse(path.contains(QPointF(117, 102)))
        self.assertTrue(path.contains(QPointF(150, 103)))

    def test_upward_variant(self) -> None:
        stem = QRectF(120, 0, 60, 28)
        panel = QRectF(0, 34, 300, 100)
        path = bubble_path(panel, stem, radius=12, stem_radius=14, fillet=10,
                           down=False)
        self.assertTrue(path.contains(QPointF(150, 31)))


class BubbleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self._prev = motion._user_intensity
        motion.set_intensity(motion.Intensity.OFF)
        self.addCleanup(motion.set_intensity, self._prev)
        self.host = QWidget()
        self.host._settings = SimpleNamespace(preset="modern")
        self.host.resize(600, 400)
        self.host.move(100, 400)
        col = QVBoxLayout(self.host)
        col.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn = SpeedButton()
        row.addWidget(self.btn)
        row.addStretch(1)
        col.addLayout(row)
        self.host.show()
        QTest.qWait(20)
        self.spy: list[float] = []
        self.btn.speed_changed.connect(self.spy.append)

    def tearDown(self) -> None:
        if self.btn._popover is not None:
            self.btn._popover.hide()
        self.host.deleteLater()
        QTest.qWait(20)

    def _open(self) -> SpeedBubble:
        self.btn._open_popover()
        QTest.qWait(10)
        return self.btn._popover

    def test_stem_lands_exactly_on_the_pill(self) -> None:
        bub = self._open()
        pill = QRect(self.btn.mapToGlobal(QPoint(0, 0)), self.btn.size())
        stem = bub._stem.toAlignedRect().translated(bub.geometry().topLeft())
        self.assertLessEqual(abs(stem.left() - pill.left()), 1)
        self.assertLessEqual(abs(stem.bottom() - pill.bottom()), 1)
        self.assertLessEqual(abs(stem.width() - pill.width()), 1)
        # the panel sits above the pill
        self.assertLess(bub._panel.bottom(), bub._stem.top())

    def test_value_big_and_controls(self) -> None:
        self.btn.set_speed(1.25, emit=False)
        bub = self._open()
        self.assertEqual(bub.display.text(), "1.25×")
        self.assertGreater(bub.display.font().pointSizeF(),
                           bub.reset_btn.font().pointSizeF())
        bub.plus_btn.click()
        self.assertAlmostEqual(self.btn.speed(), 1.3)
        bub.minus_btn.click()
        bub.minus_btn.click()
        self.assertAlmostEqual(self.btn.speed(), 1.2)
        bub.reset_btn.click()
        self.assertEqual(self.btn.speed(), 1.0)
        self.assertFalse(bub.reset_btn.isEnabled())

    def test_slider_has_detents_and_no_value_bubble(self) -> None:
        bub = self._open()
        self.assertTrue(bub.slider._detents)
        self.assertIsNone(bub.slider._bubble_rect("1×"))

    def test_press_on_stem_closes(self) -> None:
        bub = self._open()
        self.assertTrue(bub.isVisible())
        QTest.mouseClick(bub, motion_button(), pos=bub._stem.center().toPoint())
        QTest.qWait(10)
        self.assertFalse(bub.isVisible())

    def test_brutalist_square_face(self) -> None:
        theming.manager().refresh()
        theming.manager().apply("brutalist-mono")
        bub = self._open()
        self.assertTrue(bub.slider._square)
        theming.manager().apply("adaptive")
        QTest.qWait(10)
        self.assertFalse(bub.slider._square)

    def test_grow_reaches_full_panel(self) -> None:
        bub = self._open()
        self.assertEqual(bub._grow, 1.0)
        self.assertEqual(bub._grown_panel(), bub._panel)
        bub._set_grow(0.0)
        start = bub._grown_panel()
        self.assertAlmostEqual(start.width(), bub._stem.width(), delta=1.0)
        bub._set_grow(1.0)


def motion_button():
    from PySide6.QtCore import Qt
    return Qt.LeftButton


if __name__ == "__main__":
    unittest.main()
