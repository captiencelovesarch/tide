"""StreamPrefetch.warm() — source filter, limit, dedupe, cancellation.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import time
import unittest

from PySide6.QtWidgets import QApplication

from tide.playback.prefetch import StreamPrefetch
from tide.sources import Track


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _spin(cond, timeout_ms: int = 3000) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return bool(cond())


class _RecordingPrefetch(StreamPrefetch):
    """warm() drives request(); recording it tests the queue logic without
    spawning resolver threads."""

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[str] = []
        self._warm_spacing_ms = 1   # drain fast in tests

    def request(self, track) -> None:
        self.requested.append(track.video_id)


def _yt(vid: str) -> Track:
    return Track(video_id=vid, title=vid, artists="x", source="ytmusic")


def _local(vid: str) -> Track:
    return Track(video_id=vid, title=vid, artists="x", source="local")


class PrefetchWarmTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.prefetch = _RecordingPrefetch()

    def tearDown(self) -> None:
        self.prefetch.shutdown(wait_ms=500)

    def test_filters_fast_sources_and_dedupes(self) -> None:
        tracks = [_yt("a"), _local("skip"), _yt("a"), _yt("b"), None, _yt("c")]
        self.prefetch.warm(tracks, limit=5)
        self.assertTrue(_spin(lambda: len(self.prefetch.requested) == 3))
        self.assertEqual(self.prefetch.requested, ["a", "b", "c"])

    def test_limit_truncates(self) -> None:
        self.prefetch.warm([_yt(v) for v in "abcdefg"], limit=2)
        self.assertTrue(_spin(lambda: len(self.prefetch.requested) == 2))
        # Give the (stopped) chain a beat to prove nothing else fires.
        _spin(lambda: False, timeout_ms=50)
        self.assertEqual(self.prefetch.requested, ["a", "b"])

    def test_second_warm_cancels_first(self) -> None:
        self.prefetch.warm([_yt("a1"), _yt("a2"), _yt("a3")], limit=3)
        # First track fires synchronously; replace the queue before the
        # timer chain delivers the rest.
        self.prefetch.warm([_yt("b1"), _yt("b2")], limit=2)
        self.assertTrue(_spin(lambda: "b2" in self.prefetch.requested))
        _spin(lambda: False, timeout_ms=50)
        self.assertNotIn("a2", self.prefetch.requested)
        self.assertNotIn("a3", self.prefetch.requested)
        self.assertEqual(self.prefetch.requested, ["a1", "b1", "b2"])

    def test_clear_stops_pending_warm(self) -> None:
        self.prefetch.warm([_yt("x1"), _yt("x2"), _yt("x3")], limit=3)
        self.prefetch.clear()
        _spin(lambda: False, timeout_ms=50)
        self.assertEqual(self.prefetch.requested, ["x1"])


if __name__ == "__main__":
    unittest.main()
