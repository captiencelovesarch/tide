"""StreamPrefetch dedupe/join behavior.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import threading
import time
import unittest

from PySide6.QtWidgets import QApplication

from tide.playback.prefetch import StreamPrefetch
from tide.sources import MusicSource, StreamRef, Track, registry


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _spin(cond, timeout_ms: int = 5000) -> bool:
    """Process Qt events until cond() or timeout. Returns cond()'s final value."""
    app = _app()
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return bool(cond())


class _FakeSource(MusicSource):
    """Counts resolve calls; blocks each one on a gate so tests can observe
    the in-flight window deterministically."""

    slug = "fakesrc"
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.gate = threading.Event()
        self.fail = False
        self._lock = threading.Lock()

    def search_songs(self, query: str, limit: int = 20) -> list:
        return []

    def resolve_stream(self, track: Track) -> StreamRef:
        with self._lock:
            self.calls += 1
        self.gate.wait(5)
        if self.fail:
            raise RuntimeError("resolve exploded")
        return StreamRef(backend="mpv", payload=f"https://cdn/{track.video_id}")


def _track(vid: str) -> Track:
    return Track(video_id=vid, title=vid, artists="x", source="fakesrc")


class PrefetchCoalesceTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.source = _FakeSource()
        registry().register(self.source)
        self.prefetch = StreamPrefetch()

    def tearDown(self) -> None:
        self.source.gate.set()
        self.prefetch.shutdown(wait_ms=2000)

    def test_duplicate_requests_resolve_once(self) -> None:
        tr = _track("aaa")
        self.prefetch.request(tr)
        self.prefetch.request(tr)   # while first is blocked on the gate
        self.assertTrue(_spin(lambda: self.source.calls >= 1))
        self.source.gate.set()
        self.assertTrue(_spin(lambda: self.prefetch.lookup("aaa") is not None))
        self.assertEqual(self.source.calls, 1)

    def test_is_inflight_tracks_the_window(self) -> None:
        tr = _track("bbb")
        self.assertFalse(self.prefetch.is_inflight("bbb"))
        self.prefetch.request(tr)
        self.assertTrue(self.prefetch.is_inflight("bbb"))
        self.source.gate.set()
        self.assertTrue(_spin(lambda: not self.prefetch.is_inflight("bbb")))
        self.assertIsNotNone(self.prefetch.lookup("bbb"))
        # A fresh request on a cached vid must not spawn another resolve.
        self.prefetch.request(tr)
        self.assertFalse(self.prefetch.is_inflight("bbb"))
        self.assertEqual(self.source.calls, 1)

    def test_failed_signal_fires_and_clears_inflight(self) -> None:
        self.source.fail = True
        self.source.gate.set()
        got: list = []
        self.prefetch.failed.connect(lambda vid, msg: got.append((vid, msg)))
        self.prefetch.request(_track("ccc"))
        self.assertTrue(_spin(lambda: len(got) == 1))
        vid, msg = got[0]
        self.assertEqual(vid, "ccc")
        self.assertIn("resolve exploded", msg)
        self.assertFalse(self.prefetch.is_inflight("ccc"))
        self.assertIsNone(self.prefetch.lookup("ccc"))


if __name__ == "__main__":
    unittest.main()
