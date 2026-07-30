"""Every shipped theme must be complete and internally consistent.

Guards the v1.3 six (abyss, blackwater, golden-hour, seaglass, storm,
undertow) and everything that was already here: tokens resolve to real
colors, the QSS substitutes fully, and every declarative hook (visualizer,
slots, control style, case) names something that actually exists.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import re
import sys
import unittest

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.ui import variants
from tide.ui.visualizer import renderer_slugs

REQUIRED_TOKENS = (
    "bg", "bg_alt", "fg", "dim", "accent",
    "sel_bg", "sel_fg", "hover_bg", "hover_fg",
    "border_col", "border_dim",
)
VALID_CASES = {"lower", "upper", "normal", "leet", "zalgo"}
VALID_CONTROL_STYLES = {"bracket", "glyph", "icon"}
NEW_IN_13 = {"abyss", "blackwater", "golden-hour", "storm", "undertow"}


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class ThemeValidityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        cls.themes = theming.discover_themes()

    def test_the_v13_six_are_discovered(self) -> None:
        self.assertLessEqual(NEW_IN_13, set(self.themes),
                             "a v1.3 theme directory failed discovery")

    def test_required_tokens_present_and_parseable(self) -> None:
        for slug, theme in self.themes.items():
            if slug == "adaptive":
                continue    # adaptive derives its palette at runtime
            with self.subTest(theme=slug):
                for token in REQUIRED_TOKENS:
                    value = theme.tokens.get(token)
                    self.assertTrue(value, f"{slug} is missing token '{token}'")
                    self.assertTrue(
                        QColor(value).isValid(),
                        f"{slug}.{token} = {value!r} is not a color QColor "
                        "can parse (custom painters construct QColor(token))",
                    )

    def test_qss_substitutes_completely(self) -> None:
        leftover = re.compile(r"@[a-z_]+")
        for slug, theme in self.themes.items():
            with self.subTest(theme=slug):
                qss = theming._substitute(theme.qss, theme)
                found = sorted(set(leftover.findall(qss)))
                self.assertEqual(
                    found, [],
                    f"{slug}: unresolved tokens {found} would render literally",
                )

    def test_visualizer_slugs_exist(self) -> None:
        known = set(renderer_slugs())
        for slug, theme in self.themes.items():
            with self.subTest(theme=slug):
                vis = str(theme.t("layout", "visualizer", "bars-mono"))
                self.assertIn(vis, known,
                              f"{slug} names unknown visualizer {vis!r}")

    def test_slot_values_exist(self) -> None:
        valid = {
            "progress": set(variants.PROGRESS_VARIANTS),
            "volume": set(variants.VOLUME_VARIANTS),
            "album_art": set(variants.ALBUM_ART_VARIANTS),
            "controls": set(variants.CONTROLS_VARIANTS),
            "now_label": set(variants.NOW_LABEL_VARIANTS),
        }
        for slug, theme in self.themes.items():
            for slot, value in (theme.slots or {}).items():
                with self.subTest(theme=slug, slot=slot):
                    self.assertIn(slot, valid, f"{slug} has unknown slot {slot!r}")
                    self.assertIn(
                        value, valid[slot],
                        f"{slug}.slots.{slot} = {value!r} is not a variant",
                    )

    def test_typography_hooks_are_valid(self) -> None:
        for slug, theme in self.themes.items():
            with self.subTest(theme=slug):
                case = str(theme.t("typography", "case", "lower"))
                self.assertIn(case, VALID_CASES)
                style = str(theme.t("layout", "control_style", "bracket"))
                self.assertIn(style, VALID_CONTROL_STYLES)

    def test_translucent_themes_use_alpha_backgrounds(self) -> None:
        """window_translucent without alpha in bg would just look opaque —
        and alpha without the flag would composite against garbage."""
        for slug, theme in self.themes.items():
            if slug == "adaptive":
                continue
            with self.subTest(theme=slug):
                flag = bool(theme.t("layout", "window_translucent", False))
                bg_alpha = QColor(theme.tokens.get("bg", "#000000")).alpha()
                if flag:
                    self.assertLess(bg_alpha, 255,
                                    f"{slug} is window_translucent but bg is opaque")
                else:
                    self.assertEqual(bg_alpha, 255,
                                     f"{slug} has an alpha bg without the flag")


if __name__ == "__main__":
    unittest.main()
