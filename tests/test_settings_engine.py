"""v2.0 phase 2 — the settings engine: the dialog generated from
settings_schema.REGISTRY plus MainWindow's live-apply chain. Accept
writes only the changed fields (save_fields) against the LIVE settings
object; previews revert on cancel with zero persistence; and
LIVE_APPLY_ORDER encodes scale→theme, corner→csd (with the corner-only
extra trigger) and pitch-last as data — appliers run in list order,
each once, and a theme+font+size+case accept costs exactly one
apply_bundle and one queued restyle.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QSpinBox,
)

from tide import config, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.settings import Settings
from tide.ui import settings_schema as schema


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# ---------- dialog-side cases ----------

class _DialogCase(unittest.TestCase):
    """Every settings write stubbed (dialog tests must never be able to
    clobber a config file), manager preview state pinned."""

    def setUp(self) -> None:
        _app()
        self.saved_field_calls: list[tuple[str, ...]] = []
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(
            settings_module, "save_fields",
            lambda s, *names: self.saved_field_calls.append(tuple(names)),
        ).start()
        self.addCleanup(mock.patch.stopall)
        theming.manager().set_user_font("")
        theming.manager().set_user_font_size(0)
        theming.set_case_override("")

    def tearDown(self) -> None:
        theming.manager().set_user_font("")
        theming.manager().set_user_font_size(0)
        theming.set_case_override("")
        from tide.ui.track_row import set_thumbnail_override
        set_thumbnail_override("theme")
        QTest.qWait(10)

    def _dialog(self, s: Settings | None = None):
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(s or Settings())
        self.addCleanup(dlg.deleteLater)
        return dlg


class GenerationTests(_DialogCase):
    def test_every_registry_row_builds_a_widget(self) -> None:
        dlg = self._dialog()
        kind_types = {
            "bool": QCheckBox,
            "choice": QComboBox,
            "int": QSpinBox,
            "str": QLineEdit,
            "font": QComboBox,
        }
        for desc in schema.REGISTRY:
            with self.subTest(key=desc.key):
                w = dlg.widget_for(desc.key)
                self.assertIsNotNone(w, f"{desc.key}: no widget generated")
                if desc.kind in kind_types:
                    self.assertIsInstance(w, kind_types[desc.kind])

    def test_legacy_attribute_names_cover_the_registry(self) -> None:
        # legacy names (dlg.theme_picker etc.) must keep working, and
        # every descriptor needs a named attribute — no anonymous widgets
        from tide.ui.settings import _WIDGET_ATTRS
        self.assertEqual(set(_WIDGET_ATTRS), {d.key for d in schema.REGISTRY})
        dlg = self._dialog()
        for key, attr in _WIDGET_ATTRS.items():
            self.assertIs(getattr(dlg, attr), dlg.widget_for(key), key)

    def test_choice_rows_match_the_schema(self) -> None:
        dlg = self._dialog()
        for desc in schema.REGISTRY:
            if desc.kind != "choice":
                continue
            with self.subTest(key=desc.key):
                w = dlg.widget_for(desc.key)
                rows = [(w.itemData(i), w.itemText(i))
                        for i in range(w.count())]
                self.assertEqual(rows, list(schema.resolve_choices(desc)))

    def test_rows_owned_elsewhere_are_gone(self) -> None:
        # fields with their own GUI surface get no row here (mini menu,
        # strip builder, keymap/glyph editors own them)
        dlg = self._dialog()
        for key in ("mini_pin", "fullscreen_pane", "federated_search",
                    "layout_overrides", "keymap", "glyph_overrides"):
            self.assertIsNone(dlg.widget_for(key), key)
        self.assertFalse(hasattr(dlg, "mini_pin_toggle"))
        self.assertFalse(hasattr(dlg, "_slot_pickers"))

    def test_per_preset_markers_match_stash_fields(self) -> None:
        dlg = self._dialog()
        expected = {d.key for d in schema.REGISTRY if d.per_preset}
        self.assertEqual(set(dlg._preset_markers), expected)
        self.assertEqual(
            expected,
            set(presets.STASH_FIELDS) & {d.key for d in schema.REGISTRY},
        )
        for key, marker in dlg._preset_markers.items():
            self.assertIn("per personality", marker.text(), key)

    def test_tab_pages_follow_the_schema_taxonomy(self) -> None:
        dlg = self._dialog()
        labels = [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())]
        self.assertEqual(labels, list(schema.TABS) + ["about"])

    def test_preview_keys_pin_the_schema_flags(self) -> None:
        from tide.ui.settings import SettingsDialog
        self.assertEqual(
            SettingsDialog.PREVIEW_KEYS,
            {d.key for d in schema.REGISTRY if d.preview},
        )

    def test_enable_rules_reference_real_descriptors(self) -> None:
        from tide.ui.settings import _ENABLE_RULES
        keys = {d.key for d in schema.REGISTRY}
        for controller, deps in _ENABLE_RULES.items():
            self.assertIn(controller, keys)
            for dep in deps:
                self.assertIn(dep, keys, f"{controller} → {dep}")

    def test_dependent_rows_follow_their_controller(self) -> None:
        s = Settings()
        s.discord_enabled = False
        dlg = self._dialog(s)
        self.assertFalse(dlg.discord_app_id.isEnabled())
        dlg.discord_toggle.setChecked(True)
        self.assertTrue(dlg.discord_app_id.isEnabled())
        self.assertTrue(dlg.discord_lyrics_toggle.isEnabled())


class DiffApplyTests(_DialogCase):
    def test_only_changed_keys_are_saved(self) -> None:
        s = Settings()
        dlg = self._dialog(s)
        dlg.csd_toggle.setChecked(False)                 # True → False
        idx = dlg.motion_picker.findData("off")
        self.assertGreaterEqual(idx, 0)
        dlg.motion_picker.setCurrentIndex(idx)           # lite → off
        dlg._on_save()
        self.assertEqual(len(self.saved_field_calls), 1)
        self.assertEqual(set(self.saved_field_calls[0]),
                         {"csd_titlebar", "motion"})
        self.assertEqual(set(dlg.changed_keys()), {"csd_titlebar", "motion"})
        self.assertFalse(s.csd_titlebar)
        self.assertEqual(s.motion, "off")
        self.assertTrue(s.local_auto_index)

    def test_untouched_accept_saves_nothing(self) -> None:
        dlg = self._dialog(Settings())
        dlg._on_save()
        self.assertEqual(self.saved_field_calls, [],
                         "an untouched accept must not write anything")
        self.assertEqual(dlg.changed_keys(), ())

    def test_dialog_edits_the_live_object(self) -> None:
        # v1 deep-copied and re-saved the whole object — the write-race
        # this phase kills; the dialog must hold the caller's object
        s = Settings()
        dlg = self._dialog(s)
        self.assertIs(dlg._settings, s)
        self.assertIs(dlg.updated_settings(), s)

    def test_report_plays_on_stamps_answered(self) -> None:
        s = Settings()
        dlg = self._dialog(s)
        dlg.report_plays_toggle.setChecked(True)
        dlg._on_save()
        self.assertTrue(s.report_plays)
        self.assertTrue(s.report_plays_answered)
        self.assertEqual(set(self.saved_field_calls[0]),
                         {"report_plays", "report_plays_answered"})

    def test_report_plays_untouched_off_does_not_stamp(self) -> None:
        s = Settings()
        dlg = self._dialog(s)
        dlg._on_save()
        self.assertFalse(s.report_plays_answered,
                         "untouched-and-off is not an answer")

    def test_report_plays_already_on_counts_as_answered(self) -> None:
        s = Settings()
        s.report_plays = True
        dlg = self._dialog(s)
        dlg._on_save()
        self.assertTrue(s.report_plays_answered)
        self.assertEqual(set(self.saved_field_calls[0]),
                         {"report_plays_answered"})


class PreviewTests(_DialogCase):
    """Previews apply while the dialog is open and revert on cancel —
    they never commit to the settings object and never persist."""

    def setUp(self) -> None:
        super().setUp()
        theming.manager().refresh()
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)

    def tearDown(self) -> None:
        theming.manager().apply("brutalist-mono")
        super().tearDown()
        QTest.qWait(20)

    def test_theme_preview_applies_and_cancel_reverts(self) -> None:
        s = Settings()          # theme = brutalist-mono
        dlg = self._dialog(s)
        idx = dlg.theme_picker.findData("gruvbox")
        self.assertGreaterEqual(idx, 0)
        dlg.theme_picker.setCurrentIndex(idx)
        self.assertEqual(theming.manager().current().slug, "gruvbox",
                         "picking a theme must preview immediately")
        dlg._on_cancel()
        self.assertEqual(theming.manager().current().slug, "brutalist-mono",
                         "cancel must restore the pre-dialog theme")
        self.assertEqual(s.theme, "brutalist-mono",
                         "a preview must never commit to the settings")
        self.assertEqual(self.saved_field_calls, [],
                         "a preview must never persist")

    def test_case_preview_cancel_reverts(self) -> None:
        s = Settings()
        dlg = self._dialog(s)
        idx = dlg.case_picker.findData("upper")
        self.assertGreaterEqual(idx, 0)
        dlg.case_picker.setCurrentIndex(idx)
        self.assertEqual(theming._CASE_OVERRIDE, "upper")
        dlg._on_cancel()
        self.assertEqual(theming._CASE_OVERRIDE, "")
        self.assertEqual(s.text_case_override, "")

    def test_thumbnails_preview_cancel_reverts(self) -> None:
        from tide.ui import track_row
        s = Settings()
        dlg = self._dialog(s)
        idx = dlg.thumbnails_picker.findData("off")
        dlg.thumbnails_picker.setCurrentIndex(idx)
        self.assertEqual(track_row._thumbnail_setting_override, "off")
        dlg._on_cancel()
        self.assertEqual(track_row._thumbnail_setting_override, "theme")
        self.assertEqual(s.show_thumbnails, "theme")

    def test_esc_reverts_previews_like_cancel(self) -> None:
        # QDialog's Esc calls reject() directly, never the cancel button —
        # the revert must live on reject() or previews leak past a
        # keyboard cancel (rule 8)
        from PySide6.QtCore import Qt as _Qt
        s = Settings()          # theme = brutalist-mono
        dlg = self._dialog(s)
        idx = dlg.theme_picker.findData("gruvbox")
        self.assertGreaterEqual(idx, 0)
        dlg.theme_picker.setCurrentIndex(idx)
        self.assertEqual(theming.manager().current().slug, "gruvbox")
        QTest.keyClick(dlg, _Qt.Key_Escape)
        from PySide6.QtWidgets import QDialog
        self.assertEqual(dlg.result(), QDialog.Rejected)
        self.assertEqual(theming.manager().current().slug, "brutalist-mono",
                         "Esc must revert previews exactly like [cancel]")
        self.assertEqual(s.theme, "brutalist-mono")
        self.assertEqual(self.saved_field_calls, [])

    def test_bare_reject_reverts_previews(self) -> None:
        # the WM close button routes through closeEvent → reject(), so a
        # direct reject() stands in for close-X
        s = Settings()
        dlg = self._dialog(s)
        idx = dlg.case_picker.findData("upper")
        self.assertGreaterEqual(idx, 0)
        dlg.case_picker.setCurrentIndex(idx)
        self.assertEqual(theming._CASE_OVERRIDE, "upper")
        dlg.reject()
        self.assertEqual(theming._CASE_OVERRIDE, "",
                         "close-X (reject) must revert the case preview")
        self.assertEqual(s.text_case_override, "")

    def test_revert_runs_once_across_cancel_paths(self) -> None:
        # button-cancel then teardown reject must not revert twice — a
        # second apply would be a pointless extra restyle
        s = Settings()
        dlg = self._dialog(s)
        idx = dlg.theme_picker.findData("gruvbox")
        dlg.theme_picker.setCurrentIndex(idx)
        with mock.patch.object(theming.manager(), "apply",
                               wraps=theming.manager().apply) as spy:
            dlg._on_cancel()
            dlg.reject()
            self.assertEqual(spy.call_count, 1,
                             "the revert must run exactly once")

    def test_reject_after_accept_does_not_revert(self) -> None:
        # accept commits the previewed theme; a stray reject afterwards
        # (teardown paths) must not un-apply committed state
        s = Settings()
        dlg = self._dialog(s)
        idx = dlg.theme_picker.findData("gruvbox")
        dlg.theme_picker.setCurrentIndex(idx)
        dlg._on_save()
        self.assertEqual(s.theme, "gruvbox")
        dlg.reject()
        self.assertEqual(theming.manager().current().slug, "gruvbox",
                         "reject after accept reverted committed state")

    def test_populate_does_not_fire_previews(self) -> None:
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)
        s = Settings()
        s.theme = "gruvbox"
        self._dialog(s)
        self.assertEqual(theming.manager().current().slug, "brutalist-mono",
                         "construction previewed the stored theme")


# ---------- the live-apply chain (window side) ----------

class ChainStructureTests(unittest.TestCase):
    def test_every_live_name_is_a_real_method_in_the_chain(self) -> None:
        from tide.ui.window import LIVE_APPLY_ORDER, MainWindow
        for desc in schema.REGISTRY:
            if desc.live is None:
                continue
            with self.subTest(key=desc.key):
                self.assertIn(desc.live, LIVE_APPLY_ORDER)
                self.assertTrue(hasattr(MainWindow, desc.live))
        for name in LIVE_APPLY_ORDER:
            self.assertTrue(callable(getattr(MainWindow, name)), name)

    def test_no_duplicate_chain_entries(self) -> None:
        from tide.ui.window import LIVE_APPLY_ORDER
        self.assertEqual(len(LIVE_APPLY_ORDER), len(set(LIVE_APPLY_ORDER)))

    def test_contract_orderings_are_encoded_as_data(self) -> None:
        from tide.ui.window import LIVE_APPLY_EXTRA_TRIGGERS, LIVE_APPLY_ORDER
        i = LIVE_APPLY_ORDER.index
        # scale→theme: a scale change re-applies the theme, so the final
        # theme bundle must come after it
        self.assertLess(i("apply_ui_scale_setting"),
                        i("apply_theme_bundle_setting"))
        # corner→csd: the translucency re-check in the csd applier must
        # see the final corner radius AND run on a corner-only change
        self.assertLess(i("apply_corner_setting"), i("apply_csd_setting"))
        self.assertEqual(LIVE_APPLY_EXTRA_TRIGGERS["apply_csd_setting"],
                         ("corner_style",))
        # pitch→speed: pitch correction re-applies the filter chain the
        # current speed rides on — it runs last
        self.assertEqual(LIVE_APPLY_ORDER[-1], "apply_pitch_setting")


class _WindowCase(unittest.TestCase):
    """Real-MainWindow cases (the test_preset_flip harness pattern)."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-engine-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
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


class LiveApplyRunTests(_WindowCase):
    def _recorded_window(self):
        from tide.ui import window as window_module
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        calls: list[str] = []
        for name in window_module.LIVE_APPLY_ORDER:
            setattr(w, name, (lambda n=name: calls.append(n)))
        return w, calls

    def test_appliers_run_in_list_order_each_once(self) -> None:
        from tide.ui import window as window_module
        w, calls = self._recorded_window()
        w.run_live_appliers({d.key for d in schema.REGISTRY})
        expected = [
            name for name in window_module.LIVE_APPLY_ORDER
            if any(d.live == name for d in schema.REGISTRY)
        ]
        self.assertEqual(calls, expected,
                         "appliers must run in LIVE_APPLY_ORDER, once each")

    def test_corner_only_change_still_rechecks_translucency(self) -> None:
        w, calls = self._recorded_window()
        w.run_live_appliers({"corner_style"})
        self.assertEqual(calls, ["apply_corner_setting", "apply_csd_setting"],
                         "a corner-only change must re-run the csd/"
                         "translucency applier, after the corner applier")

    def test_scale_change_lands_before_the_theme_bundle(self) -> None:
        w, calls = self._recorded_window()
        w.run_live_appliers({"ui_scale", "theme"})
        self.assertEqual(calls, ["apply_ui_scale_setting",
                                 "apply_theme_bundle_setting"])

    def test_keys_without_appliers_run_nothing(self) -> None:
        w, calls = self._recorded_window()
        w.run_live_appliers(
            {"home_layout", "prefetch_warm_results", "report_plays",
             "spotify_bitrate", "local_auto_index"})
        self.assertEqual(calls, [])

    def test_empty_change_set_is_a_noop(self) -> None:
        w, calls = self._recorded_window()
        w.run_live_appliers(set())
        self.assertEqual(calls, [])

    def test_theme_bundle_accept_is_one_apply_bundle_one_restyle(self) -> None:
        # a theme+font+size+case accept costs exactly ONE apply_bundle
        # (not three serial setter pushes) and ONE queued restyle
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)   # drain construction-time restyles
        s.theme = "gruvbox"
        s.font_family_override = "IBM Plex Mono"
        s.font_size_override_pt = 12
        s.text_case_override = "upper"
        mgr = theming.manager()
        with mock.patch.object(mgr, "apply_bundle",
                               wraps=mgr.apply_bundle) as bundle_spy, \
             mock.patch.object(self.app, "setStyleSheet") as restyle_spy:
            w.run_live_appliers({
                "theme", "font_family_override",
                "font_size_override_pt", "text_case_override",
            })
            self.assertEqual(bundle_spy.call_count, 1,
                             "theme+font+size+case must be ONE apply_bundle")
            self.assertEqual(restyle_spy.call_count, 0,
                             "the restyle must be deferred out of the "
                             "calling turn")
            QTest.qWait(30)
            self.assertEqual(restyle_spy.call_count, 1,
                             "the four axes must coalesce into one restyle")
        self.assertEqual(mgr.current().slug, "gruvbox")
        self.assertEqual(mgr.user_font(), "IBM Plex Mono")
        self.assertEqual(mgr.user_font_size(), 12)

    def test_layout_preview_reverts_on_esc_too(self) -> None:
        # Esc goes through QDialog.reject(), not the cancel button (rule
        # 8) — and the layout preview rebuilds the REAL window's strip,
        # not just manager state
        from PySide6.QtCore import Qt as _Qt
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(settings_module, "save_fields",
                          lambda s, *names: None).start()
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(s, parent=w)
        idx = dlg.layout_picker.findData("focused")
        self.assertGreaterEqual(idx, 0)
        dlg.layout_picker.setCurrentIndex(idx)
        self.assertEqual(w._layout_slug, "focused")
        QTest.keyClick(dlg, _Qt.Key_Escape)
        self.assertEqual(layout_module.manager().current().slug, "classic",
                         "Esc must revert the layout preview")
        self.assertEqual(w._layout_slug, "classic")
        self.assertEqual(s.layout, "classic")

    def test_layout_preview_through_parent_reverts_on_cancel(self) -> None:
        # rule 8 end-to-end: picking a layout hot-swaps the real window;
        # cancel puts it back
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(settings_module, "save_fields",
                          lambda s, *names: None).start()
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        from tide.ui.settings import SettingsDialog
        # no explicit cleanup: parented to the window, dies with it in teardown
        dlg = SettingsDialog(s, parent=w)
        idx = dlg.layout_picker.findData("focused")
        self.assertGreaterEqual(idx, 0)
        dlg.layout_picker.setCurrentIndex(idx)
        self.assertEqual(layout_module.manager().current().slug, "focused",
                         "picking a layout must preview immediately")
        self.assertEqual(w._layout_slug, "focused")
        dlg._on_cancel()
        self.assertEqual(layout_module.manager().current().slug, "classic",
                         "cancel must restore the pre-dialog layout")
        self.assertEqual(w._layout_slug, "classic")
        self.assertEqual(s.layout, "classic")


class ParkedStripOverrideTests(_WindowCase):
    """A strip-builder pick made while the layout combo previews an
    unaccepted layout must not commit immediately — that would persist
    overrides for a layout settings doesn't hold. The dialog parks the
    pick: accept commits layout + bar together, cancel drops both."""

    def setUp(self) -> None:
        super().setUp()
        self.save_calls: list[tuple[str, ...]] = []
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(
            settings_module, "save_fields",
            lambda s, *names: self.save_calls.append(tuple(names)),
        ).start()

    def _previewing_dialog(self):
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(s, parent=w)   # dies with the window
        idx = dlg.layout_picker.findData("focused")
        self.assertGreaterEqual(idx, 0)
        dlg.layout_picker.setCurrentIndex(idx)      # preview applies
        self.assertEqual(w._layout_slug, "focused")
        return s, w, dlg

    def test_pick_during_preview_parks_instead_of_committing(self) -> None:
        s, w, dlg = self._previewing_dialog()
        dlg._on_strip_overrides({"progress": "dotted"})   # builder accept
        self.assertEqual(s.layout_overrides, {},
                         "a pick against a previewed layout must not "
                         "touch the settings object")
        self.assertNotIn(("layout_overrides",), self.save_calls,
                         "…and must not persist")
        self.assertEqual(w._slot_progress, "dotted")
        self.assertEqual(w._layout_slug, "focused")

    def test_cancel_drops_the_parked_pick(self) -> None:
        s, w, dlg = self._previewing_dialog()
        dlg._on_strip_overrides({"progress": "dotted"})
        dlg._on_cancel()
        self.assertEqual(w._layout_slug, "classic")
        self.assertEqual(w._slot_progress, "blocks",
                         "cancel left the parked bar on the window")
        self.assertEqual(s.layout, "classic")
        self.assertEqual(s.layout_overrides, {})
        self.assertEqual(self.save_calls, [])

    def test_accept_commits_layout_and_bar_together(self) -> None:
        s, w, dlg = self._previewing_dialog()
        dlg._on_strip_overrides({"progress": "dotted"})
        dlg._on_save()
        self.assertEqual(s.layout, "focused")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"},
                         "accept must commit the parked bar with its "
                         "layout")
        self.assertIn(("layout_overrides",), self.save_calls)
        self.assertEqual(w._slot_progress, "dotted")
        self.assertIn("layout", dlg.changed_keys())

    def test_abandoned_base_drops_the_parked_pick_on_accept(self) -> None:
        # the parked diff describes focused's slots — committing it after
        # the combo moved back to classic is exactly the skew this fixes
        s, w, dlg = self._previewing_dialog()
        dlg._on_strip_overrides({"progress": "dotted"})
        idx = dlg.layout_picker.findData("classic")
        dlg.layout_picker.setCurrentIndex(idx)      # preview re-applies
        dlg._on_save()
        self.assertEqual(s.layout, "classic")
        self.assertEqual(s.layout_overrides, {},
                         "an abandoned-base pick must be dropped")
        self.assertNotIn(("layout_overrides",), self.save_calls)
        self.assertEqual(w._slot_progress, "blocks")

    def test_no_preview_pending_still_commits_immediately(self) -> None:
        # the v1 immediate-commit path is untouched when the layout combo
        # sits on the opening value
        s = Settings()
        s.first_launch_complete = True
        w = self._make_window(s)
        QTest.qWait(30)
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(s, parent=w)
        dlg._on_strip_overrides({"progress": "dotted"})
        self.assertEqual(s.layout_overrides, {"progress": "dotted"})
        self.assertIn(("layout_overrides",), self.save_calls)
        self.assertEqual(w._slot_progress, "dotted")

    def test_reopened_builder_starts_from_the_parked_pick(self) -> None:
        s, w, dlg = self._previewing_dialog()
        dlg._on_strip_overrides({"progress": "dotted"})
        from tide.ui import strip_builder as sb_module
        with mock.patch.object(sb_module, "open_strip_builder") as spy:
            dlg.strip_builder_btn.click()
            self.assertEqual(spy.call_args.kwargs["overrides"],
                             {"progress": "dotted"},
                             "re-opening must show the parked pick, not "
                             "the stale saved overrides")


if __name__ == "__main__":
    unittest.main()
