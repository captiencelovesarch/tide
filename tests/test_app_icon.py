"""The app icon drawn per theme (ui/app_icon.py) and its window hookup.

Every bundled theme renders at every icon size with no transparent hole
in the tile, modern and brutalist get their own cut, the classic palette
matches the shipped assets/icon.svg, and the window follows the theme or
stays classic per the ``app_icon`` setting.

The tray has its own cut: a one-colour glyph, because every other icon
in a panel tray is flat white (or flat dark on a light panel) and the
full-colour tile stood out. It's drawn per size on the pixel grid.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.settings import Settings
from tide.ui import app_icon


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class DrawingTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_every_bundled_theme_renders(self) -> None:
        for slug, theme in theming.discover_themes().items():
            svg = app_icon.svg_for(theme)
            for size in (16, 48, 256):
                with self.subTest(theme=slug, size=size):
                    img = app_icon.render(svg, size)
                    c = size // 2
                    # The middle of the tile is painted, never see-through.
                    self.assertEqual(img.pixelColor(c, c).alpha(), 255)

    def test_brutalist_themes_get_the_flat_cut(self) -> None:
        themes = theming.discover_themes()
        self.assertNotIn("Gradient", app_icon.svg_for(themes["gruvbox"]))
        self.assertNotIn('rx="', app_icon.svg_for(themes["gruvbox"]))
        self.assertIn("Gradient", app_icon.svg_for(themes["nord"]))

    def test_the_shipped_svg_is_the_classic_drawing(self) -> None:
        shipped = Path(__file__).resolve().parent.parent / "assets" / "icon.svg"
        self.assertEqual(shipped.read_text(encoding="utf-8"),
                         app_icon.classic_svg())

    def test_no_clip_paths(self) -> None:
        # Qt's (and so KDE's) SVG renderer ignores them.
        for theme in (None, *theming.discover_themes().values()):
            self.assertNotIn("clip", app_icon.svg_for(theme))


class TrayGlyphTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_one_colour_on_a_clear_ground_at_every_size(self) -> None:
        for ink in (app_icon.TRAY_INK_ON_DARK, app_icon.TRAY_INK_ON_LIGHT):
            want = QColor(ink)
            for size in app_icon.TRAY_SIZES:
                img = app_icon.tray_image(size, ink)
                self.assertEqual((img.width(), img.height()), (size, size))
                inked = 0
                for y in range(size):
                    for x in range(size):
                        c = img.pixelColor(x, y)
                        if c.alpha() == 0:
                            continue
                        inked += 1
                        # Antialiased edges fade out in alpha, never in hue.
                        if c.alpha() == 255:
                            self.assertEqual(c.rgb(), want.rgb(),
                                             f"{size}px at {x},{y}")
                self.assertGreater(inked, size * size // 8, f"{size}px")
                for x, y in ((0, 0), (size - 1, 0)):
                    self.assertEqual(img.pixelColor(x, y).alpha(), 0)

    def test_bars_sit_on_whole_pixels(self) -> None:
        # The reason it isn't an SVG: at tray sizes a scaled drawing's
        # bars smear across two half-lit rows. Every row through the
        # middle of the icon is either solid at the centre or empty.
        for size in (16, 22, 24):
            img = app_icon.tray_image(size, app_icon.TRAY_INK_ON_DARK)
            bottom_half = [img.pixelColor(size // 2, y).alpha()
                           for y in range(size * 2 // 3, size)]
            for a in bottom_half:
                self.assertIn(a, (0, 255), f"{size}px column {bottom_half}")
            self.assertGreaterEqual(bottom_half.count(255), 3)

    def test_ink_follows_the_system_light_or_dark(self) -> None:
        self.assertEqual(app_icon.tray_ink(Qt.ColorScheme.Light),
                         app_icon.TRAY_INK_ON_LIGHT)
        self.assertEqual(app_icon.tray_ink(Qt.ColorScheme.Dark),
                         app_icon.TRAY_INK_ON_DARK)
        # No platform theme to ask: most panels are dark.
        self.assertEqual(app_icon.tray_ink(Qt.ColorScheme.Unknown),
                         app_icon.TRAY_INK_ON_DARK)


class _FakeTray:
    def __init__(self) -> None:
        self.icons = []

    def set_icon(self, icon) -> None:
        self.icons.append(icon)


class WindowIconTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        from tide import settings as settings_module
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)

    def tearDown(self) -> None:
        from tide import settings as settings_module
        self.w.close()
        settings_module.save = self._real_save

    def test_follows_the_theme_or_stays_classic(self) -> None:
        self.w._settings = Settings(app_icon="theme")
        self.w._app_icon_svg = None
        self.w.apply_app_icon_setting()
        self.assertEqual(self.w._app_icon_svg,
                         app_icon.svg_for(theming.manager().current()))
        self.assertFalse(QApplication.windowIcon().isNull())
        self.w._settings.app_icon = "classic"
        self.w.apply_app_icon_setting()
        self.assertEqual(self.w._app_icon_svg, app_icon.classic_svg())

    def test_tray_gets_the_glyph_or_the_app_icon(self) -> None:
        tray = _FakeTray()
        self.w._tray = tray
        self.w._tray_icon_key = None
        self.w._settings = Settings(tray_icon="mono")
        self.w.apply_app_icon_setting()
        self.assertEqual(self.w._tray_icon_key,
                         ("mono", app_icon.tray_ink()))
        self.assertEqual(len(tray.icons), 1)
        # Per-song theme_changed re-runs it: nothing to redo.
        self.w.apply_app_icon_setting()
        self.assertEqual(len(tray.icons), 1)
        self.w._settings.tray_icon = "app"
        self.w.apply_app_icon_setting()
        self.assertEqual(self.w._tray_icon_key[0], "app")
        self.assertEqual(len(tray.icons), 2)
        self.assertEqual(tray.icons[-1].cacheKey(),
                         QApplication.windowIcon().cacheKey())
        self.w._tray = None


if __name__ == "__main__":
    unittest.main()
