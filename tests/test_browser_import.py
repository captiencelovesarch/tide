"""The browser cookie importer with yt-dlp's extractor mocked out.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import http.cookiejar
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tide import browser_import as bi


# 2026-01-01T00:00:00Z as Chromium counts it (microseconds since 1601).
UNIX_2026 = 1767225600
CHROME_2026 = (UNIX_2026 + 11644473600) * 1_000_000


def _cookie(name, value, domain=".youtube.com", expires=None):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
        path="/", path_specified=True, secure=True, expires=expires,
        discard=expires is None, comment=None, comment_url=None, rest={},
    )


def _profile(slug="chromium", family="chromium", candidates=("/x/Default/Cookies",)):
    paths = tuple(Path(p) for p in candidates)
    return bi.BrowserProfile(slug=slug, label=slug, cookies_path=paths[0],
                             family=family, candidates=paths)


class JarScopingTest(unittest.TestCase):
    def test_only_what_a_browser_sends_music_youtube_com(self) -> None:
        jar = [
            _cookie("__Secure-3PAPISID", "yt", ".youtube.com"),
            _cookie("host_only", "m", "music.youtube.com"),
            _cookie("SID", "google-value", ".google.com"),
            _cookie("www_only", "w", "www.youtube.com"),
            _cookie("lookalike", "x", ".notyoutube.com"),
        ]
        result = bi._result_from_jar(_profile(), jar)
        self.assertEqual(set(result.cookies), {"__Secure-3PAPISID", "host_only"})
        self.assertTrue(result.looks_signed_in)

    def test_duplicate_names_keep_the_one_that_expires_last(self) -> None:
        jar = [
            _cookie("SID", "old", expires=CHROME_2026),
            _cookie("SID", "new", expires=CHROME_2026 + 10 * 86400 * 1_000_000),
            _cookie("YSC", "dated", expires=CHROME_2026),
            _cookie("YSC", "session"),
        ]
        result = bi._result_from_jar(_profile(), jar)
        self.assertEqual(result.cookies["SID"], "new")
        self.assertEqual(result.cookies["YSC"], "dated")

    def test_empty_values_are_dropped(self) -> None:
        result = bi._result_from_jar(_profile(), [_cookie("__Secure-3PAPISID", "")])
        self.assertFalse(result.looks_signed_in)


class ExpiryTest(unittest.TestCase):
    def test_chromium_microseconds_become_unix(self) -> None:
        self.assertAlmostEqual(bi._cookie_expiry_unix(CHROME_2026), UNIX_2026, places=3)

    def test_firefox_seconds_pass_through(self) -> None:
        self.assertEqual(bi._cookie_expiry_unix(UNIX_2026), UNIX_2026)

    def test_session_cookie_has_no_expiry(self) -> None:
        self.assertIsNone(bi._cookie_expiry_unix(None))
        self.assertIsNone(bi._cookie_expiry_unix(0))

    def test_session_is_the_earliest_auth_cookie(self) -> None:
        jar = [
            _cookie("__Secure-3PAPISID", "a", expires=CHROME_2026 + 5 * 86400 * 1_000_000),
            _cookie("SID", "b", expires=CHROME_2026),
            _cookie("PREF", "c", expires=CHROME_2026 - 86400 * 1_000_000),  # not an auth cookie
        ]
        result = bi._result_from_jar(_profile(), jar)
        self.assertAlmostEqual(result.expires_at, UNIX_2026, places=3)

    def test_all_session_scoped_auth_cookies_means_unknown(self) -> None:
        result = bi._result_from_jar(_profile(), [_cookie("__Secure-3PAPISID", "a")])
        self.assertTrue(result.looks_signed_in)
        self.assertIsNone(result.expires_at)


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _touch(self, rel: str, age: float = 0.0) -> Path:
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        when = time.time() - age
        os.utime(p, (when, when))
        return p

    def test_finds_cookie_dbs_in_picker_order(self) -> None:
        ff = self._touch(".mozilla/firefox/abc.default-release/cookies.sqlite")
        chromium = self._touch(".config/chromium/Default/Network/Cookies")
        (self.home / ".config/vivaldi").mkdir(parents=True)      # installed, never run
        profiles = bi.available_profiles()
        self.assertEqual([p.slug for p in profiles], ["chromium", "firefox"])
        self.assertEqual(profiles[0].cookies_path, chromium)
        self.assertEqual(profiles[0].family, "chromium")
        self.assertEqual(profiles[1].cookies_path, ff)
        self.assertEqual(profiles[1].family, "firefox")

    def test_every_profile_is_a_candidate_newest_first(self) -> None:
        old = self._touch(".config/chromium/Default/Cookies", age=3600)
        new = self._touch(".config/chromium/Profile 1/Network/Cookies", age=60)
        flatpak = self._touch(".var/app/org.chromium.Chromium/config/chromium/Default/Cookies", age=600)
        (chromium,) = bi.available_profiles()
        self.assertEqual(chromium.candidates, (new, flatpak, old))
        self.assertEqual(chromium.cookies_path, new)

    def test_nothing_installed(self) -> None:
        self.assertEqual(bi.available_profiles(), [])


class ImportTest(unittest.TestCase):
    """yt-dlp is mocked at the module seam; what is under test is how tide
    drives it and reads its answers."""

    def _signed_in(self):
        return [_cookie("__Secure-3PAPISID", "v", expires=CHROME_2026)]

    def test_hands_ytdlp_the_profile_dir_and_scopes_the_jar(self) -> None:
        with mock.patch.object(bi, "extract_cookies_from_browser",
                               return_value=self._signed_in()) as ext:
            result = bi.import_cookies(_profile(candidates=("/x/Default/Network/Cookies",)))
        self.assertTrue(result.looks_signed_in)
        ext.assert_called_once()
        args, kwargs = ext.call_args
        self.assertEqual(args[0], "chromium")
        self.assertEqual(kwargs["profile"], "/x/Default/Network")
        self.assertIsNone(kwargs["keyring"])

    def test_next_profile_db_when_the_first_is_signed_out(self) -> None:
        answers = iter([[], self._signed_in()])
        with mock.patch.object(bi, "extract_cookies_from_browser",
                               side_effect=lambda *a, **k: next(answers)) as ext:
            result = bi.import_cookies(_profile(candidates=("/x/Default/Cookies", "/x/Profile 1/Cookies")))
        self.assertTrue(result.looks_signed_in)
        self.assertEqual(ext.call_count, 2)
        self.assertEqual(ext.call_args_list[1].kwargs["profile"], "/x/Profile 1")

    def test_signed_out_everywhere_reports_signed_out(self) -> None:
        with mock.patch.object(bi, "extract_cookies_from_browser", return_value=[]):
            result = bi.import_cookies(_profile(candidates=("/x/a/Cookies", "/x/b/Cookies")))
        self.assertFalse(result.looks_signed_in)
        self.assertEqual(result.note, "")

    def test_extractor_failure_becomes_import_error(self) -> None:
        with mock.patch.object(bi, "extract_cookies_from_browser",
                               side_effect=FileNotFoundError("no db")):
            with self.assertRaises(bi.ImportError_) as cm:
                bi.import_cookies(_profile())
        self.assertIn("no db", str(cm.exception))

    def test_one_broken_db_does_not_hide_a_live_one(self) -> None:
        def _extract(slug, profile=None, logger=None, keyring=None):
            if profile == "/x/a":
                raise RuntimeError("locked")
            return self._signed_in()
        with mock.patch.object(bi, "extract_cookies_from_browser", side_effect=_extract):
            result = bi.import_cookies(_profile(candidates=("/x/a/Cookies", "/x/b/Cookies")))
        self.assertTrue(result.looks_signed_in)


class KeyringRetryTest(unittest.TestCase):
    def _extract_factory(self, unlock_with: str | None):
        """yt-dlp stand-in: the auto pass picks KWALLET6 and can't decrypt;
        only ``unlock_with`` yields the session."""
        calls: list[str | None] = []

        def _extract(slug, profile=None, logger=None, keyring=None):
            calls.append(keyring)
            if keyring is None:
                logger.debug("Chosen keyring: KWALLET6")
                logger.warning("cannot decrypt v11 cookies: no key found", only_once=True)
                logger.info("Extracted 3 cookies from chromium (40 could not be decrypted)")
                return [_cookie("PREF", "x")]
            if keyring == unlock_with:
                return [_cookie("__Secure-3PAPISID", "v")]
            return []
        return _extract, calls

    def test_tries_the_other_keyrings_and_skips_the_one_already_tried(self) -> None:
        extract, calls = self._extract_factory(unlock_with="GNOMEKEYRING")
        with mock.patch.object(bi, "extract_cookies_from_browser", side_effect=extract):
            result = bi.import_cookies(_profile())
        self.assertTrue(result.looks_signed_in)
        self.assertEqual(calls, [None, "KWALLET5", "GNOMEKEYRING"])

    def test_no_keyring_works_notes_why(self) -> None:
        extract, calls = self._extract_factory(unlock_with=None)
        with mock.patch.object(bi, "extract_cookies_from_browser", side_effect=extract):
            result = bi.import_cookies(_profile())
        self.assertFalse(result.looks_signed_in)
        self.assertIn("keyring", result.note)
        self.assertNotIn("KWALLET6", calls[1:])
        self.assertNotIn("BASICTEXT", calls)

    def test_plain_signed_out_does_not_touch_other_keyrings(self) -> None:
        def _extract(slug, profile=None, logger=None, keyring=None):
            logger.debug("Chosen keyring: KWALLET6")
            logger.info("Extracted 300 cookies from chromium")
            return [_cookie("PREF", "x")]
        with mock.patch.object(bi, "extract_cookies_from_browser", side_effect=_extract) as ext:
            result = bi.import_cookies(_profile())
        self.assertFalse(result.looks_signed_in)
        self.assertEqual(ext.call_count, 1)
        self.assertEqual(result.note, "")

    def test_firefox_has_no_keyring_to_retry(self) -> None:
        def _extract(slug, profile=None, logger=None, keyring=None):
            logger.info("Extracted 0 cookies from firefox (5 could not be decrypted)")
            return []
        ff = _profile(slug="firefox", family="firefox", candidates=("/x/abc.default/cookies.sqlite",))
        with mock.patch.object(bi, "extract_cookies_from_browser", side_effect=_extract) as ext:
            bi.import_cookies(ff)
        self.assertEqual(ext.call_count, 1)

    def test_a_keyring_pass_that_raises_moves_on(self) -> None:
        def _extract(slug, profile=None, logger=None, keyring=None):
            if keyring is None:
                logger.debug("Chosen keyring: GNOMEKEYRING")
                logger.error("failed to read from keyring")
                return []
            if keyring == "KWALLET6":
                raise RuntimeError("kwallet-query exploded")
            if keyring == "KWALLET5":
                return [_cookie("__Secure-3PAPISID", "v")]
            return []
        with mock.patch.object(bi, "extract_cookies_from_browser", side_effect=_extract):
            result = bi.import_cookies(_profile())
        self.assertTrue(result.looks_signed_in)


if __name__ == "__main__":
    unittest.main()
