"""The app icon drawn per theme (ui/app_icon.py) and its window hookup.

Every bundled theme renders at every icon size with no transparent hole
in the tile, modern and brutalist get their own cut, the classic palette
matches the shipped assets/icon.svg, and the window follows the theme or
stays classic per the ``app_icon`` setting.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
