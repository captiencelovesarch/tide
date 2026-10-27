"""The word-timing service (offscreen): stages, the cache, settings and
the fallback to line mode. NetEase and the aligner are swapped for fakes
through the service's seams — no network, no model."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tide import lyric_timing, lyric_words as lw  # noqa: E402
from tide.lyric_words import Word, WordLine  # noqa: E402
from tide.lyrics_provider import LyricsResult  # noqa: E402

_app = QApplication.instance() or QApplication([])

LINES = [(10.0, "White shirt now red"), (13.0, "my bloody nose"),
         (16.0, "Sleeping you're on your tippy toes")]


def _source_sheet(shift: float) -> list[WordLine]:
    out = []
    for t, text in LINES:
        ws = tuple(Word(t + shift + 0.4 * i, t + shift + 0.4 * i + 0.3, w)
                   for i, w in enumerate(text.split()))
        out.append(WordLine(ws[0].start, ws[-1].end, text, ws, "source"))
    return out


def _wait(pred, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch("tide.cache.config.CACHE_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.svc = lyric_timing.WordTimingService()
        self.addCleanup(self.svc.close)
        self.settings = SimpleNamespace(lyrics_word_timing="sources", lyrics_netease=True)
        self.svc.set_settings_provider(lambda: self.settings)
        self.got: list = []
        self.svc.updated.connect(lambda key, sheet: self.got.append((key, sheet)))
        self.calls = 0

        def fake_ne(**_kw):
            self.calls += 1
            return _source_sheet(1.5)
        self.svc.fetch_netease = fake_ne
        self.track = SimpleNamespace(video_id="vid1", source="ytmusic", title="bad guy",
                                     artists="Billie Eilish", duration_seconds=194)
        self.result = LyricsResult(timed_lines=list(LINES), line_ends=[12.0, 15.0, None])

    def test_line_mode_now_source_words_later(self) -> None:
        now = self.svc.sheet_for(self.track, self.result)
        self.assertEqual([l.timing for l in now], ["line"] * 3)
        self.assertAlmostEqual(now[0].end, 12.0)          # YT's line end is used
        self.assertTrue(_wait(lambda: self.got))
        key, sheet = self.got[-1]
        self.assertEqual(key, "ytmusic:vid1")
        self.assertEqual([l.timing for l in sheet], ["source"] * 3)
        # NetEase's 1.5 s offset is removed against the video's lines
        self.assertAlmostEqual(sheet[1].words[0].start, 13.0, places=3)

    def test_cached_after_the_first_time(self) -> None:
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: self.got))
        fresh = lyric_timing.WordTimingService()
        self.addCleanup(fresh.close)
        fresh.set_settings_provider(lambda: self.settings)
        fresh.fetch_netease = lambda **_kw: self.fail("should come from the cache")
        sheet = fresh.sheet_for(self.track, self.result)
        self.assertEqual(sheet[0].timing, "source")

    def test_lines_setting_skips_sources(self) -> None:
        self.settings.lyrics_word_timing = "lines"
        sheet = self.svc.sheet_for(self.track, self.result)
        self.assertEqual(sheet[0].timing, "line")
        _wait(lambda: False, timeout=0.3)
        self.assertEqual(self.calls, 0)

    def test_netease_toggle(self) -> None:
        self.settings.lyrics_netease = False
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: self.got))
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.got[-1][1][0].timing, "line")

    def test_own_word_lines_win(self) -> None:
        self.result.word_lines = _source_sheet(0.0)
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: self.got))
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.got[-1][1][0].timing, "source")

    def test_wrong_song_falls_back_to_lines(self) -> None:
        self.svc.fetch_netease = lambda **_kw: [
            WordLine(1.0, 2.0, "something else entirely", (Word(1.0, 2.0, "x"),), "source"),
            WordLine(3.0, 4.0, "and more of it", (Word(3.0, 4.0, "y"),), "source")]
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: self.got))
        self.assertEqual({l.timing for l in self.got[-1][1]}, {"line"})

    def test_untimed_lyrics_take_the_word_sheet(self) -> None:
        plain = LyricsResult(plain_text="White shirt now red")
        self.assertEqual(self.svc.sheet_for(self.track, plain), [])
        self.assertTrue(_wait(lambda: self.got))
        self.assertEqual(self.got[-1][1][0].text, "White shirt now red")

    def test_precise_stage_runs_after_sources(self) -> None:
        # the lyric timing plugin's aligner (ctx.lyrics.add_aligner)
        from tide.sources.base import StreamRef
        seen = []

        def aligner(sheet, ref, should_stop, say):
            seen.append(([l.timing for l in sheet], ref.payload))
            say("timing words to the audio...")
            return [lw.shift(l, 0.25) for l in sheet]
        self.svc.set_aligner(aligner)
        self.svc.set_stream_provider(lambda track: StreamRef(backend="mpv", payload="file:///song"))
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: len(self.got) >= 2))
        self.assertEqual(seen, [(["source"] * 3, "file:///song")])
        self.assertAlmostEqual(self.got[-1][1][1].words[0].start, 13.25, places=3)

    def test_no_aligner_is_sources_only(self) -> None:
        self.svc.set_stream_provider(lambda track: self.fail("no aligner, no stream wait"))
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: self.got))
        self.assertEqual(self.got[-1][1][0].timing, "source")

    def test_a_broken_aligner_keeps_source_timing(self) -> None:
        from tide.sources.base import StreamRef
        self.svc.set_aligner(lambda *a: 1 / 0)
        self.svc.set_stream_provider(lambda track: StreamRef(backend="mpv", payload="x"))
        self.svc.sheet_for(self.track, self.result)
        self.assertTrue(_wait(lambda: self.got))
        self.assertEqual(self.got[-1][1][0].timing, "source")


class PickTest(unittest.TestCase):
    def test_title_artist_and_duration_must_agree(self) -> None:
        from tide.lyrics_netease import pick
        songs = [
            {"id": 1, "name": "bad guy (Live)", "artists": [{"name": "Billie Eilish"}], "duration": 250000},
            {"id": 2, "name": "bad guy", "artists": [{"name": "Some Cover Band"}], "duration": 194000},
            {"id": 3, "name": "bad guy", "artists": [{"name": "Billie Eilish"}], "duration": 194100},
        ]
        self.assertEqual(pick(songs, title="bad guy", artist="Billie Eilish",
                              duration_seconds=194), 3)
        self.assertIsNone(pick(songs[:2], title="bad guy", artist="Billie Eilish",
                               duration_seconds=194))
        self.assertIsNone(pick([{"id": "x"}, None], title="a", artist="b", duration_seconds=0))

    def test_fetch_caches_misses(self) -> None:
        from tide import lyrics_netease as ne
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ne.config, "LYRICS_CACHE_DIR", Path(tmp)), \
                mock.patch.object(ne, "_get_json", return_value={"result": {"songs": []}}) as get:
            self.assertIsNone(ne.fetch_yrc(title="t", artist="a", duration_seconds=100))
            self.assertIsNone(ne.fetch_yrc(title="t", artist="a", duration_seconds=100))
            self.assertEqual(get.call_count, 1)

    def test_network_error_is_not_cached(self) -> None:
        from tide import lyrics_netease as ne
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ne.config, "LYRICS_CACHE_DIR", Path(tmp)), \
                mock.patch.object(ne, "_get_json", side_effect=OSError("down")) as get:
            ne.fetch_yrc(title="t", artist="a")
            ne.fetch_yrc(title="t", artist="a")
            self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
