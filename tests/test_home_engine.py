"""v1.5 home engine: pattern picking + browse-feed parsing.

The pattern picker is what keeps home from being shelf × N — stable YT
shelf names map to their signature shapes, artist shelves go circular,
and mixed shelves rotate on the daily seed (same day = same page, next
day = fresh arrangement). The parsing tests pin the ChartEntry/moods
shapes the ytmusicapi payloads reduce to, with ranks surviving and
garbage tolerated.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tide import cache
from tide.sources import ytmusic as ym
from tide.sources.base import ShelfItem
from tide.ui.home import pick_pattern
from tide.ui.home.view import _country_from_env, _weekly_stats


def _items(kind: str, n: int = 4) -> list:
    return [ShelfItem(kind=kind, title=f"t{i}") for i in range(n)]


class PickPatternTest(unittest.TestCase):
    def test_stable_yt_names(self) -> None:
        self.assertEqual(pick_pattern("Quick picks", _items("song"), 3, 1), "tap_grid")
        self.assertEqual(pick_pattern("Listen again", _items("album"), 2, 1), "dense_grid")

    def test_artist_shelves_go_circular(self) -> None:
        self.assertEqual(pick_pattern("Similar artists", _items("artist"), 5, 9), "circle_row")

    def test_first_song_shelf_is_a_tap_grid(self) -> None:
        self.assertEqual(pick_pattern("Today's hits", _items("song"), 0, 9), "tap_grid")
        self.assertEqual(pick_pattern("Today's hits", _items("song"), 2, 9), "shelf_row")

    def test_mixed_shelves_rotate_with_the_seed(self) -> None:
        items = _items("album") + _items("playlist")
        day1 = [pick_pattern("Mixed for you", items, i, seed=100) for i in range(3)]
        day1_again = [pick_pattern("Mixed for you", items, i, seed=100) for i in range(3)]
        day2 = [pick_pattern("Mixed for you", items, i, seed=101) for i in range(3)]
        self.assertEqual(day1, day1_again)      # deterministic within a day
        self.assertNotEqual(day1, day2)         # fresh arrangement next day
        for p in day1 + day2:
            self.assertIn(p, ("mosaic", "shelf_row", "dense_grid"))


class CountryFromEnvTest(unittest.TestCase):
    def test_lang_parse(self) -> None:
        with mock.patch.dict("os.environ", {"LC_ALL": "", "LC_MESSAGES": "",
                                            "LANG": "en_US.UTF-8"}):
            self.assertEqual(_country_from_env(), "US")

    def test_fallback_global(self) -> None:
        with mock.patch.dict("os.environ", {"LC_ALL": "C", "LC_MESSAGES": "",
                                            "LANG": "C"}):
            self.assertEqual(_country_from_env(), "ZZ")


class WeeklyStatsTest(unittest.TestCase):
    def test_counts_only_the_last_week(self) -> None:
        import time as _time
        now = _time.time()
        entries = [
            mock.Mock(played_at=now - 3600, duration_seconds=3600, artists="frank ocean"),
            mock.Mock(played_at=now - 2 * 3600, duration_seconds=1800, artists="frank ocean, x"),
            mock.Mock(played_at=now - 10 * 86400, duration_seconds=9999, artists="old"),
        ]
        with mock.patch.object(ym, "time", _time):     # no-op, keeps import shape
            with mock.patch("tide.ui.home.view.history_module") as hist:
                hist.read_recent.return_value = entries
                line = _weekly_stats()
        self.assertIn("1h 30m", line)
        self.assertIn("mostly frank ocean", line)

    def test_empty_history_is_silent(self) -> None:
        with mock.patch("tide.ui.home.view.history_module") as hist:
            hist.read_recent.return_value = []
            self.assertEqual(_weekly_stats(), "")


class _BrowseFake:
    def __init__(self, explore=None, charts=None, moods=None) -> None:
        self._explore = explore or {}
        self._charts = charts or {}
        self._moods = moods or {}

    def get_explore(self):
        return self._explore

    def get_charts(self, country="ZZ"):
        return self._charts

    def get_mood_categories(self):
        return self._moods

    def get_mood_playlists(self, params):
        return []


class BrowseParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.enterContext(mock.patch.object(
            cache.config, "CACHE_DIR", Path(self._tmp.name)))
        cache._data_mem.clear()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(cache._data_mem.clear)

    def test_explore_reduces_to_entries(self) -> None:
        fake = _BrowseFake(explore={
            "new_releases": [
                {"title": "Hangang", "browseId": "MPREb_1",
                 "artists": [{"name": "Dept"}], "thumbnails": [],
                 "audioPlaylistId": "OLAK_1"},
                {"title": "no browse id — dropped"},
            ],
            "top_songs": {"playlist": "VLPL1", "items": [
                {"title": "Outside", "videoId": "v1", "rank": "1",
                 "trend": "up", "artists": [{"name": "MO3"}]},
                {"title": "unranked", "videoId": "v2"},
            ]},
            "moods_and_genres": [{"title": "Chill", "params": "ggMP1"}],
        })
        src = ym.YTMusicSource(fake)
        data = src.get_explore_data()
        self.assertEqual(data["new_releases"][0].browse_id, "MPREb_1")
        self.assertEqual(data["new_releases"][0].playlist_id, "OLAK_1")
        self.assertEqual(len(data["new_releases"]), 1)
        top = data["top_songs"]
        self.assertEqual((top[0].rank, top[0].trend), (1, "up"))
        # Unranked rows get positional ranks so the chart stays a chart.
        self.assertEqual(top[1].rank, 2)
        self.assertEqual(data["moods"][0][1][0].title, "Chill")

    def test_charts_artists_rank_and_playlists_merge(self) -> None:
        fake = _BrowseFake(charts={
            "countries": {"selected": {"text": "United States"}},
            "artists": [
                {"title": "Artist A", "browseId": "UC1", "rank": "1",
                 "trend": "neutral", "subscribers": "10M"},
            ],
            "videos": [{"title": "Top 100", "playlistId": "PL1"}],
            "genres": [{"title": "Top Pop", "playlistId": "PL2"}],
        })
        src = ym.YTMusicSource(fake)
        data = src.get_charts_data("US")
        self.assertEqual(data["selected"], "United States")
        self.assertEqual(data["artists"][0].item.artist.channel_id, "UC1")
        self.assertIn("subscribers", data["artists"][0].item.subtitle)
        self.assertEqual({p.playlist_id for p in data["playlists"]}, {"PL1", "PL2"})

    def test_moods_fallback_when_explore_omits_them(self) -> None:
        fake = _BrowseFake(explore={}, moods={
            "Moods & moments": [{"title": "Chill", "params": "p1"},
                                {"title": "Commute", "params": "p2"}],
        })
        src = ym.YTMusicSource(fake)
        data = src.get_explore_data()
        sections = data["moods"]
        self.assertEqual(sections[0][0], "Moods & moments")
        self.assertEqual([c.title for c in sections[0][1]], ["Chill", "Commute"])


class _HomeFakeApi:
    """Just enough source for a full HomeView.reload() round trip."""
    slug = "ytmusic"
    name = "youtube music"
    account_name = "captience"

    def supports(self, cap: str) -> bool:
        return cap in {"home", "explore", "charts"}

    def get_home(self, limit: int = 5):
        from tide.sources.base import Shelf, ShelfItem, Track
        song = ShelfItem(kind="song", title="nights", subtitle="frank ocean",
                        track=Track(video_id="v1", title="nights",
                                    artists="frank ocean"))
        artist = ShelfItem(kind="artist", title="frank ocean",
                           subtitle="artist")
        return [
            Shelf(title="Quick picks", items=[song] * 6),
            Shelf(title="Similar artists", items=[artist] * 4),
        ]

    def get_explore_data(self):
        from tide.sources.base import AlbumEntry
        return {"new_releases": [
            AlbumEntry(browse_id="MPREb1", title="blonde",
                       artists="frank ocean")] * 3}

    def get_charts_data(self, country="ZZ"):
        return {}


class HomeViewSmokeTest(unittest.TestCase):
    def test_reload_builds_blocks_without_exceptions(self) -> None:
        import sys
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from tide.ui.home import HomeView
        QApplication.instance() or QApplication(sys.argv[:1])
        view = HomeView(_HomeFakeApi())
        with mock.patch("tide.ui.home.view.history_module") as hist, \
                mock.patch("tide.ui.home.view.session_module") as sess:
            hist.read_recent.return_value = []
            sess.load.return_value = None
            view.reload()
            # Workers (against the fake, instant) + the one-block-per-tick
            # build chain both need event-loop turns.
            QTest.qWait(200)
        # Hero + 2 shelf blocks (heading+body each) + new-releases extra
        # (heading+body) + trailing stretch — at minimum 6 widgets.
        self.assertGreaterEqual(view._content_col.count(), 6)
        self.assertTrue(view._loaded)


if __name__ == "__main__":
    unittest.main()
