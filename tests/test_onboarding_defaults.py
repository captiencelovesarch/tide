"""Onboarding defaults: no pre-selected sources, living-background toggle.

Run offscreen:  QT_QPA_PLATFORM=offscreen python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtWidgets import QApplication

from tide.ui.onboarding import OnboardingResult, _FeelStep, _SourcesStep


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class NoPreselectedSourcesTest(unittest.TestCase):
    """A checked box the user never touched isn't a choice — the sources
    step must start with everything off (soundcloud + bandcamp used to
    arrive pre-checked)."""

    def test_step_starts_all_off(self) -> None:
        _app()
        step = _SourcesStep()
        self.assertFalse(any(step._enabled.values()))
        self.assertFalse(any(c.isChecked() for c in step._cards.values()))
        # Zero sources is a legal choice; the wizard must still advance.
        self.assertTrue(step.can_advance())

    def test_result_default_matches(self) -> None:
        self.assertFalse(any(OnboardingResult().sources_enabled.values()))


class LivingBackgroundToggleTest(unittest.TestCase):
    """The feel step offers the living background (it used to offer only
    the adaptive accent, so the flagship backdrop was undiscoverable
    until the user dug through Settings)."""

    def test_default_on(self) -> None:
        _app()
        step = _FeelStep()
        r = OnboardingResult()
        step.apply_to(r)
        self.assertTrue(r.adaptive_background)

    def test_uncheck_carries_through(self) -> None:
        _app()
        step = _FeelStep()
        step._living_bg_check.setChecked(False)
        r = OnboardingResult()
        step.apply_to(r)
        self.assertFalse(r.adaptive_background)
        self.assertTrue(r.adaptive_accent)   # independent toggles


if __name__ == "__main__":
    unittest.main()
