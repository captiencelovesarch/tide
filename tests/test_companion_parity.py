"""Phase 3 companion parity — the mini + fullscreen consume the personality.

Pinned: ``backdrops.resolve`` is the ONE encoding of the companion
"follow" semantics (full truth table, including the deliberate
mini-vs-fullscreen divergence), and both companions route through it; a
``switch_preset`` while a companion is open re-lands backdrop style /
pulse gate / motion on it, and the incoming glyph overrides reach the
companion transports; at intensity OFF every companion move snaps
synchronously with ZERO animation objects constructed.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import backdrops, config, glyphs, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui import motion as motion_module


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _brutalist_settings() -> Settings:
    """A settings object living as the brutalist builtin (backdrops off,
    motion off), with its stash snapshotted so a flip can round-trip."""
    s = Settings()
    s.first_launch_complete = True
    presets.restore(s, "brutalist")
    presets.stash(s)
    return s


class ResolveTruthTableTest(unittest.TestCase):
    """backdrops.resolve — the follow semantics as data. Every row of the
    docstring's truth table, pinned."""

    def test_explicit_style_ignores_the_main_toggle_on_both_surfaces(self) -> None:
        for surface in backdrops.SURFACES:
            for adaptive_on in (True, False):
                with self.subTest(surface=surface, adaptive_on=adaptive_on):
                    self.assertEqual(
                        backdrops.resolve("depths", adaptive_on=adaptive_on,
                                          surface=surface, main_style="field"),
                        "depths")

    def test_off_is_off_everywhere(self) -> None:
        for surface in backdrops.SURFACES:
            for adaptive_on in (True, False):
                with self.subTest(surface=surface, adaptive_on=adaptive_on):
                    self.assertEqual(
                        backdrops.resolve(backdrops.OFF,
                                          adaptive_on=adaptive_on,
                                          surface=surface,
                                          main_style="aurora"),
                        "off")

    def test_follow_mirrors_the_main_style_when_adaptive_is_on(self) -> None:
        for surface in backdrops.SURFACES:
            with self.subTest(surface=surface):
                self.assertEqual(
                    backdrops.resolve(backdrops.FOLLOW, adaptive_on=True,
                                      surface=surface, main_style="aurora"),
                    "aurora")

    def test_adaptive_off_divergence_is_explicit_data(self) -> None:
        # fullscreen mirrors the main surface's whole look, master toggle
        # included; the mini keeps its gradient because the mini IS its
        # backdrop — this split is the reason resolve exists
        self.assertEqual(
            backdrops.resolve(backdrops.FOLLOW, adaptive_on=False,
                              surface="fullscreen", main_style="aurora"),
            "off")
        self.assertEqual(
            backdrops.resolve(backdrops.FOLLOW, adaptive_on=False,
                              surface="mini", main_style="aurora"),
            "aurora")

    def test_empty_pick_means_follow(self) -> None:
        self.assertEqual(
            backdrops.resolve("", adaptive_on=True, surface="mini",
                              main_style="smoke"),
            "smoke")
        self.assertEqual(
            backdrops.resolve("", adaptive_on=False, surface="fullscreen",
                              main_style="smoke"),
            "off")

    def test_follow_defaults_to_field_when_the_main_style_is_unset(self) -> None:
        for surface in backdrops.SURFACES:
            with self.subTest(surface=surface):
                self.assertEqual(
                    backdrops.resolve(backdrops.FOLLOW, adaptive_on=True,
                                      surface=surface),
                    "field")

    def test_unknown_style_passes_through_verbatim(self) -> None:
        self.assertEqual(
            backdrops.resolve("wibble", adaptive_on=False, surface="mini"),
            "wibble")

    def test_unknown_surface_raises(self) -> None:
        with self.assertRaises(ValueError):
            backdrops.resolve("follow", adaptive_on=True, surface="main")


class _FlipCase(unittest.TestCase):
    """Window-building preset-flip case — test_preset_flip._FlipCase's
    hygiene plus companion reaping and glyph/motion restore."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-companion-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
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
            companions = list(self.w._companions())
            self.w.close()
            # the companions are parentless top-levels — the main
            # window's deleteLater won't reap them, and a leaked mini
            # keeps ~100 widgets listening to every later restyle
            for c in companions:
                c.close()
                c.deleteLater()
            self.w.deleteLater()
            self.w = None
        QTest.qWait(30)
        glyphs.set_overrides({})
        motion_module.set_intensity("lite")
        config.SETTINGS_FILE = self._orig_settings_file
        mgr = theming.manager()
        mgr.set_user_override("radius", None)
        theming.set_case_override("")
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0)
        layout_module.manager().apply("classic", {})
        QTest.qWait(30)   # drain the queued restyles into THIS test
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


class _AmbientSpy:
    """Stands in for AmbientController so tests never spawn parec."""

    def __init__(self) -> None:
        self.targets: list = []
        self.mini_active = False
        self.pulse_enabled = False

    def add_target(self, t) -> None:
        if t not in self.targets:
            self.targets.append(t)

    def remove_target(self, t) -> None:
        if t in self.targets:
            self.targets.remove(t)

    def set_mini_active(self, on) -> None:
        self.mini_active = bool(on)

    def set_pulse_enabled(self, on) -> None:
        self.pulse_enabled = bool(on)


class MiniFlipParityTest(_FlipCase):
    """switch_preset while the mini is open — the phase-1 apply_settings
    path must re-land the incoming personality on it, live."""

    def test_flip_relands_backdrop_pulse_and_motion_on_the_mini(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w._ambient = amb = _AmbientSpy()
        w.set_mini_mode(True)
        mini = w._mini
        QTest.qWait(30)
        # brutalist: flat card, no pulse consumer
        self.assertEqual(mini.resolved_backdrop_style(), "off")
        self.assertFalse(mini.central_bg._enabled)
        self.assertFalse(amb.mini_active)

        w.switch_preset("modern")
        QTest.qWait(30)
        # modern: follow → the main window's liquid, breathing at full
        self.assertEqual(mini.resolved_backdrop_style(), "liquid")
        self.assertTrue(mini.central_bg._enabled)
        self.assertEqual(mini.central_bg._style, "liquid")
        self.assertEqual(mini.central_bg._motion, "full")
        self.assertTrue(amb.mini_active, "modern must hold the pulse gate")
        self.assertIn(mini, amb.targets)

        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(mini.resolved_backdrop_style(), "off")
        self.assertFalse(mini.central_bg._enabled)
        self.assertFalse(amb.mini_active)

    def test_flip_glyph_overrides_reach_the_mini_transport(self) -> None:
        s = _brutalist_settings()
        s.preset_state["modern"] = {
            "glyph_overrides": {"prev": "«", "next": "»", "shuffle": "⤨"},
        }
        w = self._make_window(s)
        w.set_mini_mode(True)
        mini = w._mini
        self.assertEqual(mini.prev_btn._glyph, "◂◂")

        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertEqual(mini.prev_btn._glyph, "«")
        self.assertEqual(mini.next_btn._glyph, "»")
        self.assertEqual(mini.shuffle_btn._glyph, "⤨")
        self.assertEqual(mini.shuffle_btn._label, "⤨",
                         "glyph-only buttons carry the glyph as label too")

        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(mini.prev_btn._glyph, "◂◂",
                         "modern's glyphs must not leak into brutalist")
        self.assertEqual(mini.shuffle_btn._glyph, "⇋")


class FullscreenFlipParityTest(_FlipCase):
    def test_flip_relands_backdrop_and_motion_on_the_fullscreen(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w._ambient = amb = _AmbientSpy()
        w.set_fullscreen_mode(True)
        fs = w._fs
        QTest.qWait(30)
        self.assertEqual(fs.resolved_backdrop_style(), "off")
        self.assertFalse(fs.central_bg._enabled)
        self.assertFalse(amb.mini_active)

        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertEqual(fs.resolved_backdrop_style(), "liquid")
        self.assertTrue(fs.central_bg._enabled)
        self.assertEqual(fs.central_bg._style, "liquid")
        self.assertEqual(fs.central_bg._motion, "full")
        self.assertTrue(amb.mini_active)

        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(fs.resolved_backdrop_style(), "off")
        self.assertFalse(fs.central_bg._enabled)
        self.assertFalse(amb.mini_active)

    def test_flip_glyph_overrides_reach_the_fullscreen_transport(self) -> None:
        s = _brutalist_settings()
        s.preset_state["modern"] = {
            "glyph_overrides": {"prev": "«", "shuffle": "⤨"},
        }
        w = self._make_window(s)
        w.set_fullscreen_mode(True)
        fs = w._fs
        self.assertEqual(fs.prev_btn._glyph, "◂◂")

        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertEqual(fs.prev_btn._glyph, "«")
        self.assertEqual(fs.shuffle_btn._glyph, "⤨")

        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(fs.prev_btn._glyph, "◂◂")


class _CompanionCase(unittest.TestCase):
    """Lighter window case for the non-flip tests: no theme applies, so
    no restyle suppression needed — but the same deleteLater teardown."""

    def setUp(self) -> None:
        _app()
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        self._prior_intensity = motion_module.intensity()
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.w._settings = Settings()

    def tearDown(self) -> None:
        companions = list(self.w._companions())
        self.w.close()
        for c in companions:
            c.close()
            c.deleteLater()
        self.w.deleteLater()
        QTest.qWait(30)
        motion_module.set_intensity(self._prior_intensity)
        settings_module.save = self._real_save

    def _pump(self, n: int = 4) -> None:
        QTest.qWait(30)
        app = _app()
        for _ in range(n):
            app.processEvents()


class CompanionResolveRoutingTest(_CompanionCase):
    """Both companions must actually route through backdrops.resolve —
    the whole point is that the follow semantics live in ONE place."""

    def test_companions_route_through_backdrops_resolve(self) -> None:
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self.w.set_fullscreen_mode(True)   # closes the mini, builds fs
        fs = self.w._fs
        with mock.patch.object(backdrops, "resolve",
                               return_value="depths") as spy:
            self.assertEqual(mini.resolved_backdrop_style(), "depths")
            self.assertEqual(fs.resolved_backdrop_style(), "depths")
        surfaces = sorted(c.kwargs.get("surface")
                          for c in spy.call_args_list)
        self.assertEqual(surfaces, ["fullscreen", "mini"])


class CompanionMotionOffTest(_CompanionCase):
    """Brutalist zero-animation is a product contract: at intensity OFF
    the companions snap synchronously and construct NO animation objects
    (spied at motion's own Qt constructors — every companion move now
    routes through motion.value_lerp)."""

    def test_mini_moves_snap_with_zero_animation_objects(self) -> None:
        motion_module.set_intensity("off")
        self.w.set_mini_mode(True)
        mini = self.w._mini
        self._pump()
        with mock.patch.object(motion_module, "QVariantAnimation") as vspy, \
                mock.patch.object(motion_module, "QPropertyAnimation") as pspy:
            # Zen sleep: fade + collapse both snap.
            mini._zen_sleep(force=True)
            self.assertTrue(mini._zen_asleep)
            self.assertEqual(mini._zen_eff.opacity(), 0.0)
            self.assertEqual(mini._fade_group.maximumHeight(), 0)
            self.assertIsNone(mini._zen_anim)
            self.assertIsNone(mini._zen_h_anim)
            # Wake (non-snap arg — OFF must still land synchronously).
            mini._zen_wake()
            self.assertEqual(mini._zen_eff.opacity(), 1.0)
            self.assertFalse(mini._group_constrained())
            mini._on_ticker_line(
                "a long enough synced line that must wrap to more rows "
                "than one and therefore grow the reserved ticker height")
            self.assertIsNone(mini._ticker_h_anim)
            mini._toggle_lyrics(True)
            self.assertEqual(mini.lyrics_panel.height(),
                             mini.lyrics_panel.maximumHeight())
            mini._toggle_lyrics(True)
            self.assertFalse(mini._lyrics_open)
            self.assertEqual(vspy.call_count, 0,
                             "intensity OFF constructed a QVariantAnimation")
            self.assertEqual(pspy.call_count, 0,
                             "intensity OFF constructed a QPropertyAnimation")

    def test_fullscreen_moves_snap_with_zero_animation_objects(self) -> None:
        motion_module.set_intensity("off")
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        self._pump()
        with mock.patch.object(motion_module, "QVariantAnimation") as vspy, \
                mock.patch.object(motion_module, "QPropertyAnimation") as pspy:
            fs._zen_sleep()
            self.assertEqual(fs._top_eff.opacity(), 0.0)
            self.assertEqual(fs._bottom_eff.opacity(), 0.0)
            self.assertIsNone(fs._zen_anim)
            fs._zen_wake()
            self.assertEqual(fs._top_eff.opacity(), 1.0)
            self.assertEqual(fs._bottom_eff.opacity(), 1.0)
            fs._apply_pane("off")
            self.assertIsNone(fs._pane_anim)
            self.assertFalse(fs._lyrics_host.isVisibleTo(fs))
            fs._apply_pane("lyrics")
            self.assertIsNone(fs._pane_anim)
            self.assertEqual(vspy.call_count, 0,
                             "intensity OFF constructed a QVariantAnimation")
            self.assertEqual(pspy.call_count, 0,
                             "intensity OFF constructed a QPropertyAnimation")


class CompanionMotionRegistryTest(_CompanionCase):
    """With motion on, the migrated companion moves register in motion's
    per-owner table so a re-trigger coalesces instead of two animations
    fighting over the same property."""

    def test_zen_fade_registers_and_coalesces(self) -> None:
        motion_module.set_intensity("lite")
        self.w.set_mini_mode(True)
        mini = self.w._mini
        try:
            mini._animate_zen(0.0)
            first = mini._zen_anim
            self.assertIsNotNone(first)
            self.assertIs(mini._motion_anims.get("zen/opacity"), first)
            mini._animate_zen(1.0)
            second = mini._zen_anim
            self.assertIsNotNone(second)
            self.assertIsNot(second, first)
            self.assertIs(mini._motion_anims.get("zen/opacity"), second)
            from PySide6.QtCore import QAbstractAnimation
            self.assertEqual(first.state(), QAbstractAnimation.Stopped,
                             "the superseded fade must be cancelled")
        finally:
            # Never let a mid-flight tick outlive this window into the
            # next test's event loop.
            anim = mini._zen_anim
            if anim is not None:
                anim.stop()
            mini._zen_anim = None

    def test_pane_glide_registers_under_the_owner(self) -> None:
        motion_module.set_intensity("lite")
        self.w.set_fullscreen_mode(True)
        fs = self.w._fs
        try:
            fs._apply_pane("off")
            self.assertIsNotNone(fs._pane_anim)
            self.assertIs(fs._motion_anims.get("pane"), fs._pane_anim)
        finally:
            if fs._pane_anim is not None:
                fs._pane_anim.stop()
                fs._pane_anim = None


if __name__ == "__main__":
    unittest.main()
