"""SpringSlider — the modern personality's magnetic-detent spring slider.

What's pinned here:
- detent snap math: ~6px magnet radius in value-space, detents win over
  the step grid (even off-grid detents), radius scales with groove width;
- step quantization on drag / programmatic sets, range renormalization;
- wheel = one step per notch (with sub-notch accumulation), arrow keys /
  Home / End, bound nudges emit nothing;
- motion OFF ⇒ every settle is a synchronous snap and no animation
  object is ever created (the brutalist zero-animation contract);
- motion on ⇒ the settle animates then fires value_committed exactly
  once, with rapid retargets coalescing into a single commit;
- the modern personality gets the springy OutBack overshoot, brutalist
  stays mechanical at every intensity (the dialect follows the
  personality, never the motion level);
- painting reads theming tokens at paint time (current_effective), with
  hex fallbacks when no theme is applied;
- the value bubble fits: at the widget's own sizeHint it clears the
  handle disc and the tick band, and a host that pins us shorter gets no
  bubble at all rather than one painted over the ticks;
- a theme tick is a repaint, never a layout invalidation (theme_changed
  is a ~10 Hz bus under the adaptive driver);
- teardown mid-settle / mid-drag doesn't crash and leaves no timer.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_spring_slider.py
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.ui import motion, scale
from tide.ui.spring_slider import SpringSlider


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _MouseEv:
    """Duck-typed mouse event — deterministic offscreen, no QMouseEvent
    overload roulette. Only ever a left-button event; the handlers route
    non-left buttons to super(), which these fakes never exercise."""

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
        self.accepted: bool | None = None

    def angleDelta(self) -> QPoint:
        return self._delta

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


class _SliderCase(unittest.TestCase):
    """Speed-law-shaped slider: range 0.5..2.0, step 0.05, the preset
    detents. Wide enough that 6px is a small value-space radius."""

    def setUp(self) -> None:
        self.app = _app()
        self._prev_intensity = motion._user_intensity
        self.addCleanup(motion.set_intensity, self._prev_intensity)
        # The motion dialect is process-global and sticky: any earlier
        # test file that applied a preset would otherwise pick this
        # widget's curves. Start from "nothing bound, no reduced-motion
        # clamp" and put it all back afterwards.
        for name in ("_profile_override", "_preset_profile", "_reduced_motion"):
            self.addCleanup(setattr, motion, name, getattr(motion, name))
        motion._profile_override = None
        motion._preset_profile = None
        motion._reduced_motion = False
        self._prev_scale = scale.current()
        scale.set_factor("normal")
        self.addCleanup(scale.set_factor, self._prev_scale)
        self.s = SpringSlider()
        self.s.resize(400, 36)
        self.s.set_range(0.5, 2.0, 0.05)
        self.s.set_detents([0.5, 0.75, 1.0, 1.25, 1.5, 2.0])
        self.s.set_value(1.0)
        self.changed: list[float] = []
        self.committed: list[float] = []
        self.s.value_changed.connect(self.changed.append)
        self.s.value_committed.connect(self.committed.append)

    def tearDown(self) -> None:
        # deleteLater + drain: a leaked widget stays connected to
        # theme_changed and gets repainted by every later theme apply.
        self.s.deleteLater()
        self.s = None
        QTest.qWait(30)

    # helpers ------------------------------------------------------------

    def _press(self, x: float) -> None:
        self.s.mousePressEvent(_MouseEv(x))

    def _move(self, x: float) -> None:
        self.s.mouseMoveEvent(_MouseEv(x))

    def _release(self, x: float) -> None:
        self.s.mouseReleaseEvent(_MouseEv(x))

    def _click(self, x: float) -> None:
        self._press(x)
        self._release(x)

    def _wheel(self, dy: int) -> _WheelEv:
        ev = _WheelEv(dy)
        self.s.wheelEvent(ev)
        return ev

    def _wait_committed(self, n: int = 1, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.committed) < n and time.monotonic() < deadline:
            QTest.qWait(10)

    def _off(self) -> None:
        motion.set_intensity(motion.Intensity.OFF)

    def _lite(self) -> None:
        motion.set_intensity(motion.Intensity.LITE)


class DetentSnapTests(_SliderCase):
    def test_press_within_radius_snaps_to_detent(self) -> None:
        self._off()
        x = self.s._x_for_value(1.0) + 4    # inside the 6px magnet
        self._click(x)
        self.assertAlmostEqual(self.s.value(), 1.0)

    def test_press_outside_radius_quantizes_instead(self) -> None:
        self._off()
        x = self.s._x_for_value(1.0) + 20   # well outside the magnet
        self._click(x)
        v = self.s.value()
        self.assertNotAlmostEqual(v, 1.0)
        # …but still on the 0.05 grid.
        self.assertAlmostEqual((v - 0.5) / 0.05, round((v - 0.5) / 0.05),
                               places=6)

    def test_off_grid_detent_wins_exactly(self) -> None:
        # A detent that isn't on the step grid must come back verbatim —
        # detents beat quantization.
        self._off()
        self.s.set_detents([1.333])
        self._click(self.s._x_for_value(1.333) + 3)
        self.assertEqual(self.s.value(), 1.333)

    def test_magnet_radius_is_pixel_space(self) -> None:
        # Same 5px offset, narrower widget: the value-space radius grows,
        # the snap still lands.
        self._off()
        self.s.resize(140, 36)
        self._click(self.s._x_for_value(1.25) + 5)
        self.assertAlmostEqual(self.s.value(), 1.25)

    def test_out_of_range_detents_are_inert(self) -> None:
        self._off()
        self.s.set_detents([0.1, 5.0])      # both outside 0.5..2.0
        self._click(self.s._x_for_value(1.1) + 1)
        self.assertAlmostEqual(self.s.value(), 1.1)

    def test_detent_sticks_the_display_during_drag(self) -> None:
        self._off()
        self._press(self.s._x_for_value(0.75))
        self._move(self.s._x_for_value(1.5) + 4)    # inside 1.5's magnet
        self.assertAlmostEqual(self.s.value(), 1.5)
        self.assertAlmostEqual(self.s._display, 1.5)
        # …but between detents the handle follows the finger raw.
        x_between = self.s._x_for_value(1.1)
        self._move(x_between)
        self.assertAlmostEqual(self.s._display, 1.1, places=2)
        self._release(x_between)


class StepAndRangeTests(_SliderCase):
    def test_set_value_quantizes_clamps_and_stays_silent(self) -> None:
        self.s.set_value(1.313)
        self.assertAlmostEqual(self.s.value(), 1.3)
        self.s.set_value(3.7)
        self.assertAlmostEqual(self.s.value(), 2.0)
        self.s.set_value(-1)
        self.assertAlmostEqual(self.s.value(), 0.5)
        self.assertEqual(self.changed, [])
        self.assertEqual(self.committed, [])

    def test_set_range_requantizes_current_value(self) -> None:
        self.s.set_value(1.3)
        self.s.set_range(0.0, 1.0, 0.1)
        self.assertAlmostEqual(self.s.value(), 1.0)
        self.assertAlmostEqual(self.s._display, 1.0)

    def test_reversed_range_normalizes(self) -> None:
        self.s.set_range(2.0, 0.5, 0.05)
        self.s.set_value(0.9)
        self.assertAlmostEqual(self.s.value(), 0.9)

    def test_zero_step_is_continuous(self) -> None:
        self.s.set_range(0.0, 1.0, 0.0)
        self.s.set_value(0.123456)
        self.assertAlmostEqual(self.s.value(), 0.123456)


class WheelKeyTests(_SliderCase):
    def test_wheel_up_is_one_step(self) -> None:
        self._off()
        self._wheel(120)
        self.assertAlmostEqual(self.s.value(), 1.05)
        self.assertEqual(len(self.changed), 1)
        self.assertEqual(len(self.committed), 1)
        self.assertAlmostEqual(self.committed[0], 1.05)

    def test_wheel_down_is_one_step(self) -> None:
        self._off()
        self._wheel(-120)
        self.assertAlmostEqual(self.s.value(), 0.95)

    def test_wheel_at_bound_emits_nothing(self) -> None:
        self._off()
        self.s.set_value(2.0)
        ev = self._wheel(120)
        self.assertAlmostEqual(self.s.value(), 2.0)
        self.assertEqual(self.changed, [])
        self.assertEqual(self.committed, [])
        # A notch that moved nothing belongs to whatever scrolls behind
        # us — these sliders sit inside the fx rack's QScrollArea, and
        # swallowing dead notches would freeze the page under the
        # pointer.
        self.assertIs(ev.accepted, False)

    def test_wheel_sub_notch_accumulates(self) -> None:
        self._off()
        ev = self._wheel(60)     # half a notch — nothing yet
        self.assertAlmostEqual(self.s.value(), 1.0)
        self.assertEqual(self.changed, [])
        self.assertIs(ev.accepted, False, "sub-notch must pass through")
        ev = self._wheel(60)     # completes the notch — one step
        self.assertAlmostEqual(self.s.value(), 1.05)
        self.assertEqual(len(self.changed), 1)
        self.assertIs(ev.accepted, True)

    def test_arrow_keys_step(self) -> None:
        self._off()
        QTest.keyClick(self.s, Qt.Key_Right)
        self.assertAlmostEqual(self.s.value(), 1.05)
        QTest.keyClick(self.s, Qt.Key_Left)
        QTest.keyClick(self.s, Qt.Key_Left)
        self.assertAlmostEqual(self.s.value(), 0.95)
        self.assertEqual(len(self.committed), 3)

    def test_home_end(self) -> None:
        self._off()
        QTest.keyClick(self.s, Qt.Key_End)
        self.assertAlmostEqual(self.s.value(), 2.0)
        QTest.keyClick(self.s, Qt.Key_Home)
        self.assertAlmostEqual(self.s.value(), 0.5)
        self.assertEqual(self.committed, [2.0, 0.5])

    def test_key_at_bound_emits_nothing(self) -> None:
        self._off()
        self.s.set_value(0.5)
        QTest.keyClick(self.s, Qt.Key_Left)
        self.assertEqual(self.changed, [])
        self.assertEqual(self.committed, [])


class MotionOffTests(_SliderCase):
    """The brutalist contract: OFF = zero animation, synchronous snaps."""

    def test_click_commits_synchronously(self) -> None:
        self._off()
        self._click(self.s._x_for_value(1.5) + 2)
        self.assertAlmostEqual(self.s.value(), 1.5)
        self.assertAlmostEqual(self.s._display, 1.5)
        self.assertEqual(self.committed, [1.5])
        self.assertIsNone(self.s._settle,
                          "motion OFF created a settle animation")

    def test_release_always_commits_even_unchanged(self) -> None:
        # Consumers flush pending debounce on committed — a round-trip
        # drag must still deliver it.
        self._off()
        self._click(self.s._x_for_value(1.0))
        self.assertEqual(self.changed, [])
        self.assertEqual(self.committed, [1.0])

    def test_drag_emits_changed_live_and_dedupes(self) -> None:
        self._off()
        self._press(self.s._x_for_value(0.75))
        self._move(self.s._x_for_value(1.25) + 1)
        self._move(self.s._x_for_value(1.25) + 2)   # same snapped value
        self._release(self.s._x_for_value(1.25) + 2)
        self.assertEqual(self.changed, [0.75, 1.25])
        self.assertEqual(self.committed, [1.25])

    def test_animated_set_value_snaps(self) -> None:
        self._off()
        self.s.set_value(1.5, animate=True)
        self.assertAlmostEqual(self.s._display, 1.5)
        self.assertIsNone(self.s._settle)
        self.assertEqual(self.committed, [])


class MotionOnTests(_SliderCase):
    def test_settle_animates_then_commits_once(self) -> None:
        self._lite()
        target_x = self.s._x_for_value(2.0)
        self._click(target_x)
        self.assertAlmostEqual(self.s.value(), 2.0)
        self.assertEqual(len(self.changed), 1)
        self.assertEqual(self.committed, [],
                         "commit fired before the settle landed")
        self.assertIsNotNone(self.s._settle)
        self.assertLess(self.s._display, 2.0,
                        "display jumped instead of settling")
        self._wait_committed()
        self.assertEqual(self.committed, [2.0])
        self.assertAlmostEqual(self.s._display, 2.0)
        self.assertIsNone(self.s._settle,
                          "settle animation not cleared after landing")

    def test_rapid_nudges_coalesce_into_one_commit(self) -> None:
        self._lite()
        self._wheel(120)
        self._wheel(120)
        self.assertAlmostEqual(self.s.value(), 1.10)
        self.assertEqual(len(self.changed), 2)
        self._wait_committed()
        QTest.qWait(50)     # would catch a straggler double-commit
        self.assertEqual(self.committed, [1.10],
                         "retargeted settles must coalesce to ONE commit")

    def test_drag_move_cancels_the_press_settle(self) -> None:
        self._lite()
        self._press(self.s._x_for_value(2.0))
        self.assertIsNotNone(self.s._settle)
        self._move(self.s._x_for_value(0.6))
        self.assertIsNone(self.s._settle,
                          "drag tracking must cancel the click-jump settle")
        self._release(self.s._x_for_value(0.6))
        self._wait_committed()
        self.assertEqual(len(self.committed), 1)

    def test_settle_rides_motion_spring_settle(self) -> None:
        self._lite()
        with mock.patch.object(motion, "spring_settle",
                               wraps=motion.spring_settle) as spy:
            self._click(self.s._x_for_value(2.0))
        self.assertTrue(spy.called,
                        "the settle must route through motion.spring_settle")
        self.assertIs(spy.call_args.kwargs.get("owner"), self.s,
                      "the settle animation must die with the widget")
        self._wait_committed()

    def test_modern_settles_with_the_springy_overshoot(self) -> None:
        # The repealed law: bouncy is the MODERN dialect — the
        # personality picks it, not the intensity (a brutalist user at
        # full motion still gets mechanical curves, see
        # test_motion_profiles).
        motion.bind_preset("modern")
        with mock.patch.object(motion, "intensity",
                               return_value=motion.Intensity.FULL):
            self._click(self.s._x_for_value(2.0))
            anim = self.s._settle
            self.assertIsNotNone(anim)
            self.assertEqual(anim.easingCurve().type(), QEasingCurve.OutBack)
        self._wait_committed()
        self.assertEqual(self.committed, [2.0])

    def test_brutalist_settle_has_no_bounce(self) -> None:
        # Mechanical is the brutalist dialect at every intensity —
        # decisive, never OutBack.
        motion.bind_preset("brutalist")
        motion.set_intensity(motion.Intensity.FULL)
        self._click(self.s._x_for_value(2.0))
        anim = self.s._settle
        self.assertIsNotNone(anim)
        self.assertNotEqual(anim.easingCurve().type(), QEasingCurve.OutBack)
        self._wait_committed()

    def test_programmatic_animate_stays_silent(self) -> None:
        self._lite()
        self.s.set_value(1.5, animate=True)
        self.assertIsNotNone(self.s._settle)
        QTest.qWait(50)
        deadline = time.monotonic() + 2.0
        while self.s._settle is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertAlmostEqual(self.s._display, 1.5)
        self.assertEqual(self.changed, [])
        self.assertEqual(self.committed, [])


class FormatterTests(_SliderCase):
    def test_default_formatter(self) -> None:
        self.assertEqual(self.s._bubble_text(), "1")

    def test_custom_formatter(self) -> None:
        self.s.set_formatter(lambda v: f"{v:.2f}×")
        self.assertEqual(self.s._bubble_text(), "1.00×")

    def test_broken_formatter_never_breaks_paint(self) -> None:
        def _boom(_v):
            raise ValueError("nope")
        self.s.set_formatter(_boom)
        self.assertEqual(self.s._bubble_text(), "1")
        self._off()
        self._press(self.s._x_for_value(1.5))   # bubble path is live
        self.s.grab()                            # must not raise
        self._release(self.s._x_for_value(1.5))


class PaintTests(_SliderCase):
    def test_paint_reads_tokens_at_paint_time(self) -> None:
        fake = theming.Theme(
            slug="fake", name="fake", path=Path("."),
            tokens={
                "accent": "#ff0000", "fg": "#ffffff", "dim": "#666666",
                "border_dim": "#2a2a2a", "bg_alt": "#101010",
            },
        )
        self.s.set_value(2.0)   # fill spans the whole groove
        with mock.patch.object(theming.manager(), "current_effective",
                               return_value=fake):
            img = self.s.grab().toImage()
        g = self.s._groove_rect()
        c = img.pixelColor(int(g.center().x()), int(g.center().y()))
        self.assertGreater(c.red(), 150, "groove fill ignored the accent token")
        self.assertLess(c.green(), 80)
        self.assertLess(c.blue(), 80)

    def test_paint_without_theme_uses_fallback_hexes(self) -> None:
        with mock.patch.object(theming.manager(), "current_effective",
                               return_value=None):
            self.s.grab()   # must not raise

    def test_paint_during_drag_draws_the_bubble(self) -> None:
        self._off()
        self.s.set_formatter(lambda v: f"{v:.2f}×")
        self._press(self.s._x_for_value(1.25))
        self.s.grab()       # bubble branch executes
        self._release(self.s._x_for_value(1.25))

    def test_disabled_paints_muted(self) -> None:
        self.s.setEnabled(False)
        self.s.grab()       # must not raise


class BubbleLayoutTests(_SliderCase):
    """The signature readout must not cover what it annotates."""

    def _tick_band(self) -> tuple[float, float]:
        g = self.s._groove_rect()
        tick_h = float(scale.px(4, minimum=3))
        return (g.top() - tick_h - 1.0, g.top() - 1.0)

    def test_size_hint_leaves_room_for_the_bubble(self) -> None:
        hint = self.s.sizeHint()
        self.s.resize(400, hint.height())
        rect = self.s._bubble_rect("1.00×")
        self.assertIsNotNone(
            rect, "the widget's own sizeHint can't fit its value bubble")
        hr = float(self.s._handle_r())
        handle_top = self.s._groove_rect().center().y() - hr
        self.assertLessEqual(rect.bottom(), handle_top,
                             "bubble overlaps the handle disc")
        self.assertLessEqual(rect.bottom(), self._tick_band()[0],
                             "bubble overlaps the detent ticks")
        self.assertGreaterEqual(rect.top(), 0.0)

    def test_handle_and_ticks_stay_inside_the_widget(self) -> None:
        for h in (24, 26, 34, self.s.sizeHint().height(), 80):
            with self.subTest(height=h):
                self.s.resize(400, h)
                g = self.s._groove_rect()
                hr = float(self.s._handle_r())
                self.assertLessEqual(g.center().y() + hr, float(h) + 0.01)
                self.assertGreaterEqual(self._tick_band()[0], -0.01)

    def test_short_host_drops_the_bubble_instead_of_overlapping(self) -> None:
        # The transport's SpringVolume seat is 26px — there is no honest
        # place for a bubble there.
        self.s.resize(400, scale.px(26))
        self.assertIsNone(self.s._bubble_rect("100%"))
        self._off()
        self._press(self.s._x_for_value(1.25))
        self.s.grab()       # the bubble branch must still be paint-safe
        self._release(self.s._x_for_value(1.25))

    def test_bubble_stays_within_the_widget_at_the_rails(self) -> None:
        self.s.resize(400, self.s.sizeHint().height())
        for value in (0.5, 2.0):
            with self.subTest(value=value):
                self.s.set_value(value)
                rect = self.s._bubble_rect("0.50×")
                self.assertGreaterEqual(rect.left(), 0.0)
                self.assertLessEqual(rect.right(), float(self.s.width()))


class ThemeTickTests(_SliderCase):
    """theme_changed is a ~10 Hz bus, not a frame clock or a relayout
    trigger (ground rule 7)."""

    def test_theme_tick_is_a_repaint_not_a_relayout(self) -> None:
        with mock.patch.object(SpringSlider, "updateGeometry") as geom:
            for _ in range(5):
                self.s._on_theme(None)
        self.assertEqual(geom.call_count, 0,
                         "a theme tick invalidated the parent layout")

    def test_scale_flip_still_re_derives_geometry(self) -> None:
        with mock.patch.object(SpringSlider, "updateGeometry") as geom:
            scale.set_factor("large")
            self.s._on_theme(None)
            self.assertEqual(geom.call_count, 1)
            self.s._on_theme(None)      # settled again
            self.assertEqual(geom.call_count, 1)

    def test_host_minimum_width_survives_a_theme_tick(self) -> None:
        # The speed popover asks for a 220px groove; a theme tick used to
        # stomp it back to the 80px floor.
        self.s.setMinimumWidth(scale.px(220))
        self.s._on_theme(None)
        self.assertEqual(self.s.minimumWidth(), scale.px(220))


class TeardownTests(_SliderCase):
    def test_destroy_mid_settle_no_crash(self) -> None:
        self._lite()
        self._click(self.s._x_for_value(2.0))
        self.assertIsNotNone(self.s._settle)
        self.s.deleteLater()
        self.s = SpringSlider()     # tearDown still has something to reap
        QTest.qWait(60)             # anim would have fired mid-drain

    def test_destroy_mid_drag_no_crash(self) -> None:
        self._lite()
        self._press(self.s._x_for_value(1.5))
        self.s.deleteLater()
        self.s = SpringSlider()
        QTest.qWait(60)


if __name__ == "__main__":
    unittest.main()
