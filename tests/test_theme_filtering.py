"""v2.0 phase 4 — personality-aware theme picking. The appearance tab
lists only the current personality's themes (5 brutalist / 11 modern
today) with a persisted "show all themes" escape. The schema keeps the
whole catalog in ``theme_choices`` (what the meta-tests read);
narrowing is ``theme_choices_for``. The filtered picker always lists
the theme it is holding — one that can't show its own value rewrites
the setting on the next accept — and a cross-personality pick with
show-all on routes through the existing reconcile → switch_preset path;
the dialog never flips anything itself.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox

from tide import config, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.settings import Settings
from tide.ui import settings_schema as schema


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _catalog() -> dict:
    return theming.discover_themes()


def _slugs(rows) -> list[str]:
    return [value for value, _label in rows]


def _by_aesthetic(aesthetic: str) -> list[str]:
    """Catalog slugs one personality's picker should list, in display-name
    order — computed independently of the code under test. A theme that
    never declared an aesthetic belongs to both sides (the mono guess is
    not a claim)."""
    themes = _catalog()
    return [
        slug for slug, theme in sorted(themes.items(), key=lambda kv: kv[1].name)
        if theme.aesthetic == aesthetic or not theme.aesthetic_declared
    ]


def _install_theme(case, slug: str, *, aesthetic: str = "",
                   mono: bool = False) -> Path:
    """Drop a theme into a temp USER themes dir for the rest of ``case``
    — the third-party theme the bundled catalog can't stand in for."""
    tmp = tempfile.TemporaryDirectory(prefix="tide-usertheme-")
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / slug).mkdir(parents=True)
    meta = f'[meta]\nslug = "{slug}"\nname = "{slug}"\n'
    if aesthetic:
        meta += f'aesthetic = "{aesthetic}"\n'
    (root / slug / "theme.toml").write_text(
        f'{meta}[typography]\nmono = {"true" if mono else "false"}\n'
        '[tokens]\nbg = "#101010"\nfg = "#eeeeee"\n',
        encoding="utf-8")
    patch = mock.patch.object(config, "USER_THEMES_DIR", root)
    patch.start()
    case.addCleanup(patch.stop)
    return root


# ---------- the schema layer ----------

class SchemaFilterTests(unittest.TestCase):
    def test_the_provider_is_still_the_whole_catalog(self) -> None:
        # resolve_choices() is what the schema meta-tests read; narrowing
        # it in place would make every-default-pickable unrepresentable
        rows = schema.theme_choices()
        self.assertEqual(sorted(_slugs(rows)), sorted(_catalog()))
        self.assertEqual(
            rows, schema.resolve_choices(schema.by_key()["theme"]))

    def test_bundled_catalog_is_five_brutalist_and_eleven_modern(self) -> None:
        # the shipped split, pinned; scoped to the bundled dir so a local
        # user theme can't flake it
        bundled = [
            t for t in _catalog().values()
            if theming.BUNDLED_THEMES_DIR in t.path.parents
        ]
        counts = {"brutalist": 0, "modern": 0}
        for t in bundled:
            counts[t.aesthetic] += 1
            self.assertTrue(
                t.aesthetic_declared,
                f"bundled theme {t.slug} doesn't declare [meta] aesthetic")
        self.assertEqual(counts, {"brutalist": 5, "modern": 11})
        self.assertEqual(len(bundled), 16)

    def test_filtered_list_matches_the_aesthetic(self) -> None:
        for preset_id in ("brutalist", "modern"):
            with self.subTest(preset=preset_id):
                rows = schema.theme_choices_for(preset_id)
                self.assertEqual(_slugs(rows), _by_aesthetic(preset_id))
                self.assertTrue(rows)

    def test_the_two_halves_cover_the_catalog(self) -> None:
        brutalist = set(_slugs(schema.theme_choices_for("brutalist")))
        modern = set(_slugs(schema.theme_choices_for("modern")))
        # only themes that claimed neither side are on both lists
        unclaimed = {slug for slug, t in _catalog().items()
                     if not t.aesthetic_declared}
        self.assertEqual(brutalist & modern, unclaimed)
        self.assertEqual(brutalist | modern, set(_catalog()))

    def test_show_all_returns_the_catalog_verbatim(self) -> None:
        self.assertEqual(
            schema.theme_choices_for("brutalist", show_all=True),
            schema.theme_choices(),
        )

    def test_no_personality_means_no_filter(self) -> None:
        # an unadopted pre-2.0 config has nothing to filter by; show everything
        for preset_id in ("", "haunted"):
            with self.subTest(preset=preset_id):
                self.assertEqual(schema.theme_choices_for(preset_id),
                                 schema.theme_choices())

    def test_keep_pins_cross_aesthetic_slugs_in_catalog_order(self) -> None:
        rows = schema.theme_choices_for("brutalist", keep=("nord",))
        self.assertIn("nord", _slugs(rows))
        expected = [
            slug for slug in _slugs(schema.theme_choices())
            if slug in set(_by_aesthetic("brutalist")) | {"nord"}
        ]
        self.assertEqual(_slugs(rows), expected)

    def test_keep_ignores_blanks_and_same_side_slugs(self) -> None:
        plain = schema.theme_choices_for("brutalist")
        self.assertEqual(
            schema.theme_choices_for("brutalist", keep=("", "gruvbox")),
            plain,
        )

    def test_labels_are_the_catalog_labels(self) -> None:
        catalog = dict(schema.theme_choices())
        for slug, label in schema.theme_choices_for("modern"):
            self.assertEqual(label, catalog[slug], slug)


# ---------- third-party themes that predate [meta] aesthetic ----------

class UndeclaredAestheticTests(unittest.TestCase):
    """Hand-installed themes predate ``[meta] aesthetic``. tide guesses a
    dialect from typography.mono so QSS has something to go on, but the
    picker must not act on a guess — a power user's own themes would
    look deleted."""

    def setUp(self) -> None:
        self.app = _app()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)

    def test_an_undeclared_theme_is_offered_to_both_sides(self) -> None:
        _install_theme(self, "handrolled")
        self.assertIn("handrolled", _catalog())
        for preset_id in ("brutalist", "modern"):
            with self.subTest(preset=preset_id):
                self.assertIn(
                    "handrolled",
                    _slugs(schema.theme_choices_for(preset_id)),
                    "a theme that claimed neither side was hidden")

    def test_an_undeclared_mono_theme_is_still_offered_to_both(self) -> None:
        # the mono guess renders it brutalist but is not a claim — a
        # modern user keeps seeing their theme
        _install_theme(self, "handrolled", mono=True)
        self.assertEqual(_catalog()["handrolled"].aesthetic, "brutalist")
        self.assertFalse(_catalog()["handrolled"].aesthetic_declared)
        self.assertIn("handrolled",
                      _slugs(schema.theme_choices_for("modern")))
        self.assertIn("handrolled",
                      _slugs(schema.theme_choices_for("brutalist")))

    def test_a_declared_third_party_theme_is_filtered_like_any_other(self) -> None:
        _install_theme(self, "declared", aesthetic="modern")
        self.assertTrue(_catalog()["declared"].aesthetic_declared)
        self.assertIn("declared", _slugs(schema.theme_choices_for("modern")))
        self.assertNotIn("declared",
                         _slugs(schema.theme_choices_for("brutalist")))

    def test_the_flag_survives_the_manager_override_copy(self) -> None:
        # _with_overrides rebuilds Theme field by field and feeds
        # current_effective() — a dropped flag silently reclassifies a
        # live theme as modern
        _install_theme(self, "handrolled")
        theme = _catalog()["handrolled"]
        self.assertFalse(theme.aesthetic_declared)
        mgr = theming.manager()
        mgr.set_user_override("radius", "7px")
        try:
            copied = mgr._with_overrides(theme)
            self.assertIsNot(copied, theme, "nothing was copied")
            self.assertEqual(copied.tokens.get("radius"), "7px")
            self.assertEqual(copied.aesthetic, theme.aesthetic)
            self.assertFalse(copied.aesthetic_declared)
        finally:
            mgr.set_user_override("radius", None)
            QTest.qWait(20)


# ---------- the new Settings field vs the P2 guarantees ----------

class EscapeHatchFieldTests(unittest.TestCase):
    def test_field_exists_and_defaults_off(self) -> None:
        self.assertFalse(Settings().theme_picker_show_all)

    def test_coverage_meta_test_still_holds(self) -> None:
        # a Settings field without a descriptor or written internal
        # reason fails by design
        missing, orphaned = schema.coverage_report()
        self.assertEqual(missing, [])
        self.assertEqual(orphaned, [])

    def test_the_field_has_a_descriptor_next_to_the_theme_picker(self) -> None:
        desc = schema.by_key()["theme_picker_show_all"]
        self.assertEqual(desc.kind, "bool")
        self.assertEqual((desc.tab, desc.section), ("appearance", "theme"))
        self.assertIsNone(desc.live, "it reshapes the picker, not the app")
        self.assertFalse(desc.preview)
        self.assertTrue(desc.tooltip.strip())
        keys = [d.key for d in schema.REGISTRY]
        self.assertEqual(keys[keys.index("theme") + 1], "theme_picker_show_all")

    def test_it_is_not_a_personality_stash_field(self) -> None:
        # a picker preference, not part of either look — it must not ride
        # the flip, and per_preset must say so
        self.assertNotIn("theme_picker_show_all", presets.STASH_FIELDS)
        self.assertFalse(schema.by_key()["theme_picker_show_all"].per_preset)
        self.assertEqual(schema.stash_membership_consistent(), [])

    def test_it_is_not_hidden_in_internal_fields(self) -> None:
        self.assertNotIn("theme_picker_show_all", schema.INTERNAL_FIELDS)

    def test_it_has_a_named_widget_attribute(self) -> None:
        from tide.ui.settings import _WIDGET_ATTRS
        self.assertEqual(_WIDGET_ATTRS["theme_picker_show_all"],
                         "theme_show_all_toggle")
        self.assertEqual(set(_WIDGET_ATTRS), {d.key for d in schema.REGISTRY})

    def test_it_round_trips_through_save_load(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="tide-themefilter-")
        self.addCleanup(tmp.cleanup)
        real = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(tmp.name) / "settings.toml"
        try:
            s = Settings()
            s.theme_picker_show_all = True
            settings_module.save(s)
            self.assertTrue(settings_module.load().theme_picker_show_all)
        finally:
            config.SETTINGS_FILE = real

    def test_old_config_files_load_with_it_off(self) -> None:
        # migration invariant: an upgrader's file has no such key
        known = {f.name for f in fields(Settings)}
        self.assertIn("theme_picker_show_all", known)
        tmp = tempfile.TemporaryDirectory(prefix="tide-themefilter-")
        self.addCleanup(tmp.cleanup)
        real = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(tmp.name) / "settings.toml"
        try:
            config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.SETTINGS_FILE.write_text(
                'theme = "gruvbox"\nvolume = 41\n', encoding="utf-8")
            back = settings_module.load()
            self.assertEqual(back.volume, 41)
            self.assertFalse(back.theme_picker_show_all)
        finally:
            config.SETTINGS_FILE = real


# ---------- the picker itself ----------

class _DialogCase(unittest.TestCase):
    """Settings writes stubbed (dialog tests must never clobber a config
    file), theme manager state restored, QSS pushes suppressed, dialogs
    closed + drained — the two real apply() restyles per test once made
    this file the second-slowest in the suite."""

    def setUp(self) -> None:
        self.app = _app()
        self.saved_field_calls: list[tuple[str, ...]] = []
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(
            settings_module, "save_fields",
            lambda s, *names: self.saved_field_calls.append(tuple(names)),
        ).start()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self._dialogs: list = []
        theming.manager().refresh()
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)

    def tearDown(self) -> None:
        # here rather than addCleanup: cleanups run AFTER tearDown, so a
        # dialog registered there would outlive the drain below and still
        # be repolished during the next test
        for dlg in self._dialogs:
            try:
                dlg.close()
                dlg.deleteLater()
            except RuntimeError:
                pass
        self._dialogs.clear()
        QTest.qWait(30)
        theming.manager().apply("brutalist-mono")
        QTest.qWait(20)

    def _dialog(self, s: Settings):
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(s)
        self._dialogs.append(dlg)
        return dlg

    def _rows(self, dlg) -> list[str]:
        combo = dlg.theme_picker
        return [combo.itemData(i) for i in range(combo.count())]


def _settings_for(preset_id: str, theme: str) -> Settings:
    s = Settings()
    s.first_launch_complete = True
    s.preset = preset_id
    s.preset_chosen = True
    s.theme = theme
    s.layout = "classic"
    presets.stash(s)
    return s


class PickerFilterTests(_DialogCase):
    def test_brutalist_sees_only_the_brutalist_themes(self) -> None:
        dlg = self._dialog(_settings_for("brutalist", "brutalist-mono"))
        self.assertEqual(self._rows(dlg), _by_aesthetic("brutalist"))
        self.assertEqual(dlg.theme_picker.currentData(), "brutalist-mono")

    def test_modern_sees_only_the_modern_themes(self) -> None:
        dlg = self._dialog(_settings_for("modern", "adaptive"))
        self.assertEqual(self._rows(dlg), _by_aesthetic("modern"))
        self.assertEqual(dlg.theme_picker.currentData(), "adaptive")

    def test_an_unadopted_config_still_sees_the_whole_catalog(self) -> None:
        # unadopted 1.x file: nothing to filter by — also keeps the
        # generated-dialog meta-test's combo-rows == schema-rows honest
        dlg = self._dialog(Settings())
        combo = dlg.theme_picker
        rows = [(combo.itemData(i), combo.itemText(i))
                for i in range(combo.count())]
        self.assertEqual(rows, list(schema.theme_choices()))

    def test_the_escape_hatch_is_a_checkbox_in_the_appearance_tab(self) -> None:
        dlg = self._dialog(_settings_for("brutalist", "brutalist-mono"))
        self.assertIsInstance(dlg.theme_show_all_toggle, QCheckBox)
        self.assertIs(dlg.theme_show_all_toggle,
                      dlg.widget_for("theme_picker_show_all"))
        self.assertFalse(dlg.theme_show_all_toggle.isChecked())

    def test_show_all_reveals_every_theme_and_keeps_the_pick(self) -> None:
        s = _settings_for("brutalist", "gruvbox")
        dlg = self._dialog(s)
        self.assertEqual(len(self._rows(dlg)), len(_by_aesthetic("brutalist")))
        dlg.theme_show_all_toggle.setChecked(True)
        self.assertEqual(sorted(self._rows(dlg)), sorted(_catalog()))
        self.assertEqual(len(self._rows(dlg)), len(_catalog()))
        self.assertEqual(dlg.theme_picker.currentData(), "gruvbox",
                         "widening the list must not move the selection")

    def test_unticking_narrows_back_down(self) -> None:
        dlg = self._dialog(_settings_for("modern", "adaptive"))
        dlg.theme_show_all_toggle.setChecked(True)
        dlg.theme_show_all_toggle.setChecked(False)
        self.assertEqual(self._rows(dlg), _by_aesthetic("modern"))
        self.assertEqual(dlg.theme_picker.currentData(), "adaptive")

    def test_a_cross_pick_survives_unticking(self) -> None:
        # the held theme must survive the untick, or the next accept
        # silently writes a different theme
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        dlg.theme_show_all_toggle.setChecked(True)
        idx = dlg.theme_picker.findData("nord")
        self.assertGreaterEqual(idx, 0)
        dlg.theme_picker.setCurrentIndex(idx)
        dlg.theme_show_all_toggle.setChecked(False)
        self.assertIn("nord", self._rows(dlg))
        self.assertEqual(dlg.theme_picker.currentData(), "nord")
        # …and the brutalist half is back around it
        self.assertEqual([r for r in self._rows(dlg) if r != "nord"],
                         _by_aesthetic("brutalist"))

    def test_a_stray_current_theme_is_always_listed(self) -> None:
        # hand-edited state: modern personality, brutalist theme
        s = _settings_for("modern", "gruvbox")
        dlg = self._dialog(s)
        self.assertIn("gruvbox", self._rows(dlg))
        self.assertEqual(dlg.theme_picker.currentData(), "gruvbox")
        dlg._on_save()
        self.assertEqual(s.theme, "gruvbox")
        self.assertEqual(self.saved_field_calls, [],
                         "an untouched accept must not rewrite the theme")

    def test_opening_with_show_all_saved_starts_wide(self) -> None:
        s = _settings_for("brutalist", "brutalist-mono")
        s.theme_picker_show_all = True
        dlg = self._dialog(s)
        self.assertTrue(dlg.theme_show_all_toggle.isChecked())
        self.assertEqual(len(self._rows(dlg)), len(_catalog()))

    def test_toggling_previews_nothing_and_writes_nothing(self) -> None:
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        mgr = theming.manager()
        with mock.patch.object(mgr, "apply", wraps=mgr.apply) as spy:
            dlg.theme_show_all_toggle.setChecked(True)
            dlg.theme_show_all_toggle.setChecked(False)
            self.assertEqual(spy.call_count, 0,
                             "reshaping the list is not a theme pick")
        self.assertEqual(mgr.current().slug, "brutalist-mono")
        self.assertEqual(s.theme, "brutalist-mono")
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(self.saved_field_calls, [])

    def test_the_preference_persists_on_accept_and_nothing_else(self) -> None:
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        dlg.theme_show_all_toggle.setChecked(True)
        dlg._on_save()
        self.assertTrue(s.theme_picker_show_all)
        self.assertEqual(self.saved_field_calls, [("theme_picker_show_all",)])
        self.assertEqual(dlg.changed_keys(), ("theme_picker_show_all",))
        self.assertEqual(s.theme, "brutalist-mono")

    def test_cancel_leaves_the_preference_alone(self) -> None:
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        dlg.theme_show_all_toggle.setChecked(True)
        dlg._on_cancel()
        self.assertFalse(s.theme_picker_show_all)
        self.assertEqual(self.saved_field_calls, [])

    def test_a_rebase_onto_the_other_personality_drops_the_old_theme(self) -> None:
        # the flip's contract: re-base the snapshot and clear the pending
        # diff, THEN rebuild — the outgoing theme must not linger
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        self.assertEqual(self._rows(dlg), _by_aesthetic("brutalist"))
        s.preset = "modern"
        s.theme = "adaptive"
        dlg._pending.clear()
        dlg._initial_theme = s.theme
        dlg.refresh_theme_choices()
        self.assertEqual(self._rows(dlg), _by_aesthetic("modern"))
        self.assertEqual(dlg.theme_picker.currentData(), "adaptive")

    def test_the_kept_slug_is_the_pending_pick_not_the_raw_combo(self) -> None:
        # the kept slug is read from the pending diff — the one place
        # that survives a re-base honestly
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        dlg.theme_show_all_toggle.setChecked(True)
        idx = dlg.theme_picker.findData("nord")
        dlg.theme_picker.setCurrentIndex(idx)
        self.assertEqual(dlg._pending.get("theme"), "nord")
        dlg.refresh_theme_choices()
        self.assertIn("nord", self._rows(dlg))

    def test_a_saved_theme_joins_the_list_even_cross_aesthetic(self) -> None:
        # the theme editor's reconcile: the slug it just wrote must be
        # listed and staged whichever side of the filter it landed on
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        dlg._on_theme_saved("nord")
        self.assertEqual(dlg.theme_picker.currentData(), "nord")
        self.assertEqual(dlg._pending.get("theme"), "nord")
        self.assertEqual(s.theme, "brutalist-mono",
                         "staging must not touch the settings object")

    def test_a_saved_slug_that_isnt_real_stages_nothing(self) -> None:
        s = _settings_for("brutalist", "brutalist-mono")
        dlg = self._dialog(s)
        dlg._on_theme_saved("not-a-theme")
        self.assertEqual(dlg.theme_picker.currentData(), "brutalist-mono")
        self.assertNotIn("theme", dlg._pending)


# ---------- cross-personality picks route through the existing path ----------

class _WindowCase(unittest.TestCase):
    """Real-MainWindow harness (the test_preset_flip pattern)."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-themefilter-")
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
        QTest.qWait(30)
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


class CrossPersonalityPickTests(_WindowCase):
    def _open_and_pick(self, w, slug: str, show_all: bool):
        """Drive the REAL _do_open_settings route with a scripted exec:
        tick the escape hatch, pick ``slug``, accept."""
        from tide.ui.settings import SettingsDialog
        seen: dict = {}

        def fake_exec(dlg):
            dlg.theme_show_all_toggle.setChecked(show_all)
            seen["rows"] = [dlg.theme_picker.itemData(i)
                            for i in range(dlg.theme_picker.count())]
            idx = dlg.theme_picker.findData(slug)
            seen["found"] = idx
            if idx < 0:
                dlg.reject()
                return dlg.result()
            dlg.theme_picker.setCurrentIndex(idx)
            dlg._on_save()
            # the dialog itself must never flip the personality — the
            # window's reconcile owns that (do not duplicate the logic)
            seen["preset_at_accept"] = dlg._settings.preset
            return dlg.result()

        with mock.patch.object(SettingsDialog, "exec", fake_exec):
            w._do_open_settings()
        return seen

    def test_show_all_off_hides_the_other_side_entirely(self) -> None:
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(settings_module, "save_fields",
                          lambda s, *names: None).start()
        s = _settings_for("brutalist", "brutalist-mono")
        w = self._make_window(s)
        QTest.qWait(30)
        seen = self._open_and_pick(w, "nord", show_all=False)
        self.assertEqual(seen["found"], -1)
        self.assertEqual(seen["rows"], _by_aesthetic("brutalist"))
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "brutalist-mono")

    def test_cross_pick_with_show_all_flips_through_switch_preset(self) -> None:
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(settings_module, "save_fields",
                          lambda s, *names: None).start()
        s = _settings_for("brutalist", "brutalist-mono")
        w = self._make_window(s)
        QTest.qWait(30)
        with mock.patch.object(w, "switch_preset",
                               wraps=w.switch_preset) as spy:
            seen = self._open_and_pick(w, "nord", show_all=True)
            QTest.qWait(30)
            self.assertEqual(
                spy.call_count, 1,
                "exactly one flip — the reconcile path owns it, and "
                "nothing duplicates it")
            self.assertEqual(spy.call_args.args, ("modern",))
        self.assertEqual(seen["preset_at_accept"], "brutalist",
                         "the dialog must not flip the personality itself")
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "nord")
        self.assertEqual(theming.manager().current().slug, "nord")
        # the outgoing personality kept the theme it wore
        self.assertEqual(s.preset_state["brutalist"]["theme"],
                         "brutalist-mono")

    def test_same_side_pick_never_flips(self) -> None:
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(settings_module, "save_fields",
                          lambda s, *names: None).start()
        s = _settings_for("brutalist", "brutalist-mono")
        w = self._make_window(s)
        QTest.qWait(30)
        with mock.patch.object(w, "switch_preset",
                               wraps=w.switch_preset) as spy:
            self._open_and_pick(w, "gruvbox", show_all=False)
            QTest.qWait(30)
            self.assertEqual(spy.call_count, 0)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "gruvbox")

    def test_the_live_cross_theme_route_is_untouched(self) -> None:
        # no dialog: applying a modern theme while wearing brutalist still
        # flips, via _on_theme_changed → _maybe_apply_theme_slot_prefs
        # (deferred)
        mock.patch.object(settings_module, "save", lambda s: None).start()
        mock.patch.object(settings_module, "save_fields",
                          lambda s, *names: None).start()
        s = _settings_for("brutalist", "brutalist-mono")
        w = self._make_window(s)
        QTest.qWait(30)
        theming.manager().apply("nord")
        QTest.qWait(60)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "nord")


if __name__ == "__main__":
    unittest.main()
