"""v1.5 play reporting is a *choice*, not a default.

The wizard step must hold [next] until one of the two radios is picked
(nothing pre-selected — whether tide tells YouTube Music what you play is
a privacy call), a cancelled wizard must not count as an answer, and the
setting must survive a settings.toml round-trip.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

from tide import settings as settings_module
from tide.ui.onboarding import OnboardingResult, _PlayReportingStep


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class PlayReportingStepTest(unittest.TestCase):
    def test_no_answer_no_advance(self) -> None:
        _app()
        step = _PlayReportingStep()
        self.assertFalse(step._yes.isChecked())
        self.assertFalse(step._no.isChecked())
        self.assertFalse(step.can_advance())

    def test_yes_carries_through(self) -> None:
        _app()
        step = _PlayReportingStep()
        step._yes.setChecked(True)
        self.assertTrue(step.can_advance())
        r = OnboardingResult()
        step.apply_to(r)
        self.assertTrue(r.report_plays)
        self.assertTrue(r.report_plays_answered)

    def test_no_is_an_answer_too(self) -> None:
        _app()
        step = _PlayReportingStep()
        step._no.setChecked(True)
        self.assertTrue(step.can_advance())
        r = OnboardingResult()
        step.apply_to(r)
        self.assertFalse(r.report_plays)
        self.assertTrue(r.report_plays_answered)

    def test_unanswered_apply_leaves_result_untouched(self) -> None:
        """apply_to on an unanswered step (wizard cancelled mid-flight)
        must not stamp an answer the user never gave."""
        _app()
        step = _PlayReportingStep()
        r = OnboardingResult()
        step.apply_to(r)
        self.assertFalse(r.report_plays)
        self.assertFalse(r.report_plays_answered)

    def test_default_is_off(self) -> None:
        self.assertFalse(OnboardingResult().report_plays)
        self.assertFalse(settings_module.Settings().report_plays)


class SettingsRoundTripTest(unittest.TestCase):
    def test_report_plays_survives_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.toml"
            with mock.patch.object(settings_module.config, "SETTINGS_FILE", path):
                s = settings_module.Settings()
                s.report_plays = True
                s.report_plays_answered = True
                settings_module.save(s)
                loaded = settings_module.load()
        self.assertTrue(loaded.report_plays)
        self.assertTrue(loaded.report_plays_answered)


if __name__ == "__main__":
    unittest.main()
