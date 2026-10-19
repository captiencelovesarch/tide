"""The plugin host (plugin_host.py): manifests, discovery, on/off without a
restart, undo by construction, settings that only exist if declared, safe
mode, and error counting. Every bundled plugin's manifest is checked too.

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
    BUNDLED_DIR, ManifestError, PluginHost, PluginSettings, discover, parse_manifest,
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
        self.bundled = self.tmp / "bundled"
        self.user.mkdir()
        self.bundled.mkdir()
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
                       roots=[(self.bundled, "bundled"), (self.user, "yours")],
                       state_file=self.tmp / "plugins.toml",
                       settings_dir=self.tmp / "plugin-settings",
                       stamp=self.tmp / "plugin-starting", **kw)
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

    def test_only_bundled_may_default_on(self):
        self.assertFalse(parse_manifest(self.write("mine", default="true"), "yours").default)
        self.assertTrue(parse_manifest(self.write("ours", root=self.bundled, default="true"),
                                       "bundled").default)

    def test_later_root_overrides(self):
        self.write("same", root=self.bundled, name="bundled one")
        self.write("same", name="my one")
        found = discover([(self.bundled, "bundled"), (self.user, "yours")])
        self.assertEqual(found.manifests["same"].name, "my one")
        self.assertEqual(found.overrides["same"], "bundled")

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

    def test_bundled_default_on_starts(self):
        self.write("ours", root=self.bundled, default="true")
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


class BundledTests(unittest.TestCase):
    def test_every_bundled_manifest_is_valid(self):
        if not BUNDLED_DIR.is_dir():
            self.skipTest("no bundled plugins")
        found = discover([(BUNDLED_DIR, "bundled")])
        self.assertEqual(found.broken, [], found.broken)
        for m in found.manifests.values():
            with self.subTest(m.slug):
                self.assertEqual(m.api, plugin_api.API_VERSION)
                self.assertEqual(m.name, m.name.lower(), "names are lowercase")
                for d in m.settings:
                    self.assertEqual(d.label, d.label.lower(), f"{d.key}: labels are lowercase")


if __name__ == "__main__":
    unittest.main()


class PluginsTabTests(Base):
    """the settings dialog's plugins tab (ui/plugins_tab.py)."""

    def page(self, h):
        """the page inside a container, the way the dialog holds it: the
        container owns the widgets and deleting it takes them all (loose
        widgets would live until python's exit and crash it there)."""
        import shiboken6
        from tide.ui.plugins_tab import PluginsPage
        from tide.ui.settings import _page
        p = PluginsPage(h)
        holder = _page(*p.items())

        def cleanup():
            p.close()
            if shiboken6.isValid(holder):
                shiboken6.delete(holder)
        self.addCleanup(cleanup)
        return p, {s.slug: s for s in p.sections}

    def test_switch_and_settings_apply_on_commit(self):
        self.write("mine")
        h = self.host()
        page, sections = self.page(h)
        sec = sections["mine"]
        sec.switch.setChecked(True)
        sec.widgets["greeting"].setText("hello")
        self.assertEqual(self.events, [])            # nothing until save
        page.commit()
        self.assertTrue(h.is_enabled("mine"))
        self.assertEqual(self.events, [("start", "mine", "hello")])

    def test_settings_reach_a_running_plugin(self):
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        page, sections = self.page(h)
        sections["mine"].widgets["loud"].setChecked(True)
        idx = sections["mine"].widgets["mode"].findData("b")
        sections["mine"].widgets["mode"].setCurrentIndex(idx)
        page.commit()
        _pump()
        self.assertIn(("settings", ["loud", "mode"]), self.events)
        self.assertEqual(h.record("mine").settings["mode"], "b")

    def test_untouched_page_changes_nothing(self):
        self.write("mine")
        h = self.host()
        h.set_enabled("mine", True)
        page, _ = self.page(h)
        self.events.clear()
        page.commit()
        _pump()
        self.assertEqual(self.events, [])

    def test_blocked_plugin_cant_be_switched_on(self):
        self.write("needy", requires='["definitely_not_a_module_xyz"]')
        h = self.host()
        _page, sections = self.page(h)
        self.assertFalse(sections["needy"].switch.isEnabled())
        self.assertIn("needs definitely_not_a_module_xyz", sections["needy"].status.text())

    def test_status_follows_the_plugin(self):
        self.write("mine")
        h = self.host()
        _page, sections = self.page(h)
        h.set_enabled("mine", True)
        h.record("mine").ctx.set_status("connected")
        self.assertTrue(sections["mine"].status.text().startswith("connected"))

    def test_dialog_has_the_tab(self):
        from tide.settings import Settings
        from tide.ui.settings import SettingsDialog
        self.write("mine")
        h = self.host()
        plugin_host.set_host(h)
        self.addCleanup(plugin_host.set_host, None)
        import shiboken6
        dlg = SettingsDialog(Settings())
        self.addCleanup(lambda: shiboken6.isValid(dlg) and dlg.reject() is None
                        and shiboken6.delete(dlg))
        names = [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())]
        self.assertIn("plugins", names)
        self.assertEqual(names[-1], "about")
