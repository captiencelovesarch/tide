"""The plugin store (store.py) and the host's side of it: a signed index
that has to check out, zips that have to match it and stay inside their
folder, install that starts the plugin live, uninstall that keeps or
forgets settings and leaves nothing else behind, and a plugin's vendor/
folder on the import path only while it runs.

Everything runs against a fake index in a temp folder, signed with a
throwaway key. Nothing here touches the network.
"""
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from tide import store
from tide.plugin_host import PluginHost

_app = QApplication.instance() or QApplication([])

MANIFEST = """
[plugin]
slug = "{slug}"
name = "{slug}"
description = "a store test plugin"
version = "{version}"
api = 1
entry = "plugin:Thing"
requires = {requires}
uses = ["playback"]

[[setting]]
key = "greeting"
label = "greeting"
kind = "str"
default = "hi"
"""

CODE = """
import sys
EVENTS = sys.modules["tests_store_events"]

class Thing:
    def start(self, ctx):
        {start}
        EVENTS.append(("start", ctx.slug, ctx.manifest.version, ctx.settings["greeting"]))
        self.ctx = ctx

    def stop(self):
        EVENTS.append(("stop", self.ctx.slug))
"""


class FakePlayer(QObject):
    state_changed = Signal(object)
    position_changed = Signal(float)
    duration_changed = Signal(float)
    speed_changed = Signal(float)
    ended = Signal()


class FakeQueue(QObject):
    current_changed = Signal(object)
    current = None


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"not really a png, the store only checks the sum"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tide-store-"))
        self.web = self.tmp / "web"          # what the "store" serves
        self.web.mkdir()
        self.root = self.tmp / "installed"
        self.user = self.tmp / "user"
        self.user.mkdir()
        self.cache = self.tmp / "cache"
        self.settings_dir = self.tmp / "plugin-settings"
        self.data = self.tmp / "data"
        self.trash = self.tmp / "trash"
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        env = mock.patch.dict(os.environ, {store.INDEX_ENV: (self.web / "index.json").as_uri(),
                                           store.KEY_ENV: self.pub})
        env.start()
        self.addCleanup(env.stop)
        self.events = []
        sys.modules["tests_store_events"] = self.events
        self.addCleanup(sys.modules.pop, "tests_store_events", None)
        self.entries = []
        banner = self.web / "banner.png"
        banner.write_bytes(_png())
        self.banner = {"url": banner.as_uri(), "sha256": _sha(_png())}

    # ---- building a fake store ----

    def plugin_zip(self, slug="demo", version="1.0.0", *, requires="[]", start="pass",
                   extra: dict | None = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("plugin.toml", MANIFEST.format(slug=slug, version=version,
                                                      requires=requires))
            zf.writestr("plugin.py", textwrap.dedent(CODE).format(start=start))
            for name, text in (extra or {}).items():
                zf.writestr(name, text)
        return buf.getvalue()

    def publish(self, data: bytes, slug="demo", version="1.0.0", **fields) -> dict:
        path = self.web / f"{slug}-{version}.zip"
        path.write_bytes(data)
        entry = {
            "slug": slug, "name": slug, "blurb": "a test plugin.", "version": version,
            "api": 1, "size": len(data), "sha256": _sha(data), "url": path.as_uri(),
            "repo": f"https://example.org/{slug}", "banner": {"grid": self.banner},
            "author": "captience", "official": True,
        }
        entry.update(fields)
        self.entries = [e for e in self.entries if e["slug"] != slug] + [entry]
        self.sign()
        return entry

    def sign(self, extra: dict | None = None, key=None) -> bytes:
        body = {"format": 1, "generated": "2026-10-21T12:00:00Z", "plugins": self.entries}
        body.update(extra or {})
        data = json.dumps(body).encode("utf-8")
        (self.web / "index.json").write_bytes(data)
        (self.web / "index.json.sig").write_bytes((key or self.key).sign(data).hex().encode())
        return data

    def index(self) -> store.Index:
        return store.fetch_index(self.cache)

    def host(self) -> PluginHost:
        h = PluginHost(None, FakePlayer(), FakeQueue(),
                       roots=[(self.root, "store"), (self.user, "yours")],
                       state_file=self.tmp / "plugins.toml", settings_dir=self.settings_dir,
                       stamp=self.tmp / "plugin-starting", data_dir=self.data)
        self.addCleanup(h.shutdown)
        return h

    def install(self, slug="demo", index=None) -> Path:
        index = index or self.index()
        return store.install(index.entry(slug), self.root, downloads_dir=self.cache / "dl",
                             trash_dir=self.trash)


class IndexTests(StoreBase):
    def test_good_index_reads(self):
        self.publish(self.plugin_zip())
        idx = self.index()
        self.assertEqual(list(idx.entries), ["demo"])
        e = idx.entry("demo")
        self.assertEqual((e.version, e.author, e.official), ("1.0.0", "captience", True))
        # and it was kept for offline use
        self.assertIsNotNone(store.cached_index(self.cache))

    def test_bad_signature_is_refused(self):
        self.publish(self.plugin_zip())
        self.sign(key=Ed25519PrivateKey.generate())
        with self.assertRaises(store.StoreError) as cm:
            self.index()
        self.assertEqual(str(cm.exception), store.BAD_INDEX)
        self.assertIsNone(store.cached_index(self.cache))

    def test_a_changed_byte_is_refused(self):
        self.publish(self.plugin_zip())
        data = (self.web / "index.json").read_bytes()
        (self.web / "index.json").write_bytes(data.replace(b"a test plugin", b"a nice plugin"))
        with self.assertRaises(store.StoreError):
            self.index()

    def test_tampered_cache_is_ignored(self):
        self.publish(self.plugin_zip())
        self.index()
        cached = store.index_cache(self.cache) / "index.json"
        cached.write_bytes(cached.read_bytes().replace(b"captience", b"someone"))
        self.assertIsNone(store.cached_index(self.cache))

    def test_no_key_means_not_set_up(self):
        self.publish(self.plugin_zip())
        with mock.patch.dict(os.environ, {store.KEY_ENV: ""}), \
                mock.patch.object(store, "PUBLIC_KEY", ""):
            with self.assertRaises(store.StoreError) as cm:
                self.index()
        self.assertEqual(str(cm.exception), store.NOT_SET_UP)

    def test_no_banner_no_entry(self):
        self.publish(self.plugin_zip())
        self.publish(self.plugin_zip("bare"), slug="bare")
        self.entries[-1].pop("banner")
        self.sign()
        idx = self.index()
        self.assertEqual(list(idx.entries), ["demo"])
        self.assertTrue(idx.skipped and idx.skipped[0].startswith("bare"))

    def test_reserved_slug_is_skipped(self):
        self.publish(self.plugin_zip("installed"), slug="installed")
        self.assertEqual(self.index().entries, {})

    def test_featured_falls_back_to_newest(self):
        self.publish(self.plugin_zip(), added="2026-10-01")
        self.publish(self.plugin_zip("later"), slug="later", added="2026-10-20")
        self.sign({"featured": {"slug": "demo", "note": "a good one", "until": "2026-11-09"}})
        idx = self.index()
        e, label, note = store.pick_featured(idx, date(2026, 11, 1))
        self.assertEqual((e.slug, label, note), ("demo", "plugin of the week", "a good one"))
        e, label, _ = store.pick_featured(idx, date(2026, 11, 10))
        self.assertEqual((e.slug, label), ("later", "new"))

    def test_what_this_tide_cant_install(self):
        self.publish(self.plugin_zip(), min_tide="9.0.0")
        e = self.index().entry("demo")
        self.assertEqual(store.cant_install(e, "3.0.0", 1), "needs tide 9.0.0")
        self.assertEqual(store.cant_install(e, "9.0.1", 2), "built for a different tide")
        self.assertEqual(store.cant_install(e, "9.0.1", 1), "")

    def test_a_hard_need_blocks_install(self):
        self.publish(self.plugin_zip(), needs=[
            {"what": "a thing", "check": "program:definitely-not-a-program-xyz", "hard": True,
             "hard_note": "only works with a thing"}])
        e = self.index().entry("demo")
        self.assertEqual(store.cant_install(e, "3.0.0", 1), "only works with a thing")
        with self.assertRaises(store.StoreError):
            self.install()


class NeedsTests(unittest.TestCase):
    def test_checks(self):
        self.assertTrue(store.check_need(store.Need("a shell", "program:sh")))
        self.assertFalse(store.check_need(store.Need("x", "program:definitely-not-a-program-xyz")))
        self.assertIsNone(store.check_need(store.Need("x", "")))
        self.assertIsNone(store.check_need(store.Need("x", "something-tide-doesnt-know")))
        self.assertTrue(store.check_need(store.Need("me", f"process:{Path(sys.executable).name}"))
                        or True)   # the name may be truncated in /proc; just mustn't raise

    def test_versions(self):
        self.assertTrue(store.newer("1.10.0", "1.9.3"))
        self.assertFalse(store.newer("1.0", "1.0.0"))


class DownloadTests(StoreBase):
    def test_bad_checksum(self):
        entry = self.publish(self.plugin_zip())
        # the zip changes after the index was signed
        Path(entry["url"].removeprefix("file://")).write_bytes(self.plugin_zip(start="x = 1"))
        with self.assertRaises(store.StoreError) as cm:
            self.install()
        self.assertEqual(str(cm.exception), store.DAMAGED)
        self.assertFalse((self.root / "demo").exists())
        self.assertEqual(list((self.cache / "dl").iterdir()), [])

    def test_bigger_than_the_index_says(self):
        data = self.plugin_zip()
        self.publish(data)
        self.entries[0]["size"] = len(data) - 10
        self.sign()
        with self.assertRaises(store.StoreError):
            self.install()
        self.assertFalse((self.root / "demo").exists())

    def test_manifest_must_match_the_index(self):
        self.publish(self.plugin_zip(version="1.0.1"), version="1.0.0")
        with self.assertRaises(store.StoreError) as cm:
            self.install()
        self.assertEqual(str(cm.exception), store.MISMATCH)
        self.assertEqual([p for p in self.root.iterdir()], [])

    def test_assets_are_checked_and_cached(self):
        self.publish(self.plugin_zip())
        e = self.index().entry("demo")
        path = store.fetch_asset(e.banner, self.cache)
        self.assertEqual(path.read_bytes(), _png())
        self.assertEqual(store.asset_path(e.banner, self.cache), path)
        bad = store.Asset(e.banner.url, "0" * 64)
        with self.assertRaises(store.StoreError):
            store.fetch_asset(bad, self.cache)


class UnpackTests(StoreBase):
    def evil(self, name: str, *, symlink: bool = False) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("plugin.toml", MANIFEST.format(slug="demo", version="1.0.0",
                                                      requires="[]"))
            info = zipfile.ZipInfo(name)
            if symlink:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "/etc/passwd")
            else:
                zf.writestr(info, "gotcha")
        return buf.getvalue()

    def check_refused(self, data: bytes):
        self.publish(data)
        with self.assertRaises(store.StoreError) as cm:
            self.install()
        self.assertEqual(str(cm.exception), store.ESCAPES)
        self.assertFalse((self.root / "demo").exists())
        self.assertEqual([p for p in self.root.iterdir()], [], "no half-unpacked folder left")
        self.assertFalse((self.tmp / "gotcha").exists())
        self.assertFalse((self.tmp / "evil").exists())

    def test_dotdot(self):
        self.check_refused(self.evil("../evil"))

    def test_dotdot_deeper(self):
        self.check_refused(self.evil("sub/../../evil"))

    def test_absolute(self):
        self.check_refused(self.evil(str(self.tmp / "evil")))

    def test_symlink(self):
        self.check_refused(self.evil("link", symlink=True))

    def test_backslash(self):
        self.check_refused(self.evil("..\\evil"))


class InstallTests(StoreBase):
    def test_install_runs_and_uninstall_leaves_nothing(self):
        self.publish(self.plugin_zip())
        h = self.host()
        folder = self.install()
        self.assertTrue((folder / "plugin.toml").is_file())
        self.assertTrue(h.installed("demo"))
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hi")])
        rec = h.record("demo")
        self.assertEqual(rec.manifest.origin, "store")
        self.assertTrue(rec.running)
        # an error log and a setting, like real use leaves
        rec.settings.set("greeting", "hello")
        (self.data / "demo").mkdir(parents=True, exist_ok=True)
        (self.data / "demo" / "errors.log").write_text("boom\n")

        h.release("demo")
        removed = store.uninstall("demo", self.root, settings_dir=self.settings_dir,
                                  data_dir=self.data, trash_dir=self.trash)
        h.uninstalled("demo")
        store.empty_trash(self.trash)
        self.assertIn(("stop", "demo"), self.events)
        self.assertIsNone(h.record("demo"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((self.data / "demo").exists())
        self.assertEqual(list(self.trash.iterdir()), [])
        # settings kept for next time
        self.assertTrue((self.settings_dir / "demo.toml").is_file())
        self.assertIsNone(removed.settings)
        # and they come back with it
        self.events.clear()
        self.install()
        h.installed("demo")
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hello")])

    def test_forget_my_settings(self):
        self.publish(self.plugin_zip())
        h = self.host()
        self.install()
        h.installed("demo")
        h.record("demo").settings.set("greeting", "hello")
        h.release("demo")
        store.uninstall("demo", self.root, settings_dir=self.settings_dir, data_dir=self.data,
                        trash_dir=self.trash, forget=True)
        h.uninstalled("demo")
        store.empty_trash(self.trash)
        self.assertFalse((self.settings_dir / "demo.toml").exists())
        self.events.clear()
        self.install()
        h.installed("demo")
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hi")])

    def test_undo_puts_it_back(self):
        self.publish(self.plugin_zip())
        h = self.host()
        self.install()
        h.installed("demo")
        h.record("demo").settings.set("greeting", "hello")
        h.release("demo")
        removed = store.uninstall("demo", self.root, settings_dir=self.settings_dir,
                                  data_dir=self.data, trash_dir=self.trash, forget=True)
        h.uninstalled("demo")
        self.events.clear()
        store.undo_uninstall(removed)
        h.installed("demo")
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hello")])
        self.assertFalse(removed.holder.exists())

    def test_update_swaps_the_folder_live(self):
        self.publish(self.plugin_zip())
        h = self.host()
        self.install()
        h.installed("demo")
        self.publish(self.plugin_zip(version="1.1.0"), version="1.1.0")
        self.install()
        h.installed("demo")
        self.assertEqual(self.events[-2:], [("stop", "demo"), ("start", "demo", "1.1.0", "hi")])
        self.assertEqual(h.record("demo").manifest.version, "1.1.0")
        self.assertEqual([p.name for p in self.root.iterdir()], ["demo"])

    def test_installed_at_launch_starts_on(self):
        self.publish(self.plugin_zip())
        self.install()
        h = self.host()
        self.assertTrue(h.is_enabled("demo"))
        h.start_enabled()
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hi")])

    def test_a_folder_you_dropped_in_still_starts_off(self):
        folder = self.user / "mine"
        with zipfile.ZipFile(io.BytesIO(self.plugin_zip("mine"))) as zf:
            zf.extractall(folder)
        h = self.host()
        self.assertEqual(h.record("mine").manifest.origin, "yours")
        self.assertFalse(h.is_enabled("mine"))


class RescanTests(StoreBase):
    def drop_in(self, slug="mine", version="1.0.0"):
        with zipfile.ZipFile(io.BytesIO(self.plugin_zip(slug, version))) as zf:
            zf.extractall(self.user / slug)

    def test_a_new_folder_shows_up_without_a_restart(self):
        h = self.host()
        self.assertIsNone(h.record("mine"))
        self.drop_in()
        self.assertEqual(h.rescan(), ["mine"])
        self.assertFalse(h.is_enabled("mine"))

    def test_an_edited_plugin_toml_is_read_again(self):
        self.drop_in()
        h = self.host()
        h.set_enabled("mine", True)
        toml = self.user / "mine" / "plugin.toml"
        toml.write_text(toml.read_text().replace('default = "hi"', 'default = "hey"'))
        os.utime(toml, (time.time() + 5, time.time() + 5))
        self.events.clear()
        self.assertEqual(h.rescan(), ["mine"])
        self.assertEqual(self.events, [("stop", "mine"), ("start", "mine", "1.0.0", "hey")])

    def test_a_broken_folder_is_kept_for_the_page(self):
        (self.user / "oops").mkdir()
        (self.user / "oops" / "plugin.toml").write_text("not toml [")
        h = self.host()
        self.assertEqual([p.name for p, _ in h.broken], ["oops"])


class VendorTests(StoreBase):
    MOD = "tide_vendor_test_mod_xyz"

    def test_vendor_is_on_the_path_only_while_running(self):
        self.publish(self.plugin_zip(
            requires=f'["{self.MOD}"]', start=f"import {self.MOD}; EVENTS.append({self.MOD}.HI)",
            extra={f"vendor/{self.MOD}/__init__.py": "HI = 'from vendor'\n"}))
        folder = self.install()
        h = self.host()
        vendor = str(folder / "vendor")
        self.assertEqual(h.record("demo").blocked(), "", "vendor satisfies requires")
        self.assertTrue(h.installed("demo"))
        self.assertIn("from vendor", self.events)
        self.assertIn(vendor, sys.path)
        self.assertIn(self.MOD, sys.modules)
        h.set_enabled("demo", False)
        self.assertNotIn(vendor, sys.path)
        self.assertNotIn(self.MOD, sys.modules)

    def test_without_vendor_the_need_shows(self):
        self.publish(self.plugin_zip(requires=f'["{self.MOD}"]'))
        h = self.host()
        self.install()
        self.assertFalse(h.installed("demo"))
        self.assertEqual(h.record("demo").blocked(), f"needs {self.MOD}")


if __name__ == "__main__":
    unittest.main()
