"""The karaoke sweep line, rendered offscreen: the sung part of a word
is in the accent and the rest in the body colour, in both
personalities; plus the play clock that carries it between position
reports."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tide.lyric_words import Word, WordLine  # noqa: E402
from tide.ui import karaoke_line  # noqa: E402
from tide.ui.karaoke_line import PlayClock, SweepLine  # noqa: E402

_app = QApplication.instance() or QApplication([])

FG = QColor("#00ff00")
ACCENT = QColor("#ff0000")


def _share(img: QImage, x0: int, x1: int) -> tuple[int, int]:
    """(accent-ish pixels, fg-ish pixels) in columns [x0, x1)."""
    red = green = 0
    for x in range(max(0, x0), min(img.width(), x1)):
        for y in range(img.height()):
            c = img.pixelColor(x, y)
            if c.alpha() < 128:
                continue
            if c.red() > 150 and c.green() < 100:
                red += 1
            elif c.green() > 150 and c.red() < 100:
                green += 1
    return red, green


class SweepRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        karaoke_line.set_style("sweep")
        self.addCleanup(karaoke_line.set_style, "sweep")
        self.w = SweepLine()
        self.w.resize(600, 120)
        line = WordLine(0.0, 3.0, "MMMMMMMM WWWW",
                        (Word(1.0, 2.0, "MMMMMMMM"), Word(2.0, 3.0, "WWWW")), "source")
        self.w.set_line(line)
        self.at = 0.0
        self.w.clock.now = lambda wall=None: self.at

    def _render(self, modern: bool, t: float) -> QImage:
        self.w.set_look(fg=FG, accent=ACCENT, modern=modern, pt=28)
        self.at = t
        return self.w.grab().toImage()

    def _word0_span(self) -> tuple[int, int]:
        self.w._relayout(float(self.w.width()))
        _i, x, ww = self.w._rows[0][0]
        return int(x), int(x + ww)

    def _check_half_sung(self, modern: bool) -> None:
        img = self._render(modern, 1.5)
        a, b = self._word0_span()
        mid = (a + b) // 2
        left_red, left_green = _share(img, a, mid - 12)
        right_red, right_green = _share(img, mid + 12, b)
        self.assertGreater(left_red, 5 * max(1, left_green))
        self.assertGreater(right_green, 5 * max(1, right_red))

    def test_modern_sweeps_through_the_word(self) -> None:
        self._check_half_sung(modern=True)

    def test_brutalist_sweeps_by_letters(self) -> None:
        self._check_half_sung(modern=False)

    def test_before_and_after(self) -> None:
        a, b = self._word0_span()
        img = self._render(True, 0.5)
        red, green = _share(img, a, b)
        self.assertEqual(red, 0)
        self.assertGreater(green, 0)
        img = self._render(True, 2.5)
        red, green = _share(img, a, b)
        self.assertGreater(red, 0)
        self.assertEqual(green, 0)

    def test_word_style_lights_whole_words(self) -> None:
        karaoke_line.set_style("word")
        img = self._render(True, 1.1)
        a, b = self._word0_span()
        red, green = _share(img, a, b)
        self.assertGreater(red, 0)
        self.assertEqual(green, 0)

    def test_brutalist_edge_sits_on_a_letter_boundary(self) -> None:
        # 8 letters, 40% through: exactly 3 letters lit, the 4th not yet
        self.w.set_look(fg=FG, accent=ACCENT, modern=False, pt=28)
        self.at = 1.4
        self.assertEqual(self.w._progress(self.at), (0, self.w._progress(self.at)[1]))
        img = self.w.grab().toImage()
        a, b = self._word0_span()
        per = (b - a) / 8.0
        red, green = _share(img, int(a + 3 * per + 3), int(a + 4 * per - 3))
        self.assertEqual(red, 0)
        red, green = _share(img, int(a + 2 * per + 3), int(a + 3 * per - 3))
        self.assertEqual(green, 0)

    def test_wraps_to_the_width(self) -> None:
        self.w.resize(120, 120)
        self.w._relayout(120.0)
        self.assertGreater(len(self.w._rows), 1)
        self.assertGreater(self.w.heightForWidth(120), self.w.heightForWidth(600))


class PlayClockTest(unittest.TestCase):
    def test_coasts_between_reports(self) -> None:
        c = PlayClock()
        c.report(10.0, wall=100.0)
        c.report(10.25, wall=100.25)
        self.assertAlmostEqual(c.now(wall=100.35), 10.35, places=3)
        self.assertTrue(c.running(wall=100.35))

    def test_coast_is_bounded_and_silence_stops_it(self) -> None:
        c = PlayClock()
        c.report(10.0, wall=100.0)
        c.report(10.25, wall=100.25)
        self.assertAlmostEqual(c.now(wall=105.0), 10.25 + karaoke_line.MAX_COAST_S, places=3)
        self.assertFalse(c.running(wall=105.0))

    def test_follows_playback_speed(self) -> None:
        c = PlayClock()
        c.report(0.0, wall=0.0)
        for i in range(1, 8):
            c.report(0.5 * i, wall=0.25 * i)       # 2x speed
        self.assertAlmostEqual(c.rate, 2.0, delta=0.05)

    def test_pause_in_place(self) -> None:
        c = PlayClock()
        c.report(5.0, wall=0.0)
        c.report(5.0, wall=0.3)
        self.assertFalse(c.running(wall=0.35))


if __name__ == "__main__":
    unittest.main()
