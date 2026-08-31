"""v2.0 phase 4 — the three doors to "choose your tide".

Every route funnels through app.commit_personality_choice, so what's
pinned here is the routing and what each door is allowed to change:

  (b1) fresh install — the wizard's aesthetic step IS the chooser's
       panes now; a click there SELECTS, and only app.py's post-wizard
       handoff commits (pre-window, before MainWindow exists);
  (b2) update into 2.0 — the chooser is offered exactly once, and a
       DISMISSAL moves nothing on disk but the "asked" stamp (the
       migration invariant's last mile);
  (b3) settings re-pick — the appearance tab's personality section flips
       live through window.switch_preset, keeps each personality's own
       customizations, and re-bases the open dialog so neither accept
       nor cancel can undo the commit.

Run offscreen:
  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from tide import app as app_module
from tide import config, presets, theming
from tide import layout as layout_module
from tide import settings as settings_module
from tide.playback import MpvBackend, PlaybackRouter
from tide.settings import Settings
from tide.sources.local import LocalSource
from tide.ui import chooser as chooser_module
from tide.ui import motion as motion_module
from tide.ui import scale as scale_module
from tide.ui.onboarding import OnboardingResult, _AestheticStep


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _FakeChooser:
    """Stands in for ChooserDialog: records how it was opened, answers
    instantly. Class attributes are configured per test."""

    answer = ""             # "" = dismissed
    opened: list = []       # (parent, initial) per construction

    def __init__(self, parent=None, *, initial: str = "") -> None:
        type(self).opened.append((parent, initial))

    def exec(self) -> int:
        return (QDialog.DialogCode.Accepted if type(self).answer
                else QDialog.DialogCode.Rejected)

    def choice(self) -> str:
        return type(self).answer

    def deleteLater(self) -> None:
        pass


class _FakeWizard:
    """Stands in for OnboardingDialog (test_preset_bootstrap's shape)."""

    DialogCode = QDialog.DialogCode
    accept = True
    result: OnboardingResult | None = None

    def __init__(self) -> None:
        pass

    def exec(self):
        return (QDialog.DialogCode.Accepted if type(self).accept
                else QDialog.DialogCode.Rejected)

    def result_data(self):
        return type(self).result


class _RouteCase(unittest.TestCase):
    """Hermetic settings file, real managers, no app-wide QSS pushes.

    The QSS spy is test_restyle_coalesce's pattern: this deep into the
    suite every real push repolishes every window an earlier file leaked.
    Manager state stays fully real — these routes are only interesting
    because they drive the actual theming/layout/motion managers.
    """

    def setUp(self) -> None:
        self.app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-routes-")
        self.addCleanup(self._tmp.cleanup)
        self._orig_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        theming.manager().refresh()
        layout_module.manager().refresh()
        mock.patch.object(self.app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)
        _FakeChooser.opened = []
        _FakeChooser.answer = ""
        self._widgets: list = []

    def tearDown(self) -> None:
        for w in self._widgets:
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        self._widgets.clear()
        QTest.qWait(30)
        config.SETTINGS_FILE = self._orig_settings_file
        mgr = theming.manager()
        mgr.set_user_override("radius", None)
        theming.set_case_override("")
        mgr.apply_bundle(slug="brutalist-mono", font_family="", font_size=0,
                         case="")
        layout_module.manager().apply("classic", {})
        motion_module.set_intensity("lite")
        scale_module.set_factor("normal")
        QTest.qWait(30)
        for _ in range(10):
            if not mgr._restyle_scheduled:
                break
            QTest.qWait(10)

    # ---- helpers

    def _upgrader(self) -> Settings:
        """A 1.6 user mid-migration: adopted, never asked."""
        s = Settings()
        s.first_launch_complete = True
        s.theme = "gruvbox"                     # aesthetic=brutalist
        s.motion = "off"
        s.corner_style = "rounded"
        s.layout_overrides = {"progress": "blocks"}
        settings_module.save(s)
        live = settings_module.load()
        app_module._bootstrap_preset(live)      # the real startup order
        return live

    def _fake_chooser(self, answer: str):
        _FakeChooser.answer = answer
        return mock.patch.object(chooser_module, "ChooserDialog", _FakeChooser)


# ---------------------------------------------------------------------------
# (b1) fresh install — the wizard's aesthetic step
# ---------------------------------------------------------------------------


class FreshInstallRouteTest(_RouteCase):
    def _step(self) -> _AestheticStep:
        step = _AestheticStep()
        self._widgets.append(step)
        return step

    def test_the_step_hosts_the_real_chooser_panes(self) -> None:
        # Not a fork of the design: literally the chooser's widget, so
        # the pitch can only ever be written once.
        step = self._step()
        self.assertEqual(sorted(step._panes), ["brutalist", "modern"])
        for preset_id, pane in step._panes.items():
            self.assertIsInstance(pane, chooser_module.PersonalityPane)
            self.assertEqual(pane.preset_id, preset_id)
            # Real theme tokens, not a hardcoded swatch.
            self.assertEqual(pane.theme.slug,
                             presets.builtin(preset_id).theme)
            self.assertTrue(pane._compact, "the wizard canvas needs compact")

    def test_the_step_still_fits_the_wizard(self) -> None:
        from tide.ui.onboarding import OnboardingDialog
        dlg = OnboardingDialog()
        self._widgets.append(dlg)
        dlg.show()
        QTest.qWait(20)
        self.assertLessEqual(dlg.minimumSizeHint().width(), 720)
        self.assertLessEqual(dlg.minimumSizeHint().height(), 600)
        self.assertEqual((dlg.width(), dlg.height()), (720, 600))

    def test_a_click_selects_and_carries_into_the_result(self) -> None:
        step = self._step()
        step.resize(660, 430)
        step.show()
        QTest.qWait(10)
        self.assertTrue(step._panes["brutalist"].is_selected())
        QTest.mouseClick(step._panes["modern"], Qt.LeftButton,
                         pos=QPoint(6, 6))
        self.assertEqual(step._choice, "modern")
        self.assertTrue(step._panes["modern"].is_selected())
        self.assertFalse(step._panes["brutalist"].is_selected())
        result = OnboardingResult()
        step.apply_to(result)
        self.assertEqual(result.aesthetic, "modern")

    def test_the_pane_button_says_what_it_does_in_this_host(self) -> None:
        # The pane's button COMMITS in the standalone dialog, so there it
        # says "choose". Here it only moves the selection ring — the
        # wizard's own [next] advances — so a button labelled "choose"
        # would look broken the moment it was pressed.
        step = self._step()
        brutalist = step._panes["brutalist"]
        modern = step._panes["modern"]
        self.assertEqual(brutalist._button.text(), "[picked]")  # default pick
        self.assertEqual(modern._button.text(), "this one")
        modern.clicked.emit("modern")
        self.assertEqual(modern._button.text(), "picked")
        self.assertEqual(brutalist._button.text(), "[this one]")

    def test_the_standalone_dialog_keeps_the_commit_wording(self) -> None:
        dlg = chooser_module.ChooserDialog()
        self._widgets.append(dlg)
        self.assertEqual(dlg.pane("brutalist")._button.text(), "[choose]")
        self.assertEqual(dlg.pane("modern")._button.text(), "choose")
        # Arrow-key focus is not a decision: the label must not move.
        dlg._move_focus(1)
        self.assertTrue(dlg.pane("modern").is_selected())
        self.assertEqual(dlg.pane("modern")._button.text(), "choose")

    def test_going_back_shows_the_carried_pick(self) -> None:
        step = self._step()
        step.on_enter(OnboardingResult(aesthetic="modern"))
        self.assertEqual(step._choice, "modern")
        self.assertTrue(step._panes["modern"].is_selected())

    def test_the_step_commits_nothing(self) -> None:
        # Selecting in the wizard is not a personality: only the
        # post-wizard handoff commits (tools emit, callers persist).
        with mock.patch.object(settings_module, "save") as save, \
                mock.patch.object(settings_module, "save_fields") as fields:
            step = self._step()
            step.show()
            QTest.qWait(10)
            step._panes["modern"].clicked.emit("modern")
            QTest.qWait(20)
            self.assertEqual(save.call_count, 0)
            self.assertEqual(fields.call_count, 0)
        self.assertFalse(config.SETTINGS_FILE.exists())

    def test_wizard_pick_is_persisted_and_applied_before_any_window(self) -> None:
        from tide.ui import onboarding as onboarding_module
        _FakeWizard.accept = True
        _FakeWizard.result = OnboardingResult(
            completed=True, aesthetic="modern", theme_slug="nord",
            adaptive_background=True, motion="full",
        )
        s = Settings()
        app_module._bootstrap_preset(s)
        with mock.patch.object(onboarding_module, "OnboardingDialog",
                               _FakeWizard), \
                mock.patch.object(app_module, "MainWindow") as window_cls:
            self.assertTrue(app_module.run_onboarding_if_needed(s))
            self.assertEqual(window_cls.call_count, 0,
                             "the wizard route built a window")
        self.assertEqual(s.preset, "modern")
        self.assertTrue(s.preset_chosen)
        self.assertEqual(s.theme, "nord")
        # Live on the managers before MainWindow is even a thought.
        self.assertEqual(theming.manager().current().slug, "nord")
        self.assertEqual(motion_module.intensity().value, "full")
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertTrue(disk.preset_chosen)
        self.assertEqual(disk.preset_state["modern"]["theme"], "nord")
        # run()'s actual next line: the update route must stay quiet for
        # someone who just answered the question in the wizard.
        with self._fake_chooser("brutalist"):
            self.assertEqual(app_module.run_chooser_if_needed(s), "")
        self.assertEqual(_FakeChooser.opened, [])

    def test_a_fresh_install_never_sees_the_update_route(self) -> None:
        # The wizard owns first launch; the standalone chooser must not
        # pile on top of it.
        s = Settings()                          # first_launch_complete False
        with self._fake_chooser("modern"):
            self.assertEqual(app_module.run_chooser_if_needed(s), "")
        self.assertEqual(_FakeChooser.opened, [])
        self.assertFalse(s.preset_chosen)


# ---------------------------------------------------------------------------
# (b2) update into 2.0 — the standalone chooser, once
# ---------------------------------------------------------------------------


class UpdateRouteTest(_RouteCase):
    def test_chooser_runs_once_and_applies_the_pick(self) -> None:
        s = self._upgrader()
        self.assertFalse(s.preset_chosen)
        with self._fake_chooser("modern"):
            self.assertEqual(app_module.run_chooser_if_needed(s), "modern")
            self.assertEqual(len(_FakeChooser.opened), 1)
            # Pre-window: no parent, and preselected on the personality
            # the silent adoption filed them under.
            parent, initial = _FakeChooser.opened[0]
            self.assertIsNone(parent)
            self.assertEqual(initial, "brutalist")
            # "Second launch": reload from disk and run the route again.
            again = settings_module.load()
            self.assertTrue(again.preset_chosen)
            self.assertEqual(app_module.run_chooser_if_needed(again), "")
            self.assertEqual(len(_FakeChooser.opened), 1,
                             "the chooser came back for a second helping")
        self.assertEqual(s.preset, "modern")
        self.assertTrue(s.preset_chosen)
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertTrue(disk.preset_chosen)
        # The brutalist look they arrived with is stashed, not lost.
        self.assertEqual(disk.preset_state["brutalist"]["theme"], "gruvbox")
        self.assertEqual(
            disk.preset_state["brutalist"]["layout_overrides"],
            {"progress": "blocks"})

    def test_first_visit_wears_the_new_personality_slots(self) -> None:
        # Pre-window parity with switch_preset's first-visit seeding —
        # without it a chooser pick made before MainWindow exists would
        # wear the outgoing personality's slot variants forever.
        s = self._upgrader()
        with self._fake_chooser("modern"):
            app_module.run_chooser_if_needed(s)
        self.assertEqual(s.layout_overrides.get("progress"), "bar")
        self.assertEqual(s.layout_overrides.get("volume"), "spring")
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "bar")

    def test_dismissal_stamps_the_question_and_nothing_else(self) -> None:
        s = self._upgrader()
        before = dataclasses.asdict(s)
        theme_before = theming.manager().current().slug
        with self._fake_chooser(""):            # esc / close-X
            self.assertEqual(app_module.run_chooser_if_needed(s), "")
            self.assertEqual(len(_FakeChooser.opened), 1)
        after = dataclasses.asdict(s)
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"preset_chosen"},
                         "a dismissal changed something the user can see")
        self.assertTrue(s.preset_chosen)
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(theming.manager().current().slug, theme_before)
        disk = settings_module.load()
        self.assertTrue(disk.preset_chosen)
        self.assertEqual(disk.theme, "gruvbox")
        self.assertEqual(disk.corner_style, "rounded")
        self.assertEqual(disk.layout_overrides, {"progress": "blocks"})

    def test_dismissal_is_not_asked_again(self) -> None:
        s = self._upgrader()
        with self._fake_chooser(""):
            app_module.run_chooser_if_needed(s)
            again = settings_module.load()
            app_module.run_chooser_if_needed(again)
        self.assertEqual(len(_FakeChooser.opened), 1)

    def test_an_explicit_picker_is_never_offered_again(self) -> None:
        s = self._upgrader()
        s.preset_chosen = True
        with self._fake_chooser("modern"):
            self.assertEqual(app_module.run_chooser_if_needed(s), "")
        self.assertEqual(_FakeChooser.opened, [])


# ---------------------------------------------------------------------------
# the shared commit helper
# ---------------------------------------------------------------------------


class CommitHelperTest(_RouteCase):
    def test_unknown_id_changes_nothing(self) -> None:
        s = self._upgrader()
        before = dataclasses.asdict(s)
        self.assertFalse(
            app_module.commit_personality_choice(s, "vaporwave"))
        self.assertEqual(dataclasses.asdict(s), before)

    def test_commit_stamps_and_persists_in_one_write(self) -> None:
        s = self._upgrader()
        with mock.patch.object(settings_module, "save_fields",
                               wraps=settings_module.save_fields) as saves:
            self.assertTrue(
                app_module.commit_personality_choice(s, "modern"))
            self.assertEqual(saves.call_count, 1,
                             "the commit cost more than one persist")
            self.assertIn("preset_chosen", saves.call_args.args)
        self.assertTrue(settings_module.load().preset_chosen)

    def test_re_picking_the_active_personality_keeps_its_tweaks(self) -> None:
        # Re-picking must not reset anything — the same-id apply_preset
        # path refreshes the stash from the live fields, it never
        # restores over them. "Re-pick" means the question has already
        # been answered; the FIRST answer is the case below.
        s = self._upgrader()
        s.preset_chosen = True
        self.assertTrue(app_module.commit_personality_choice(s, "brutalist"))
        self.assertEqual(s.theme, "gruvbox")
        self.assertEqual(s.corner_style, "rounded")
        self.assertEqual(s.motion, "off")
        self.assertEqual(s.layout_overrides, {"progress": "blocks"})
        self.assertTrue(s.preset_chosen)

    def test_first_answer_keeps_are_all_real_stash_fields(self) -> None:
        # A typo in FIRST_ANSWER_KEEPS silently resets a field the user
        # brought with them, and nothing else would notice.
        self.assertTrue(
            set(presets.FIRST_ANSWER_KEEPS) <= set(presets.STASH_FIELDS),
            set(presets.FIRST_ANSWER_KEEPS) - set(presets.STASH_FIELDS))
        self.assertNotIn("motion", presets.FIRST_ANSWER_KEEPS,
                         "rule 9: brutalist's stillness is not negotiable")
        with self.assertRaises(KeyError):
            presets.claim_builtin(Settings(), "vaporwave")

    def test_the_first_answer_delivers_the_product_it_advertises(self) -> None:
        # The migration adopts an upgrader into a personality before the
        # chooser opens, so picking that same pane used to be a dead
        # button: they clicked the side that says "nothing moves. ever."
        # and kept every rounded corner. A FIRST answer claims the
        # builtin feel; the look they brought (theme, layout, slots)
        # survives it.
        s = Settings()
        s.first_launch_complete = True
        s.theme = "gruvbox"                     # aesthetic=brutalist
        s.layout_overrides = {"progress": "blocks"}
        s.corner_style = "rounded"
        s.motion = "full"
        s.adaptive_background = True
        s.nav_icon_set = "classic"
        settings_module.save(s)
        s = settings_module.load()
        app_module._bootstrap_preset(s)         # the real startup order
        self.assertFalse(s.preset_chosen)
        self.assertEqual(s.preset, "brutalist")
        self.assertTrue(app_module.commit_personality_choice(s, "brutalist"))
        self.assertEqual(s.corner_style, "sharp")
        self.assertEqual(s.motion, "off")
        self.assertFalse(s.adaptive_background)
        self.assertEqual(s.nav_icon_set, "off")
        # …and the look they arrived with is untouched.
        self.assertEqual(s.theme, "gruvbox")
        self.assertEqual(s.layout, "classic")
        self.assertEqual(s.layout_overrides, {"progress": "blocks"})
        # Live on the managers, and stashed as brutalist's own state.
        self.assertEqual(motion_module.intensity().value, "off")
        self.assertEqual(s.preset_state["brutalist"]["corner_style"], "sharp")
        disk = settings_module.load()
        self.assertEqual(disk.motion, "off")
        self.assertEqual(disk.theme, "gruvbox")

    def test_the_first_answer_on_the_modern_side_wakes_it_up(self) -> None:
        # The mirror: a 1.5 user whose theme reads modern but who has
        # motion off and no backdrop picks the pane promising "backdrops
        # that breathe with the track" — and gets them.
        s = Settings()
        s.first_launch_complete = True
        s.theme = "adaptive"                    # aesthetic=modern
        s.motion = "off"
        s.corner_style = "sharp"
        s.adaptive_background = False
        s.nav_icon_set = "off"
        settings_module.save(s)
        live = settings_module.load()
        app_module._bootstrap_preset(live)
        self.assertEqual(live.preset, "modern")
        self.assertTrue(app_module.commit_personality_choice(live, "modern"))
        self.assertEqual(live.motion, "full")
        self.assertEqual(live.corner_style, "soft")
        self.assertTrue(live.adaptive_background)
        self.assertEqual(live.nav_icon_set, "classic")
        self.assertEqual(live.theme, "adaptive")

    def test_a_second_answer_is_a_re_pick_not_a_reset(self) -> None:
        # Answer once (claims the builtin), tweak, re-pick: the tweak
        # stands. The claim is gated on the stamp, not on the id.
        s = self._upgrader()
        app_module.commit_personality_choice(s, "brutalist")
        s.corner_style = "rounded"
        s.motion = "lite"
        app_module.commit_personality_choice(s, "brutalist")
        self.assertEqual(s.corner_style, "rounded")
        self.assertEqual(s.motion, "lite")

    def test_a_dismissal_then_a_settings_pick_is_a_re_pick(self) -> None:
        # Dismissing stamps the question as ASKED (they kept their 1.x
        # look on purpose), so a later pick of the same personality from
        # settings must not retroactively reset it.
        s = self._upgrader()
        with self._fake_chooser(""):
            app_module.run_chooser_if_needed(s)
        self.assertEqual(s.corner_style, "rounded")
        app_module.commit_personality_choice(s, "brutalist")
        self.assertEqual(s.corner_style, "rounded")
        self.assertEqual(s.theme, "gruvbox")

    def test_the_wizards_own_answers_are_never_claimed_over(self) -> None:
        # Route (b1) stamps preset_chosen itself before handing off, so
        # the handoff is a re-pick by construction — a wizard user who
        # asked for motion=off on the modern side keeps it.
        from tide.ui import onboarding as onboarding_module
        _FakeWizard.accept = True
        _FakeWizard.result = OnboardingResult(
            completed=True, aesthetic="modern", theme_slug="nord",
            adaptive_background=False, motion="off",
        )
        s = Settings()
        app_module._bootstrap_preset(s)
        with mock.patch.object(onboarding_module, "OnboardingDialog",
                               _FakeWizard):
            self.assertTrue(app_module.run_onboarding_if_needed(s))
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.motion, "off")
        self.assertFalse(s.adaptive_background)
        self.assertEqual(s.theme, "nord")


# ---------------------------------------------------------------------------
# --theme vs the boot side-quests
# ---------------------------------------------------------------------------


class CliThemeOverrideTest(_RouteCase):
    """``--theme`` is a run-only override. The chooser commits a theme
    bundle, so the override has to survive the commit."""

    def test_a_chooser_pick_does_not_eat_the_cli_theme(self) -> None:
        s = self._upgrader()
        app_module._bootstrap_preset(s, cli_theme="nord")
        self.assertEqual(theming.manager().current().slug, "nord")
        with self._fake_chooser("modern"):
            app_module.run_chooser_if_needed(s)
        # The commit legitimately re-applied modern's bundle over it…
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        # …and run()'s next line puts the override back.
        app_module._reassert_cli_theme("nord")
        self.assertEqual(theming.manager().current().slug, "nord")
        # Still run-only: nothing about it reached settings or the stash.
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        disk = settings_module.load()
        self.assertEqual(disk.theme, presets.builtin("modern").theme)
        for preset_id, stashed in disk.preset_state.items():
            self.assertNotEqual(stashed.get("theme"), "nord", preset_id)

    def test_a_dismissal_leaves_the_override_where_it_was(self) -> None:
        s = self._upgrader()
        app_module._bootstrap_preset(s, cli_theme="nord")
        with self._fake_chooser(""):
            app_module.run_chooser_if_needed(s)
        app_module._reassert_cli_theme("nord")
        self.assertEqual(theming.manager().current().slug, "nord")
        self.assertEqual(s.theme, "gruvbox")

    def test_no_cli_theme_costs_nothing(self) -> None:
        mgr = theming.manager()
        with mock.patch.object(mgr, "apply") as spy:
            app_module._reassert_cli_theme(None)
            app_module._reassert_cli_theme("")
            self.assertEqual(spy.call_count, 0)


# ---------------------------------------------------------------------------
# (b3) settings re-pick
# ---------------------------------------------------------------------------


class SettingsRepickTest(_RouteCase):
    """The appearance tab's personality section, against a real window."""

    def tearDown(self) -> None:
        win = getattr(self, "w", None)
        if win is not None:
            try:
                theming.manager().theme_changed.disconnect(
                    win._on_theme_changed)
            except (RuntimeError, TypeError):
                pass
            self.w = None
        super().tearDown()

    def _window(self, settings: Settings):
        theming.manager().apply(settings.theme)
        layout_module.manager().apply(settings.layout or "classic",
                                      dict(settings.layout_overrides or {}))
        from tide.ui.window import MainWindow
        router = PlaybackRouter()
        router.register(MpvBackend())
        w = MainWindow(LocalSource(), router)
        w._settings = settings
        self.w = w
        self._widgets.append(w)
        return w

    def _brutalist(self) -> Settings:
        s = Settings()
        s.first_launch_complete = True
        s.preset_chosen = True
        s.preset = "brutalist"
        s.theme = "gruvbox"
        s.layout = "classic"
        s.layout_overrides = {"progress": "dotted"}
        s.motion = "off"
        s.corner_style = "sharp"
        presets.stash(s)
        settings_module.save(s)
        return s

    def _dialog(self, settings: Settings, window=None):
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(settings, parent=window)
        self._widgets.append(dlg)
        return dlg

    def _flip_to(self, dlg, preset_id: str) -> None:
        idx = dlg.personality_picker.findData(preset_id)
        self.assertGreaterEqual(idx, 0)
        dlg.personality_picker.setCurrentIndex(idx)
        dlg.personality_picker.activated.emit(idx)   # a user activation
        QTest.qWait(40)                              # the deferred flip

    def test_picker_flips_the_window_live(self) -> None:
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        self.assertEqual(dlg.personality_picker.currentData(), "brutalist")
        self._flip_to(dlg, "modern")
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self.assertEqual(w._slot_progress, "bar")   # the window rebuilt
        self.assertEqual(settings_module.load().preset, "modern")
        # The picker and the theme combo followed the flip.
        self.assertEqual(dlg.personality_picker.currentData(), "modern")
        self.assertEqual(dlg.theme_picker.currentData(), s.theme)

    def test_repick_keeps_each_personality_customizations(self) -> None:
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        self._flip_to(dlg, "modern")
        self._flip_to(dlg, "brutalist")
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "gruvbox")
        self.assertEqual(s.corner_style, "sharp")
        self.assertEqual(s.layout_overrides, {"progress": "dotted"},
                         "a re-pick reset the user's slot tweaks")
        self.assertEqual(theming.manager().current().slug, "gruvbox")

    def test_accept_after_a_flip_keeps_the_new_personality(self) -> None:
        # The dialog's snapshots were taken against brutalist; without
        # the re-base, accept would diff modern's live values against
        # them and write the whole outgoing look back over the flip.
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        self._flip_to(dlg, "modern")
        dlg._on_save()
        QTest.qWait(20)
        self.assertNotIn("theme", dlg.changed_keys())
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertEqual(disk.theme, presets.builtin("modern").theme)

    def test_the_windows_accept_path_does_not_flip_back(self) -> None:
        # _do_open_settings reconciles the preset after an accepted
        # dialog. Its `before` snapshot predates the flip, so the
        # reconcile has to read the ACCEPTED state and leave it alone.
        import copy
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        before = copy.copy(s)                   # what _do_open_settings holds
        dlg = self._dialog(s, w)
        self._flip_to(dlg, "modern")
        dlg._on_save()
        w._reconcile_preset_after_dialog(before, s)
        QTest.qWait(30)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        # The brutalist look is still remembered for the trip back.
        self.assertEqual(s.preset_state["brutalist"]["theme"], "gruvbox")

    # ---- flip + cross-personality pick, through the REAL open path

    def _open_flip_and_pick(self, w, flip_to: str, slug: str):
        """Drive the real _do_open_settings: flip the personality from
        inside the dialog, tick the show-all escape, pick ``slug``,
        accept. The window's own snapshot bookkeeping runs for real."""
        from tide.ui.settings import SettingsDialog

        def fake_exec(dlg):
            self._flip_to(dlg, flip_to)
            dlg.theme_show_all_toggle.setChecked(True)
            idx = dlg.theme_picker.findData(slug)
            self.assertGreaterEqual(idx, 0, f"{slug} is not listed")
            dlg.theme_picker.setCurrentIndex(idx)
            dlg._on_save()
            return dlg.result()

        with mock.patch.object(SettingsDialog, "exec", fake_exec):
            w._do_open_settings()
        QTest.qWait(40)

    def test_a_flip_then_a_cross_pick_files_each_theme_on_its_own_side(self) -> None:
        # The window snapshots the settings at dialog-OPEN for its
        # accept-time reconcile, and the reconcile hands that snapshot's
        # theme back to the outgoing personality. Phase 4 lets the
        # personality move while the dialog is up, so the snapshot has to
        # move with it — otherwise modern ends up remembering the
        # brutalist theme the dialog happened to open on.
        s = self._brutalist()                   # brutalist / gruvbox
        w = self._window(s)
        QTest.qWait(20)
        self._open_flip_and_pick(w, "modern", "terminal-green")
        # The pick was brutalist, so it took them back to brutalist…
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "terminal-green")
        self.assertEqual(theming.manager().current().slug, "terminal-green")
        self.assertEqual(s.preset_state["brutalist"]["theme"],
                         "terminal-green")
        # …and modern kept what it was actually wearing when they left
        # it, not the theme the dialog opened on.
        self.assertEqual(
            s.preset_state["modern"]["theme"],
            presets.builtin("modern").theme,
            "the flip's outgoing theme was filed under the wrong "
            "personality (a stale pre-dialog snapshot)")
        self.assertEqual(settings_module.load().preset_state["modern"]["theme"],
                         presets.builtin("modern").theme)

    def test_the_flip_back_wears_what_it_was_left_in(self) -> None:
        # The promise the appearance tab's blurb makes, end to end.
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        self._open_flip_and_pick(w, "modern", "terminal-green")
        dlg = self._dialog(s, w)
        self._flip_to(dlg, "modern")
        self.assertEqual(s.theme, presets.builtin("modern").theme)
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)

    def test_rebasing_the_snapshot_is_a_no_op_with_no_dialog_open(self) -> None:
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        self.assertIsNone(getattr(w, "_settings_before_dialog", None))
        w.rebase_settings_snapshot(s)
        self.assertIsNone(getattr(w, "_settings_before_dialog", None))

    def test_cancel_after_a_flip_does_not_undo_it(self) -> None:
        # A flip is a commit. Cancel reverts PREVIEWS, and after the
        # re-base there are none to revert.
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        self._flip_to(dlg, "modern")
        dlg.reject()
        QTest.qWait(30)
        self.assertEqual(s.preset, "modern")
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self.assertEqual(settings_module.load().preset, "modern")

    def test_a_flip_keeps_unsaved_shared_edits(self) -> None:
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        dlg.lb_token.setText("half-typed-token")
        self._flip_to(dlg, "modern")
        self.assertEqual(dlg.lb_token.text(), "half-typed-token",
                         "the flip ate an edit it had no business touching")
        dlg._on_save()
        self.assertEqual(s.listenbrainz_token, "half-typed-token")

    def test_chooser_button_reopens_the_chooser_deferred(self) -> None:
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        with self._fake_chooser("modern"):
            dlg.chooser_btn.click()
            self.assertEqual(_FakeChooser.opened, [],
                             "the modal opened inside the click handler")
            QTest.qWait(40)
            self.assertEqual(len(_FakeChooser.opened), 1)
            parent, initial = _FakeChooser.opened[0]
            self.assertIs(parent, dlg)
            self.assertEqual(initial, "brutalist")
        self.assertEqual(s.preset, "modern")
        self.assertEqual(theming.manager().current().slug,
                         presets.builtin("modern").theme)
        self.assertEqual(dlg.personality_picker.currentData(), "modern")

    def test_dismissing_the_repick_changes_nothing(self) -> None:
        s = self._brutalist()
        w = self._window(s)
        QTest.qWait(20)
        dlg = self._dialog(s, w)
        before = dataclasses.asdict(s)
        with self._fake_chooser(""):
            dlg.chooser_btn.click()
            QTest.qWait(40)
        self.assertEqual(dataclasses.asdict(s), before)
        self.assertEqual(theming.manager().current().slug, "gruvbox")

    def test_preset_never_rides_the_dialog_diff(self) -> None:
        # The personality is not a descriptor on purpose: a generic
        # setattr would move settings.preset without stashing, restoring
        # or applying anything.
        from tide.ui import settings_schema
        self.assertNotIn("preset", settings_schema.by_key())
        self.assertIn("preset", settings_schema.INTERNAL_FIELDS)

    def test_picker_does_not_claim_an_unadopted_config(self) -> None:
        s = Settings()                          # preset == ""
        dlg = self._dialog(s)
        self.assertEqual(dlg.personality_picker.currentData(), "")
        self.assertEqual(dlg.personality_picker.currentText(),
                         "— not picked yet —")


if __name__ == "__main__":
    unittest.main()
