"""Radio: what plays after the song you picked.

Covers the queue side (refills stay on the picked song's radio and read
further down it, the anchor only moves when that radio runs dry, shuffle
refills by what's left to hear rather than row position, stale answers
are dropped, same-song duplicates and disliked tracks stay out), the
YouTube side (paging by depth, fan uploads and podcasts filtered unless
the seed is one), and the window's listen threshold for the opt-in play
report.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest

from PySide6.QtWidgets import QApplication

from tide.queue import Queue, song_key
from tide.sources.base import Track


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _t(vid: str, title: str | None = None, artists: str = "artist") -> Track:
    return Track(video_id=vid, title=title or f"song {vid}", artists=artists,
                 duration="3:00", thumbnail="")


def _batch(prefix: str, n: int) -> list[Track]:
    return [_t(f"{prefix}{i}") for i in range(n)]


class QueueRadioTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.q = Queue()
        self.asks: list[tuple] = []
        self.q.refill_requested.connect(
            lambda seed, exclude, depth, gen:
            self.asks.append((seed, set(exclude), depth, gen)))

    def _play_now(self, track: Track) -> None:
        # What MainWindow._play_now does.
        self.q.blockSignals(True)
        self.q.clear()
        self.q.blockSignals(False)
        self.q.add(track)
        self.q.set_current(0)
        self.q.enable_radio(track.video_id)

    def test_play_now_asks_once_for_the_pick(self) -> None:
        self._play_now(_t("pick"))
        self.assertEqual(len(self.asks), 1)
        seed, exclude, depth, _gen = self.asks[0]
        self.assertEqual(seed, "pick")
        self.assertIn("pick", exclude)
        self.assertEqual(depth, 0)

    def test_refills_stay_on_the_picked_songs_radio(self) -> None:
        # The drift bug: the second refill used to seed from whatever
        # radio track was playing.
        self._play_now(_t("pick"))
        gen = self.asks[-1][3]
        self.assertEqual(self.q.absorb_radio(_batch("r", 10), gen), 10)
        while self.q.advance() is not None and len(self.asks) < 2:
            pass
        self.assertEqual(len(self.asks), 2)
        seed, exclude, depth, _ = self.asks[1]
        self.assertEqual(seed, "pick")
        self.assertEqual(depth, 1, "the second refill reads further down")
        self.assertIn("r0", exclude)

    def test_the_anchor_moves_when_its_radio_runs_dry(self) -> None:
        self._play_now(_t("pick"))
        gen = self.asks[-1][3]
        self.q.absorb_radio(_batch("r", 6), gen)
        for _ in range(4):
            self.q.advance()
        playing = self.q.current.video_id
        gen = self.asks[-1][3]
        # Only two new tracks: the pick's radio is read out.
        self.q.absorb_radio(_batch("s", 2), gen)
        self.q.advance()
        self.assertEqual(self.asks[-1][0], playing)
        self.assertEqual(self.asks[-1][2], 0)

    def test_an_answer_for_an_old_pick_is_dropped(self) -> None:
        self._play_now(_t("first"))
        stale_gen = self.asks[-1][3]
        self._play_now(_t("second"))
        self.assertEqual(self.asks[-1][0], "second",
                         "the new pick must not wait on the old refill")
        self.assertEqual(self.q.absorb_radio(_batch("old", 10), stale_gen), 0)
        self.assertEqual(self.q.rowCount(), 1)
        self.assertEqual(self.q.absorb_radio(_batch("new", 10),
                                             self.asks[-1][3]), 10)

    def test_a_late_answer_after_radio_off_is_dropped(self) -> None:
        self._play_now(_t("pick"))
        gen = self.asks[-1][3]
        self.q.disable_radio()
        self.assertEqual(self.q.absorb_radio(_batch("r", 10), gen), 0)

    def test_the_same_song_under_another_id_stays_out(self) -> None:
        self._play_now(_t("pick", "Homage", "Mild High Club"))
        gen = self.asks[-1][3]
        added = self.q.absorb_radio([
            _t("omv", "Homage (Official Video)", "Mild High Club"),
            _t("x1", "Other Song", "Someone"),
            _t("x2", "Other Song [Audio]", "Someone"),
        ], gen)
        self.assertEqual(added, 1)
        self.assertEqual([t.video_id for t in self.q.tracks], ["pick", "x1"])

    def test_song_key_ignores_tags_and_case(self) -> None:
        self.assertEqual(song_key(_t("a", "Homage", "Mild High Club")),
                         song_key(_t("b", "HOMAGE (Lyrics)",
                                     "Mild High Club, Someone")))
        self.assertNotEqual(song_key(_t("a", "Homage", "Mild High Club")),
                            song_key(_t("b", "Homage", "Other Artist")))

    def test_disliked_tracks_never_come_back(self) -> None:
        self._play_now(_t("pick"))
        self.q.block_from_radio("bad")
        gen = self.asks[-1][3]
        self.q.absorb_radio([_t("bad"), *_batch("r", 6)], gen)
        self.assertNotIn("bad", self.q.video_ids())

    def test_shuffle_refills_by_what_is_left_to_hear(self) -> None:
        # Under shuffle the old row-position count refilled whenever a
        # random pick landed near the bottom of the list.
        self._play_now(_t("pick"))
        self.q.absorb_radio(_batch("r", 20), self.asks[-1][3])
        self.q.set_shuffle(True)
        asks_before = len(self.asks)
        self.q.set_current(self.q.rowCount() - 1)   # last row, 19 unplayed
        self.assertEqual(len(self.asks), asks_before)
        while self.q.advance() is not None:
            if len(self.asks) > asks_before:
                break
        unplayed = sum(1 for i, t in enumerate(self.q.tracks)
                       if i != self.q.current_index
                       and t.video_id not in self.q._played_vids)
        self.assertLessEqual(unplayed, Queue.REFILL_TAIL)


class _FakeYT:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[dict] = []

    def get_watch_playlist(self, **kwargs):
        self.calls.append(kwargs)
        return {"tracks": list(self.items)}


def _item(vid: str, vtype: str = "MUSIC_VIDEO_TYPE_ATV") -> dict:
    return {"videoId": vid, "title": vid, "artists": [{"name": "a"}],
            "videoType": vtype, "length": "3:00"}


class YTRadioTest(unittest.TestCase):
    def _src(self, items):
        from tide.sources.ytmusic import YTMusicSource
        src = YTMusicSource.__new__(YTMusicSource)
        src.yt = _FakeYT(items)
        return src

    def test_depth_reads_further_down(self) -> None:
        src = self._src([_item("seed")])
        src.get_radio("seed", depth=0)
        src.get_radio("seed", depth=2)
        src.get_radio("seed", depth=40)
        limits = [c["limit"] for c in src.yt.calls]
        self.assertEqual(limits, [50, 150, 250])
        self.assertTrue(all(c["radio"] for c in src.yt.calls))

    def test_fan_uploads_and_podcasts_are_dropped(self) -> None:
        src = self._src([
            _item("seed"),
            _item("ok"),
            _item("mv", "MUSIC_VIDEO_TYPE_OMV"),
            _item("fan", "MUSIC_VIDEO_TYPE_UGC"),
            _item("pod", "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"),
        ])
        got = [t.video_id for t in src.get_radio("seed")]
        self.assertEqual(got, ["ok", "mv"])

    def test_a_fan_upload_seed_keeps_fan_uploads(self) -> None:
        src = self._src([
            _item("seed", "MUSIC_VIDEO_TYPE_UGC"),
            _item("fan", "MUSIC_VIDEO_TYPE_UGC"),
            _item("pod", "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"),
        ])
        got = [t.video_id for t in src.get_radio("seed")]
        self.assertEqual(got, ["fan"])


class ReportThresholdTest(unittest.TestCase):
    """The opt-in history ping waits for a real listen: 30 s, or half of
    a short song. A skip never reaches the account's history."""

    def setUp(self) -> None:
        _app()
        from tide import settings as settings_module
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.settings import Settings
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        self._real_save = settings_module.save
        settings_module.save = lambda s: None
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.w._settings = Settings(report_plays=True)
        self.spawned: list[dict] = []
        self.w._spawn_play_started_worker = (
            lambda track, **kw: self.spawned.append(kw))
        self.track = _t("v1")
        self.w._current = self.track
        self.w._report_pending_for = "v1"
        self.w._report_listened = 0.0
        self.w._report_last_pos = None

    def tearDown(self) -> None:
        from tide import settings as settings_module
        self.w.close()
        settings_module.save = self._real_save

    def _play_to(self, until: float, start: float = 0.0) -> None:
        pos = start
        while pos <= until:
            self.w._maybe_report_play(pos)
            pos += 0.5

    def test_a_skip_is_not_reported(self) -> None:
        self._play_to(12.0)
        self.assertEqual(self.spawned, [])

    def test_thirty_seconds_is_a_listen(self) -> None:
        self._play_to(31.0)
        self.assertEqual(self.spawned, [{"report": True, "insights": False}])
        self._play_to(90.0, start=31.5)
        self.assertEqual(len(self.spawned), 1, "reported once per play")

    def test_a_seek_does_not_count_as_listening(self) -> None:
        self._play_to(5.0)
        self.w._maybe_report_play(170.0)       # dragged to the end
        self._play_to(180.0, start=170.5)
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
