"""Shuffle + repeat (v1.4): queue model semantics, session persistence,
and the MPRIS property mapping.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtWidgets import QApplication

from tide.queue import Queue, RepeatMode
from tide.sources.base import Track


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _track(i: int) -> Track:
    return Track(
        video_id=f"vid{i}", title=f"song {i}", artists=f"artist {i}",
        album="album", duration="3:00", duration_seconds=180,
        thumbnail="", source="local",
    )


def _fill(queue: Queue, n: int) -> list[Track]:
    tracks = [_track(i) for i in range(n)]
    queue.add_many(tracks)
    return tracks


class RepeatModeEnumTest(unittest.TestCase):
    def test_parse_accepts_strings_enums_and_junk(self) -> None:
        self.assertIs(RepeatMode.parse("all"), RepeatMode.ALL)
        self.assertIs(RepeatMode.parse(" ONE "), RepeatMode.ONE)
        self.assertIs(RepeatMode.parse(RepeatMode.OFF), RepeatMode.OFF)
        self.assertIs(RepeatMode.parse(None), RepeatMode.OFF)
        self.assertIs(RepeatMode.parse("bogus"), RepeatMode.OFF)

    def test_cycle_order_is_off_all_one(self) -> None:
        self.assertIs(RepeatMode.OFF.cycled(), RepeatMode.ALL)
        self.assertIs(RepeatMode.ALL.cycled(), RepeatMode.ONE)
        self.assertIs(RepeatMode.ONE.cycled(), RepeatMode.OFF)


class RepeatLinearTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.q = Queue()

    def test_repeat_off_still_ends_at_the_tail(self) -> None:
        _fill(self.q, 3)
        self.q.set_current(2)
        self.assertIsNone(self.q.advance())
        self.assertFalse(self.q.can_advance())

    def test_repeat_all_wraps_to_the_top(self) -> None:
        tracks = _fill(self.q, 3)
        self.q.set_repeat(RepeatMode.ALL)
        self.q.set_current(2)
        self.assertTrue(self.q.can_advance())
        self.assertEqual(self.q.peek_next().video_id, tracks[0].video_id)
        nxt = self.q.advance()
        self.assertEqual(nxt.video_id, tracks[0].video_id)
        self.assertEqual(self.q.current_index, 0)

    def test_repeat_one_does_not_trap_manual_advance(self) -> None:
        tracks = _fill(self.q, 3)
        self.q.set_repeat(RepeatMode.ONE)
        self.q.set_current(0)
        # advance() is the manual-next path — it must move on; the
        # track-ended replay is the window's job, not the model's.
        self.assertEqual(self.q.advance().video_id, tracks[1].video_id)

    def test_modes_changed_fires_once_per_actual_change(self) -> None:
        seen = []
        self.q.modes_changed.connect(lambda: seen.append(True))
        self.q.set_repeat(RepeatMode.ALL)
        self.q.set_repeat(RepeatMode.ALL)      # no-op
        self.q.set_shuffle(True)
        self.q.set_shuffle(True)               # no-op
        self.assertEqual(len(seen), 2)

    def test_cycle_repeat_walks_all_three(self) -> None:
        self.assertIs(self.q.cycle_repeat(), RepeatMode.ALL)
        self.assertIs(self.q.cycle_repeat(), RepeatMode.ONE)
        self.assertIs(self.q.cycle_repeat(), RepeatMode.OFF)


class ShuffleTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.q = Queue()

    def test_shuffle_visits_every_track_exactly_once(self) -> None:
        tracks = _fill(self.q, 8)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        seen = [self.q.current.video_id]
        while True:
            tr = self.q.advance()
            if tr is None:
                break
            seen.append(tr.video_id)
        self.assertEqual(sorted(seen), sorted(t.video_id for t in tracks),
                         "one full cycle must cover the whole queue")
        self.assertEqual(len(seen), len(set(seen)), "no repeats within a cycle")

    def test_peek_next_promise_holds_under_shuffle(self) -> None:
        _fill(self.q, 10)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        for _ in range(9):
            promised = self.q.peek_next().video_id
            self.assertEqual(self.q.advance().video_id, promised,
                             "advance() must deliver what peek_next() promised")

    def test_shuffle_with_repeat_all_never_ends(self) -> None:
        _fill(self.q, 4)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        self.q.set_repeat(RepeatMode.ALL)
        last = self.q.current.video_id
        for _ in range(40):
            tr = self.q.advance()
            self.assertIsNotNone(tr, "repeat-all shuffle must never dead-end")
            self.assertNotEqual(tr.video_id, last,
                                "cycle seam must not repeat the same track")
            last = tr.video_id

    def test_back_walks_the_shuffle_trail(self) -> None:
        _fill(self.q, 6)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        visited = [self.q.current.video_id]
        for _ in range(4):
            visited.append(self.q.advance().video_id)
        self.assertTrue(self.q.can_go_back())
        for expect in reversed(visited[:-1]):
            self.assertEqual(self.q.back().video_id, expect)
        self.assertFalse(self.q.can_go_back())

    def test_toggle_off_and_on_resets_the_cycle(self) -> None:
        _fill(self.q, 5)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        self.q.advance()
        self.q.set_shuffle(False)
        self.q.set_shuffle(True)
        # Fresh cycle: only the current track is "played", so 4 more
        # advances must all succeed.
        for _ in range(4):
            self.assertIsNotNone(self.q.advance())

    def test_queue_row_click_counts_toward_the_cycle(self) -> None:
        _fill(self.q, 3)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        self.q.set_current(1)   # user double-clicks row 1
        self.q.set_current(2)   # then row 2
        self.assertIsNone(self.q.advance(),
                          "everything played → cycle exhausted (repeat off)")

    def test_removed_precommit_pick_is_replaced(self) -> None:
        _fill(self.q, 4)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        promised = self.q.peek_next().video_id
        row = next(i for i, t in enumerate(self.q.tracks)
                   if t.video_id == promised)
        self.q.remove(row)
        nxt = self.q.advance()
        self.assertIsNotNone(nxt)
        self.assertNotEqual(nxt.video_id, promised)

    def test_clear_resets_cycle_but_keeps_modes(self) -> None:
        _fill(self.q, 3)
        self.q.set_current(0)
        self.q.set_shuffle(True)
        self.q.set_repeat(RepeatMode.ALL)
        self.q.advance()
        self.q.clear()
        self.assertTrue(self.q.shuffle_enabled)
        self.assertIs(self.q.repeat_mode, RepeatMode.ALL)
        self.assertFalse(self.q.can_go_back())


class SessionPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_snapshot_carries_modes(self) -> None:
        from tide import session
        from tide.player import PlayState
        q = Queue()
        _fill(q, 2)
        q.set_current(0)
        q.set_shuffle(True)
        q.set_repeat(RepeatMode.ONE)
        snap = session.snapshot_from(q, PlayState.PAUSED, 12.0)
        self.assertTrue(snap.shuffle)
        self.assertEqual(snap.repeat, "one")

    def test_legacy_snapshot_defaults_to_modes_off(self) -> None:
        from tide.session import Snapshot
        snap = Snapshot()
        self.assertFalse(snap.shuffle)
        self.assertEqual(snap.repeat, "off")


class MprisMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def _service(self):
        from tide.mpris import MprisService
        q = Queue()
        _fill(q, 3)
        q.set_current(0)
        return MprisService(player=object(), queue=q, window=object()), q

    def test_loop_status_maps_all_three_modes(self) -> None:
        svc, q = self._service()
        self.assertEqual(svc.loop_status, "None")
        q.set_repeat(RepeatMode.ALL)
        self.assertEqual(svc.loop_status, "Playlist")
        q.set_repeat(RepeatMode.ONE)
        self.assertEqual(svc.loop_status, "Track")

    def test_set_loop_status_round_trips(self) -> None:
        svc, q = self._service()
        svc.on_set_loop_status("Playlist")
        self.assertIs(q.repeat_mode, RepeatMode.ALL)
        svc.on_set_loop_status("Track")
        self.assertIs(q.repeat_mode, RepeatMode.ONE)
        svc.on_set_loop_status("None")
        self.assertIs(q.repeat_mode, RepeatMode.OFF)
        # Junk input leaves the mode alone.
        svc.on_set_loop_status("Banana")
        self.assertIs(q.repeat_mode, RepeatMode.OFF)

    def test_shuffle_property_reflects_and_writes(self) -> None:
        svc, q = self._service()
        self.assertFalse(svc.shuffle)
        svc.on_set_shuffle(True)
        self.assertTrue(q.shuffle_enabled)
        self.assertTrue(svc.shuffle)


if __name__ == "__main__":
    unittest.main()
