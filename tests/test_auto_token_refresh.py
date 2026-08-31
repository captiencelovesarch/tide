"""Silent YT Music token auto-refresh (v1.4.x) + manual refresh (v1.5.x).

Expiry used to stop at a toast — detection was automatic, the actual fix
waited for a click on [refresh token]. These tests pin the new behavior:
a 401 (or an approaching cookie deadline) starts the silent browser
re-import on its own, and the toast survives only as the fallback for
the cases silence can't fix.

The manual path (refresh_session_manual — settings button, Ctrl+Shift+R)
is the same worker with opposite manners: it ignores the auto cooldown,
shares the in-flight dedup, and always announces its outcome.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import time
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


class _RefreshTestBase(unittest.TestCase):
    """Shared harness: stubbed worker, captured toasts, fake yt source."""

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


class AutoRefreshTest(_RefreshTestBase):
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


class ManualRefreshTest(_RefreshTestBase):
    """refresh_session_manual — the settings button / Ctrl+Shift+R path."""

    def setUp(self) -> None:
        super().setUp()
        self.outcomes: list[tuple[bool, str]] = []
        self.w.session_refresh_finished.connect(
            lambda ok, msg: self.outcomes.append((ok, msg))
        )

    def test_manual_bypasses_cooldown(self) -> None:
        """The cooldown gates the machine, not the human: right after the
        auto path refused, a click must still fire."""
        self.w._auto_refresh_at = time.monotonic()      # "just tried"
        self.w._on_source_auth_expired("ytmusic")
        self.assertEqual(self.stub.calls, [], "cooldown didn't gate the auto path")
        self.assertTrue(self.w.refresh_session_manual())
        self.assertEqual(len(self.stub.calls), 1, "manual path obeyed the cooldown")

    def test_manual_inflight_dedup(self) -> None:
        """A second click while the worker runs must not double-fire, and
        completion still announces exactly once."""
        self.assertTrue(self.w.refresh_session_manual())
        self.assertFalse(self.w.refresh_session_manual())
        self.assertEqual(len(self.stub.calls), 1, "stacked a second worker")
        self.stub.calls[0][0]("firefox")
        self.assertEqual(self._toast_texts(), ["session refreshed from firefox"])
        # Attempt finished — the next click may start a fresh one.
        self.assertTrue(self.w.refresh_session_manual())
        self.assertEqual(len(self.stub.calls), 2)

    def test_manual_success_feedback_and_reloads(self) -> None:
        """Success is loud (toast + signal) and reloads the stale views
        through the same hook the auto path uses."""
        with mock.patch.object(self.w, "_refresh_after_reauth") as reload_hook:
            self.w.refresh_session_manual()
            self.stub.calls[0][0]("chromium")
            reload_hook.assert_called_once_with("ytmusic")
        self.assertEqual(self.fake.reloads, 1, "fresh cookies weren't loaded")
        self.assertEqual(self._toast_texts(), ["session refreshed from chromium"])
        self.assertEqual(self.outcomes, [(True, "session refreshed from chromium")])
        self.assertNotIn("ytmusic", self.w._auth_expired_toasted)

    def test_manual_no_browser_points_at_sign_in(self) -> None:
        """'' from the worker = every profile signed out: say so and offer
        the sign-in flow instead of pretending it worked."""
        self.w.refresh_session_manual()
        self.stub.calls[0][0]("")
        self.assertEqual(len(self.toasts), 1)
        _args, kwargs = self.toasts[0]
        self.assertEqual(_args[1], "no signed-in browser found")
        self.assertEqual(kwargs.get("action_label"), "sign in")
        self.assertEqual(len(self.outcomes), 1)
        ok, msg = self.outcomes[0]
        self.assertFalse(ok)
        self.assertIn("sign in", msg)
        self.assertEqual(self.fake.reloads, 0)

    def test_manual_worker_error_announces(self) -> None:
        self.w.refresh_session_manual()
        self.stub.calls[0][1]("locked cookie db")
        self.assertEqual(self._toast_texts(),
                         ["session refresh failed: locked cookie db"])
        self.assertEqual(self.outcomes,
                         [(False, "session refresh failed: locked cookie db")])

    def test_click_during_auto_attempt_makes_finish_loud(self) -> None:
        """Dedup across paths: a click while the silent auto attempt is in
        flight starts nothing new, but the user asked — the attempt's
        completion switches from silent to announced."""
        self.w._on_source_auth_expired("ytmusic")       # auto attempt running
        self.assertFalse(self.w.refresh_session_manual())
        self.assertEqual(len(self.stub.calls), 1, "stacked a second worker")
        on_done, _ = self.stub.calls[0]                 # the AUTO callbacks
        on_done("brave")
        self.assertEqual(self._toast_texts(), ["session refreshed from brave"])
        self.assertEqual(self.outcomes, [(True, "session refreshed from brave")])
        self.assertEqual(self.fake.reloads, 1)

    def test_no_yt_source_reports_instead_of_crashing(self) -> None:
        source_registry()._sources.clear()
        source_registry()._enabled.clear()
        self.assertFalse(self.w.refresh_session_manual())
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(self.outcomes, [(False, "youtube music isn't set up")])

    def test_set_up_but_disabled_points_at_the_switch(self) -> None:
        """Wizard ran (auth saved) but the source is toggled off — startup
        never registered it, so get() is None while the settings row right
        above the button says "signed in". "isn't set up" would be a lie;
        point at the enable switch instead."""
        source_registry()._sources.clear()
        source_registry()._enabled.clear()
        with mock.patch.object(auth, "have_auth", return_value=True):
            self.assertFalse(self.w.refresh_session_manual())
        self.assertEqual(self.stub.calls, [])
        self.assertEqual(self.outcomes, [(
            False, "youtube music is turned off. enable it in settings → sources"
        )])

    def test_toast_reauth_then_manual_click_starts_one_worker(self) -> None:
        """[refresh token] on the expiry toast routes through the manual
        path, so a manual refresh (button / Ctrl+Shift+R) landing while it
        runs must dedup against it — one worker, one cookie harvest, one
        announced outcome."""
        self.w._begin_source_reauth("ytmusic")
        self.assertEqual(len(self.stub.calls), 1, "toast action started nothing")
        self.assertFalse(self.w.refresh_session_manual())
        self.assertEqual(len(self.stub.calls), 1, "stacked a second worker")
        self.stub.calls[0][0]("firefox")
        self.assertEqual(self._toast_texts(), ["session refreshed from firefox"])
        self.assertEqual(self.outcomes, [(True, "session refreshed from firefox")])
        self.assertEqual(self.fake.reloads, 1)
        # Attempt finished — flag released, a fresh click starts a new one.
        self.assertFalse(self.w._auto_refresh_inflight)
        self.assertTrue(self.w.refresh_session_manual())


class SettingsRefreshButtonTest(_RefreshTestBase):
    """The [refresh session] row in settings → integrations delegates to
    the window and mirrors the outcome inline."""

    def _dialog(self):
        from tide import settings as settings_module
        from tide.ui.settings import SettingsDialog
        return SettingsDialog(settings_module.Settings(), parent=self.w)

    def test_button_state_cycle(self) -> None:
        dlg = self._dialog()
        try:
            self.assertEqual(dlg.refresh_session_status.text(), "not signed in")
            dlg._on_refresh_session()
            self.assertFalse(dlg.refresh_session_btn.isEnabled())
            self.assertEqual(dlg.refresh_session_status.text(), "checking browsers…")
            self.assertEqual(len(self.stub.calls), 1)
            self.stub.calls[0][0]("brave")
            self.assertTrue(dlg.refresh_session_btn.isEnabled())
            self.assertEqual(dlg.refresh_session_status.text(),
                             "session refreshed from brave")
        finally:
            dlg.deleteLater()

    def test_failure_lands_inline(self) -> None:
        dlg = self._dialog()
        try:
            dlg._on_refresh_session()
            self.stub.calls[0][0]("")
            self.assertTrue(dlg.refresh_session_btn.isEnabled())
            self.assertIn("no signed-in browser found",
                          dlg.refresh_session_status.text())
        finally:
            dlg.deleteLater()


class ExpiryLabelTest(_RefreshTestBase):
    """Sub-hour expiry used to render "expires in 0h" on both surfaces
    (settings row + warning toast) — the formatters need a minutes tier."""

    def test_settings_row_counts_minutes_under_an_hour(self) -> None:
        from tide import settings as settings_module
        from tide.ui.settings import SettingsDialog
        with mock.patch.object(auth, "have_auth", return_value=True), \
             mock.patch.object(auth, "seconds_until_expiry", return_value=1800.0):
            dlg = SettingsDialog(settings_module.Settings(), parent=self.w)
        try:
            self.assertEqual(dlg.refresh_session_status.text(),
                             "signed in · expires in 30m")
        finally:
            dlg.deleteLater()

    def test_toast_counts_minutes_under_an_hour(self) -> None:
        self.w._warn_session_expiring(1800.0)
        self.assertEqual(self._toast_texts(),
                         ["youtube music: token expires in 30m"])

    def test_under_a_minute_rounds_up_not_down(self) -> None:
        # "expires in 0m" would be the same bug one tier lower.
        self.w._warn_session_expiring(20.0)
        self.assertEqual(self._toast_texts(),
                         ["youtube music: token expires in 1m"])


if __name__ == "__main__":
    unittest.main()
