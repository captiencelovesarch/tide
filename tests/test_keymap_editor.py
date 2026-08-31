"""Keymap editor + the ACTIONS shortcut table (v2.0 phase 2).

What's pinned here:
- the ACTIONS table carries every v1 shortcut with its exact default —
  the full id→sequence map is asserted so no binding can silently
  regress out of the extraction;
- _wire_shortcuts builds from the table, rebind_shortcuts applies a
  changed settings.keymap to the live QShortcuts without a restart, and
  an empty-string binding really unbinds;
- tooltips (shuffle / repeat / sleep / fullscreen) and the audio-fx
  popover hint derive from the live keymap instead of hardcoding key
  names;
- the editor: rows for every action, live conflict highlight,
  cancel = zero trace (settings object, disk, main window untouched),
  accept persists ONLY the keymap field and stores only deviations from
  the defaults;
- open_keymap_editor defers the modal out of the calling emission
  (the PySide6 + py3.14 modal-from-click crash rule).

Run offscreen:
  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_keymap_editor.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from tide import config, theming
from tide import settings as settings_module
from tide.settings import Settings


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# The shipped bindings, verbatim from v1's hand-wired QShortcut lines.
# This is the anti-regression pin: extracting the table must not lose,
# rename or re-key a single shortcut.
EXPECTED_DEFAULTS = {
    "search": "Ctrl+L",
    "search_alt": "Ctrl+F",
    "view_home": "Ctrl+1",
    "view_library": "Ctrl+2",
    "view_queue": "Ctrl+3",
    "view_lyrics": "Ctrl+4",
    "view_history": "Ctrl+5",
    "view_visualizer": "Ctrl+6",
    "view_source": "Ctrl+7",
    "view_audio_fx": "Ctrl+8",
    "open_settings": "Ctrl+9",
    "open_settings_alt": "Ctrl+,",
    "play_pause": "Space",
    "next_track": "Ctrl+Right",
    "prev_track": "Ctrl+Left",
    "volume_up": "Ctrl+Up",
    "volume_down": "Ctrl+Down",
    "like": "Ctrl+H",
    "shuffle": "Ctrl+S",
    "repeat": "Ctrl+R",
    "speed_slower": "[",
    "speed_faster": "]",
    "speed_reset": "\\",
    "sleep_timer": "Ctrl+I",
    "fullscreen": "F11",
    "mini_mode": "Ctrl+M",
    "refresh_session": "Ctrl+Shift+R",
}


class ActionsTableTests(unittest.TestCase):
    """The module-level table itself — no window needed."""

    def setUp(self) -> None:
        _app()

    def test_defaults_match_v1_exactly(self) -> None:
        from tide.ui.window import default_keymap
        self.assertEqual(default_keymap(), EXPECTED_DEFAULTS)

    def test_ids_unique_and_rows_sane(self) -> None:
        from tide.ui.window import ACTIONS
        ids = [a.id for a in ACTIONS]
        self.assertEqual(len(ids), len(set(ids)), "duplicate action id")
        for a in ACTIONS:
            self.assertTrue(a.label, f"{a.id}: empty label")
            self.assertTrue(a.group, f"{a.id}: empty group")
            self.assertTrue(callable(a.run), f"{a.id}: handler not callable")
            self.assertFalse(QKeySequence(a.default).isEmpty(),
                             f"{a.id}: default {a.default!r} doesn't parse")

    def test_no_two_defaults_collide(self) -> None:
        from tide.ui.window import ACTIONS
        seqs = [QKeySequence(a.default).toString(QKeySequence.PortableText)
                for a in ACTIONS]
        self.assertEqual(len(seqs), len(set(seqs)),
                         "two actions ship the same default key")

    def test_effective_keymap_overrides_ride_the_defaults(self) -> None:
        from tide.ui.window import effective_keymap
        s = Settings()
        s.keymap = {"shuffle": "Ctrl+B", "view_queue": ""}
        km = effective_keymap(s)
        self.assertEqual(km["shuffle"], "Ctrl+B")
        self.assertEqual(km["view_queue"], "")           # unbound
        self.assertEqual(km["repeat"], "Ctrl+R")         # untouched default

    def test_effective_keymap_ignores_unknown_ids(self) -> None:
        from tide.ui.window import effective_keymap
        s = Settings()
        s.keymap = {"warp_drive": "Ctrl+W"}
        km = effective_keymap(s)
        self.assertNotIn("warp_drive", km,
                         "a stale config binding leaked into the keymap")
        self.assertEqual(km, EXPECTED_DEFAULTS)

    def test_effective_keymap_survives_no_settings(self) -> None:
        from tide.ui.window import effective_keymap
        self.assertEqual(effective_keymap(None), EXPECTED_DEFAULTS)


class _WindowCase(unittest.TestCase):
    """Full-MainWindow cases — spy the app-wide QSS pushes and tear the
    window down hard (test_preset_flip's pattern: leaked windows turn
    every later restyle quadratic)."""

    def setUp(self) -> None:
        self.app = _app()
        # Hermetic settings file: a window wearing a Settings object
        # persists window_sizes on close (the phase-1 size memory) —
        # that write must not land on the shared sandbox path other
        # test files assert against.
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-keymap-w-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        theming.manager().apply("nord")
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self.w = None

    def tearDown(self) -> None:
        if self.w is not None:
            try:
                theming.manager().theme_changed.disconnect(
                    self.w._on_theme_changed)
            except (RuntimeError, TypeError):
                pass
            self.w.close()
            self.w.deleteLater()
            self.w = None
        QTest.qWait(30)
        config.SETTINGS_FILE = self._orig_settings_file

    def _make_window(self):
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        return self.w


class WindowShortcutTests(_WindowCase):
    def test_every_action_has_a_live_shortcut(self) -> None:
        from tide.ui.window import ACTIONS
        w = self._make_window()
        self.assertEqual(set(w._shortcuts), {a.id for a in ACTIONS})
        for a in ACTIONS:
            self.assertEqual(
                w._shortcuts[a.id].key().toString(QKeySequence.PortableText),
                QKeySequence(a.default).toString(QKeySequence.PortableText),
                f"{a.id} wired with the wrong default")

    def test_rebind_applies_without_restart(self) -> None:
        w = self._make_window()
        s = Settings()
        s.keymap = {"view_queue": "Ctrl+G"}
        w._settings = s
        w.rebind_shortcuts()
        self.assertEqual(
            w._shortcuts["view_queue"].key().toString(
                QKeySequence.PortableText),
            "Ctrl+G")
        w.show()
        QTest.qWait(30)
        QTest.keyClick(w, Qt.Key_G, Qt.ControlModifier)
        QTest.qWait(20)
        self.assertEqual(w.stack.currentIndex(), 2,
                         "the rebound key didn't reach the action")
        # The old default is gone — Ctrl+3 must no longer switch views.
        w._switch_view("home")
        QTest.qWait(20)
        QTest.keyClick(w, Qt.Key_3, Qt.ControlModifier)
        QTest.qWait(20)
        self.assertEqual(w.stack.currentIndex(), 0,
                         "the replaced default binding still fires")

    def test_empty_binding_unbinds(self) -> None:
        w = self._make_window()
        s = Settings()
        s.keymap = {"view_queue": ""}
        w._settings = s
        w.rebind_shortcuts()
        w.show()
        QTest.qWait(30)
        QTest.keyClick(w, Qt.Key_3, Qt.ControlModifier)
        QTest.qWait(20)
        self.assertEqual(w.stack.currentIndex(), 0,
                         "an unbound action still fired")

    def test_tooltips_keep_v1_wording_on_defaults(self) -> None:
        w = self._make_window()
        self.assertEqual(w.shuffle_btn.toolTip(), "shuffle (ctrl+s)")
        self.assertEqual(w.repeat_btn.toolTip(),
                         "repeat: off / all / one (ctrl+r)")
        self.assertEqual(w.sleep_btn.toolTip(), "sleep timer (ctrl+i)")
        self.assertEqual(w.fullscreen_btn.toolTip(), "fullscreen (f11)")

    def test_tooltips_follow_a_rebind(self) -> None:
        w = self._make_window()
        s = Settings()
        s.keymap = {"shuffle": "Ctrl+B", "sleep_timer": ""}
        w._settings = s
        w.rebind_shortcuts()
        self.assertEqual(w.shuffle_btn.toolTip(), "shuffle (ctrl+b)")
        # Unbound: no parenthetical — never advertise a dead key.
        self.assertEqual(w.sleep_btn.toolTip(), "sleep timer")

    def test_binding_display(self) -> None:
        w = self._make_window()
        self.assertEqual(w.binding_display("shuffle"), "ctrl+s")
        self.assertEqual(w.binding_display("fullscreen"), "f11")
        self.assertEqual(w.binding_display("nope"), "")
        s = Settings()
        s.keymap = {"shuffle": ""}
        w._settings = s
        self.assertEqual(w.binding_display("shuffle"), "")


class PopoverHintTests(_WindowCase):
    def test_hint_derives_from_the_window_keymap(self) -> None:
        from tide.audio_fx import AudioFxState
        from tide.ui.audio_fx_popover import AudioFxPopover
        w = self._make_window()
        pop = AudioFxPopover(w)   # parented: the window teardown owns it
        self.assertEqual(pop._hint.text(), "ctrl+8 → full panel")
        s = Settings()
        s.keymap = {"view_audio_fx": "Ctrl+0"}
        w._settings = s
        pop.sync(AudioFxState())     # every open re-syncs → re-derives
        self.assertEqual(pop._hint.text(), "ctrl+0 → full panel")
        s.keymap = {"view_audio_fx": ""}
        pop.sync(AudioFxState())
        self.assertEqual(pop._hint.text(), "full panel → the [fx] nav tab")

    def test_standalone_popover_falls_back_to_the_default(self) -> None:
        from tide.ui.audio_fx_popover import AudioFxPopover
        pop = AudioFxPopover(None)
        self.addCleanup(pop.deleteLater)
        self.assertEqual(pop._hint.text(), "ctrl+8 → full panel")


class _StubWindow(QWidget):
    """Parent that quacks like the MainWindow for _find_main_window."""

    def __init__(self) -> None:
        super().__init__()
        self.rebind_calls = 0

    def rebind_shortcuts(self) -> None:
        self.rebind_calls += 1


class EditorTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        # Hermetic settings file — accept must write it, cancel must not.
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-keymap-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        self.addCleanup(self._restore_settings_file)

    def _restore_settings_file(self) -> None:
        config.SETTINGS_FILE = self._orig_settings_file
        QTest.qWait(20)   # drain deleteLater'd dialogs

    def _editor(self, s: Settings, parent=None):
        from tide.ui.keymap_editor import KeymapEditor
        dlg = KeymapEditor(s, parent)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_a_row_for_every_action(self) -> None:
        from tide.ui.window import ACTIONS
        dlg = self._editor(Settings())
        ids = {a.id for a in ACTIONS}
        self.assertEqual(set(dlg._edits), ids)
        self.assertEqual(set(dlg._current_labels), ids)
        self.assertEqual(set(dlg._reset_btns), ids)

    def test_rows_show_the_active_bindings(self) -> None:
        s = Settings()
        s.keymap = {"shuffle": "Ctrl+B"}
        dlg = self._editor(s)
        self.assertEqual(dlg._current_labels["shuffle"].text(), "ctrl+b")
        self.assertEqual(
            dlg._edits["shuffle"].keySequence().toString(
                QKeySequence.PortableText),
            "Ctrl+B")
        self.assertEqual(dlg._current_labels["repeat"].text(), "ctrl+r")

    def test_cancel_leaves_zero_trace(self) -> None:
        stub = _StubWindow()
        self.addCleanup(stub.deleteLater)
        s = Settings()
        dlg = self._editor(s, parent=stub)
        dlg._edits["shuffle"].setKeySequence(QKeySequence("Ctrl+B"))
        dlg._edits["view_queue"].setKeySequence(QKeySequence())
        dlg.cancel_btn.click()
        self.assertEqual(dlg.result(), QDialog.Rejected)
        self.assertEqual(s.keymap, {}, "cancel mutated the settings object")
        self.assertFalse(config.SETTINGS_FILE.exists(),
                         "cancel wrote settings to disk")
        self.assertEqual(stub.rebind_calls, 0,
                         "cancel rebound the live shortcuts")

    def test_accept_persists_only_the_keymap_field(self) -> None:
        # Disk holds volume=33; the editor's live object says volume=80.
        # Accept must write the keymap WITHOUT exporting the object's
        # other fields (save_fields merge semantics).
        on_disk = Settings()
        on_disk.volume = 33
        settings_module.save(on_disk)
        s = Settings()
        s.volume = 80
        stub = _StubWindow()
        self.addCleanup(stub.deleteLater)
        dlg = self._editor(s, parent=stub)
        dlg._edits["shuffle"].setKeySequence(QKeySequence("Ctrl+B"))
        dlg.save_btn.click()
        self.assertEqual(dlg.result(), QDialog.Accepted)
        self.assertEqual(s.keymap, {"shuffle": "Ctrl+B"})
        back = settings_module.load()
        self.assertEqual(back.keymap, {"shuffle": "Ctrl+B"})
        self.assertEqual(back.volume, 33,
                         "accept leaked a non-keymap field to disk")
        self.assertEqual(stub.rebind_calls, 1,
                         "accept must rebind the live shortcuts once")

    def test_only_deviations_are_stored(self) -> None:
        s = Settings()
        dlg = self._editor(s)
        # Touch a binding, then type the default back in: no override.
        dlg._edits["shuffle"].setKeySequence(QKeySequence("Ctrl+B"))
        dlg._edits["shuffle"].setKeySequence(QKeySequence("Ctrl+S"))
        dlg.save_btn.click()
        self.assertEqual(s.keymap, {})
        self.assertEqual(settings_module.load().keymap, {})

    def test_reset_row_returns_to_default(self) -> None:
        s = Settings()
        s.keymap = {"shuffle": "Ctrl+B"}
        dlg = self._editor(s)
        dlg._reset_btns["shuffle"].click()
        dlg.save_btn.click()
        self.assertEqual(s.keymap, {},
                         "a reset-to-default row must drop out of the map")

    def test_unbind_stores_empty_string(self) -> None:
        s = Settings()
        dlg = self._editor(s)
        dlg._edits["view_queue"].setKeySequence(QKeySequence())
        dlg.save_btn.click()
        self.assertEqual(s.keymap, {"view_queue": ""})

    def test_reset_all(self) -> None:
        s = Settings()
        s.keymap = {"shuffle": "Ctrl+B", "view_queue": "", "like": "Ctrl+J"}
        dlg = self._editor(s)
        dlg.reset_all_btn.click()
        dlg.save_btn.click()
        self.assertEqual(s.keymap, {})

    def test_conflict_highlight_is_live(self) -> None:
        dlg = self._editor(Settings())
        self.assertEqual(dlg.conflicted_ids(), set())
        dlg._edits["like"].setKeySequence(QKeySequence("Ctrl+S"))
        bad = dlg.conflicted_ids()
        self.assertIn("like", bad)
        self.assertIn("shuffle", bad)
        self.assertTrue(dlg._row_labels["like"].styleSheet(),
                        "conflicting row not highlighted")
        self.assertTrue(dlg._row_labels["shuffle"].styleSheet())
        # Resolving the clash clears the highlight.
        dlg._edits["like"].setKeySequence(QKeySequence("Ctrl+J"))
        self.assertEqual(dlg.conflicted_ids(), set())
        self.assertFalse(dlg._row_labels["like"].styleSheet())
        self.assertFalse(dlg._row_labels["shuffle"].styleSheet())

    def test_unbound_rows_never_conflict(self) -> None:
        dlg = self._editor(Settings())
        dlg._edits["shuffle"].setKeySequence(QKeySequence())
        dlg._edits["like"].setKeySequence(QKeySequence())
        self.assertEqual(dlg.conflicted_ids(), set(),
                         "two unbound rows counted as a key clash")

    def test_conflicted_save_is_refused(self) -> None:
        # Persisting a conflict silently deadens BOTH shortcuts (Qt
        # emits activatedAmbiguously, which nothing connects) — so the
        # editor must refuse, not highlight-and-save-anyway.
        from PySide6.QtWidgets import QDialog as _QDialog
        stub = _StubWindow()
        self.addCleanup(stub.deleteLater)
        s = Settings()
        dlg = self._editor(s, parent=stub)
        dlg._edits["like"].setKeySequence(QKeySequence("Ctrl+S"))  # = shuffle
        dlg.save_btn.click()
        self.assertNotEqual(dlg.result(), _QDialog.Accepted,
                            "a conflicted map was allowed to save")
        self.assertEqual(s.keymap, {},
                         "the refused save mutated the settings object")
        self.assertFalse(config.SETTINGS_FILE.exists(),
                         "the refused save reached the disk")
        self.assertEqual(stub.rebind_calls, 0,
                         "the refused save rebound the live shortcuts")
        # The refusal explains itself inline on the blurb row.
        self.assertIn("share a key", dlg.blurb.text())
        self.assertTrue(dlg.blurb.styleSheet(),
                        "the refusal must be visually distinct")

    def test_resolving_the_conflict_unblocks_the_save(self) -> None:
        from tide.ui.keymap_editor import _BLURB_DEFAULT
        from PySide6.QtWidgets import QDialog as _QDialog
        stub = _StubWindow()
        self.addCleanup(stub.deleteLater)
        s = Settings()
        dlg = self._editor(s, parent=stub)
        dlg._edits["like"].setKeySequence(QKeySequence("Ctrl+S"))
        dlg.save_btn.click()                       # refused
        dlg._edits["like"].setKeySequence(QKeySequence("Ctrl+J"))
        self.assertEqual(dlg.blurb.text(), _BLURB_DEFAULT,
                         "fixing the clash must clear the refusal text")
        self.assertFalse(dlg.blurb.styleSheet())
        dlg.save_btn.click()
        self.assertEqual(dlg.result(), _QDialog.Accepted)
        self.assertEqual(s.keymap, {"like": "Ctrl+J"})
        self.assertEqual(stub.rebind_calls, 1)

    def test_open_is_deferred_out_of_the_emission(self) -> None:
        from tide.ui.keymap_editor import KeymapEditor, open_keymap_editor
        s = Settings()
        with mock.patch.object(KeymapEditor, "exec",
                               return_value=QDialog.Rejected) as ex:
            open_keymap_editor(None, s)
            self.assertEqual(ex.call_count, 0,
                             "the editor opened inside the calling turn")
            QTest.qWait(30)
            self.assertEqual(ex.call_count, 1)


class CompanionKeymapTests(_WindowCase):
    """The mini and fullscreen windows are separate top-levels, so their
    QShortcuts never see the main window's rebinds — they must build
    from the SAME effective keymap and be re-keyed by the main window's
    rebind_shortcuts companion walk. v1 hardcoded their keys; a rebind
    left the old defaults live in mini/fullscreen mode."""

    def _key(self, sc) -> str:
        return sc.key().toString(QKeySequence.PortableText)

    def test_mini_defaults_match_v1_wiring(self) -> None:
        from tide.ui.mini import MiniPlayer
        w = self._make_window()
        w._settings = Settings()
        mini = MiniPlayer(w)
        self.addCleanup(mini.deleteLater)
        self.assertEqual(
            {aid: self._key(sc)
             for aid, sc in mini._keymap_shortcuts.items()},
            {"mini_mode": "Ctrl+M", "play_pause": "Space",
             "next_track": "Ctrl+Right", "prev_track": "Ctrl+Left",
             "like": "Ctrl+H"},
            "the mini's shortcut set must keep v1's exact keys on "
            "default settings")

    def test_fullscreen_defaults_match_v1_wiring(self) -> None:
        from tide.ui.fullscreen import FullscreenPlayer
        w = self._make_window()
        w._settings = Settings()
        fs = FullscreenPlayer(w)
        self.addCleanup(fs.deleteLater)
        self.assertEqual(
            {aid: self._key(sc)
             for aid, sc in fs._keymap_shortcuts.items()},
            {"fullscreen": "F11", "mini_mode": "Ctrl+M",
             "play_pause": "Space", "next_track": "Ctrl+Right",
             "prev_track": "Ctrl+Left", "like": "Ctrl+H",
             "volume_up": "Ctrl+Up", "volume_down": "Ctrl+Down"})

    def test_companion_built_after_a_rebind_wears_it(self) -> None:
        from tide.ui.mini import MiniPlayer
        w = self._make_window()
        s = Settings()
        s.keymap = {"next_track": "Ctrl+G", "play_pause": ""}
        w._settings = s
        mini = MiniPlayer(w)     # lazy construction reads the live map
        self.addCleanup(mini.deleteLater)
        self.assertEqual(self._key(mini._keymap_shortcuts["next_track"]),
                         "Ctrl+G")
        self.assertTrue(
            mini._keymap_shortcuts["play_pause"].key().isEmpty(),
            "an unbound action must be inert in the mini too")

    def test_rebind_walks_the_open_companions(self) -> None:
        from tide.ui.fullscreen import FullscreenPlayer
        from tide.ui.mini import MiniPlayer
        w = self._make_window()
        s = Settings()
        w._settings = s
        # Lazily-constructed companions, registered the way the window
        # holds them — _companions() is the walk rebind uses.
        w._mini = MiniPlayer(w)
        w._fs = FullscreenPlayer(w)
        s.keymap = {"like": "Ctrl+J", "mini_mode": "Ctrl+Shift+M"}
        w.rebind_shortcuts()               # the editor's accept path
        self.assertEqual(self._key(w._mini._keymap_shortcuts["like"]),
                         "Ctrl+J")
        self.assertEqual(self._key(w._mini._keymap_shortcuts["mini_mode"]),
                         "Ctrl+Shift+M")
        self.assertEqual(self._key(w._fs._keymap_shortcuts["like"]),
                         "Ctrl+J")
        self.assertEqual(self._key(w._fs._keymap_shortcuts["mini_mode"]),
                         "Ctrl+Shift+M")
        # The replaced defaults are really gone from the companions.
        self.assertNotIn(
            "Ctrl+H",
            {self._key(sc) for sc in w._mini._keymap_shortcuts.values()})

    def test_rebound_key_fires_in_the_mini(self) -> None:
        # Behavioral end-to-end: the handler table reads the window's
        # attributes at construction, so a pre-construction stub sees
        # the fire.
        from tide.ui.mini import MiniPlayer
        w = self._make_window()
        s = Settings()
        s.keymap = {"next_track": "Ctrl+G"}
        w._settings = s
        fired: list[int] = []
        w._on_next_clicked = lambda: fired.append(1)
        mini = MiniPlayer(w)
        self.addCleanup(mini.deleteLater)
        mini.show()
        QTest.qWait(30)
        QTest.keyClick(mini, Qt.Key_G, Qt.ControlModifier)
        QTest.qWait(20)
        self.assertEqual(fired, [1],
                         "the rebound key must fire in the mini")
        # …and the shipped default it replaced must NOT.
        QTest.keyClick(mini, Qt.Key_Right, Qt.ControlModifier)
        QTest.qWait(20)
        self.assertEqual(fired, [1],
                         "the replaced default still fires in the mini")
        mini.hide()

    def test_escape_stays_a_fixed_exit(self) -> None:
        # Escape is deliberately not in the rebindable set — it must
        # keep exiting the companions whatever the keymap says.
        from tide.ui.mini import MiniPlayer
        w = self._make_window()
        w._settings = Settings()
        mini = MiniPlayer(w)
        self.addCleanup(mini.deleteLater)
        self.assertNotIn("Escape", {
            self._key(sc) for sc in mini._keymap_shortcuts.values()})


class LibraryHintTests(_WindowCase):
    def test_source_hint_derives_from_the_keymap(self) -> None:
        # library.py's no-library placeholder advertises the view_source
        # key — hardcoding "(ctrl+7)" would orphan it after a rebind.
        w = self._make_window()
        s = Settings()
        s.keymap = {"view_source": "Ctrl+0"}
        w._settings = s

        class _NoLibrarySource:
            name = "siren radio"

            def supports(self, cap: str) -> bool:
                return False

        lib = w.library_view
        old_api = lib.api
        lib.api = _NoLibrarySource()
        try:
            lib.reload_playlists()
            text = lib.playlists_list.item(0).text()
            self.assertIn("ctrl+0", text.lower(),
                          "the hint must advertise the LIVE binding")
            self.assertNotIn("ctrl+7", text.lower())
        finally:
            lib.api = old_api

    def test_source_hint_drops_the_key_when_unbound(self) -> None:
        w = self._make_window()
        s = Settings()
        s.keymap = {"view_source": ""}
        w._settings = s

        class _NoLibrarySource:
            name = "siren radio"

            def supports(self, cap: str) -> bool:
                return False

        lib = w.library_view
        old_api = lib.api
        lib.api = _NoLibrarySource()
        try:
            lib.reload_playlists()
            text = lib.playlists_list.item(0).text()
            self.assertNotIn("ctrl", text.lower(),
                             "an unbound action must not be advertised")
            self.assertIn("[source]", text.lower())
        finally:
            lib.api = old_api


if __name__ == "__main__":
    unittest.main()
