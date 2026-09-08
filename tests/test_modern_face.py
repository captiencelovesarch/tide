"""The modern personality's button face: no brackets, icons where the
action has one, the user's glyph override still winning.

brutalist is the control group throughout — every case asserts it kept
the bracketed text it always had.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_modern_face.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import glyphs, theming
from tide.ui import nav_icons
from tide.ui.variants import CONTROLS_VARIANTS, make_controls
from tide.ui.widgets import BracketButton


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


MODERN = "nord"
BRUTALIST = "brutalist-mono"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()
        cls._prior = theming.manager().current()

    @classmethod
    def tearDownClass(cls) -> None:
        glyphs.set_overrides({})
        if cls._prior is not None:
            theming.manager().apply(cls._prior.slug)

    def setUp(self) -> None:
        glyphs.set_overrides({})
        self._widgets = []
        # Every theme apply pushes an app-wide stylesheet, and that
        # repolishes every widget alive in the process — thousands by the
        # time the full suite reaches this module, tens of seconds per
        # apply. Nothing here reads the app sheet (faces are checked via
        # text / icon / dynamic properties), so stub the push.
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()

    def tearDown(self) -> None:
        self._sheet.stop()
        for w in self._widgets:
            w.deleteLater()
        QTest.qWait(5)

    def _btn(self, *a, **kw) -> BracketButton:
        b = BracketButton(*a, **kw)
        self._widgets.append(b)
        return b

    def _apply(self, slug: str) -> None:
        theming.manager().apply(slug)
        QTest.qWait(5)


class LabelsTest(_Base):
    def test_modern_drops_the_brackets_brutalist_keeps_them(self) -> None:
        self._apply(MODERN)
        self.assertEqual(self._btn("home").text(), "home")
        self._apply(BRUTALIST)
        self.assertEqual(self._btn("home").text(), "[home]")

    def test_control_style_glyph_no_longer_brackets_plain_labels_in_modern(self) -> None:
        # nord is a control_style = "glyph" theme: pre-2.1 every label
        # without a glyph fell through to brackets there.
        self._apply(MODERN)
        self.assertEqual(theming.manager().current().t("layout", "control_style"), "glyph")
        self.assertEqual(self._btn("refresh").text(), "refresh")

    def test_a_theme_switch_restyles_a_live_button_both_ways(self) -> None:
        self._apply(BRUTALIST)
        b = self._btn("queue")
        self.assertEqual(b.text(), "[queue]")
        self._apply(MODERN)
        self.assertEqual(b.text(), "queue")
        self.assertEqual(b.role(), "pill")
        self._apply(BRUTALIST)
        self.assertEqual(b.text(), "[queue]")
        # back on brutalist's inline face (modern hands the face to the app sheet)
        self.assertIn('[activeState="true"]', b.styleSheet())
        self.assertIn('[current="true"]', b.styleSheet())

    def test_plain_text_is_a_pill_in_modern(self) -> None:
        self._apply(MODERN)
        b = self._btn("1.0×")
        self.assertEqual(b.role(), "pill")
        self.assertEqual(b.property("role"), "pill")
        self.assertTrue(b.icon().isNull())


class IconsTest(_Base):
    def test_a_registry_glyph_becomes_its_icon(self) -> None:
        self._apply(MODERN)
        b = self._btn("play", glyphs.glyph("play"))
        self.assertFalse(b.icon().isNull())
        self.assertEqual(b.text(), "")
        self.assertEqual(b.role(), "icon")
        self.assertEqual(b.toolTip(), "play")     # the name survives as the tip

    def test_the_same_glyph_stays_text_in_brutalist(self) -> None:
        self._apply(BRUTALIST)
        b = self._btn("play", glyphs.glyph("play"))
        self.assertTrue(b.icon().isNull())
        self.assertEqual(b.text(), "[play]")

    def test_swapping_the_glyph_swaps_the_icon(self) -> None:
        self._apply(MODERN)
        b = self._btn("play", glyphs.glyph("play"))
        before = b.icon().pixmap(20).toImage()
        b.setGlyph(glyphs.glyph("pause"))
        after = b.icon().pixmap(20).toImage()
        self.assertFalse(b.icon().isNull())
        self.assertNotEqual(before, after)

    def test_a_user_override_is_never_replaced_by_an_icon(self) -> None:
        self._apply(MODERN)
        glyphs.set_overrides({"play": "GO"})
        b = self._btn("play", glyphs.glyph("play"))
        self.assertTrue(b.icon().isNull())
        self.assertEqual(b.text(), "GO")
        self.assertEqual(b.role(), "pill")

    def test_key_for_reverse_lookup(self) -> None:
        self.assertEqual(glyphs.key_for(glyphs.glyph("shuffle")), "shuffle")
        self.assertEqual(glyphs.key_for(glyphs.glyph("like_on")), "like_on")
        self.assertIsNone(glyphs.key_for(""))
        self.assertIsNone(glyphs.key_for("no such face"))
        glyphs.set_overrides({"repeat": "R"})
        self.assertIsNone(glyphs.key_for("R"))
        self.assertTrue(glyphs.is_overridden("repeat"))
        self.assertFalse(glyphs.is_overridden("play"))

    def test_an_explicit_icon_key_wins_over_the_label(self) -> None:
        self._apply(MODERN)
        b = self._btn("refresh")
        b.setIconKey("refresh")
        self.assertFalse(b.icon().isNull())
        self.assertEqual(b.text(), "")
        b.setIconKey(None)
        self.assertTrue(b.icon().isNull())
        self.assertEqual(b.text(), "refresh")

    def test_icon_key_is_inert_in_brutalist(self) -> None:
        self._apply(BRUTALIST)
        b = self._btn("refresh")
        b.setIconKey("refresh")
        self.assertTrue(b.icon().isNull())
        self.assertEqual(b.text(), "[refresh]")

    def test_pill_role_keeps_icon_and_label(self) -> None:
        self._apply(MODERN)
        b = self._btn("back")
        b.setIconKey("back")
        b.setRole("pill")
        self.assertFalse(b.icon().isNull())
        self.assertEqual(b.text(), "back")

    def test_sleep_face_strips_the_glyph_and_keeps_the_state(self) -> None:
        self._apply(MODERN)
        b = self._btn(glyphs.glyph("sleep"))
        b.setIconKey("sleep")
        self.assertEqual(b.text(), "")
        self.assertEqual(b.role(), "icon")
        b.setLabel(f"{glyphs.glyph('sleep')} 12m")
        self.assertEqual(b.text(), "12m")
        self.assertEqual(b.role(), "pill")
        b.setLabel(glyphs.glyph("sleep"))
        self.assertEqual(b.text(), "")
        self.assertEqual(b.role(), "icon")

    def test_every_icon_key_the_app_uses_ships_an_svg(self) -> None:
        for key in ("play", "pause", "prev", "next", "shuffle", "repeat",
                    "repeat_one", "like_on", "like_off", "sleep", "fullscreen",
                    "refresh", "back", "pin", "close", "audio_fx", "lyrics",
                    "queue"):
            self.assertIsNotNone(nav_icons.svg_text_for(key), key)

    def test_state_ink_is_re_rendered(self) -> None:
        self._apply(MODERN)
        b = self._btn("shuffle", glyphs.glyph("shuffle"))
        plain = b.icon().pixmap(20).toImage()
        b.setActiveState(True)
        lit = b.icon().pixmap(20).toImage()
        self.assertNotEqual(plain, lit)
        b.setActiveState(False)
        b.setMuted(True)
        dim = b.icon().pixmap(20).toImage()
        self.assertNotEqual(plain, dim)
        self.assertEqual(b.property("muted"), True)


class TransportTest(_Base):
    def test_icons_variant_is_registered(self) -> None:
        self.assertIn("icons", CONTROLS_VARIANTS)

    def test_icons_bundle_exposes_six_buttons_with_play_as_primary(self) -> None:
        self._apply(MODERN)
        c = make_controls("icons")
        self._widgets.append(c)
        for name in ("shuffle_btn", "prev_btn", "play_btn", "next_btn",
                     "repeat_btn", "like_btn"):
            b = getattr(c, name)
            self.assertFalse(b.icon().isNull(), name)
            self.assertEqual(b.text(), "", name)
            self.assertEqual(b.role(), "transport", name)
        self.assertEqual(c.play_btn.property("primary"), True)
        self.assertIsNone(c.prev_btn.property("primary"))

    def test_large_and_compact_wear_icons_in_modern_and_glyphs_in_brutalist(self) -> None:
        for variant in ("large", "compact"):
            self._apply(MODERN)
            c = make_controls(variant)
            self._widgets.append(c)
            self.assertFalse(c.play_btn.icon().isNull(), variant)
            self.assertEqual(c.play_btn.text(), "", variant)
            self._apply(BRUTALIST)
            self.assertTrue(c.play_btn.icon().isNull(), variant)
            self.assertEqual(c.play_btn.text(), glyphs.glyph("play"), variant)

    def test_the_four_variants_look_different_in_modern(self) -> None:
        self._apply(MODERN)
        bundles = {v: make_controls(v) for v in ("icons", "large", "compact", "bracket")}
        self._widgets.extend(bundles.values())
        # prev, not play: play wears the one shared primary size in two variants
        sizes = {v: b.prev_btn.iconSize().width() for v, b in bundles.items() if v != "bracket"}
        self.assertGreater(sizes["large"], sizes["icons"])
        self.assertGreater(sizes["icons"], sizes["compact"])
        self.assertEqual(bundles["icons"].play_btn.property("primary"), True)
        self.assertEqual(bundles["large"].play_btn.property("primary"), True)
        self.assertIsNone(bundles["compact"].play_btn.property("primary"))
        # bracket keeps the typed glyphs as text pills
        b = bundles["bracket"].play_btn
        self.assertTrue(b.icon().isNull())
        self.assertEqual(b.text(), glyphs.glyph("play"))
        self.assertEqual(b.role(), "pill")
        self.assertIsNone(b.property("primary"))

    def test_icons_variant_degrades_to_the_bracket_face_in_brutalist(self) -> None:
        self._apply(BRUTALIST)
        c = make_controls("icons")
        self._widgets.append(c)
        self.assertEqual(c.play_btn.text(), "[play]")
        self.assertTrue(c.play_btn.icon().isNull())


class NavRowTest(_Base):
    def test_row_role_keeps_the_label_beside_the_svg(self) -> None:
        self._apply(MODERN)
        b = self._btn("home")
        b.setRole("row")
        b.setSvgIcon(nav_icons.svg_text_for("home"))
        self.assertEqual(b.text(), "home")
        self.assertFalse(b.icon().isNull())
        self.assertEqual(b.property("role"), "row")
        b.setCurrent(True)
        self.assertEqual(b.property("current"), True)
        self.assertTrue(b.isCurrent())


if __name__ == "__main__":
    unittest.main()


class HeadlineLabelTest(_Base):
    """The modern strip label: title as the headline, artist as the byline."""

    def test_registered_and_built(self) -> None:
        from tide.ui.variants import NOW_LABEL_VARIANTS, HeadlineNowLabel, make_now_label
        self.assertIn("headline", NOW_LABEL_VARIANTS)
        lbl = make_now_label("headline")
        self._widgets.append(lbl)
        self.assertIsInstance(lbl, HeadlineNowLabel)

    def test_title_font_is_larger_and_paints(self) -> None:
        from tide.ui.variants import make_now_label
        from tide import material
        self._apply(MODERN)
        lbl = make_now_label("headline")
        self._widgets.append(lbl)
        lbl.resize(400, 60)
        lbl.setTrack("radiohead", "creep", "pablo honey")
        lbl.setInsights("84.2m plays")
        lbl.setStatus("loading…")
        self.assertGreater(lbl._title_font().pointSizeF(), lbl.font().pointSizeF())
        self.assertAlmostEqual(lbl._title_font().pointSizeF(),
                               lbl.font().pointSizeF() * material.TITLE_SCALE, places=3)
        lbl.show(); QTest.qWait(20)
        img = lbl.grab().toImage()
        self.assertFalse(img.isNull())
        # tooltip still leads with the whole run so nothing is hidden
        self.assertIn("radiohead — creep", lbl.toolTip())
        self.assertIn("pablo honey", lbl.toolTip())

    def test_every_modern_theme_defaults_to_headline(self) -> None:
        for theme in theming.manager().list_themes():
            if theme.aesthetic != "modern" or not theme.slots:
                continue
            if theme.path.is_relative_to(theming.BUNDLED_THEMES_DIR):
                self.assertEqual(theme.slots.get("now_label"), "headline", theme.slug)
                self.assertEqual(theme.slots.get("controls"), "icons", theme.slug)
