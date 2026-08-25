"""The adaptive backdrop must never wear the previous song's colors.

The reported bug: skip to a new song and the backdrop sometimes keeps the
old palette. Three causes, all pinned here:
  * results from a superseded track change landed anyway (no generation
    guard, only URL equality),
  * failures (dead art fetch, empty palette) returned without touching
    the override, so the old colors stayed for the whole song,
  * overrides were merged per-key, so a cover that produced only some
    keys kept the previous cover's remaining keys.

Plus the fix's second half: CentralBg crossfades tones instead of
snapping, and snaps when motion is off.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from tide import theming
from tide.sources.base import Track
from tide.ui import adaptive as adaptive_module
from tide.ui.adaptive import AdaptiveDriver
from tide.ui.central_bg import CentralBg, _lerp_color


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _FakeQueue(QObject):
    current_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.current = None


class _FakeArtCache:
    """request() never answers synchronously; the test delivers callbacks."""

    def __init__(self) -> None:
        self.pending: list = []

    def request(self, url, callback=None):
        self.pending.append((url, callback))
        return None


class _StubWorker:
    class _Done:
        def disconnect(self):
            pass

    class _Sig:
        def __init__(self) -> None:
            self.done = _StubWorker._Done()

    def __init__(self) -> None:
        self.signals = self._Sig()


def _mgr():
    return theming.manager()


def _drain_pending_restyles() -> None:
    """Token pushes schedule a deferred restyle on the GLOBAL manager; left
    queued, it fires inside whichever test runs next (the restyle-coalesce
    assertions see a stylesheet write they didn't cause). Flush it here."""
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
        app.processEvents()


class DriverStaleTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        if _mgr().current() is None:
            _mgr().apply("brutalist-mono")
        self.queue = _FakeQueue()
        self.fake_cache = _FakeArtCache()
        self.enterContext(mock.patch.object(
            adaptive_module.art_cache, "cache", lambda: self.fake_cache))
        self.driver = AdaptiveDriver(self.queue)
        self.driver._background_enabled = True   # enabled without re-fire
        self.driver._enabled = True
        # Seed "previous song" colors into the dynamic layer.
        _mgr().replace_dynamic_tokens({
            "accent": "#ff0000", "accent_alt": "#aa0000",
            "ambient_bg": "#330000",
        })
        # LIFO: clear runs first, then the drain flushes its queued restyle.
        self.addCleanup(_drain_pending_restyles)
        self.addCleanup(lambda: _mgr().replace_dynamic_tokens({}))

    def _colorful_palette(self) -> list:
        return [(QColor("#2244cc"), 3000), (QColor("#3366dd"), 800)]

    def test_failed_art_fetch_clears_previous_song(self) -> None:
        self.queue.current = Track(video_id="b", title="b", artists="b",
                                   thumbnail="https://art/b.jpg")
        self.driver._on_track_changed(self.queue.current)
        url, cb = self.fake_cache.pending[-1]
        cb(None)      # fetch failed for the CURRENT track
        self.assertEqual(_mgr()._dynamic_overrides, {},
                         "dead art fetch must clear the old palette")

    def test_stale_generation_is_discarded(self) -> None:
        # Track B starts, then track C starts before B's palette lands.
        self.driver._on_track_changed(Track(video_id="b", title="b",
                                            artists="b", thumbnail="https://art/b.jpg"))
        stale_gen = self.driver._gen
        self.driver._on_track_changed(Track(video_id="c", title="c",
                                            artists="c", thumbnail="https://art/c.jpg"))
        before = dict(_mgr()._dynamic_overrides)
        self.driver._on_palette_done_from_worker(
            _StubWorker(), stale_gen, self._colorful_palette())
        self.assertEqual(_mgr()._dynamic_overrides, before,
                         "a palette for a superseded track must not apply")

    def test_empty_palette_clears_previous_song(self) -> None:
        self.driver._on_track_changed(Track(video_id="b", title="b",
                                            artists="b", thumbnail="https://art/b.jpg"))
        self.driver._on_palette_done_from_worker(_StubWorker(),
                                                 self.driver._gen, [])
        self.assertEqual(_mgr()._dynamic_overrides, {})

    def test_replace_never_merges_with_previous_song(self) -> None:
        # A grayscale cover with a usable body tint: no accent is picked.
        # Under the old merge semantics the red accent from setUp survived.
        gray_with_tint = [(QColor("#303038"), 4000)]
        self.driver._on_track_changed(Track(video_id="b", title="b",
                                            artists="b", thumbnail="https://art/b.jpg"))
        self.driver._on_palette_done_from_worker(
            _StubWorker(), self.driver._gen, gray_with_tint)
        self.assertNotIn("accent", _mgr()._dynamic_overrides,
                         "previous song's accent leaked through a merge")

    def test_fresh_palette_replaces_wholesale(self) -> None:
        self.driver._on_track_changed(Track(video_id="b", title="b",
                                            artists="b", thumbnail="https://art/b.jpg"))
        self.driver._on_palette_done_from_worker(
            _StubWorker(), self.driver._gen, self._colorful_palette())
        got = _mgr()._dynamic_overrides
        self.assertIn("accent", got)
        self.assertNotEqual(got["accent"], "#ff0000")


class ToneCrossfadeTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        if _mgr().current() is None:
            _mgr().apply("brutalist-mono")
        _mgr().replace_dynamic_tokens({})
        self.bg = CentralBg(QWidget())
        self.bg.set_enabled(True)
        self.bg.set_motion("lite")
        self.bg.show()
        self.addCleanup(_drain_pending_restyles)
        self.addCleanup(self.bg.deleteLater)
        self.addCleanup(lambda: _mgr().replace_dynamic_tokens({}))

    def test_palette_change_fades_instead_of_snapping(self) -> None:
        before = QColor(self.bg._tone_b)
        _mgr().replace_dynamic_tokens({
            "accent": "#22cc44", "accent_alt": "#22aacc",
            "ambient_bg": "#0a2a14",
        })
        self.assertLess(self.bg._tone_blend, 1.0, "fade should have started")
        self.assertEqual(self.bg._tone_b.rgb(), before.rgb(),
                         "displayed tone must not snap on the change")
        for _ in range(80):
            self.bg._tick()
        self.assertEqual(self.bg._tone_blend, 1.0)
        self.assertEqual(self.bg._tone_b.rgb(), self.bg._tone_tb.rgb(),
                         "fade must settle exactly on the target")

    def test_motion_off_snaps(self) -> None:
        self.bg.set_motion("off")
        _mgr().replace_dynamic_tokens({
            "accent": "#cc2244", "accent_alt": "#cc8822",
            "ambient_bg": "#2a0a14",
        })
        self.assertEqual(self.bg._tone_blend, 1.0)
        self.assertEqual(self.bg._tone_b.rgb(), self.bg._tone_tb.rgb())

    def test_lerp_endpoints(self) -> None:
        a, b = QColor("#000000"), QColor("#ffffff")
        self.assertEqual(_lerp_color(a, b, 0.0).rgb(), a.rgb())
        self.assertEqual(_lerp_color(a, b, 1.0).rgb(), b.rgb())
        mid = _lerp_color(a, b, 0.5)
        self.assertTrue(126 <= mid.red() <= 129)


if __name__ == "__main__":
    unittest.main()
