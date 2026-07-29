"""A dead YT Music session must announce itself.

The bug: YouTube does not always 401 an expired cookie jar. It answers HTTP
200 with the *signed-out* payload — the account menu comes back with no
activeAccountHeaderRenderer — so the 401-shaped sentinel never fired, the
source kept reporting "signed in", and the user just started getting
anonymous results mid-session with no explanation.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import unittest
from unittest import mock

from tide.sources import ytmusic as ym


# Abridged from a real signed-out response captured from music.youtube.com.
SIGNED_OUT_KEYERROR = KeyError(
    "Unable to find 'header' using path ['actions', 0, 'openPopupAction', "
    "'popup', 'multiPageMenuRenderer', 'header', 'activeAccountHeaderRenderer', "
    "'accountName', 'runs', 0, 'text'] on {'sections': [...]}, exception: 'header'"
)


class ClassifierTest(unittest.TestCase):
    def test_signed_out_payload_recognised(self) -> None:
        self.assertTrue(ym._is_signed_out_payload(SIGNED_OUT_KEYERROR))

    def test_not_confused_with_a_401(self) -> None:
        self.assertFalse(ym._is_signed_out_payload(RuntimeError("HTTP 401")))

    def test_unrelated_parse_errors_are_not_sign_out(self) -> None:
        """A YouTube layout change elsewhere must not nag the user to re-auth."""
        for exc in (
            KeyError("Unable to find 'contents' using path ['contents', 0]"),
            KeyError("musicShelfRenderer"),
            ValueError("expecting value: line 1 column 1"),
        ):
            with self.subTest(exc=exc):
                self.assertFalse(ym._is_signed_out_payload(exc))

    def test_401_still_classified(self) -> None:
        self.assertTrue(
            ym._is_auth_error(RuntimeError("Server returned HTTP 401: Unauthorized."))
        )


def _source(account_info=None, raises=None) -> ym.YTMusicSource:
    client = mock.Mock()
    if raises is not None:
        client.get_account_info.side_effect = raises
    else:
        client.get_account_info.return_value = account_info
    return ym.YTMusicSource(client)


class ProbeAuthTest(unittest.TestCase):
    def test_signed_out_keyerror_flips_expired(self) -> None:
        src = _source(raises=SIGNED_OUT_KEYERROR)
        with mock.patch("tide.sources.registry") as reg:
            with self.assertRaises(KeyError):
                src.probe_auth()
            reg.return_value.notify_auth_expired.assert_called_once_with("ytmusic")
        self.assertFalse(src.is_authenticated())

    def test_nameless_account_flips_expired(self) -> None:
        """Parsed cleanly but anonymous — still a dead session."""
        src = _source(account_info={"accountName": ""})
        with mock.patch("tide.sources.registry") as reg:
            with self.assertRaises(RuntimeError):
                src.probe_auth()
            reg.return_value.notify_auth_expired.assert_called_once_with("ytmusic")
        self.assertFalse(src.is_authenticated())

    def test_live_session_stays_authenticated_and_records_the_check(self) -> None:
        src = _source(account_info={"accountName": "captience"})
        with mock.patch("tide.sources.registry") as reg:
            src.probe_auth()
            reg.return_value.notify_auth_expired.assert_not_called()
        self.assertTrue(src.is_authenticated())
        self.assertIsNotNone(src._last_auth_ok)

    def test_network_blip_does_not_sign_anyone_out(self) -> None:
        src = _source(raises=OSError("connection reset by peer"))
        with mock.patch("tide.sources.registry") as reg:
            with self.assertRaises(OSError):
                src.probe_auth()
            reg.return_value.notify_auth_expired.assert_not_called()
        self.assertTrue(src.is_authenticated())

    def test_expiry_is_announced_only_once(self) -> None:
        src = _source(raises=SIGNED_OUT_KEYERROR)
        with mock.patch("tide.sources.registry") as reg:
            for _ in range(3):
                with self.assertRaises(KeyError):
                    src.probe_auth()
            self.assertEqual(reg.return_value.notify_auth_expired.call_count, 1)


class StatusTextTest(unittest.TestCase):
    def test_expired_points_at_the_refresh_action(self) -> None:
        src = _source(raises=SIGNED_OUT_KEYERROR)
        with mock.patch("tide.sources.registry"):
            with self.assertRaises(KeyError):
                src.probe_auth()
        self.assertIn("refresh token", src.status_text())

    def test_a_far_off_cookie_deadline_is_not_advertised(self) -> None:
        """394 days of nominal cookie life says nothing about whether the
        session is alive, so quoting it would be false reassurance."""
        src = _source(account_info={"accountName": "captience"})
        with mock.patch("tide.sources.registry"):
            src.probe_auth()
        with mock.patch("tide.auth.seconds_until_expiry", return_value=394 * 86400):
            text = src.status_text()
        self.assertIn("verified", text)
        self.assertNotIn("394", text)

    def test_a_near_cookie_deadline_is_advertised(self) -> None:
        src = _source(account_info={"accountName": "captience"})
        with mock.patch("tide.sources.registry"):
            src.probe_auth()
        with mock.patch("tide.auth.seconds_until_expiry", return_value=2 * 86400):
            self.assertIn("cookies expire in 2d", src.status_text())


if __name__ == "__main__":
    unittest.main()
