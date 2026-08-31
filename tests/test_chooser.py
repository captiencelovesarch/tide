"""v2.0 phase 4 — the chooser ("choose your tide").

Pinned: both panes build offscreen, standalone or inside the dialog;
each pane wears its OWN personality's real theme — proven by putting a
light theme on the app and rendering the panes anyway; a click / Enter
resolves to the right preset id, Esc resolves to nothing; the chooser
never writes settings — the caller applies; brutalist never animates,
modern's backdrop stops dead on hide/close, and a backdrop frame never
touches tokens or QSS.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import config, presets, theming
from tide import settings as settings_module
from tide.ui import chooser, motion


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _ChooserCase(unittest.TestCase):
    """The test_preset_flip-shaped harness: hermetic settings file, QSS
    pushes suppressed, deleteLater + drained teardown."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-chooser-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        # pin FULL so "brutalist is still anyway" is a real assertion,
        # not an artifact of whatever the adopted preset set
        self._orig_intensity = motion.intensity()
        motion.initialize("full")
        self._widgets: list = []

    def tearDown(self) -> None:
        for w in self._widgets:
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        self._widgets.clear()
        QTest.qWait(30)
        motion.set_intensity(self._orig_intensity)
        config.SETTINGS_FILE = self._orig_settings_file
        mgr = theming.manager()
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0)
        QTest.qWait(30)
        for _ in range(10):
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    # ---- helpers

    def _dialog(self, **kw) -> chooser.ChooserDialog:
        d = chooser.ChooserDialog(**kw)
        self._widgets.append(d)
        d.show()
        QTest.qWait(30)
        return d

    def _pane(self, preset_id: str) -> chooser.PersonalityPane:
        p = chooser.PersonalityPane(preset_id)
        self._widgets.append(p)
        return p

    @staticmethod
    def _render(widget):
        pm = QPixmap(widget.size())
        pm.fill(Qt.transparent)
        widget.render(pm)
        return pm.toImage()


class BuildTests(_ChooserCase):
    def test_dialog_builds_both_panes(self) -> None:
        d = self._dialog()
        self.assertEqual(sorted(d.panes()), ["brutalist", "modern"])
        for preset_id, pane in d.panes().items():
            self.assertEqual(pane.preset_id, preset_id)
            self.assertTrue(pane.isVisible())

    def test_pane_builds_standalone(self) -> None:
        # phase 4B hosts this widget inside the onboarding wizard, with
        # no ChooserDialog anywhere — it has to stand on its own
        for preset_id in chooser.PANE_ORDER:
            pane = self._pane(preset_id)
            pane.resize(420, 520)
            pane.show()
            QTest.qWait(10)
            self.assertEqual(pane.preset_id, preset_id)

    def test_compact_panes_fit_the_wizard_canvas(self) -> None:
        # phase 4B drops these into the 720x600 wizard; the roomy pane's
        # ~480px minimum doesn't fit, compact is the same widget tighter
        for preset_id in chooser.PANE_ORDER:
            roomy = self._pane(preset_id)
            tight = chooser.PersonalityPane(preset_id, compact=True)
            self._widgets.append(tight)
            self.assertLess(tight.minimumSizeHint().height(),
                            roomy.minimumSizeHint().height())
            self.assertLessEqual(tight.minimumSizeHint().width(),
                                 roomy.minimumSizeHint().width())
            self.assertLess(tight.minimumSizeHint().height(), 400)
            self.assertEqual(tight.preset_id, preset_id)
            self.assertIsNotNone(tight.theme)

    def test_unknown_personality_is_an_error(self) -> None:
        with self.assertRaises(KeyError):
            chooser.PersonalityPane("chartreuse")

    def test_no_screen_falls_back_to_a_sane_size(self) -> None:
        with mock.patch.object(chooser, "_available_geometry",
                               return_value=None):
            d = chooser.ChooserDialog()
            self._widgets.append(d)
        self.assertEqual((d.width(), d.height()), chooser.FALLBACK_SIZE)

    def test_sizes_to_the_available_geometry(self) -> None:
        geo = chooser._available_geometry()
        self.assertIsNotNone(geo, "offscreen should still report a screen")
        d = self._dialog()
        self.assertEqual(d.width(), geo.width())
        self.assertEqual(d.height(), geo.height())


class ThemeTests(_ChooserCase):
    """The panes are previews of two REAL themes, not two paint jobs."""

    def test_pane_theme_is_the_personality_builtin(self) -> None:
        discovered = theming.discover_themes()
        for preset_id in chooser.PANE_ORDER:
            pane = self._pane(preset_id)
            slug = presets.builtin(preset_id).theme
            self.assertIsNotNone(pane.theme)
            self.assertEqual(pane.theme.slug, slug)
            self.assertEqual(pane.theme.tokens, discovered[slug].tokens)
            self.assertEqual(pane.theme.aesthetic, preset_id)

    def test_tokens_reach_the_paint_not_the_app_theme(self) -> None:
        # dress the APP in a light theme, then render the panes — a pane
        # that picked up the app's palette instead of its theme file fails
        theming.manager().apply("paper")
        QTest.qWait(20)
        paper_bg = theming.manager().current().token("bg")
        d = self._dialog()
        brutalist = d.pane("brutalist")
        img = self._render(brutalist)
        corner = img.pixelColor(6, 6).name()
        self.assertEqual(corner, brutalist.theme.token("bg"),
                         "the brutalist pane did not paint its own bg token")
        self.assertNotEqual(corner, paper_bg,
                            "the pane wore the app's theme")
        modern = d.pane("modern")
        modern_corner = self._render(modern).pixelColor(24, 24)
        self.assertNotEqual(modern_corner.name(), paper_bg)
        self.assertLess(modern_corner.lightness(), 110,
                        "the modern pane's dark theme did not survive a "
                        "light app theme")

    def test_typography_is_the_themes_own(self) -> None:
        # every theme's base QSS carries a universal font rule, so the
        # pane fonts have to ride in its own stylesheet to win
        for preset_id in chooser.PANE_ORDER:
            pane = self._pane(preset_id)
            family = str(pane.theme.t("typography", "family", ""))
            self.assertIn(family, pane.styleSheet())
        brutalist = self._pane("brutalist")
        modern = self._pane("modern")
        self.assertTrue(bool(brutalist.theme.t("typography", "mono", False)))
        self.assertFalse(bool(modern.theme.t("typography", "mono", False)))

    def test_accent_token_styles_the_commit_control(self) -> None:
        modern = self._pane("modern")
        self.assertIn(modern.theme.token("accent").lower(),
                      modern._button.styleSheet().lower())

    def test_generated_art_needs_no_network_or_cache(self) -> None:
        for preset_id in chooser.PANE_ORDER:
            pane = self._pane(preset_id)
            pm = chooser.placeholder_art(48, pane.theme, radius=6)
            self.assertFalse(pm.isNull())
            self.assertEqual(pm.size().width(), 48)

    def test_preview_slider_leaves_the_theme_manager_alone(self) -> None:
        # _PreviewSpring shadows the manager's effective-theme accessor
        # for exactly one paint — a leaked swap would repaint the whole
        # app in the chooser's theme
        mgr = theming.manager()
        theming.manager().apply("gruvbox")
        QTest.qWait(20)
        before = mgr.current_effective()
        pane = self._pane("modern")
        pane.resize(420, 520)
        self._render(pane)
        self.assertNotIn("current_effective", mgr.__dict__,
                         "the preview swap leaked onto the theme manager")
        self.assertIs(mgr.current_effective(), before)

    def test_ambient_theme_change_does_not_bleed_into_a_pane(self) -> None:
        d = self._dialog()
        brutalist = d.pane("brutalist")
        before = self._render(brutalist).pixelColor(6, 6).name()
        theming.manager().apply("paper")     # light theme, mid-life
        QTest.qWait(30)
        self.assertEqual(self._render(brutalist).pixelColor(6, 6).name(),
                         before,
                         "an app restyle repainted the preview")

    def test_pane_survives_a_themeless_install(self) -> None:
        with mock.patch.object(chooser, "pane_theme", return_value=None):
            pane = self._pane("modern")
            pane.resize(320, 420)
            self.assertIsNone(pane.theme)
            self.assertFalse(self._render(pane).isNull())

    # ---- the live per-personality text layers must not bleed in

    @staticmethod
    def _labels(widget) -> list[str]:
        from PySide6.QtWidgets import QLabel
        return [lab.text() for lab in widget.findChildren(QLabel)]

    def _heading(self, pane) -> str:
        for text in self._labels(pane):
            if "playing" in text.lower():
                return text
        self.fail("the brutalist mock lost its 'now playing' rule")

    def test_the_brutalist_rule_ignores_the_live_case_override(self) -> None:
        # text_case_override is a per-personality stash field and
        # theming's override beats any theme — an upgrader wearing
        # "upper" would otherwise meet a pane shouting NOW PLAYING
        from tide import glyphs
        try:
            theming.set_case_override("upper")
            glyphs.set_overrides({"heading_dash": "="})
            QTest.qWait(20)
            heading = self._heading(self._pane("brutalist"))
        finally:
            glyphs.set_overrides({})
            theming.set_case_override("")
            QTest.qWait(20)
        self.assertIn("now playing", heading)
        self.assertNotIn("NOW PLAYING", heading)
        self.assertNotIn("=", heading,
                         "a per-personality glyph swap bled into the pitch")
        self.assertTrue(heading.startswith("──"), heading)

    def test_the_apps_own_headings_still_follow_the_user(self) -> None:
        # the preview escape is opt-in: ordinary chrome keeps obeying the
        # override and the glyph pack exactly as before
        from tide import glyphs
        from tide.ui.headings import line_heading
        try:
            theming.set_case_override("upper")
            glyphs.set_overrides({"heading_dash": "="})
            QTest.qWait(20)
            live = line_heading("now playing", 34)
        finally:
            glyphs.set_overrides({})
            theming.set_case_override("")
            QTest.qWait(20)
        self.assertIn("NOW PLAYING", live)
        self.assertTrue(live.startswith("=="), live)


class ResolutionTests(_ChooserCase):
    """What the chooser resolves to, and what it refuses to do about it."""

    def test_pane_click_chooses_that_personality(self) -> None:
        d = self._dialog(initial="brutalist")
        got: list[str] = []
        d.chosen.connect(got.append)
        modern = d.pane("modern")
        QTest.mouseClick(modern, Qt.LeftButton, pos=QPoint(6, 6))
        # choice() lands synchronously; the close + signal are deferred a
        # turn so nothing tears the dialog down inside a click handler
        self.assertEqual(d.choice(), "modern")
        self.assertEqual(got, [])
        QTest.qWait(30)
        self.assertEqual(got, ["modern"])
        self.assertFalse(d.isVisible())
        self.assertEqual(d.result(), int(chooser.ChooserDialog.Accepted))

    def test_choose_button_chooses(self) -> None:
        d = self._dialog()
        got: list[str] = []
        d.chosen.connect(got.append)
        QTest.mouseClick(d.pane("brutalist")._button, Qt.LeftButton)
        QTest.qWait(30)
        self.assertEqual(got, ["brutalist"])

    def test_arrows_move_focus_and_enter_chooses(self) -> None:
        d = self._dialog(initial="brutalist")
        got: list[str] = []
        d.chosen.connect(got.append)
        self.assertEqual(d.focused_preset(), "brutalist")
        self.assertTrue(d.pane("brutalist").is_selected())
        QTest.keyClick(d, Qt.Key_Right)
        self.assertEqual(d.focused_preset(), "modern")
        self.assertTrue(d.pane("modern").is_selected())
        self.assertFalse(d.pane("brutalist").is_selected())
        QTest.keyClick(d, Qt.Key_Left)
        self.assertEqual(d.focused_preset(), "brutalist")
        QTest.keyClick(d, Qt.Key_Return)
        QTest.qWait(30)
        self.assertEqual(got, ["brutalist"])

    def test_initial_preselects_a_pane(self) -> None:
        d = self._dialog(initial="modern")
        self.assertEqual(d.focused_preset(), "modern")
        self.assertTrue(d.pane("modern").is_selected())

    def test_escape_resolves_to_nothing(self) -> None:
        d = self._dialog()
        got: list[str] = []
        d.chosen.connect(got.append)
        QTest.keyClick(d, Qt.Key_Escape)
        QTest.qWait(30)
        self.assertEqual(got, [], "a dismissal emitted a choice")
        self.assertEqual(d.choice(), "")
        self.assertEqual(d.result(), int(chooser.ChooserDialog.Rejected))

    def test_close_resolves_to_nothing(self) -> None:
        d = self._dialog()
        got: list[str] = []
        d.chosen.connect(got.append)
        d.close()
        QTest.qWait(30)
        self.assertEqual(got, [])
        self.assertEqual(d.choice(), "")

    def test_a_second_click_cannot_change_the_answer(self) -> None:
        d = self._dialog()
        got: list[str] = []
        d.chosen.connect(got.append)
        QTest.mouseClick(d.pane("modern"), Qt.LeftButton, pos=QPoint(6, 6))
        QTest.mouseClick(d.pane("brutalist"), Qt.LeftButton, pos=QPoint(6, 6))
        QTest.qWait(30)
        self.assertEqual(got, ["modern"])

    def test_the_chooser_never_writes_settings(self) -> None:
        # nothing in here — construction, choosing, dismissing — may
        # touch the settings file; the caller persists
        with mock.patch.object(settings_module, "save") as save, \
                mock.patch.object(settings_module, "save_fields") as fields:
            d = self._dialog()
            QTest.mouseClick(d.pane("modern"), Qt.LeftButton,
                             pos=QPoint(6, 6))
            QTest.qWait(30)
            d2 = self._dialog()
            QTest.keyClick(d2, Qt.Key_Escape)
            QTest.qWait(30)
            self.assertEqual(save.call_count, 0)
            self.assertEqual(fields.call_count, 0)
        self.assertFalse(config.SETTINGS_FILE.exists(),
                         "the chooser wrote settings behind the caller's back")


class MotionTests(_ChooserCase):
    """Brutalist stillness is a product contract; the modern backdrop is
    a frame loop that must never touch the theme bus."""

    def test_brutalist_never_animates(self) -> None:
        d = self._dialog()
        pane = d.pane("brutalist")
        self.assertFalse(pane.animates())
        self.assertFalse(pane.is_animating())
        pane.start_animation()          # even when asked directly
        self.assertFalse(pane.is_animating())
        QTest.qWait(120)
        self.assertEqual(pane._phase, 0.0)

    def test_modern_animates_within_the_frame_cap(self) -> None:
        d = self._dialog()
        pane = d.pane("modern")
        self.assertTrue(pane.animates())
        self.assertTrue(pane.is_animating())
        self.assertGreaterEqual(pane._timer.interval(),
                                1000 // chooser.BACKDROP_FPS)
        QTest.qWait(150)
        self.assertGreater(pane._phase, 0.0, "the backdrop never ticked")

    def test_frames_never_touch_tokens_or_qss(self) -> None:
        # theme_changed is a ~10Hz bus, not a frame clock — a backdrop
        # frame is a float and an update(), full stop
        d = self._dialog()
        pane = d.pane("modern")
        mgr = theming.manager()
        with mock.patch.object(self.app, "setStyleSheet") as qss, \
                mock.patch.object(mgr, "override_tokens") as tokens, \
                mock.patch.object(mgr, "apply_bundle") as bundle:
            before = pane._phase
            QTest.qWait(200)
            self.assertGreater(pane._phase, before)
            self.assertEqual(qss.call_count, 0)
            self.assertEqual(tokens.call_count, 0)
            self.assertEqual(bundle.call_count, 0)

    def test_hide_and_close_stop_every_timer(self) -> None:
        d = self._dialog()
        modern = d.pane("modern")
        self.assertTrue(modern.is_animating())
        d.hide()
        QTest.qWait(20)
        self.assertFalse(modern.is_animating())
        d.show()
        QTest.qWait(20)
        self.assertTrue(modern.is_animating())
        d.close()
        QTest.qWait(20)
        for pane in d.panes().values():
            self.assertFalse(pane.is_animating(),
                             "a backdrop timer outlived the chooser")

    def test_choosing_stops_the_backdrop(self) -> None:
        d = self._dialog()
        modern = d.pane("modern")
        QTest.mouseClick(modern, Qt.LeftButton, pos=QPoint(6, 6))
        self.assertFalse(modern.is_animating())

    def test_reduced_motion_stills_the_modern_pane_too(self) -> None:
        os.environ["QT_REDUCED_MOTION"] = "1"
        try:
            motion.initialize("full")
            pane = self._pane("modern")
            pane.resize(360, 460)
            pane.show()
            QTest.qWait(30)
            self.assertFalse(pane.animates())
            self.assertFalse(pane.is_animating())
        finally:
            os.environ.pop("QT_REDUCED_MOTION", None)
            motion.initialize("full")
        self.assertFalse(self._render(pane).isNull())

    def test_app_motion_off_does_not_still_the_modern_pitch(self) -> None:
        # the panes preview their own personality's motion, not the
        # app's — a brutalist user still gets shown what modern looks like
        motion.set_intensity("off")
        pane = self._pane("modern")
        pane.resize(360, 460)
        pane.show()
        QTest.qWait(30)
        self.assertTrue(pane.animates())
        self.assertTrue(pane.is_animating())


if __name__ == "__main__":
    unittest.main()
