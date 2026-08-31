"""v2.0 phase 2 — the glyph editor power tool (ui/glyph_editor.py).
Every key in glyphs.KEYS gets an edit + preview + reset row with a 1-3
character cap; typing previews through the live override layer and
reaches the parent window's refresh_glyphs (hasattr-guarded); cancel /
Esc leaves zero trace; accept persists exactly glyph_overrides via
save_fields, stale keys filtered; open_glyph_editor never constructs
inside the calling turn (the modal-from-click segfault rule).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from tide import config, glyphs, settings as settings_module
from tide.settings import Settings
from tide.ui import glyph_editor
from tide.ui.glyph_editor import GlyphEditorDialog, open_glyph_editor

# The shipped defaults, escape-spelled like test_registries — a
# lookalike codepoint can't sneak through a visual diff.
PLAY = "\u25b6"                 # ▶
PAUSE = "\u25ae\u25ae"          # ▮▮
NEXT = "\u25b8\u25b8"           # ▸▸
SHUFFLE = "\u21cb"              # ⇋
LIKE_ON = "\u2665"              # ♥


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _FakeWindow(QWidget):
    """Stands in for the MainWindow: integration builds refresh_glyphs;
    the editor only ever hasattr-probes for it."""

    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    def refresh_glyphs(self) -> None:
        self.refreshes += 1


class _EditorCase(unittest.TestCase):
    """Hermetic base: per-test settings path, glyph layer restored, and
    a queue drain AFTER the deleteLater cleanups (rule: window-building
    tests tear down properly)."""

    def setUp(self) -> None:
        _app()
        # Added first => runs last: drains every deleteLater queued by
        # the cleanups below before the next test starts.
        self.addCleanup(QTest.qWait, 20)
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-glyph-editor-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        self.addCleanup(self._restore_settings_file)
        self.addCleanup(glyphs.set_overrides, {})

    def _restore_settings_file(self) -> None:
        config.SETTINGS_FILE = self._orig_settings_file

    def _dialog(self, s: Settings, parent=None) -> GlyphEditorDialog:
        dlg = GlyphEditorDialog(s, parent)
        self.addCleanup(dlg.deleteLater)
        return dlg


class _StubbedCase(_EditorCase):
    """Settings writes stubbed + recorded — the dialog cases assert the
    call shape and must not be able to touch disk at all."""

    def setUp(self) -> None:
        super().setUp()
        self.saved_field_calls: list[tuple[str, ...]] = []
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(
            settings_module, "save_fields",
            lambda s, *names: self.saved_field_calls.append(tuple(names)),
        ).start()
        self.addCleanup(mock.patch.stopall)


# ---------- rows ----------

class RowTests(_StubbedCase):
    def test_window_title(self) -> None:
        dlg = self._dialog(Settings())
        self.assertEqual(dlg.windowTitle(), "tide — glyphs")

    def test_every_vocabulary_key_has_a_row(self) -> None:
        dlg = self._dialog(Settings())
        for key in glyphs.KEYS:
            with self.subTest(key=key):
                self.assertIsNotNone(dlg.edit_for(key))
                self.assertIsNotNone(dlg.preview_for(key))
                self.assertIsNotNone(dlg.reset_for(key))
                self.assertEqual(dlg.base_glyph(key), glyphs.glyph(key))

    def test_edit_caps_at_three_characters(self) -> None:
        dlg = self._dialog(Settings())
        dlg.edit_for("play").setText("abcd")
        self.assertEqual(dlg.edit_for("play").text(), "abc")

    def test_existing_overrides_populate(self) -> None:
        s = Settings()
        s.glyph_overrides = {"play": "P"}
        glyphs.set_overrides({"play": "P"})   # boot contract: live == settings
        dlg = self._dialog(s)
        self.assertEqual(dlg.edit_for("play").text(), "P")
        self.assertEqual(dlg.preview_for("play").text(), "P")
        self.assertEqual(dlg.edit_for("pause").text(), "")
        self.assertEqual(dlg.preview_for("pause").text(), PAUSE)

    def test_pack_column_shows_the_override_free_base(self) -> None:
        # With an override active, the base column still shows what
        # [reset] returns to — the pack's glyph, not the override.
        s = Settings()
        s.glyph_overrides = {"play": "P"}
        glyphs.set_overrides({"play": "P"})
        dlg = self._dialog(s)
        self.assertEqual(dlg.base_glyph("play"), PLAY)
        self.assertEqual(glyphs.glyph("play"), "P")

    def test_stale_keys_from_another_version_do_not_crash(self) -> None:
        s = Settings()
        s.glyph_overrides = {"play": "P", "warp": "W"}
        glyphs.set_overrides(s.glyph_overrides)   # the module filters too
        dlg = self._dialog(s)
        self.assertEqual(dlg.edit_for("play").text(), "P")
        self.assertNotIn("warp", dlg.current_overrides())


# ---------- live preview ----------

class LivePreviewTests(_StubbedCase):
    def test_typing_previews_in_dialog_and_through_the_registry(self) -> None:
        s = Settings()
        dlg = self._dialog(s)
        dlg.edit_for("shuffle").setText("xX")
        self.assertEqual(dlg.preview_for("shuffle").text(), "xX")
        self.assertEqual(glyphs.glyph("shuffle"), "xX",
                         "live preview must ride the override layer")
        self.assertEqual(s.glyph_overrides, {})
        self.assertEqual(self.saved_field_calls, [])

    def test_typing_the_pack_glyph_back_is_not_an_override(self) -> None:
        dlg = self._dialog(Settings())
        dlg.edit_for("shuffle").setText(SHUFFLE)
        self.assertNotIn("shuffle", dlg.current_overrides())
        self.assertEqual(glyphs.glyph("shuffle"), SHUFFLE)

    def test_reset_row_falls_back_to_the_pack(self) -> None:
        dlg = self._dialog(Settings())
        dlg.edit_for("play").setText("P")
        self.assertEqual(glyphs.glyph("play"), "P")
        dlg.reset_for("play").click()
        self.assertEqual(dlg.edit_for("play").text(), "")
        self.assertEqual(dlg.preview_for("play").text(), PLAY)
        self.assertEqual(glyphs.glyph("play"), PLAY)

    def test_reset_all_clears_every_row(self) -> None:
        s = Settings()
        s.glyph_overrides = {"like_on": "L"}
        glyphs.set_overrides({"like_on": "L"})
        dlg = self._dialog(s)
        dlg.edit_for("play").setText("P")
        dlg.edit_for("pause").setText("::")
        dlg.reset_all_btn.click()
        for key in glyphs.KEYS:
            with self.subTest(key=key):
                self.assertEqual(dlg.edit_for(key).text(), "")
                self.assertEqual(dlg.preview_for(key).text(),
                                 dlg.base_glyph(key))
        self.assertEqual(dlg.current_overrides(), {})
        self.assertEqual(glyphs.glyph("like_on"), LIKE_ON)

    def test_live_edits_reach_refresh_glyphs_up_the_parent_chain(self) -> None:
        win = _FakeWindow()
        self.addCleanup(win.deleteLater)
        host = QWidget(win)   # the editor may be parented deeper (settings dialog)
        dlg = self._dialog(Settings(), parent=host)
        dlg.edit_for("play").setText("P")
        self.assertGreaterEqual(win.refreshes, 1,
                                "a live edit must repaint the window")

    def test_no_refresh_glyphs_attr_is_fine(self) -> None:
        # Integration builds MainWindow.refresh_glyphs later — until
        # then (and for any bare parent) the walk must just no-op.
        host = QWidget()
        self.addCleanup(host.deleteLater)
        dlg = self._dialog(Settings(), parent=host)
        dlg.edit_for("play").setText("P")     # must not raise
        dlg.save_btn.click()                  # must not raise
        self.assertEqual(dlg.result(), QDialog.Accepted)


# ---------- cancel — previews never commit ----------

class CancelTests(_StubbedCase):
    def test_cancel_leaves_zero_trace(self) -> None:
        s = Settings()
        s.glyph_overrides = {"like_on": "L"}
        glyphs.set_overrides({"like_on": "L"})
        dlg = self._dialog(s)
        dlg.edit_for("play").setText("P")
        dlg.edit_for("pause").setText("::")
        dlg.reset_for("like_on").click()      # even a reset is a preview
        self.assertEqual(glyphs.glyph("play"), "P")
        dlg.cancel_btn.click()
        self.assertEqual(dlg.result(), QDialog.Rejected)
        self.assertEqual(glyphs.glyph("play"), PLAY)
        self.assertEqual(glyphs.glyph("pause"), PAUSE)
        self.assertEqual(glyphs.glyph("like_on"), "L")
        self.assertEqual(s.glyph_overrides, {"like_on": "L"})
        self.assertEqual(self.saved_field_calls, [])
        self.assertFalse(config.SETTINGS_FILE.exists())

    def test_escape_reject_reverts_too(self) -> None:
        # Esc and the titlebar close both land in QDialog.reject —
        # overridden to revert, so they can't leak a preview.
        s = Settings()
        dlg = self._dialog(s)
        dlg.edit_for("next").setText(">>")
        self.assertEqual(glyphs.glyph("next"), ">>")
        dlg.reject()
        self.assertEqual(glyphs.glyph("next"), NEXT)
        self.assertEqual(s.glyph_overrides, {})
        self.assertEqual(self.saved_field_calls, [])

    def test_cancel_repaints_the_window_with_the_revert(self) -> None:
        win = _FakeWindow()
        self.addCleanup(win.deleteLater)
        dlg = self._dialog(Settings(), parent=win)
        dlg.edit_for("play").setText("P")
        before = win.refreshes
        dlg.cancel_btn.click()
        self.assertGreater(win.refreshes, before,
                           "the revert must reach the window's labels")
        self.assertEqual(glyphs.glyph("play"), PLAY)


# ---------- accept — the only write path ----------

class AcceptTests(_StubbedCase):
    def test_accept_persists_exactly_glyph_overrides(self) -> None:
        s = Settings()
        dlg = self._dialog(s)
        dlg.edit_for("play").setText("P")
        dlg.edit_for("pause").setText("::")
        dlg.save_btn.click()
        self.assertEqual(dlg.result(), QDialog.Accepted)
        self.assertEqual(s.glyph_overrides, {"play": "P", "pause": "::"})
        self.assertEqual(self.saved_field_calls, [("glyph_overrides",)],
                         "accept must field-save glyph_overrides, once, "
                         "and nothing else")
        # The live layer stays applied.
        self.assertEqual(glyphs.glyph("play"), "P")
        self.assertEqual(glyphs.glyph("pause"), "::")

    def test_untouched_accept_writes_nothing(self) -> None:
        s = Settings()
        s.glyph_overrides = {"play": "P"}
        glyphs.set_overrides({"play": "P"})
        dlg = self._dialog(s)
        dlg.save_btn.click()
        self.assertEqual(self.saved_field_calls, [])
        self.assertEqual(s.glyph_overrides, {"play": "P"})
        self.assertEqual(glyphs.glyph("play"), "P")

    def test_clearing_an_override_saves_the_removal(self) -> None:
        s = Settings()
        s.glyph_overrides = {"play": "P"}
        glyphs.set_overrides({"play": "P"})
        dlg = self._dialog(s)
        dlg.reset_for("play").click()
        dlg.save_btn.click()
        self.assertEqual(s.glyph_overrides, {})
        self.assertEqual(self.saved_field_calls, [("glyph_overrides",)])
        self.assertEqual(glyphs.glyph("play"), PLAY)

    def test_accept_drops_stale_keys(self) -> None:
        # A stale key from another version is filtered on the way in;
        # accepting (with any edit) persists the cleaned set.
        s = Settings()
        s.glyph_overrides = {"play": "P", "warp": "W"}
        glyphs.set_overrides(s.glyph_overrides)
        dlg = self._dialog(s)
        dlg.save_btn.click()
        self.assertEqual(s.glyph_overrides, {"play": "P"})
        self.assertEqual(self.saved_field_calls, [("glyph_overrides",)])

    def test_accept_calls_refresh_glyphs_when_present(self) -> None:
        win = _FakeWindow()
        self.addCleanup(win.deleteLater)
        dlg = self._dialog(Settings(), parent=win)
        dlg.edit_for("play").setText("P")
        before = win.refreshes
        dlg.save_btn.click()
        self.assertGreater(win.refreshes, before)


# ---------- disk round-trip — the real save_fields, sandboxed path ----------

class DiskRoundTripTests(_EditorCase):
    """No stubs: accept writes exactly glyph_overrides to the settings
    file (field-scoped merge), cancel never creates it."""

    def test_accept_round_trips_through_the_settings_file(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        dlg = self._dialog(s)
        dlg.edit_for("next").setText("»")   # »
        dlg.save_btn.click()
        self.assertTrue(config.SETTINGS_FILE.exists())
        back = settings_module.load()
        self.assertEqual(back.glyph_overrides, {"next": "»"})

    def test_field_scoped_save_leaves_other_fields_alone(self) -> None:
        # Another saver's field on disk must survive the editor's save —
        # the reason save_fields exists at all.
        disk = Settings()
        disk.first_launch_complete = True
        disk.theme = "gruvbox"
        settings_module.save(disk)
        s = Settings()               # a STALE object: theme still default
        s.first_launch_complete = True
        dlg = self._dialog(s)
        dlg.edit_for("play").setText("P")
        dlg.save_btn.click()
        back = settings_module.load()
        self.assertEqual(back.glyph_overrides, {"play": "P"})
        self.assertEqual(back.theme, "gruvbox",
                         "the editor's save clobbered another field")

    def test_cancel_never_touches_disk(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        dlg = self._dialog(s)
        dlg.edit_for("play").setText("P")
        dlg.cancel_btn.click()
        self.assertFalse(config.SETTINGS_FILE.exists())


# ---------- the deferred-dialog rule ----------

class DeferredOpenTests(_EditorCase):
    def test_open_is_deferred_out_of_the_calling_turn(self) -> None:
        host = QWidget()
        self.addCleanup(host.deleteLater)
        open_glyph_editor(Settings(), host)
        self.assertEqual(len(glyph_editor._open_dialogs), 0,
                         "the dialog was constructed inside the calling "
                         "turn — the modal-from-click crash pattern")
        QTest.qWait(30)
        self.assertEqual(len(glyph_editor._open_dialogs), 1)
        dlg = next(iter(glyph_editor._open_dialogs))
        self.assertTrue(dlg.isVisible())
        dlg.reject()
        QTest.qWait(30)
        self.assertEqual(len(glyph_editor._open_dialogs), 0,
                         "finished must release the module's reference")

    def test_parentless_open_survives_gc(self) -> None:
        # open() doesn't block like exec(); without the module's strong
        # reference a parentless dialog would be collected mid-open.
        import gc
        open_glyph_editor(Settings(), None)
        QTest.qWait(30)
        gc.collect()
        self.assertEqual(len(glyph_editor._open_dialogs), 1)
        dlg = next(iter(glyph_editor._open_dialogs))
        self.assertTrue(dlg.isVisible())
        dlg.reject()
        QTest.qWait(30)
        self.assertEqual(len(glyph_editor._open_dialogs), 0)


if __name__ == "__main__":
    unittest.main()
