"""Silent YT Music token auto-refresh (v1.4.x).

Expiry used to stop at a toast — detection was automatic, the actual fix
waited for a click on [refresh token]. These tests pin the new behavior:
a 401 (or an approaching cookie deadline) starts the silent browser
re-import on its own, and the toast survives only as the fallback for
the cases silence can't fix.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import auth, theming
from tide.playback import MpvBackend, PlaybackRouter
from tide.sources import registry as source_registry
from tide.sources.local import LocalSource


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _FakeYt:
    """Just enough MusicSource surface for the expiry/auto-refresh path."""

    slug = "ytmusic"
    name = "youtube music"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.reloads = 0

    def status_text(self) -> str:
        return "signed in"

    def is_authenticated(self) -> bool:
        return True

    def reload_client(self) -> bool:
        self.reloads += 1
        return True


class _RefreshStub:
    """Stands in for wizard.refresh_token_async; captures the callbacks so
    the test can resolve the 'worker' synchronously."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, on_done, on_failed=None):
        self.calls.append((on_done, on_failed))
        return None


class AutoRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("nord")
        reg = source_registry()
        self._saved = (dict(reg._sources), dict(reg._enabled), reg._active)
        reg._sources.clear()
        reg._enabled.clear()
        self.fake = _FakeYt()
        reg.register(self.fake, enabled=True)

        from tide.ui import toast as toast_module, wizard as wizard_module
        from tide.ui.window import MainWindow
        self.toasts = []
        self._real_toast = toast_module.show_toast
        toast_module.show_toast = lambda *a, **k: self.toasts.append((a, k))
        self.stub = _RefreshStub()
        self._real_refresh = wizard_module.refresh_token_async
        wizard_module.refresh_token_async = self.stub

        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)

    def tearDown(self) -> None:
        from tide.ui import toast as toast_module, wizard as wizard_module
        toast_module.show_toast = self._real_toast
        wizard_module.refresh_token_async = self._real_refresh
        self.w.close()
        QTest.qWait(30)
        reg = source_registry()
        reg._sources.clear()
        reg._enabled.clear()
        reg._sources.update(self._saved[0])
        reg._enabled.update(self._saved[1])
        reg._active = self._saved[2]

    def _toast_texts(self) -> list[str]:
        return [a[1] for a, _k in self.toasts]

    def test_401_heals_silently(self) -> None:
        """Dead cookies → silent re-import → no toast, client rebuilt."""
        self.w._on_source_auth_expired("ytmusic")
        self.assertEqual(self.toasts, [], "auto path raised a toast before trying")
        self.assertEqual(len(self.stub.calls), 1, "silent refresh never started")
        on_done, _ = self.stub.calls[0]
        on_done("chromium")
        self.assertEqual(self.fake.reloads, 1, "fresh cookies weren't loaded")
        self.assertEqual(self.toasts, [], "a successful auto-refresh must be quiet")
        self.assertNotIn("ytmusic", self.w._auth_expired_toasted)

    def test_no_browser_session_falls_back_to_toast(self) -> None:
        """'' from the worker = every profile signed out → user must act."""
        self.w._on_source_auth_expired("ytmusic")
        on_done, _ = self.stub.calls[0]
        on_done("")
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("token expired", self._toast_texts()[0])
        self.assertIn("ytmusic", self.w._auth_expired_toasted)

    def test_worker_error_falls_back_to_toast(self) -> None:
        self.w._on_source_auth_expired("ytmusic")
        _, on_failed = self.stub.calls[0]
        on_failed("locked cookie db")
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("token expired", self._toast_texts()[0])

    def test_second_death_within_cooldown_toasts(self) -> None:
        """Refreshed cookies that die again immediately mean the browser's
        session is the corpse — don't loop, hand the problem to the user."""
        self.w._on_source_auth_expired("ytmusic")
        self.stub.calls[0][0]("chromium")       # first attempt "succeeds"
        self.w._on_source_auth_expired("ytmusic")   # ...but 401s again
        self.assertEqual(len(self.stub.calls), 1, "looped instead of backing off")
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("token expired", self._toast_texts()[0])

    def test_disabled_source_neither_refreshes_nor_toasts(self) -> None:
        source_registry().set_enabled("ytmusic", False)
        self.w._on_source_auth_expired("ytmusic")
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(self.toasts, [])

    def test_approaching_deadline_renews_silently(self) -> None:
        """Pre-expiry used to warn-toast; now it renews, and stays quiet
        when the renewal actually moved the deadline."""
        with mock.patch.object(auth, "seconds_until_expiry") as m:
            m.return_value = 3600.0
            self.w._check_session_expiry()
            self.assertEqual(self.toasts, [], "warned instead of renewing")
            self.assertEqual(len(self.stub.calls), 1)
            m.return_value = 90 * 86400.0       # renewal extended the cookies
            self.stub.calls[0][0]("brave")
        self.assertEqual(self.toasts, [])
        self.assertEqual(self.fake.reloads, 1)

    def test_renewal_that_moves_nothing_warns_once(self) -> None:
        """Browser jar is near-expiry too: re-importing it can't help, and
        silently retrying every tick would hide a real deadline."""
        with mock.patch.object(auth, "seconds_until_expiry") as m:
            m.return_value = 2 * 86400.0
            self.w._check_session_expiry()
            self.stub.calls[0][0]("brave")      # same short jar came back
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("expires in 2d", self._toast_texts()[0])
        # _expiry_warned is set: the 30-min tick must not re-warn.
        with mock.patch.object(auth, "seconds_until_expiry", return_value=2 * 86400.0):
            self.w._check_session_expiry()
        self.assertEqual(len(self.toasts), 1)


if __name__ == "__main__":
    unittest.main()
