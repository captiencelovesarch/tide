"""tide keeps its YouTube Music session alive itself (2026-10-07).

The old way: save a cookie snapshot at import and send it unchanged
forever. Google rotates the session's __Secure-*PSIDTS pair about every
ten minutes and stops honouring old ones, so the snapshot died within the
hour (it did, the day this was written: a silent browser re-import fired
an hour after launch). Now the import only seeds one live jar that
ytmusicapi and yt-dlp share, and a keeper rotates it on Google's schedule.

2026-11-01: rotating a session imported from a browser signed the browser
out (helium, 20 s after a refresh token). Only a session tide owns is
rotated now; a shared one follows its browser's cookie db.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_yt_session.py
"""
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from tide import auth, browser_import as bi, cache, config, yt_session

_COOKIES = {
    "__Secure-3PAPISID": "abc/def",
    "SAPISID": "abc/def",
    "SID": "sid-1",
    "__Secure-1PSIDTS": "ts-old",
    "__Secure-3PSIDTS": "ts-old",
}


def _reset() -> None:
    yt_session._jar = None
    yt_session._seeded_from = None
    yt_session._last_attempt = 0.0


def _live() -> dict:
    return {c.name: c.value for c in yt_session.jar()}


def _bump(path, seconds: int = 1) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 10**9))


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _rotating_post(status=200, body=')]}\'\n[["identity.hfcr",600],["di",57]]'):
    """A RotateCookies stand-in: sets the fresh pair on the session's jar
    the way requests does with the real Set-Cookie headers."""
    calls = []

    def post(self, url, **kw):
        calls.append((url, kw))
        if status == 200:
            n = len(calls)
            for name in ("__Secure-1PSIDTS", "__Secure-3PSIDTS"):
                self.cookies.set_cookie(yt_session._cookie(name, f"ts-new-{n}"))
        return _Resp(status, body)
    return post, calls


class _Base(unittest.TestCase):
    owned = False

    def setUp(self) -> None:
        auth.clear_saved_auth()
        _reset()
        auth.save_browser_auth(dict(_COOKIES), owned=self.owned)
        self.addCleanup(auth.clear_saved_auth)
        self.addCleanup(_reset)


class JarTests(_Base):
    def test_import_seeds_the_jar(self) -> None:
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-old")
        self.assertTrue(yt_session.jar_file().is_file())

    def test_jar_file_is_owner_only(self) -> None:
        yt_session.jar()
        mode = stat.S_IMODE(yt_session.jar_file().stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_changed_jar_survives_a_restart(self) -> None:
        yt_session.jar().set_cookie(yt_session._cookie("SID", "sid-2"))
        yt_session.save()
        _reset()                                  # a new process
        self.assertEqual(_live()["SID"], "sid-2")

    def test_a_new_import_reseeds_in_place(self) -> None:
        held = yt_session.jar()                   # what live sessions hold
        held.set_cookie(yt_session._cookie("SID", "rotated"))
        yt_session.save()
        auth.save_browser_auth({**_COOKIES, "SID": "reimported"})
        _bump(config.BROWSER_AUTH_FILE, 5)
        self.assertIs(yt_session.jar(), held)
        self.assertEqual(_live()["SID"], "reimported")

    def test_a_stale_jar_never_overwrites_a_fresh_import(self) -> None:
        yt_session.jar().set_cookie(yt_session._cookie("SID", "stale"))
        auth.save_browser_auth({**_COOKIES, "SID": "fresh"})
        _bump(config.BROWSER_AUTH_FILE, 5)
        yt_session.save()                         # the keeper's tick
        _reset()
        self.assertEqual(_live()["SID"], "fresh")

    def test_signed_out_is_none(self) -> None:
        auth.clear_saved_auth()
        self.assertIsNone(yt_session.jar())
        self.assertIsNone(yt_session.session())
        self.assertIsNone(auth.yt_dlp_cookiefile())

    def test_merge_takes_what_ytdlp_rotated(self) -> None:
        path = str(config.CONFIG_DIR / "snap.txt")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        yt_session.snapshot(path)
        text = open(path).read().replace("\tSID\tsid-1", "\tSID\tsid-9")
        open(path, "w").write(text)
        yt_session.merge(path)
        self.assertEqual(_live()["SID"], "sid-9")
        self.assertIn("\tSID\tsid-9", yt_session.jar_file().read_text())


class RotationTests(_Base):
    owned = True

    def test_rotation_refreshes_the_pair_and_records_the_interval(self) -> None:
        post, calls = _rotating_post()
        with mock.patch.object(requests.Session, "post", post):
            self.assertTrue(yt_session.rotate())
        self.assertEqual(calls[0][0], yt_session.ROTATE_URL)
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-new-1")
        self.assertIn("ts-new-1", yt_session.jar_file().read_text())
        self.assertAlmostEqual(yt_session.next_rotation_at() - yt_session.rotated_at(),
                               600.0)

    def test_not_due_is_skipped(self) -> None:
        post, calls = _rotating_post()
        with mock.patch.object(requests.Session, "post", post):
            yt_session.rotate()
            yt_session._last_attempt = 0.0        # the gap guard aside
            self.assertFalse(yt_session.rotate())
        self.assertEqual(len(calls), 1)

    def test_force_still_respects_googles_gap(self) -> None:
        post, calls = _rotating_post()
        with mock.patch.object(requests.Session, "post", post):
            yt_session.rotate()
            self.assertFalse(yt_session.rotate(force=True))
        self.assertEqual(len(calls), 1)

    def test_a_refusal_changes_nothing(self) -> None:
        post, _ = _rotating_post(status=429, body="")
        with mock.patch.object(requests.Session, "post", post):
            self.assertFalse(yt_session.rotate())
        self.assertIsNone(yt_session.rotated_at())
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-old")

    def test_offline_is_a_quiet_false(self) -> None:
        def boom(self, url, **kw):
            raise requests.ConnectionError("offline")
        with mock.patch.object(requests.Session, "post", boom):
            self.assertFalse(yt_session.rotate())

    def test_signed_out_never_calls_google(self) -> None:
        auth.clear_saved_auth()
        post, calls = _rotating_post()
        with mock.patch.object(requests.Session, "post", post):
            self.assertFalse(yt_session.rotate(force=True))
        self.assertEqual(calls, [])

    def test_sign_out_forgets_the_rotation_record(self) -> None:
        post, _ = _rotating_post()
        with mock.patch.object(requests.Session, "post", post):
            yt_session.rotate()
        auth.clear_saved_auth()
        self.assertIsNone(yt_session.rotated_at())


class ClientTests(_Base):
    owned = True

    def setUp(self) -> None:
        super().setUp()
        cache.clear_namespace(auth._VISITOR_NS)
        self.enterContext(mock.patch(
            "ytmusicapi.ytmusic.get_visitor_id",
            lambda f: {"X-Goog-Visitor-Id": "VISITOR123"}))

    def test_ytmusicapi_sends_the_live_jar(self) -> None:
        yt = auth.yt_client()
        self.assertNotIn("cookie", yt.base_headers)      # no frozen snapshot
        self.assertIs(yt._session.cookies, yt_session.jar())
        self.assertIn("SAPISIDHASH", yt.headers["authorization"])

    def test_a_rotation_reaches_a_client_built_before_it(self) -> None:
        yt = auth.yt_client()
        post, _ = _rotating_post()
        with mock.patch.object(requests.Session, "post", post):
            yt_session.rotate()
        sent = {c.name: c.value for c in yt._session.cookies}
        self.assertEqual(sent["__Secure-3PSIDTS"], "ts-new-1")


class SharedSessionTests(_Base):
    """an import from the user's browser is the browser's session too"""

    def test_a_shared_session_is_never_rotated(self) -> None:
        self.assertFalse(yt_session.owned())
        post, calls = _rotating_post()
        with mock.patch.object(requests.Session, "post", post), \
                mock.patch.object(bi, "available_profiles", lambda: []):
            self.assertFalse(yt_session.rotate(force=True))
            yt_session.freshen()
        self.assertEqual(calls, [])
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-old")

    def test_a_new_shared_import_drops_an_old_owned_flag(self) -> None:
        auth.save_browser_auth(dict(_COOKIES), owned=True)
        self.assertTrue(yt_session.owned())
        auth.save_browser_auth(dict(_COOKIES))
        self.assertFalse(yt_session.owned())


class FollowTests(_Base):
    """a shared session takes the browser's rotations from its cookie db"""

    def setUp(self) -> None:
        super().setUp()
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mine = tmp / "helium.db"
        self.other = tmp / "chromium.db"
        self.browser = {}
        self._set(self.other, {**_COOKIES, "SID": "sid-other"})
        self._set(self.mine, dict(_COOKIES), bump=2)      # helium used last
        self.profiles = [
            bi.BrowserProfile(slug="helium", label="helium",
                              cookies_path=self.mine, candidates=(self.mine,)),
            bi.BrowserProfile(slug="chromium", label="chromium",
                              cookies_path=self.other, candidates=(self.other,)),
        ]
        self.reads = []
        self.broken = set()

        def import_cookies(profile):
            self.reads.append(profile.cookies_path)
            if profile.cookies_path in self.broken:
                raise bi.ImportError_("locked")
            return bi.ImportResult(profile=profile,
                                   cookies=dict(self.browser[profile.cookies_path]))

        self.enterContext(mock.patch.object(bi, "available_profiles",
                                            lambda: list(self.profiles)))
        self.enterContext(mock.patch.object(bi, "import_cookies", import_cookies))

    def _set(self, db: Path, cookies: dict, bump: int = 5) -> None:
        """what the browser holds; its db gets the youtube rows a chromium
        db has (encrypted_value standing in for the real ciphertext)"""
        self.browser[db] = cookies
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS cookies (host_key, name, "
                    "value, encrypted_value)")
        con.execute("DELETE FROM cookies")
        con.executemany("INSERT INTO cookies VALUES ('.youtube.com', ?, '', ?)",
                        [(k, ("enc:" + v).encode()) for k, v in cookies.items()])
        con.commit()
        con.close()
        _bump(db, bump)

    def _browser_rotates(self, ts: str = "ts-browser") -> None:
        self._set(self.mine, {**self.browser[self.mine],
                              "__Secure-1PSIDTS": ts, "__Secure-3PSIDTS": ts})

    def _following(self):
        return (yt_session._load_meta().get("follow") or {}).get("db")

    def test_finds_the_browser_holding_this_sign_in(self) -> None:
        yt_session.follow()
        self.assertEqual(self._following(), str(self.mine))

    def test_the_browsers_rotation_reaches_tide(self) -> None:
        held = yt_session.session()               # a client built before it
        yt_session.follow()
        self._browser_rotates()
        self.assertTrue(yt_session.follow())
        self.assertEqual(_live()["__Secure-3PSIDTS"], "ts-browser")
        sent = {c.name: c.value for c in held.cookies}
        self.assertEqual(sent["__Secure-1PSIDTS"], "ts-browser")
        self.assertIsNotNone(yt_session.refreshed_at())
        self.assertIn("\tts-browser", yt_session.jar_file().read_text())

    def test_an_unchanged_db_is_not_read_again(self) -> None:
        yt_session.follow()
        self.reads.clear()
        self.assertFalse(yt_session.follow())
        self.assertEqual(self.reads, [])

    def test_a_db_change_without_a_rotation_skips_the_full_read(self) -> None:
        yt_session.follow()
        self.reads.clear()
        _bump(self.mine, 5)                       # some other site's cookie
        self.assertFalse(yt_session.follow())
        self.assertEqual(self.reads, [])
        self._browser_rotates()                   # and the next one counts
        self.assertTrue(yt_session.follow())

    def test_a_browser_signed_in_as_someone_else_is_let_go(self) -> None:
        yt_session.follow()
        self._set(self.mine, {**_COOKIES, "SID": "sid-new",
                              "__Secure-1PSIDTS": "ts-x"})
        self.assertFalse(yt_session.follow())
        self.assertEqual(_live()["SID"], "sid-1")
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-old")
        self.assertIsNone(self._following())

    def test_a_signed_out_browser_is_let_go(self) -> None:
        yt_session.follow()
        self._set(self.mine, {"__Secure-3PSID": "x", "__Secure-1PSIDTS": "y"})
        self.assertFalse(yt_session.follow())
        self.assertEqual(_live()["SID"], "sid-1")
        self.assertIsNone(self._following())

    def test_a_search_skips_signed_out_browsers(self) -> None:
        self.profiles.pop(0)
        self._set(self.other, {"__Secure-3PSID": "x"})
        self.assertFalse(yt_session.follow())
        self.assertEqual(self.reads, [])

    def test_no_browser_with_it_waits_to_be_asked_again(self) -> None:
        self.profiles.pop(0)
        self.assertFalse(yt_session.follow())
        self.assertIsNone(self._following())
        self.profiles.insert(0, bi.BrowserProfile(
            slug="helium", label="helium", cookies_path=self.mine,
            candidates=(self.mine,)))
        self.reads.clear()
        self.assertFalse(yt_session.follow())     # the keeper's tick
        self.assertEqual(self.reads, [])
        yt_session.follow(look="again")           # the next run's first tick
        self.assertEqual(self._following(), str(self.mine))

    def test_a_new_import_looks_again(self) -> None:
        yt_session.follow()
        self._set(self.mine, {**_COOKIES, "SID": "sid-new"})
        self._set(self.other, dict(_COOKIES), bump=10)
        auth.save_browser_auth(dict(_COOKIES))
        _bump(config.BROWSER_AUTH_FILE, 5)
        yt_session.follow()
        self.assertEqual(self._following(), str(self.other))

    def test_tide_losing_its_sid_still_follows(self) -> None:
        yt_session.follow()
        yt_session.jar().clear(".youtube.com", "/", "SID")
        self._browser_rotates()
        self.assertTrue(yt_session.follow())
        self.assertEqual(_live()["SID"], "sid-1")

    def test_an_unreadable_db_is_tried_again(self) -> None:
        yt_session.follow()
        self.broken.add(self.mine)
        self._browser_rotates()
        self.assertFalse(yt_session.follow())
        self.assertEqual(self._following(), str(self.mine))
        self.broken.clear()
        self.assertTrue(yt_session.follow())
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-browser")

    def test_freshen_catches_up_after_tide_was_closed(self) -> None:
        yt_session.follow()
        _reset()                                  # tide quits
        self._browser_rotates()                   # helium carries on
        yt_session.freshen()                      # the next launch
        self.assertEqual(_live()["__Secure-1PSIDTS"], "ts-browser")

    def test_freshen_never_searches(self) -> None:
        yt_session.freshen()                      # launch doesn't wait on it
        self.assertEqual(self.reads, [])
        self.assertIsNone(self._following())


if __name__ == "__main__":
    unittest.main()
