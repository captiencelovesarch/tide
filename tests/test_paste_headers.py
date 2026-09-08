"""The manual header-paste sign-in fallback.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tide import auth


# A realistic Chrome "copy request headers" dump: pseudo-headers, the cookie
# line carrying httpOnly auth cookies, a user-agent, unrelated headers.
CHROME_PASTE = """\
:authority: music.youtube.com
:method: POST
:path: /youtubei/v1/browse
accept: */*
cookie: VISITOR_INFO1_LIVE=abc; __Secure-3PAPISID=the-secret; SID=sid-value; SAPISID=sap
user-agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0
x-goog-authuser: 0
"""


class ParseTest(unittest.TestCase):
    def test_chrome_paste(self) -> None:
        cookies, ua = auth.parse_pasted_headers(CHROME_PASTE)
        self.assertEqual(cookies["__Secure-3PAPISID"], "the-secret")
        self.assertEqual(cookies["SID"], "sid-value")
        self.assertEqual(cookies["VISITOR_INFO1_LIVE"], "abc")
        self.assertEqual(ua, "Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0")

    def test_capitalised_header_name(self) -> None:
        cookies, _ = auth.parse_pasted_headers(
            "Cookie: __Secure-3PAPISID=x; SID=y\r\nUser-Agent: FF"
        )
        self.assertEqual(cookies["__Secure-3PAPISID"], "x")

    def test_bare_cookie_string(self) -> None:
        cookies, ua = auth.parse_pasted_headers(
            "SID=1; __Secure-3PAPISID=only-the-cookie; SAPISID=2"
        )
        self.assertEqual(cookies["__Secure-3PAPISID"], "only-the-cookie")
        self.assertIsNone(ua)

    def test_value_may_contain_equals_and_colon(self) -> None:
        cookies, _ = auth.parse_pasted_headers(
            "cookie: __Secure-3PAPISID=a=b=c; TOKEN=http://x"
        )
        self.assertEqual(cookies["__Secure-3PAPISID"], "a=b=c")
        self.assertEqual(cookies["TOKEN"], "http://x")

    def test_no_cookie_header_is_a_clear_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            auth.parse_pasted_headers("accept: */*\nuser-agent: FF")
        self.assertIn("cookie header", str(cm.exception))

    def test_signed_out_paste_names_the_missing_cookie(self) -> None:
        with self.assertRaises(ValueError) as cm:
            auth.parse_pasted_headers("cookie: VISITOR_INFO1_LIVE=abc; PREF=x")
        self.assertIn(auth.REQUIRED_COOKIE, str(cm.exception))

    def test_set_cookie_response_line_is_ignored(self) -> None:
        # A response paste has set-cookie, not cookie — must not be mistaken
        # for a session.
        with self.assertRaises(ValueError):
            auth.parse_pasted_headers("set-cookie: __Secure-3PAPISID=x; Path=/")


class WizardWiringTest(unittest.TestCase):
    """The dialog handler, driven directly so no modal is opened."""

    def setUp(self) -> None:
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        self._tmp = TemporaryDirectory()
        d = Path(self._tmp.name)
        for attr, value in (
            ("CONFIG_DIR", d),
            ("BROWSER_AUTH_FILE", d / "browser.json"),
            ("OAUTH_FILE", d / "oauth.json"),
        ):
            p = mock.patch.object(auth.config, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _dialog(self):
        from tide.ui.wizard import SignInDialog
        return SignInDialog()

    def test_good_paste_saves_and_accepts(self) -> None:
        dlg = self._dialog()
        saved = {}
        with mock.patch.object(auth, "save_browser_auth",
                               side_effect=lambda c, **k: saved.update(cookies=c, kw=k)):
            dlg._import_pasted(CHROME_PASTE)
        self.assertEqual(saved["cookies"]["__Secure-3PAPISID"], "the-secret")
        self.assertEqual(saved["kw"]["user_agent"], "Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0")
        self.assertIsNone(saved["kw"]["expires_at"])
        self.assertEqual(dlg.result(), dlg.DialogCode.Accepted)

    def test_bad_paste_reports_and_stays_open(self) -> None:
        dlg = self._dialog()
        with mock.patch.object(auth, "save_browser_auth") as save:
            dlg._import_pasted("accept: */*")
        save.assert_not_called()
        self.assertIn("cookie header", dlg._status.text())
        self.assertNotEqual(dlg.result(), dlg.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
