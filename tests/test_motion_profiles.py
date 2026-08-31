"""Motion profiles (P3): mechanical vs springy dialects.

Pinned: the mechanical profile is byte-for-byte the pre-P3 constants
(short, decisive, no Back/Elastic/Bounce anywhere); the springy profile
carries the overshoot, only under the "spring" key; the dialect follows
the PERSONALITY (bind_preset: modern → springy, everything else →
mechanical) — brutalist at full motion never bounces; reduced motion
lands on mechanical, set_profile pins/unpins, nothing bound falls back
to following intensity; legacy DUR_*/EASE_* stay as mechanical aliases;
intensity OFF constructs NOTHING and lands end-state synchronously (the
toast used to leak raw QPropertyAnimations).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QEasingCurve, QPoint
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from tide.ui import motion


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# Curve families that are against brutalist law.
_BOUNCY_TYPES = {
    QEasingCurve.InBack, QEasingCurve.OutBack, QEasingCurve.InOutBack,
    QEasingCurve.OutInBack, QEasingCurve.InElastic, QEasingCurve.OutElastic,
    QEasingCurve.InOutElastic, QEasingCurve.OutInElastic,
    QEasingCurve.InBounce, QEasingCurve.OutBounce, QEasingCurve.InOutBounce,
    QEasingCurve.OutInBounce,
}


class _MotionState(unittest.TestCase):
    """Save/restore the motion module's globals around each test."""

    def setUp(self) -> None:
        _app()
        self._intensity = motion._user_intensity
        self._override = motion._profile_override
        self._reduced = motion._reduced_motion
        # the personality binding is process-global and sticky — an
        # earlier test's preset would otherwise decide this one's dialect
        self._bound = motion._preset_profile
        motion._preset_profile = None

    def tearDown(self) -> None:
        motion._user_intensity = self._intensity
        motion._profile_override = self._override
        motion._reduced_motion = self._reduced
        motion._preset_profile = self._bound


class ProfileValuesTest(_MotionState):
    def test_mechanical_is_exactly_the_pre_p3_values(self) -> None:
        durs = motion.PROFILES["mechanical"]["dur"]
        self.assertEqual(
            {k: durs[k] for k in ("micro", "short", "med", "long")},
            {"micro": 120, "short": 200, "med": 350, "long": 600},
        )
        eases = motion.PROFILES["mechanical"]["ease"]
        self.assertEqual(eases["linear"].type(), QEasingCurve.Linear)
        self.assertEqual(eases["out"].type(), QEasingCurve.OutQuad)
        self.assertEqual(eases["out_strong"].type(), QEasingCurve.OutCubic)
        self.assertEqual(eases["in_out"].type(), QEasingCurve.InOutQuad)

    def test_mechanical_has_no_bouncy_curves(self) -> None:
        for key, curve in motion.PROFILES["mechanical"]["ease"].items():
            self.assertNotIn(
                curve.type(), _BOUNCY_TYPES,
                f"mechanical {key!r} violates the no-bounce law",
            )

    def test_springy_is_slower_and_springs(self) -> None:
        mech = motion.PROFILES["mechanical"]["dur"]
        springy = motion.PROFILES["springy"]["dur"]
        self.assertEqual(set(springy), set(mech))
        for key in mech:
            self.assertGreaterEqual(
                springy[key], mech[key],
                f"springy {key!r} shorter than mechanical",
            )
        self.assertEqual(
            motion.PROFILES["springy"]["ease"]["spring"].type(),
            QEasingCurve.OutBack,
        )

    def test_springy_overshoot_is_confined_to_the_spring_key(self) -> None:
        for key, curve in motion.PROFILES["springy"]["ease"].items():
            if key == "spring":
                continue
            self.assertNotIn(
                curve.type(), _BOUNCY_TYPES,
                f"springy {key!r} may not overshoot — opacity safety",
            )

    def test_profiles_share_a_key_vocabulary(self) -> None:
        self.assertEqual(
            set(motion.PROFILES["mechanical"]["ease"]),
            set(motion.PROFILES["springy"]["ease"]),
        )

    def test_ease_returns_a_copy(self) -> None:
        motion.set_profile("mechanical")
        curve = motion.ease("out")
        curve.setType(QEasingCurve.OutBounce)
        self.assertEqual(
            motion.PROFILES["mechanical"]["ease"]["out"].type(),
            QEasingCurve.OutQuad,
            "mutating a returned curve contaminated the profile table",
        )

    def test_unknown_keys_raise(self) -> None:
        with self.assertRaises(KeyError):
            motion.dur("gigantic")
        with self.assertRaises(KeyError):
            motion.ease("wobble")


class PersonalityDialectTest(_MotionState):
    """The dialect is the personality's, not the intensity's."""

    def test_modern_speaks_springy_at_every_intensity(self) -> None:
        motion.set_profile(None)
        motion._reduced_motion = False
        motion.bind_preset("modern")
        for level in ("off", "lite", "full"):
            motion.set_intensity(level)
            self.assertEqual(motion.profile(), "springy",
                             f"modern lost its dialect at {level}")

    def test_brutalist_never_bounces_even_at_full(self) -> None:
        # The regression this binding exists for: motion intensity is an
        # independent setting, so brutalist + full must stay mechanical.
        motion.set_profile(None)
        motion._reduced_motion = False
        motion.bind_preset("brutalist")
        motion.set_intensity("full")
        self.assertEqual(motion.profile(), "mechanical")
        self.assertNotIn(motion.ease("spring").type(), _BOUNCY_TYPES)
        self.assertEqual(motion.dur("short"), 200)

    def test_unknown_preset_gets_mechanical(self) -> None:
        motion.set_profile(None)
        motion._reduced_motion = False
        motion.set_intensity("full")
        motion.bind_preset("third-party-thing")
        self.assertEqual(motion.profile(), "mechanical")

    def test_unbinding_falls_back_to_intensity(self) -> None:
        motion.set_profile(None)
        motion._reduced_motion = False
        motion.set_intensity("full")
        motion.bind_preset("brutalist")
        self.assertEqual(motion.profile(), "mechanical")
        motion.bind_preset(None)
        self.assertIsNone(motion.bound_profile())
        self.assertEqual(motion.profile(), "springy")

    def test_reduced_motion_outranks_the_personality(self) -> None:
        motion.set_profile(None)
        motion.bind_preset("modern")
        motion.set_intensity("full")
        motion._reduced_motion = True
        self.assertEqual(motion.profile(), "mechanical")

    def test_explicit_pin_outranks_the_personality(self) -> None:
        motion._reduced_motion = False
        motion.bind_preset("brutalist")
        motion.set_profile("springy")
        self.assertEqual(motion.profile(), "springy")
        motion.set_profile(None)
        self.assertEqual(motion.profile(), "mechanical")

    def test_preset_profiles_table_is_the_whole_vocabulary(self) -> None:
        # Every dialect a personality can name must exist in PROFILES —
        # a typo here would raise deep inside dur()/ease() at paint time.
        for preset_id, name in motion.PRESET_PROFILES.items():
            self.assertIn(name, motion.PROFILES, f"{preset_id} → {name!r}")


class ProfileBindingTest(_MotionState):
    def test_full_binds_springy_lite_and_off_bind_mechanical(self) -> None:
        motion.set_profile(None)
        motion._reduced_motion = False
        motion.set_intensity("full")
        self.assertEqual(motion.profile(), "springy")
        motion.set_intensity("lite")
        self.assertEqual(motion.profile(), "mechanical")
        motion.set_intensity("off")
        self.assertEqual(motion.profile(), "mechanical")

    def test_reduced_motion_clamp_lands_on_mechanical(self) -> None:
        motion.set_profile(None)
        motion.set_intensity("full")
        motion._reduced_motion = True
        self.assertEqual(motion.profile(), "mechanical")

    def test_set_profile_pins_and_unpins(self) -> None:
        motion._reduced_motion = False
        motion.set_intensity("lite")
        motion.set_profile("springy")
        self.assertEqual(motion.profile(), "springy")
        self.assertEqual(motion.dur("short"),
                         motion.PROFILES["springy"]["dur"]["short"])
        motion.set_profile(None)
        self.assertEqual(motion.profile(), "mechanical")
        self.assertEqual(motion.dur("short"), 200)

    def test_set_profile_rejects_typos(self) -> None:
        with self.assertRaises(ValueError):
            motion.set_profile("bouncy")

    def test_dur_and_ease_follow_the_binding(self) -> None:
        motion.set_profile(None)
        motion._reduced_motion = False
        motion.set_intensity("full")
        self.assertEqual(motion.dur("med"),
                         motion.PROFILES["springy"]["dur"]["med"])
        self.assertEqual(motion.ease("spring").type(), QEasingCurve.OutBack)
        motion.set_intensity("lite")
        self.assertEqual(motion.dur("med"), 350)
        self.assertNotIn(motion.ease("spring").type(), _BOUNCY_TYPES)


class LegacyAliasTest(_MotionState):
    def test_dur_constants_are_mechanical_aliases(self) -> None:
        self.assertEqual(motion.DUR_MICRO, 120)
        self.assertEqual(motion.DUR_SHORT, 200)
        self.assertEqual(motion.DUR_MED, 350)
        self.assertEqual(motion.DUR_LONG, 600)
        durs = motion.PROFILES["mechanical"]["dur"]
        self.assertEqual(motion.DUR_MICRO, durs["micro"])
        self.assertEqual(motion.DUR_SHORT, durs["short"])
        self.assertEqual(motion.DUR_MED, durs["med"])
        self.assertEqual(motion.DUR_LONG, durs["long"])

    def test_ease_constants_are_mechanical_aliases(self) -> None:
        self.assertEqual(motion.EASE_LINEAR.type(), QEasingCurve.Linear)
        self.assertEqual(motion.EASE_OUT_QUAD.type(), QEasingCurve.OutQuad)
        self.assertEqual(motion.EASE_OUT_CUBIC.type(), QEasingCurve.OutCubic)
        self.assertEqual(motion.EASE_IN_OUT_QUAD.type(), QEasingCurve.InOutQuad)

    def test_dur_matches_constants_under_mechanical(self) -> None:
        motion.set_profile("mechanical")
        self.assertEqual(motion.dur("micro"), motion.DUR_MICRO)
        self.assertEqual(motion.dur("short"), motion.DUR_SHORT)
        self.assertEqual(motion.dur("med"), motion.DUR_MED)
        self.assertEqual(motion.dur("long"), motion.DUR_LONG)


class _AnimSpies:
    """Patch the animation classes inside motion.py so a test can prove
    that NOTHING was constructed. Helpers resolve these names from the
    module namespace, so every internal construction goes through here."""

    def __init__(self) -> None:
        self.count = 0

    def install(self, case: unittest.TestCase) -> None:
        rec = self

        def spy(real_cls):
            class Spy(real_cls):
                def __init__(self, *a, **k):
                    rec.count += 1
                    super().__init__(*a, **k)
            return Spy

        for name in ("QPropertyAnimation", "QVariantAnimation", "_ScrambleAnim"):
            patcher = mock.patch.object(motion, name, spy(getattr(motion, name)))
            patcher.start()
            case.addCleanup(patcher.stop)


class OffIsSynchronousTest(_MotionState):
    """Intensity OFF: zero animation objects, end state lands now."""

    def setUp(self) -> None:
        super().setUp()
        motion.set_profile(None)
        motion.set_intensity("off")
        self.spies = _AnimSpies()
        self.spies.install(self)
        self.host = QWidget()
        self.host.resize(400, 300)
        self.host.show()

    def tearDown(self) -> None:
        self.host.close()
        self.host.deleteLater()
        QTest.qWait(10)
        super().tearDown()

    def _assert_sync(self, handle, done: list) -> None:
        self.assertIsNone(handle)
        self.assertEqual(done, ["done"])
        self.assertEqual(self.spies.count, 0, "an animation was constructed at OFF")

    def test_fade_in(self) -> None:
        done: list = []
        w = QWidget(self.host)
        h = motion.fade_in(w, on_done=lambda: done.append("done"))
        self._assert_sync(h, done)
        self.assertFalse(w.isHidden())
        self.assertIsNone(w.graphicsEffect())

    def test_fade_out(self) -> None:
        done: list = []
        w = QWidget(self.host)
        w.show()
        h = motion.fade_out(w, on_done=lambda: done.append("done"))
        self._assert_sync(h, done)
        self.assertTrue(w.isHidden())

    def test_slide(self) -> None:
        done: list = []
        w = QWidget(self.host)
        h = motion.slide(w, QPoint(0, 0), QPoint(30, 40),
                         on_done=lambda: done.append("done"))
        self._assert_sync(h, done)
        self.assertEqual(w.pos(), QPoint(30, 40))

    def test_color_lerp(self) -> None:
        done: list = []
        seen: list = []
        h = motion.color_lerp(
            QColor("#000000"), QColor("#ffffff"),
            on_update=seen.append, owner=self.host,
            on_done=lambda: done.append("done"),
        )
        self._assert_sync(h, done)
        self.assertEqual(seen, [QColor("#ffffff")])

    def test_value_lerp(self) -> None:
        done: list = []
        seen: list = []
        h = motion.value_lerp(
            0.0, 88.0, on_update=seen.append, owner=self.host,
            on_done=lambda: done.append("done"),
        )
        self._assert_sync(h, done)
        self.assertEqual(seen, [88.0])

    def test_spring_settle(self) -> None:
        done: list = []
        seen: list = []
        h = motion.spring_settle(
            0.25, 1.0, on_update=seen.append, owner=self.host,
            on_done=lambda: done.append("done"),
        )
        self._assert_sync(h, done)
        self.assertEqual(seen, [1.0])

    def test_nudge(self) -> None:
        done: list = []
        w = QWidget(self.host)
        w.move(5, 5)
        h = motion.nudge(w, on_done=lambda: done.append("done"))
        self._assert_sync(h, done)
        self.assertEqual(w.pos(), QPoint(5, 5))

    def test_crossfade_pixmap(self) -> None:
        done: list = []
        frames: list = []
        old = QPixmap(10, 10)
        new = QPixmap(10, 10)
        h = motion.crossfade_pixmap(
            frames.append, old, new, owner=self.host,
            on_done=lambda: done.append("done"),
        )
        self._assert_sync(h, done)
        self.assertEqual(len(frames), 1)
        self.assertIs(frames[0], new)

    def test_crossfade_stack(self) -> None:
        done: list = []
        stack = QStackedWidget(self.host)
        stack.addWidget(QWidget())
        stack.addWidget(QWidget())
        h = motion.crossfade_stack(stack, 1,
                                   on_done=lambda: done.append("done"))
        self._assert_sync(h, done)
        self.assertEqual(stack.currentIndex(), 1)

    def test_scramble_text(self) -> None:
        done: list = []
        seen: list = []
        h = motion.scramble_text(seen.append, "hello",
                                 owner=self.host,
                                 on_done=lambda: done.append("done"))
        self._assert_sync(h, done)
        self.assertEqual(seen, ["hello"])

    def test_toast_constructs_zero_animations(self) -> None:
        from tide.ui.toast import Toast
        # lifetime_ms=0: no auto-dismiss QTimer left aimed at a widget the
        # test destroys (its late fire would hit a dead wrapper).
        t = Toast(self.host, "off means off", lifetime_ms=0)
        try:
            self.assertEqual(self.spies.count, 0,
                             "toast leaked an animation at intensity off")
            self.assertEqual(t.pos(), t._target_position())
            self.assertFalse(t.isHidden())
            self.assertIsNone(t.graphicsEffect())
        finally:
            t.dismiss()
            QTest.qWait(10)
        self.assertEqual(self.spies.count, 0)


class SpringyHelpersTest(_MotionState):
    """LITE stays mechanical; FULL gets the spring — by construction."""

    def setUp(self) -> None:
        super().setUp()
        motion.set_profile(None)
        motion._reduced_motion = False
        self.host = QWidget()
        self.host.resize(400, 300)
        self.host.show()
        self._anims: list = []

    def tearDown(self) -> None:
        for anim in self._anims:
            if anim is not None:
                anim.stop()
        self.host.close()
        self.host.deleteLater()
        QTest.qWait(10)
        super().tearDown()

    def _track(self, anim):
        self._anims.append(anim)
        return anim

    def test_spring_settle_is_mechanical_at_lite(self) -> None:
        motion.set_intensity("lite")
        anim = self._track(motion.spring_settle(
            0.0, 1.0, on_update=lambda v: None, owner=self.host))
        self.assertIsNotNone(anim)
        self.assertNotIn(anim.easingCurve().type(), _BOUNCY_TYPES)
        self.assertEqual(anim.duration(), 200)

    def test_spring_settle_overshoots_at_full(self) -> None:
        motion.set_intensity("full")
        anim = self._track(motion.spring_settle(
            0.0, 1.0, on_update=lambda v: None, owner=self.host))
        self.assertIsNotNone(anim)
        self.assertEqual(anim.easingCurve().type(), QEasingCurve.OutBack)
        self.assertEqual(anim.duration(),
                         motion.PROFILES["springy"]["dur"]["short"])

    def test_nudge_is_a_noop_below_full(self) -> None:
        for level in ("off", "lite"):
            with self.subTest(level=level):
                motion.set_intensity(level)
                done: list = []
                w = QWidget(self.host)
                w.move(7, 9)
                anim = motion.nudge(w, on_done=lambda: done.append("done"))
                self.assertIsNone(anim)
                self.assertEqual(done, ["done"])
                self.assertEqual(w.pos(), QPoint(7, 9))

    def test_nudge_runs_and_returns_home_at_full(self) -> None:
        motion.set_intensity("full")
        w = QWidget(self.host)
        w.move(7, 9)
        anim = self._track(motion.nudge(w, dy=4))
        self.assertIsNotNone(anim)
        anim.setCurrentTime(anim.duration())   # scrub to the end, no waiting
        QTest.qWait(10)
        self.assertEqual(w.pos(), QPoint(7, 9))

    def _fresh_stack(self) -> QStackedWidget:
        stack = QStackedWidget(self.host)
        stack.resize(200, 150)
        stack.addWidget(QWidget())
        stack.addWidget(QWidget())
        stack.show()
        return stack

    def _overlay_of(self, stack: QStackedWidget):
        # The overlay is the only QLabel child of the target page.
        from PySide6.QtWidgets import QLabel
        labels = stack.widget(1).findChildren(QLabel)
        return labels[0] if labels else None

    def test_crossfade_stack_overshoot_only_under_springy(self) -> None:
        motion.set_intensity("lite")
        stack = self._fresh_stack()
        anim = self._track(motion.crossfade_stack(stack, 1, overshoot=True))
        self.assertIsNotNone(anim)
        overlay = self._overlay_of(stack)
        self.assertIsNotNone(overlay)
        self.assertIsNone(getattr(overlay, "_motion_overshoot_anim", None),
                          "mechanical profile must not get the lift flourish")

        motion.set_intensity("full")
        stack2 = self._fresh_stack()
        anim2 = self._track(motion.crossfade_stack(stack2, 1, overshoot=True))
        self.assertIsNotNone(anim2)
        overlay2 = self._overlay_of(stack2)
        self.assertIsNotNone(overlay2)
        lift = getattr(overlay2, "_motion_overshoot_anim", None)
        self.assertIsNotNone(lift, "springy overshoot flourish missing")
        self._anims.append(lift)
        self.assertEqual(lift.easingCurve().type(), QEasingCurve.OutBack)


class HelpersFollowTheProfileTest(_MotionState):
    """Default durations/easings are resolved at call time, per profile."""

    def setUp(self) -> None:
        super().setUp()
        motion._reduced_motion = False
        motion.set_intensity("lite")   # any non-OFF; the pin decides feel
        self.host = QWidget()
        self.host.resize(200, 150)
        self.host.show()
        self._anims: list = []

    def tearDown(self) -> None:
        for anim in self._anims:
            if anim is not None:
                anim.stop()
        self.host.close()
        self.host.deleteLater()
        QTest.qWait(10)
        super().tearDown()

    def test_fade_defaults_swap_with_the_profile(self) -> None:
        motion.set_profile("mechanical")
        w = QWidget(self.host)
        anim = motion.fade_in(w)
        self._anims.append(anim)
        self.assertEqual(anim.duration(), 200)
        self.assertEqual(anim.easingCurve().type(), QEasingCurve.OutQuad)
        anim.stop()

        motion.set_profile("springy")
        w2 = QWidget(self.host)
        anim2 = motion.fade_in(w2)
        self._anims.append(anim2)
        self.assertEqual(anim2.duration(),
                         motion.PROFILES["springy"]["dur"]["short"])
        self.assertNotIn(anim2.easingCurve().type(), _BOUNCY_TYPES,
                         "fades must never overshoot — opacity safety")

    def test_explicit_dur_still_wins(self) -> None:
        motion.set_profile("springy")
        w = QWidget(self.host)
        anim = motion.fade_in(w, dur=123)
        self._anims.append(anim)
        self.assertEqual(anim.duration(), 123)


class ToastGatingTest(_MotionState):
    """The toast's slide/fade now go through motion (the pre-P3 code built
    raw QPropertyAnimations that ran even at intensity off)."""

    def setUp(self) -> None:
        super().setUp()
        motion.set_profile(None)
        motion._reduced_motion = False
        self.host = QWidget()
        self.host.resize(800, 600)
        self.host.show()

    def tearDown(self) -> None:
        self.host.close()
        self.host.deleteLater()
        QTest.qWait(10)
        super().tearDown()

    def test_lite_toast_registers_motion_animations(self) -> None:
        from tide.ui.toast import Toast
        motion.set_intensity("lite")
        t = Toast(self.host, "hello", lifetime_ms=0)
        try:
            table = getattr(t, "_motion_anims", {})
            self.assertIn("slide", table)
            self.assertIn("fade", table)
            self.assertEqual(table["slide"].duration(), 200)
            # mechanical "spring" is decisive — no bounce
            self.assertNotIn(table["slide"].easingCurve().type(),
                             _BOUNCY_TYPES)
        finally:
            for anim in list(getattr(t, "_motion_anims", {}).values()):
                anim.stop()
            t.deleteLater()
            QTest.qWait(10)

    def test_full_toast_slides_in_with_the_spring(self) -> None:
        from tide.ui.toast import Toast
        motion.set_intensity("full")
        t = Toast(self.host, "hello", lifetime_ms=0)
        try:
            table = getattr(t, "_motion_anims", {})
            self.assertEqual(table["slide"].easingCurve().type(),
                             QEasingCurve.OutBack)
            self.assertEqual(
                table["slide"].duration(),
                motion.PROFILES["springy"]["dur"]["short"])
            # the fade must not overshoot even at full
            self.assertNotIn(table["fade"].easingCurve().type(),
                             _BOUNCY_TYPES)
        finally:
            for anim in list(getattr(t, "_motion_anims", {}).values()):
                anim.stop()
            t.deleteLater()
            QTest.qWait(10)


class LyricsGatingTest(_MotionState):
    """The lyric line-advance glides route through motion helpers now;
    at OFF they must construct nothing (the accent is already painted by
    the synchronous restyle)."""

    def setUp(self) -> None:
        super().setUp()
        motion.set_profile(None)

    def test_line_advance_is_silent_at_off(self) -> None:
        from tide import theming
        from tide.ui.lyrics import LyricsView
        theming.manager().refresh()
        motion.set_intensity("off")
        spies = _AnimSpies()
        spies.install(self)
        view = LyricsView(mock.Mock())
        try:
            view._show_timed([(0.0, "line one"), (4.0, "line two")])
            view.update_position(0.5)   # activates line 0: restyle + glide
            QTest.qWait(10)
            self.assertEqual(spies.count, 0,
                             "lyrics animated at intensity off")
        finally:
            view.deleteLater()
            QTest.qWait(10)

    def test_line_advance_uses_motion_at_lite(self) -> None:
        from tide import theming
        from tide.ui.lyrics import LyricsView
        theming.manager().refresh()
        motion.set_intensity("lite")
        view = LyricsView(mock.Mock())
        try:
            view._show_timed([(0.0, "line one"), (4.0, "line two")])
            view.update_position(0.5)
            table = getattr(view, "_motion_anims", {})
            self.assertIn("color/lyric_active", table)
            anim = table["color/lyric_active"]
            self.assertEqual(anim.duration(), 350)
            view._stop_line_motion()
        finally:
            view.deleteLater()
            QTest.qWait(10)


if __name__ == "__main__":
    unittest.main()
