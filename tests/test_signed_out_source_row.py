"""A signed-out YT Music source must still hold down its Sources row.

The bug: YTMusicSource was only *registered* when a live client existed, and
the client only got built when ``sources_enabled["ytmusic"]`` was already
true. So a cancelled sign-in — which app.py answers by flipping that setting
off — removed the source from the registry entirely. SourcePanel builds its
rows from the registry, so there was no row, no toggle, no gear, no
[sign in], and reauth_source() bailed on the None lookup. The only way back
was hand-editing settings.toml. Every other source registers unconditionally
and merely reports itself unauthenticated; YT Music now does the same.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import unittest
from unittest import mock

from tide.sources import SourceRegistry
from tide.sources import ytmusic as ym


class SignedOutSourceTest(unittest.TestCase):
    def test_constructs_without_a_client(self) -> None:
        src = ym.YTMusicSource(None)
        self.assertIsNone(src.yt)
        self.assertFalse(src.is_authenticated())

    def test_none_is_not_wrapped_in_the_sentinel(self) -> None:
        """is_authenticated() tests ``self.yt is not None``, so wrapping None
        in an _AuthSentinel would read as a live, signed-in client."""
        src = ym.YTMusicSource(None)
        self.assertNotIsInstance(src.yt, ym._AuthSentinel)

    def test_row_offers_the_way_back(self) -> None:
        src = ym.YTMusicSource(None)
        self.assertEqual(src.status_text(), "sign in via [import]")
        self.assertTrue(src.supports_in_app_auth)

    def test_registers_so_the_panel_can_build_a_row(self) -> None:
        """SourcePanel iterates registry.all(); absence from it is the bug."""
        reg = SourceRegistry()
        reg.register(ym.YTMusicSource(None), enabled=False)
        self.assertIn("ytmusic", [s.slug for s in reg.all()])
        self.assertIsNotNone(reg.get("ytmusic"))
        self.assertFalse(reg.is_enabled("ytmusic"))

    def test_signing_in_recovers_without_a_restart(self) -> None:
        """The row's [sign in] runs begin_auth → reload_client, which is what
        turns the source back on in-place."""
        src = ym.YTMusicSource(None)
        with mock.patch("tide.auth.yt_client", return_value=mock.Mock()):
            self.assertTrue(src.reload_client())
        self.assertTrue(src.is_authenticated())

    def test_a_live_client_still_wraps_in_the_sentinel(self) -> None:
        client = mock.Mock()
        src = ym.YTMusicSource(client)
        self.assertIsInstance(src.yt, ym._AuthSentinel)
        self.assertTrue(src.is_authenticated())


if __name__ == "__main__":
    unittest.main()
