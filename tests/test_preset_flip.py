"""v2.0 mid-session personality flip — MainWindow.switch_preset and the
reworked theme-pick handler.

Pinned: a flip costs exactly one queued app restyle (apply_bundle
batches theme+font+size+case; the sticky radius coalesces into the same
flush) and one window slot-rebuild pass; the theme-change handler is
guarded during a programmatic flip so the restored layout_overrides are
never wiped; a cross-personality theme pick routes through switch_preset
and lands ON the picked theme, seeding the target's stash; a
same-personality pick leaves slots and overrides alone.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import config, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _brutalist_settings() -> Settings:
    """A brutalist personality with a personal slot tweak — the thing the
    old aesthetic-flip code used to destroy."""
    s = Settings()
    s.first_launch_complete = True
    s.preset = "brutalist"
    s.theme = "gruvbox"
    s.layout = "classic"
    s.layout_overrides = {"progress": "dotted"}
    s.motion = "off"
    s.corner_style = "sharp"
    s.nav_icon_set = "off"
    s.adaptive_accent = False
    s.adaptive_background = False
    presets.stash(s)
    return s


class _FlipCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        # Hermetic settings file — flips persist via save_fields.
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-flip-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
        # Suppress the real app-wide QSS pushes (test_restyle_coalesce's
        # spy pattern): earlier tests' closed-but-alive windows are still
        # polishable, so every real push repolishes all of them. Restyle
        # COUNTS are asserted against a nested spy; state stays real.
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self.w = None

    def tearDown(self) -> None:
        if self.w is not None:
            # detach before later tests apply themes — a closed-but-alive
            # window still listens and would flip a stale settings object
            try:
                theming.manager().theme_changed.disconnect(
                    self.w._on_theme_changed)
            except (RuntimeError, TypeError):
                pass
            self.w.close()
            # close() alone leaks the C++ widget tree (~400 widgets a
            # window) until GC, and every restyle repolishes every leaked
            # window — quadratic. deleteLater + the qWait destroys it.
            self.w.deleteLater()
            self.w = None
        QTest.qWait(30)
        config.SETTINGS_FILE = self._orig_settings_file
        mgr = theming.manager()
        mgr.set_user_override("radius", None)
        theming.set_case_override("")
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0)
        layout_module.manager().apply("classic", {})
        QTest.qWait(30)   # drain the queued restyles into THIS test
        # a late palette worker can queue one more flush as qWait returns;
        # don't export it — downstream counts would see a phantom push
        for _ in range(10):
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    def _make_window(self, settings: Settings):
        theming.manager().apply(settings.theme)
        layout_module.manager().apply(settings.layout,
                                      dict(settings.layout_overrides))
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        self.w = MainWindow(LocalSource(), router)
        self.w._settings = settings
        return self.w


class SwitchPresetTests(_FlipCase):
    def test_flip_lands_in_one_queued_restyle(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)   # drain construction-time restyles
        with mock.patch.object(self.app, "setStyleSheet") as spy:
            w.switch_preset("modern")
            self.assertEqual(spy.call_count, 0,
                             "flip pushed QSS inside the calling turn")
            QTest.qWait(30)
            self.assertEqual(spy.call_count, 1,
                             "flip must cost exactly one app restyle")
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "adaptive")
        self.assertEqual(theming.manager().current().slug, "adaptive")

    def test_flip_is_one_slot_rebuild_pass(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        passes: list[str] = []
        orig = w.apply_layout

        def _counting(layout):
            passes.append(layout.slug)
            orig(layout)

        w.apply_layout = _counting
        w.switch_preset("modern")
        self.assertEqual(passes, ["classic"],
                         "flip must rebuild slots in exactly one pass")

    def test_first_visit_to_modern_wears_modern_slots(self) -> None:
        # no modern stash: the flip seeds the builtin theme's [slots] as
        # overrides so modern doesn't wear brutalist blocks forever
        s = _brutalist_settings()
        w = self._make_window(s)
        w.switch_preset("modern")
        self.assertEqual(s.layout_overrides.get("progress"), "bar")
        self.assertEqual(s.layout_overrides.get("volume"), "spring")
        self.assertEqual(w._slot_progress, "bar")
        self.assertEqual(w._slot_controls, "icons")

    def test_flip_roundtrip_keeps_layout_overrides(self) -> None:
        # the guard: apply_bundle's theme_changed lands back in
        # _on_theme_changed mid-flip; without the flag the slot-prefs
        # handler would reset the restored overrides to the new theme's
        s = _brutalist_settings()
        w = self._make_window(s)
        w.switch_preset("modern")
        QTest.qWait(30)
        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "gruvbox")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"},
                         "the flip wiped the user's slot overrides")
        self.assertEqual(w._slot_progress, "dotted")
        self.assertEqual(theming.manager().current().slug, "gruvbox")

    def test_flip_persists_field_scoped(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w.switch_preset("modern")
        back = settings_module.load()
        self.assertEqual(back.preset, "modern")
        self.assertEqual(back.preset_state["brutalist"]["theme"], "gruvbox")
        self.assertEqual(
            back.preset_state["brutalist"]["layout_overrides"],
            {"progress": "dotted"},
        )

    def test_reentry_is_guarded(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w._switching_preset = True
        try:
            w.switch_preset("modern")
        finally:
            w._switching_preset = False
        self.assertEqual(s.preset, "brutalist",
                         "a reentrant switch_preset must be a no-op")

    def test_no_settings_is_a_noop(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        w._settings = None
        w.switch_preset("modern")   # must not raise
        self.assertEqual(s.preset, "brutalist")


class ThemePickTests(_FlipCase):
    def test_same_personality_pick_keeps_slots(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("terminal-green")   # brutalist aesthetic
        QTest.qWait(30)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"},
                         "a same-personality theme pick wiped slot overrides")
        self.assertEqual(w._slot_progress, "dotted")
        self.assertEqual(theming.manager().current().slug, "terminal-green")

    def test_cross_personality_pick_flips_onto_the_picked_theme(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")   # modern aesthetic
        QTest.qWait(30)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "nord",
                         "flip must land ON the picked theme, not the "
                         "personality's builtin default")
        self.assertEqual(theming.manager().current().slug, "nord")
        self.assertEqual(s.layout_overrides.get("progress"), "bar")
        self.assertEqual(w._slot_progress, "bar")
        self.assertEqual(w._slot_volume, "knob")
        self.assertEqual(s.preset_state["brutalist"]["theme"], "gruvbox")
        self.assertEqual(
            s.preset_state["brutalist"]["layout_overrides"],
            {"progress": "dotted"},
        )

    def test_cross_pick_with_existing_stash_updates_only_theme(self) -> None:
        s = _brutalist_settings()
        s.preset_state["modern"] = {
            "theme": "abyss",
            "layout_overrides": {"volume": "wedge"},
        }
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")
        QTest.qWait(30)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "nord")
        self.assertEqual(s.layout_overrides, {"volume": "wedge"},
                         "an existing stash's slot picks must survive a "
                         "cross-personality theme pick")
        self.assertEqual(w._slot_volume, "wedge")

    def test_pick_without_active_preset_does_nothing(self) -> None:
        # pre-adoption config (preset "") — the handler must not invent a
        # flip; bootstrap adoption owns that transition
        s = _brutalist_settings()
        s.preset = ""
        s.preset_state.clear()
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")
        QTest.qWait(30)
        self.assertEqual(s.preset, "")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"})

    def test_cross_pick_defers_the_flip_out_of_the_emission(self) -> None:
        # switch_preset runs a theme apply; nested inside theme_changed
        # delivery it would resume the outer emission with the stale
        # pre-flip theme. The flip lands on its own event-loop turn.
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")
        self.assertEqual(s.preset, "brutalist",
                         "flip ran nested inside theme_changed delivery")
        QTest.qWait(30)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "nord")

    def test_superseded_cross_pick_does_not_flip(self) -> None:
        # a cross-aesthetic pick overridden in the same turn: the
        # deferred flip is stale by the time it runs and must stand down
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")             # modern, deferred…
        theming.manager().apply("terminal-green")   # …brutalist again
        QTest.qWait(30)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(theming.manager().current().slug, "terminal-green")


class DialogInterplayTests(_FlipCase):
    """While the settings dialog is open a cross-aesthetic slug is a
    PREVIEW — never a flip, never a disk write; the accept path
    reconciles the final pick instead."""

    def test_preview_while_dialog_open_never_flips(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        w._settings_dialog_open = True
        try:
            theming.manager().apply("nord")     # browse into modern…
            QTest.qWait(30)
            theming.manager().apply("abyss")    # …and keep browsing
            QTest.qWait(30)
        finally:
            w._settings_dialog_open = False
        self.assertEqual(s.preset, "brutalist",
                         "a dialog preview committed a personality flip")
        self.assertNotIn("modern", s.preset_state,
                         "browsing polluted the other personality's stash")
        self.assertFalse(config.SETTINGS_FILE.exists(),
                         "a preview wrote settings to disk")

    def test_cancel_style_revert_leaves_zero_trace(self) -> None:
        # _on_cancel re-applies the initial theme while the dialog is
        # still up — that revert must not flip or persist either
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        before_state = dict(s.preset_state["brutalist"])
        w._settings_dialog_open = True
        try:
            theming.manager().apply("nord")
            QTest.qWait(30)
            theming.manager().apply("gruvbox")   # the cancel revert
            QTest.qWait(30)
        finally:
            w._settings_dialog_open = False
        QTest.qWait(30)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.preset_state["brutalist"], before_state)
        self.assertFalse(config.SETTINGS_FILE.exists())

    def test_flip_deferred_past_dialog_open_stands_down(self) -> None:
        # emission before the flag, timer after: the deferred closure
        # re-checks and must not flip under an open dialog
        s = _brutalist_settings()
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")
        w._settings_dialog_open = True      # dialog opens the same turn
        try:
            QTest.qWait(30)
        finally:
            w._settings_dialog_open = False
        self.assertEqual(s.preset, "brutalist")


class DialogAcceptReconcileTests(_FlipCase):
    """MainWindow._reconcile_preset_after_dialog — the accept-side half
    of the dialog integration. ``before`` is the pre-dialog settings
    object, ``new`` the dialog's accepted copy."""

    def _accepted_copy(self, s: Settings) -> Settings:
        import copy
        return copy.deepcopy(s)

    def test_same_personality_accept_refreshes_the_stash(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        new = self._accepted_copy(s)
        new.theme = "terminal-green"        # same aesthetic
        new.corner_style = "rounded"
        w._settings = new
        w._reconcile_preset_after_dialog(s, new)
        self.assertEqual(new.preset, "brutalist")
        self.assertEqual(
            new.preset_state["brutalist"]["theme"], "terminal-green")
        self.assertEqual(
            new.preset_state["brutalist"]["corner_style"], "rounded")
        back = settings_module.load()
        self.assertEqual(
            back.preset_state["brutalist"]["theme"], "terminal-green")

    def test_cross_aesthetic_accept_flips_onto_the_picked_theme(self) -> None:
        s = _brutalist_settings()
        w = self._make_window(s)
        new = self._accepted_copy(s)
        new.theme = "nord"                  # modern aesthetic accepted
        new.motion = "lite"                 # tweaked in the dialog too
        w._settings = new
        w._reconcile_preset_after_dialog(s, new)
        # same semantics as a live cross pick: land ON the picked theme
        # wearing the target's state, never the outgoing widget bundle
        self.assertEqual(new.preset, "modern")
        self.assertEqual(new.theme, "nord")
        self.assertEqual(new.motion, "full")          # modern builtin
        self.assertEqual(new.corner_style, "soft")
        self.assertEqual(new.layout_overrides.get("progress"), "bar")
        self.assertEqual(theming.manager().current().slug, "nord")
        # the dialog's widget edits stay with the outgoing personality —
        # they were made looking at its values; only the theme pick
        # rides to the target
        self.assertEqual(new.preset_state["brutalist"]["theme"], "gruvbox")
        self.assertEqual(new.preset_state["brutalist"]["motion"], "lite")
        self.assertEqual(
            new.preset_state["brutalist"]["layout_overrides"],
            {"progress": "dotted"})
        self.assertFalse(new.preset_chosen)
        back = settings_module.load()
        self.assertEqual(back.preset, "modern")
        self.assertEqual(back.theme, "nord")
        self.assertEqual(back.preset_state["brutalist"]["theme"], "gruvbox")

    def test_cross_accept_keeps_targets_remembered_tweaks(self) -> None:
        # a visited modern remembers its own look — an accepted pick
        # updates ONLY its theme, exactly like the live path
        s = _brutalist_settings()
        s.preset_state["modern"] = {
            "theme": "abyss",
            "layout_overrides": {"volume": "wedge"},
        }
        w = self._make_window(s)
        new = self._accepted_copy(s)
        new.theme = "nord"
        w._settings = new
        w._reconcile_preset_after_dialog(s, new)
        self.assertEqual(new.preset, "modern")
        self.assertEqual(new.theme, "nord")
        self.assertEqual(new.layout_overrides, {"volume": "wedge"},
                         "modern's remembered tweaks were clobbered by "
                         "the dialog's outgoing widget bundle")

    def test_flip_back_restores_the_dialog_edits(self) -> None:
        # accept a cross pick, then flip back: brutalist returns wearing
        # its theme plus that session's dialog edits
        s = _brutalist_settings()
        w = self._make_window(s)
        new = self._accepted_copy(s)
        new.theme = "nord"
        new.corner_style = "rounded"        # a dialog edit
        w._settings = new
        w._reconcile_preset_after_dialog(s, new)
        w.switch_preset("brutalist")
        QTest.qWait(30)
        self.assertEqual(new.preset, "brutalist")
        self.assertEqual(new.theme, "gruvbox")
        self.assertEqual(new.corner_style, "rounded")
        self.assertEqual(new.layout_overrides, {"progress": "dotted"})
        self.assertEqual(new.preset_state["modern"]["theme"], "nord")

    def test_pre_adoption_accept_is_a_noop(self) -> None:
        s = _brutalist_settings()
        s.preset = ""
        s.preset_state.clear()
        w = self._make_window(s)
        new = self._accepted_copy(s)
        new.theme = "nord"
        w._settings = new
        w._reconcile_preset_after_dialog(s, new)
        self.assertEqual(new.preset, "")
        self.assertEqual(new.preset_state, {})
        self.assertFalse(config.SETTINGS_FILE.exists())


if __name__ == "__main__":
    unittest.main()
