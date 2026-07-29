"""Cookie-expiry capture and the countdown helpers behind the expiry warning.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tide import auth, browser_import as bi


class ChromeTimeTest(unittest.TestCase):
    def test_epoch_conversion(self) -> None:
        # 2026-01-01T00:00:00Z == 1767225600 unix
        chrome = (1767225600 + 11644473600) * 1_000_000
        self.assertAlmostEqual(bi._chrome_time_to_unix(chrome), 1767225600, places=3)

    def test_zero_means_session_cookie(self) -> None:
        self.assertIsNone(bi._chrome_time_to_unix(0))


class ExpiryHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(auth.config, "CONFIG_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write_meta(self, expires_at) -> None:
        (self.dir / "browser_meta.json").write_text(
            json.dumps({"expires_at": expires_at}), encoding="utf-8"
        )

    def test_unknown_when_no_meta_file(self) -> None:
        self.assertIsNone(auth.session_expires_at())
        self.assertIsNone(auth.seconds_until_expiry())

    def test_unknown_when_expiry_is_null(self) -> None:
        """Session-scoped cookies record None — that is 'no data', and must
        never be mistaken for 'expired'."""
        self._write_meta(None)
        self.assertIsNone(auth.seconds_until_expiry())

    def test_countdown(self) -> None:
        self._write_meta(time.time() + 3600)
        remaining = auth.seconds_until_expiry()
        self.assertIsNotNone(remaining)
        self.assertTrue(3500 < remaining <= 3600, remaining)

    def test_negative_once_past(self) -> None:
        self._write_meta(time.time() - 60)
        self.assertLess(auth.seconds_until_expiry(), 0)

    def test_corrupt_meta_is_unknown_not_a_crash(self) -> None:
        (self.dir / "browser_meta.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(auth.session_expires_at())


class SaveAndClearTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(auth.config, "CONFIG_DIR", self.dir)
        patcher.start()
        auth_file = mock.patch.object(
            auth.config, "BROWSER_AUTH_FILE", self.dir / "browser.json"
        )
        auth_file.start()
        oauth = mock.patch.object(auth.config, "OAUTH_FILE", self.dir / "oauth.json")
        oauth.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(auth_file.stop)
        self.addCleanup(oauth.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_expiry_is_stored_beside_not_inside_browser_json(self) -> None:
        """browser.json is read by ytmusicapi as a literal headers dict — an
        extra key would be sent as a bogus HTTP header."""
        expires = time.time() + 86400
        auth.save_browser_auth({"__Secure-3PAPISID": "x"}, expires_at=expires)
        headers = json.loads((self.dir / "browser.json").read_text())
        self.assertNotIn("expires_at", headers)
        self.assertAlmostEqual(auth.session_expires_at(), expires, places=3)

    def test_secrets_are_owner_only(self) -> None:
        auth.save_browser_auth({"__Secure-3PAPISID": "x"}, expires_at=1.0)
        for name in ("browser.json", "browser_meta.json"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600, name)

    def test_clear_removes_the_sidecar(self) -> None:
        auth.save_browser_auth({"__Secure-3PAPISID": "x"}, expires_at=1.0)
        auth.clear_saved_auth()
        self.assertFalse((self.dir / "browser_meta.json").exists())
        self.assertIsNone(auth.session_expires_at())


class RefreshFromBrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for attr, value in (
            ("CONFIG_DIR", self.dir),
            ("BROWSER_AUTH_FILE", self.dir / "browser.json"),
        ):
            p = mock.patch.object(auth.config, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_skips_signed_out_profiles_and_takes_the_live_one(self) -> None:
        dead = mock.Mock(label="chromium")
        live = mock.Mock(label="brave")
        results = {
            dead: bi.ImportResult(profile=dead, cookies={}),
            live: bi.ImportResult(
                profile=live,
                cookies={"__Secure-3PAPISID": "v"},
                expires_at=time.time() + 600,
            ),
        }
        with mock.patch.object(bi, "available_profiles", return_value=[dead, live]), \
             mock.patch.object(bi, "import_cookies", side_effect=lambda p: results[p]):
            self.assertEqual(auth.refresh_from_browser(), "brave")
        self.assertIsNotNone(auth.session_expires_at())

    def test_returns_none_when_no_browser_has_a_session(self) -> None:
        dead = mock.Mock(label="chromium")
        with mock.patch.object(bi, "available_profiles", return_value=[dead]), \
             mock.patch.object(
                 bi, "import_cookies",
                 return_value=bi.ImportResult(profile=dead, cookies={})):
            self.assertIsNone(auth.refresh_from_browser())

    def test_one_unreadable_profile_does_not_block_the_next(self) -> None:
        broken = mock.Mock(label="chromium")
        live = mock.Mock(label="brave")

        def _import(p):
            if p is broken:
                raise RuntimeError("locked cookie db")
            return bi.ImportResult(profile=p, cookies={"__Secure-3PAPISID": "v"})

        with mock.patch.object(bi, "available_profiles", return_value=[broken, live]), \
             mock.patch.object(bi, "import_cookies", side_effect=_import):
            self.assertEqual(auth.refresh_from_browser(), "brave")


if __name__ == "__main__":
    unittest.main()
