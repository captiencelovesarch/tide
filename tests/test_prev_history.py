"""[prev] must walk back across queue replacements, not just within one queue.

_play_now() clears the queue on every pick, so a user who plays songs one at a
time always sits at row 0 and queue.back() has nowhere to go — the prev button
was dead for the most common way of using tide.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtWidgets import QApplication

from tide.playback import MpvBackend, PlaybackRouter
from tide.queue import Queue
from tide.sources.base import Track
from tide.sources.local import LocalSource
from tide.ui.window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _track(i: int) -> Track:
    return Track(video_id=f"v{i}", title=f"t{i}", artists="a")


class AddPrevTest(unittest.TestCase):
    def test_inserts_before_current_and_keeps_index(self) -> None:
        q = Queue()
        q.add_many([_track(1), _track(2)])
        q.set_current(1)
        q.add_prev(_track(0))
        self.assertEqual([t.video_id for t in q.tracks], ["v1", "v0", "v2"])
        # The playing track must still be the one that was playing.
        self.assertEqual(q.current.video_id, "v2")
        self.assertEqual(q.current_index, 2)

    def test_empty_queue(self) -> None:
        q = Queue()
        q.add_prev(_track(0))
        self.assertEqual([t.video_id for t in q.tracks], ["v0"])


class PrevHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.played: list[str] = []
        real = self.w._play_track
        def spy(tr, *a, **k):
            self.played.append(tr.video_id)
            real(tr, *a, **k)
        self.w._play_track = spy

    def tearDown(self) -> None:
        self.w.close()

    def test_prev_walks_back_through_separately_played_tracks(self) -> None:
        for i in range(3):
            self.w._play_now(_track(i))     # each call clears the queue
        self.assertEqual(self.w.queue.rowCount(), 1)
        self.assertIsNone(self.w.queue.back(), "queue alone has no history")

        self.played.clear()
        self.w._last_position = 0.0
        self.w._on_prev_clicked()
        self.assertEqual(self.w._current.video_id, "v1")
        self.w._on_prev_clicked()
        self.assertEqual(self.w._current.video_id, "v0")
        self.assertEqual(self.played, ["v1", "v0"])

    def test_prev_stops_at_the_end_of_history(self) -> None:
        self.w._play_now(_track(0))
        self.w._last_position = 0.0
        self.w._on_prev_clicked()
        self.played.clear()
        self.w._on_prev_clicked()
        self.assertEqual(self.played, [], "nothing left to go back to")

    def test_going_back_does_not_re_record_history(self) -> None:
        """Otherwise prev/prev ping-pongs between the same two tracks."""
        for i in range(3):
            self.w._play_now(_track(i))
        self.w._last_position = 0.0
        self.w._on_prev_clicked()
        self.w._on_prev_clicked()
        self.assertEqual(self.w._play_history, [])

    def test_restart_then_back_on_a_double_press(self) -> None:
        """First press restarts the track; a second press must go back even
        before mpv delivers its next position tick."""
        seeks: list[float] = []
        self.w.player.seek = lambda s: seeks.append(s)
        type(self.w.player).duration = property(lambda _s: 200.0)
        try:
            self.w._play_now(_track(1))
            self.w._push_play_history(_track(0))
            self.w._last_position = 30.0
            self.played.clear()

            self.w._on_prev_clicked()
            self.assertEqual(seeks, [0], "first press restarts")
            self.assertEqual(self.played, [])

            self.w._on_prev_clicked()
            self.assertEqual(seeks, [0], "second press must not restart again")
            self.assertEqual(self.played, ["v0"])
        finally:
            del type(self.w.player).duration


if __name__ == "__main__":
    unittest.main()
