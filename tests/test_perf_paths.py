"""The optimization pass's guarantees (2026-10-07).

tide sat near 850 MB and most of a core while playing. These pin the
fixes so a later change can't quietly undo them:

  * the art cache is bounded in bytes, not just entries, and painters get
    display-size pixmaps that are scaled once, not per paint;
  * the backdrop renders a frame per tick and reuses it for every child
    repaint in between;
  * progress bars repaint only when something they draw moves.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_perf_paths.py
"""
import sys
import unittest

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _cover(side: int = 544) -> QImage:
    img = QImage(side, side, QImage.Format_RGB32)
    img.fill(0xFF803020)
    return img


class ArtCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        from tide.ui import art_cache
        self.mod = art_cache
        self.cache = art_cache._ArtCache()

    def test_full_size_lru_is_bounded_by_bytes(self) -> None:
        for i in range(200):
            self.cache._store(f"https://x/{i}.jpg", _cover())
        self.assertLessEqual(self.cache._mem_bytes, self.mod.MEM_BYTES_LIMIT)
        self.assertEqual(self.cache._mem_bytes,
                         sum(i.sizeInBytes() for i in self.cache._mem.values()))
        # the newest covers are the ones kept
        self.assertIn("https://x/199.jpg", self.cache._mem)
        self.assertNotIn("https://x/0.jpg", self.cache._mem)

    def test_scaled_is_made_once_and_reused(self) -> None:
        self.cache._store("https://x/a.jpg", _cover())
        first = self.cache.scaled("https://x/a.jpg", 40, 40)
        self.assertIsNotNone(first)
        self.assertEqual((first.width(), first.height()), (40, 40))
        again = self.cache.scaled("https://x/a.jpg", 40, 40)
        self.assertEqual(first.cacheKey(), again.cacheKey())

    def test_scaled_honours_device_pixel_ratio(self) -> None:
        self.cache._store("https://x/a.jpg", _cover())
        pix = self.cache.scaled("https://x/a.jpg", 40, 40, 2.0)
        self.assertEqual((pix.width(), pix.height()), (80, 80))
        self.assertEqual(pix.devicePixelRatio(), 2.0)

    def test_scaled_pixmaps_are_bounded(self) -> None:
        self.cache._store("https://x/a.jpg", _cover())
        for side in range(40, 400):
            self.cache.scaled("https://x/a.jpg", side, side)
        self.assertLessEqual(self.cache._pix_bytes, self.mod.PIX_BYTES_LIMIT)

    def test_unknown_url_is_none(self) -> None:
        self.assertIsNone(self.cache.scaled("https://x/missing.jpg", 40, 40))
        self.assertIsNone(self.cache.scaled("", 40, 40))


class BackdropFrameReuseTests(unittest.TestCase):
    def test_child_repaints_reuse_the_frame(self) -> None:
        _app()
        from tide.ui.central_bg import CentralBg

        child = QWidget()
        bg = CentralBg(child)
        bg.resize(640, 400)
        bg.set_enabled(True)
        bg.set_style("horizon")
        bg.set_motion("full")
        bg.show()
        QApplication.processEvents()
        renders = []
        real = bg._render_buffer
        bg._render_buffer = lambda w, h: renders.append((w, h)) or real(w, h)
        bg._invalidate()
        bg.repaint()
        self.assertEqual(len(renders), 1)
        # a child repainting a corner of itself doesn't re-render the scene
        for _ in range(5):
            child.repaint(0, 0, 50, 20)
        self.assertEqual(len(renders), 1)
        # a tick (the scene moved) does
        bg._tick()
        bg.repaint()
        self.assertEqual(len(renders), 2)
        bg.hide()


class ProgressRepaintTests(unittest.TestCase):
    def _count_updates(self, widget) -> list:
        calls = []
        widget.update = lambda *a: calls.append(a)
        return calls

    def test_bars_update_only_when_the_drawing_moves(self) -> None:
        _app()
        from tide.ui.variants import BarProgress, DottedProgress, ThinProgress
        from tide.ui.widgets import MonoProgress

        for cls in (BarProgress, ThinProgress, DottedProgress, MonoProgress):
            w = cls()
            w.resize(300, w.height())
            w.setDuration(300.0)
            calls = self._count_updates(w)
            # 0.05 s steps across a single second: well under one pixel or
            # one cell of a 300 s song on a 300 px bar
            for i in range(20):
                w.setPosition(10.0 + i * 0.05)
            self.assertLessEqual(len(calls), 2, cls.__name__)
            calls.clear()
            w.setPosition(200.0)
            self.assertEqual(len(calls), 1, cls.__name__)


if __name__ == "__main__":
    unittest.main()
