"""A cover with no confident colour gets a colourless backdrop.

The UI accent keeps falling back to the theme (that was decided in 2.0);
the backdrop no longer borrows it, so a black-and-white sleeve sits behind
greys at its own brightness instead of the theme's blue.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_neutral_backdrop.py
"""
import colorsys
import sys
import unittest
from unittest import mock

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from tide import theming
from tide.ui import adaptive, central_bg


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


GREYS = [(QColor("#777777"), 1800), (QColor("#4c4c4c"), 1200), (QColor("#a0a0a0"), 400)]
DARK_GREYS = [(QColor("#141414"), 2000), (QColor("#2a2a2a"), 900)]
COLOURED = [(QColor("#366b45"), 1800), (QColor("#713b85"), 1400)]


def _sat(c: QColor) -> float:
    return colorsys.rgb_to_hls(c.redF(), c.greenF(), c.blueF())[2]


class DriverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("blackwater")

    def _driver(self, background: bool):
        d = adaptive.AdaptiveDriver.__new__(adaptive.AdaptiveDriver)
        d._background_enabled = background
        d._mini_active = False
        d._theme_demands_adaptive = lambda: False
        return d

    def _pushed(self, driver, palette) -> dict:
        with mock.patch.object(theming.manager(), "replace_dynamic_tokens") as rep:
            driver._apply_palette(palette)
            rep.assert_called_once()
            return dict(rep.call_args.args[0])

    def test_grey_cover_pushes_a_neutral_flag_and_no_accent(self) -> None:
        out = self._pushed(self._driver(background=True), GREYS)
        self.assertNotIn("accent", out)
        self.assertEqual(out.get("ambient_neutral"), "1")
        grey = QColor(out["ambient_bg"])
        self.assertLess(_sat(grey), 0.01)
        self.assertGreater(grey.lightnessF(), 0.3)
        darker = QColor(self._pushed(self._driver(background=True), DARK_GREYS)["ambient_bg"])
        self.assertLess(darker.lightnessF(), grey.lightnessF())

    def test_backdrop_off_keeps_the_empty_baseline(self) -> None:
        self.assertEqual(self._pushed(self._driver(background=False), GREYS), {})

    def test_coloured_cover_is_untouched(self) -> None:
        out = self._pushed(self._driver(background=True), COLOURED)
        self.assertIn("accent", out)
        self.assertNotIn("ambient_neutral", out)

    def test_neutral_tint_is_grey_at_the_mean_lightness(self) -> None:
        g = adaptive.neutral_tint([(QColor("#ffffff"), 1), (QColor("#000000"), 1)])
        self.assertEqual((g.red(), g.green(), g.blue()), (128, 128, 128))


class BackdropTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("blackwater")

    def _tones(self, extra: dict) -> list[QColor]:
        base = theming.manager().current()
        theme = theming.Theme(slug=base.slug, name=base.name, path=base.path,
                              tokens={**base.tokens, **extra}, typography=base.typography,
                              layout=base.layout, aesthetic=base.aesthetic, dark=base.dark)
        bg = central_bg.CentralBg(QWidget())
        bg.set_motion("off")
        bg._on_theme(theme)
        return [bg._tone_ta, bg._tone_tb, bg._tone_tc]

    def test_theme_baseline_glows_in_the_accent_and_neutral_does_not(self) -> None:
        baseline = self._tones({})
        self.assertGreater(max(_sat(c) for c in baseline), 0.2, "blackwater's blue")
        neutral = self._tones({"ambient_bg": "#777777", "ambient_neutral": "1"})
        self.assertLess(max(_sat(c) for c in neutral), 0.01)
        dark = self._tones({"ambient_bg": "#141414", "ambient_neutral": "1"})
        self.assertLess(dark[0].lightnessF(), neutral[0].lightnessF())
        self.assertGreater(dark[0].lightnessF(), 0.05)


if __name__ == "__main__":
    unittest.main()
