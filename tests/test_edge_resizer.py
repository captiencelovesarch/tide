"""The frameless edge resizer must not ride every event in the app.

It used to be a QApplication-wide event filter: a Python call for every
event of every widget, ~24k per stylesheet repolish, which was ~40% of
the restyle each song change pays (measured 270ms → 165ms without it).
It now filters the window's QWindow, which still sees each press before
a child widget can take it.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from tide.ui import titlebar


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class EdgeResizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.win = QWidget()
        self.win.setWindowFlag(Qt.FramelessWindowHint, True)
        lay = QVBoxLayout(self.win)
        lay.setContentsMargins(0, 0, 0, 0)
        # wall to wall, like the real shell
        lay.addWidget(QLabel("content"))
        self.win.resize(400, 300)
        self.win.show()
        QTest.qWaitForWindowExposed(self.win)
        self.resizer = titlebar.EdgeResizer(self.win)
        self.calls = []
        self.enterContext(mock.patch.object(
            QWindow, "startSystemResize",
            lambda handle, edges: self.calls.append(edges) or True))
        self.addCleanup(self.win.deleteLater)

    def test_press_on_the_edge_starts_a_resize(self) -> None:
        QTest.mousePress(self.win.windowHandle(), Qt.LeftButton, pos=QPoint(399, 150))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0] & Qt.RightEdge)

    def test_press_inside_is_left_alone(self) -> None:
        QTest.mousePress(self.win.windowHandle(), Qt.LeftButton, pos=QPoint(200, 150))
        QTest.mouseRelease(self.win.windowHandle(), Qt.LeftButton, pos=QPoint(200, 150))
        self.assertEqual(self.calls, [])

    def test_repolish_does_not_call_the_filter(self) -> None:
        lay = self.win.layout()
        for i in range(200):
            lay.addWidget(QLabel(str(i)))
        QTest.qWait(10)
        seen = []
        real = titlebar.EdgeResizer.eventFilter

        def counting(this, obj, event):
            seen.append(event.type())
            return real(this, obj, event)

        self.resizer.detach()
        with mock.patch.object(titlebar.EdgeResizer, "eventFilter", counting):
            # built under the patch: shiboken resolves overrides per instance
            self.resizer = titlebar.EdgeResizer(self.win)
            self.app.setStyleSheet("QLabel { color: #123456; }")
            self.app.setStyleSheet("")
        self.resizer.detach()
        # only the window widget's own few events, never the per-widget flood
        self.assertLess(len(seen), 20)

    def test_rebuilt_native_window_is_followed(self) -> None:
        self.win.hide()
        self.win.destroy()
        self.win.show()
        QTest.qWaitForWindowExposed(self.win)
        QTest.mousePress(self.win.windowHandle(), Qt.LeftButton, pos=QPoint(0, 150))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0] & Qt.LeftEdge)

    def test_detach_stops_resizing(self) -> None:
        self.resizer.detach()
        QTest.mousePress(self.win.windowHandle(), Qt.LeftButton, pos=QPoint(399, 150))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
