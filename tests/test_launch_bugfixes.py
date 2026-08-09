"""Launch-time regressions (v1.3.x): CSD hide-on-map, wizard [next] staleness,
and disabled-Spotify expiry noise.

Run offscreen:  QT_QPA_PLATFORM=offscreen python -m pytest tests/
"""
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
