"""2.3 icon sets (modern) and glyph packs (brutalist).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_icon_sets.py
"""
import re
import sys
import unittest

from PySide6.QtCore import QByteArray
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from tide import glyphs
from tide.ui import icon_sets, nav_icons


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class IconSetTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.addCleanup(icon_sets.set_active, icon_sets.active())

    def test_every_set_answers_every_key_with_valid_svg(self) -> None:
        for name in icon_sets.SETS:
            for key in icon_sets.keys():
                with self.subTest(set=name, key=key):
                    svg = icon_sets.svg_for(key, name)
                    self.assertIsNotNone(svg)
                    self.assertIn("currentColor", svg)
                    r = QSvgRenderer(QByteArray(
                        svg.replace("currentColor", "#fff").encode()))
                    self.assertTrue(r.isValid())

    def test_regular_is_the_shipped_set(self) -> None:
        for key in icon_sets.keys():
            shipped = (icon_sets.REGULAR_DIR / f"{key}.svg").read_text()
            self.assertEqual(icon_sets.svg_for(key, "regular"), shipped)

    def test_weights_restroke(self) -> None:
        sw = lambda s: float(re.search(r'<svg[^>]*stroke-width="([^"]+)"', s).group(1))  # noqa: E731
        self.assertLess(sw(icon_sets.svg_for("play", "thin")),
                        sw(icon_sets.svg_for("play", "regular")))
        self.assertGreater(sw(icon_sets.svg_for("play", "bold")),
                           sw(icon_sets.svg_for("play", "regular")))

    def test_drawn_sets_differ_from_regular(self) -> None:
        for name in ("filled", "rounded", "duotone"):
            differ = [k for k in icon_sets.keys()
                      if icon_sets.svg_for(k, name) != icon_sets.svg_for(k, "regular")]
            self.assertGreater(len(differ), len(icon_sets.keys()) // 2, name)

    def test_duotone_layers_the_fill_under_the_outline(self) -> None:
        svg = icon_sets.svg_for("sleep", "duotone")
        self.assertIn('fill-opacity="0.32"', svg)

    def test_active_set_drives_nav_lookup(self) -> None:
        icon_sets.set_active("filled")
        self.assertEqual(nav_icons.svg_text_for("home"),
                         icon_sets.svg_for("home", "filled"))
        self.assertTrue(icon_sets.set_active("regular"))
        self.assertFalse(icon_sets.set_active("regular"))

    def test_unknown_set_and_key(self) -> None:
        icon_sets.set_active("nope")
        self.assertEqual(icon_sets.active(), "regular")
        self.assertIsNone(icon_sets.svg_for("no_such_icon"))
        self.assertIsNone(icon_sets.svg_for("../svg/play"))


class GlyphPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(glyphs.set_pack, glyphs.active_pack())

    def test_packs_registered_with_labels(self) -> None:
        for name in ("default", "ascii", "blocks", "arrows", "dots"):
            self.assertIn(name, glyphs.packs())
            self.assertIn(name, glyphs.PACK_LABELS)

    def test_pack_switch_and_reverse_lookup(self) -> None:
        glyphs.set_pack("ascii")
        self.assertEqual(glyphs.glyph("play"), ">")
        # modern still finds the icon behind a pack face
        self.assertEqual(glyphs.key_for(">"), "play")

    def test_each_pack_tells_like_on_from_off(self) -> None:
        for name in glyphs.packs():
            glyphs.set_pack(name)
            self.assertNotEqual(glyphs.glyph("like_on"), glyphs.glyph("like_off"))
            self.assertNotEqual(glyphs.glyph("play"), glyphs.glyph("pause"))


if __name__ == "__main__":
    unittest.main()
