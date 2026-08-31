"""v2.0 phase 3 — the stitch seams: cross-agent wiring landed after the
builders finished their contracts.

What's pinned here:
- a personality flip re-points the UI-sound player at the incoming
  preset's pack (modern → the watery ``sounds/modern`` blips, brutalist
  → the default clicks) — derived from the builtin def, never a
  Settings/STASH field;
- modern's builtin theme now seeds the SpringSlider volume face
  (adaptive ``[slots] volume = "spring"``) and a flip actually builds a
  SpringVolume in the strip;
- the main-view crossfade is profile-aware: ``_set_stack_index`` passes
  ``motion.dur("short")`` and opts into the overshoot lift, which the
  DIALECT gates — modern springs, brutalist stays flat even at motion
  full — and stays a synchronous index swap at intensity OFF.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEasingCurve
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from tide import config, glyphs, presets, theming
from tide import layout as layout_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui import motion as motion_module
from tide.ui.variants import SpringVolume
from tide.ui_sounds import SOUND_KEYS, UiSoundPlayer


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _brutalist_settings() -> Settings:
    s = Settings()
    s.first_launch_complete = True
    s.preset = "brutalist"
    s.theme = "gruvbox"
    s.layout = "classic"
    s.layout_overrides = {"progress": "dotted"}
    s.motion = "off"
    s.corner_style = "sharp"
    s.nav_icon_set = "off"
    s.adaptive_accent = False
    s.adaptive_background = False
    presets.stash(s)
    return s


class _WindowCase(unittest.TestCase):
    """test_preset_flip's hygiene: hermetic settings file, app-wide
    setStyleSheet suppression, deleteLater+drain teardown, and global
    motion/glyph/theme state restored after every test."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-phase3-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
        # Suppress the REAL app-wide QSS pushes for the whole case —
        # closed-but-alive windows from earlier files repolish on every
        # push (see test_preset_flip for the war story).
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        # Flips move global motion intensity — restore the suite default.
        self._orig_intensity = motion_module._user_intensity
        self.w = None

    def tearDown(self) -> None:
        if self.w is not None:
            try:
                theming.manager().theme_changed.disconnect(
                    self.w._on_theme_changed)
            except (RuntimeError, TypeError):
                pass
            self.w.close()
            self.w.deleteLater()
            self.w = None
        QTest.qWait(30)
        config.SETTINGS_FILE = self._orig_settings_file
        motion_module.set_intensity(self._orig_intensity)
        glyphs.set_overrides({})
        mgr = theming.manager()
        mgr.set_user_override("radius", None)
        theming.set_case_override("")
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0)
        layout_module.manager().apply("classic", {})
        QTest.qWait(30)
        for _ in range(10):
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    def _make_window(self, settings: Settings):
        theming.manager().apply(settings.theme)
        layout_module.manager().apply(settings.layout,
                                      dict(settings.layout_overrides))
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.w._settings = settings
        return self.w


class SoundPackFlipTests(_WindowCase):
    """apply_preset_visuals → _apply_sound_pack: the pack follows the
    personality (D's phase-3 seam, wired by stitch)."""

    def test_flip_to_modern_wears_the_modern_pack(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w.ui_sounds = UiSoundPlayer(parent=w)
        self.assertEqual(w.ui_sounds.pack, "default")
        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertEqual(w.ui_sounds.pack, "modern")
        # The pack actually landed on files, not just the name: every
        # key resolves inside sounds/modern/ (the pack ships all six).
        for key in SOUND_KEYS:
            path = w.ui_sounds._sounds.get(key)
            self.assertIsNotNone(path, f"{key} lost its file on set_pack")
            self.assertEqual(path.parent.name, "modern",
                             f"{key} did not resolve into the modern pack")

    def test_flip_back_restores_the_default_pack(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w.ui_sounds = UiSoundPlayer(parent=w)
        w.switch_preset("modern")
        QTest.qWait(30)
        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(w.ui_sounds.pack, "default")
        for key in SOUND_KEYS:
            path = w.ui_sounds._sounds.get(key)
            self.assertIsNotNone(path)
            self.assertEqual(path.parent.name, "sounds")


class SoundPackResolutionTests(unittest.TestCase):
    """_apply_sound_pack's derivation rules, on a stub — no window.

    The method only touches ``self.ui_sounds`` / ``self._settings`` so
    the unbound function runs against any object; that keeps these
    cases window-free (and restyle-free)."""

    class _Stub:
        pass

    def _run(self, stub) -> None:
        from tide.ui.window import MainWindow
        MainWindow._apply_sound_pack(stub)

    def test_no_player_is_a_noop(self) -> None:
        stub = self._Stub()
        stub._settings = _brutalist_settings()
        self._run(stub)   # must not raise

    def test_unknown_and_empty_preset_ids_wear_default(self) -> None:
        _app()
        for preset_id in ("", "some-third-party-personality"):
            with self.subTest(preset=preset_id):
                stub = self._Stub()
                stub.ui_sounds = UiSoundPlayer()
                stub.ui_sounds.set_pack("modern")   # poison the state
                stub._settings = Settings()
                stub._settings.preset = preset_id
                self._run(stub)
                self.assertEqual(stub.ui_sounds.pack, "default")

    def test_no_settings_wears_default(self) -> None:
        _app()
        stub = self._Stub()
        stub.ui_sounds = UiSoundPlayer()
        stub.ui_sounds.set_pack("modern")
        stub._settings = None
        self._run(stub)
        self.assertEqual(stub.ui_sounds.pack, "default")

    def test_builtin_defs_carry_the_expected_packs(self) -> None:
        # The derivation source itself: modern declares its pack, the
        # brutalist def keeps the default (D's one-line presets change).
        self.assertEqual(presets.builtin("modern").sound_pack, "modern")
        self.assertEqual(presets.builtin("brutalist").sound_pack, "default")

    def test_unknown_pack_name_falls_back_per_key(self) -> None:
        _app()
        p = UiSoundPlayer()
        p.set_pack("no-such-pack")
        self.assertEqual(p.pack, "no-such-pack")
        for key in SOUND_KEYS:
            path = p._sounds.get(key)
            self.assertIsNotNone(path, f"{key} vanished under unknown pack")
            self.assertEqual(path.parent.name, "sounds",
                             f"{key} must fall back to the default pack")


class SpringVolumeSeedTests(_WindowCase):
    """adaptive's [slots] now seeds volume = "spring": a first visit to
    modern wears the SpringSlider volume face end-to-end."""

    def test_first_visit_to_modern_builds_spring_volume(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        self.assertNotIsInstance(w.volume, SpringVolume)
        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertEqual(s.layout_overrides.get("volume"), "spring")
        self.assertEqual(w._slot_volume, "spring")
        self.assertIsInstance(w.volume, SpringVolume)
        # The swapped face keeps the shared volume surface wired.
        w.volume.setVolume(37, emit=False)
        self.assertEqual(w.volume.volume(), 37)

    def test_roundtrip_restores_brutalist_face(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w.switch_preset("modern")
        QTest.qWait(30)
        w.switch_preset("brutalist")
        QTest.qWait(30)
        # classic layout default (no brutalist volume override stashed).
        self.assertEqual(w._slot_volume, "blocks")
        self.assertNotIsInstance(w.volume, SpringVolume)


class CrossfadeSeamTests(_WindowCase):
    """window._set_stack_index rides the profile-aware motion API now
    (B's deferred one-liner): dur('short') + the springy-only overshoot
    opt-in, still a synchronous swap at OFF."""

    def _unclamp_motion(self) -> None:
        # Offscreen platforms can report reduced motion, which forces
        # mechanical — pin it off so the springy branch is actually
        # exercised (test_motion_profiles' pattern). Both globals are
        # restored: the dialect binding is process-wide and sticky.
        reduced = motion_module._reduced_motion
        bound = motion_module._preset_profile
        self.addCleanup(
            lambda: setattr(motion_module, "_preset_profile", bound))
        self.addCleanup(
            lambda: setattr(motion_module, "_reduced_motion", reduced))
        motion_module._reduced_motion = False

    def test_stack_switch_passes_profile_aware_dur(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        self._unclamp_motion()
        w.switch_preset("modern")
        QTest.qWait(30)
        motion_module.set_intensity("full")
        try:
            with mock.patch.object(
                    motion_module, "crossfade_stack") as spy:
                w._set_stack_index(1)
            self.assertEqual(spy.call_count, 1)
            _, kwargs = spy.call_args
            self.assertEqual(kwargs.get("dur"), motion_module.dur("short"),
                             "crossfade must ask the profile, not DUR_SHORT")
            self.assertTrue(kwargs.get("overshoot"),
                            "modern's view switch opts into the lift")
            # And under the modern personality that dur really is the
            # springy dialect's.
            self.assertEqual(motion_module.profile(), "springy")
        finally:
            motion_module.set_intensity(self._orig_intensity)

    def test_brutalist_at_full_never_gets_the_springy_lift(self) -> None:
        # The overshoot opt-in is unconditional at the call site; the
        # dialect is what gates it. A brutalist user who turns motion up
        # to full must get a plain crossfade — no snapshot lift, no
        # OutBack anywhere.
        s = _brutalist_settings()
        w = self._make_window(s)
        self._unclamp_motion()
        w.apply_motion_setting()        # binds the dialect from settings
        motion_module.set_intensity("full")
        try:
            self.assertEqual(motion_module.profile(), "mechanical")
            self.assertNotEqual(
                motion_module.ease("spring").type(),
                QEasingCurve.OutBack,
                "brutalist + motion=full leaked the modern dialect",
            )
            target = 1 if w.stack.currentIndex() != 1 else 2
            motion_module.crossfade_stack(w.stack, target, overshoot=True)
            overlay = w.stack.widget(target).findChildren(QLabel)
            self.assertFalse(
                [o for o in overlay
                 if getattr(o, "_motion_overshoot_anim", None) is not None],
                "the springy lift fired under the brutalist personality",
            )
        finally:
            motion_module.set_intensity(self._orig_intensity)

    def test_stack_switch_off_is_synchronous(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        motion_module.set_intensity("off")
        target = 1 if w.stack.currentIndex() != 1 else 2
        w._set_stack_index(target)
        self.assertEqual(w.stack.currentIndex(), target,
                         "OFF must swap the index inside the call")


if __name__ == "__main__":
    unittest.main()
