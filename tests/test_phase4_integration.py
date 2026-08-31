"""v2.0 phase 4 — the whole "two tides" story, end to end.

The three builders each pinned their own contract (tests/test_chooser.py,
test_chooser_routes.py, test_theme_filtering.py). This file drives the
seams BETWEEN them: the real ChooserDialog, opened by the real route, on
a real Settings file, followed by a real MainWindow — the sequence a
user actually lives through.

Four stories:

  1. a 1.5 upgrader launches into 2.0, is offered the chooser, picks
     modern, and every layer follows: theme, layout slots, motion
     (intensity AND dialect), corners, backdrop, sounds, glyphs,
     thumbnails. Their brutalist look is stashed, not lost. The next
     launch never asks again.
  2. the same upgrader dismisses instead. The migration invariant's last
     mile: nothing they can see moves — not one settings field, not the
     theme, not the layout, not the motion — and the only byte on disk
     that changes is the "asked" stamp. No second nag.
  3. a fresh install walks the wizard and lands ON the picked
     personality with that personality's slot variants live — the P1
     first-run bug (a modern pick rendering on brutalist blocks
     forever) stays fixed, and the standalone chooser stays out of it.
  4. a settings re-pick, both directions, keeps each side's own
     customizations: theme, slots, corners and glyphs all come back
     exactly as they were left.

Plus the two seams that only exist because three agents built into the
same surfaces at once: the panes preview the SHIPPED product rather than
the user's personal copy of one (glyph overrides are per-personality, so
the live resolver would have leaked one side into the other's pitch), and
the appearance tab's personality section (4B) sits directly on top of the
personality-aware theme picker (4C) — a flip has to rebuild the theme
rows before it re-selects, or the combo answers for the wrong tide.

Everything here uses the REAL dialog (driven by synthetic clicks, the
way a user drives it) rather than a stand-in — the point of a stitch
test is that the pieces actually meet.

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
    """The REAL chooser, driven the way a user drives it.

    ``exec()`` is the only thing stubbed, and only because a blocking
    modal loop can't be clicked from the same thread: it shows the
    dialog, delivers a real mouse press/release on a real pane (or a
    real Esc), and spins until the dialog resolves itself. Everything
    under test — the panes, the deferred commit, the signal, the
    dismissal — is the shipping code.
    """

    answer = ""          # "" = walk away (esc)
    built: list = []     # one entry per construction: (parent, initial)
    last = None          # the live instance, for inspecting the panes
    pane_themes: dict = {}   # preset id -> the theme slug the pane wore

    def __init__(self, parent=None, *, initial: str = "brutalist") -> None:
        super().__init__(parent, initial=initial)
        type(self).built.append((parent, initial))
        type(self).last = self

    def exec(self) -> int:
        # Snapshot what the panes resolved to WHILE they're alive — proof
        # the route opened us after the theme registry was live (run()
        # keeps the chooser between _bootstrap_preset and MainWindow for
        # exactly this reason), and the caller deletes us on the way out.
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
        # The commit is deferred one event-loop turn on purpose (rule 3);
        # give it several.
        for _ in range(30):
            QTest.qWait(10)
            if not self.isVisible():
                break
        return (QDialog.DialogCode.Accepted if self.choice()
                else QDialog.DialogCode.Rejected)


class _AutoWizard:
    """Stands in for OnboardingDialog — the wizard's own steps are
    covered in test_chooser_routes/test_onboarding_defaults; what this
    file cares about is what app.py does with the answer."""

    DialogCode = QDialog.DialogCode
    result: OnboardingResult | None = None

    def exec(self):
        return QDialog.DialogCode.Accepted

    def result_data(self):
        return type(self).result


class _Phase4Case(unittest.TestCase):
    """test_preset_flip's hygiene, which every window-building file in
    this suite inherits: hermetic settings file, app-wide setStyleSheet
    suppressed (a leaked window from an earlier file repolishes on every
    real push), deleteLater+drain teardown, and every global the routes
    move — theme, layout, motion, scale, glyphs, radius — put back.
    """

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
        """A settings file as v1.5 left it.

        v1.5 had no preset/preset_chosen/preset_state keys at all, which
        is byte-for-byte what a 2.0 Settings() default writes — so a
        plain save with the 2.0 fields untouched loads back identically
        to the real thing (pinned below).
        """
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
        """What run() builds after the chooser has had its say: glyph
        registry seeded, window constructed, settings attached, the
        startup apply_preset_visuals, then the UI-sound player bound
        (which app.py follows with a second pack apply)."""
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
        """Everything the user can SEE, read off the live managers plus
        the settings object — the comparison the migration invariant is
        actually about."""
        mgr = theming.manager()
        current = mgr.current()
        return {
            "theme": getattr(current, "slug", ""),
            "layout": layout_module.manager().current().slug,
            "slots": dict(layout_module.manager().current().slots),
            "intensity": motion_module.intensity().value,
            "profile": motion_module.profile(),
            # The sticky @radius the corner style pushes (test_theme_editor's
            # accessor — the manager exposes no reader).
            "radius": mgr._user_overrides.get("radius"),
            "glyph_play": glyphs.glyph("play"),
            "fields": dataclasses.asdict(settings),
        }

    def _visible_fields(self, settings: Settings) -> dict:
        """Every settings field except the per-personality memory.

        ``preset_state`` is bookkeeping, not look, and it legitimately
        differs between two routes that took a different number of round
        trips: the incoming side keeps its seed entry until something
        stashes it, so brutalist→modern and brutalist→modern→brutalist→
        modern land on the same screen with different stash depth.
        """
        return {k: v for k, v in dataclasses.asdict(settings).items()
                if k != "preset_state"}


# ---------------------------------------------------------------------------
# story 1 — the 1.5 upgrader who picks modern
# ---------------------------------------------------------------------------


class UpgraderPicksModernTest(_Phase4Case):
    def test_a_1_5_config_loads_as_never_asked(self) -> None:
        # The premise of the whole update route: pre-2.0 keys are absent,
        # which loads as preset "" / preset_chosen False.
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
        # Silent adoption filed them under the personality their theme
        # belongs to — and changed nothing they can see.
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
        # Both panes previewed a REAL theme, which they can only do if
        # the route opened them after the registry bootstrap.
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
        # First visit wears modern's theme slots, not the ones brutalist
        # was rendering with (the pre-window twin of switch_preset's
        # seeding).
        self.assertEqual(live.layout_overrides.get("progress"), "bar")
        self.assertEqual(live.layout_overrides.get("volume"), "spring")
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "bar")
        # Glyphs are chrome and chrome is personality: the brutalist ▶
        # swap did not follow them across.
        self.assertEqual(live.glyph_overrides, {})
        # Shared fields are shared — a personality never touches volume.
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
        # Ordering contract: run() puts the chooser between the bootstrap
        # and MainWindow, so the very first frame is already modern —
        # never a brutalist frame that restyles a beat later.
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


# ---------------------------------------------------------------------------
# story 2 — the same upgrader who walks away
# ---------------------------------------------------------------------------


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
        # The adoption still stands: they are a brutalist user who has
        # now been asked once.
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
        # The offer is a nicety; the player is the product. A broken
        # chooser must degrade to "not offered", never to "won't start" —
        # and must not stamp the question as asked, since it wasn't.
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
        # And the next launch offers it properly.
        with self._chooser("modern"):
            self.assertEqual(app_module.run_chooser_if_needed(live), "modern")

    def test_they_can_still_find_it_later(self) -> None:
        # Dismissing is "not now", not "never": the settings door is the
        # advertised way back, and it works on the dismissed state.
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


# ---------------------------------------------------------------------------
# story 3 — the fresh install
# ---------------------------------------------------------------------------


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
        # The P1 first-run bug: the wizard's aesthetic flip happened with
        # no window listening, so a modern install rendered on brutalist
        # block variants forever.
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
        # run()'s real call order: wizard, then the update route. A user
        # who just answered the question in the wizard must not be asked
        # it again thirty milliseconds later.
        s = self._run_wizard("modern", "synthwave")
        with self._chooser("brutalist"):
            self.assertEqual(app_module.run_chooser_if_needed(s), "")
        self.assertEqual(_AutoChooser.built, [])
        self.assertEqual(s.preset, "modern")

    def test_the_first_visit_to_the_other_side_starts_clean(self) -> None:
        # The wizard drops its pre-wizard adoption scaffolding, so the
        # first flip to the unchosen personality gets that side's builtin
        # defaults — not a phantom snapshot of the installer's state.
        s = self._run_wizard("modern", "synthwave")
        self.assertNotIn("brutalist", s.preset_state)
        self.assertTrue(app_module.commit_personality_choice(s, "brutalist"))
        brutalist = presets.builtin("brutalist")
        self.assertEqual(s.theme, brutalist.theme)
        self.assertEqual(s.motion, "off")
        self.assertEqual(s.layout_overrides.get("progress"), "blocks")


# ---------------------------------------------------------------------------
# story 4 — the settings re-pick, both directions
# ---------------------------------------------------------------------------


class RepickKeepsBothSidesTest(_Phase4Case):
    """A flip is a round trip. Whatever each side was wearing when you
    left it is what you find when you come back — theme, slots, corners
    and glyphs, in both directions."""

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

        # The theme picker re-based onto modern's half of the catalog
        # (agent C's filter, agent B's re-base) — pick from what's there.
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
        # A glyph tweak on this side too, so both halves of the stash are
        # under test.
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

        # Both sides are on disk, each remembering itself.
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertEqual(disk.preset_state["brutalist"]["theme"], "storm")
        self.assertEqual(disk.preset_state["brutalist"]["glyph_overrides"],
                         {"play": "»"})

    def test_the_dialog_survives_a_double_flip(self) -> None:
        # Flip, flip back, accept: the dialog must be describing what is
        # actually on screen, or accept writes the wrong personality's
        # look over the right one.
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
        # The flip owns the look and nothing else — a shared field must
        # survive both legs of the trip.
        s = self._brutalist_user()
        s.volume = 31
        s.report_plays = True
        w, dlg, _before = self._open(s)
        self._flip_via_picker(dlg, "modern")
        self._flip_via_picker(dlg, "brutalist")
        self.assertEqual(s.volume, 31)
        self.assertTrue(s.report_plays)

    def test_the_chooser_door_and_the_picker_door_agree(self) -> None:
        # Two controls, one commit path: whichever the user reaches for,
        # the result is the same personality in the same state.
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

        # Now the same trip via the quick-flip picker.
        self._flip_via_picker(dlg, "brutalist")
        self._flip_via_picker(dlg, "modern")
        self.assertEqual(self._visible_fields(s), via_chooser)
        picker_look = self._look(s)
        for key in ("theme", "layout", "slots", "intensity", "profile",
                    "radius", "glyph_play"):
            self.assertEqual(chooser_look[key], picker_look[key],
                             f"the two doors disagree about {key}")


# ---------------------------------------------------------------------------
# the panes sell the PRODUCTS, not the user's copy of one
# ---------------------------------------------------------------------------


class PreviewShowsTheShippedProductTest(_Phase4Case):
    """Glyph overrides are a STASH_FIELD — chrome is personality. The
    live resolver would therefore dress BOTH panes in whichever side's
    swaps happen to be loaded, so a modern preview would show the ▶ the
    user retyped in brutalist last week. The panes read the shipped pack.
    """

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
        # One vocabulary: the preview can't drift from what the transport
        # actually draws.
        text = self._pane_text("brutalist")
        for key in ("prev", "play", "next"):
            self.assertIn(f"[{glyphs.DEFAULT_PACK[key]}]", text)


# ---------------------------------------------------------------------------
# the appearance tab's own seam: the personality section (4B) sitting on
# top of the personality-aware theme picker (4C)
# ---------------------------------------------------------------------------


class AppearanceTabSeamTest(_Phase4Case):
    """The quick flip and the theme filter share one widget.

    A flip re-bases the whole dialog, and the theme rows have to be
    rebuilt for the incoming personality BEFORE the re-populate re-selects
    — otherwise the combo still holds the outgoing catalog and the
    re-populate silently falls back to a theme nobody picked. Both halves
    landed in parallel; this is where they meet.
    """

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
        # The selection is the theme that actually landed, not a fallback.
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
        # Browse a theme (which previews live), then flip personality.
        # The preview was never a commit, so the personality the user is
        # leaving must be stashed wearing the theme it actually had.
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
        # And going back brings storm, not the browsed-past gruvbox.
        self._flip_via_picker(dlg, "brutalist")
        self.assertEqual(s.theme, "storm")
        self.assertEqual(theming.manager().current().slug, "storm")

    def test_cancel_after_a_flip_leaves_the_flip_standing(self) -> None:
        # Rule 8 in the mirror: cancel reverts previews, never commits.
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
        # Whichever tide you're wearing, the filter never hides a theme
        # from the person who owns it.
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
