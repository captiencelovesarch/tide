"""v1.2.5 playback-prefetch settings — defaults, TOML round-trip, dialog.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tomllib
import unittest
from dataclasses import fields

from PySide6.QtWidgets import QApplication

from tide import settings as settings_module
from tide.settings import Settings, _to_toml


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class SettingsDefaultsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = Settings()
        self.assertTrue(s.prefetch_hover)
        self.assertEqual(s.prefetch_warm_results, 3)

    def test_toml_round_trip(self) -> None:
        s = Settings()
        s.prefetch_hover = False
        s.prefetch_warm_results = 5
        raw = tomllib.loads(_to_toml(s))
        known = {f.name for f in fields(Settings)}
        back = Settings(**{k: v for k, v in raw.items() if k in known})
        self.assertFalse(back.prefetch_hover)
        self.assertEqual(back.prefetch_warm_results, 5)

    def test_pre_125_config_gets_defaults(self) -> None:
        # A config written by 1.2.4 has neither key — load()'s filter path
        # must fall back to the dataclass defaults.
        raw = {"theme": "dark"}
        known = {f.name for f in fields(Settings)}
        back = Settings(**{k: v for k, v in raw.items() if k in known})
        self.assertTrue(back.prefetch_hover)
        self.assertEqual(back.prefetch_warm_results, 3)


class SettingsDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        # The dialog's save handler persists to the real config file —
        # stub it out so tests never touch ~/.config/tide.
        self._real_save = settings_module.save
        settings_module.save = lambda s: None

    def tearDown(self) -> None:
        settings_module.save = self._real_save

    def test_populate_and_save(self) -> None:
        from tide.ui.settings import SettingsDialog

        s = Settings()
        s.prefetch_hover = False
        s.prefetch_warm_results = 5
        dlg = SettingsDialog(s)
        try:
            self.assertFalse(dlg.prefetch_hover_toggle.isChecked())
            self.assertEqual(dlg.prefetch_warm_picker.currentData(), 5)

            dlg.prefetch_hover_toggle.setChecked(True)
            idx = dlg.prefetch_warm_picker.findData(0)
            self.assertGreaterEqual(idx, 0)
            dlg.prefetch_warm_picker.setCurrentIndex(idx)
            dlg._on_save()

            out = dlg.updated_settings()
            self.assertTrue(out.prefetch_hover)
            self.assertEqual(out.prefetch_warm_results, 0)
        finally:
            dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
