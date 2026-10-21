"""The plugin host (plugin_host.py): manifests, discovery, on/off without a
restart, undo by construction, settings that only exist if declared, safe
mode, and error counting.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from tide import plugin_api, plugin_host
from tide.plugin_host import (
    ManifestError, PluginHost, PluginSettings, discover, parse_manifest,
)
from tide.sources.base import Track

_app = QApplication.instance() or QApplication([])


def _pump() -> None:
    for _ in range(3):
        _app.processEvents()


MANIFEST = """
[plugin]
slug = "{slug}"
name = "{name}"
description = "a test plugin"
version = "1.0.0"
api = {api}
entry = "plugin:Thing"
requires = {requires}
uses = ["playback"]
default = {default}

[[setting]]
key = "greeting"
label = "greeting"
kind = "str"
default = "hi"

[[setting]]
key = "loud"
label = "loud"
kind = "bool"
default = false

[[setting]]
key = "mode"
label = "mode"
kind = "choice"
choices = [["a", "first"], ["b", "second"]]
default = "a"
"""

# records what happens to it in a module-level list the test can read
CODE = """
import sys
EVENTS = sys.modules["tests_plugin_events"]

class Thing:
    def start(self, ctx):
        EVENTS.append(("start", ctx.slug, ctx.settings["greeting"]))
        self.ctx = ctx
        ctx.playback.track_changed.connect(self.on_track)
        ctx.settings_changed.connect(lambda keys: EVENTS.append(("settings", sorted(keys))))
        {start_extra}

    def on_track(self, track):
        EVENTS.append(("track", track.title if track else None))
        {on_track_extra}

    def stop(self):
        EVENTS.append(("stop", self.ctx.slug))
"""


class FakePlayer(QObject):
    state_changed = Signal(object)
    position_changed = Signal(float)
    duration_changed = Signal(float)
    speed_changed = Signal(float)
    ended = Signal()

    def __init__(self):
        super().__init__()
        self.state = None
        self.duration = 0.0
        self.seeks = []

    def seek(self, s):
        self.seeks.append(s)


class FakeQueue(QObject):
    current_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.current = None

    def tracks(self):
        return []

    def current_index(self):
        return -1


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tide-plugins-"))
        self.user = self.tmp / "user"
        self.store_root = self.tmp / "store"
        self.user.mkdir()
        self.store_root.mkdir()
        self.events = []
        sys.modules["tests_plugin_events"] = self.events
        self.player, self.queue = FakePlayer(), FakeQueue()

    def tearDown(self):
        sys.modules.pop("tests_plugin_events", None)

    def write(self, slug, *, root=None, name=None, api=1, requires="[]", default="false",
              start_extra="pass", on_track_extra="pass", code=None):
        folder = (root or self.user) / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plugin.toml").write_text(MANIFEST.format(
            slug=slug, name=name or slug, api=api, requires=requires, default=default))
        (folder / "plugin.py").write_text(code or textwrap.dedent(CODE).format(
            start_extra=start_extra, on_track_extra=on_track_extra))
        return folder

    def host(self, **kw):
        h = PluginHost(None, self.player, self.queue,
                       roots=[(self.store_root, "store"), (self.user, "yours")],
                       state_file=self.tmp / "plugins.toml",
                       settings_dir=self.tmp / "plugin-settings",
                       stamp=self.tmp / "plugin-starting", data_dir=self.tmp / "data", **kw)
        self.addCleanup(h.shutdown)
        return h


class ManifestTests(Base):
    def test_parses(self):
        m = parse_manifest(self.write("good-one"), "yours")
        self.assertEqual(m.slug, "good-one")
        self.assertEqual(m.ident, "good_one")
        self.assertEqual([s.key for s in m.settings], ["greeting", "loud", "mode"])
        self.assertEqual(m.settings[2].choices, (("a", "first"), ("b", "second")))

    def test_rejects(self):
        cases = {
            "no plugin table": "x = 1\n",
            "bad slug": '[plugin]\nslug = "Bad Slug"\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="p:C"\n',
            "bad entry": '[plugin]\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="nope"\n',
            "no api": '[plugin]\nname="n"\ndescription="d"\nversion="1"\nentry="p:C"\n',
            "unknown uses": '[plugin]\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="p:C"\nuses=["rockets"]\n',
            "bad kind": '[plugin]\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="p:C"\n'
                        '[[setting]]\nkey="k"\nlabel="l"\nkind="slider"\n',
            "default type": '[plugin]\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="p:C"\n'
                            '[[setting]]\nkey="k"\nlabel="l"\nkind="int"\ndefault="3"\n',
            "choice default": '[plugin]\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="p:C"\n'
                              '[[setting]]\nkey="k"\nlabel="l"\nkind="choice"\nchoices=["x"]\ndefault="y"\n',
            "no label": '[plugin]\nname="n"\ndescription="d"\nversion="1"\napi=1\nentry="p:C"\n'
                        '[[setting]]\nkey="k"\nkind="bool"\ndefault=true\n',
        }
        for why, text in cases.items():
            folder = self.tmp / why.replace(" ", "-")
            folder.mkdir()
            (folder / "plugin.toml").write_text(text)
            with self.subTest(why), self.assertRaises(ManifestError):
                parse_manifest(folder, "yours")

    def test_a_default_line_switches_nothing_on(self):
        # tide 2.x let its own bundled plugins say default = true; tide 3
        # has none, and a folder you drop in always starts off
        self.write("mine", default="true")
        self.assertFalse(self.host().is_enabled("mine"))

    def test_later_root_overrides(self):
        self.write("same", root=self.store_root, name="store one")
        self.write("same", name="my one")
        found = discover([(self.store_root, "store"), (self.user, "yours")])
        self.assertEqual(found.manifests["same"].name, "my one")
        self.assertEqual(found.overrides["same"], "store")

    def test_broken_ones_are_listed_not_fatal(self):
        (self.user / "broken").mkdir()
        (self.user / "broken" / "plugin.toml").write_text("not toml [")
        self.write("fine")
        found = discover([(self.user, "yours")])
        self.assertIn("fine", found.manifests)
        self.assertEqual(len(found.broken), 1)


class LifecycleTests(Base):
    def test_user_plugins_start_off(self):
        self.write("mine")
        h = self.host()
        h.start_enabled()
        self.assertFalse(h.is_enabled("mine"))
        self.assertEqual(self.events, [])

    def test_a_store_plugin_starts_on(self):
        self.write("ours", root=self.store_root)
        h = self.host()
        h.start_enabled()
        self.assertEqual(self.events, [("start", "ours", "hi")])
        self.assertEqual(h.record("ours").status, "on")

    def test_switch_on_and_off_live_and_undo(self):
        self.write("mine")
        h = self.host()
        self.assertTrue(h.set_enabled("mine", True))
        self.queue.current_changed.emit(Track("v1", "song one", "artist"))
        _pump()
        h.set_enabled("mine", False)
        # after switching off, core signals must not reach it any more
        self.queue.current_changed.emit(Track("v2", "song two", "artist"))
        _pump()
        self.assertEqual(self.events, [("start", "mine", "hi"), ("track", "song one"),
                                       ("stop", "mine")])
        self.assertNotIn("tide_plugins.mine.plugin", sys.modules)
        # and the switch is saved
        self.assertFalse(self.host().is_enabled("mine"))

    def test_on_state_survives_restart(self):
        self.write("mine")
        self.host().set_enabled("mine", True)
        self.events.clear()
        h2 = self.host()
        h2.start_enabled()
        self.assertEqual(self.events, [("start", "mine", "hi")])

    def test_missing_requirement_blocks(self):
        self.write("needy", requires='["definitely_not_a_module_xyz"]')
        h = self.host()
        self.assertFalse(h.set_enabled("needy", True))
        self.assertEqual(h.record("needy").status, "needs definitely_not_a_module_xyz")
        self.assertFalse(h.is_enabled("needy"))

    def test_other_api_blocks(self):
        self.write("future", api=99)
        h = self.host()
        self.assertFalse(h.set_enabled("future", True))
        self.assertEqual(h.record("future").status, "built for a different tide")

    def test_crash_in_start_is_contained(self):
        self.write("boom", start_extra="raise RuntimeError('nope')")
        h = self.host()
        notices = []
        h.notice.connect(notices.append)
        self.assertFalse(h.set_enabled("boom", True))
        rec = h.record("boom")
        self.assertFalse(rec.running)
        self.assertTrue(rec.status.startswith("crashed: nope"))
        self.assertEqual(len(notices), 1)
        self.assertFalse((self.tmp / "plugin-starting").exists())

    def test_reload_picks_up_new_code(self):
        folder = self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        (folder / "plugin.py").write_text(textwrap.dedent(CODE).format(
            start_extra="EVENTS.append(('v2',))", on_track_extra="pass"))
        self.assertTrue(h.reload("mine"))
        self.assertIn(("v2",), self.events)


class SafeModeTests(Base):
    def test_stamp_switches_the_crasher_off(self):
        self.write("crasher")
        self.write("innocent")
        h = self.host()
        h.set_enabled("crasher", True)
        h.set_enabled("innocent", True)
        h.shutdown()
        # what a native crash during crasher's start would leave behind
        (self.tmp / "plugin-starting").write_text("crasher")
        self.events.clear()
        h2 = self.host()
        notices = []
        h2.notice.connect(notices.append)
        h2.start_enabled()
        self.assertFalse(h2.is_enabled("crasher"))
        self.assertEqual(self.events, [("start", "innocent", "hi")])
        self.assertIn("crashed tide last time", notices[0])
        self.assertFalse((self.tmp / "plugin-starting").exists())


class ErrorTests(Base):
    def test_repeated_errors_switch_it_off(self):
        self.write("flaky", on_track_extra="raise ValueError('bad track')")
        h = self.host()
        h.install_hooks()
        h.set_enabled("flaky", True)
        notices = []
        h.notice.connect(notices.append)
        for i in range(plugin_host.ERROR_LIMIT):
            # a raising slot goes to sys.excepthook, which the host owns
            try:
                self.events.clear()
                h.record("flaky").instance.on_track(None)
            except ValueError:
                h._attribute(*sys.exc_info())
        _pump()
        self.assertFalse(h.record("flaky").running)
        self.assertEqual(h.record("flaky").status, "off: it kept failing")
        self.assertIn("kept failing", notices[0])
        # each one, with where it came from, for after the fact
        logged = h.error_log("flaky").read_text()
        self.assertEqual(logged.count("Traceback"), plugin_host.ERROR_LIMIT)
        self.assertIn("on_track", logged)

    def test_errors_elsewhere_pass_through(self):
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        try:
            raise KeyError("not a plugin's")
        except KeyError:
            self.assertFalse(h._attribute(*sys.exc_info()))


class SettingsTests(Base):
    def test_defaults_set_and_persist(self):
        m = parse_manifest(self.write("mine"), "yours")
        path = self.tmp / "s.toml"
        s = PluginSettings(m, path)
        self.assertEqual(s["greeting"], "hi")
        self.assertTrue(s.set("greeting", "hello"))
        self.assertFalse(s.set("greeting", "hello"))
        self.assertEqual(PluginSettings(m, path)["greeting"], "hello")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_undeclared_and_bad_values(self):
        s = PluginSettings(parse_manifest(self.write("mine"), "yours"), self.tmp / "s.toml")
        with self.assertRaises(KeyError):
            s["secret_thing"]
        with self.assertRaises(KeyError):
            s.set("secret_thing", 1)
        with self.assertRaises(ValueError):
            s.set("loud", "yes")
        with self.assertRaises(ValueError):
            s.set("mode", "c")

    def test_garbage_on_disk_falls_back(self):
        m = parse_manifest(self.write("mine"), "yours")
        path = self.tmp / "s.toml"
        path.write_text('greeting = 3\nloud = true\nmode = "zzz"\nstray = 1\n')
        s = PluginSettings(m, path)
        self.assertEqual((s["greeting"], s["loud"], s["mode"]), ("hi", True, "a"))

    def test_apply_settings_reaches_the_plugin(self):
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        h.apply_settings("mine", {"loud"})
        _pump()
        self.assertIn(("settings", ["loud"]), self.events)


class CtxTests(Base):
    def test_playback_controls_reach_tide(self):
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        ctx = h.record("mine").ctx
        ctx.playback.seek(12.5)
        self.assertEqual(self.player.seeks, [12.5])
        self.player.position_changed.emit(3.0)
        self.assertEqual(ctx.playback.position(), 3.0)

    def test_track_info(self):
        info = plugin_api.track_info(Track("abc", "t", "a", album="al", duration_seconds=200,
                                           thumbnail="https://x/y.jpg"))
        self.assertEqual(info.link, "https://music.youtube.com/watch?v=abc")
        self.assertEqual(info.duration, 200)
        self.assertEqual(plugin_api.track_link("/home/me/song.flac", "local"), "")

    def test_jobs_come_back_on_gui_thread_and_stop_with_plugin(self):
        import threading
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        ctx = h.record("mine").ctx
        got = []
        main = threading.get_ident()
        ctx.jobs.run(lambda: threading.get_ident(),
                     on_done=lambda tid: got.append((tid != main, threading.get_ident() == main)))
        for _ in range(200):
            _pump()
            if got:
                break
        self.assertEqual(got, [(True, True)])


if __name__ == "__main__":
    unittest.main()


class MusicAndSpeedTests(Base):
    """ctx.music through a fake source, speed through the speed button."""

    def setUp(self):
        super().setUp()
        from tide.sources import registry
        from tide.sources.base import AlbumEntry, AlbumDetail, MusicSource, PlaylistDetail

        class Fake(MusicSource):
            slug = "fakesrc"
            def search_songs(self, q, limit=20):
                return [Track("s1", f"{q} one", "a"), Track("s2", f"{q} two", "b")]
            def resolve_stream(self, track):
                return None
            def search_albums(self, q, limit=20):
                return [AlbumEntry("al1", "album", "artist", "2001")]
            def get_album(self, bid):
                return AlbumDetail(bid, "album", tracks=[Track("t1", "track", "a")])
            def get_playlist(self, pid, limit=500):
                return PlaylistDetail(pid, "liked", tracks=[Track("l1", "liked one", "a")])

        reg = registry()
        self._prev_active = reg.active_slug
        reg.register(Fake())
        reg.set_active("fakesrc")
        self.addCleanup(lambda: reg.set_active(self._prev_active) if self._prev_active in
                        [s.slug for s in reg.all()] else None)

    def run_until(self, cond):
        for _ in range(300):
            _pump()
            if cond():
                return
        self.fail("timed out")

    def test_search_open_and_play(self):
        played = []

        class Win:
            _current = None
            _auto_radio_on_play = True
            def _play_now(self, t, seed_radio=False): played.append(("now", t.video_id, seed_radio))
            def _start_radio(self, t): played.append(("radio", t.video_id))
            def _queue_next(self, t): played.append(("next", t.video_id))
            def _play_all(self, ts): played.append(("all", [t.video_id for t in ts]))
        self.write("mine")
        h = PluginHost(Win(), self.player, self.queue,
                       roots=[(self.user, "yours")], state_file=self.tmp / "p.toml",
                       settings_dir=self.tmp / "s", stamp=self.tmp / "st")
        self.addCleanup(h.shutdown)
        h.set_enabled("mine", True)
        music = h.record("mine").ctx.music
        got = {}
        music.search("daft", "songs", lambda r: got.setdefault("songs", r))
        music.search("daft", "albums", lambda r: got.setdefault("albums", r))
        music.library("liked", lambda r: got.setdefault("liked", r))
        self.run_until(lambda: len(got) == 3)
        self.assertEqual([t.title for t in got["songs"]], ["daft one", "daft two"])
        self.assertEqual(got["albums"][0].subtitle, "artist · 2001")
        self.assertEqual(got["liked"][0].title, "liked one")
        music.open(got["albums"][0], lambda r: got.setdefault("album", r))
        self.run_until(lambda: "album" in got)
        music.play_now(got["songs"][1])
        music.play_now(got["songs"][1], radio=False)
        music.radio(got["songs"][0])
        music.play_next(got["songs"][0])
        music.play(got["songs"], start=1)
        # a song played on its own keeps going like tide's search does
        self.assertEqual(played, [("now", "s2", True), ("now", "s2", False), ("radio", "s1"),
                                  ("next", "s1"), ("all", ["s2", "s1"])])

    def test_speed_goes_through_the_button(self):
        class Btn:
            value = 1.0
            def speed(self): return self.value
            def set_speed(self, v): self.value = v
            def backend_supported(self): return True

        class Win:
            speed_btn = Btn()
        self.write("mine")
        h = PluginHost(Win(), self.player, self.queue, roots=[(self.user, "yours")],
                       state_file=self.tmp / "p.toml", settings_dir=self.tmp / "s",
                       stamp=self.tmp / "st")
        self.addCleanup(h.shutdown)
        h.set_enabled("mine", True)
        pb = h.record("mine").ctx.playback
        pb.set_speed(1.234)
        self.assertEqual(pb.speed(), 1.25)          # tide's 0.05 steps
        pb.set_speed(9)
        self.assertEqual(pb.speed(), 2.0)           # and its range
        self.assertTrue(pb.speed_supported())

    def test_speech_says_what_it_needs(self):
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        ok, why = h.record("mine").ctx.speech.available()
        if not ok:
            self.assertTrue(why.startswith("voice needs"))
