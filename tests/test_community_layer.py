"""v1.5 data layer: the JSON payload cache, count normalization, and the
YT Music community surface (insights / report_play / credits / comments).

The load-bearing bits:
  * ``cache.update_json`` merges split writers — the yt-dlp resolver knows
    likes, ``get_song`` knows views, and either may land first. A plain
    put from one side would drop the other's half.
  * ``report_play`` sends the get_song payload (that's what carries the
    playbackTracking URI) and only claims success on YouTube's 200/204.
  * ``get_comments`` runs anonymously by construction — a dead cookie jar
    must not be able to break a public-data fetch.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tide import cache
from tide.sources import ytmusic as ym
from tide.sources.base import Track, human_count, parse_count


class _CacheIsolation(unittest.TestCase):
    """Route the JSON cache at a temp dir and reset its memory mirror."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.enterContext(mock.patch.object(
            cache.config, "CACHE_DIR", Path(self._tmp.name)))
        cache._data_mem.clear()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(cache._data_mem.clear)


class JsonCacheTest(_CacheIsolation):
    def test_roundtrip_and_expiry(self) -> None:
        cache.put_json("ns", "k", {"a": 1}, ttl_seconds=60)
        self.assertEqual(cache.get_json("ns", "k"), {"a": 1})
        cache.put_json("ns", "gone", {"b": 2}, ttl_seconds=-1)
        self.assertIsNone(cache.get_json("ns", "gone"))

    def test_empty_payload_is_a_hit_not_a_miss(self) -> None:
        """[] must round-trip as [] — credits negative-caches empty results
        so credit-less tracks don't re-fetch an album every page open."""
        cache.put_json("ns", "empty", [], ttl_seconds=60)
        self.assertEqual(cache.get_json("ns", "empty"), [])

    def test_survives_memory_reset(self) -> None:
        cache.put_json("ns", "k", {"a": 1}, ttl_seconds=60)
        cache._data_mem.clear()
        self.assertEqual(cache.get_json("ns", "k"), {"a": 1})

    def test_update_json_merges_split_writers(self) -> None:
        # yt-dlp harvest lands likes first…
        cache.update_json("ns", "vid", {"likes": 43000}, ttl_seconds=60)
        # …then get_song lands views. Neither may clobber the other.
        merged = cache.update_json("ns", "vid", {"views": 84_000_000,
                                                 "likes": 0}, ttl_seconds=60)
        self.assertEqual(merged["likes"], 43000)      # zero didn't overwrite
        self.assertEqual(merged["views"], 84_000_000)
        self.assertEqual(cache.get_json("ns", "vid"), merged)

    def test_clear_namespace(self) -> None:
        cache.put_json("ns", "k", 1, ttl_seconds=60)
        cache.clear_namespace("ns")
        self.assertIsNone(cache.get_json("ns", "k"))


class CountHelpersTest(unittest.TestCase):
    def test_parse_count(self) -> None:
        self.assertEqual(parse_count("84.2M views"), 84_200_000)
        self.assertEqual(parse_count("1,204"), 1204)
        self.assertEqual(parse_count("3.4K"), 3400)
        self.assertEqual(parse_count("1.2B"), 1_200_000_000)
        self.assertEqual(parse_count(512), 512)
        self.assertEqual(parse_count(None), 0)
        self.assertEqual(parse_count("no numbers"), 0)

    def test_human_count(self) -> None:
        self.assertEqual(human_count(84_200_000), "84.2m")
        self.assertEqual(human_count(43_000), "43k")
        self.assertEqual(human_count(999), "999")
        self.assertEqual(human_count(1_200_000_000), "1.2b")
        # 0 renders as absence, never as "0 likes".
        self.assertEqual(human_count(0), "")


def _song_payload(video_id: str = "vid1") -> dict:
    return {
        "videoDetails": {"videoId": video_id, "viewCount": "84,200,000",
                         "author": "frank ocean"},
        "microformat": {"microformatDataRenderer": {"publishDate": "2016-08-20"}},
        "playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": "https://s.yt/x"}},
    }


class _FakeYT:
    def __init__(self) -> None:
        self.get_song_calls = 0
        self.history_items: list = []
        self.rate_calls: list = []
        self.album: dict = {}

    def get_song(self, video_id):
        self.get_song_calls += 1
        return _song_payload(video_id)

    def add_history_item(self, song):
        self.history_items.append(song)
        return mock.Mock(status_code=204)

    def rate_song(self, video_id, rating):
        self.rate_calls.append((video_id, rating))

    def get_album(self, browse_id):
        return self.album


class InsightsTest(_CacheIsolation):
    def setUp(self) -> None:
        super().setUp()
        self.fake = _FakeYT()
        self.src = ym.YTMusicSource(self.fake)

    def test_get_song_insights_fetches_and_caches(self) -> None:
        ins = self.src.get_song_insights("vid1")
        self.assertEqual(ins.views, 84_200_000)
        self.assertEqual(ins.year, "2016")
        self.assertEqual(ins.channel, "frank ocean")
        # Second call: served from the "_song"-marked cache entry.
        self.src.get_song_insights("vid1")
        self.assertEqual(self.fake.get_song_calls, 1)

    def test_harvest_merges_with_get_song(self) -> None:
        # Resolver harvest lands likes before get_song ever runs…
        ym._harvest_ytdlp_info("vid1", {"like_count": 43000})
        ins = self.src.get_song_insights("vid1")
        # …and the merged record carries both halves.
        self.assertEqual(ins.likes, 43000)
        self.assertEqual(ins.views, 84_200_000)

    def test_harvest_ignores_empty_info(self) -> None:
        ym._harvest_ytdlp_info("vid1", {"url": "https://cdn/x"})
        self.assertIsNone(cache.get_json(ym._NS_INSIGHTS, "vid1"))


class ReportPlayTest(_CacheIsolation):
    def setUp(self) -> None:
        super().setUp()
        self.fake = _FakeYT()
        self.src = ym.YTMusicSource(self.fake)
        self.track = Track(video_id="vid1", title="t", artists="a")

    def test_reports_the_get_song_payload(self) -> None:
        self.assertTrue(self.src.report_play(self.track))
        self.assertEqual(len(self.fake.history_items), 1)
        self.assertIn("playbackTracking", self.fake.history_items[0])

    def test_shares_the_get_song_fetch_with_insights(self) -> None:
        """One track start = one get_song, even when both consumers run."""
        self.src.get_song_insights("vid1")
        self.src.report_play(self.track)
        self.assertEqual(self.fake.get_song_calls, 1)

    def test_false_when_signed_out(self) -> None:
        self.src._signed_out = True
        self.assertFalse(self.src.report_play(self.track))
        self.assertEqual(self.fake.history_items, [])

    def test_false_when_tracking_uri_missing(self) -> None:
        self.fake.get_song = lambda vid: {"videoDetails": {}}    # type: ignore
        self.fake.add_history_item = mock.Mock(
            side_effect=KeyError("playbackTracking"))
        self.assertFalse(self.src.report_play(self.track))

    def test_dislike_sends_DISLIKE(self) -> None:
        self.src.dislike_song("vid1")
        self.assertEqual(self.fake.rate_calls, [("vid1", "DISLIKE")])


class CreditsTest(_CacheIsolation):
    def setUp(self) -> None:
        super().setUp()
        self.fake = _FakeYT()
        self.src = ym.YTMusicSource(self.fake)

    def test_credits_via_album_lookup(self) -> None:
        self.fake.album = {"tracks": [
            {"videoId": "other", "creditsBrowseId": "MPTCwrong"},
            {"videoId": "vid1", "creditsBrowseId": "MPTCright"},
        ]}
        self.fake.get_song_credits = mock.Mock(return_value={
            "performed_by": {"localized_title": "Performed by",
                             "data": ["Frank Ocean"]},
            "other_sections": [{"localized_title": "Piano", "data": ["X"]}],
        })
        track = Track(video_id="vid1", title="t", artists="a",
                      extras={"album": {"name": "blonde", "id": "MPREb1"}})
        sections = self.src.get_credits_for(track)
        self.fake.get_song_credits.assert_called_once_with("MPTCright")
        self.assertEqual(sections[0].title, "Performed by")
        self.assertEqual(sections[0].names, ["Frank Ocean"])
        self.assertEqual(sections[1].title, "Piano")

    def test_empty_result_is_negative_cached(self) -> None:
        get_album = mock.Mock(return_value={"tracks": []})
        self.fake.get_album = get_album
        track = Track(video_id="vid1", title="t", artists="a",
                      extras={"album": {"name": "x", "id": "MPREb1"}})
        self.assertEqual(self.src.get_credits_for(track), [])
        self.assertEqual(self.src.get_credits_for(track), [])
        # Second call came from the cached empty list, not another album fetch.
        self.assertEqual(get_album.call_count, 1)


class CommentsTest(_CacheIsolation):
    def _fake_ydl(self, info, seen_opts):
        class FakeYDL:
            def __init__(self, opts):
                seen_opts.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                return info

        return FakeYDL

    def test_mapping_and_anonymity(self) -> None:
        info = {
            "comment_count": 12041,
            "comments": [
                {"id": "c1", "parent": "root", "text": "rewired my brain",
                 "like_count": 3400, "author": "@fan",
                 "timestamp": time.time() - 2 * 365 * 86400,
                 "is_pinned": True, "is_favorited": True},
                {"id": "c2", "parent": "c1", "text": "same",
                 "author": "@other"},
                {"id": "c3", "parent": "root", "text": "   "},   # dropped
            ],
        }
        seen: list = []
        fake = _FakeYT()
        src = ym.YTMusicSource(fake)
        with mock.patch.object(ym.yt_dlp, "YoutubeDL",
                               self._fake_ydl(info, seen)):
            comments = src.get_comments("vid1", sort="top", limit=60)
        # Anonymous by construction: no cookiefile may ever appear here.
        self.assertNotIn("cookiefile", seen[0])
        self.assertEqual(len(comments), 2)
        top = comments[0]
        self.assertEqual(top.author, "@fan")
        self.assertTrue(top.pinned)
        self.assertTrue(top.hearted)
        self.assertEqual(top.likes, 3400)
        self.assertEqual(top.time_text, "2y")
        self.assertEqual(top.parent_id, "")          # root normalized away
        self.assertEqual(comments[1].parent_id, "c1")
        # The ride-along counts were banked into insights.
        banked = cache.get_json(ym._NS_INSIGHTS, "vid1")
        self.assertEqual(banked.get("comment_count"), 12041)


if __name__ == "__main__":
    unittest.main()
