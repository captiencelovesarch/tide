"""Launch-time regressions: CSD hide-on-map, wizard [next] staleness,
disabled-Spotify expiry noise, and the v2.0 startup fixes (deferred
single-instance raise, first-run modern-pick-on-brutalist-slots).

Run offscreen:  QT_QPA_PLATFORM=offscreen python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from tide import theming
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources import registry as source_registry
from tide.sources.local import LocalSource


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _FakeSpotify:
    """Just enough MusicSource surface for a _SourceRow + probe job."""

    slug = "spotify"
    name = "spotify"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.probes = []

    def status_text(self) -> str:
        return "signed in"

    def is_authenticated(self) -> bool:
        return True

    def probe(self) -> bool:
        self.probes.append(True)
        return True


class _RegistrySandbox(unittest.TestCase):
    """Snapshot/restore the global source registry around each test."""

    def setUp(self) -> None:
        _app()
        reg = source_registry()
        self._saved = (dict(reg._sources), dict(reg._enabled), reg._active)
        reg._sources.clear()
        reg._enabled.clear()

    def tearDown(self) -> None:
        reg = source_registry()
        reg._sources.clear()
        reg._enabled.clear()
        reg._sources.update(self._saved[0])
        reg._enabled.update(self._saved[1])
        reg._active = self._saved[2]


class CsdKeepsWindowVisibleTest(unittest.TestCase):
    """setWindowFlag hides a mapped window; set_csd_titlebar must re-show.

    Regression: the re-show was guarded by isVisible() read AFTER the flag
    flip — always False — so enabling the CSD titlebar on a shown window
    (launch did exactly this) left tide invisible with only the tray icon.
    """

    def test_flag_flip_on_shown_window_reshows(self) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("nord")
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        w = MainWindow(LocalSource(), router)
        try:
            w.show()
            QTest.qWait(30)
            self.assertTrue(w.isVisible())
            w.set_csd_titlebar(True)
            QTest.qWait(60)     # deferred _remap fires on the next tick
            self.assertTrue(w.isVisible(), "CSD enable hid the window")
            w.set_csd_titlebar(False)
            QTest.qWait(60)
            self.assertTrue(w.isVisible(), "CSD disable hid the window")
        finally:
            w.close()
            QTest.qWait(30)


class WizardNextAfterSigninTest(unittest.TestCase):
    """A successful in-step sign-in must re-enable [next] on its own.

    Regression: _do_setup set _yt_authed but never emitted state_changed,
    so the user had to toggle YT Music off and back on to advance.
    """

    def test_next_enables_without_toggle_dance(self) -> None:
        _app()
        from tide.ui import wizard as wizard_module
        from tide.ui.onboarding import OnboardingDialog

        class _AcceptingSignIn:
            def __init__(self, parent=None) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def deleteLater(self) -> None:
                pass

        real = wizard_module.SignInDialog
        wizard_module.SignInDialog = _AcceptingSignIn
        dlg = OnboardingDialog()
        try:
            sources_idx = 3
            dlg._stack.setCurrentIndex(sources_idx)
            dlg._on_step_entered(sources_idx)
            step = dlg._steps[sources_idx]
            step._on_toggled("ytmusic", True)
            self.assertFalse(dlg._next_btn.isEnabled())   # setup pending
            QTest.qWait(80)     # deferred _do_setup runs + "signs in"
            self.assertTrue(step._yt_authed)
            self.assertTrue(step.can_advance())
            self.assertTrue(
                dlg._next_btn.isEnabled(),
                "[next] stayed disabled after a successful sign-in",
            )
        finally:
            wizard_module.SignInDialog = real
            dlg.deleteLater()
            QTest.qWait(30)


class DisabledSpotifyStaysQuietTest(_RegistrySandbox):
    """A disabled source must neither probe (token refresh) nor toast."""

    def test_probe_skips_disabled_spotify(self) -> None:
        from PySide6.QtCore import QThreadPool
        from tide.ui.source_panel import SourcePanel
        fake = _FakeSpotify()
        reg = source_registry()
        reg.register(fake, enabled=False)
        panel = SourcePanel(Settings())
        try:
            panel._probe_async_sources()
            QThreadPool.globalInstance().waitForDone(2000)
            QTest.qWait(30)
            self.assertEqual(fake.probes, [], "disabled spotify was probed")
            reg.set_enabled("spotify", True)
            panel._probe_async_sources()
            QThreadPool.globalInstance().waitForDone(2000)
            QTest.qWait(30)
            self.assertEqual(len(fake.probes), 1)
        finally:
            panel.deleteLater()
            QTest.qWait(30)

    def test_expiry_toast_gated_on_enabled(self) -> None:
        theming.manager().refresh()
        theming.manager().apply("nord")
        from tide.ui import toast as toast_module
        from tide.ui.window import MainWindow
        fake = _FakeSpotify()
        source_registry().register(fake, enabled=False)
        router = PlaybackRouter()
        router.register(MpvBackend())
        w = MainWindow(LocalSource(), router)
        toasts = []
        real = toast_module.show_toast
        toast_module.show_toast = lambda *a, **k: toasts.append(a)
        try:
            w._on_source_auth_expired("spotify")
            self.assertEqual(toasts, [], "disabled spotify raised a toast")
            # NOT marked toasted: enabling later must still be able to shout.
            self.assertNotIn("spotify", w._auth_expired_toasted)
            source_registry().set_enabled("spotify", True)
            w._on_source_auth_expired("spotify")
            self.assertEqual(len(toasts), 1)
        finally:
            toast_module.show_toast = real
            w.close()
            QTest.qWait(30)


class InstanceRaiseDeferralTest(unittest.TestCase):
    """The single-instance 'raise' command arrives mid-signal-emission
    (socket readyRead), so presenting the window must defer to a later
    event-loop turn — and unknown commands / no-window-yet must no-op."""

    class _FakeWindow:
        def __init__(self) -> None:
            self.presented = 0

        def present_active(self) -> None:
            self.presented += 1

    def test_raise_defers_and_filters(self) -> None:
        _app()
        from tide.app import _instance_message_handler
        target: list = [None]
        handler = _instance_message_handler(target)
        handler("raise")                    # no window yet — dropped
        QTest.qWait(20)
        win = self._FakeWindow()
        target[0] = win
        handler("bogus")                    # unknown commands ignored
        QTest.qWait(20)
        self.assertEqual(win.presented, 0)
        handler("raise")
        # NOT synchronous — the show/raise must unwind the emitting stack
        # first (the modal-from-click crash family)
        self.assertEqual(win.presented, 0)
        QTest.qWait(30)
        self.assertEqual(win.presented, 1)


class WizardPickLandsBeforeWindowTest(unittest.TestCase):
    """First-run regression (fixed in 2.0): a modern wizard pick rendered
    on the brutalist DEFAULT_SLOTS forever, because the slot sync only
    fires on flips it can observe. run_onboarding_if_needed now
    re-applies the pick as a preset before MainWindow exists."""

    def setUp(self) -> None:
        app = _app()
        from tide import config
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-launch-")
        self.addCleanup(self._tmp.cleanup)
        self._config = config
        self._real_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        from tide.ui import onboarding as onboarding_module
        self._onb = onboarding_module
        self._real_dialog = onboarding_module.OnboardingDialog
        # suppress app-wide QSS pushes (test_restyle_coalesce's spy
        # pattern); manager/slot state stays fully real
        mock.patch.object(app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        self._onb.OnboardingDialog = self._real_dialog
        self._config.SETTINGS_FILE = self._real_settings_file
        from tide import layout as layout_module
        from tide.ui import motion as motion_module
        theming.manager().set_user_override("radius", None)
        theming.manager().apply_bundle(
            slug="brutalist-mono", font_family="", font_size=0, case="")
        layout_module.manager().apply("classic", {})
        motion_module.set_intensity("lite")

    def test_modern_pick_gets_modern_slot_variants(self) -> None:
        from tide import app as app_module
        from tide import layout as layout_module
        from tide.ui.onboarding import OnboardingResult

        result = OnboardingResult(
            completed=True, aesthetic="modern", theme_slug="nord",
            adaptive_accent=True, adaptive_background=True, motion="full",
        )

        class _AcceptingWizard:
            DialogCode = QDialog.DialogCode

            def exec(self):
                return QDialog.DialogCode.Accepted

            def result_data(self):
                return result

        self._onb.OnboardingDialog = _AcceptingWizard
        s = Settings()                          # true first launch
        app_module._bootstrap_preset(s)         # pre-wizard startup state
        self.assertTrue(app_module.run_onboarding_if_needed(s))
        # the state MainWindow will construct from: picked theme applied,
        # its slot prefs on the layout — not the brutalist defaults
        self.assertEqual(theming.manager().current().slug, "nord")
        slots = layout_module.manager().current().slots
        self.assertEqual(slots["progress"], "bar")
        self.assertEqual(slots["controls"], "large")
        self.assertEqual(slots["now_label"], "inline")
        self.assertEqual(s.layout_overrides.get("progress"), "bar")


if __name__ == "__main__":
    unittest.main()
