"""tide-drawn titlebar (CSD) — frameless flag, chrome wiring, theming.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import settings as settings_module, theming
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui.titlebar import TitleBar
from tide.ui.window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class TitleBarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("nord")
        QTest.qWait(30)
        router = PlaybackRouter()
        router.register(MpvBackend())
        cls.w = MainWindow(LocalSource(), router)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.w.close()
        QTest.qWait(30)

    def test_enable_installs_titlebar_and_frameless(self) -> None:
        self.w.set_csd_titlebar(True)
        self.assertIsInstance(self.w.menuWidget(), TitleBar)
        self.assertTrue(self.w.windowFlags() & Qt.FramelessWindowHint)

    def test_disable_restores_native_decoration(self) -> None:
        self.w.set_csd_titlebar(True)
        self.w.set_csd_titlebar(False)
        self.assertIsNone(self.w.menuWidget())
        self.assertFalse(self.w.windowFlags() & Qt.FramelessWindowHint)
        self.w.set_csd_titlebar(True)   # leave on for the other tests

    def test_maximize_toggle_round_trips(self) -> None:
        self.w.set_csd_titlebar(True)
        bar = self.w.menuWidget()
        self.w.show()
        QTest.qWait(30)
        bar.toggle_maximized()
        QTest.qWait(30)
        self.assertTrue(self.w.isMaximized())
        bar.toggle_maximized()
        QTest.qWait(30)
        self.assertFalse(self.w.isMaximized())

    def test_glyphs_follow_theme_aesthetic(self) -> None:
        self.w.set_csd_titlebar(True)
        bar = self.w.menuWidget()
        theming.manager().apply("storm")        # brutalist, case=upper
        QTest.qWait(30)
        self.assertEqual(bar.close_btn.text(), "[✕]")
        self.assertEqual(bar.title.text(), "TIDE")
        theming.manager().apply("nord")         # modern
        QTest.qWait(30)
        self.assertEqual(bar.close_btn.text(), "✕")

    def test_titlebar_qss_defaults_are_injected(self) -> None:
        theme = theming.manager().current()
        qss = theming._substitute(theme.qss, theme)
        self.assertIn("#TitleBar", qss)
        self.assertNotIn("@bg_alt", qss.split("/*")[0])


class CsdSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None

    def tearDown(self) -> None:
        settings_module.save = self._real_save

    def test_toggle_round_trips_through_save(self) -> None:
        from tide.ui.settings import SettingsDialog
        s = Settings()
        s.csd_titlebar = True
        dlg = SettingsDialog(s)
        try:
            self.assertTrue(dlg.csd_toggle.isChecked())
            dlg.csd_toggle.setChecked(False)
            dlg._on_save()
            self.assertFalse(dlg._settings.csd_titlebar)
        finally:
            dlg.deleteLater()

    def test_default_is_on(self) -> None:
        self.assertTrue(Settings().csd_titlebar)


if __name__ == "__main__":
    unittest.main()
