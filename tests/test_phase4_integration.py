"""v2.0 phase 4, stitched end to end: the real ChooserDialog, opened by
the real route, on a real Settings file, followed by a real MainWindow.
Scenarios: the 1.5 upgrader who picks modern (every layer follows, the
old look is stashed, no re-ask), the one who dismisses (only the
"asked" stamp may change), the fresh install landing on the picked
personality's slots, the settings re-pick round trip, and the two
cross-agent seams (shipped-pack pane previews; flip vs theme rows —
each pinned on its class below).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from tide import app as app_module
from tide import config, glyphs, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui import chooser as chooser_module
from tide.ui import motion as motion_module
from tide.ui import scale as scale_module
from tide.ui.onboarding import OnboardingResult
from tide.ui_sounds import UiSoundPlayer


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _AutoChooser(chooser_module.ChooserDialog):
    """The real chooser with only ``exec()`` stubbed — a blocking modal
    loop can't be clicked from its own thread. It shows the dialog,
    delivers a real click on a pane (or Esc), and spins until the dialog
    resolves; everything else under test is the shipping code."""

    answer = ""          # "" = walk away (esc)
    built: list = []     # one entry per construction: (parent, initial)
    last = None          # the live instance, for inspecting the panes
    pane_themes: dict = {}   # preset id -> the theme slug the pane wore

    def __init__(self, parent=None, *, initial: str = "brutalist") -> None:
        super().__init__(parent, initial=initial)
        type(self).built.append((parent, initial))
        type(self).last = self

    def exec(self) -> int:
        # Snapshot pane themes while the panes are alive — the caller
        # deletes the dialog on the way out. A real slug proves the route
        # opened us after the theme-registry bootstrap.
        type(self).pane_themes = {
            preset_id: getattr(pane.theme, "slug", "")
            for preset_id, pane in self.panes().items()
        }
        self.show()
        QTest.qWait(10)
        want = type(self).answer
        if want:
            QTest.mouseClick(self.pane(want), Qt.LeftButton,
                             pos=QPoint(8, 8))
        else:
            QTest.keyClick(self, Qt.Key_Escape)
        # commit is deferred one event-loop turn (rule 3); give it several
        for _ in range(30):
            QTest.qWait(10)
            if not self.isVisible():
                break
        return (QDialog.DialogCode.Accepted if self.choice()
                else QDialog.DialogCode.Rejected)


class _AutoWizard:
    """OnboardingDialog stand-in — the wizard's own steps are covered in
    test_chooser_routes; this file cares what app.py does with the answer."""

    DialogCode = QDialog.DialogCode
    result: OnboardingResult | None = None

    def exec(self):
        return QDialog.DialogCode.Accepted

    def result_data(self):
        return type(self).result


class _Phase4Case(unittest.TestCase):
    """test_preset_flip's hygiene: hermetic settings file, app-wide
    setStyleSheet suppressed (a leaked window from an earlier file
    repolishes on every real push), deleteLater+drain teardown, and every
    global the routes move put back."""

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-phase4-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        self._orig_intensity = motion_module._user_intensity
        _AutoChooser.built = []
        _AutoChooser.answer = ""
        _AutoChooser.last = None
        _AutoChooser.pane_themes = {}
        self._windows: list = []
        self._widgets: list = []

    def tearDown(self) -> None:
        for w in self._windows:
            try:
                theming.manager().theme_changed.disconnect(
                    w._on_theme_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        for w in self._widgets:
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        self._windows.clear()
        self._widgets.clear()
        QTest.qWait(30)
        config.SETTINGS_FILE = self._orig_settings_file
        motion_module.set_intensity(self._orig_intensity)
        motion_module.bind_preset(None)
        glyphs.set_overrides({})
        scale_module.set_factor("normal")
        mgr = theming.manager()
        mgr.set_user_override("radius", None)
        theming.set_case_override("")
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0,
                         case="")
        layout_module.manager().apply("classic", {})
        QTest.qWait(30)
        for _ in range(10):
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    # ---------------------------------------------------------- fixtures

    def _chooser(self, answer: str):
        """Patch in the auto-driven REAL dialog. ``answer`` "" = dismiss."""
        _AutoChooser.answer = answer
        return mock.patch.object(chooser_module, "ChooserDialog",
                                 _AutoChooser)

    def _write_1_5_config(self) -> None:
        """A settings file as v1.5 left it: no preset/preset_chosen/
        preset_state keys, which is byte-for-byte what a 2.0 Settings()
        with those fields untouched writes (pinned below)."""
        s = Settings()
        s.first_launch_complete = True
        s.theme = "gruvbox"                   # [meta] aesthetic = brutalist
        s.layout = "classic"
        s.layout_overrides = {"progress": "blocks", "volume": "blocks"}
        s.motion = "off"
        s.corner_style = "sharp"
        s.nav_icon_set = "off"
        s.show_thumbnails = "on"
        s.adaptive_accent = False
        s.adaptive_background = False
        s.glyph_overrides = {"play": "»"}     # their own transport tweak
        s.volume = 43                         # a shared field, not a look
        settings_module.save(s)

    def _boot(self) -> Settings:
        """run()'s startup prologue, in order: load, then the preset
        bootstrap that silently adopts a pre-2.0 config."""
        live = settings_module.load()
        app_module._bootstrap_preset(live)
        return live

    def _make_window(self, settings: Settings):
        """run()'s post-chooser build: glyph registry seeded, window
        built, settings attached, apply_preset_visuals, then the UI-sound
        player bound and the pack applied (app.py's order)."""
        app_module._boot_glyph_overrides(settings)
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        w = MainWindow(LocalSource(), router)
        self._windows.append(w)
        w._settings = settings
        w.apply_preset_visuals()
        w.ui_sounds = UiSoundPlayer(parent=w)
        w.ui_sounds.set_enabled(bool(settings.ui_sounds_enabled))
        w._apply_sound_pack()
        return w

    def _settings_dialog(self, settings: Settings, window=None):
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(settings, parent=window)
        self._widgets.append(dlg)
        return dlg

    def _flip_via_picker(self, dlg, preset_id: str) -> None:
        idx = dlg.personality_picker.findData(preset_id)
        self.assertGreaterEqual(idx, 0)
        dlg.personality_picker.setCurrentIndex(idx)
        dlg.personality_picker.activated.emit(idx)   # a user activation
        QTest.qWait(50)                              # the deferred flip

    def _look(self, settings: Settings) -> dict:
        """Everything the user can see, read off the live managers plus
        the settings object — what the migration invariant compares."""
        mgr = theming.manager()
        current = mgr.current()
        return {
            "theme": getattr(current, "slug", ""),
            "layout": layout_module.manager().current().slug,
            "slots": dict(layout_module.manager().current().slots),
            "intensity": motion_module.intensity().value,
            "profile": motion_module.profile(),
            # the sticky @radius the corner style pushes; the manager
            # exposes no reader
            "radius": mgr._user_overrides.get("radius"),
            "glyph_play": glyphs.glyph("play"),
            "fields": dataclasses.asdict(settings),
        }

    def _visible_fields(self, settings: Settings) -> dict:
        """All settings fields except ``preset_state`` — bookkeeping, not
        look, and it legitimately differs between routes that took a
        different number of flips (the incoming side keeps its seed
        entry until something stashes it)."""
        return {k: v for k, v in dataclasses.asdict(settings).items()
                if k != "preset_state"}


# ---------- story 1: the 1.5 upgrader who picks modern ----------


class UpgraderPicksModernTest(_Phase4Case):
    def test_a_1_5_config_loads_as_never_asked(self) -> None:
        self._write_1_5_config()
        raw = config.SETTINGS_FILE.read_text()
        loaded = settings_module.load()
        self.assertEqual(loaded.preset, "")
        self.assertFalse(loaded.preset_chosen)
        self.assertEqual(loaded.preset_state, {})
        self.assertIn("gruvbox", raw)

    def test_the_whole_upgrade_into_modern(self) -> None:
        self._write_1_5_config()
        live = self._boot()
        self.assertEqual(live.preset, "brutalist")
        self.assertFalse(live.preset_chosen)
        self.assertEqual(theming.manager().current().slug, "gruvbox")

        with self._chooser("modern"):
            choice = app_module.run_chooser_if_needed(live)
        self.assertEqual(choice, "modern")
        self.assertEqual(len(_AutoChooser.built), 1)
        parent, initial = _AutoChooser.built[0]
        self.assertIsNone(parent, "the update route runs pre-window")
        self.assertEqual(initial, "brutalist",
                         "the chooser opened on somebody else's tide")
        self.assertEqual(_AutoChooser.pane_themes,
                         {"brutalist": presets.builtin("brutalist").theme,
                          "modern": presets.builtin("modern").theme})

        # ---- every layer followed the pick, before any window exists
        modern = presets.builtin("modern")
        self.assertEqual(live.preset, "modern")
        self.assertTrue(live.preset_chosen)
        self.assertEqual(live.theme, modern.theme)
        self.assertEqual(theming.manager().current().slug, modern.theme)
        self.assertEqual(live.motion, "full")
        self.assertEqual(motion_module.intensity().value, "full")
        self.assertEqual(motion_module.profile(), "springy",
                         "modern got brutalist's motion dialect")
        self.assertEqual(live.corner_style, "soft")
        self.assertIsNotNone(
            theming.manager()._user_overrides.get("radius"),
            "soft corners never reached the @radius token")
        self.assertTrue(live.adaptive_background)
        self.assertEqual(live.adaptive_background_style, "liquid")
        self.assertTrue(live.adaptive_accent)
        self.assertEqual(live.show_thumbnails, "theme")
        # first visit wears modern's theme slots (pre-window twin of
        # switch_preset's seeding)
        self.assertEqual(live.layout_overrides.get("progress"), "bar")
        self.assertEqual(live.layout_overrides.get("volume"), "spring")
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "bar")
        self.assertEqual(live.glyph_overrides, {})
        self.assertEqual(live.volume, 43)

        # ---- the window run() builds next wears all of it
        w = self._make_window(live)
        QTest.qWait(20)
        self.assertEqual(w._slot_progress, "bar")
        self.assertEqual(w._slot_volume, "spring")
        self.assertEqual(w.ui_sounds.pack, "modern",
                         "the personality's sound pack never landed")
        self.assertNotEqual(glyphs.glyph("play"), "»",
                            "brutalist's glyph override leaked into modern")

        # ---- their old look is stashed, not lost
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertTrue(disk.preset_chosen)
        stash = disk.preset_state["brutalist"]
        self.assertEqual(stash["theme"], "gruvbox")
        self.assertEqual(stash["layout_overrides"],
                         {"progress": "blocks", "volume": "blocks"})
        self.assertEqual(stash["glyph_overrides"], {"play": "»"})
        self.assertEqual(stash["corner_style"], "sharp")

        # ---- relaunch: the question is never asked twice
        _AutoChooser.built = []
        relaunched = settings_module.load()
        with self._chooser("brutalist"):
            self.assertEqual(
                app_module.run_chooser_if_needed(relaunched), "")
        self.assertEqual(_AutoChooser.built, [],
                         "the chooser came back on the second launch")
        self.assertEqual(relaunched.preset, "modern")

    def test_the_pick_is_live_before_the_window_is_built(self) -> None:
        # ordering contract: run() puts the chooser between bootstrap and
        # MainWindow, so the first frame is already modern
        self._write_1_5_config()
        live = self._boot()
        with self._chooser("modern"), \
                mock.patch.object(app_module, "MainWindow") as window_cls:
            app_module.run_chooser_if_needed(live)
            self.assertEqual(window_cls.call_count, 0)
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "bar")


# ---------- story 2: the same upgrader who dismisses ----------


class UpgraderDismissesTest(_Phase4Case):
    def test_dismissal_changes_nothing_they_can_see(self) -> None:
        self._write_1_5_config()
        live = self._boot()
        before = self._look(live)

        with self._chooser(""):              # esc
            self.assertEqual(app_module.run_chooser_if_needed(live), "")
        self.assertEqual(len(_AutoChooser.built), 1,
                         "the chooser never opened")
        after = self._look(live)

        moved = {k for k in before["fields"]
                 if before["fields"][k] != after["fields"][k]}
        self.assertEqual(moved, {"preset_chosen"},
                         "a dismissal moved a field the user can see")
        for key in ("theme", "layout", "slots", "intensity", "profile",
                    "radius", "glyph_play"):
            self.assertEqual(before[key], after[key], f"{key} moved")
        self.assertEqual(live.preset, "brutalist")
        self.assertTrue(live.preset_chosen)

    def test_the_window_after_a_dismissal_is_their_1_5_window(self) -> None:
        self._write_1_5_config()
        live = self._boot()
        with self._chooser(""):
            app_module.run_chooser_if_needed(live)
        w = self._make_window(live)
        QTest.qWait(20)
        self.assertEqual(w._slot_progress, "blocks")
        self.assertEqual(w._slot_volume, "blocks")
        self.assertEqual(w.ui_sounds.pack, "default")
        self.assertEqual(glyphs.glyph("play"), "»",
                         "their own glyph override was dropped")
        self.assertEqual(theming.manager().current().slug, "gruvbox")

    def test_only_the_stamp_reaches_the_disk(self) -> None:
        self._write_1_5_config()
        live = self._boot()
        on_disk_before = dataclasses.asdict(settings_module.load())
        with self._chooser(""):
            app_module.run_chooser_if_needed(live)
        on_disk_after = dataclasses.asdict(settings_module.load())
        moved = {k for k in on_disk_before
                 if on_disk_before[k] != on_disk_after[k]}
        self.assertEqual(moved, {"preset_chosen"})

    def test_no_second_nag_and_no_third(self) -> None:
        self._write_1_5_config()
        live = self._boot()
        with self._chooser(""):
            app_module.run_chooser_if_needed(live)
            for _ in range(2):
                relaunched = settings_module.load()
                self.assertEqual(
                    app_module.run_chooser_if_needed(relaunched), "")
        self.assertEqual(len(_AutoChooser.built), 1)

    def test_a_chooser_that_cannot_build_never_blocks_the_launch(self) -> None:
        # a broken chooser degrades to "not offered", never to "won't
        # start" — and must not stamp the question as asked
        self._write_1_5_config()
        live = self._boot()
        before = self._look(live)
        boom = mock.Mock(side_effect=RuntimeError("no screen for you"))
        with mock.patch.object(chooser_module, "ChooserDialog", boom):
            self.assertEqual(app_module.run_chooser_if_needed(live), "")
        self.assertFalse(live.preset_chosen,
                         "a chooser that never opened stamped the question")
        after = self._look(live)
        for key in ("theme", "layout", "slots", "intensity", "fields"):
            self.assertEqual(before[key], after[key])
        with self._chooser("modern"):
            self.assertEqual(app_module.run_chooser_if_needed(live), "modern")

    def test_they_can_still_find_it_later(self) -> None:
        self._write_1_5_config()
        live = self._boot()
        with self._chooser(""):
            app_module.run_chooser_if_needed(live)
        w = self._make_window(live)
        QTest.qWait(20)
        dlg = self._settings_dialog(live, w)
        with self._chooser("modern"):
            dlg.chooser_btn.click()
            QTest.qWait(60)
        self.assertEqual(live.preset, "modern")
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self.assertEqual(dlg.personality_picker.currentData(), "modern")


# ---------- story 3: the fresh install ----------


class FreshInstallTest(_Phase4Case):
    def _run_wizard(self, aesthetic: str, theme_slug: str) -> Settings:
        from tide.ui import onboarding as onboarding_module
        _AutoWizard.result = OnboardingResult(
            completed=True, aesthetic=aesthetic, theme_slug=theme_slug,
            adaptive_accent=(aesthetic == "modern"),
            adaptive_background=(aesthetic == "modern"),
            motion="full" if aesthetic == "modern" else "off",
            sources_enabled={"local": True},
            active_source="local",
        )
        s = Settings()                       # no config file at all
        app_module._bootstrap_preset(s)
        with mock.patch.object(onboarding_module, "OnboardingDialog",
                               _AutoWizard):
            self.assertTrue(app_module.run_onboarding_if_needed(s))
        return s

    def test_a_modern_pick_lands_on_modern_slots(self) -> None:
        # the P1 first-run bug: the wizard's flip ran with no window
        # listening, so a modern install rendered on brutalist blocks forever
        s = self._run_wizard("modern", "synthwave")
        self.assertEqual(s.preset, "modern")
        self.assertTrue(s.preset_chosen)
        self.assertEqual(s.theme, "synthwave")
        self.assertEqual(theming.manager().current().slug, "synthwave")
        self.assertEqual(s.layout_overrides.get("progress"), "bar")
        self.assertEqual(s.layout_overrides.get("volume"), "knob")
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "bar")
        self.assertEqual(motion_module.profile(), "springy")

        w = self._make_window(s)
        QTest.qWait(20)
        self.assertEqual(w._slot_progress, "bar")
        self.assertEqual(w._slot_volume, "knob")
        self.assertEqual(w.ui_sounds.pack, "modern")

    def test_a_brutalist_pick_stays_brutalist(self) -> None:
        s = self._run_wizard("brutalist", "terminal-green")
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "terminal-green")
        self.assertEqual(s.layout_overrides.get("progress"), "blocks")
        self.assertEqual(motion_module.intensity().value, "off")
        self.assertEqual(motion_module.profile(), "mechanical")
        w = self._make_window(s)
        QTest.qWait(20)
        self.assertEqual(w._slot_progress, "blocks")
        self.assertEqual(w.ui_sounds.pack, "default")

    def test_the_wizard_answer_survives_the_relaunch(self) -> None:
        self._run_wizard("modern", "synthwave")
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertTrue(disk.preset_chosen)
        self.assertEqual(disk.preset_state["modern"]["theme"], "synthwave")
        self.assertTrue(disk.first_launch_complete)

    def test_the_standalone_chooser_never_piles_on(self) -> None:
        s = self._run_wizard("modern", "synthwave")
        with self._chooser("brutalist"):
            self.assertEqual(app_module.run_chooser_if_needed(s), "")
        self.assertEqual(_AutoChooser.built, [])
        self.assertEqual(s.preset, "modern")

    def test_the_first_visit_to_the_other_side_starts_clean(self) -> None:
        # the wizard drops the pre-wizard adoption entry — the first flip
        # to the other side gets builtin defaults, not a phantom snapshot
        s = self._run_wizard("modern", "synthwave")
        self.assertNotIn("brutalist", s.preset_state)
        self.assertTrue(app_module.commit_personality_choice(s, "brutalist"))
        brutalist = presets.builtin("brutalist")
        self.assertEqual(s.theme, brutalist.theme)
        self.assertEqual(s.motion, "off")
        self.assertEqual(s.layout_overrides.get("progress"), "blocks")


# ---------- story 4: the settings re-pick, both directions ----------


class RepickKeepsBothSidesTest(_Phase4Case):
    """Each side comes back wearing what it was left in — theme, slots,
    corners, glyphs, both directions."""

    def _brutalist_user(self) -> Settings:
        s = Settings()
        s.first_launch_complete = True
        s.preset_chosen = True
        s.preset = "brutalist"
        s.theme = "storm"
        s.layout = "classic"
        s.layout_overrides = {"progress": "dotted", "volume": "blocks"}
        s.motion = "off"
        s.corner_style = "sharp"
        s.glyph_overrides = {"play": "»"}
        presets.stash(s)
        settings_module.save(s)
        return s

    def _open(self, settings: Settings):
        import copy
        theming.manager().apply(settings.theme)
        layout_module.manager().apply(settings.layout,
                                      dict(settings.layout_overrides))
        w = self._make_window(settings)
        QTest.qWait(20)
        before = copy.copy(settings)     # what _do_open_settings holds
        dlg = self._settings_dialog(settings, w)
        return w, dlg, before

    def test_a_round_trip_returns_both_looks_intact(self) -> None:
        s = self._brutalist_user()
        w, dlg, before = self._open(s)

        # ---- over to modern, and customize it there
        self._flip_via_picker(dlg, "modern")
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        self.assertEqual(w._slot_progress, "bar")
        self.assertEqual(w.ui_sounds.pack, "modern")

        rows = [dlg.theme_picker.itemData(i)
                for i in range(dlg.theme_picker.count())]
        self.assertIn("undertow", rows)
        self.assertNotIn("storm", rows,
                         "the outgoing personality's theme stayed listed")
        dlg.theme_picker.setCurrentIndex(dlg.theme_picker.findData("undertow"))
        QTest.qWait(20)
        dlg._on_save()
        w._reconcile_preset_after_dialog(before, s)
        QTest.qWait(40)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "undertow")
        s.glyph_overrides = {"play": "▶︎"}
        presets.stash(s)

        # ---- back to brutalist: their old look returns verbatim
        self.assertTrue(
            app_module.commit_personality_choice(s, "brutalist", window=w))
        QTest.qWait(40)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "storm")
        self.assertEqual(s.corner_style, "sharp")
        self.assertEqual(s.motion, "off")
        self.assertEqual(s.layout_overrides,
                         {"progress": "dotted", "volume": "blocks"},
                         "a re-pick reset the user's slot tweaks")
        self.assertEqual(s.glyph_overrides, {"play": "»"})
        self.assertEqual(glyphs.glyph("play"), "»")
        self.assertEqual(theming.manager().current().slug, "storm")
        self.assertEqual(w._slot_progress, "dotted")
        self.assertEqual(w.ui_sounds.pack, "default")
        self.assertEqual(motion_module.profile(), "mechanical")

        # ---- and over to modern again: so does modern's
        self.assertTrue(
            app_module.commit_personality_choice(s, "modern", window=w))
        QTest.qWait(40)
        self.assertEqual(s.theme, "undertow",
                         "modern forgot the theme it was left wearing")
        self.assertEqual(s.glyph_overrides, {"play": "▶︎"})
        self.assertEqual(glyphs.glyph("play"), "▶︎")
        self.assertEqual(s.layout_overrides.get("volume"), "spring")
        self.assertEqual(theming.manager().current().slug, "undertow")
        self.assertEqual(w.ui_sounds.pack, "modern")
        self.assertEqual(motion_module.profile(), "springy")

        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertEqual(disk.preset_state["brutalist"]["theme"], "storm")
        self.assertEqual(disk.preset_state["brutalist"]["glyph_overrides"],
                         {"play": "»"})

    def test_the_dialog_survives_a_double_flip(self) -> None:
        # flip, flip back, accept: the dialog must describe what's on
        # screen, or accept writes the wrong personality's look
        s = self._brutalist_user()
        w, dlg, _before = self._open(s)
        self._flip_via_picker(dlg, "modern")
        self._flip_via_picker(dlg, "brutalist")
        self.assertEqual(dlg.personality_picker.currentData(), "brutalist")
        self.assertEqual(dlg.theme_picker.currentData(), "storm")
        dlg._on_save()
        QTest.qWait(20)
        self.assertNotIn("theme", dlg.changed_keys())
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "storm")
        self.assertEqual(s.layout_overrides,
                         {"progress": "dotted", "volume": "blocks"})
        self.assertEqual(settings_module.load().preset, "brutalist")

    def test_a_shared_setting_is_untouched_by_either_direction(self) -> None:
        s = self._brutalist_user()
        s.volume = 31
        s.report_plays = True
        w, dlg, _before = self._open(s)
        self._flip_via_picker(dlg, "modern")
        self._flip_via_picker(dlg, "brutalist")
        self.assertEqual(s.volume, 31)
        self.assertTrue(s.report_plays)

    def test_the_chooser_door_and_the_picker_door_agree(self) -> None:
        s = self._brutalist_user()
        w, dlg, _before = self._open(s)
        with self._chooser("modern"):
            dlg.chooser_btn.click()
            self.assertEqual(_AutoChooser.built, [],
                             "the modal opened inside the click handler")
            QTest.qWait(80)
        self.assertEqual(len(_AutoChooser.built), 1)
        parent, initial = _AutoChooser.built[0]
        self.assertIs(parent, dlg)
        self.assertEqual(initial, "brutalist")
        via_chooser = self._visible_fields(s)
        chooser_look = self._look(s)

        self._flip_via_picker(dlg, "brutalist")
        self._flip_via_picker(dlg, "modern")
        self.assertEqual(self._visible_fields(s), via_chooser)
        picker_look = self._look(s)
        for key in ("theme", "layout", "slots", "intensity", "profile",
                    "radius", "glyph_play"):
            self.assertEqual(chooser_look[key], picker_look[key],
                             f"the two doors disagree about {key}")


# ---------- the panes preview the shipped pack ----------


class PreviewShowsTheShippedProductTest(_Phase4Case):
    """Glyph overrides are per-personality, so the live resolver would
    dress BOTH panes in whichever side's swaps are loaded. The panes read
    the shipped pack instead."""

    def _pane_text(self, preset_id: str) -> str:
        pane = chooser_module.PersonalityPane(preset_id)
        self._widgets.append(pane)
        return " ".join(lab.text() for lab in pane.findChildren(QLabel))

    def test_a_users_glyph_swap_never_reaches_a_pane(self) -> None:
        glyphs.set_overrides({"play": "PLAY", "prev": "<<", "next": ">>"})
        for preset_id in chooser_module.PANE_ORDER:
            with self.subTest(preset_id):
                text = self._pane_text(preset_id)
                self.assertIn(glyphs.DEFAULT_PACK["prev"], text)
                self.assertIn(glyphs.DEFAULT_PACK["next"], text)
                self.assertNotIn("PLAY", text)
                self.assertNotIn("<<", text)

    def test_the_mock_speaks_the_apps_own_vocabulary(self) -> None:
        text = self._pane_text("brutalist")
        for key in ("prev", "play", "next"):
            self.assertIn(f"[{glyphs.DEFAULT_PACK[key]}]", text)


# ---------- appearance tab: personality section (4B) over theme picker (4C) ----------


class AppearanceTabSeamTest(_Phase4Case):
    """The quick flip and the theme filter share one widget: a flip must
    rebuild the theme rows for the incoming personality BEFORE the
    re-populate re-selects, or the combo still holds the outgoing catalog
    and silently falls back to a theme nobody picked."""

    def _user(self, preset_id: str, theme: str) -> Settings:
        s = Settings()
        s.first_launch_complete = True
        s.preset_chosen = True
        s.preset = preset_id
        s.theme = theme
        s.layout = "classic"
        s.motion = "off" if preset_id == "brutalist" else "full"
        s.corner_style = "sharp" if preset_id == "brutalist" else "soft"
        presets.stash(s)
        settings_module.save(s)
        theming.manager().apply(theme)
        return s

    def _dialog_for(self, s: Settings):
        w = self._make_window(s)
        QTest.qWait(20)
        return w, self._settings_dialog(s, w)

    def _rows(self, dlg) -> list:
        return [dlg.theme_picker.itemData(i)
                for i in range(dlg.theme_picker.count())]

    def test_a_flip_rebuilds_the_rows_before_reselecting(self) -> None:
        s = self._user("brutalist", "storm")
        w, dlg = self._dialog_for(s)
        self.assertEqual(len(self._rows(dlg)), 5)
        self._flip_via_picker(dlg, "modern")
        rows = self._rows(dlg)
        self.assertEqual(len(rows), 11)
        self.assertNotIn("storm", rows)
        self.assertEqual(dlg.theme_picker.currentData(), s.theme)
        self.assertEqual(s.theme, presets.builtin("modern").theme)

    def test_the_show_all_tick_survives_a_flip(self) -> None:
        s = self._user("brutalist", "storm")
        w, dlg = self._dialog_for(s)
        dlg.theme_show_all_toggle.setChecked(True)
        QTest.qWait(10)
        self.assertEqual(len(self._rows(dlg)), 16)
        self._flip_via_picker(dlg, "modern")
        self.assertTrue(dlg.theme_show_all_toggle.isChecked(),
                        "the flip ate the escape hatch")
        self.assertEqual(len(self._rows(dlg)), 16,
                         "the rows narrowed behind a ticked show-all")
        dlg._on_save()
        QTest.qWait(20)
        self.assertTrue(s.theme_picker_show_all)
        self.assertTrue(settings_module.load().theme_picker_show_all)

    def test_an_abandoned_theme_preview_never_reaches_the_stash(self) -> None:
        # browsing previews live but never commits — the outgoing side
        # must be stashed wearing the theme it actually had
        s = self._user("brutalist", "storm")
        w, dlg = self._dialog_for(s)
        dlg.theme_picker.setCurrentIndex(dlg.theme_picker.findData("gruvbox"))
        QTest.qWait(20)
        self.assertEqual(theming.manager().current().slug, "gruvbox",
                         "the picker stopped previewing")
        self._flip_via_picker(dlg, "modern")
        self.assertEqual(s.preset_state["brutalist"]["theme"], "storm",
                         "a preview got persisted by the flip")
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self._flip_via_picker(dlg, "brutalist")
        self.assertEqual(s.theme, "storm")
        self.assertEqual(theming.manager().current().slug, "storm")

    def test_cancel_after_a_flip_leaves_the_flip_standing(self) -> None:
        s = self._user("brutalist", "storm")
        w, dlg = self._dialog_for(s)
        dlg.theme_picker.setCurrentIndex(dlg.theme_picker.findData("gruvbox"))
        QTest.qWait(20)
        self._flip_via_picker(dlg, "modern")
        dlg.reject()
        QTest.qWait(40)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self.assertEqual(settings_module.load().preset, "modern")

    def test_the_two_halves_of_the_catalog_add_up(self) -> None:
        from tide.ui import settings_schema
        whole = {slug for slug, _ in settings_schema.theme_choices()}
        halves = set()
        for preset_id in ("brutalist", "modern"):
            s = Settings()
            s.preset = preset_id
            s.theme = presets.builtin(preset_id).theme
            rows = set(self._rows(self._settings_dialog(s)))
            self.assertNotIn("", rows)
            halves |= rows
        self.assertEqual(halves, whole)


if __name__ == "__main__":
    unittest.main()
