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

    def test_vivid_keeps_the_hue_at_full_strength(self):
        import colorsys
        out = art.vivid("#5854aa")                     # tide's muted blue-violet
        r, g, b = (int(out[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        h0 = colorsys.rgb_to_hsv(0x58 / 255, 0x54 / 255, 0xaa / 255)[0]
        self.assertAlmostEqual(h, h0, places=2)
        self.assertGreaterEqual(s, 0.9)
        self.assertEqual(v, 1.0)
        self.assertEqual(art.vivid("#808080"), "#808080")   # no colour: left alone

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
        self.tokens = {"accent": "#ff2266", "ambient_neutral": ""}

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
        plugin._theme_settle.setInterval(20)
        plugin.ctx.track.art = lambda url, cb: cb(self.cover)
        # tide's adaptive colours, as the theme would give them
        plugin.ctx.theme.token = lambda name, default="": self.tokens.get(name, default)
        return plugin

    def tide_picks(self, plugin, accent: str, neutral: bool = False) -> None:
        """tide's adaptive driver settling on a colour for the cover."""
        self.tokens["accent"] = accent
        self.tokens["ambient_neutral"] = "1" if neutral else ""
        plugin.ctx.theme.changed.emit()

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

    def test_a_black_and_white_cover_is_a_white_accent(self):
        plugin = self.start()
        self.wait(plugin)
        self.assertEqual(self.accents(), [art.vivid("#ff2266")])     # tide's colour, plain
        self.queue.current = types.SimpleNamespace(
            video_id="b", title="Grey", artists="x", album="", duration_seconds=1,
            thumbnail="https://example/grey.jpg", source="ytmusic")
        plugin._track_changed(self.host.current_track())
        self.tide_picks(plugin, "#4a5d8f", neutral=True)  # tide found no real colour
        self.wait(plugin)
        self.assertEqual(self.accents()[-1], "#f0f0f0")   # white, not the last song's

    def test_follows_tides_colour_as_it_settles(self):
        plugin = self.start()
        self.wait(plugin, 0.6)
        # a cover coming in can step tide's colour a few times quickly:
        # the desktop takes the one it lands on, once
        for c in ("#111111", "#222222", "#33ccaa"):
            self.tide_picks(plugin, c)
            self.app.processEvents()
        self.wait(plugin, 0.6)
        self.assertEqual(self.accents()[-1], art.vivid("#33ccaa"))
        self.assertNotIn(art.vivid("#222222"), self.accents())

    # ---- the rules (2026-10-21) ----

    def set_state(self, state: str) -> None:
        self.player.state = state
        self.player.state_changed.emit(state)

    def next_song(self, plugin, vid: str, color: str) -> None:
        from PySide6.QtGui import QColor
        self.cover.fill(QColor(color))
        self.queue.current = types.SimpleNamespace(
            video_id=vid, title=vid, artists="x", album="", duration_seconds=1,
            thumbnail=f"https://example/{vid}.jpg", source="ytmusic")
        plugin._track_changed(self.host.current_track())
        self.tide_picks(plugin, color)

    def test_pause_keeps_it_3_seconds_then_yours_then_play_brings_it_back(self):
        plugin = self.start()
        plugin._pause.setInterval(300)
        self.wait(plugin, 1.0)
        song = self.accents()[-1]
        self.set_state("paused")
        self.wait(plugin, 0.15)
        self.assertEqual(self.accents()[-1], song)                  # still the song's
        self.wait(plugin, 0.6)
        self.assertEqual(self.accents()[-1], "#7300ff")             # then yours
        self.set_state("playing")
        self.wait(plugin, 0.6)
        self.assertEqual(self.accents()[-1], song)                  # and back on play

    def test_the_gap_between_songs_isnt_a_pause(self):
        # idle, then loading for longer than the pause wait, then playing:
        # this used to put your colour back, then the old song's
        plugin = self.start()
        plugin._pause.setInterval(300)
        self.wait(plugin, 1.0)
        self.set_state("idle")                                      # the song ended
        self.wait(plugin, 0.1)
        self.set_state("loading")                                   # the next one loads
        self.next_song(plugin, "b", "#22aaff")
        self.wait(plugin, 1.0)                                      # past the pause wait
        self.set_state("playing")
        self.wait(plugin, 0.5)
        self.assertNotIn("#7300ff", self.accents())                 # never yours in between
        self.assertEqual(self.accents()[-1], art.vivid("#22aaff"))             # b's colour, not a's

    def test_a_skip_changes_the_colour_while_the_stream_is_found(self):
        # a skip stops the old song (idle) and spends 1-3 s finding the new
        # stream before it loads; the old colour used to hold all that time
        plugin = self.start()
        plugin._pause.setInterval(300)
        self.wait(plugin, 0.6)
        self.assertEqual(self.accents(), [art.vivid("#ff2266")])
        self.set_state("idle")                         # the skip stops song a
        self.next_song(plugin, "b", "#22aaff")         # b chosen, tide picks its colour
        self.wait(plugin, 0.5)                         # still idle: finding b's stream
        self.assertEqual(self.accents()[-1], art.vivid("#22aaff"))
        self.wait(plugin, 0.5)
        self.assertNotIn("#7300ff", self.accents())    # and no "go back to yours"
        self.set_state("loading")
        self.set_state("playing")
        self.wait(plugin, 0.3)
        self.assertEqual(self.accents()[-1], art.vivid("#22aaff"))

    def test_a_skip_that_never_starts_still_gives_yours_back(self):
        plugin = self.start()
        plugin._pause.setInterval(200)
        plugin._starting.setInterval(400)
        self.wait(plugin, 0.6)
        self.set_state("idle")
        self.next_song(plugin, "b", "#22aaff")
        plugin._starting_until = time.monotonic() + 0.3
        self.wait(plugin, 1.4)                         # never loads: an error
        self.assertEqual(self.accents()[-1], "#7300ff")

    def test_a_skip_while_paused_waits_for_play(self):
        plugin = self.start()
        plugin._pause.setInterval(5000)
        self.wait(plugin, 1.0)
        self.set_state("paused")
        before = len(self.accents())
        self.next_song(plugin, "c", "#22ff66")
        self.wait(plugin, 1.0)
        self.assertEqual(len(self.accents()), before)               # paused: nothing yet
        self.set_state("playing")
        self.wait(plugin, 0.6)
        self.assertEqual(len(self.accents()), before + 1)           # its colour on play

    def test_the_music_running_out_brings_yours_back(self):
        plugin = self.start()
        plugin._pause.setInterval(300)
        self.wait(plugin, 1.0)
        self.set_state("idle")
        self.wait(plugin, 0.8)
        self.assertEqual(self.accents()[-1], "#7300ff")

    def test_an_old_settings_file_still_loads(self):
        plugin = self.start('level = "accent"\npaused = "restore"\n')
        self.wait(plugin, 1.0)
        self.assertEqual(len(self.accents()), 1)

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
