"""2.3 regression: a cover change never shows the NEXT cover before its
transition has started.

The flash: a song change restyles the app (the adaptive accent reads the
new cover), AlbumArt._apply_theme re-rendered ``_pixmap_raw`` — already
the new cover — straight onto the label, and only then did the flip's
first frame land.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_art_flash.py
"""
import sys
import unittest

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import theming
from tide.ui import art_fx, motion
from tide.ui.variants import make_album_art

RED, BLUE = QColor("#ff0000"), QColor("#0000ff")


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _cover(color: QColor) -> QImage:
    img = QImage(64, 64, QImage.Format_ARGB32)
    img.fill(color)
    return img


def _centre(pix: QPixmap) -> QColor:
    img = pix.toImage()
    return img.pixelColor(img.width() // 2, img.height() // 2)


def _is(c: QColor, want: QColor) -> bool:
    return abs(c.red() - want.red()) < 40 and abs(c.blue() - want.blue()) < 40


class FirstFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        theming.manager().refresh()
        theming.manager().apply("brutalist-mono")
        self._style = art_fx.style()
        self._motion = motion._user_intensity
        motion.set_intensity(motion.Intensity.FULL)
        self.addCleanup(motion.set_intensity, self._motion)
        self.addCleanup(art_fx.set_style, self._style)

    def _tile(self, slug: str):
        art = make_album_art(slug, 96)
        art.setImage(_cover(RED))            # first load snaps
        self.assertTrue(_is(_centre(art.pixmap()), RED))
        self.addCleanup(art.deleteLater)
        return art

    def test_every_style_starts_on_the_old_cover(self) -> None:
        old = QPixmap.fromImage(_cover(RED))
        new = QPixmap.fromImage(_cover(BLUE))
        for name in ("flip", "slide", "pop", "blocks", "fade"):
            with self.subTest(style=name):
                self.assertTrue(_is(_centre(art_fx.frame(name, old, new, 0.0)), RED))

    def test_restyle_mid_change_does_not_flash_the_next_cover(self) -> None:
        for slug in ("square", "circle", "polaroid"):
            for name in ("flip", "slide", "pop", "blocks", "fade"):
                with self.subTest(tile=slug, style=name):
                    art_fx.set_style(name)
                    art = self._tile(slug)
                    art.setImage(_cover(BLUE))
                    self.assertTrue(art_fx.running(art))
                    # the song change's restyle lands before the first frame
                    theming.manager().theme_changed.emit(
                        theming.manager().current_effective())
                    self.assertTrue(_is(_centre(art.pixmap()), RED),
                                    "the next cover painted before the transition")
                    art_fx.cancel(art)

    def test_the_transition_still_lands_on_the_new_cover(self) -> None:
        art_fx.set_style("flip")
        art = self._tile("square")
        art.setImage(_cover(BLUE))
        theming.manager().theme_changed.emit(theming.manager().current_effective())
        QTest.qWait(art_fx.duration("flip") + 300)
        self.assertFalse(art_fx.running(art))
        self.assertTrue(_is(_centre(art.pixmap()), BLUE))


if __name__ == "__main__":
    unittest.main()
