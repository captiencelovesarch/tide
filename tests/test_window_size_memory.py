"""v2.0 window-size memory + the debounced field-scoped settings savers.

settings.window_sizes remembers the main window's size per layout slug:
recorded at the top of apply_layout (before anything moves) and at quit,
restored instead of the old unconditional resize(*window_default). That
also kills the classic annoyance where any slot tweak snapped the window
back to the layout's default size.

The saver hygiene half: volume/speed share one trailing 300 ms debounce
that writes ONLY their fields via save_fields (no more whole-object
save() clobbering concurrent savers), and closeEvent flushes so the last
tick before quit is never lost.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import config, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _WindowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-winsize-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        theming.manager().apply("brutalist-mono")
        layout_module.manager().refresh()
        layout_module.manager().apply("classic")
        # Suppress real app-wide QSS pushes (test_restyle_coalesce's spy
        # pattern) — the theme/layout applies above queue one per test,
        # and each real push repolishes every window earlier suite files
        # leaked. Size/save logic under test never reads the pushed QSS.
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self.w = None

    def tearDown(self) -> None:
        if self.w is not None:
            try:
                theming.manager().theme_changed.disconnect(
                    self.w._on_theme_changed)
            except (RuntimeError, TypeError):
                pass
            self.w.close()
            # close() alone leaks the C++ widget tree until GC; leaked
            # windows make every later app-wide restyle slower (each one
            # gets repolished). deleteLater + the qWait below destroys it.
            self.w.deleteLater()
            self.w = None
        QTest.qWait(30)
        config.SETTINGS_FILE = self._orig_settings_file

    def _make_window(self):
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        return self.w


class CtorSizeTests(_WindowCase):
    def test_ctor_uses_remembered_size_for_active_layout(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        s.window_sizes = {"classic": [900, 600]}
        settings_module.save(s)
        w = self._make_window()
        self.assertEqual((w.width(), w.height()), (900, 600))
        self.assertEqual(w._layout_slug, "classic")

    def test_ctor_falls_back_to_layout_default(self) -> None:
        # No settings file at all — the layout's declared default (the
        # pre-2.0 behavior, byte for byte).
        w = self._make_window()
        self.assertEqual((w.width(), w.height()), (1100, 720))

    def test_ctor_ignores_garbage_entries(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        s.window_sizes = {"classic": [10, -3]}   # absurd → default
        settings_module.save(s)
        w = self._make_window()
        self.assertEqual((w.width(), w.height()), (1100, 720))

    def test_upgrader_on_non_classic_layout_keeps_1100x720(self) -> None:
        # Migration invariant, end to end: a 1.5 user on "focused" saw
        # 1100x720 at every launch (the old ctor hardcode; window_default
        # never applied at startup). Their first 2.0 launch — bootstrap
        # adoption, then window construction — must come up at exactly
        # that size, not focused's declared 1000x720.
        s = Settings()
        s.first_launch_complete = True
        s.theme = "gruvbox"
        s.layout = "focused"
        settings_module.save(s)
        loaded = settings_module.load()
        from tide import app as app_module
        app_module._bootstrap_preset(loaded)     # adopts + seeds the size
        w = self._make_window()
        self.assertEqual((w.width(), w.height()), (1100, 720))
        self.assertEqual(w._layout_slug, "focused")


class ApplyLayoutSizeTests(_WindowCase):
    def test_records_outgoing_and_restores_remembered(self) -> None:
        w = self._make_window()
        s = Settings()
        w._settings = s
        w.resize(999, 555)
        focused = layout_module.manager().get("focused")
        self.assertIsNotNone(focused)
        w.apply_layout(focused)
        # Outgoing classic size recorded before anything changed…
        self.assertEqual(s.window_sizes["classic"], [999, 555])
        # …incoming focused has no memory yet → its declared default.
        self.assertEqual((w.width(), w.height()), focused.window_default)
        self.assertEqual(w._layout_slug, "focused")
        # Size focused, go back: classic's remembered size returns.
        w.resize(777, 444)
        classic = layout_module.manager().get("classic")
        w.apply_layout(classic)
        self.assertEqual(s.window_sizes["focused"], [777, 444])
        self.assertEqual((w.width(), w.height()), (999, 555))
        self.assertEqual(w._layout_slug, "classic")

    def test_slot_tweak_no_longer_snaps_to_default(self) -> None:
        # The old code did resize(*window_default) unconditionally, so
        # every slot-variant tweak snapped a hand-sized window back.
        w = self._make_window()
        w._settings = Settings()
        w.resize(901, 601)
        w.apply_layout(layout_module.manager().current())
        self.assertEqual((w.width(), w.height()), (901, 601))

    def test_without_settings_falls_back_to_default(self) -> None:
        # Bare window (no settings attached, e.g. tests): pre-2.0 resize.
        w = self._make_window()
        w.resize(901, 601)
        w.apply_layout(layout_module.manager().current())
        self.assertEqual((w.width(), w.height()), (1100, 720))


class DebouncedSaverTests(_WindowCase):
    def test_volume_and_speed_share_one_debounced_save(self) -> None:
        w = self._make_window()
        s = Settings()
        s.first_launch_complete = True
        w._settings = s
        with mock.patch.object(settings_module, "save_fields") as spy:
            w._on_volume_changed(10)
            w._on_volume_changed(11)
            w._on_volume_changed(12)
            w._on_speed_changed(1.25)
            self.assertEqual(spy.call_count, 0,
                             "saver must debounce, not write per tick")
            QTest.qWait(500)
            self.assertEqual(spy.call_count, 1,
                             "one burst must coalesce into one write")
            args, _ = spy.call_args
            self.assertIs(args[0], s)
            self.assertEqual(sorted(args[1:]), ["playback_speed", "volume"])
        self.assertEqual(s.volume, 12)
        self.assertEqual(s.playback_speed, 1.25)

    def test_unchanged_values_schedule_nothing(self) -> None:
        w = self._make_window()
        s = Settings()
        w._settings = s
        with mock.patch.object(settings_module, "save_fields") as spy:
            w._on_volume_changed(s.volume)          # same as stored
            w._on_speed_changed(s.playback_speed)   # same as stored
            QTest.qWait(500)
            self.assertEqual(spy.call_count, 0)

    def test_close_flushes_pending_ticks_and_persists_size(self) -> None:
        w = self._make_window()
        s = Settings()
        s.first_launch_complete = True
        w._settings = s
        w.resize(940, 560)
        w._on_volume_changed(37)   # still pending when close hits
        w.close()
        w.deleteLater()   # destroyed by tearDown's qWait
        self.w = None     # closed here; tearDown must not double-close
        back = settings_module.load()
        self.assertEqual(back.volume, 37,
                         "the last pre-quit volume tick was lost")
        self.assertEqual(back.window_sizes["classic"], [940, 560])

    def test_sleep_preset_save_is_field_scoped(self) -> None:
        from tide.ui.sleep_timer import SleepMode
        w = self._make_window()
        s = Settings()
        w._settings = s
        with mock.patch.object(settings_module, "save_fields") as spy:
            w._sleep_start(SleepMode.MINUTES, 12)
            self.assertEqual(spy.call_count, 1)
            args, _ = spy.call_args
            self.assertEqual(args[1:], ("sleep_preset_minutes",))
        w._sleep_cancel(silent=True)
        self.assertEqual(s.sleep_preset_minutes, 12)

    def test_source_panel_persist_is_field_scoped(self) -> None:
        w = self._make_window()
        s = Settings()
        w._settings = s
        with mock.patch.object(settings_module, "save_fields") as spy:
            w._persist_settings()
            self.assertEqual(spy.call_count, 1)
            args, _ = spy.call_args
            self.assertIn("sources_enabled", args[1:])
            self.assertIn("active_source", args[1:])
            self.assertNotIn("theme", args[1:],
                             "the source panel must not save look/feel "
                             "fields it never touches")


if __name__ == "__main__":
    unittest.main()
