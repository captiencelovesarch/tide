"""v2.0 phase 2 — INTEGRATION: the four power tools wired into the app.

Pinned here (the phase-2 integration contract):
- boot: app._boot_glyph_overrides pushes saved glyph overrides into the
  registry BEFORE the window is built (the first frame already wears
  them), and app._attach_settings rebinds the shortcuts right after the
  settings attach — construction wires defaults, so a saved custom
  keymap only lands through that rebind;
- personality flips carry glyph overrides: glyph_overrides is a stash
  field and apply_preset_visuals re-applies the incoming set + repaints,
  so brutalist's custom glyphs never leak into modern (or back);
- MainWindow.refresh_glyphs re-pushes state to every transport label,
  quietly — no restyle, no saves, no status spam (the glyph editor
  calls it on every live keystroke);
- the strip builder's overrides_chosen payload routes through
  apply_strip_overrides → update_overrides → apply_layout (the
  keep-list path) + a field-scoped save, and survives a flip roundtrip;
- keymap rebinds apply live, and the tooltips/dialog blurbs that
  advertise keys derive from the live keymap;
- the settings dialog carries the four launchers, opens every one
  DEFERRED, and reconciles a theme-editor save through its normal
  pending-diff path.

Window-building cases use the test_preset_flip harness: app-wide QSS
pushes suppressed, per-test settings.toml, deleteLater + drain teardown.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import app as app_module
from tide import config, glyphs, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.settings import Settings


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _brutalist_settings() -> Settings:
    """An active brutalist personality with a custom glyph — the thing
    that must not leak into modern."""
    s = Settings()
    s.first_launch_complete = True
    s.preset = "brutalist"
    s.theme = "brutalist-mono"
    s.layout = "classic"
    s.glyph_overrides = {"play": "P"}
    presets.stash(s)
    return s


class _IntegrationCase(unittest.TestCase):
    """Real-MainWindow harness (the test_preset_flip pattern): app-wide
    QSS pushes suppressed, hermetic settings file, deleteLater + drain
    teardown — plus glyph-registry hygiene, since the override layer is
    module-global and later files must inherit a clean one."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-p2int-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        glyphs.set_overrides({})
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
        glyphs.set_overrides({})
        config.SETTINGS_FILE = self._orig_settings_file
        mgr = theming.manager()
        mgr.set_user_override("radius", None)
        theming.set_case_override("")
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0)
        layout_module.manager().apply("classic", {})
        QTest.qWait(30)   # drain the queued restyles into THIS test
        for _ in range(10):
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    def _make_window(self, settings: Settings):
        theming.manager().apply(settings.theme)
        layout_module.manager().apply(settings.layout,
                                      dict(settings.layout_overrides))
        from tide.playback import MpvBackend, PlaybackRouter
        from tide.sources.local import LocalSource
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.w._settings = settings
        return self.w


# ---------------------------------------------------------------------------
# boot wiring (app.py)
# ---------------------------------------------------------------------------

class BootTests(_IntegrationCase):
    def test_boot_glyphs_land_before_the_window(self) -> None:
        # The contract: overrides go into the registry BEFORE the window
        # ctor, so every transport label is BUILT already wearing them —
        # no post-construction repaint needed for the first frame.
        s = Settings()
        s.first_launch_complete = True
        s.glyph_overrides = {"play": "P", "like_off": "no"}
        app_module._boot_glyph_overrides(s)
        self.assertEqual(glyphs.glyph("play"), "P")
        w = self._make_window(s)
        self.assertEqual(w.play_btn._glyph, "P",
                         "the first build must already wear the override")
        self.assertEqual(w.like_btn._glyph, "no")

    def test_boot_glyphs_tolerate_stale_keys(self) -> None:
        s = Settings()
        s.glyph_overrides = {"play": "P", "warp_drive": "!"}
        app_module._boot_glyph_overrides(s)   # must not raise
        self.assertEqual(glyphs.glyph("play"), "P")

    def test_attach_settings_rebinds_the_saved_keymap(self) -> None:
        # _wire_shortcuts runs in the ctor, before settings exist — the
        # boot attach is what makes a saved custom keymap live.
        s = Settings()
        s.first_launch_complete = True
        s.keymap = {"sleep_timer": "Ctrl+Alt+Z"}
        w = self._make_window(s)
        w._settings = None                    # ctor state: no settings
        w.rebind_shortcuts()
        self.assertEqual(
            w._shortcuts["sleep_timer"].key().toString(
                QKeySequence.PortableText),
            "Ctrl+I", "precondition: defaults before the attach")
        app_module._attach_settings(w, s)
        self.assertIs(w._settings, s)
        self.assertEqual(
            w._shortcuts["sleep_timer"].key().toString(
                QKeySequence.PortableText),
            "Ctrl+Alt+Z",
            "_attach_settings must rebind the saved keymap")


# ---------------------------------------------------------------------------
# glyphs × personality flips
# ---------------------------------------------------------------------------

class GlyphFlipTests(_IntegrationCase):
    def test_flip_does_not_leak_brutalist_glyphs_into_modern(self) -> None:
        s = _brutalist_settings()
        glyphs.set_overrides(dict(s.glyph_overrides))
        w = self._make_window(s)
        QTest.qWait(30)
        self.assertEqual(w.play_btn._glyph, "P")
        w.switch_preset("modern")
        QTest.qWait(30)
        # Modern has never been visited: its glyph set is the Settings
        # default ({}), and the registry + window must wear it.
        self.assertEqual(s.glyph_overrides, {})
        self.assertEqual(glyphs.glyph("play"), "▶")
        self.assertEqual(w.play_btn._glyph, "▶",
                         "the flip must repaint the transport labels")

    def test_flip_roundtrip_restores_each_side_s_glyphs(self) -> None:
        s = _brutalist_settings()
        glyphs.set_overrides(dict(s.glyph_overrides))
        w = self._make_window(s)
        QTest.qWait(30)
        w.switch_preset("modern")
        QTest.qWait(30)
        # Modern picks its own swap (what a glyph-editor accept does).
        s.glyph_overrides = {"play": "M"}
        glyphs.set_overrides({"play": "M"})
        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(glyphs.glyph("play"), "P")
        self.assertEqual(w.play_btn._glyph, "P")
        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertEqual(glyphs.glyph("play"), "M")
        self.assertEqual(w.play_btn._glyph, "M")
        # And each side's stash remembers its own set.
        self.assertEqual(
            s.preset_state["brutalist"]["glyph_overrides"], {"play": "P"})


# ---------------------------------------------------------------------------
# refresh_glyphs
# ---------------------------------------------------------------------------

class RefreshGlyphTests(_IntegrationCase):
    def test_refresh_repaints_every_transport_label(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        glyphs.set_overrides({
            "play": "P", "prev": "<", "next": ">", "shuffle": "X",
            "repeat": "R", "like_off": "n", "sleep": "Zz",
            "fullscreen": "F",
        })
        w.refresh_glyphs()
        self.assertEqual(w.play_btn._glyph, "P")     # idle → play face
        self.assertEqual(w.prev_btn._glyph, "<")
        self.assertEqual(w.next_btn._glyph, ">")
        self.assertEqual(w.shuffle_btn._glyph, "X")
        self.assertEqual(w.repeat_btn._glyph, "R")   # repeat off → plain
        self.assertEqual(w.like_btn._glyph, "n")
        self.assertEqual(w.sleep_btn._label, "Zz")
        self.assertEqual(w.fullscreen_btn._glyph, "F")

    def test_refresh_is_quiet(self) -> None:
        # The glyph editor calls this per keystroke — it must never cost
        # a restyle, a save, or a status-bar message.
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        status_before = w.statusBar().currentMessage()
        with mock.patch.object(settings_module, "save_fields") as saves, \
             mock.patch.object(settings_module, "save") as full_saves, \
             mock.patch.object(self.app, "setStyleSheet") as restyles:
            glyphs.set_overrides({"play": "P"})
            w.refresh_glyphs()
            w.refresh_glyphs()          # idempotent by contract
            QTest.qWait(30)
            self.assertEqual(saves.call_count, 0)
            self.assertEqual(full_saves.call_count, 0)
            self.assertEqual(restyles.call_count, 0,
                             "a glyph repaint must never restyle the app")
        self.assertEqual(w.statusBar().currentMessage(), status_before)


# ---------------------------------------------------------------------------
# strip builder → window
# ---------------------------------------------------------------------------

class StripBuilderWiringTests(_IntegrationCase):
    def test_apply_strip_overrides_round_trips(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        w.apply_strip_overrides({"progress": "dotted"})
        self.assertEqual(w._slot_progress, "dotted",
                         "the pick must land through apply_layout")
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "dotted")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"})
        back = settings_module.load()
        self.assertEqual(back.layout_overrides, {"progress": "dotted"},
                         "the save must be field-scoped and real")

    def test_empty_payload_clears_back_to_the_layout(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        s.layout_overrides = {"progress": "dotted"}
        w = self._make_window(s)
        QTest.qWait(30)
        self.assertEqual(w._slot_progress, "dotted")
        w.apply_strip_overrides({})
        self.assertEqual(w._slot_progress, "blocks",
                         "{} means 'clear back to the layout defaults'")
        self.assertEqual(s.layout_overrides, {})
        self.assertEqual(settings_module.load().layout_overrides, {})

    def test_overrides_survive_a_flip_roundtrip(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        w.apply_strip_overrides({"progress": "dotted"})
        w.switch_preset("modern")
        QTest.qWait(30)
        self.assertNotEqual(w._slot_progress, "dotted",
                            "modern must wear its own slots")
        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(w._slot_progress, "dotted",
                         "the built bar must come back with brutalist")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"})

    def test_builder_emission_routes_through_the_dialog(self) -> None:
        # End-to-end: a real StripBuilder accept → the settings dialog's
        # on_chosen handler → the window's sanctioned apply/persist path.
        from tide.ui.settings import SettingsDialog
        from tide.ui.strip_builder import StripBuilder
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        dlg = SettingsDialog(s, parent=w)
        builder = StripBuilder(overrides=dict(s.layout_overrides or {}),
                               parent=dlg)
        builder.overrides_chosen.connect(dlg._on_strip_overrides)
        combo = builder.picker_for("progress")
        idx = combo.findData("dotted")
        self.assertGreaterEqual(idx, 0)
        combo.setCurrentIndex(idx)
        builder._on_accept()
        self.assertEqual(w._slot_progress, "dotted")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"})
        self.assertEqual(settings_module.load().layout_overrides,
                         {"progress": "dotted"})
        # The accepted bar is a commit: a later layout-preview cancel in
        # the dialog must revert onto it, not the opening snapshot.
        self.assertEqual(dlg._initial_overrides, {"progress": "dotted"})
        builder.deleteLater()


# ---------------------------------------------------------------------------
# keymap: live rebinds + derived chrome
# ---------------------------------------------------------------------------

class KeymapLiveTests(_IntegrationCase):
    def test_rebind_applies_live_and_tooltips_follow(self) -> None:
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        s.keymap = {"sleep_timer": "Ctrl+Alt+Z"}
        w.rebind_shortcuts()                    # the editor's accept path
        self.assertEqual(
            w._shortcuts["sleep_timer"].key().toString(
                QKeySequence.PortableText),
            "Ctrl+Alt+Z")
        self.assertIn("ctrl+alt+z", w.sleep_btn.toolTip(),
                      "the tooltip must derive from the live keymap")
        self.assertNotIn("ctrl+i", w.sleep_btn.toolTip())

    def test_dialog_blurbs_derive_from_the_keymap(self) -> None:
        from tide.ui.settings import SettingsDialog
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        s.keymap = {"view_audio_fx": "Ctrl+0", "mini_mode": "",
                    "refresh_session": "F9"}
        app_module._attach_settings(w, s)
        dlg = SettingsDialog(s, parent=w)   # dies with w in tearDown
        self.assertIn("ctrl+0", dlg.fx_blurb.text())
        self.assertNotIn("ctrl+8", dlg.fx_blurb.text())
        self.assertIn("f9", dlg.session_blurb.text())
        self.assertNotIn("ctrl+shift+r", dlg.session_blurb.text())
        # Unbound: the copy drops the key mention instead of lying.
        self.assertNotIn("ctrl+m", dlg.mini_blurb.text())


# ---------------------------------------------------------------------------
# the dialog's tool launchers
# ---------------------------------------------------------------------------

class _DialogCase(unittest.TestCase):
    """Parentless-dialog cases: every settings write stubbed (dialog
    tests must never be able to clobber a config file), theme state
    restored after the theme-saved reconcile test."""

    def setUp(self) -> None:
        _app()
        self.saved_field_calls: list[tuple[str, ...]] = []
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(
            settings_module, "save_fields",
            lambda s, *names: self.saved_field_calls.append(tuple(names)),
        ).start()
        self.addCleanup(mock.patch.stopall)
        theming.manager().refresh()
        theming.manager().apply("brutalist-mono")
        QTest.qWait(10)

    def tearDown(self) -> None:
        theming.manager().apply("brutalist-mono")
        theming.manager().set_user_font("")
        theming.manager().set_user_font_size(0)
        theming.set_case_override("")
        QTest.qWait(10)

    def _dialog(self, s: Settings | None = None):
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(s or Settings())
        self.addCleanup(dlg.deleteLater)
        return dlg


class DialogToolWiringTests(_DialogCase):
    def test_the_four_launchers_exist(self) -> None:
        dlg = self._dialog()
        for attr in ("theme_editor_btn", "strip_builder_btn", "glyphs_btn",
                     "keymap_btn"):
            self.assertTrue(hasattr(dlg, attr), attr)

    def test_parentless_blurbs_fall_back_to_the_defaults(self) -> None:
        dlg = self._dialog()
        self.assertIn("ctrl+8", dlg.fx_blurb.text())
        self.assertIn("ctrl+m", dlg.mini_blurb.text())
        self.assertIn("ctrl+shift+r", dlg.session_blurb.text())

    def test_theme_editor_open_is_deferred(self) -> None:
        # Ground rule 3: the editor opens on the NEXT event-loop turn,
        # never inside the click emission.
        dlg = self._dialog()
        with mock.patch.object(dlg, "_do_open_theme_editor") as spy:
            dlg.theme_editor_btn.click()
            self.assertEqual(spy.call_count, 0,
                             "theme editor opened synchronously inside "
                             "the click emission")
            QTest.qWait(30)
            self.assertEqual(spy.call_count, 1)

    def test_strip_builder_button_uses_the_deferring_opener(self) -> None:
        from tide.ui import strip_builder as sb_module
        s = Settings()
        s.layout_overrides = {"progress": "dotted"}
        dlg = self._dialog(s)
        with mock.patch.object(sb_module, "open_strip_builder") as spy:
            dlg.strip_builder_btn.click()
        spy.assert_called_once_with(
            parent=dlg,
            overrides={"progress": "dotted"},
            on_chosen=dlg._on_strip_overrides,
        )

    def test_glyphs_button_uses_the_deferring_opener(self) -> None:
        from tide.ui import glyph_editor as ge_module
        dlg = self._dialog()
        with mock.patch.object(ge_module, "open_glyph_editor") as spy:
            dlg.glyphs_btn.click()
        spy.assert_called_once_with(dlg._settings, dlg)

    def test_keymap_button_uses_the_deferring_opener(self) -> None:
        from tide.ui import keymap_editor as ke_module
        dlg = self._dialog()
        with mock.patch.object(ke_module, "open_keymap_editor") as spy:
            dlg.keymap_btn.click()
        spy.assert_called_once_with(dlg, dlg._settings)

    def test_theme_saved_selects_the_slug_and_stages_the_pick(self) -> None:
        # After a [save as my theme], the editor has already applied the
        # new slug; persisting settings.theme rides the dialog's normal
        # pending-diff path — the reconcile handler repopulates the combo
        # (registry already refreshed) and selects the slug.
        s = Settings()          # theme = brutalist-mono
        dlg = self._dialog(s)
        dlg._on_theme_saved("gruvbox")
        self.assertEqual(dlg.theme_picker.currentData(), "gruvbox")
        self.assertEqual(dlg._pending.get("theme"), "gruvbox")
        self.assertEqual(s.theme, "brutalist-mono",
                         "staging must not touch the settings object")
        dlg._on_save()
        self.assertEqual(s.theme, "gruvbox")
        self.assertIn("theme", dlg.changed_keys())
        self.assertTrue(any("theme" in call
                            for call in self.saved_field_calls))

    def test_theme_saved_selection_survives_cancel_semantics(self) -> None:
        # Previews never commit: cancelling the settings dialog after a
        # theme-editor save reverts the applied theme; the saved theme
        # dir simply stays available in the pickers.
        s = Settings()
        dlg = self._dialog(s)
        dlg._on_theme_saved("gruvbox")
        self.assertEqual(theming.manager().current().slug, "gruvbox")
        dlg._on_cancel()
        self.assertEqual(theming.manager().current().slug, "brutalist-mono")
        self.assertEqual(s.theme, "brutalist-mono")
        self.assertEqual(self.saved_field_calls, [])


if __name__ == "__main__":
    unittest.main()
