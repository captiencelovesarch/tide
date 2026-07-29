"""Full-app restyle deferral regressions.

QApplication.setStyleSheet repolishes every live widget, and the widget style
runs its own code (QObject::connect included) during that repolish. Calling it
synchronously inside a signal emission deadlocked on PySide6 + py3.14 + Breeze
the moment an adaptive palette landed — the "tide froze then aborted for no
reason" crash. ThemeManager therefore must never push QSS inline: every restyle
is coalesced onto the next event-loop turn via _queue_restyle.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import theming


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class RestyleCoalesceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        # Fresh manager per test so scheduled flushes can't leak across tests.
        self.mgr = theming.ThemeManager()
        self.mgr.refresh()
        self.spy = mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        # Drain any pending flush so it fires against this test's spy, not
        # the next test's.
        QTest.qWait(20)

    def _pump(self) -> None:
        # singleShot(0) rides the timer queue — the loop has to run timers,
        # not just deliver posted events.
        QTest.qWait(20)

    def test_apply_never_sets_stylesheet_synchronously(self) -> None:
        self.mgr.apply("brutalist-mono")
        self.assertEqual(self.spy.call_count, 0,
                         "apply() pushed QSS inside the calling turn")
        self._pump()
        self.assertEqual(self.spy.call_count, 1)

    def test_override_burst_coalesces_to_one_repolish(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.spy.reset_mock()
        for accent in ("#111111", "#222222", "#333333"):
            self.mgr.override_tokens({"accent": accent})
        self.assertEqual(self.spy.call_count, 0,
                         "override_tokens pushed QSS inside the calling turn")
        self._pump()
        self.assertEqual(self.spy.call_count, 1,
                         "a burst of overrides must cost one repolish")
        # The one push carries the LAST value of the burst.
        (qss,), _ = self.spy.call_args
        self.assertIn("#333333", qss)
        self.assertNotIn("#111111", qss)

    def test_identical_qss_is_not_repushed(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.spy.reset_mock()
        self.mgr.override_tokens({"accent": "#123456"})
        self._pump()
        self.assertEqual(self.spy.call_count, 1)
        self.mgr.override_tokens({"accent": "#123456"})  # same value again
        self._pump()
        self.assertEqual(self.spy.call_count, 1,
                         "unchanged QSS repolished the whole app for nothing")

    def test_clear_accent_override_defers_too(self) -> None:
        self.mgr.apply("brutalist-mono")
        self._pump()
        self.mgr.override_tokens({"accent": "#123456"})
        self._pump()
        self.spy.reset_mock()
        self.mgr.clear_accent_override()
        self.assertEqual(self.spy.call_count, 0)
        self._pump()
        self.assertEqual(self.spy.call_count, 1)

    def test_theme_changed_still_fires_immediately(self) -> None:
        # Custom-painted widgets repaint off this signal; deferring the QSS
        # must not defer them.
        seen: list[object] = []
        self.mgr.theme_changed.connect(seen.append)
        self.mgr.apply("brutalist-mono")
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
