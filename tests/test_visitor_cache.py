"""Launch must not wait on music.youtube.com for the visitor id.

ytmusicapi downloads the whole YT Music page on construction to read
VISITOR_DATA unless the headers already carry X-Goog-Visitor-Id. That ran
on the GUI thread before the window came up (~1.2s measured, no timeout),
so auth.yt_client caches the id per sign-in.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import json
import os
import unittest
from unittest import mock

from tide import auth, cache, config

_HEADERS = {
    "cookie": "__Secure-3PAPISID=abc/def; SAPISID=abc/def",
    "origin": "https://music.youtube.com",
    "user-agent": "Mozilla/5.0",
    "authorization": "SAPISIDHASH 1_abc",
    "x-goog-authuser": "0",
}


class VisitorCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config.BROWSER_AUTH_FILE.write_text(json.dumps(_HEADERS))
        self.addCleanup(config.BROWSER_AUTH_FILE.unlink, missing_ok=True)
        cache.clear_namespace(auth._VISITOR_NS)
        self.fetches = []

        def fake_visitor(request_func):
            self.fetches.append(1)
            return {"X-Goog-Visitor-Id": "VISITOR123"}

        self.enterContext(mock.patch("ytmusicapi.ytmusic.get_visitor_id", fake_visitor))

    def test_second_client_skips_the_fetch(self) -> None:
        first = auth.yt_client()
        self.assertEqual(first.base_headers["X-Goog-Visitor-Id"], "VISITOR123")
        second = auth.yt_client()
        self.assertEqual(second.base_headers["X-Goog-Visitor-Id"], "VISITOR123")
        self.assertEqual(len(self.fetches), 1)

    def test_new_sign_in_fetches_its_own_id(self) -> None:
        auth.yt_client()
        st = config.BROWSER_AUTH_FILE.stat()
        os.utime(config.BROWSER_AUTH_FILE, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        auth.yt_client()
        self.assertEqual(len(self.fetches), 2)

    def test_empty_id_is_not_cached(self) -> None:
        with mock.patch("ytmusicapi.ytmusic.get_visitor_id",
                        lambda f: self.fetches.append(1) or {"X-Goog-Visitor-Id": ""}):
            auth.yt_client()
            auth.yt_client()
        self.assertEqual(len(self.fetches), 2)


if __name__ == "__main__":
    unittest.main()
