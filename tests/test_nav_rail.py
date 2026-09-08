"""The rail knows which view is showing, and modern lets it breathe.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_nav_rail.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSizePolicy

from tide import theming
from tide.playback import MpvBackend, PlaybackRouter
from tide.sources.local import LocalSource
from tide.ui import scale


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _RailCase(unittest.TestCase):
    THEME = "nord"

    def setUp(self) -> None:
        self.app = _app()
        # theme applies repolish every widget alive; the rail's state is
        # read through properties and geometry, never the app sheet.
        self._sheet = mock.patch.object(self.app, "setStyleSheet")
        self._sheet.start()
        theming.manager().refresh()
        theming.manager().apply(self.THEME)
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)

    def tearDown(self) -> None:
        self.w.close()
        self._sheet.stop()
        QTest.qWait(20)

    def _current(self) -> list[str]:
        return [name for name, b in self.w._nav_buttons.items() if b.isCurrent()]


class CurrentViewTest(_RailCase):
    def test_home_is_current_at_start(self) -> None:
        self.assertEqual(self._current(), ["home"])

    def test_switching_moves_the_marker(self) -> None:
        self.w._switch_view("library")
        QTest.qWait(20)
        self.assertEqual(self._current(), ["library"])
        self.assertEqual(self.w.nav_library_btn.property("current"), True)
        self.assertEqual(self.w.nav_home_btn.property("current"), False)

    def test_a_detail_page_keeps_the_root_it_came_from(self) -> None:
        self.w._switch_view("library")
        QTest.qWait(20)
        self.w._push_view(6)                 # album page
        QTest.qWait(20)
        self.assertEqual(self._current(), ["library"])
        self.w._go_back()
        QTest.qWait(20)
        self.assertEqual(self._current(), ["library"])

    def test_a_direct_stack_set_is_seen_too(self) -> None:
        self.w.stack.setCurrentIndex(9)      # source, no _switch_view
        QTest.qWait(20)
        self.assertEqual(self._current(), ["source"])

    def test_settings_is_never_current(self) -> None:
        for name in ("home", "library", "queue", "lyrics", "history",
                     "visualizer", "source", "audio_fx"):
            self.w._switch_view(name)
            QTest.qWait(10)
            self.assertFalse(self.w.nav_settings_btn.isCurrent(), name)


class GeometryTest(_RailCase):
    def test_modern_floats_the_strip_and_brutalist_keeps_it_flush(self) -> None:
        m = self.w._strip_row.contentsMargins()
        self.assertEqual((m.left(), m.right()), (scale.px(12), scale.px(12)))
        self.assertGreater(m.bottom(), 0)
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)
        m = self.w._strip_row.contentsMargins()
        self.assertEqual((m.left(), m.top(), m.right(), m.bottom()), (0, 0, 0, 0))
        theming.manager().apply("nord")
        QTest.qWait(20)
        self.assertEqual(self.w._strip_row.contentsMargins().left(), scale.px(12))

    def test_modern_rail_is_wider_and_rows_fill_it(self) -> None:
        self.assertEqual(self.w._nav_frame.width(), scale.px(176))
        for name, b in self.w._nav_buttons.items():
            self.assertEqual(b.sizePolicy().horizontalPolicy(),
                             QSizePolicy.Expanding, name)
            self.assertEqual(b.property("role"), "row", name)

    def test_brutalist_keeps_its_column(self) -> None:
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)
        self.assertEqual(self.w._nav_frame.width(), 140)
        for name, b in self.w._nav_buttons.items():
            self.assertEqual(b.sizePolicy().horizontalPolicy(),
                             QSizePolicy.Maximum, name)
            self.assertEqual(b.text(), f"[{name.replace('audio_fx', 'fx')}]"
                             if name != "audio_fx" else "[fx]", name)
        # the marker survives the flip: brutalist shows it as accent text
        self.assertEqual(self._current(), ["home"])
        self.assertIn('[current="true"]', self.w.nav_home_btn.styleSheet())
        theming.manager().apply("nord")
        QTest.qWait(20)
        self.assertEqual(self.w._nav_frame.width(), scale.px(176))


class CollapseTest(_RailCase):
    def tearDown(self) -> None:
        # a window with settings attached persists its size on close;
        # detach so this module leaves no settings file for the next one
        self.w._settings = None
        super().tearDown()

    def _attach(self, icon_set: str = "svg"):
        from tide.settings import Settings
        s = Settings()
        s.nav_icon_set = icon_set
        self.w._settings = s
        self.w.apply_nav_icons(icon_set)
        return s

    def test_shortcut_is_registered(self) -> None:
        from tide.ui.window import ACTIONS
        action = next(a for a in ACTIONS if a.id == "toggle_rail")
        self.assertEqual(action.default, "Ctrl+B")

    def test_toggle_folds_to_icons_and_back(self) -> None:
        s = self._attach("svg")
        self.assertTrue(self.w.nav_collapse_btn.isVisibleTo(self.w))
        wide = self.w._nav_frame.width()
        with mock.patch("tide.settings.save_fields") as save:
            self.w.toggle_nav_rail()
            save.assert_called_once_with(s, "nav_rail_collapsed")
        self.assertTrue(s.nav_rail_collapsed)
        self.assertEqual(self.w._nav_frame.width(), scale.px(56))
        self.assertLess(self.w._nav_frame.width(), wide)
        for name, b in self.w._nav_buttons.items():
            self.assertEqual(b.text(), "", name)
            self.assertFalse(b.icon().isNull(), name)
            self.assertEqual(b.toolTip(), name.replace("audio_fx", "fx"), name)
            self.assertEqual(b.property("role"), "icon", name)
        self.assertEqual(self._current(), ["home"])
        with mock.patch("tide.settings.save_fields"):
            self.w.toggle_nav_rail()
        self.assertFalse(s.nav_rail_collapsed)
        self.assertEqual(self.w._nav_frame.width(), wide)
        self.assertEqual(self.w.nav_home_btn.text(), "home")
        self.assertEqual(self.w.nav_home_btn.property("role"), "row")

    def test_no_icons_means_no_collapse(self) -> None:
        s = self._attach("off")
        self.assertFalse(self.w.nav_collapse_btn.isVisibleTo(self.w))
        with mock.patch("tide.settings.save_fields") as save:
            self.w.toggle_nav_rail()
            save.assert_not_called()
        self.assertFalse(s.nav_rail_collapsed)
        self.assertEqual(self.w.nav_home_btn.text(), "home")
        self.assertIn("nav icons", self.w.statusBar().currentMessage())

    def test_collapsed_state_survives_an_icon_set_change_and_brutalist(self) -> None:
        s = self._attach("svg")
        s.nav_rail_collapsed = True
        self.w.apply_nav_icons("classic")          # glyph prefixes: the glyph alone
        self.assertEqual(self.w.nav_home_btn.text(), "⌂")
        self.assertEqual(self.w.nav_home_btn.property("role"), "icon")
        self.assertEqual(self.w.nav_home_btn.toolTip(), "home")
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)
        # brutalist folds to the glyph alone, no brackets, tooltip carries the name
        self.assertEqual(self.w.nav_home_btn.text(), "⌂")
        self.assertEqual(self.w.nav_home_btn.toolTip(), "home")
        self.assertEqual(self.w._nav_frame.width(), scale.px(56))
        s.nav_rail_collapsed = False
        self.w._apply_nav_rail()
        self.assertEqual(self.w.nav_home_btn.text(), "⌂ [home]")
        self.assertEqual(self.w._nav_frame.width(), 140)


class ShellSheetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()

    def test_modern_gets_the_shell_panels_and_brutalist_does_not(self) -> None:
        themes = {t.slug: t for t in theming.manager().list_themes()}
        modern = theming._substitute(themes["blackwater"].qss, themes["blackwater"])
        brutal = theming._substitute(themes["brutalist-mono"].qss, themes["brutalist-mono"])
        strip_rule = modern[modern.rindex("QFrame#now_playing {"):]
        self.assertIn("background: #0dffffff;", strip_rule)
        self.assertIn("border: 1px solid #1affffff;", strip_rule)
        self.assertIn("border-radius: 0px;", strip_rule)     # blackwater: radius 0
        # and it lands AFTER the transparentizing backdrop block
        self.assertGreater(modern.rindex("QFrame#now_playing {"),
                           modern.index("QSizeGrip { background: transparent; }"))
        # the rail stays bare: no panel, no divider (cut 2026-07-30 after a
        # sober test; putting either back drew an edge the user had removed)
        self.assertNotIn("QFrame#nav { background: #", modern)
        # brutalist paints its own flat strip pane in _base.qss, but gets no
        # shell block appended after the backdrop block
        self.assertLess(brutal.rindex("QFrame#now_playing {"),
                        brutal.index("QSizeGrip { background: transparent; }"))
        self.assertNotIn("border-radius", brutal[brutal.rindex("QFrame#now_playing {"):][:120])
        self.assertNotIn("@surface", modern)
        self.assertNotIn("@radius_strip", modern)

    def test_strip_radius_follows_the_corner_style_capped(self) -> None:
        themes = {t.slug: t for t in theming.manager().list_themes()}
        t = themes["adaptive"]                                   # radius_px 6
        self.assertIn("border-radius: 12px;", theming._substitute(t.qss, t)
                      [theming._substitute(t.qss, t).rindex("QFrame#now_playing"):])
        big = theming.Theme(slug="x", name="x", path=t.path, tokens=dict(t.tokens),
                            typography=t.typography, layout={**t.layout, "radius_px": 12},
                            aesthetic="modern", dark=True, qss=t.qss)
        out = theming._substitute(big.qss, big)
        self.assertIn("border-radius: 20px;", out[out.rindex("QFrame#now_playing"):])


if __name__ == "__main__":
    unittest.main()


class DialogScaleTest(unittest.TestCase):
    """Dialogs size themselves through the UI scale, and the settings
    dialog refits when the scale changes while it is open."""

    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("nord")
        cls._saved = scale.current()

    @classmethod
    def tearDownClass(cls) -> None:
        scale.set_factor(cls._saved)

    def setUp(self) -> None:
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()

    def tearDown(self) -> None:
        self._sheet.stop()
        scale.set_factor(scale.Scale.NORMAL)

    def test_settings_dialog_scales_and_refits_live(self) -> None:
        from tide.settings import Settings
        from tide.ui.settings import SettingsDialog
        scale.set_factor(scale.Scale.NORMAL)
        dlg = SettingsDialog(Settings())
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.minimumWidth(), 620)
        self.assertEqual((dlg.width(), dlg.height()), (680, 720))
        scale.set_factor(scale.Scale.HUGE)
        theming.manager().apply("nord")            # re-emits theme_changed
        QTest.qWait(10)
        self.assertEqual(dlg.minimumWidth(), scale.px(620))
        self.assertGreaterEqual(dlg.width(), scale.px(680))
        # grow only: a bigger box the user dragged stays bigger
        dlg.resize(1200, 1000)
        theming.manager().apply("nord")
        QTest.qWait(10)
        self.assertEqual((dlg.width(), dlg.height()), (1200, 1000))

    def test_other_dialogs_take_the_scale_at_construction(self) -> None:
        from tide.ui.keymap_editor import KeymapEditor
        from tide.settings import Settings
        scale.set_factor(scale.Scale.HUGE)
        for cls, kwargs in ((KeymapEditor, {"current_settings": Settings()}),):
            try:
                dlg = cls(**kwargs)
            except TypeError:
                dlg = cls(Settings())
            self.addCleanup(dlg.deleteLater)
            self.assertEqual(dlg.minimumWidth(), scale.px(560), cls.__name__)
            self.assertEqual(dlg.width(), scale.px(600), cls.__name__)
