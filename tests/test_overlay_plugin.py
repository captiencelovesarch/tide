"""The stream overlay plugin: its page, its live events, and who it
answers. A real local http server on a spare port; no obs needed.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import http.client
import json
import socket
import tempfile
import time
import types
import unittest
from pathlib import Path

from tide.plugin_host import BUNDLED_DIR


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Events:
    """an /events stream, read a line at a time."""

    def __init__(self, port: int) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        self.conn.request("GET", "/events")
        self.resp = self.conn.getresponse()

    def next(self, kind: str, tries: int = 50) -> dict:
        event = None
        for _ in range(tries * 4):
            line = self.resp.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event == kind:
                return json.loads(line[6:])
        raise AssertionError(f"no {kind} event")

    def close(self) -> None:
        self.conn.close()


class OverlayTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        from tide.plugin_host import PluginHost
        from tests.test_plugins import FakePlayer, FakeQueue
        self.app = QApplication.instance() or QApplication([])
        self.port = free_port()
        tmp = Path(tempfile.mkdtemp(prefix="tide-overlay-"))
        (tmp / "s").mkdir()
        # lyrics off: with a song playing, tide's lyric tracker would go and
        # fetch them for real on a thread the test outlives
        (tmp / "s" / "overlay.toml").write_text(f"port = {self.port}\nlyrics = false\n")
        self.player, self.queue = FakePlayer(), FakeQueue()
        self.queue.current = types.SimpleNamespace(
            video_id="abc", title="Noid", artists="Tyler, The Creator", album="CHROMAKOPIA",
            duration_seconds=285, thumbnail="", source="ytmusic")
        self.player.state = "playing"
        self.host = PluginHost(None, self.player, self.queue, roots=[(BUNDLED_DIR, "bundled")],
                               state_file=tmp / "p.toml", settings_dir=tmp / "s",
                               stamp=tmp / "stamp", data_dir=tmp / "d")
        self.assertTrue(self.host.set_enabled("overlay", True), self.host.record("overlay").status)
        self.plugin = self.host.record("overlay").instance

    def tearDown(self):
        self.host.set_enabled("overlay", False)
        self.host.shutdown()

    def get(self, path: str, host: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", path, headers={"Host": host} if host else {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body, resp.getheader("Content-Type")

    def test_serves_the_page_and_says_where(self):
        status, body, mime = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"EventSource", body)
        self.assertTrue(mime.startswith("text/html"))
        self.assertEqual(self.host.record("overlay").detail, f"for obs: http://127.0.0.1:{self.port}/")

    def test_only_answers_to_this_computer(self):
        status, _body, _mime = self.get("/", host="evil.example:7311")
        self.assertEqual(status, 403)

    def test_a_page_gets_the_song_then_changes(self):
        ev = Events(self.port)
        try:
            state = ev.next("state")
            self.assertEqual(state["track"]["title"].lower(), "noid")
            self.assertTrue(state["playing"])
            self.assertIn("fonts", state["theme"])
            self.queue.current = types.SimpleNamespace(
                video_id="def", title="Sticky", artists="Tyler", album="", duration_seconds=1,
                thumbnail="", source="ytmusic")
            self.plugin._track(self.host.current_track())
            self.assertEqual(ev.next("state")["track"]["title"].lower(), "sticky")
        finally:
            ev.close()

    def test_the_beat_reaches_the_page_capped(self):
        ev = Events(self.port)                       # a page is open: beats get sent
        sent = []
        real = self.plugin.server.publish
        self.plugin.server.publish = lambda kind, data: (sent.append((kind, data)), real(kind, data))
        try:
            ev.next("state")
            hits = []
            for i in range(30):                      # 8 hits a second for a second
                self.plugin._pulse(1.0 if i % 4 == 0 else 0.0)
                self.plugin._send_pulse()
                if i % 4 == 0:
                    hits.append(sent[-1][1]["l"])
                time.sleep(0.033)
            self.assertEqual(sum(1 for lvl in hits if lvl >= 0.99), 3)   # three full flashes
            self.assertTrue(all(lvl > 0.2 for lvl in hits))               # the rest still show
        finally:
            ev.close()

    def test_lyric_lines_reach_the_page(self):
        ctx = self.plugin.ctx
        ctx.lyrics.subscribe = lambda: None          # no fetching, see setUp
        ctx.lyrics.lines_around = lambda _pos: ("one", "Two", None)
        ctx.settings.set("lyrics", True)
        self.host.apply_settings("overlay", {"lyrics"})
        ev = Events(self.port)
        try:
            self.assertTrue(ev.next("state")["lyrics"])
            self.plugin._lyrics()
            lyric = ev.next("lyric")
            self.assertEqual((lyric["prev"], lyric["now"].lower(), lyric["next"]), ("one", "two", ""))
        finally:
            ev.close()

    def test_cover_is_served(self):
        from PySide6.QtGui import QColor, QImage
        img = QImage(64, 64, QImage.Format_RGB32)
        img.fill(QColor("#ff2266"))
        self.plugin._art_url = "https://example/art.jpg"
        self.plugin._got_art("https://example/art.jpg", img)
        status, body, mime = self.get("/art")
        self.assertEqual((status, mime), (200, "image/jpeg"))
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertGreater(self.plugin._state()["glow"][0][0], 200)

    def test_a_taken_port_says_so(self):
        from tide.plugin_host import PluginHost
        from tests.test_plugins import FakePlayer, FakeQueue
        tmp = Path(tempfile.mkdtemp(prefix="tide-overlay-"))
        (tmp / "s").mkdir()
        (tmp / "s" / "overlay.toml").write_text(f"port = {self.port}\n")
        other = PluginHost(None, FakePlayer(), FakeQueue(), roots=[(BUNDLED_DIR, "bundled")],
                           state_file=tmp / "p.toml", settings_dir=tmp / "s",
                           stamp=tmp / "stamp", data_dir=tmp / "d")
        try:
            # same port, already in use by setUp's overlay
            self.host.set_enabled("overlay", False)
            blocker = socket.socket()
            blocker.bind(("127.0.0.1", self.port))
            blocker.listen(1)
            self.assertTrue(other.set_enabled("overlay", True))
            self.assertIn("is taken", other.record("overlay").detail)
            blocker.close()
        finally:
            other.shutdown()


if __name__ == "__main__":
    unittest.main()
