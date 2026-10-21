"""The store page (ui/store_view.py): the controller's button states and
actions against a fake signed index, the undo toast, updates and their
dot count, and the widgets built offscreen one at a time (cards, the
plugin page with its live settings, the settings dialog's one line).
No full-window renders.
"""
import os
import time
import unittest
from datetime import date, timedelta

import shiboken6
from PySide6.QtWidgets import QApplication

from test_store import StoreBase
from tide import store
from tide.ui import store_view
from tide.ui.store_view import PluginPage, StoreController, StoreView

_app = QApplication.instance() or QApplication([])


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class ViewBase(StoreBase):
    def setUp(self):
        super().setUp()
        self.toasts = []

    def ctl(self, host=None) -> StoreController:
        h = host or self.host()
        c = StoreController(h, root=self.root, cache_dir=self.cache,
                            settings_dir=self.settings_dir, data_dir=self.data,
                            toast=lambda text, label, fn: self.toasts.append((text, label, fn)))
        self.addCleanup(lambda: shiboken6.isValid(c) and shiboken6.delete(c))
        return c

    def fetched(self, c: StoreController) -> None:
        c.refresh(force=True)
        self.assertTrue(_wait(lambda: not c.fetching()))

    def installed(self, c: StoreController, slug="demo") -> None:
        c.install(slug)
        self.assertEqual(c.state(slug)[0], "busy")
        self.assertTrue(_wait(lambda: c.state(slug)[0] != "busy"))

    def widget(self, w):
        self.addCleanup(lambda: shiboken6.isValid(w) and shiboken6.delete(w))
        return w


class ControllerTests(ViewBase):
    def test_install_then_uninstall_then_undo(self):
        self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        self.assertEqual(c.state("demo"), ("install", "install"))
        self.installed(c)
        self.assertEqual(c.state("demo"), ("installed", "uninstall"))
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hi")])
        self.assertEqual(c.status("demo"), "on")

        c.press("demo")
        self.assertEqual(c.state("demo")[0], "install")
        self.assertEqual(self.toasts[-1][:2], ("uninstalled demo", "undo"))
        self.assertIn(("stop", "demo"), self.events)
        self.assertFalse((self.root / "demo").exists())

        self.events.clear()
        self.toasts[-1][2]()                     # the toast's undo
        self.assertEqual(c.state("demo")[0], "installed")
        self.assertEqual(self.events, [("start", "demo", "1.0.0", "hi")])

    def test_undo_window_closing_deletes_the_trash(self):
        self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        self.installed(c)
        removed = c.uninstall("demo")
        self.assertTrue(removed.holder.exists())
        c._finish_undo(removed)
        self.assertFalse(removed.holder.exists())
        self.assertFalse(c.undo(removed))

    def test_forget_from_the_button(self):
        self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        self.installed(c)
        c.host.record("demo").settings.set("greeting", "hello")
        c.press("demo", forget=True)
        c._finish_undo()
        self.assertFalse((self.settings_dir / "demo.toml").exists())

    def test_a_damaged_download_says_so(self):
        entry = self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        (self.web / "demo-1.0.0.zip").write_bytes(self.plugin_zip(start="x = 2"))
        self.assertTrue(entry)
        self.installed(c)
        self.assertEqual(c.state("demo")[0], "install")
        self.assertEqual(c.status("demo"), store.DAMAGED)
        self.assertFalse((self.root / "demo").exists())

    def test_updates_count_and_install(self):
        self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        self.installed(c)
        counts = []
        c.updates_changed.connect(counts.append)
        self.publish(self.plugin_zip(version="1.1.0"), version="1.1.0")
        self.fetched(c)
        self.assertEqual(c.state("demo"), ("update", "update"))
        self.assertEqual(counts, [1])
        self.installed(c)
        self.assertEqual(c.host.record("demo").manifest.version, "1.1.0")
        self.assertEqual(counts, [1, 0])

    def test_offline_keeps_what_you_have(self):
        self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        self.installed(c)
        os.environ[store.INDEX_ENV] = (self.web / "gone" / "index.json").as_uri()
        self.fetched(c)
        self.assertEqual(c.offline, store.NO_NETWORK)
        self.assertEqual(c.state("demo")[0], "installed")    # the cached index still answers

    def test_offline_with_no_cache_lists_installed_plugins(self):
        import shutil
        self.publish(self.plugin_zip())
        self.install()
        shutil.rmtree(store.index_cache(self.cache))
        os.environ[store.INDEX_ENV] = (self.web / "gone" / "index.json").as_uri()
        c = self.ctl()
        self.fetched(c)
        self.assertIsNone(c.index)
        self.assertEqual([r.manifest.slug for r in c.local_records()], ["demo"])
        self.assertEqual(c.origin_note(c.host.record("demo")), "")
        self.assertEqual(c.state("demo")[0], "installed")

    def test_your_own_plugin_turns_on_and_off(self):
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(self.plugin_zip("mine"))) as zf:
            zf.extractall(self.user / "mine")
        c = self.ctl()
        self.assertEqual(c.state("mine"), ("yours", "turn on"))
        c.press("mine")
        self.assertEqual(c.state("mine"), ("yours", "turn off"))
        self.assertEqual(self.events, [("start", "mine", "1.0.0", "hi")])

    def test_cant_install_says_why(self):
        self.publish(self.plugin_zip(), min_tide="99.0.0")
        c = self.ctl()
        self.fetched(c)
        self.assertEqual(c.state("demo"), ("cant", "needs tide 99.0.0"))


class WidgetTests(ViewBase):
    def test_grid_filters_and_search(self):
        self.publish(self.plugin_zip(), category="hardware")
        self.publish(self.plugin_zip("other"), slug="other", category="visuals",
                     blurb="makes things glow.")
        c = self.ctl()
        self.fetched(c)
        view = self.widget(StoreView(c))
        grid = view.grid
        self.assertEqual(sorted(card.slug for card in grid.cards()), ["demo", "other"])
        grid.set_filter("hardware")
        self.assertEqual([card.slug for card in grid.cards()], ["demo"])
        grid.set_filter("installed")
        self.assertEqual(grid.cards(), [])
        self.assertEqual(grid.empty.text(), "nothing installed yet.")
        grid.set_filter("all")
        grid.search.setText("glow")
        self.assertEqual([card.slug for card in grid.cards()], ["other"])

    def test_card_button_follows_the_install(self):
        self.publish(self.plugin_zip())
        c = self.ctl()
        self.fetched(c)
        view = self.widget(StoreView(c))
        card = view.grid.cards()[0]
        self.assertEqual(card.button.button.text().strip("[]"), "install")
        card.button.button.click()
        self.assertTrue(card.button.bar.isVisibleTo(card))
        self.assertTrue(_wait(lambda: c.state("demo")[0] == "installed"))
        card = view.grid.cards()[0]
        self.assertTrue(card.button.mark.isVisibleTo(card))
        self.assertFalse(card.button.button.isVisibleTo(card))

    def test_featured_tile(self):
        self.publish(self.plugin_zip(), added="2026-10-01")
        until = (date.today() + timedelta(days=3)).isoformat()
        self.sign({"featured": {"slug": "demo", "note": "the one to try", "until": until}})
        c = self.ctl()
        self.fetched(c)
        view = self.widget(StoreView(c))
        tile = view.grid.featured
        self.assertFalse(tile.isHidden())
        self.assertEqual(tile.label.text(), "plugin of the week")
        self.assertEqual(tile.note.text(), "the one to try")

    def test_offline_line(self):
        os.environ[store.INDEX_ENV] = (self.web / "gone" / "index.json").as_uri()
        c = self.ctl()
        view = self.widget(StoreView(c))
        self.fetched(c)
        self.assertEqual(view.grid.line.text(),
                         "couldn't reach the store. your plugins still work.")

    def test_plugin_page_settings_apply_live(self):
        code_start = ("ctx.settings_changed.connect(lambda keys: EVENTS.append("
                      "('settings', sorted(keys))))")
        self.publish(self.plugin_zip(start=code_start), needs=[
            {"what": "a shell", "check": "program:sh"},
            {"what": "a unicorn", "check": "program:definitely-not-xyz",
             "how": "sudo pacman -S unicorn"}], uses=["playback", "network"])
        c = self.ctl()
        self.fetched(c)
        page = self.widget(PluginPage(c))
        page.show_plugin("demo")
        self.assertIsNone(page.form)                       # nothing to set until installed
        self.assertTrue(page.forget.isHidden())
        needs = [page.needs.itemAt(i).widget().text() for i in range(page.needs.count())]
        self.assertEqual(needs, ["✓ a shell", "✗ a unicorn: sudo pacman -S unicorn"])
        self.assertTrue(page.can.text().startswith(
            "can see and control what's playing, talk to the network."))
        self.assertIn("by captience · official", page.author.text())

        self.installed(c)
        self.assertIsNotNone(page.form)
        self.assertFalse(page.forget.isHidden())
        box = page.form.widgets["greeting"]
        box.setText("hello")
        box.editingFinished.emit()
        _wait(lambda: ("settings", ["greeting"]) in self.events, 1.0)
        self.assertIn(("settings", ["greeting"]), self.events)
        self.assertEqual(c.host.record("demo").settings["greeting"], "hello")

    def test_broken_folders_are_listed(self):
        (self.user / "oops").mkdir()
        (self.user / "oops" / "plugin.toml").write_text("not toml [")
        c = self.ctl()
        view = self.widget(StoreView(c))
        self.assertIn("oops: plugin.toml doesn't parse", view.grid.broken.text())
        self.assertFalse(view.grid.broken_head.isHidden())

    def test_your_own_plugin_has_reload(self):
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(self.plugin_zip("mine"))) as zf:
            zf.extractall(self.user / "mine")
        c = self.ctl()
        page = self.widget(PluginPage(c))
        page.show_plugin("mine")
        self.assertFalse(page.reload_btn.isHidden())
        self.assertEqual(page.author.text(), "not from the store")

    def test_can_line_is_plain(self):
        self.assertEqual(store_view.can_line([]),
                         "it runs as you, with your permissions, like any plugin.")
        self.assertEqual(store_view.size_text(1843200), "1.8 mb")


class DialogTests(unittest.TestCase):
    def test_settings_dialog_points_at_the_store(self):
        from PySide6.QtWidgets import QLabel
        from tide.settings import Settings
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        self.addCleanup(lambda: shiboken6.isValid(dlg) and dlg.reject() is None
                        and shiboken6.delete(dlg))
        names = [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())]
        self.assertEqual(names[-2:], ["plugins", "about"])
        page = dlg._tabs.widget(names.index("plugins"))
        texts = [lab.text() for lab in page.findChildren(QLabel)]
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("plugins live in the store now"))


if __name__ == "__main__":
    unittest.main()
