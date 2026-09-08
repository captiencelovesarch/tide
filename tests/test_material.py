"""Material tokens derive from what a theme already declares.

Surface tiers and the outline come from ``bg``'s polarity as translucent
overlays; the type scale comes from the theme's point size through the UI
scale. A theme that declares any of them wins. Every bundled theme must
resolve them, and the brutalist stylesheet must not reference them at all
(the material is the modern personality's; brutalist is byte-stable).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_material.py
"""
import re
import sys
import unittest
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from tide import material, theming
from tide.ui import scale


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _theme(tokens: dict, *, dark: bool = True, aesthetic: str = "modern") -> theming.Theme:
    return theming.Theme(slug="t", name="t", path=Path("."), tokens=dict(tokens),
                         typography={"size_pt": 10}, layout={},
                         aesthetic=aesthetic, dark=dark, qss="")


class SurfaceDerivationTest(unittest.TestCase):
    def test_dark_ground_gets_white_veils_light_ground_black(self) -> None:
        dark = material.surface_tokens("#000000")
        light = material.surface_tokens("#f4efe6")
        for key in material.SURFACE_KEYS:
            self.assertTrue(dark[key].endswith("ffffff"), key)
            self.assertTrue(light[key].endswith("000000"), key)

    def test_tiers_rise(self) -> None:
        for bg in ("#000000", "#2e3440", "#f4efe6"):
            t = material.surface_tokens(bg)
            a = [QColor(t[k]).alpha() for k in ("surface_1", "surface_2", "surface_3")]
            self.assertEqual(a, sorted(a), bg)
            self.assertLess(a[0], a[2], bg)
            self.assertGreater(a[0], 0, bg)

    def test_values_parse_for_both_consumers(self) -> None:
        # QSS takes #aarrggbb and so does QColor(str): one value, two homes.
        for value in material.surface_tokens("#0a0a0a").values():
            self.assertRegex(value, r"^#[0-9a-f]{8}$")
            c = QColor(value)
            self.assertTrue(c.isValid())
            self.assertLess(c.alpha(), 255)

    def test_polarity_reads_bg_and_falls_back_to_the_flag(self) -> None:
        self.assertTrue(material.is_dark("#000000", fallback=False))
        self.assertFalse(material.is_dark("#ffffff", fallback=True))
        self.assertTrue(material.is_dark("not a colour", fallback=True))
        self.assertFalse(material.is_dark("", fallback=False))

    def test_declared_token_wins_over_derivation(self) -> None:
        theme = _theme({"bg": "#000000", "surface_1": "#123456"})
        self.assertEqual(theme.token("surface_1"), "#123456")
        self.assertTrue(theme.token("surface_2").endswith("ffffff"))
        self.assertEqual(material.derived(theme.tokens).keys(),
                         {"surface_2", "surface_3", "outline"})

    def test_unknown_token_still_returns_default(self) -> None:
        theme = _theme({"bg": "#000000"})
        self.assertEqual(theme.token("no_such_token", "fallback"), "fallback")


class SubstitutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        cls._saved = scale.current()
        scale.set_factor(scale.Scale.NORMAL)
        cls.themes = {}
        for child in sorted(theming.BUNDLED_THEMES_DIR.iterdir()):
            if child.is_dir():
                t = theming._read_theme(child)
                if t is not None:
                    cls.themes[t.slug] = t

    @classmethod
    def tearDownClass(cls) -> None:
        scale.set_factor(cls._saved)

    def test_every_bundled_theme_resolves_the_material_tokens(self) -> None:
        leftover = re.compile(r"@(surface_[123]|outline|font_size_(title|display)|weight_(title|display)|radius_round)\b")
        for slug, theme in self.themes.items():
            with self.subTest(theme=slug):
                out = theming._substitute(theme.qss, theme)
                self.assertIsNone(leftover.search(out))

    def test_modern_sheets_use_the_material_and_brutalist_ones_do_not(self) -> None:
        for slug, theme in self.themes.items():
            with self.subTest(theme=slug):
                uses = "surface_1" in theme.qss or "@surface_1" in theme.qss
                # composed qss still has @tokens; look for the raw reference
                has_ref = "@surface_1" in theme.qss
                if theme.aesthetic == "modern":
                    self.assertTrue(has_ref, "modern theme without material")
                else:
                    self.assertFalse(has_ref, "brutalist theme grew material")

    def test_type_scale_rides_ui_scale(self) -> None:
        theme = _theme({"bg": "#000000"})
        qss = "QLabel.title { font-size: @font_size_title; } QLabel.display { font-size: @font_size_display; font-weight: @weight_display; }"
        normal = theming._substitute(qss, theme)
        scale.set_factor(scale.Scale.HUGE)
        try:
            huge = theming._substitute(qss, theme)
        finally:
            scale.set_factor(scale.Scale.NORMAL)
        self.assertIn("font-size: 12pt", normal)      # 10 × 1.25, rounded
        self.assertIn("font-size: 17pt", normal)      # 10 × 1.7
        self.assertIn(f"font-weight: {material.WEIGHT_DISPLAY}", normal)
        self.assertNotEqual(normal, huge)
        self.assertGreater(material.title_pt(10), 10)

    def test_declared_material_reaches_the_sheet(self) -> None:
        theme = _theme({"bg": "#000000", "outline": "#ff0000"})
        out = theming._substitute("QFrame { border: 1px solid @outline; color: @surface_1; }", theme)
        self.assertIn("#ff0000", out)
        self.assertNotIn("@surface_1", out)


if __name__ == "__main__":
    unittest.main()
