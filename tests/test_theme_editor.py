"""ThemeEditorDialog — the phase-2 token/color editor. Edits preview
live through the manager's STICKY user layer (deferred restyles only);
the swatch click defers its QColorDialog out of the click emission (the
modal-from-click segfault rule); cancel leaves zero trace and restores
pre-existing sticky overrides; [save as my theme] writes one theme dir,
refreshes the manager onto the new slug, and never touches
settings.toml (persisting settings.theme is the caller's job); slug
collisions suffix and never shadow a bundled slug.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_theme_editor.py
"""
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import config, theming
from tide.ui import theme_editor
from tide.ui.theme_editor import ThemeEditorDialog, slugify


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _EditorCase(unittest.TestCase):
    BASE_SLUG = "brutalist-mono"

    def setUp(self) -> None:
        self.app = _app()
        # hermetic disk: fresh user-themes dir + settings path per test
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-themeed-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_user_dir = config.USER_THEMES_DIR
        config.USER_THEMES_DIR = Path(self._tmp.name) / "themes"
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        # suppress app-wide QSS pushes (test_preset_flip's spy pattern);
        # manager state stays fully real
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        # known-clean baseline, both override layers cleared — apply()
        # only drops the dynamic layer on a slug CHANGE, so a same-slug
        # re-apply would leak an earlier test's tokens
        mgr = theming.manager()
        mgr.refresh()
        mgr._user_overrides.clear()
        mgr._dynamic_overrides.clear()
        theming.set_case_override("")
        mgr.apply_bundle(slug=self.BASE_SLUG, font_family="", font_size=0)
        QTest.qWait(20)
        self.dlg = None

    def tearDown(self) -> None:
        if self.dlg is not None:
            self.dlg.deleteLater()
            self.dlg = None
        QTest.qWait(30)
        mgr = theming.manager()
        mgr._user_overrides.clear()
        mgr._dynamic_overrides.clear()
        theming.set_case_override("")
        mgr.apply_bundle(slug=self.BASE_SLUG, font_family="", font_size=0)
        # restore the real dirs BEFORE the final refresh so the manager
        # forgets any tmp user theme a save test wrote
        config.USER_THEMES_DIR = self._orig_user_dir
        config.SETTINGS_FILE = self._orig_settings_file
        mgr.refresh()
        QTest.qWait(30)
        for _ in range(10):   # drain any trailing queued restyle
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    def _open(self) -> ThemeEditorDialog:
        self.dlg = ThemeEditorDialog()
        return self.dlg

    def _manager_state(self) -> dict:
        mgr = theming.manager()
        return {
            "overrides": dict(mgr._user_overrides),
            "font": mgr.user_font(),
            "font_size": mgr.user_font_size(),
            "case": theming.case_override(),
            "slug": mgr.current().slug,
        }


class BuildTests(_EditorCase):
    def test_rows_cover_the_base_themes_color_tokens(self) -> None:
        dlg = self._open()
        base = theming.manager().current()
        declared_colors = {
            k for k, v in base.tokens.items()
            if str(v).startswith("#")
        }
        self.assertEqual(set(dlg.token_keys()), declared_colors)
        # brutalist-mono declares the canonical ~11.
        for key in ("bg", "fg", "dim", "accent", "sel_bg", "border_col"):
            self.assertIn(key, dlg.token_keys())
            self.assertIn(key, dlg.swatches)

    def test_seeds_show_what_is_on_screen(self) -> None:
        # a pre-existing sticky override is part of "what I see" and must
        # seed the row; dynamic (adaptive) overrides must NOT
        mgr = theming.manager()
        mgr.set_user_override("accent", "#00ff00")
        mgr.override_tokens({"accent_alt_dynamic_ignore": "#123456"})
        QTest.qWait(20)
        dlg = self._open()
        self.assertEqual(dlg.current_tokens()["accent"], "#00ff00")
        self.assertNotIn("accent_alt_dynamic_ignore", dlg.token_keys())
        base = mgr.current()
        self.assertEqual(dlg.current_tokens()["bg"], base.tokens["bg"])
        self.assertEqual(dlg.size_spin.value(), 10)
        self.assertEqual(dlg.weight_combo.currentData(), 400)
        self.assertEqual(dlg.radius_spin.value(), 0)
        self.assertEqual(dlg.family_combo.currentText(), "IBM Plex Mono")

    def test_radius_seed_honors_the_sticky_corner_override(self) -> None:
        theming.manager().set_user_override("radius", "6px")
        QTest.qWait(20)
        dlg = self._open()
        self.assertEqual(dlg.radius_spin.value(), 6)


class DeferredColorDialogTests(_EditorCase):
    def test_swatch_click_defers_the_color_dialog(self) -> None:
        # any modal reachable from a click handler opens on the NEXT
        # event-loop turn, never inside the click emission
        dlg = self._open()
        with mock.patch.object(
            theme_editor.QColorDialog, "getColor",
            return_value=QColor("#123456"),
        ) as picker:
            dlg.swatches["accent"].click()
            self.assertEqual(picker.call_count, 0,
                             "QColorDialog opened synchronously inside "
                             "the click emission")
            QTest.qWait(30)
            self.assertEqual(picker.call_count, 1)
        self.assertEqual(dlg.current_tokens()["accent"], "#123456")
        eff = theming.manager().current_effective()
        self.assertEqual(eff.token("accent"), "#123456")

    def test_cancelled_color_dialog_changes_nothing(self) -> None:
        dlg = self._open()
        before = dlg.current_tokens()["accent"]
        with mock.patch.object(
            theme_editor.QColorDialog, "getColor",
            return_value=QColor(),   # invalid = user hit cancel
        ):
            dlg.swatches["accent"].click()
            QTest.qWait(30)
        self.assertEqual(dlg.current_tokens()["accent"], before)
        self.assertNotIn("accent", theming.manager()._user_overrides)


class LivePreviewTests(_EditorCase):
    def test_token_edit_previews_through_the_user_layer(self) -> None:
        dlg = self._open()
        dlg.set_token("accent", "#ff0000")
        mgr = theming.manager()
        self.assertEqual(mgr._user_overrides.get("accent"), "#ff0000")
        self.assertEqual(mgr.current_effective().token("accent"), "#ff0000")
        self.assertEqual(dlg.swatches["accent"].text(), "#ff0000")
        self.assertFalse(config.SETTINGS_FILE.exists())
        self.assertFalse(config.USER_THEMES_DIR.exists())

    def test_typography_and_radius_preview_live(self) -> None:
        dlg = self._open()
        dlg.family_combo.setCurrentText("Arial")
        dlg._on_family_changed()
        dlg.size_spin.setValue(13)
        dlg.case_combo.setCurrentIndex(dlg.case_combo.findData("upper"))
        dlg.radius_spin.setValue(9)
        mgr = theming.manager()
        self.assertEqual(mgr.user_font(), "Arial")
        self.assertEqual(mgr.user_font_size(), 13)
        self.assertEqual(theming.case_override(), "upper")
        self.assertEqual(mgr._user_overrides.get("radius"), "9px")
        self.assertEqual(
            theming.effective_radius_px(mgr.current_effective()), 9)

    def test_restyles_stay_deferred(self) -> None:
        # the preview path must never push app QSS inside the calling
        # turn — the manager's queue owns the flush
        dlg = self._open()
        QTest.qWait(30)   # drain construction-time queue
        with mock.patch.object(self.app, "setStyleSheet") as spy:
            dlg.set_token("accent", "#ff0000")
            dlg.radius_spin.setValue(9)
            self.assertEqual(spy.call_count, 0,
                             "preview pushed QSS synchronously")
            QTest.qWait(30)
            self.assertEqual(spy.call_count, 1,
                             "a burst of edits must coalesce into one "
                             "deferred restyle")


class CancelTests(_EditorCase):
    def test_cancel_after_edits_leaves_zero_trace(self) -> None:
        before = self._manager_state()
        dlg = self._open()
        dlg.set_token("accent", "#ff0000")
        dlg.set_token("bg", "#222222")
        dlg.family_combo.setCurrentText("Arial")
        dlg._on_family_changed()
        dlg.size_spin.setValue(13)
        dlg.case_combo.setCurrentIndex(dlg.case_combo.findData("upper"))
        dlg.radius_spin.setValue(9)
        QTest.qWait(20)
        dlg.cancel_btn.click()
        QTest.qWait(30)
        self.assertEqual(self._manager_state(), before,
                         "cancel left preview state behind")
        # drop the dynamic layer before the effective read: a stray
        # adaptive delivery from an earlier file could shadow the
        # now-un-overridden accent (same hazard test_preset_flip drains)
        mgr = theming.manager()
        mgr._dynamic_overrides.clear()
        self.assertEqual(mgr.current_effective().token("accent"),
                         mgr.current().tokens["accent"])
        self.assertFalse(config.SETTINGS_FILE.exists(),
                         "the editor must never write settings")
        self.assertFalse(config.USER_THEMES_DIR.exists(),
                         "cancel wrote a theme dir")

    def test_cancel_restores_preexisting_sticky_overrides(self) -> None:
        # the user's own sticky layer must come BACK — not get removed
        # with ours
        mgr = theming.manager()
        mgr.set_user_override("accent", "#00ff00")
        mgr.set_user_override("radius", "6px")
        QTest.qWait(20)
        dlg = self._open()
        dlg.set_token("accent", "#0000ff")
        dlg.radius_spin.setValue(12)
        QTest.qWait(20)
        dlg.reject()   # Esc path — same contract as the cancel button
        QTest.qWait(30)
        self.assertEqual(mgr._user_overrides.get("accent"), "#00ff00")
        self.assertEqual(mgr._user_overrides.get("radius"), "6px")

    def test_untouched_cancel_is_silent(self) -> None:
        # pinned on the manager call, not theme_changed (a stray adaptive
        # delivery could add emissions): no-edit close costs no re-apply
        dlg = self._open()
        QTest.qWait(30)
        mgr = theming.manager()
        with mock.patch.object(mgr, "apply_bundle",
                               wraps=mgr.apply_bundle) as spy:
            dlg.cancel_btn.click()
            QTest.qWait(30)
        self.assertEqual(spy.call_count, 0,
                         "a no-edit cancel re-applied the theme for "
                         "nothing")

    def test_reject_reverts_only_once(self) -> None:
        dlg = self._open()
        dlg.set_token("accent", "#ff0000")
        dlg.reject()
        QTest.qWait(20)
        # the manager drifted after close; a second reject (window close
        # after cancel) must not re-impose the stale snapshot
        theming.manager().set_user_override("accent", "#abcdef")
        dlg.reject()
        QTest.qWait(20)
        self.assertEqual(
            theming.manager()._user_overrides.get("accent"), "#abcdef")


class SaveTests(_EditorCase):
    def _edit_and_save(self, name: str = "My Waves") -> ThemeEditorDialog:
        dlg = self._open()
        dlg.set_token("accent", "#123456")
        dlg.radius_spin.setValue(4)
        dlg.weight_combo.setCurrentIndex(dlg.weight_combo.findData(600))
        dlg.name_edit.setText(name)
        dlg.save_btn.click()
        QTest.qWait(30)
        return dlg

    def test_save_writes_exactly_the_intended_theme(self) -> None:
        pre_open = self._manager_state()
        base = theming.manager().current()
        dlg = self._edit_and_save()
        dest = config.USER_THEMES_DIR / "my-waves"
        self.assertTrue((dest / "theme.toml").is_file())
        self.assertTrue((dest / "theme.qss").is_file())
        with open(dest / "theme.toml", "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["meta"]["name"], "My Waves")
        self.assertEqual(data["meta"]["slug"], "my-waves")
        self.assertEqual(data["meta"]["aesthetic"], "brutalist")
        self.assertTrue(data["meta"]["dark"])
        self.assertEqual(data["tokens"]["accent"], "#123456")
        self.assertEqual(data["tokens"]["bg"], base.tokens["bg"])
        self.assertEqual(data["tokens"]["border_dim"],
                         base.tokens["border_dim"])
        self.assertEqual(data["typography"]["weight"], 600)
        self.assertEqual(data["typography"]["family"],
                         base.typography["family"])
        self.assertEqual(data["typography"]["size_pt"], 10)
        self.assertEqual(data["layout"]["radius_px"], 4)
        self.assertEqual(data["layout"]["control_style"],
                         base.layout["control_style"])
        self.assertEqual(data["layout"]["list_marker"],
                         base.layout["list_marker"])
        self.assertEqual(data["slots"], dict(base.slots))
        # QSS: a standalone copy of the base's full stylesheet. Bundled
        # themes ship no theme.qss (they compose from _base.qss at load),
        # so the copy comes from Theme.qss and must not depend on _base.qss.
        self.assertEqual(
            (dest / "theme.qss").read_text(encoding="utf-8"), base.qss)
        mgr = theming.manager()
        self.assertIn("my-waves", {t.slug for t in mgr.list_themes()})
        self.assertEqual(mgr.current().slug, "my-waves")
        # …with the preview layer gone: the accent now comes from the
        # THEME, and the sticky layer is back to its pre-open state
        self.assertEqual(mgr._user_overrides, pre_open["overrides"])
        self.assertEqual(mgr.user_font(), pre_open["font"])
        self.assertEqual(mgr.user_font_size(), pre_open["font_size"])
        self.assertEqual(theming.case_override(), pre_open["case"])
        # the THEME carries the edit now. Drop the dynamic layer before
        # the effective read — a stray adaptive delivery can land
        # mid-test and shadow an un-overridden token.
        self.assertEqual(mgr.current().tokens["accent"], "#123456")
        mgr._dynamic_overrides.clear()
        self.assertEqual(mgr.current_effective().token("accent"), "#123456")
        self.assertFalse(config.SETTINGS_FILE.exists())
        self.assertEqual(dlg.saved_slug(), "my-waves")

    def test_save_emits_theme_saved_and_accepts(self) -> None:
        got: list[str] = []
        dlg = self._open()
        dlg.theme_saved.connect(got.append)
        dlg.name_edit.setText("plain clone")
        dlg.save_btn.click()
        QTest.qWait(30)
        self.assertEqual(got, ["plain-clone"])
        self.assertEqual(dlg.result(), ThemeEditorDialog.Accepted)
        dest = config.USER_THEMES_DIR / "plain-clone"
        with open(dest / "theme.toml", "rb") as f:
            data = tomllib.load(f)
        base_tokens = theming.discover_themes()[self.BASE_SLUG].tokens
        for key, value in base_tokens.items():
            self.assertEqual(data["tokens"][key], value)

    def test_saved_theme_roundtrips_through_discovery(self) -> None:
        self._edit_and_save()
        theme = theming.discover_themes()["my-waves"]
        self.assertEqual(theme.name, "My Waves")
        self.assertEqual(theme.aesthetic, "brutalist")
        self.assertEqual(theme.tokens["accent"], "#123456")
        self.assertEqual(theme.t("layout", "radius_px"), 4)
        self.assertEqual(theme.t("typography", "weight"), 600)

    def test_save_bakes_what_is_on_screen(self) -> None:
        theming.manager().set_user_override("accent", "#00ff00")
        QTest.qWait(20)
        dlg = self._open()
        dlg.name_edit.setText("green look")
        dlg.save_btn.click()
        QTest.qWait(30)
        with open(config.USER_THEMES_DIR / "green-look" / "theme.toml",
                  "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["tokens"]["accent"], "#00ff00")
        # the untouched sticky override survives the accept (user state,
        # not our preview)
        self.assertEqual(
            theming.manager()._user_overrides.get("accent"), "#00ff00")

    def test_slug_collisions_suffix(self) -> None:
        first = self._edit_and_save("waves")
        self.assertEqual(first.saved_slug(), "waves")
        first.deleteLater()
        QTest.qWait(20)
        dlg = self._open()
        dlg.name_edit.setText("waves")
        dlg.save_btn.click()
        QTest.qWait(30)
        self.assertEqual(dlg.saved_slug(), "waves-2")
        self.assertTrue(
            (config.USER_THEMES_DIR / "waves-2" / "theme.toml").is_file())

    def test_bundled_slugs_are_never_shadowed(self) -> None:
        dlg = self._open()
        dlg.name_edit.setText("nord")
        dlg.save_btn.click()
        QTest.qWait(30)
        self.assertEqual(dlg.saved_slug(), "nord-2")
        self.assertFalse((config.USER_THEMES_DIR / "nord").exists(),
                         "a user theme shadowed bundled nord")

    def test_empty_name_defaults_from_the_base_theme(self) -> None:
        dlg = self._open()
        dlg.save_btn.click()
        QTest.qWait(30)
        self.assertEqual(dlg.saved_slug(), "my-brutalist-mono")
        with open(config.USER_THEMES_DIR / "my-brutalist-mono"
                  / "theme.toml", "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["meta"]["name"], "my brutalist mono")

    def test_themes_dir_resolves_at_call_time(self) -> None:
        # the dialog was built while USER_THEMES_DIR pointed elsewhere;
        # the save must land wherever tide.config points WHEN it runs
        dlg = self._open()
        moved = Path(self._tmp.name) / "elsewhere"
        config.USER_THEMES_DIR = moved
        dlg.name_edit.setText("late bind")
        dlg.save_btn.click()
        QTest.qWait(30)
        self.assertTrue((moved / "late-bind" / "theme.toml").is_file())

    def test_write_failure_keeps_the_dialog_open(self) -> None:
        # point the themes dir at a FILE so mkdir fails: the dialog
        # reports inline, stays open, and a later cancel still reverts
        blocker = Path(self._tmp.name) / "blocked"
        blocker.write_text("not a dir")
        config.USER_THEMES_DIR = blocker
        dlg = self._open()
        dlg.set_token("accent", "#ff0000")
        dlg.name_edit.setText("doomed")
        dlg.save_btn.click()
        QTest.qWait(30)
        self.assertNotEqual(dlg.result(), ThemeEditorDialog.Accepted)
        self.assertIn("couldn't write", dlg.status_label.text())
        self.assertEqual(dlg.saved_slug(), "")
        dlg.cancel_btn.click()
        QTest.qWait(30)
        self.assertNotIn("accent", theming.manager()._user_overrides)


class SlugifyTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("My Waves"), "my-waves")
        self.assertEqual(slugify("  My!! Theme  "), "my-theme")
        self.assertEqual(slugify("Café · Nights"), "caf-nights")
        self.assertEqual(slugify(""), "my-theme")
        self.assertEqual(slugify("!!!"), "my-theme")


if __name__ == "__main__":
    unittest.main()
