"""The v1.2.7 mini player redo — dedicated frameless window.

Covers: toggle swaps windows (and reuses one MiniPlayer), the art click
opens it (deferred), walkman layouts no longer drag mini mode around,
adaptive/ambient mini gates, settings fields + dialog round trip, and the
quit path closing the mini.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tomllib
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.queue import Queue
from tide.settings import Settings
from tide.sources.base import Track
from tide.sources.local import LocalSource
from tide.ui.window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _window() -> MainWindow:
    router = PlaybackRouter()
    router.register(MpvBackend())
    return MainWindow(LocalSource(), router)


class MiniToggleTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        # Both the mini (lyrics toggle, context menu) and the dialog persist
        # settings — never let tests touch ~/.config/tide.
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w.close()
        settings_module.save = self._real_save

    def test_toggle_swaps_windows_and_reuses_the_instance(self) -> None:
        self.assertIsNone(self.w._mini)
        self.w.set_mini_mode(True)
        self.assertIsNotNone(self.w._mini)
        self.assertTrue(self.w._mini.isVisible())
        self.assertFalse(self.w.isVisible())
        first = self.w._mini

        self.w.set_mini_mode(False)
        self.assertFalse(self.w._mini.isVisible())
        self.assertTrue(self.w.isVisible())

        self.w.set_mini_mode(True)
        self.assertIs(self.w._mini, first, "mini must be constructed once")

    def test_mini_is_frameless_and_translucent(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self.assertTrue(mini.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(mini.testAttribute(Qt.WA_TranslucentBackground))

    def test_art_click_opens_the_mini(self) -> None:
        app = _app()
        QTest.mouseClick(self.w.art, Qt.LeftButton)
        # toggle_mini_mode defers via singleShot(0).
        app.processEvents()
        app.processEvents()
        self.assertTrue(self.w._mini_mode)
        self.assertTrue(self.w._mini.isVisible())

    def test_nothing_playing_state(self) -> None:
        self.w.set_mini_mode(True)
        self.assertIn("nothing playing",
                      self.w._mini.title_lbl.text().lower())

    def test_ticker_tracker_only_runs_while_visible(self) -> None:
        self.w.set_mini_mode(True)
        self.assertTrue(self.w._mini._tracker._enabled)
        self.w.set_mini_mode(False)
        self.assertFalse(self.w._mini._tracker._enabled)

    def test_lyrics_toggle_builds_panel_and_persists(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self.assertIsNone(mini.lyrics_panel)
        mini._toggle_lyrics(animate=False)
        self.assertIsNotNone(mini.lyrics_panel)
        self.assertTrue(self.w._settings.mini_lyrics_open)
        # Chrome that makes no sense in a 210px panel is gone.
        self.assertFalse(mini.lyrics_panel.heading.isVisibleTo(mini.lyrics_panel))
        mini._toggle_lyrics(animate=False)
        self.assertFalse(self.w._settings.mini_lyrics_open)

    def test_progress_style_switch(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        # Default: ring visible, thin bar hidden.
        self.assertTrue(mini.ring.isVisibleTo(mini))
        self.assertFalse(mini.progress.isVisibleTo(mini))
        self.w._settings.mini_progress_style = "thin"
        mini.apply_settings()
        self.assertFalse(mini.ring.isVisibleTo(mini))
        self.assertTrue(mini.progress.isVisibleTo(mini))

    def test_quit_closes_the_mini_too(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self.w.close()
        self.assertFalse(mini.isVisible())

    def test_walkman_layout_no_longer_drags_mini_mode(self) -> None:
        from tide import layout as layout_module
        walkman = layout_module.manager().apply("walkman", {})
        if walkman is None:
            self.skipTest("walkman layout not bundled")

        self.w.apply_layout(walkman)
        self.assertFalse(self.w._mini_mode)
        self.assertIsNone(self.w._mini)
        self.assertFalse(self.w._upper_wrap_widget.isVisibleTo(self.w))
        self.assertFalse(self.w.statusBar().isVisibleTo(self.w))

        classic = layout_module.manager().apply("classic", {})
        self.w.apply_layout(classic)
        self.assertTrue(self.w._upper_wrap_widget.isVisibleTo(self.w))
        self.assertTrue(self.w.statusBar().isVisibleTo(self.w))


class AdaptiveMiniGateTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_mini_counts_as_a_consumer(self) -> None:
        from tide.ui.adaptive import AdaptiveDriver
        d = AdaptiveDriver(Queue())
        self.assertFalse(d.is_enabled())
        self.assertFalse(d._wants_ambient_bg())
        d.set_mini_active(True)
        self.assertTrue(d.is_enabled())
        self.assertTrue(d._wants_ambient_bg())
        d.set_mini_active(False)
        self.assertFalse(d.is_enabled())

    def test_mini_gate_does_not_disturb_user_toggles(self) -> None:
        from tide.ui.adaptive import AdaptiveDriver
        d = AdaptiveDriver(Queue())
        d.set_background_enabled(True)
        d.set_mini_active(True)
        d.set_mini_active(False)
        self.assertTrue(d.is_enabled(), "background toggle must survive")


class _PulseSpy:
    def __init__(self) -> None:
        self.levels: list[float] = []

    def set_pulse(self, level: float) -> None:
        self.levels.append(level)


class AmbientMiniTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def _controller(self):
        from tide.ui.ambient import AmbientController
        router = PlaybackRouter()
        router.register(MpvBackend())
        spy = _PulseSpy()
        return AmbientController(router, spy), spy

    def test_mini_gate_wants_pulse(self) -> None:
        c, _spy = self._controller()
        self.assertFalse(c._wants_pulse())
        c.set_mini_active(True)
        self.assertTrue(c._wants_pulse())
        c.set_mini_active(False)
        self.assertFalse(c._wants_pulse())

    def test_targets_add_remove(self) -> None:
        c, spy = self._controller()
        extra = _PulseSpy()
        c.add_target(extra)
        c.add_target(extra)   # idempotent
        self.assertEqual(c._targets.count(extra), 1)
        c._set_pulse(0.5)
        self.assertIn(0.5, spy.levels)
        self.assertIn(0.5, extra.levels)
        c.remove_target(extra)
        self.assertEqual(extra.levels[-1], 0.0, "removed target settles to 0")
        c._set_pulse(0.7)
        self.assertNotIn(0.7, extra.levels)


class MiniSettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        s = Settings()
        self.assertEqual(s.mini_backdrop_style, "follow")
        self.assertEqual(s.mini_progress_style, "ring")
        self.assertTrue(s.mini_ticker)
        self.assertTrue(s.mini_zen)
        self.assertTrue(s.mini_pulse)
        self.assertFalse(s.mini_lyrics_open)
        self.assertFalse(s.mini_show_visualizer)

    def test_toml_round_trip(self) -> None:
        s = Settings()
        s.mini_backdrop_style = "vbeam"
        s.mini_progress_style = "thin"
        s.mini_ticker = False
        s.mini_lyrics_open = True
        s.mini_show_visualizer = True
        data = tomllib.loads(settings_module._to_toml(s))
        self.assertEqual(data["mini_backdrop_style"], "vbeam")
        self.assertEqual(data["mini_progress_style"], "thin")
        self.assertFalse(data["mini_ticker"])
        self.assertTrue(data["mini_lyrics_open"])
        self.assertTrue(data["mini_show_visualizer"])


class MiniDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None

    def tearDown(self) -> None:
        settings_module.save = self._real_save

    def test_populate_and_save(self) -> None:
        from tide.ui.settings import SettingsDialog
        s = Settings()
        s.mini_backdrop_style = "band"
        s.mini_mode_default = True
        s.mini_zen = False
        dlg = SettingsDialog(s)
        self.assertEqual(dlg.mini_backdrop_picker.currentData(), "band")
        self.assertTrue(dlg.mini_default_toggle.isChecked())
        self.assertFalse(dlg.mini_zen_toggle.isChecked())
        self.assertFalse(dlg.mini_vis_toggle.isChecked())

        dlg.mini_backdrop_picker.setCurrentIndex(
            dlg.mini_backdrop_picker.findData("off"))
        dlg.mini_progress_picker.setCurrentIndex(
            dlg.mini_progress_picker.findData("thin"))
        dlg.mini_pulse_toggle.setChecked(False)
        dlg.mini_vis_toggle.setChecked(True)
        dlg._on_save()
        # The dialog deep-copies its Settings — assert on the copy it saves.
        saved = dlg._settings
        self.assertEqual(saved.mini_backdrop_style, "off")
        self.assertEqual(saved.mini_progress_style, "thin")
        self.assertFalse(saved.mini_pulse)
        self.assertTrue(saved.mini_show_visualizer)


class _FeedSpy:
    """Stands in for audio_capture.feed() so tests never spawn parec."""

    def __init__(self) -> None:
        self.consumers: set[str] = set()
        self.sources: list = []

    def add_consumer(self, name: str, source=None) -> bool:
        self.consumers.add(name)
        self.sources.append(source)
        return True

    def remove_consumer(self, name: str) -> None:
        self.consumers.discard(name)


class MiniVisualizerTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w.close()
        settings_module.save = self._real_save

    def test_stack_page_follows_the_setting(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        mini._feed = _FeedSpy()
        self.assertIs(mini.art_stack.currentWidget(), mini.art)
        self.w._settings.mini_show_visualizer = True
        mini.apply_settings()
        self.assertIs(mini.art_stack.currentWidget(), mini.vis_canvas)
        self.w._settings.mini_show_visualizer = False
        mini.apply_settings()
        self.assertIs(mini.art_stack.currentWidget(), mini.art)

    def test_capture_needs_vis_page_visible_and_playing(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        spy = _FeedSpy()
        mini._feed = spy

        # Vis page on, but the player is idle — no capture.
        self.w._settings.mini_show_visualizer = True
        mini.apply_settings()
        self.assertNotIn("mini", spy.consumers)
        self.assertFalse(mini._vis_capturing)

        # Playing → the consumer is held under the mini's own name.
        mini._is_playing = lambda: True
        mini._reconcile_vis()
        self.assertIn("mini", spy.consumers)
        self.assertTrue(mini._vis_capturing)

        # Flipping back to art releases it even mid-playback.
        self.w._settings.mini_show_visualizer = False
        mini.apply_settings()
        self.assertNotIn("mini", spy.consumers)
        self.assertFalse(mini._vis_capturing)

        # And leaving mini mode (hide) releases it too.
        self.w._settings.mini_show_visualizer = True
        mini.apply_settings()
        self.assertIn("mini", spy.consumers)
        self.w.set_mini_mode(False)
        self.assertNotIn("mini", spy.consumers)
        self.assertFalse(mini._vis_capturing)


class MiniZenAndPulseTest(unittest.TestCase):
    """Zen collapse-to-art-card, the label-width pin (the anti-spazz fix),
    lyrics squeeze, and the opt-in bass resize."""

    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        from tide.ui import motion
        self._motion = motion
        motion.set_intensity("off")     # snap everything → deterministic
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w.close()
        self._motion.set_intensity("lite")
        settings_module.save = self._real_save

    def _pump(self, n: int = 4) -> None:
        # Top-level relayout under the offscreen QPA needs the loop to run
        # timers (a plain processEvents leaves the window size one state
        # behind) — qWait spins for real.
        QTest.qWait(30)
        app = _app()
        for _ in range(n):
            app.processEvents()

    def test_zen_collapses_the_window_to_the_art_card(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self._pump()
        awake_h = mini.height()
        mini._zen_sleep()
        self._pump()
        self.assertTrue(mini._zen_asleep)
        self.assertEqual(mini._fade_group.height(), 0)
        self.assertLess(mini.height(), awake_h,
                        "window must shrink toward the art card")
        mini._zen_wake(snap=True)
        self._pump()
        self.assertFalse(mini._group_constrained(),
                         "wake must return the group to natural sizing")
        self.assertGreaterEqual(mini.height(), awake_h - 2)

    def test_labels_are_pinned_to_the_art_width(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        for lbl in (mini.title_lbl, mini.artist_lbl, mini.ticker_lbl):
            self.assertEqual(lbl.maximumWidth(), mini.art.width(),
                             "unpinned labels re-negotiate the window per "
                             "scramble frame (the spazz bug)")

    def test_zen_can_run_with_lyrics_open_when_forced(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        mini._toggle_lyrics(animate=False)
        mini._zen_sleep(force=True)
        self.assertTrue(mini._zen_asleep,
                        "the lyrics-squeeze path relies on forced zen")

    def test_bass_resize_swells_from_the_center(self) -> None:
        self.assertFalse(Settings().mini_pulse_resize)
        self.w.set_mini_mode(True)
        mini = self.w._mini
        base = mini._base_margins
        # Off (default): no gutter, set_pulse must not touch geometry.
        mini.set_pulse(1.0)
        self.assertEqual(mini._pulse_pad, 0)
        self.assertEqual(mini._gutter, 0)
        # On, at rest: the full pad is pre-reserved as a transparent gutter
        # and the ring hugs the card inside it.
        self.w._settings.mini_pulse_resize = True
        mini.apply_settings()
        self.assertTrue(mini._pulse_resize_on)
        self._pump()
        self.assertEqual(mini._gutter, 8)
        rest_size = mini.size()
        self.assertEqual(mini.ring.geometry(),
                         mini.rect().adjusted(8, 8, -8, -8))
        # Full bass: card margins grow by the pad, gutter shrinks by the
        # same — the WINDOW must not move a pixel (center-origin swell).
        mini._apply_pulse_pad(1.0)
        self._pump()
        self.assertEqual(mini._gutter, 0)
        m = mini._content_col.contentsMargins()
        self.assertEqual(m.left(), base[0] + 8)
        self.assertEqual(m.top(), base[1] + 8)
        self.assertEqual(mini.size(), rest_size,
                         "the swell must not change window geometry")
        self.assertEqual(mini.ring.geometry(), mini.rect())
        # Hide resets the pad so the next open starts calm.
        self.w.set_mini_mode(False)
        self.assertEqual(mini._pulse_pad, 0)

    def test_long_ticker_lines_wrap_and_grow_the_window(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        one_line = mini._ticker_line_height()
        self.assertEqual(mini.ticker_lbl.maximumHeight(), one_line)
        long_line = ("and the city lights are calling me home through the "
                     "rain on the boulevard where we used to dance until "
                     "the morning came around again")
        self._pump()
        h_before = mini.height()
        mini._on_ticker_line(long_line)
        self._pump()
        self.assertGreater(mini.ticker_lbl.maximumHeight(), one_line,
                           "long lines must wrap, not elide")
        self.assertLessEqual(mini.ticker_lbl.maximumHeight(), one_line * 3,
                             "capped at three lines")
        self.assertEqual(mini.ticker_lbl.maximumHeight(),
                         mini.ticker_lbl.minimumHeight(),
                         "height pinned per line — scramble frames must "
                         "not re-negotiate the window")
        self.assertGreater(mini.height(), h_before,
                           "the window grows down to make room")
        # Back to a short line → back to one reserved line.
        mini._on_ticker_line("short line")
        self._pump()
        self.assertEqual(mini.ticker_lbl.maximumHeight(), one_line)
        mini._on_ticker_line(None)
        self._pump()
        self.assertEqual(mini.ticker_lbl.text(), "")
        self.assertEqual(mini.ticker_lbl.maximumHeight(), one_line)

    def test_pin_toggle_plumbing(self) -> None:
        self.assertFalse(Settings().mini_pin)
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self.assertEqual(mini.pin_btn._label, "📌")
        mini._on_pin_btn()
        self.assertTrue(self.w._settings.mini_pin)
        self.assertEqual(mini.pin_btn._label, "📍")
        mini._on_pin_btn()
        self.assertFalse(self.w._settings.mini_pin)
        self.assertEqual(mini.pin_btn._label, "📌")

    def test_mini_art_is_borderless(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self.assertFalse(mini.art._framed)
        self.assertIn("border: none", mini.art.styleSheet())
        # The main window's strip art keeps its tile frame.
        self.assertTrue(self.w.art._framed)

    def test_ambient_targets_the_mini_itself(self) -> None:
        """set_mini_mode must register the MiniPlayer (which fans out to the
        backdrop + resize), not the bare CentralBg."""
        class _Amb:
            def __init__(self):
                self.targets = []
            def add_target(self, t):
                self.targets.append(t)
            def remove_target(self, t):
                self.targets.remove(t)
            def set_mini_active(self, on):
                pass
        amb = _Amb()
        self.w._ambient = amb
        self.w.set_mini_mode(True)
        self.assertIn(self.w._mini, amb.targets)
        self.w.set_mini_mode(False)
        self.assertNotIn(self.w._mini, amb.targets)


if __name__ == "__main__":
    unittest.main()
