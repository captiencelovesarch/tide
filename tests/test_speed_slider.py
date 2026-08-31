"""Phase 3 E1 — the speed control's modern face + backend honesty + the
spring volume variant. Modern builds SpringSpeedPopover, anything else
the untouched bracket SpeedPopover; a flip swaps the face on the NEXT
open. The slider maps to speed_changed live, quantized to the 0.05
grid, and the button→popover sync never yanks an in-progress drag.
Backend honesty: supports_speed defaults True, LibrespotBackend says
False, the router follows the active backend, and the window greys the
SpeedButton on a speed-less backend and restores it after.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_speed_slider.py
"""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tide import speed_law, theming
from tide.player import PlayState
from tide.playback import PlaybackRouter
from tide.playback.base import PlaybackBackend
from tide.sources.base import StreamRef, Track
from tide.ui import motion, scale
from tide.ui.speed import (
    _TIP_NORMAL,
    _TIP_UNSUPPORTED,
    SpeedButton,
    SpeedPopover,
    SpringSpeedPopover,
)
from tide.ui.variants import (
    VOLUME_VARIANTS,
    MonoVolume,
    SpringVolume,
    make_volume,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# Duck-typed events (test_spring_slider's pattern): deterministic
# offscreen, no QMouseEvent constructor-overload roulette.


class _MouseEv:
    def __init__(self, x: float, y: float = 20.0) -> None:
        self._pos = QPointF(x, y)

    def button(self):
        return Qt.LeftButton

    def buttons(self):
        return Qt.LeftButton

    def position(self) -> QPointF:
        return self._pos

    def accept(self) -> None:
        pass


class _WheelEv:
    def __init__(self, dy: int) -> None:
        self._delta = QPoint(0, dy)

    def angleDelta(self) -> QPoint:
        return self._delta

    def accept(self) -> None:
        pass


class _FakeBackend(PlaybackBackend):
    """Minimal concrete backend for router tests. ``speed`` sets the
    supports_speed flag (instance attr shadowing the class default)."""

    def __init__(self, slug: str, *, speed: bool = True) -> None:
        super().__init__()
        self.slug = slug
        self.supports_speed = speed
        self.loads: list[str] = []

    def load(self, payload: str) -> None:
        self.loads.append(payload)

    def play(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def seek(self, seconds: float) -> None:
        pass

    def set_volume(self, percent: int) -> None:
        pass

    @property
    def state(self):
        return PlayState.IDLE

    @property
    def duration(self) -> float:
        return 0.0


# ---------- popover face pick ----------


class _HostCase(unittest.TestCase):
    """SpeedButton inside a plain host widget carrying ``_settings`` —
    the button reads the preset off its window(), same as in the app."""

    PRESET = "modern"

    def setUp(self) -> None:
        self.app = _app()
        self._prev_intensity = motion._user_intensity
        self.addCleanup(motion.set_intensity, self._prev_intensity)
        self._prev_scale = scale.current()
        scale.set_factor("normal")
        self.addCleanup(scale.set_factor, self._prev_scale)
        motion.set_intensity(motion.Intensity.OFF)
        self.host = QWidget()
        self.host._settings = SimpleNamespace(preset=self.PRESET)
        self.btn = SpeedButton(self.host)

    def tearDown(self) -> None:
        if self.btn._popover is not None:
            self.btn._popover.hide()
        # deleteLater + drain — leaked widgets stay wired to theme_changed
        # and would soak up every later restyle in the suite.
        self.host.deleteLater()
        self.host = None
        self.btn = None
        QTest.qWait(30)

    def _open(self):
        self.btn._open_popover()
        return self.btn._popover


class FacePickTests(_HostCase):
    def test_modern_preset_builds_the_spring_face(self) -> None:
        pop = self._open()
        self.assertIsInstance(pop, SpringSpeedPopover)
        self.assertNotIsInstance(pop, SpeedPopover)

    def test_brutalist_preset_keeps_the_bracket_face(self) -> None:
        self.host._settings.preset = "brutalist"
        pop = self._open()
        self.assertIsInstance(pop, SpeedPopover)
        self.assertNotIsInstance(pop, SpringSpeedPopover)

    def test_no_preset_defaults_to_bracket(self) -> None:
        self.host._settings.preset = ""
        self.assertIsInstance(self._open(), SpeedPopover)

    def test_no_settings_defaults_to_bracket(self) -> None:
        del self.host._settings
        self.assertIsInstance(self._open(), SpeedPopover)

    def test_orphan_button_defaults_to_bracket(self) -> None:
        # window() is the button itself → no _settings anywhere.
        loner = SpeedButton()
        try:
            loner._open_popover()
            self.assertIsInstance(loner._popover, SpeedPopover)
            loner._popover.hide()
        finally:
            loner.deleteLater()

    def test_flip_swaps_face_on_next_open(self) -> None:
        self.host._settings.preset = "brutalist"
        first = self._open()
        self.assertIsInstance(first, SpeedPopover)
        first.hide()
        self.host._settings.preset = "modern"
        second = self._open()
        self.assertIsInstance(second, SpringSpeedPopover)
        self.assertIsNot(second, first)

    def test_same_face_reopen_reuses_the_popover(self) -> None:
        first = self._open()
        first.hide()
        second = self._open()
        self.assertIs(second, first)

    def test_spring_face_seeds_from_the_button_value(self) -> None:
        self.btn.set_speed(1.5)
        pop = self._open()
        self.assertAlmostEqual(pop._slider.value(), 1.5)
        self.assertEqual(pop._display.text(), "1.5×")


# ---------- spring face behavior ----------


class SpringFaceTests(_HostCase):
    """Interaction against the real wiring: slider/chips → popover
    speed_changed → SpeedButton.set_speed → sync back. Motion OFF makes
    every settle land inside the call stack (deterministic)."""

    def setUp(self) -> None:
        super().setUp()
        self.spy: list[float] = []
        self.btn.speed_changed.connect(self.spy.append)
        self.pop = self._open()
        self.slider = self.pop._slider
        self.assertGreaterEqual(self.slider.width(), scale.px(220))

    def tearDown(self) -> None:
        self.pop = None
        self.slider = None
        super().tearDown()

    def _press(self, x: float) -> None:
        self.slider.mousePressEvent(_MouseEv(x))

    def _move(self, x: float) -> None:
        self.slider.mouseMoveEvent(_MouseEv(x))

    def _release(self, x: float) -> None:
        self.slider.mouseReleaseEvent(_MouseEv(x))

    def _click(self, x: float) -> None:
        self._press(x)
        self._release(x)

    def test_press_flows_live_to_the_button(self) -> None:
        self._press(self.slider._x_for_value(0.9))
        self.assertAlmostEqual(self.btn.speed(), 0.9)
        self.assertEqual(len(self.spy), 1)
        self.assertAlmostEqual(self.spy[0], 0.9)
        self.assertEqual(self.pop._display.text(), "0.9×")
        self._release(self.slider._x_for_value(0.9))
        # the committed re-emit dedupes in set_speed — no double signal
        self.assertEqual(len(self.spy), 1)

    def test_values_stay_on_the_law_grid(self) -> None:
        x = self.slider._x_for_value(1.12)
        self._click(x)
        v = self.btn.speed()
        self.assertEqual(v, speed_law.clamp(v))
        self.assertAlmostEqual(v, 1.1)

    def test_detent_lands_the_preset_exactly(self) -> None:
        self._click(self.slider._x_for_value(1.25) + 3)
        self.assertEqual(self.btn.speed(), 1.25)
        self.assertEqual(self.pop._display.text(), "1.25×")

    def test_sync_never_yanks_a_live_drag(self) -> None:
        # display follows the finger raw while the logical value
        # quantizes; the set_speed→sync round-trip must not snap the
        # handle to the grid mid-drag
        self._press(self.slider._x_for_value(1.0))
        x = self.slider._x_for_value(1.12)
        self._move(x)
        self.assertAlmostEqual(self.btn.speed(), 1.1)
        self.assertAlmostEqual(self.slider._display, 1.12, places=2)
        self._release(x)

    def test_wheel_steps_one_grid_notch(self) -> None:
        self.slider.wheelEvent(_WheelEv(120))
        self.assertAlmostEqual(self.btn.speed(), 1.05)
        self.slider.wheelEvent(_WheelEv(-120))
        self.slider.wheelEvent(_WheelEv(-120))
        self.assertAlmostEqual(self.btn.speed(), 0.95)

    def test_nudge_chips_step_005(self) -> None:
        self.pop._minus_btn.click()
        self.assertAlmostEqual(self.btn.speed(), 0.95)
        self.pop._plus_btn.click()
        self.pop._plus_btn.click()
        self.assertAlmostEqual(self.btn.speed(), 1.05)
        self.assertEqual(self.pop._display.text(), "1.05×")

    def test_chips_disable_at_the_edges(self) -> None:
        self.btn.set_speed(speed_law.SPEED_MAX)
        self.assertFalse(self.pop._plus_btn.isEnabled())
        self.assertTrue(self.pop._minus_btn.isEnabled())
        self.btn.set_speed(speed_law.SPEED_MIN)
        self.assertTrue(self.pop._plus_btn.isEnabled())
        self.assertFalse(self.pop._minus_btn.isEnabled())
        before = len(self.spy)
        self.pop._minus_btn.click()
        self.assertEqual(len(self.spy), before)
        self.assertEqual(self.btn.speed(), speed_law.SPEED_MIN)

    def test_reset_chip(self) -> None:
        self.btn.set_speed(1.5)
        self.pop._reset_btn.click()
        self.assertEqual(self.btn.speed(), 1.0)
        self.assertEqual(self.pop._display.text(), "1.0×")

    def test_external_set_speed_moves_the_slider(self) -> None:
        self.btn.set_speed(1.75)
        self.assertAlmostEqual(self.slider.value(), 1.75)
        self.assertAlmostEqual(self.slider._display, 1.75)

    def test_release_where_the_drag_began_still_syncs(self) -> None:
        x = self.slider._x_for_value(1.0)
        self._click(x)
        self.assertEqual(self.btn.speed(), 1.0)
        self.assertEqual(self.spy, [])


# ---------- backend honesty ----------


class BackendSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()

    def test_base_class_defaults_to_supported(self) -> None:
        self.assertIs(PlaybackBackend.supports_speed, True)

    def test_librespot_declares_no_speed(self) -> None:
        from tide.playback.librespot_backend import LibrespotBackend
        self.assertIs(LibrespotBackend.supports_speed, False)

    def test_router_follows_the_active_backend(self) -> None:
        router = PlaybackRouter()
        # nothing registered → optimistic default (mpv registers first
        # in the app and supports speed)
        self.assertTrue(router.active_supports_speed())
        mpv = _FakeBackend("mpv", speed=True)
        spot = _FakeBackend("librespot", speed=False)
        router.register(mpv)
        router.register(spot)
        self.assertTrue(router.active_supports_speed())
        router.load_ref(StreamRef(backend="librespot", payload="spotify:track:x"))
        self.assertFalse(router.active_supports_speed())
        self.assertEqual(spot.loads, ["spotify:track:x"])
        router.load_ref(StreamRef(backend="mpv", payload="http://x"))
        self.assertTrue(router.active_supports_speed())

    def test_button_greys_and_recovers(self) -> None:
        btn = SpeedButton()
        try:
            self.assertTrue(btn.isEnabled())
            self.assertTrue(btn.backend_supported())
            self.assertEqual(btn.toolTip(), _TIP_NORMAL)
            btn.set_backend_supported(False)
            self.assertFalse(btn.isEnabled())
            self.assertFalse(btn.backend_supported())
            self.assertEqual(btn.toolTip(), _TIP_UNSUPPORTED)
            btn.set_backend_supported(True)
            self.assertTrue(btn.isEnabled())
            self.assertEqual(btn.toolTip(), _TIP_NORMAL)
        finally:
            btn.deleteLater()
            QTest.qWait(30)

    def test_greyed_refuses_every_user_driven_set(self) -> None:
        # setEnabled(False) only stops Qt event delivery — keymap actions
        # and MPRIS SetRate call set_speed()/reset() straight in, and
        # honoring them would walk the label to a speed the audio isn't
        # playing at
        btn = SpeedButton()
        emitted: list[float] = []
        btn.speed_changed.connect(emitted.append)
        try:
            btn.set_speed(1.25)
            self.assertEqual(emitted, [1.25])
            label_before = btn.text()
            btn.set_backend_supported(False)
            btn.set_speed(1.5)
            btn.reset()
            self.assertAlmostEqual(btn.speed(), 1.25, msg="value moved")
            self.assertEqual(btn.text(), label_before, "label lied")
            self.assertEqual(emitted, [1.25], "pushed to a deaf backend")
            btn.set_backend_supported(True)
            btn.set_speed(1.5)
            self.assertAlmostEqual(btn.speed(), 1.5)
            self.assertEqual(emitted, [1.25, 1.5])
        finally:
            btn.deleteLater()
            QTest.qWait(30)

    def test_greyed_still_accepts_a_silent_restore(self) -> None:
        # app.py's startup restore (emit=False) isn't a claim about
        # what's playing — it must still land
        btn = SpeedButton()
        try:
            btn.set_backend_supported(False)
            btn.set_speed(1.75, emit=False)
            self.assertAlmostEqual(btn.speed(), 1.75)
        finally:
            btn.deleteLater()
            QTest.qWait(30)

    def test_grey_out_closes_an_open_popover(self) -> None:
        host = QWidget()
        host._settings = SimpleNamespace(preset="modern")
        btn = SpeedButton(host)
        try:
            btn._open_popover()
            self.assertTrue(btn._popover.isVisible())
            btn.set_backend_supported(False)
            self.assertFalse(btn._popover.isVisible())
        finally:
            host.deleteLater()
            QTest.qWait(30)


class WindowGreyWiringTests(unittest.TestCase):
    """End-to-end through MainWindow: a resolved track that lands on a
    speed-less backend greys the strip's SpeedButton; landing back on
    mpv restores it; an active-source switch re-evaluates too."""

    def setUp(self) -> None:
        self.app = _app()
        # suppress app-wide QSS pushes (test_preset_flip's pattern)
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        router = PlaybackRouter()
        self.mpv = _FakeBackend("mpv", speed=True)
        self.spot = _FakeBackend("librespot", speed=False)
        router.register(self.mpv)
        router.register(self.spot)
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        self.w = MainWindow(LocalSource(), router)

    def tearDown(self) -> None:
        try:
            theming.manager().theme_changed.disconnect(
                self.w._on_theme_changed)
        except (RuntimeError, TypeError):
            pass
        self.w.close()
        self.w.deleteLater()
        self.w = None
        QTest.qWait(30)

    def _resolve(self, backend: str) -> None:
        self.w._current = Track(
            video_id="v1", title="t0", artists="a", source="local")
        self.w._on_resolved(
            "v1", StreamRef(backend=backend, payload="payload"))

    def test_starts_enabled_on_mpv(self) -> None:
        self.assertTrue(self.w.speed_btn.isEnabled())

    def test_track_on_librespot_greys_the_button(self) -> None:
        self._resolve("librespot")
        self.assertFalse(self.w.speed_btn.isEnabled())
        self.assertEqual(self.w.speed_btn.toolTip(), _TIP_UNSUPPORTED)

    def test_track_back_on_mpv_restores_the_button(self) -> None:
        self._resolve("librespot")
        self.assertFalse(self.w.speed_btn.isEnabled())
        self._resolve("mpv")
        self.assertTrue(self.w.speed_btn.isEnabled())
        self.assertEqual(self.w.speed_btn.toolTip(), _TIP_NORMAL)

    def test_active_source_switch_reevaluates(self) -> None:
        # flip the router under the window's feet, then deliver a source
        # switch — the handler must re-grey without a track load
        from tide.sources import registry as source_registry
        from tide.sources.local import LocalSource
        reg = source_registry()
        if reg.get("local") is None:
            reg.register(LocalSource())

            def _unregister() -> None:
                reg._sources.pop("local", None)
                reg._enabled.pop("local", None)

            self.addCleanup(_unregister)
        self.w.player.load_ref(
            StreamRef(backend="librespot", payload="payload"))
        self.assertTrue(self.w.speed_btn.isEnabled(), "stale by design")
        self.w._on_active_source_changed("local")
        self.assertFalse(self.w.speed_btn.isEnabled())

    def test_keymap_actions_respect_the_grey_out(self) -> None:
        # [ ] \ are window-level shortcuts calling the button directly —
        # the exact hole a disabled widget doesn't close
        from tide.ui.window import ACTIONS, _nudge_speed
        self.w.speed_btn.set_speed(1.25)
        self._resolve("librespot")
        self.assertFalse(self.w.speed_btn.isEnabled())
        label = self.w.speed_btn.text()
        pushed: list[float] = []
        self.w.speed_btn.speed_changed.connect(pushed.append)
        _nudge_speed(self.w, +1)
        _nudge_speed(self.w, -1)
        reset = next(a for a in ACTIONS if a.id == "speed_reset")
        reset.run(self.w)
        self.assertAlmostEqual(self.w.speed_btn.speed(), 1.25)
        self.assertEqual(self.w.speed_btn.text(), label)
        self.assertEqual(pushed, [])
        self._resolve("mpv")
        _nudge_speed(self.w, +1)
        self.assertGreater(self.w.speed_btn.speed(), 1.25)
        self.assertEqual(len(pushed), 1)

    def test_helper_tolerates_a_plain_player(self) -> None:
        # a player without the probe counts as supporting — grey only on
        # a positive "can't"
        self.w.speed_btn.set_backend_supported(False)
        orig = self.w.player
        try:
            self.w.player = SimpleNamespace()   # no active_supports_speed
            self.w._refresh_speed_support()
        finally:
            self.w.player = orig    # closeEvent needs the real router
        self.assertTrue(self.w.speed_btn.isEnabled())


# ---------- spring volume variant ----------


class SpringVolumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self._prev_intensity = motion._user_intensity
        self.addCleanup(motion.set_intensity, self._prev_intensity)
        self._prev_scale = scale.current()
        scale.set_factor("normal")
        self.addCleanup(scale.set_factor, self._prev_scale)
        motion.set_intensity(motion.Intensity.OFF)
        self.v = SpringVolume()
        self.v._slider.resize(400, 36)
        self.spy: list[int] = []
        self.v.volume_changed.connect(self.spy.append)

    def tearDown(self) -> None:
        self.v.deleteLater()
        self.v = None
        QTest.qWait(30)

    def test_registration(self) -> None:
        self.assertIn("spring", VOLUME_VARIANTS)
        made = [make_volume("spring"), make_volume("blocks"),
                make_volume("nope")]
        try:
            self.assertIsInstance(made[0], SpringVolume)
            self.assertIsInstance(made[1], MonoVolume)
            self.assertIsInstance(made[2], MonoVolume)
        finally:
            for w in made:      # they're wired to theme_changed — reap
                w.deleteLater()

    def test_surface_parity_with_the_other_faces(self) -> None:
        self.assertEqual(self.v.volume(), 80)
        self.v.setVolume(37)
        self.assertEqual(self.v.volume(), 37)
        self.assertEqual(self.spy, [37])
        self.v.setVolume(37)            # no-op dedupe
        self.assertEqual(self.spy, [37])
        self.v.setVolume(200)           # clamp high
        self.assertEqual(self.v.volume(), 100)
        self.v.setVolume(-5, emit=False)   # clamp low, silent
        self.assertEqual(self.v.volume(), 0)
        self.assertEqual(self.spy, [37, 100])

    def test_set_volume_moves_the_slider_silently(self) -> None:
        self.v.setVolume(25, emit=False)
        self.assertAlmostEqual(self.v._slider.value(), 25.0)
        self.assertEqual(self.spy, [])

    def test_wheel_steps_five(self) -> None:
        self.v._slider.wheelEvent(_WheelEv(120))
        self.assertEqual(self.v.volume(), 85)
        self.v._slider.wheelEvent(_WheelEv(-120))
        self.v._slider.wheelEvent(_WheelEv(-120))
        self.assertEqual(self.v.volume(), 75)
        self.assertEqual(self.spy, [85, 80, 75])

    def test_detent_click_lands_50_exactly(self) -> None:
        s = self.v._slider
        s.mousePressEvent(_MouseEv(s._x_for_value(50) + 2))
        s.mouseReleaseEvent(_MouseEv(s._x_for_value(50) + 2))
        self.assertEqual(self.v.volume(), 50)
        self.assertIn(50, self.spy)

    def test_drag_emits_ints_on_the_step_grid(self) -> None:
        s = self.v._slider
        s.mousePressEvent(_MouseEv(s._x_for_value(23.4)))
        s.mouseReleaseEvent(_MouseEv(s._x_for_value(23.4)))
        self.assertEqual(self.v.volume(), 23)
        for v in self.spy:
            self.assertIsInstance(v, int)

    def test_formatter_shows_percent(self) -> None:
        self.assertEqual(self.v._slider._bubble_text(), "80%")


if __name__ == "__main__":
    unittest.main()
