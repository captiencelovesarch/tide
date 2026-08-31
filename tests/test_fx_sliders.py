"""Phase 3 E2 — the fx rack's modern face + the debounce discipline.

Face pick: construction is always bracket (the view is built before
app.py attaches window._settings); modern lands spring on show; a flip
swaps faces on the next show, carrying values. Same rule for the popover.

Debounce: a drag never pushes the filter chain per tick — live changes
ride state_changed into the window's trailing debounce (~120 ms push /
~250 ms save). A committed interaction SHORTENS that debounce, never
flushes inline, so a burst of commits (wheel notches, key auto-repeat —
what motion OFF produces) costs exactly one push and one save. The
brutalist face is verbatim v1: no commits, today's timing. EQ bands
stay QSliders under both faces but join the commit discipline under
modern only. OFF commits inside the release stack; FULL settles first.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_fx_sliders.py
"""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSlider, QWidget

from tide import theming
from tide.settings import Settings
from tide.ui import audio_fx_popover as fx_popover_module
from tide.ui import audio_fx_view as fx_view_module
from tide.ui import motion, scale
from tide.ui.audio_fx_popover import (
    AudioFxButton,
    AudioFxPopover,
    SpringAudioFxPopover,
)
from tide.ui.audio_fx_view import (
    AudioFxView,
    _FxControl,
    _ShelfSlider,
    _SpringShelfSlider,
    _commit_fx_debounce,
)
from tide.ui.spring_slider import SpringSlider


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# Duck-typed events (test_spring_slider's pattern): deterministic
# offscreen, no QMouseEvent constructor-overload roulette.


class _MouseEv:
    def __init__(self, x: float, y: float = 15.0) -> None:
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
        self.accepted: bool | None = None

    def angleDelta(self) -> QPoint:
        return self._delta

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


class _Case(unittest.TestCase):
    """Motion OFF + normal scale, restored afterwards. OFF makes every
    spring settle land inside the interaction's call stack, so commit
    assertions are deterministic without qWait."""

    def setUp(self) -> None:
        self.app = _app()
        self._prev_intensity = motion._user_intensity
        self.addCleanup(motion.set_intensity, self._prev_intensity)
        self._prev_scale = scale.current()
        self.addCleanup(scale.set_factor, self._prev_scale)
        scale.set_factor("normal")
        motion.set_intensity(motion.Intensity.OFF)


# ---------- view face pick ----------


class ViewFacePickTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.host = QWidget()
        self.host._settings = SimpleNamespace(preset="modern")
        self.view = AudioFxView(parent=self.host)

    def tearDown(self) -> None:
        # deleteLater + drain — leaked widgets stay wired to theme_changed
        # and would soak up every later restyle in the suite.
        self.host.deleteLater()
        self.host = None
        self.view = None
        QTest.qWait(30)

    def test_construction_is_always_bracket(self) -> None:
        self.assertEqual(self.view.face(), "bracket")
        for ctrl in self.view._fx_controls():
            self.assertEqual(ctrl.face(), "bracket")
            self.assertIsInstance(ctrl._inner, _ShelfSlider)
        self.assertEqual(self.view.findChildren(SpringSlider), [])

    def test_modern_lands_spring_on_ensure_face(self) -> None:
        self.view._ensure_face()
        self.assertEqual(self.view.face(), "spring")
        for ctrl in self.view._fx_controls():
            self.assertEqual(ctrl.face(), "spring")
            self.assertIsInstance(ctrl._inner, _SpringShelfSlider)
        self.assertEqual(len(self.view.findChildren(SpringSlider)), 7)

    def test_show_event_lands_the_face(self) -> None:
        self.host.show()
        QTest.qWait(20)
        self.assertEqual(self.view.face(), "spring")
        self.host.hide()

    def test_brutalist_keeps_bracket(self) -> None:
        self.host._settings.preset = "brutalist"
        self.view._ensure_face()
        self.assertEqual(self.view.face(), "bracket")
        self.assertEqual(self.view.findChildren(SpringSlider), [])

    def test_no_settings_defaults_to_bracket(self) -> None:
        del self.host._settings
        self.view._ensure_face()
        self.assertEqual(self.view.face(), "bracket")

    def test_orphan_view_defaults_to_bracket(self) -> None:
        loner = AudioFxView()
        try:
            loner._ensure_face()
            self.assertEqual(loner.face(), "bracket")
        finally:
            loner.deleteLater()

    def test_flip_swaps_faces_and_preserves_values(self) -> None:
        self.view._ensure_face()
        self.view.state().bass_db = -3.0
        self.view.sync_from_state()
        self.assertAlmostEqual(self.view._bass_slider.value(), -3.0)
        self.host._settings.preset = "brutalist"
        self.view._ensure_face()
        self.assertEqual(self.view.face(), "bracket")
        self.assertIsInstance(self.view._bass_slider._inner, _ShelfSlider)
        self.assertAlmostEqual(self.view._bass_slider.value(), -3.0)

    def test_ensure_face_is_idempotent(self) -> None:
        self.view._ensure_face()
        inner = self.view._bass_slider._inner
        self.view._ensure_face()
        self.assertIs(self.view._bass_slider._inner, inner)

    def test_eq_bands_stay_qsliders_under_both_faces(self) -> None:
        # the vertical bands keep QSlider (SpringSlider is horizontal-only)
        self.view._ensure_face()
        for band in self.view._eq_bands:
            self.assertIsInstance(band._slider, QSlider)


# ---------- spring face behavior in the view ----------


class SpringViewBehaviorTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.host = QWidget()
        self.host._settings = SimpleNamespace(preset="modern")
        self.view = AudioFxView(parent=self.host)
        self.view._ensure_face()
        self.spy: list[object] = []
        self.view.state_changed.connect(self.spy.append)
        self.flush = mock.patch.object(
            fx_view_module, "_commit_fx_debounce").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.host = None
        self.view = None
        QTest.qWait(30)

    def _slider(self, ctrl: _FxControl) -> SpringSlider:
        s = ctrl._inner._slider
        s.resize(400, 30)
        return s

    def test_drag_updates_state_live_and_commits_once(self) -> None:
        s = self._slider(self.view._reverb_wet)
        s.mousePressEvent(_MouseEv(s._x_for_value(0.2)))
        self.assertAlmostEqual(self.view.state().reverb_wet, 0.2)
        self.assertGreaterEqual(len(self.spy), 1)
        self.assertEqual(self.flush.call_count, 0)
        s.mouseMoveEvent(_MouseEv(s._x_for_value(0.75)))
        self.assertAlmostEqual(self.view.state().reverb_wet, 0.75)
        self.assertEqual(self.flush.call_count, 0)
        s.mouseReleaseEvent(_MouseEv(s._x_for_value(0.75)))
        self.assertEqual(self.flush.call_count, 1)
        self.assertEqual(
            self.view._reverb_wet._inner._readout.text(), "75%")

    def test_wheel_nudge_commits_and_flushes(self) -> None:
        s = self._slider(self.view._reverb_wet)
        ev = _WheelEv(120)
        s.wheelEvent(ev)
        self.assertAlmostEqual(self.view.state().reverb_wet, 0.55)
        self.assertEqual(self.flush.call_count, 1)
        self.assertIs(ev.accepted, True)

    def test_dead_wheel_notch_falls_through_to_the_scroll_area(self) -> None:
        # the rack lives in a QScrollArea — a notch at the rail end must
        # let the page scroll instead of freezing under the pointer
        s = self._slider(self.view._reverb_wet)
        s.set_value(1.0)                # reverb wet is 0..1
        ev = _WheelEv(120)
        s.wheelEvent(ev)
        self.assertIs(ev.accepted, False)
        self.assertEqual(self.flush.call_count, 0)

    def test_detent_lands_neutral_exactly(self) -> None:
        self.view.state().bass_db = 4.0
        self.view.sync_from_state()
        s = self._slider(self.view._bass_slider)
        x = s._x_for_value(0.0) + 3   # inside the magnet radius
        s.mousePressEvent(_MouseEv(x))
        s.mouseReleaseEvent(_MouseEv(x))
        self.assertEqual(self.view.state().bass_db, 0.0)
        self.assertEqual(
            self.view._bass_slider._inner._readout.text(), "0 dB")

    def test_set_value_is_silent(self) -> None:
        self.view._reverb_wet.set_value(0.65)
        self.assertAlmostEqual(self.view._reverb_wet.value(), 0.65)
        self.assertEqual(self.spy, [])
        self.assertEqual(self.flush.call_count, 0)

    def test_eq_release_flushes_under_modern(self) -> None:
        band = self.view._eq_bands[0]
        band._slider.setValue(30)   # 3 dB — user-driven change
        self.assertEqual(self.flush.call_count, 0)
        band._slider.sliderReleased.emit()
        self.assertEqual(self.flush.call_count, 1)


class BracketViewBehaviorTests(_Case):
    """The brutalist rack: today's controls, today's timing, no commit
    machinery anywhere near it."""

    def setUp(self) -> None:
        super().setUp()
        self.view = AudioFxView()
        self.spy: list[object] = []
        self.view.state_changed.connect(self.spy.append)
        self.commits: list[float] = []
        for ctrl in self.view._fx_controls():
            ctrl.value_committed.connect(self.commits.append)
        self.flush = mock.patch.object(
            fx_view_module, "_commit_fx_debounce").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        self.view.deleteLater()
        self.view = None
        QTest.qWait(30)

    def test_bracket_slider_drives_state_without_commits(self) -> None:
        qs = self.view._bass_slider._inner._slider
        self.assertIsInstance(qs, QSlider)
        qs.setValue(8)   # step 0.5 → 4.0 dB
        self.assertAlmostEqual(self.view.state().bass_db, 4.0)
        self.assertEqual(len(self.spy), 1)
        self.assertEqual(self.commits, [])
        self.assertEqual(self.flush.call_count, 0)

    def test_eq_release_does_not_flush_under_brutalist(self) -> None:
        band = self.view._eq_bands[0]
        band._slider.setValue(30)
        band._slider.sliderReleased.emit()
        self.assertEqual(self.flush.call_count, 0)

    def test_no_spring_sliders_in_the_tree(self) -> None:
        self.assertEqual(self.view.findChildren(SpringSlider), [])


# ---------- the commit helper ----------


class _FakeDebounceWindow(QWidget):
    """Quacks like MainWindow's fx debounce corner: two lazy single-shot
    timers + the two flush methods."""

    def __init__(self) -> None:
        super().__init__()
        self.pushes = 0
        self.saves = 0
        self._audio_fx_push_timer = QTimer(self)
        self._audio_fx_push_timer.setSingleShot(True)
        self._audio_fx_push_timer.setInterval(120)
        self._audio_fx_save_timer = QTimer(self)
        self._audio_fx_save_timer.setSingleShot(True)
        self._audio_fx_save_timer.setInterval(250)

    def _flush_audio_fx_push(self) -> None:
        self.pushes += 1

    def _flush_audio_fx_state(self) -> None:
        self.saves += 1


class CommitHelperTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.win = _FakeDebounceWindow()

    def tearDown(self) -> None:
        self.win.deleteLater()
        self.win = None
        QTest.qWait(30)

    def _drain(self) -> None:
        QTest.qWait(fx_view_module.COMMIT_DRAIN_MS + 60)

    def test_a_commit_shortens_the_debounce_instead_of_flushing(self) -> None:
        self.win._audio_fx_push_timer.start()
        self.win._audio_fx_save_timer.start()
        child = QWidget(self.win)
        _commit_fx_debounce(child)
        # not inline: with motion off commits arrive per notch/repeat, and
        # each inline flush is a 34-62 ms mpv rebuild plus a TOML write
        self.assertEqual(self.win.pushes, 0)
        self.assertEqual(self.win.saves, 0)
        self._drain()
        self.assertEqual(self.win.pushes, 1)
        self.assertEqual(self.win.saves, 1)
        self.assertFalse(self.win._audio_fx_push_timer.isActive())
        self.assertFalse(self.win._audio_fx_save_timer.isActive())

    def test_it_still_beats_the_untouched_debounce(self) -> None:
        self.assertLess(fx_view_module.COMMIT_DRAIN_MS,
                        self.win._audio_fx_push_timer.interval())

    def test_a_burst_of_commits_drains_once(self) -> None:
        # 6 notches used to mean 6 synchronous rebuilds and 6 TOML writes
        child = QWidget(self.win)
        for _ in range(6):
            self.win._audio_fx_push_timer.start()
            self.win._audio_fx_save_timer.start()
            _commit_fx_debounce(child)
            QTest.qWait(2)
        self.assertEqual(self.win.pushes, 0, "pushed mid-burst")
        self._drain()
        self.assertEqual(self.win.pushes, 1)
        self.assertEqual(self.win.saves, 1)

    def test_idle_timers_mean_nothing_pending(self) -> None:
        # no active timer → nothing to cut short → nothing scheduled (a
        # flush would just re-write the same TOML)
        _commit_fx_debounce(QWidget(self.win))
        self.assertIsNone(getattr(self.win, "_audio_fx_commit_timer", None),
                          "an idle rack must not arm a timer")
        self._drain()
        self.assertEqual(self.win.pushes, 0)
        self.assertEqual(self.win.saves, 0)

    def test_one_active_timer_drains_only_that_side(self) -> None:
        self.win._audio_fx_push_timer.start()
        _commit_fx_debounce(QWidget(self.win))
        self._drain()
        self.assertEqual(self.win.pushes, 1)
        self.assertEqual(self.win.saves, 0)

    def test_popup_popover_walks_parents_to_the_window(self) -> None:
        # Qt.Popup makes the popover its own window() — the helper must
        # walk parent()s (the _refresh_hint pattern)
        pop = SpringAudioFxPopover(self.win)
        self.win._audio_fx_push_timer.start()
        _commit_fx_debounce(pop)
        self._drain()
        self.assertEqual(self.win.pushes, 1)
        pop.deleteLater()

    def test_the_drain_timer_dies_with_its_host(self) -> None:
        self.win._audio_fx_push_timer.start()
        _commit_fx_debounce(QWidget(self.win))
        timer = self.win._audio_fx_commit_timer
        self.assertIs(timer.parent(), self.win)
        self.win.deleteLater()
        self.win = None
        QTest.qWait(fx_view_module.COMMIT_DRAIN_MS + 60)   # must not crash
        self.win = _FakeDebounceWindow()   # tearDown still has a widget

    def test_no_debounce_window_is_a_silent_noop(self) -> None:
        loner = QWidget()
        try:
            _commit_fx_debounce(loner)   # must not raise
        finally:
            loner.deleteLater()


# ---------- popover face pick ----------


class _ButtonCase(_Case):
    PRESET = "modern"

    def setUp(self) -> None:
        super().setUp()
        self.host = QWidget()
        self.host._settings = SimpleNamespace(preset=self.PRESET)
        self.btn = AudioFxButton(parent=self.host)

    def tearDown(self) -> None:
        if self.btn._popover is not None:
            self.btn._popover.hide()
        self.host.deleteLater()
        self.host = None
        self.btn = None
        QTest.qWait(30)

    def _open(self):
        self.btn._open_popover()
        return self.btn._popover


class ButtonFacePickTests(_ButtonCase):
    def test_modern_preset_builds_the_spring_face(self) -> None:
        pop = self._open()
        self.assertIsInstance(pop, SpringAudioFxPopover)

    def test_brutalist_preset_keeps_the_bracket_face(self) -> None:
        self.host._settings.preset = "brutalist"
        pop = self._open()
        self.assertIsInstance(pop, AudioFxPopover)
        self.assertNotIsInstance(pop, SpringAudioFxPopover)
        self.assertIsInstance(pop._wet_slider, QSlider)

    def test_no_preset_defaults_to_bracket(self) -> None:
        self.host._settings.preset = ""
        self.assertNotIsInstance(self._open(), SpringAudioFxPopover)

    def test_no_settings_defaults_to_bracket(self) -> None:
        del self.host._settings
        self.assertNotIsInstance(self._open(), SpringAudioFxPopover)

    def test_flip_swaps_face_on_next_open(self) -> None:
        self.host._settings.preset = "brutalist"
        first = self._open()
        self.assertNotIsInstance(first, SpringAudioFxPopover)
        first.hide()
        self.host._settings.preset = "modern"
        second = self._open()
        self.assertIsInstance(second, SpringAudioFxPopover)
        self.assertIsNot(second, first)

    def test_same_face_reopen_reuses_the_popover(self) -> None:
        first = self._open()
        first.hide()
        self.assertIs(self._open(), first)

    def test_spring_face_seeds_from_the_button_state(self) -> None:
        self.btn._state.bass_db = 3.0
        pop = self._open()
        self.assertAlmostEqual(pop._bass_slider.value(), 3.0)
        self.assertEqual(pop._bass_read.text(), "+3 dB")


# ---------- spring popover behavior ----------


class SpringPopoverTests(_ButtonCase):
    def setUp(self) -> None:
        super().setUp()
        self.spy: list[object] = []
        self.btn.state_changed.connect(self.spy.append)
        self.pop = self._open()
        self.flush = mock.patch.object(
            fx_popover_module, "_commit_fx_debounce").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        self.pop = None
        super().tearDown()

    def _wet(self) -> SpringSlider:
        s = self.pop._wet_slider
        s.resize(400, 30)
        return s

    def test_wet_drag_flows_live_then_flushes_on_release(self) -> None:
        s = self._wet()
        s.mousePressEvent(_MouseEv(s._x_for_value(0.2)))
        self.assertAlmostEqual(self.btn.state().reverb_wet, 0.2)
        self.assertGreaterEqual(len(self.spy), 1)   # live, pre-release
        s.mouseMoveEvent(_MouseEv(s._x_for_value(0.75)))
        self.assertAlmostEqual(self.btn.state().reverb_wet, 0.75)
        self.assertEqual(self.pop._wet_read.text(), "75%")
        self.assertEqual(self.flush.call_count, 0)
        s.mouseReleaseEvent(_MouseEv(s._x_for_value(0.75)))
        self.assertEqual(self.flush.call_count, 1)

    def test_wet_detent_magnetizes_50_percent(self) -> None:
        s = self._wet()
        x = s._x_for_value(0.5) + 3
        self.btn._state.reverb_wet = 0.9
        self.btn.set_state(self.btn._state)   # popover visible → syncs
        s.mousePressEvent(_MouseEv(x))
        s.mouseReleaseEvent(_MouseEv(x))
        self.assertEqual(self.btn.state().reverb_wet, 0.5)

    def test_shelf_detent_magnetizes_zero_db(self) -> None:
        self.btn._state.bass_db = 4.0
        self.btn.set_state(self.btn._state)
        s = self.pop._bass_slider
        s.resize(400, 30)
        x = s._x_for_value(0.0) + 3
        s.mousePressEvent(_MouseEv(x))
        s.mouseReleaseEvent(_MouseEv(x))
        self.assertEqual(self.btn.state().bass_db, 0.0)
        self.assertEqual(self.pop._bass_read.text(), "0 dB")

    def test_sync_never_yanks_a_live_drag(self) -> None:
        s = self._wet()
        s.mousePressEvent(_MouseEv(s._x_for_value(0.2)))
        x = s._x_for_value(0.42)
        s.mouseMoveEvent(_MouseEv(x))
        # off-grid finger: display follows raw, logic quantizes
        self.assertAlmostEqual(self.btn.state().reverb_wet, 0.4)
        self.assertAlmostEqual(s._display, 0.42, places=2)
        self.btn.set_state(self.btn._state)   # external sync mid-drag
        self.assertAlmostEqual(s._display, 0.42, places=2)
        s.mouseReleaseEvent(_MouseEv(x))

    def test_state_changed_carries_the_shared_instance(self) -> None:
        s = self._wet()
        s.mousePressEvent(_MouseEv(s._x_for_value(0.3)))
        s.mouseReleaseEvent(_MouseEv(s._x_for_value(0.3)))
        for st in self.spy:
            self.assertIs(st, self.btn.state())


class PopoverHintTokenTests(_Case):
    """The hint used to hardcode palette(mid) — it now routes through
    the theme's dim token (hex fallback), on both faces."""

    def _check(self, pop) -> None:
        try:
            sheet = pop._hint.styleSheet()
            self.assertNotIn("palette(", sheet)
            self.assertTrue(sheet.startswith("color: #"), sheet)
        finally:
            pop.deleteLater()
            QTest.qWait(10)

    def test_bracket_hint_uses_the_dim_token(self) -> None:
        self._check(AudioFxPopover(None))

    def test_spring_hint_uses_the_dim_token(self) -> None:
        self._check(SpringAudioFxPopover(None))


# ---------- the debounce, counted end-to-end through MainWindow ----------


class WindowDebounceTests(unittest.TestCase):
    """A real MainWindow, a mocked player, and counted pushes. One fx
    push costs 34-62 ms GUI-thread and cuts the reverb tail, so the
    counts ARE the contract."""

    def setUp(self) -> None:
        self.app = _app()
        # suppress real app-wide QSS pushes (test_preset_flip's pattern)
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self._prev_intensity = motion._user_intensity
        self.addCleanup(motion.set_intensity, self._prev_intensity)
        self._prev_scale = scale.current()
        self.addCleanup(scale.set_factor, self._prev_scale)
        scale.set_factor("normal")
        motion.set_intensity(motion.Intensity.OFF)
        from tide.playback import PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        self.w = MainWindow(LocalSource(), PlaybackRouter())
        s = Settings()
        s.preset = ""
        self.w._settings = s
        state = self.w.audio_fx_view.state()
        state.master_enabled = True   # a live rack → non-empty chains
        self.w.audio_fx_view.sync_from_state()
        self.push = mock.patch.object(
            self.w.player, "set_audio_filter_chain").start()
        self.save = mock.patch("tide.settings.save_fields").start()

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

    def _go_modern(self):
        self.w._settings.preset = "modern"
        view = self.w.audio_fx_view
        view._ensure_face()
        self.assertEqual(view.face(), "spring")
        return view

    def _drain(self) -> None:
        """Let the commit's shortened debounce land."""
        QTest.qWait(fx_view_module.COMMIT_DRAIN_MS + 60)

    def test_spring_drag_never_pushes_per_tick_and_flushes_on_release(self) -> None:
        view = self._go_modern()
        s = view._bass_slider._inner._slider
        s.resize(400, 30)
        s.mousePressEvent(_MouseEv(s._x_for_value(-6)))
        for db in (-4, -2, 2, 5):
            # real time passes mid-drag; every tick restarts the 120 ms
            # trailing timer, so nothing may fire in between
            QTest.qWait(40)
            s.mouseMoveEvent(_MouseEv(s._x_for_value(db)))
        self.assertEqual(self.push.call_count, 0)
        self.assertEqual(self.save.call_count, 0)
        s.mouseReleaseEvent(_MouseEv(s._x_for_value(5)))
        # the commit shortens the debounce rather than pushing inline
        self.assertEqual(self.push.call_count, 0)
        self._drain()
        self.assertEqual(self.push.call_count, 1)
        self.assertIn("bass=g=5", self.push.call_args[0][0])
        self.assertEqual(self.save.call_count, 1)
        QTest.qWait(500)
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.save.call_count, 1)

    def test_wheel_burst_at_motion_off_pushes_once(self) -> None:
        # modern face + motion off = no settle window, every notch commits
        view = self._go_modern()
        s = view._bass_slider._inner._slider
        s.resize(400, 30)
        for _ in range(6):
            s.wheelEvent(_WheelEv(120))
            QTest.qWait(2)      # a fast spin, well inside the drain window
        self.assertEqual(self.push.call_count, 0, "pushed mid-burst")
        self.assertEqual(self.save.call_count, 0, "wrote TOML mid-burst")
        self._drain()
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.save.call_count, 1)
        self.assertIn("bass=g=3", self.push.call_args[0][0])

    def test_key_repeat_burst_at_motion_off_pushes_once(self) -> None:
        view = self._go_modern()
        s = view._bass_slider._inner._slider
        s.resize(400, 30)
        s.setFocus()
        for _ in range(12):
            QTest.keyClick(s, Qt.Key_Right)
        self.assertEqual(self.push.call_count, 0)
        self._drain()
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.save.call_count, 1)

    def test_release_on_the_start_value_pushes_at_most_once(self) -> None:
        view = self._go_modern()
        s = view._bass_slider._inner._slider
        s.resize(400, 30)
        x = s._x_for_value(0.0) + 3   # magnet: press lands the current 0.0
        s.mousePressEvent(_MouseEv(x))
        s.mouseReleaseEvent(_MouseEv(x))
        self.assertEqual(self.push.call_count, 0)
        self.assertEqual(self.save.call_count, 0)

    def test_brutalist_drag_keeps_the_trailing_debounce(self) -> None:
        view = self.w.audio_fx_view
        self.assertEqual(view.face(), "bracket")
        qs = view._bass_slider._inner._slider
        for raw in (2, 4, 6, 8):   # user-driven ticks, 0.5 dB steps
            qs.setValue(raw)
        self.assertEqual(self.push.call_count, 0)
        QTest.qWait(500)
        self.assertEqual(self.push.call_count, 1)
        self.assertIn("bass=g=4", self.push.call_args[0][0])
        self.assertEqual(self.save.call_count, 1)

    def test_eq_release_flushes_modern_but_not_brutalist(self) -> None:
        view = self.w.audio_fx_view
        band = view._eq_bands[2]
        band._slider.setValue(30)   # 3 dB tick → scheduled
        band._slider.sliderReleased.emit()
        self.assertEqual(self.push.call_count, 0)   # brutalist: no commit
        QTest.qWait(500)
        self.assertEqual(self.push.call_count, 1)   # ...only the timer
        self._go_modern()
        band._slider.setValue(50)   # 5 dB
        self.assertEqual(self.push.call_count, 1)
        band._slider.sliderReleased.emit()
        self._drain()
        # modern: release shortens the debounce; the second push lands
        # before the trailing edge would deliver it
        self.assertEqual(self.push.call_count, 2)

    def test_popover_commit_flushes_through_the_parent_walk(self) -> None:
        self.w._settings.preset = "modern"
        btn = self.w.audio_fx_btn
        btn._state.master_enabled = True
        btn._open_popover()
        pop = btn._popover
        try:
            self.assertIsInstance(pop, SpringAudioFxPopover)
            s = pop._wet_slider
            s.resize(400, 30)
            s.mousePressEvent(_MouseEv(s._x_for_value(0.2)))
            s.mouseMoveEvent(_MouseEv(s._x_for_value(0.8)))
            self.assertEqual(self.push.call_count, 0)
            s.mouseReleaseEvent(_MouseEv(s._x_for_value(0.8)))
            self._drain()
            self.assertEqual(self.push.call_count, 1)
        finally:
            pop.hide()


# ---------- motion gating ----------


class MotionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self._prev_intensity = motion._user_intensity
        self.addCleanup(motion.set_intensity, self._prev_intensity)
        self._prev_scale = scale.current()
        self.addCleanup(scale.set_factor, self._prev_scale)
        scale.set_factor("normal")
        self.commits: list[float] = []
        self.widget = _SpringShelfSlider("bass", -12.0, 12.0, 0.5)
        self.widget.value_committed.connect(self.commits.append)
        self.slider = self.widget._slider
        self.slider.resize(400, 30)

    def tearDown(self) -> None:
        self.widget.deleteLater()
        self.widget = None
        self.slider = None
        QTest.qWait(50)

    def test_off_commits_inside_the_release_stack(self) -> None:
        motion.set_intensity(motion.Intensity.OFF)
        self.slider.mousePressEvent(
            _MouseEv(self.slider._x_for_value(6.0)))
        self.slider.mouseReleaseEvent(
            _MouseEv(self.slider._x_for_value(6.0)))
        self.assertEqual(self.commits, [6.0])   # synchronous — no waiting

    def test_full_settles_first_then_commits_once(self) -> None:
        motion.set_intensity(motion.Intensity.FULL)
        self.slider.mousePressEvent(
            _MouseEv(self.slider._x_for_value(6.0)))
        self.slider.mouseReleaseEvent(
            _MouseEv(self.slider._x_for_value(6.0)))
        self.assertEqual(self.commits, [])
        QTest.qWait(700)
        self.assertEqual(self.commits, [6.0])


if __name__ == "__main__":
    unittest.main()
