"""The v1.6 fullscreen mode — dedicated lean-back now-playing window.

Covers: toggle swaps windows (and reuses one FullscreenPlayer), mini and
fullscreen stay exclusive, F11 routing (visualizer view keeps its own
meaning), the strip button, lyrics pane toggle + persistence, zen chrome
fade with cursor hide, adaptive/ambient gating, the quit path, compositor
closes rerouting to exit, settings round trip, and the LyricsView
font_scale plumbing the mode depends on.

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
from tide.ui import motion as motion_module
from tide.ui.window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _window() -> MainWindow:
    router = PlaybackRouter()
    router.register(MpvBackend())
    return MainWindow(LocalSource(), router)


def _track(video_id: str = "v1", title: str = "some song",
           artists: str = "some artist") -> Track:
    return Track(video_id=video_id, title=title, artists=artists,
                 duration="3:00", thumbnail="")


class FullscreenToggleTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        # The fullscreen window persists settings (lyrics toggle, context
        # menu) — never let tests touch ~/.config/tide.
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w.close()
        settings_module.save = self._real_save

    def test_toggle_swaps_windows_and_reuses_the_instance(self) -> None:
        self.assertIsNone(self.w._fs)
        self.w.set_fullscreen_mode(True)
        self.assertIsNotNone(self.w._fs)
        self.assertTrue(self.w._fs.isVisible())
        self.assertFalse(self.w.isVisible())
        first = self.w._fs

        self.w.set_fullscreen_mode(False)
        self.assertFalse(self.w._fs.isVisible())
        self.assertTrue(self.w.isVisible())

        self.w.set_fullscreen_mode(True)
        self.assertIs(self.w._fs, first,
                      "fullscreen player must be constructed once")

    def test_mini_and_fullscreen_are_exclusive(self) -> None:
        self.w.set_mini_mode(True)
        self.assertTrue(self.w._mini_mode)
        self.w.set_fullscreen_mode(True)
        self.assertFalse(self.w._mini_mode)
        self.assertFalse(self.w._mini.isVisible())
        self.assertTrue(self.w._fs_mode)
        self.assertTrue(self.w._fs.isVisible())

        self.w.set_mini_mode(True)
        self.assertFalse(self.w._fs_mode)
        self.assertFalse(self.w._fs.isVisible())
        self.assertTrue(self.w._mini_mode)
        self.assertTrue(self.w._mini.isVisible())

    def test_f11_opens_fullscreen_outside_the_visualizer(self) -> None:
        app = _app()
        self.assertNotEqual(self.w.stack.currentIndex(), 8)
        self.w._on_f11()
        # toggle_fullscreen_mode defers via singleShot(0).
        app.processEvents()
        app.processEvents()
        self.assertTrue(self.w._fs_mode)

    def test_f11_on_the_visualizer_view_keeps_its_meaning(self) -> None:
        app = _app()
        self.w.stack.setCurrentIndex(8)
        self.w._on_f11()
        app.processEvents()
        app.processEvents()
        self.assertFalse(self.w._fs_mode, "visualizer view owns its F11")
        self.assertTrue(self.w.visualizer_view._fullscreen)
        # Restore, and release the capture the view auto-started on show —
        # in the app that's the shutdown path's job (app.py), not close.
        self.w.visualizer_view._toggle_fullscreen()
        self.w.visualizer_view.teardown()

    def test_strip_button_opens_fullscreen(self) -> None:
        app = _app()
        QTest.mouseClick(self.w.fullscreen_btn, Qt.LeftButton)
        app.processEvents()
        app.processEvents()
        self.assertTrue(self.w._fs_mode)
        self.assertTrue(self.w._fs.isVisible())

    def test_nothing_playing_state(self) -> None:
        self.w.set_fullscreen_mode(True)
        self.assertIn("nothing playing", self.w._fs.title_lbl.text().lower())

    def test_sync_now_paints_the_track(self) -> None:
        self.w._current = _track()
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self.assertIn("some song", fs.title_lbl.text().lower())
        self.assertIn("some artist", fs.artist_lbl.text().lower())

    def test_lyrics_pane_embeds_without_panel_chrome(self) -> None:
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        panel = fs.lyrics_panel
        self.assertEqual(fs._pane, "lyrics", "lyrics pane defaults open")
        self.assertTrue(panel.isVisibleTo(fs))
        self.assertFalse(fs.queue_view.isVisibleTo(fs))
        for chrome in (panel.heading, panel.karaoke_check, panel.mute_btn,
                       panel.swap_status):
            self.assertFalse(chrome.isVisibleTo(panel))

    def test_pane_tabs_swap_and_persist(self) -> None:
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        fs._on_queue_btn()
        self.assertEqual(fs._pane, "queue")
        self.assertEqual(self.w._settings.fullscreen_pane, "queue")
        self.assertTrue(fs.queue_view.isVisibleTo(fs))
        self.assertFalse(fs.lyrics_panel.isVisibleTo(fs))
        self.assertTrue(fs.queue_btn.activeState())
        self.assertFalse(fs.lyrics_btn.activeState())
        # Clicking the active tab closes the pane entirely.
        fs._on_queue_btn()
        self.assertEqual(fs._pane, "off")
        self.assertEqual(self.w._settings.fullscreen_pane, "off")
        self.assertFalse(fs.queue_view.isVisibleTo(fs))
        fs._on_lyrics_btn()
        self.assertEqual(fs._pane, "lyrics")
        self.assertEqual(self.w._settings.fullscreen_pane, "lyrics")

    def test_queue_pane_shares_the_main_queue_model(self) -> None:
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self.assertIs(fs.queue_view.model(), self.w.queue)
        self.w.queue.add(_track("q1"))
        self.w.queue.add(_track("q2", title="other song"))
        self.w.queue.set_current(0)
        fs._apply_pane("queue")
        self.assertEqual(fs.queue_view.model().rowCount(), 2)

    def test_remembered_pane_restores_on_show(self) -> None:
        self.w._settings.fullscreen_pane = "off"
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self.assertEqual(fs._pane, "off")
        self.assertFalse(fs.lyrics_panel.isVisibleTo(fs),
                         "the widgets must agree with the pane state")
        self.assertFalse(fs.queue_view.isVisibleTo(fs))

    def test_karaoke_routes_through_the_panel_and_wants_lyrics(self) -> None:
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        fs._apply_pane("queue")
        self.assertFalse(fs.lyrics_panel._karaoke_mode)
        fs._toggle_karaoke()
        self.assertTrue(fs.lyrics_panel._karaoke_mode)
        self.assertEqual(fs._pane, "lyrics",
                         "enabling karaoke brings the lyrics pane up")
        fs._toggle_karaoke()
        self.assertFalse(fs.lyrics_panel._karaoke_mode)

    def test_zen_fades_chrome_and_hides_the_cursor(self) -> None:
        prior = motion_module.intensity()
        motion_module.set_intensity(motion_module.Intensity.OFF)
        try:
            self.w.set_fullscreen_mode(True)
            fs = self.w._fs
            self.assertEqual(fs._top_eff.opacity(), 1.0)
            fs._zen_sleep()
            self.assertTrue(fs._zen_asleep)
            self.assertEqual(fs._top_eff.opacity(), 0.0)
            self.assertEqual(fs._bottom_eff.opacity(), 0.0)
            self.assertEqual(fs.cursor().shape(), Qt.BlankCursor)
            fs._zen_wake()
            self.assertFalse(fs._zen_asleep)
            self.assertEqual(fs._top_eff.opacity(), 1.0)
            self.assertNotEqual(fs.cursor().shape(), Qt.BlankCursor)
        finally:
            motion_module.set_intensity(prior)

    def test_hide_restores_cursor_and_chrome(self) -> None:
        prior = motion_module.intensity()
        motion_module.set_intensity(motion_module.Intensity.OFF)
        try:
            self.w.set_fullscreen_mode(True)
            fs = self.w._fs
            fs._zen_sleep()
            self.w.set_fullscreen_mode(False)
            self.assertFalse(fs._zen_asleep)
            self.assertEqual(fs._top_eff.opacity(), 1.0)
        finally:
            motion_module.set_intensity(prior)

    def test_no_idle_inhibit_while_nothing_plays(self) -> None:
        self.w.set_fullscreen_mode(True)
        self.assertFalse(self.w._fs._inhibitor.active)

    def test_quit_closes_the_fullscreen_too(self) -> None:
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self.w.close()
        self.assertFalse(fs.isVisible())

    def test_compositor_close_reroutes_to_exit(self) -> None:
        app = _app()
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        fs.close()
        app.processEvents()
        app.processEvents()
        self.assertFalse(self.w._fs_mode)
        self.assertTrue(self.w.isVisible(), "main window must come back")

    def test_active_window_and_toast_host_prefer_fullscreen(self) -> None:
        self.w.set_fullscreen_mode(True)
        self.assertIs(self.w.active_app_window(), self.w._fs)
        self.assertIs(self.w.toast_host(), self.w._fs)
        self.w.set_fullscreen_mode(False)
        self.assertIs(self.w.active_app_window(), self.w)


class FullscreenVolumeSpeedTest(unittest.TestCase):
    """The couch controls: volume and speed live on fullscreen's bottom
    bar and stay in step with the main window both ways."""

    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()
        self.w.set_fullscreen_mode(True)
        self.fs = self.w._fs

    def tearDown(self) -> None:
        self.w.close()
        settings_module.save = self._real_save

    def test_controls_sit_on_the_bottom_bar(self) -> None:
        bar = self.fs._bottom_bar
        self.assertTrue(bar.isAncestorOf(self.fs.volume))
        self.assertTrue(bar.isAncestorOf(self.fs.speed_btn))

    def test_opens_with_the_main_window_values(self) -> None:
        self.w.set_fullscreen_mode(False)
        self.w.volume.setVolume(37)
        self.w.speed_btn.set_speed(1.25)
        self.w.set_fullscreen_mode(True)
        self.assertEqual(self.fs.volume.volume(), 37)
        self.assertAlmostEqual(self.fs.speed_btn.speed(), 1.25)

    def test_fullscreen_volume_drives_the_player_path(self) -> None:
        self.fs.volume.setVolume(22)
        self.assertEqual(self.w.volume.volume(), 22)
        self.assertEqual(self.w._settings.volume, 22)

    def test_main_volume_changes_reach_fullscreen(self) -> None:
        # The keymap's volume keys call the main widget directly.
        self.w.volume.setVolume(64)
        self.assertEqual(self.fs.volume.volume(), 64)

    def test_fullscreen_speed_drives_the_player_path(self) -> None:
        self.fs.speed_btn.set_speed(1.5)
        self.assertAlmostEqual(self.w.speed_btn.speed(), 1.5)
        self.assertAlmostEqual(self.w._settings.playback_speed, 1.5)

    def test_main_speed_changes_reach_fullscreen(self) -> None:
        self.w.speed_btn.set_speed(0.75)
        self.assertAlmostEqual(self.fs.speed_btn.speed(), 0.75)

    def test_speed_greys_with_the_backend(self) -> None:
        self.fs.set_speed_supported(False)
        self.assertFalse(self.fs.speed_btn.isEnabled())
        self.fs.set_speed_supported(True)
        self.assertTrue(self.fs.speed_btn.isEnabled())

    def test_volume_face_follows_the_strip(self) -> None:
        self.w.set_fullscreen_mode(False)
        self.w._slot_volume = "spring"
        self.w.set_fullscreen_mode(True)
        from tide.ui.variants import SpringVolume
        self.assertIsInstance(self.fs.volume, SpringVolume)
        self.assertTrue(self.fs._bottom_bar.isAncestorOf(self.fs.volume))
        self.fs.volume.setVolume(41)
        self.assertEqual(self.w.volume.volume(), 41)


class FullscreenGateTest(unittest.TestCase):
    """The fullscreen window shares the mini's consumer gates."""

    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w.close()
        settings_module.save = self._real_save

    def test_adaptive_counts_fullscreen_as_a_consumer(self) -> None:
        from tide.ui.adaptive import AdaptiveDriver
        self.w._adaptive = AdaptiveDriver(self.w.queue)
        self.assertFalse(self.w._adaptive.is_enabled())
        self.w.set_fullscreen_mode(True)
        self.assertTrue(self.w._adaptive.is_enabled())
        self.w.set_fullscreen_mode(False)
        self.assertFalse(self.w._adaptive.is_enabled())

    def test_ambient_targets_add_and_remove(self) -> None:
        from tide.ui.ambient import AmbientController

        class _Spy:
            def set_pulse(self, level: float) -> None:
                pass

        # With the main backdrop on, "follow" resolves to a live style,
        # so the pulse gate is held while visible.
        self.w._settings.adaptive_background = True
        self.w._ambient = AmbientController(self.w.player, _Spy())
        self.w.set_fullscreen_mode(True)
        self.assertIn(self.w._fs, self.w._ambient._targets)
        self.assertTrue(self.w._ambient._wants_pulse())
        self.w.set_fullscreen_mode(False)
        self.assertNotIn(self.w._fs, self.w._ambient._targets)
        self.assertFalse(self.w._ambient._wants_pulse())

    def test_no_pulse_gate_when_the_backdrop_is_flat(self) -> None:
        from tide.ui.ambient import AmbientController

        class _Spy:
            def set_pulse(self, level: float) -> None:
                pass

        # Main backdrop off + "follow" → flat card → no capture held.
        self.w._settings.adaptive_background = False
        self.w._ambient = AmbientController(self.w.player, _Spy())
        self.w.set_fullscreen_mode(True)
        self.assertFalse(self.w._ambient._wants_pulse())
        self.w.set_fullscreen_mode(False)

    def test_adaptive_art_reaches_the_fullscreen_backdrop(self) -> None:
        from PySide6.QtGui import QImage
        from tide.ui.adaptive import AdaptiveDriver
        self.w._adaptive = AdaptiveDriver(self.w.queue)
        self.w.set_fullscreen_mode(True)
        img = QImage(8, 8, QImage.Format_RGB32)
        img.fill(Qt.red)
        # Same wiring app.py gives the main backdrop — the liquid style
        # must melt the same cover on both surfaces.
        self.w._adaptive.art_ready.emit(img)
        self.assertIs(self.w._fs.central_bg._art, img)


class _StubScreen:
    """Just enough QScreen for prepare_for_screen."""

    def __init__(self, w: int, h: int) -> None:
        from PySide6.QtCore import QRect
        self._geo = QRect(0, 0, w, h)

    def geometry(self):
        return self._geo


class FullscreenBackdropAndScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self.w = _window()
        self.w._settings = Settings()

    def tearDown(self) -> None:
        self.w.close()
        settings_module.save = self._real_save

    def test_follow_honors_the_main_backdrop_toggle(self) -> None:
        # Main backdrop off → follow means flat, not a forced gradient
        # (which painted the theme's bg_alt hue — nord's blue).
        self.w._settings.adaptive_background = False
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self.assertEqual(fs.resolved_backdrop_style(), "off")
        self.assertFalse(fs.central_bg._enabled)

        self.w._settings.adaptive_background = True
        self.w._settings.adaptive_background_style = "stage"
        fs.apply_settings()
        self.assertEqual(fs.resolved_backdrop_style(), "stage")
        self.assertTrue(fs.central_bg._enabled)

    def test_explicit_style_ignores_the_main_toggle(self) -> None:
        self.w._settings.adaptive_background = False
        self.w._settings.fullscreen_backdrop_style = "depths"
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self.assertEqual(fs.resolved_backdrop_style(), "depths")
        self.assertTrue(fs.central_bg._enabled)

    def test_prepare_for_screen_uses_that_screens_numbers(self) -> None:
        from tide.ui import scale as scale_module
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs

        fs.prepare_for_screen(_StubScreen(2560, 1440))
        want_px = min(int(1440 * 0.50), int(2560 * 0.38))
        want_base = max(200, int(want_px / scale_module.factor()))
        self.assertEqual(fs.art._base_size, want_base)
        self.assertEqual(fs._pane_w, int(2560 * 0.40))
        self.assertEqual(fs._lyrics_host.minimumWidth(), fs._pane_w,
                         "open pane host pins to the pane width")
        # The side panes pin to the art column's band so they sit
        # parallel with the art.
        band = scale_module.px(want_base) + scale_module.px(120)
        self.assertEqual(fs.lyrics_panel.maximumHeight(), band)
        self.assertEqual(fs.queue_view.maximumHeight(), band)
        big_art = fs.art.width()

        fs.prepare_for_screen(_StubScreen(1366, 768))
        self.assertLess(fs.art.width(), big_art,
                        "a smaller monitor gets smaller art")
        self.assertEqual(fs._pane_w, int(1366 * 0.40))

    def test_entering_sizes_against_the_main_windows_screen(self) -> None:
        # Offscreen has one screen; what matters is that entry runs the
        # per-screen sizing against it (not construction-time numbers).
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        geo = self.w.screen().geometry()
        self.assertEqual(fs._pane_w, int(geo.width() * 0.40))
        self.assertEqual(fs._lyrics_host.minimumWidth(), fs._pane_w)

    def test_pane_swaps_never_shift_the_layout(self) -> None:
        # The reported bug: showing the incoming pane before hiding the
        # outgoing one stacked two band-height panes for a layout pass,
        # the minimum height spiked past the screen, and the window
        # grew — everything shifted down and stayed there.
        from PySide6.QtCore import QPoint
        prior = motion_module.intensity()
        motion_module.set_intensity(motion_module.Intensity.OFF)
        try:
            app = _app()
            self.w.set_fullscreen_mode(True)
            fs = self.w._fs
            app.processEvents()
            art_y = fs.art.mapTo(fs, QPoint(0, 0)).y()
            height = fs.height()
            for pane in ("queue", "lyrics", "queue", "lyrics"):
                fs._apply_pane(pane)
                app.processEvents()
                self.assertEqual(fs.art.mapTo(fs, QPoint(0, 0)).y(), art_y,
                                 f"art must not move on swap to {pane}")
                self.assertEqual(fs.height(), height,
                                 "the window must not grow on swaps")
        finally:
            motion_module.set_intensity(prior)

    def test_closing_the_pane_centers_and_grows_the_art(self) -> None:
        prior = motion_module.intensity()
        motion_module.set_intensity(motion_module.Intensity.OFF)
        try:
            self.w.set_fullscreen_mode(True)
            fs = self.w._fs
            self.assertEqual(fs.art._base_size, fs._art_base)
            fs._apply_pane("off")
            self.assertFalse(fs._lyrics_host.isVisibleTo(fs))
            self.assertEqual(fs._lyrics_host.minimumWidth(), 0)
            self.assertEqual(fs.art._base_size, fs._art_base_solo)
            self.assertGreater(fs._art_base_solo, fs._art_base)
            fs._apply_pane("lyrics")
            self.assertTrue(fs._lyrics_host.isVisibleTo(fs))
            self.assertEqual(fs._lyrics_host.minimumWidth(), fs._pane_w)
            self.assertEqual(fs.art._base_size, fs._art_base)
        finally:
            motion_module.set_intensity(prior)

    def test_pane_open_close_animates_when_motion_is_on(self) -> None:
        prior = motion_module.intensity()
        motion_module.set_intensity(motion_module.Intensity.LITE)
        try:
            self.w.set_fullscreen_mode(True)
            fs = self.w._fs
            fs._apply_pane("off")
            self.assertIsNotNone(fs._pane_anim, "close glides")
            # Don't let a mid-flight tick outlive this window into the
            # next test's event loop.
            fs._pane_anim.stop()
            fs._pane_anim = None
        finally:
            motion_module.set_intensity(prior)


class FullscreenPaletteCarryTest(unittest.TestCase):
    """The reported v1.6 bug: a fullscreen opened mid-song anchored its
    backdrop to the BASE theme (nord's blue bg_alt), because current()
    carries no dynamic overrides and the driver's same-song re-push is a
    no-op that never re-emits. Late-built backdrops must anchor from the
    effective theme."""

    def setUp(self) -> None:
        app = _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self._prior_font = app.font()

    def tearDown(self) -> None:
        settings_module.save = self._real_save
        # Leave the process exactly as the suite found it: NO theme
        # applied. The rest of the suite runs themeless (only titlebar
        # tests apply themes, and they sort after everything they could
        # disturb); a live app stylesheet slows every later widget
        # construction, which broke the home engine's budget-timed
        # block-build smoke test.
        from PySide6.QtWidgets import QApplication
        from tide import theming
        m = theming.manager()
        m._dynamic_overrides.clear()
        m._current = None
        m._applied_qss = None
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet("")
            app.setFont(self._prior_font)

    def test_fullscreen_backdrop_carries_the_album_palette(self) -> None:
        from PySide6.QtCore import QThreadPool
        from PySide6.QtGui import QColor, QImage
        from tide import theming
        from tide.ui import art_cache
        from tide.ui.adaptive import AdaptiveDriver

        app = _app()
        theming.manager().apply("nord")
        w = _window()
        w._settings = Settings(adaptive_background=True,
                               adaptive_background_style="field")
        try:
            w.central_bg.set_enabled(True)
            adaptive = AdaptiveDriver(w.queue)
            adaptive.set_background_enabled(True)
            w._adaptive = adaptive

            # Red cover through the real pipeline: queued track with a
            # thumbnail URL, art cache pre-seeded so the fetch resolves
            # synchronously, palette worker + token push genuine.
            cover = QImage(300, 300, QImage.Format_RGB32)
            cover.fill(QColor("#a03028"))
            url = "https://example.invalid/red-cover.jpg"
            art_cache.cache()._mem[url] = cover
            track = Track(video_id="v-red", title="red record",
                          artists="someone", duration="3:00", thumbnail=url)
            w.queue.add(track)
            w.queue.set_current(0)
            w._current = track
            QThreadPool.globalInstance().waitForDone(5000)
            for _ in range(50):
                app.processEvents()

            w.set_fullscreen_mode(True)
            fs = w._fs
            self.assertEqual(fs.central_bg._tone_ta.name(),
                             w.central_bg._tone_ta.name(),
                             "fullscreen must carry the main palette")
            tone = fs.central_bg._tone_ta
            self.assertGreater(tone.red(), tone.blue(),
                               "album red, not the theme's baseline blue")
            w.set_fullscreen_mode(False)
        finally:
            w.close()


class FullscreenSettingsTest(unittest.TestCase):
    def test_fields_round_trip_through_toml(self) -> None:
        s = Settings(fullscreen_backdrop_style="stage",
                     fullscreen_pane="queue",
                     fullscreen_pulse=False)
        raw = tomllib.loads(settings_module._to_toml(s))
        self.assertEqual(raw["fullscreen_backdrop_style"], "stage")
        self.assertEqual(raw["fullscreen_pane"], "queue")
        self.assertFalse(raw["fullscreen_pulse"])

    def test_defaults(self) -> None:
        s = Settings()
        self.assertEqual(s.fullscreen_backdrop_style, "follow")
        self.assertEqual(s.fullscreen_pane, "lyrics")
        self.assertTrue(s.fullscreen_pulse)


class LyricsMotionTest(unittest.TestCase):
    """Line advances glide the scroll and fade the accent in; motion
    "off" keeps the old snap-into-view behavior."""

    def setUp(self) -> None:
        _app()
        self._prior = motion_module.intensity()

    def tearDown(self) -> None:
        motion_module.set_intensity(self._prior)

    def test_motion_off_snaps(self) -> None:
        from tide.ui.lyrics import LyricsView
        motion_module.set_intensity(motion_module.Intensity.OFF)
        view = LyricsView(LocalSource())
        view._show_timed([(0.0, "one"), (5.0, "two"), (10.0, "three")])
        view.update_position(6.0)
        self.assertIsNone(view._scroll_anim)
        self.assertIsNone(view._active_anim)
        # The accent lands immediately, no fade in flight.
        self.assertIn("font-weight: 700",
                      view._line_widgets[1].styleSheet())

    def test_motion_on_animates_fade_and_glide(self) -> None:
        from tide.ui.lyrics import LyricsView
        app = _app()
        motion_module.set_intensity(motion_module.Intensity.LITE)
        view = LyricsView(LocalSource())
        view.setFixedSize(240, 120)
        view.show()
        app.processEvents()
        # Enough lines to overflow the viewport so the glide has a
        # scroll range to work with.
        view._show_timed([(i * 2.0, f"line {i}") for i in range(40)])
        app.processEvents()
        view.update_position(41.0)   # line 20, far below the fold
        self.assertIsNotNone(view._active_anim, "accent fade runs")
        self.assertIsNotNone(view._scroll_anim, "scroll glides")
        # Rebuilding the list stops any motion in flight.
        view._clear_timed()
        self.assertIsNone(view._active_anim)
        self.assertIsNone(view._scroll_anim)
        view.hide()


class LyricsFontScaleTest(unittest.TestCase):
    """Lyric type follows the size step, the body text and the ui scale;
    the fullscreen embed multiplies on top. "small" at the normal scale
    is the classic 10 / 13 / 12 / 28 panel."""

    def setUp(self) -> None:
        _app()
        from tide.ui import lyrics, scale
        self._lyrics = lyrics
        self._scale = scale
        self._size0 = lyrics.size()
        self._scale0 = scale.current()
        scale.set_factor("normal")

    def tearDown(self) -> None:
        self._lyrics.set_size(self._size0)
        self._scale.set_factor(self._scale0)

    def test_small_is_the_classic_panel(self) -> None:
        from tide.ui.lyrics import LyricsView, _KaraokeWidget
        self._lyrics.set_size("small")
        view = LyricsView(LocalSource())
        view._show_timed([(0.0, "one"), (5.0, "two")])
        self.assertIn("10pt", view._line_widgets[0].styleSheet())
        view.update_position(1.0)
        self.assertIn("13pt", view._line_widgets[0].styleSheet())
        classic = _KaraokeWidget()
        self.assertIn("28pt", classic.current_label.styleSheet())
        self.assertIn("12pt", classic.prev_label.styleSheet())

    def test_default_is_bigger_than_the_body_text(self) -> None:
        from tide.ui.lyrics import LyricsView
        self.assertEqual(self._lyrics.DEFAULT_SIZE, "large")
        self._lyrics.set_size("large")
        view = LyricsView(LocalSource())
        view._show_timed([(0.0, "one"), (5.0, "two")])
        self.assertIn("16pt", view._line_widgets[0].styleSheet())
        view.update_position(1.0)
        self.assertIn("20pt", view._line_widgets[0].styleSheet())

    def test_timed_lines_scale(self) -> None:
        from tide.ui.lyrics import LyricsView
        self._lyrics.set_size("small")
        view = LyricsView(LocalSource(), font_scale=2.0)
        view._show_timed([(0.0, "one"), (5.0, "two")])
        self.assertIn("20pt", view._line_widgets[0].styleSheet())
        view.update_position(1.0)
        self.assertIn("26pt", view._line_widgets[0].styleSheet(),
                      "active line scales its 13pt base")

    def test_ui_scale_reaches_the_lines(self) -> None:
        # The reported bug: at "huge" the rest of the app grew and the
        # lyrics stayed at a fixed 10pt.
        from tide.ui.lyrics import LyricsView
        self._lyrics.set_size("small")
        self._scale.set_factor("huge")
        view = LyricsView(LocalSource())
        view._show_timed([(0.0, "one"), (5.0, "two")])
        self.assertIn("13pt", view._line_widgets[0].styleSheet())

    def test_size_change_restyles_open_views(self) -> None:
        from tide.ui.lyrics import LyricsView
        self._lyrics.set_size("small")
        view = LyricsView(LocalSource())
        view._show_timed([(0.0, "one"), (5.0, "two")])
        self._lyrics.set_size("huge")
        self.assertIn("21pt", view._line_widgets[0].styleSheet())
        self.assertIn("21pt", view._plain_label.styleSheet())

    def test_unknown_size_falls_back(self) -> None:
        self._lyrics.set_size("enormous")
        self.assertEqual(self._lyrics.size(), self._lyrics.DEFAULT_SIZE)

    def test_karaoke_scales(self) -> None:
        from tide.ui.lyrics import _KaraokeWidget
        self._lyrics.set_size("small")
        big = _KaraokeWidget(font_scale=2.0)
        self.assertIn("56pt", big.current_label.styleSheet())
        self.assertIn("24pt", big.prev_label.styleSheet())


if __name__ == "__main__":
    unittest.main()
