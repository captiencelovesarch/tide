"""The kde desktop plugin with plasma's tools faked: every command it
would run is recorded instead, so no real desktop changes.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tide.plugin_host import BUNDLED_DIR

_pkg = types.ModuleType("tide_plugins_test_kde")
_pkg.__path__ = [str(BUNDLED_DIR / "kde")]
sys.modules["tide_plugins_test_kde"] = _pkg
desk = importlib.import_module("tide_plugins_test_kde.desk")
art = importlib.import_module("tide_plugins_test_kde.art")


class FakePlasma:
    """answers like plasma's tools, remembers what was asked."""

    def __init__(self, accent="115,0,255"):
        self.calls: list[list[str]] = []
        self.config = {"AccentColor": accent, "LastUsedCustomAccentColor": accent,
                       "accentColorFromWallpaper": "", "ColorScheme": "BreezeDark"}
        self.walls = [{"id": 1, "screen": 0, "plugin": "org.kde.image",
                       "image": "file:///home/me/mine.png"},
                      {"id": 2, "screen": 1, "plugin": "org.kde.slideshow", "image": ""}]

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "kreadconfig6":
            return self.config.get(cmd[cmd.index("--key") + 1], "") + "\n"
        if cmd[0] == "qdbus6" and "print(JSON.stringify(out))" in cmd[-1]:
            return json.dumps(self.walls) + "\n"
        return ""

    def tools(self):
        return [c[0] for c in self.calls]

    def accents(self):
        return [c[-1] for c in self.calls if c[0] == "plasma-apply-colorscheme"]

    def scripts(self):
        return [c[-1] for c in self.calls if c[0] == "qdbus6"]


class ColourTests(unittest.TestCase):
    def test_colour_text_both_ways(self):
        self.assertEqual(desk.rgb_text("#7300ff"), "115,0,255")
        self.assertEqual(desk.hex_of("115,0,255"), "#7300ff")

    def test_accent_stays_readable_under_white_text(self):
        for rgb in ((1.0, 0.95, 0.4), (0.6, 1.0, 0.7), (0.2, 0.3, 0.9), (1.0, 0.2, 0.4)):
            hx = art.accent(rgb)
            c = tuple(int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
            self.assertGreaterEqual(1.05 / (art._luminance(c) + 0.05), 3.0, hx)

    def test_wallpaper_is_a_soft_dimmed_fill(self):
        from PySide6.QtGui import QColor, QGuiApplication, QImage
        QGuiApplication.instance() or QGuiApplication([])
        cover = QImage(300, 300, QImage.Format_RGB32)
        cover.fill(QColor("#ff2266"))
        for y in range(150):
            for x in range(300):
                cover.setPixelColor(x, y, QColor("#22aaff"))
        img = art.wallpaper(cover, 1280, 720)
        self.assertEqual((img.width(), img.height()), (1280, 720))
        top, bottom = img.pixelColor(640, 40), img.pixelColor(640, 680)
        self.assertGreater(top.blue(), top.red())                      # still the cover's colours
        self.assertGreater(bottom.red(), bottom.blue())
        mid = img.pixelColor(640, 360)
        self.assertTrue(40 < mid.red() < 220 and 40 < mid.blue() < 220)  # blended: a blur
        self.assertLess(top.blue(), 240)                               # dimmed


class DeskTests(unittest.TestCase):
    def test_restores_a_custom_accent(self):
        plasma = FakePlasma()
        d = desk.Desk(plasma)
        saved = d.read_accent()
        d.set_accent("#ff2266")
        d.restore_accent(saved)
        self.assertEqual(plasma.accents(), ["#ff2266", "#7300ff"])
        writes = [c for c in plasma.calls if c[0] == "kwriteconfig6"]
        self.assertIn(["kwriteconfig6", "--file", "kdeglobals", "--group", "General",
                       "--key", "AccentColor", "115,0,255"], writes)
        self.assertIn(["kwriteconfig6", "--file", "kdeglobals", "--group", "General",
                       "--key", "accentColorFromWallpaper", "--delete"], writes)

    def test_restores_a_scheme_accent(self):
        plasma = FakePlasma(accent="")
        d = desk.Desk(plasma)
        saved = d.read_accent()
        with mock.patch.object(d, "_scheme_highlight", return_value="#3daee9"):
            d.restore_accent(saved)
        self.assertEqual(plasma.accents(), ["#3daee9"])
        self.assertIn(["kwriteconfig6", "--file", "kdeglobals", "--group", "General",
                       "--key", "AccentColor", "--delete"], plasma.calls)

    def test_wallpapers_round_trip(self):
        plasma = FakePlasma()
        d = desk.Desk(plasma)
        saved = d.read_wallpapers()
        self.assertEqual(saved, plasma.walls)
        d.set_wallpaper(Path("/tmp/x/wallpaper-1.jpg"))
        d.restore_wallpapers(saved)
        set_js, restore_js = plasma.scripts()[1:]
        self.assertIn('"file:///tmp/x/wallpaper-1.jpg"', set_js)
        self.assertIn("mine.png", restore_js)
        self.assertIn("org.kde.slideshow", restore_js)

    def test_says_when_it_isnt_kde(self):
        with mock.patch.dict("os.environ", {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertEqual(desk.missing(), "this needs kde plasma")


FAKE_TOOL = r"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["TIDE_FAKE_PLASMA"]
name = os.path.basename(sys.argv[0])
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps([name] + sys.argv[1:]) + "\n")
state = json.load(open(os.path.join(d, "state.json")))
if name == "kreadconfig6":
    print(state["config"].get(sys.argv[sys.argv.index("--key") + 1], ""))
elif name == "qdbus6" and "print(JSON.stringify(out))" in sys.argv[-1]:
    print(json.dumps(state["walls"]))
"""


class KdePluginTests(unittest.TestCase):
    """the whole plugin under the host. plasma's tools are fake scripts put
    first on PATH and tide's data folder is a temp one, so nothing the
    plugin runs can reach the real desktop."""

    def setUp(self):
        import os
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication
        from tide import config
        from tide.plugin_host import PluginHost
        from tests.test_plugins import FakePlayer, FakeQueue
        self.app = QApplication.instance() or QApplication([])
        self.tmp = Path(tempfile.mkdtemp(prefix="tide-kde-"))
        (self.tmp / "s").mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in desk.TOOLS:
            f = bin_dir / tool
            f.write_text(FAKE_TOOL)
            f.chmod(0o755)
        self.fake = self.tmp / "plasma"
        self.fake.mkdir()
        plasma = FakePlasma()
        (self.fake / "state.json").write_text(json.dumps({"config": plasma.config,
                                                          "walls": plasma.walls}))
        self.walls = plasma.walls
        self.data = self.tmp / "data"
        self.patches = [
            mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                                         "XDG_CURRENT_DESKTOP": "KDE",
                                         "TIDE_FAKE_PLASMA": str(self.fake)}),
            mock.patch.object(config, "PLUGIN_DATA_DIR", self.data),
        ]
        for p in self.patches:
            p.start()
        self.player, self.queue = FakePlayer(), FakeQueue()
        self.player.state = "playing"
        self.queue.current = types.SimpleNamespace(
            video_id="a", title="Noid", artists="Tyler", album="", duration_seconds=1,
            thumbnail="https://example/noid.jpg", source="ytmusic")
        self.cover = QImage(64, 64, QImage.Format_RGB32)
        self.cover.fill(QColor("#ff2266"))
        self.host = None

    def tearDown(self):
        if self.host is not None:
            self.host.shutdown()
        for p in reversed(self.patches):
            p.stop()

    def calls(self) -> list[list[str]]:
        f = self.fake / "calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def accents(self):
        return [c[-1] for c in self.calls() if c[0] == "plasma-apply-colorscheme"]

    def scripts(self):
        return [c[-1] for c in self.calls() if c[0] == "qdbus6"]

    def start(self, settings: str = ""):
        from tide.plugin_host import PluginHost
        (self.tmp / "s" / "kde.toml").write_text(settings)     # before the host reads it
        self.host = PluginHost(None, self.player, self.queue, roots=[(BUNDLED_DIR, "bundled")],
                               state_file=self.tmp / "p.toml", settings_dir=self.tmp / "s",
                               stamp=self.tmp / "stamp", data_dir=self.tmp / "errors")
        self.assertTrue(self.host.set_enabled("kde", True), self.host.record("kde").status)
        plugin = self.host.record("kde").instance
        self.assertTrue(str(plugin.dir).startswith(str(self.tmp)))    # never the real folder
        plugin._settle.setInterval(10)
        plugin.ctx.track.art = lambda url, cb: cb(self.cover)
        return plugin

    def wait(self, plugin, seconds=2.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            self.app.processEvents()
            time.sleep(0.02)

    def test_accent_follows_the_song_and_comes_back(self):
        plugin = self.start()
        self.wait(plugin)
        self.assertEqual(len(self.accents()), 1)
        self.assertNotEqual(self.accents()[0], "#7300ff")
        self.assertTrue(plugin.restore_file.exists())          # saved before the change
        self.assertEqual(self.scripts(), [])                   # accent only: wallpaper untouched
        self.host.set_enabled("kde", False)
        self.assertEqual(self.accents()[-1], "#7300ff")
        self.assertFalse(plugin.restore_file.exists())

    def test_wallpaper_level_and_back(self):
        plugin = self.start('level = "wallpaper"\n')
        self.wait(plugin, 3.0)
        scripts = self.scripts()
        self.assertEqual(len(scripts), 2, scripts)             # read yours, set the cover
        self.assertIn("wallpaper-", scripts[1])
        self.assertEqual(len(list(plugin.dir.glob("wallpaper-*.jpg"))), 1)
        self.host.set_enabled("kde", False)
        self.assertIn("mine.png", self.scripts()[-1])
        self.assertEqual(list(plugin.dir.glob("wallpaper-*.jpg")), [])

    def test_a_colourless_cover_brings_your_accent_back(self):
        from PySide6.QtGui import QColor
        plugin = self.start()
        self.wait(plugin)
        self.assertEqual(len(self.accents()), 1)          # the pink cover's colour
        self.cover.fill(QColor("#808080"))
        self.queue.current = types.SimpleNamespace(
            video_id="b", title="Grey", artists="x", album="", duration_seconds=1,
            thumbnail="https://example/grey.jpg", source="ytmusic")
        plugin._track_changed(self.host.current_track())
        self.wait(plugin)
        self.assertEqual(self.accents()[-1], "#7300ff")   # yours, not the last song's

    def test_a_crash_last_time_is_put_right_first(self):
        crashed = self.data / "kde"
        crashed.mkdir(parents=True, exist_ok=True)
        (crashed / "restore.json").write_text(json.dumps(
            {"accent": {"AccentColor": "1,2,3"}, "wallpapers": self.walls}))
        self.queue.current = None                              # nothing playing: only the fix-up
        plugin = self.start()
        self.wait(plugin, 1.5)
        self.assertEqual(self.accents(), ["#010203"])
        self.assertIn("mine.png", self.scripts()[0])
        self.assertFalse(plugin.restore_file.exists())


if __name__ == "__main__":
    unittest.main()
