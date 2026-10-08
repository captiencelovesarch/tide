"""tide keeps its YouTube Music session alive itself (2026-10-07).

The old way: save a cookie snapshot at import and send it unchanged
forever. Google rotates the session's __Secure-*PSIDTS pair about every
ten minutes and stops honouring old ones, so the snapshot died within the
hour (it did, the day this was written: a silent browser re-import fired
an hour after launch). Now the import only seeds one live jar that
ytmusicapi and yt-dlp share, and a keeper rotates it on Google's schedule.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_yt_session.py
"""
import os
import stat
import unittest
from unittest import mock

import requests

from tide import auth, cache, config, yt_session

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
    def setUp(self) -> None:
        auth.clear_saved_auth()
        _reset()
        auth.save_browser_auth(dict(_COOKIES))
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


if __name__ == "__main__":
    unittest.main()
