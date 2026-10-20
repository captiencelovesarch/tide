"""The lights plugin against fake devices: a WLED strip (its json info
over http, frames over udp) and an OpenRGB sdk server speaking protocol
0 over tcp. No real lights are touched.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import importlib
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tide.plugin_host import BUNDLED_DIR

_pkg = types.ModuleType("tide_plugins_test_lights")
_pkg.__path__ = [str(BUNDLED_DIR / "lights")]
sys.modules["tide_plugins_test_lights"] = _pkg
wled = importlib.import_module("tide_plugins_test_lights.wled")
openrgb = importlib.import_module("tide_plugins_test_lights.openrgb")
show_mod = importlib.import_module("tide_plugins_test_lights.show")


def _s(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def controller_v0(name: str, colors: list, matrix: bool = False) -> bytes:
    """protocol-0 controller data, laid out like OpenRGB writes it."""
    body = struct.pack("<i", 2) + _s(name) + _s("a device") + _s("1.0") + _s("") + _s("usb")
    modes = [("direct", [0x0000FF]), ("rainbow", [])]
    body += struct.pack("<Hi", len(modes), 1)
    for mname, mcolors in modes:
        body += _s(mname) + struct.pack("<iIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0)
        body += struct.pack("<H", len(mcolors)) + b"".join(struct.pack("<I", c) for c in mcolors)
    body += struct.pack("<H", 1)
    body += _s("zone") + struct.pack("<iIII", 0, 1, 64, len(colors))
    if matrix:
        h, w = 2, 2
        mat = struct.pack("<II", h, w) + b"".join(struct.pack("<I", i) for i in range(h * w))
        body += struct.pack("<H", len(mat)) + mat
    else:
        body += struct.pack("<H", 0)
    body += struct.pack("<H", len(colors))
    for i in range(len(colors)):
        body += _s(f"led {i}") + struct.pack("<I", i)
    body += struct.pack("<H", len(colors))
    body += b"".join(struct.pack("<I", r | (g << 8) | (b << 16)) for r, g, b in colors)
    return struct.pack("<I", 4 + len(body)) + body


class FakeOpenRGB(threading.Thread):
    def __init__(self, devices: dict) -> None:
        super().__init__(daemon=True)
        self.devices = list(devices.items())
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.updates: list[tuple[int, list]] = []
        self.custom: set[int] = set()
        self.client_name = b""

    def run(self) -> None:
        conn, _ = self.sock.accept()
        conn.settimeout(5)
        buf = b""
        try:
            while True:
                while len(buf) < 16:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                magic, dev, kind, size = struct.unpack("<4sIII", buf[:16])
                while len(buf) < 16 + size:
                    buf += conn.recv(4096)
                data, buf = buf[16:16 + size], buf[16 + size:]
                if kind == openrgb.SET_CLIENT_NAME:
                    self.client_name = data
                elif kind == openrgb.REQUEST_CONTROLLER_COUNT:
                    # a device list update first: the client has to skip it
                    conn.sendall(openrgb.packet(0, openrgb.DEVICE_LIST_UPDATED))
                    conn.sendall(openrgb.packet(0, kind, struct.pack("<I", len(self.devices))))
                elif kind == openrgb.REQUEST_CONTROLLER_DATA:
                    name, colors = self.devices[dev]
                    conn.sendall(openrgb.packet(dev, kind, controller_v0(name, colors, matrix=dev == 1)))
                elif kind == openrgb.SET_CUSTOM_MODE:
                    self.custom.add(dev)
                elif kind == openrgb.UPDATE_LEDS:
                    n = struct.unpack("<H", data[4:6])[0]
                    self.updates.append((dev, [tuple(data[6 + 4 * i:9 + 4 * i]) for i in range(n)]))
        except OSError:
            return


class FakeWled:
    def __init__(self, count: int) -> None:
        info = json.dumps({"name": "desk", "leds": {"count": count}}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(info)

            def log_message(self, *a):
                pass
        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(2)

    def strip(self):
        s = wled.Strip(f"127.0.0.1:{self.udp.getsockname()[1]}")
        s.http_port = self.http.server_address[1]
        return s

    def close(self):
        self.http.shutdown()
        self.udp.close()


class WledTests(unittest.TestCase):
    def test_small_strips_get_one_plain_packet(self):
        pkts = wled.packets([(1, 2, 3)] * 10)
        self.assertEqual(len(pkts), 1)
        self.assertEqual(pkts[0][:2], bytes([wled.DRGB, wled.TIMEOUT_S]))
        self.assertEqual(len(pkts[0]), 2 + 30)

    def test_long_strips_go_in_numbered_chunks(self):
        pkts = wled.packets([(9, 9, 9)] * 1000)
        self.assertEqual([p[0] for p in pkts], [wled.DNRGB] * 3)
        starts = [(p[2] << 8) | p[3] for p in pkts]
        self.assertEqual(starts, [0, 489, 978])
        self.assertEqual(sum((len(p) - 4) // 3 for p in pkts), 1000)

    def test_addresses(self):
        self.assertEqual(wled.parse_address("192.168.1.40"), ("192.168.1.40", 21324, 80))
        self.assertEqual(wled.parse_address(" desk.local:5000 ")[:2], ("desk.local", 5000))


class OpenRgbTests(unittest.TestCase):
    def test_parses_protocol_zero_controller_data(self):
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        name, count, now = openrgb.parse_controller(controller_v0("ram", colors, matrix=True))
        self.assertEqual((name, count, now), ("ram", 3, colors))

    def test_short_data_is_an_error_not_a_crash(self):
        with self.assertRaises(ValueError):
            openrgb.parse_controller(controller_v0("ram", [(1, 2, 3)])[:30])

    def test_leds_packet_says_its_own_size(self):
        data = openrgb.leds_packet([(1, 2, 3), (4, 5, 6)])
        self.assertEqual(struct.unpack("<I", data[:4])[0], len(data))
        self.assertEqual(data[6:10], bytes((1, 2, 3, 0)))


class ShowTests(unittest.TestCase):
    def run_show(self, outputs, seconds=0.6, **config):
        said = []
        show = show_mod.Show(outputs, said.append)
        show.configure(playing=True, colors=((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)), **config)
        show.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            show.set_pulse(1.0 if int((time.monotonic() - t0) * 4) % 2 == 0 else 0.1)
            time.sleep(0.01)
        show.stop()
        show.join(3)
        self.assertFalse(show.is_alive())
        return said

    def test_drives_openrgb_and_gives_the_colours_back(self):
        before = {"keyboard": [(10, 20, 30)] * 4, "strip": [(1, 1, 1)] * 6}
        server = FakeOpenRGB(before)
        server.start()
        said = self.run_show([openrgb.Server(f"127.0.0.1:{server.port}")])
        server.join(2)
        self.assertEqual(server.client_name, b"tide\0")
        self.assertEqual(server.custom, {0, 1})
        driven = [u for u in server.updates if u[1] not in (before["keyboard"], before["strip"])]
        self.assertGreater(len(driven), 10)
        self.assertEqual(len(driven[0][1]), 4)
        # the last word to each device is its old colours
        last = {dev: colors for dev, colors in server.updates}
        self.assertEqual(last, {0: before["keyboard"], 1: before["strip"]})
        self.assertTrue(any("lighting openrgb (2)" in s for s in said))

    def test_drives_a_wled_strip(self):
        fake = FakeWled(30)
        try:
            said = self.run_show([fake.strip()], seconds=0.4)
            frames = []
            try:
                while True:
                    frames.append(fake.udp.recv(2048))
            except OSError:
                pass
        finally:
            fake.close()
        self.assertGreater(len(frames), 5)
        self.assertEqual(frames[0][0], wled.DRGB)
        self.assertEqual(len(frames[0]), 2 + 90)
        self.assertTrue(any("lighting desk" in s for s in said))

    def test_a_missing_device_doesnt_stall_the_rest(self):
        fake = FakeWled(8)
        try:
            dead = openrgb.Server("127.0.0.1:1")        # nothing listens there
            said = self.run_show([dead, fake.strip()], seconds=0.4)
            got = 0
            try:
                while True:
                    fake.udp.recv(2048)
                    got += 1
            except OSError:
                pass
        finally:
            fake.close()
        self.assertGreater(got, 10)
        self.assertTrue(any(s.startswith("openrgb 127.0.0.1") for s in said))

    def test_gradient_and_brightness(self):
        leds = show_mod.gradient(4, ((1, 0, 0), (0, 0, 1)), 1.0, 0.0)
        self.assertEqual(leds[0], (255, 0, 0))
        self.assertEqual(leds[2], (0, 0, 255))
        self.assertEqual(show_mod.gradient(3, ((1, 1, 1), (1, 1, 1)), 0.0, 0.3), [(0, 0, 0)] * 3)
        self.assertLess(show_mod.brightness("kick", 0.0), show_mod.brightness("breathing", 0.0))
        self.assertAlmostEqual(show_mod.brightness("kick", 1.0), 1.0)


class LightsPluginTests(unittest.TestCase):
    def host(self, settings: str):
        from PySide6.QtWidgets import QApplication
        from tide.plugin_host import PluginHost
        from tests.test_plugins import FakePlayer, FakeQueue
        self.app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.mkdtemp(prefix="tide-lights-"))
        (tmp / "s").mkdir()
        (tmp / "s" / "lights.toml").write_text(settings)
        host = PluginHost(None, FakePlayer(), FakeQueue(), roots=[(BUNDLED_DIR, "bundled")],
                          state_file=tmp / "p.toml", settings_dir=tmp / "s",
                          stamp=tmp / "stamp", data_dir=tmp / "d")
        self.addCleanup(host.shutdown)
        return host

    def pump(self, seconds):
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            self.app.processEvents()
            time.sleep(0.01)

    def test_starts_lights_and_lets_go(self):
        server = FakeOpenRGB({"fans": [(5, 5, 5)] * 3})
        server.start()
        host = self.host(f'openrgb_address = "127.0.0.1:{server.port}"\n')
        self.assertTrue(host.set_enabled("lights", True), host.record("lights").status)
        plugin = host.record("lights").instance
        self.pump(0.4)
        self.assertEqual(host.record("lights").detail, "lighting openrgb (1)")
        plugin.on_action("test")                         # the sweep doesn't throw
        self.pump(0.1)
        host.set_enabled("lights", False)
        self.assertIsNone(plugin.show)
        server.join(2)
        self.assertEqual(server.updates[-1], (0, [(5, 5, 5)] * 3))

    def test_nothing_set_up_says_so(self):
        host = self.host('openrgb = false\nwled = ""\n')
        self.assertTrue(host.set_enabled("lights", True))
        self.assertIn("no lights set up", host.record("lights").detail)
        self.assertIsNone(host.record("lights").instance.show)


if __name__ == "__main__":
    unittest.main()
