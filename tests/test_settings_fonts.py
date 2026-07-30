"""v1.3 font feature — full-family picker, live preview, size override.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import settings as settings_module, theming
from tide.settings import Settings


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _proportional_family() -> str | None:
    latin = QFontDatabase.WritingSystem.Latin
    for f in QFontDatabase.families():
        if QFontDatabase.isFixedPitch(f) or f.startswith("."):
            continue
        systems = QFontDatabase.writingSystems(f)
        if systems and latin not in systems:
            continue    # the picker filters non-Latin faces out
        return f
    return None


class FontPickerTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        # The dialog previews through the process-global manager — pin its
        # state so tests can't leak font overrides into each other.
        theming.manager().set_user_font("")
        theming.manager().set_user_font_size(0)

    def tearDown(self) -> None:
        settings_module.save = self._real_save
        theming.manager().set_user_font("")
        theming.manager().set_user_font_size(0)
        QTest.qWait(10)

    def _dialog(self, s: Settings | None = None):
        from tide.ui.settings import SettingsDialog
        return SettingsDialog(s or Settings())

    def test_picker_lists_proportional_families(self) -> None:
        family = _proportional_family()
        if family is None:
            self.skipTest("no proportional fonts installed")
        dlg = self._dialog()
        try:
            self.assertGreaterEqual(
                dlg.font_picker.findData(family), 0,
                "non-monospace system families must be pickable",
            )
        finally:
            dlg.deleteLater()

    def test_rows_preview_their_own_face(self) -> None:
        dlg = self._dialog()
        try:
            # Row 1 is the first bundled font; its FontRole drives the
            # in-dropdown preview.
            item_font = dlg.font_picker.itemData(1, Qt.FontRole)
            self.assertIsInstance(item_font, QFont)
            self.assertEqual(item_font.family(), dlg.font_picker.itemData(1))
        finally:
            dlg.deleteLater()

    def test_selection_live_applies_and_cancel_reverts(self) -> None:
        family = _proportional_family()
        if family is None:
            self.skipTest("no proportional fonts installed")
        dlg = self._dialog()
        try:
            idx = dlg.font_picker.findData(family)
            dlg.font_picker.setCurrentIndex(idx)
            self.assertEqual(theming.manager().user_font(), family,
                             "picking a row must preview immediately")
            dlg._on_cancel()
            self.assertEqual(theming.manager().user_font(), "",
                             "cancel must restore the pre-dialog font")
        finally:
            dlg.deleteLater()

    def test_size_override_round_trips_through_save(self) -> None:
        dlg = self._dialog()
        try:
            dlg.font_size_spin.setValue(13)
            dlg._on_save()
            self.assertEqual(dlg._settings.font_size_override_pt, 13)
        finally:
            dlg.deleteLater()

    def test_size_zero_means_theme_default(self) -> None:
        s = Settings()
        s.font_size_override_pt = 0
        dlg = self._dialog(s)
        try:
            self.assertEqual(dlg.font_size_spin.value(), 0)
            self.assertEqual(dlg.font_size_spin.text(), "theme default")
        finally:
            dlg.deleteLater()


class FontSizeApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.mgr = theming.ThemeManager()
        self.mgr.refresh()

    def tearDown(self) -> None:
        QTest.qWait(20)   # drain any queued restyle against this test

    def test_size_override_reaches_the_app_font(self) -> None:
        self.mgr.set_user_font_size(15)
        self.mgr.apply("brutalist-mono")
        QTest.qWait(20)   # font lands with the deferred restyle
        from tide.ui import scale
        self.assertEqual(self.app.font().pointSize(), scale.round_pt(15))

    def test_clearing_size_returns_to_theme_size(self) -> None:
        self.mgr.set_user_font_size(15)
        self.mgr.apply("brutalist-mono")
        QTest.qWait(20)
        self.mgr.set_user_font_size(0)   # re-applies internally
        QTest.qWait(20)
        from tide.ui import scale
        theme = self.mgr.current()
        expected = scale.round_pt(float(theme.t("typography", "size_pt", 10)))
        self.assertEqual(self.app.font().pointSize(), expected)


if __name__ == "__main__":
    unittest.main()


class FontPlaceholderSaveTest(unittest.TestCase):
    """Regression: `currentData() or currentText()` saved the "from theme"
    row's LABEL as a literal font family, so every save while on the default
    row wrote font_family_override = "from theme" and the app fell back to
    a random Qt font after restart."""

    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None

    def tearDown(self) -> None:
        settings_module.save = self._real_save
        theming.manager().set_user_font("")

    def test_from_theme_row_saves_empty_override(self) -> None:
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        try:
            dlg.font_picker.setCurrentIndex(0)   # "from theme"
            dlg._on_save()
            self.assertEqual(dlg._settings.font_family_override, "")
        finally:
            dlg.deleteLater()

    def test_bundled_row_saves_family_not_label(self) -> None:
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        try:
            dlg.font_picker.setCurrentIndex(1)   # "IBM Plex Mono · bundled"
            dlg._on_save()
            self.assertEqual(dlg._settings.font_family_override, "IBM Plex Mono")
        finally:
            dlg.deleteLater()

    def test_typed_family_still_saves(self) -> None:
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        try:
            dlg.font_picker.setCurrentText("Some Font I Typed")
            dlg._on_save()
            self.assertEqual(dlg._settings.font_family_override,
                             "Some Font I Typed")
        finally:
            dlg.deleteLater()
