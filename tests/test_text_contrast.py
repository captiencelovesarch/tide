"""fg/dim must stay readable on the surface they land on.

The reported bug: "grey text on a bright background becomes unreadable".
The greys are per-theme, and the light themes are where it bites —
solarized-light's dim sits at 2.5:1 on its own bg, which is below the
threshold at which text stops being text.

``tide.contrast`` derives corrected inks from a token table; the theming
manager applies them as a derived layer UNDER the dynamic and user
override layers, so a colour someone picked by hand in the theme editor
is never silently rewritten. These tests pin both halves: the maths, and
the layering.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_text_contrast.py
"""
import sys
import unittest

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from tide import contrast, theming


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class RatioTests(unittest.TestCase):
    """The WCAG maths, against values the standard pins exactly."""

    def test_black_on_white_is_the_maximum(self) -> None:
        ratio = contrast.contrast_ratio(QColor("#000000"), QColor("#ffffff"))
        self.assertAlmostEqual(ratio, 21.0, places=2)

    def test_a_colour_against_itself_is_one(self) -> None:
        self.assertAlmostEqual(
            contrast.contrast_ratio(QColor("#3b4252"), QColor("#3b4252")),
            1.0, places=6)

    def test_ratio_is_symmetric(self) -> None:
        a, b = QColor("#586e75"), QColor("#fdf6e3")
        self.assertAlmostEqual(contrast.contrast_ratio(a, b),
                               contrast.contrast_ratio(b, a), places=9)

    def test_luminance_is_linearized_not_naive(self) -> None:
        # Mid grey is ~0.216 in linear light, not 0.5. Getting this wrong
        # is what makes naive contrast checks pass unreadable pairs.
        self.assertAlmostEqual(
            contrast.relative_luminance(QColor("#808080")), 0.2159, places=3)


class ReadableInkTests(unittest.TestCase):
    def test_passing_ink_is_returned_untouched(self) -> None:
        ink, bg = QColor("#e6e6e6"), QColor("#0b0b0b")
        out = contrast.readable_ink(ink, bg, contrast.MIN_FG_CONTRAST)
        self.assertEqual(out.name(), ink.name())

    def test_failing_ink_is_lifted_to_the_floor(self) -> None:
        ink, bg = QColor("#93a1a1"), QColor("#fdf6e3")   # solarized-light dim
        self.assertLess(contrast.contrast_ratio(ink, bg), 3.0)
        out = contrast.readable_ink(ink, bg, 3.0)
        self.assertGreaterEqual(contrast.contrast_ratio(out, bg), 3.0)

    def test_correction_is_minimal_not_a_slam_to_the_pole(self) -> None:
        # The point of bisecting is keeping the theme's hue: a dim that
        # needs a small push must not come back as pure black.
        ink, bg = QColor("#a3866a"), QColor("#fff8ec")   # golden-hour
        out = contrast.readable_ink(ink, bg, 3.0)
        self.assertNotEqual(out.name(), "#000000")
        self.assertLess(contrast.contrast_ratio(out, bg), 4.2)
        # ...and it stays recognisably warm.
        self.assertGreater(out.red(), out.blue())

    def test_dark_surface_pushes_toward_white(self) -> None:
        out = contrast.readable_ink(QColor("#555555"), QColor("#0b0b0b"), 4.5)
        self.assertGreater(contrast.relative_luminance(out),
                           contrast.relative_luminance(QColor("#555555")))

    def test_light_surface_pushes_toward_black(self) -> None:
        out = contrast.readable_ink(QColor("#aaaaaa"), QColor("#ffffff"), 4.5)
        self.assertLess(contrast.relative_luminance(out),
                        contrast.relative_luminance(QColor("#aaaaaa")))

    def test_mid_surface_returns_the_best_available(self) -> None:
        # No ink clears 4.5:1 on a mid grey. Returning the better pole
        # beats returning something worse than we started with.
        out = contrast.readable_ink(QColor("#7f7f7f"), QColor("#7f7f7f"), 4.5)
        self.assertIn(out.name(), ("#000000", "#ffffff"))


class TokenTableTests(unittest.TestCase):
    def test_readable_palette_is_passed_through_empty(self) -> None:
        tokens = {"bg": "#0b0b0b", "bg_alt": "#141414",
                  "fg": "#e6e6e6", "dim": "#8a8a8a"}
        self.assertEqual(contrast.readable_text_tokens(tokens), {})

    def test_only_the_failing_key_is_returned(self) -> None:
        tokens = {"bg": "#0b0b0b", "bg_alt": "#141414",
                  "fg": "#e6e6e6", "dim": "#2b2b2b"}
        out = contrast.readable_text_tokens(tokens)
        self.assertEqual(set(out), {"dim"})

    def test_missing_surfaces_are_a_no_op(self) -> None:
        self.assertEqual(
            contrast.readable_text_tokens({"fg": "#111", "dim": "#222"}), {})

    def test_garbage_values_do_not_raise(self) -> None:
        contrast.readable_text_tokens(
            {"bg": "not-a-colour", "fg": "", "dim": None})

    def test_every_bundled_theme_ends_up_above_its_floor(self) -> None:
        for child in sorted(theming.BUNDLED_THEMES_DIR.iterdir()):
            if not child.is_dir():
                continue
            theme = theming._read_theme(child)
            if theme is None:
                continue
            with self.subTest(theme=theme.slug):
                fixed = dict(theme.tokens)
                fixed.update(contrast.readable_text_tokens(theme.tokens))
                for surface in ("bg", "bg_alt"):
                    bg = QColor(fixed.get(surface, ""))
                    if not bg.isValid():
                        continue
                    for key, floor in (("fg", contrast.MIN_FG_CONTRAST),
                                       ("dim", contrast.MIN_DIM_CONTRAST)):
                        ink = QColor(fixed.get(key, ""))
                        if not ink.isValid():
                            continue
                        self.assertGreaterEqual(
                            contrast.contrast_ratio(ink, bg), floor - 0.01,
                            f"{theme.slug}: {key} on {surface}")


class LayeringTests(unittest.TestCase):
    """Where the derived layer sits in the override stack."""

    def setUp(self) -> None:
        self.app = _app()
        self.mgr = theming.manager()
        self.addCleanup(self.mgr.set_text_contrast, self.mgr.text_contrast())
        self.addCleanup(self.mgr.set_user_override, "dim", None)
        self.mgr.set_text_contrast(False)
        self.mgr.apply("solarized-light")

    def test_off_leaves_the_theme_exactly_as_authored(self) -> None:
        self.assertEqual(self.mgr.current_effective().token("dim"), "#93a1a1")

    def test_on_corrects_the_theme_greys(self) -> None:
        self.mgr.set_text_contrast(True)
        dim = self.mgr.current_effective().token("dim")
        self.assertNotEqual(dim, "#93a1a1")
        self.assertGreaterEqual(
            contrast.contrast_ratio(QColor(dim), QColor("#fdf6e3")),
            contrast.MIN_DIM_CONTRAST - 0.01)

    def test_an_explicit_user_pick_beats_the_correction(self) -> None:
        # The theme editor writes exactly these tokens. Second-guessing a
        # colour someone just chose would read as the editor being broken.
        self.mgr.set_text_contrast(True)
        self.mgr.set_user_override("dim", "#93a1a1")
        self.assertEqual(self.mgr.current_effective().token("dim"), "#93a1a1")

    def test_turning_it_back_off_restores_the_theme(self) -> None:
        self.mgr.set_text_contrast(True)
        self.mgr.set_text_contrast(False)
        self.assertEqual(self.mgr.current_effective().token("dim"), "#93a1a1")

    def test_repeat_sets_are_cheap_no_ops(self) -> None:
        self.mgr.set_text_contrast(True)
        first = self.mgr.current_effective().token("dim")
        self.mgr.set_text_contrast(True)
        self.assertEqual(self.mgr.current_effective().token("dim"), first)

    def test_the_derived_layer_is_cached_across_repeat_reads(self) -> None:
        # current_effective() is a paint-time call; it must not redo the
        # bisection on every frame.
        self.mgr.set_text_contrast(True)
        self.mgr.current_effective()
        key = self.mgr._readable_key
        cache = self.mgr._readable_cache
        for _ in range(50):
            self.mgr.current_effective()
        self.assertIs(self.mgr._readable_cache, cache)
        self.assertEqual(self.mgr._readable_key, key)


if __name__ == "__main__":
    unittest.main()
