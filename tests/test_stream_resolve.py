"""A resolved stream URL must actually answer before anyone trusts it.

The bug: YouTube gates individual songs behind PO tokens now. For those,
the cookie/web yt-dlp pass dies with "Requested format is not available"
and the anonymous default client still *returns* a URL — which answers
HTTP 403 to every request mpv makes. tide then cached that poison URL on
disk for four hours, and the player-error handler only cleared the
in-memory prefetch layer, so every retry replayed the same dead URL.
Bonus failure: the per-song auth error tripped the "auth pass broken"
memo, converting one gated song into thirty minutes of poison URLs for
every song.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tide import auth, cache
from tide.sources import ytmusic as ym


LIVE_URL = "https://rr1.example/videoplayback?ok=1"
DEAD_URL = "https://rr2.example/videoplayback?gated=1"


def _fake_ydl(responses):
    """Build a YoutubeDL stand-in that answers per pass.

    ``responses`` maps a pass key to either an Exception (raised from
    extract_info) or a URL (returned as the info dict). Keys: "auth" for
    the cookiefile pass, "anon" for the bare default pass, or the client
    name for a ``player_client`` fallback pass.
    """
    calls = []

    class FakeYDL:
        def __init__(self, opts):
            clients = (opts.get("extractor_args", {})
                       .get("youtube", {}).get("player_client"))
            if clients:
                self.key = clients[0]
            elif opts.get("cookiefile"):
                self.key = "auth"
            else:
                self.key = "anon"
            calls.append(self.key)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            out = responses[self.key]
            if isinstance(out, Exception):
                raise out
            return {"url": out}

    return FakeYDL, calls


class ResolveFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        ym._auth_pass_broken = None
        self.jar = tempfile.NamedTemporaryFile(suffix=".txt")  # real mtime
        self.enterContext(mock.patch.object(
            auth, "yt_dlp_cookiefile", return_value=self.jar.name))
        self.enterContext(mock.patch.object(
            ym.cache, "get_stream_url", return_value=None))
        self.put = self.enterContext(mock.patch.object(ym.cache, "put_stream_url"))
        self.addCleanup(self.jar.close)

    def _alive(self, url: str) -> bool:
        return url == LIVE_URL

    def test_gated_song_falls_through_to_working_client(self) -> None:
        """auth loses its formats, anon returns a 403 URL — web_music wins."""
        fake, calls = _fake_ydl({
            "auth": Exception("Requested format is not available"),
            "anon": DEAD_URL,
            "web_music": LIVE_URL,
        })
        with mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            url = ym.resolve_stream_url("vid1")
        self.assertEqual(url, LIVE_URL)
        self.assertEqual(calls, ["auth", "anon", "web_music"])

    def test_dead_urls_are_never_cached(self) -> None:
        """Only the URL that answered the probe may enter the disk cache."""
        fake, _ = _fake_ydl({
            "auth": DEAD_URL,
            "anon": DEAD_URL,
            "web_music": DEAD_URL,
            "android": LIVE_URL,
        })
        with mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            ym.resolve_stream_url("vid2")
        self.put.assert_called_once()
        self.assertEqual(self.put.call_args.args[2], LIVE_URL)

    def test_all_passes_dead_raises(self) -> None:
        fake, _ = _fake_ydl({
            "auth": DEAD_URL, "anon": DEAD_URL,
            "web_music": DEAD_URL, "android": DEAD_URL,
        })
        with mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            with self.assertRaises(RuntimeError):
                ym.resolve_stream_url("vid3")
        self.put.assert_not_called()

    def test_per_song_auth_error_does_not_break_the_auth_pass(self) -> None:
        """One gated song must not force every song anonymous for 30 min."""
        fake, calls = _fake_ydl({
            "auth": Exception("Requested format is not available"),
            "anon": LIVE_URL,
        })
        with mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            ym.resolve_stream_url("vid4")
            self.assertIsNone(ym._auth_pass_broken)
            ym.resolve_stream_url("vid5")
        # The second resolve still tried the auth pass first.
        self.assertEqual(calls, ["auth", "anon", "auth", "anon"])

    def test_bot_check_does_not_break_the_auth_pass(self) -> None:
        """'Sign in to confirm you're not a bot' is IP reputation, not a
        dead jar — it must not force every song anonymous for 30 min."""
        # Real message — note it names cookies as the *remedy*, so naive
        # "cookie" matching would misclassify it as a dead jar.
        fake, _ = _fake_ydl({
            "auth": Exception(
                "Sign in to confirm you're not a bot. Use "
                "--cookies-from-browser or --cookies for the authentication."),
            "anon": LIVE_URL,
        })
        with mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            ym.resolve_stream_url("vid8")
        self.assertIsNone(ym._auth_pass_broken)

    def test_dead_cached_url_is_dropped_and_reresolved(self) -> None:
        """A disk-cache hit must answer a probe or make way for a fresh
        resolve — returning it unprobed replays the failure until TTL."""
        fake, calls = _fake_ydl({"auth": LIVE_URL})
        remove = self.enterContext(
            mock.patch.object(ym.cache, "remove_stream_url"))
        with mock.patch.object(ym.cache, "get_stream_url",
                               return_value=DEAD_URL), \
                mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            url = ym.resolve_stream_url("vid9")
        self.assertEqual(url, LIVE_URL)
        remove.assert_called_once_with("ytmusic", "vid9")
        self.assertEqual(calls, ["auth"])

    def test_live_cached_url_short_circuits(self) -> None:
        fake, calls = _fake_ydl({})
        with mock.patch.object(ym.cache, "get_stream_url",
                               return_value=LIVE_URL), \
                mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            url = ym.resolve_stream_url("vid10")
        self.assertEqual(url, LIVE_URL)
        self.assertEqual(calls, [])

    def test_dead_jar_still_breaks_the_auth_pass(self) -> None:
        fake, calls = _fake_ydl({
            "auth": Exception("HTTP Error 401: Unauthorized"),
            "anon": LIVE_URL,
        })
        with mock.patch.object(ym.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(ym, "_stream_url_alive", self._alive):
            ym.resolve_stream_url("vid6")
            self.assertIsNotNone(ym._auth_pass_broken)
            ym.resolve_stream_url("vid7")
        # The second resolve skipped straight to anonymous.
        self.assertEqual(calls, ["auth", "anon", "anon"])


class RemoveStreamUrlTest(unittest.TestCase):
    """The player-error handler purges the disk layer, not just prefetch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.enterContext(mock.patch.object(
            cache.config, "CACHE_DIR", Path(self.tmp.name)))
        self.enterContext(mock.patch.dict(cache._mem, clear=True))
        self.addCleanup(self.tmp.cleanup)

    def test_removed_entry_is_gone_from_memory_and_disk(self) -> None:
        cache.put_stream_url("ytmusic", "vid1", DEAD_URL, ttl_seconds=3600)
        cache.put_stream_url("ytmusic", "vid2", LIVE_URL, ttl_seconds=3600)
        cache.remove_stream_url("ytmusic", "vid1")
        self.assertIsNone(cache.get_stream_url("ytmusic", "vid1"))
        # Survives a cold reload (i.e. the disk file was rewritten too).
        cache._mem.clear()
        self.assertIsNone(cache.get_stream_url("ytmusic", "vid1"))
        self.assertEqual(cache.get_stream_url("ytmusic", "vid2"), LIVE_URL)

    def test_removing_a_missing_entry_is_a_no_op(self) -> None:
        cache.remove_stream_url("ytmusic", "never-cached")  # must not raise


if __name__ == "__main__":
    unittest.main()
