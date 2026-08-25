"""v1.5 library parity: the YT source's write paths and their guards.

What must hold: playlist removal only sends rows that carry the
setVideoId (anything else can't be removed remotely and must be skipped,
not crash), remote-history removal only sends real feedbackTokens,
"VL"-prefixed playlist browse ids get normalized, and add-album refuses
politely without an audio playlist id.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tide import cache
from tide.sources import ytmusic as ym
from tide.sources.base import AlbumDetail, Track


class _FakeYT:
    def __init__(self) -> None:
        self.calls: list = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {
                "create_playlist": "PLnew",
                "add_playlist_items": {"status": "STATUS_SUCCEEDED"},
                "remove_playlist_items": "STATUS_SUCCEEDED",
                "edit_playlist": "STATUS_SUCCEEDED",
                "delete_playlist": "STATUS_SUCCEEDED",
                "rate_playlist": {"ok": True},
                "search": [],
            }.get(name, {})
        return record


class _Src(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.enterContext(mock.patch.object(
            cache.config, "CACHE_DIR", Path(self._tmp.name)))
        cache._data_mem.clear()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(cache._data_mem.clear)
        self.fake = _FakeYT()
        self.src = ym.YTMusicSource(self.fake)


class PlaylistWritesTest(_Src):
    def test_remove_skips_rows_without_setVideoId(self) -> None:
        removable = Track(video_id="v1", title="a", artists="x",
                          extras={"videoId": "v1", "setVideoId": "s1"})
        stray = Track(video_id="v2", title="b", artists="y")   # no extras
        ok = self.src.remove_from_playlist("PL1", [removable, stray])
        self.assertTrue(ok)
        name, args, kwargs = self.fake.calls[-1]
        self.assertEqual(name, "remove_playlist_items")
        self.assertEqual(args[1], [{"videoId": "v1", "setVideoId": "s1"}])

    def test_remove_with_nothing_removable_never_calls_out(self) -> None:
        stray = Track(video_id="v2", title="b", artists="y")
        self.assertFalse(self.src.remove_from_playlist("PL1", [stray]))
        self.assertEqual(self.fake.calls, [])

    def test_create_returns_id_and_add_reports_success(self) -> None:
        self.assertEqual(
            self.src.create_playlist_remote("mix", video_ids=["v1"]), "PLnew")
        self.assertTrue(self.src.add_to_playlist("PL1", ["v1", "v2"]))

    def test_edit_without_changes_is_a_noop_success(self) -> None:
        self.assertTrue(self.src.edit_playlist_remote("PL1"))
        self.assertEqual(self.fake.calls, [])


class AlbumLibraryTest(_Src):
    def test_add_album_needs_the_audio_playlist_id(self) -> None:
        without = AlbumDetail(browse_id="MPREb1", title="x")
        self.assertFalse(self.src.add_album_to_library(without))
        self.assertEqual(self.fake.calls, [])
        with_id = AlbumDetail(browse_id="MPREb1", title="x",
                              playlist_id="OLAK1")
        self.assertTrue(self.src.add_album_to_library(with_id))
        self.assertEqual(self.fake.calls[-1][0], "rate_playlist")
        self.assertEqual(self.fake.calls[-1][1], ("OLAK1", "LIKE"))


class RemoteHistoryTest(_Src):
    def test_remove_only_sends_real_tokens(self) -> None:
        with_token = Track(video_id="v1", title="a", artists="x",
                           extras={"feedbackToken": "tok1"})
        without = Track(video_id="v2", title="b", artists="y")
        self.assertTrue(self.src.remove_remote_history([with_token, without]))
        name, args, _ = self.fake.calls[-1]
        self.assertEqual(name, "remove_history_items")
        self.assertEqual(args[0], ["tok1"])

    def test_no_tokens_no_call(self) -> None:
        bare = Track(video_id="v2", title="b", artists="y")
        self.assertFalse(self.src.remove_remote_history([bare]))
        self.assertEqual(self.fake.calls, [])


class PlaylistSearchTest(_Src):
    def test_vl_prefix_is_normalized(self) -> None:
        self.fake.search = lambda *a, **k: [       # type: ignore[assignment]
            {"browseId": "VLPL123", "title": "mix", "author": "someone"},
            {"playlistId": "PL456", "title": "other"},
            {"title": "no id — dropped"},
        ]
        out = self.src.search_playlists("chill")
        self.assertEqual([p.playlist_id for p in out], ["PL123", "PL456"])


class SubscribeTest(_Src):
    def test_subscribe_and_unsubscribe_route(self) -> None:
        self.assertTrue(self.src.set_artist_subscribed("UC1", True))
        self.assertEqual(self.fake.calls[-1][0], "subscribe_artists")
        self.assertTrue(self.src.set_artist_subscribed("UC1", False))
        self.assertEqual(self.fake.calls[-1][0], "unsubscribe_artists")


if __name__ == "__main__":
    unittest.main()
