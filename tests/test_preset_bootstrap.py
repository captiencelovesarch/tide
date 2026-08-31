"""v2.0 startup preset bootstrap (app.py): silent 1.x adoption, the
wizard→preset handoff, and the --theme run-only override.

These drive the REAL theming/layout/motion managers (offscreen) against
the real bundled themes — the whole point of the migration invariant is
that startup lands in the exact visual state 1.x produced.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication, QDialog

from tide import app as app_module
from tide import config
from tide import layout as layout_module
from tide import settings as settings_module
from tide import theming
from tide.settings import Settings
from tide.ui import motion as motion_module
from tide.ui import scale as scale_module
from tide.ui.onboarding import OnboardingResult


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _BootstrapCase(unittest.TestCase):
    """Own settings file per test + restoration of the process-global
    look state (theme, layout, motion, scale, sticky overrides) that the
    bootstrap deliberately mutates."""

    def setUp(self) -> None:
        app = _app()
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-bootstrap-")
        self.addCleanup(self._tmp.cleanup)
        self._real_settings_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"
        # Suppress real app-wide QSS pushes (test_restyle_coalesce's spy
        # pattern) — the bootstrap and the tearDown reset each queue one,
        # and a real push repolishes every window earlier suite files
        # leaked (seconds each). Manager state stays fully real.
        mock.patch.object(app, "setStyleSheet").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self) -> None:
        config.SETTINGS_FILE = self._real_settings_file
        # Put the shared managers back in a neutral place so tests later
        # in the same process aren't order-dependent on ours.
        theming.manager().set_user_override("radius", None)
        theming.manager().apply_bundle(
            slug="brutalist-mono", font_family="", font_size=0, case="")
        layout_module.manager().apply("classic", {})
        motion_module.set_intensity("lite")
        scale_module.set_factor("normal")


class UpgraderAdoptionTest(_BootstrapCase):
    """A 1.x user's first 2.0 launch: their look gets filed under a preset
    with ZERO visible change, exactly once."""

    def _upgrader(self) -> Settings:
        s = Settings()
        s.theme = "gruvbox"                     # aesthetic=brutalist
        s.motion = "off"
        s.corner_style = "rounded"
        s.nav_icon_set = "brutalist"
        s.layout_overrides = {"progress": "blocks"}
        s.first_launch_complete = True
        return s

    def test_adoption_is_lossless_and_persisted(self) -> None:
        settings_module.save(self._upgrader())
        s = settings_module.load()
        before = dataclasses.asdict(s)
        app_module._bootstrap_preset(s)
        after = dataclasses.asdict(s)
        changed = {k for k in before if before[k] != after[k]}
        # The migration invariant: only bookkeeping moves — plus the seed
        # that pins the 1.x startup size (nothing visible changes: 1.x
        # resized to 1100x720 at every launch).
        self.assertEqual(changed, {"preset", "preset_state", "window_sizes"})
        self.assertEqual(s.preset, "brutalist")
        self.assertFalse(s.preset_chosen)       # adoption is not a choice
        self.assertEqual(s.window_sizes, {"classic": [1100, 720]})
        # The managers hold the user's exact 1.x state.
        self.assertEqual(theming.manager().current().slug, "gruvbox")
        self.assertEqual(
            layout_module.manager().current().slots["progress"], "blocks")
        # Persisted for the upgrader — the next launch skips adoption.
        disk = settings_module.load()
        self.assertEqual(disk.preset, "brutalist")
        self.assertFalse(disk.preset_chosen)
        self.assertEqual(disk.preset_state["brutalist"]["theme"], "gruvbox")
        self.assertEqual(disk.theme, "gruvbox")

    def test_adoption_seeds_the_1x_window_size_for_the_active_layout(self) -> None:
        # 1.x launched at 1100x720 no matter the layout (window_default
        # only applied on live layout switches), so a focused/dj-deck
        # upgrader's first 2.0 frame must too: the seed becomes the
        # remembered size the window ctor reads from disk.
        s = self._upgrader()
        s.layout = "focused"
        settings_module.save(s)
        loaded = settings_module.load()
        app_module._bootstrap_preset(loaded)
        self.assertEqual(loaded.window_sizes["focused"], [1100, 720])
        disk = settings_module.load()
        self.assertEqual(disk.window_sizes["focused"], [1100, 720])

    def test_adoption_never_overwrites_a_remembered_size(self) -> None:
        # Paranoia: a config that somehow has a size already (partial 2.0
        # write, downgrade round-trip) keeps it — seed is setdefault-only.
        s = self._upgrader()
        s.window_sizes = {"classic": [900, 600]}
        settings_module.save(s)
        loaded = settings_module.load()
        app_module._bootstrap_preset(loaded)
        self.assertEqual(loaded.window_sizes["classic"], [900, 600])

    def test_second_launch_is_idempotent(self) -> None:
        settings_module.save(self._upgrader())
        s = settings_module.load()
        app_module._bootstrap_preset(s)
        # "Second launch": reload from disk, bootstrap again.
        again = settings_module.load()
        before = dataclasses.asdict(again)
        app_module._bootstrap_preset(again)
        self.assertEqual(dataclasses.asdict(again), before)
        self.assertEqual(theming.manager().current().slug, "gruvbox")

    def test_v1_backup_written_before_adoption_persists(self) -> None:
        # The startup order run() wires: load → ensure_v1_backup →
        # bootstrap. The archived file must be the preset-free 1.x one.
        v1_body = 'theme = "gruvbox"\nfirst_launch_complete = true\n'
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text(v1_body, encoding="utf-8")
        s = settings_module.load()
        settings_module.ensure_v1_backup()
        app_module._bootstrap_preset(s)
        v1 = config.SETTINGS_FILE.with_name(
            config.SETTINGS_FILE.name + ".v1.bak")
        self.assertTrue(v1.exists())
        self.assertNotIn("preset", v1.read_text(encoding="utf-8"))
        self.assertIn("preset", config.SETTINGS_FILE.read_text(encoding="utf-8"))

    def test_true_first_launch_writes_no_file(self) -> None:
        # A cancelled wizard has always meant "no settings file yet" —
        # the pre-wizard adoption must not start leaving one behind.
        s = Settings()                          # first_launch_complete False
        app_module._bootstrap_preset(s)
        self.assertEqual(s.preset, "brutalist")     # brutalist-mono default
        self.assertEqual(theming.manager().current().slug, "brutalist-mono")
        self.assertFalse(config.SETTINGS_FILE.exists())


class DialogEditSurvivalTest(_BootstrapCase):
    """The verified startup-revert repro: the settings dialog is the ONLY
    way to change theme/motion/corners in Phase 1, and it full-saves the
    live fields WITHOUT updating preset_state. The next launch must keep
    those edits — not restore the stale stash over them."""

    def test_dialog_customizations_survive_a_relaunch(self) -> None:
        # Launch 1: a 1.x gruvbox/rounded user gets adopted.
        s = Settings()
        s.theme = "gruvbox"
        s.motion = "lite"
        s.corner_style = "rounded"
        s.first_launch_complete = True
        settings_module.save(s)
        live = settings_module.load()
        app_module._bootstrap_preset(live)
        self.assertEqual(live.preset, "brutalist")
        # Dialog-style save: mutate the surfaced fields, full save, no
        # stash refresh at this layer (SettingsDialog._on_save).
        live.theme = "abyss"
        live.motion = "off"
        live.corner_style = "sharp"
        settings_module.save(live)
        # Launch 2.
        relaunch = settings_module.load()
        app_module._bootstrap_preset(relaunch)
        self.assertEqual(relaunch.theme, "abyss",
                         "startup restored a stale stash over dialog edits")
        self.assertEqual(relaunch.motion, "off")
        self.assertEqual(relaunch.corner_style, "sharp")
        self.assertEqual(theming.manager().current().slug, "abyss")
        # The refreshed stash means the NEXT flip round-trips the edits
        # instead of making the loss durable.
        self.assertEqual(
            relaunch.preset_state["brutalist"]["theme"], "abyss")
        self.assertEqual(
            relaunch.preset_state["brutalist"]["corner_style"], "sharp")


class UnknownPresetRecoveryTest(_BootstrapCase):
    """A settings.toml whose preset value matches no builtin must never
    keep tide from launching (the no-config-file-editing rule: there'd be
    no recovery path)."""

    def test_unknown_preset_without_stash_readopts(self) -> None:
        s = Settings()
        s.preset = "vaporwave"          # hand-edited / future build
        s.theme = "gruvbox"             # aesthetic=brutalist
        s.first_launch_complete = True
        settings_module.save(s)
        loaded = settings_module.load()
        loaded.preset_state.clear()     # no [preset_state.vaporwave] table
        app_module._bootstrap_preset(loaded)   # must not raise
        self.assertEqual(loaded.preset, "brutalist")
        self.assertEqual(loaded.theme, "gruvbox")
        self.assertEqual(theming.manager().current().slug, "gruvbox")
        disk = settings_module.load()
        self.assertEqual(disk.preset, "brutalist")

    def test_unknown_preset_with_stash_keeps_working(self) -> None:
        # Phase-2 custom presets (or a downgrade that kept the stash)
        # ride the same-id path: live fields win, no restore, no crash.
        s = Settings()
        s.preset = "vaporwave"
        s.theme = "gruvbox"
        s.first_launch_complete = True
        from tide import presets
        presets.stash(s)
        settings_module.save(s)
        loaded = settings_module.load()
        app_module._bootstrap_preset(loaded)   # must not raise
        self.assertEqual(loaded.preset, "vaporwave")
        self.assertEqual(loaded.theme, "gruvbox")
        self.assertEqual(theming.manager().current().slug, "gruvbox")


class CliThemeOverrideTest(_BootstrapCase):
    def test_cli_theme_wins_the_session_but_never_persists(self) -> None:
        s = Settings()
        s.theme = "gruvbox"
        s.first_launch_complete = True
        settings_module.save(s)
        loaded = settings_module.load()
        app_module._bootstrap_preset(loaded, cli_theme="nord")
        self.assertEqual(theming.manager().current().slug, "nord")
        # Run-only: neither the live settings, the stash, nor the disk
        # learn about it.
        self.assertEqual(loaded.theme, "gruvbox")
        self.assertEqual(
            loaded.preset_state[loaded.preset]["theme"], "gruvbox")
        disk = settings_module.load()
        self.assertEqual(disk.theme, "gruvbox")
        self.assertEqual(disk.preset_state["brutalist"]["theme"], "gruvbox")


class EmptyThemeFallbackTest(_BootstrapCase):
    def test_blank_slug_still_styles_the_app(self) -> None:
        # Old code fell back via `args.theme or settings.theme or
        # DEFAULT_THEME`; the preset path must keep the guarantee. Runs on
        # a pristine manager because the fallback only fires when nothing
        # was ever applied — exactly the real startup condition.
        real = theming._manager
        theming._manager = None
        try:
            s = Settings()
            s.theme = ""
            s.first_launch_complete = True
            app_module._bootstrap_preset(s)
            self.assertEqual(
                theming.manager().current().slug, app_module.DEFAULT_THEME)
            self.assertEqual(s.theme, "")       # field itself stays verbatim
        finally:
            theming._manager = real


class _FakeWizard:
    """Stands in for OnboardingDialog: instantly answers with a canned
    result. Class attributes are configured per test."""

    DialogCode = QDialog.DialogCode
    accept = True
    result: OnboardingResult | None = None

    def __init__(self) -> None:
        pass

    def exec(self):
        return (QDialog.DialogCode.Accepted if type(self).accept
                else QDialog.DialogCode.Rejected)

    def result_data(self) -> OnboardingResult:
        return type(self).result


class WizardPresetHandoffTest(_BootstrapCase):
    """The wizard's aesthetic pick becomes the explicit personality, its
    theme survives apply_preset, and everything round-trips to disk."""

    def setUp(self) -> None:
        super().setUp()
        from tide.ui import onboarding as onboarding_module
        self._onb = onboarding_module
        self._real_dialog = onboarding_module.OnboardingDialog
        onboarding_module.OnboardingDialog = _FakeWizard

    def tearDown(self) -> None:
        self._onb.OnboardingDialog = self._real_dialog
        super().tearDown()

    def _fresh_launch(self) -> Settings:
        s = Settings()                          # no file on disk yet
        app_module._bootstrap_preset(s)         # real startup order
        return s

    def test_modern_pick_persists_and_theme_survives(self) -> None:
        _FakeWizard.accept = True
        _FakeWizard.result = OnboardingResult(
            completed=True, aesthetic="modern", theme_slug="nord",
            adaptive_accent=True, adaptive_background=True, motion="full",
        )
        s = self._fresh_launch()
        self.assertTrue(app_module.run_onboarding_if_needed(s))
        self.assertEqual(s.preset, "modern")
        self.assertTrue(s.preset_chosen)        # an explicit pick, finally
        # The verified first-run bug: the picked theme must survive the
        # post-wizard apply_preset (it restores from the seeded stash).
        self.assertEqual(s.theme, "nord")
        self.assertEqual(theming.manager().current().slug, "nord")
        self.assertEqual(s.preset_state["modern"]["theme"], "nord")
        self.assertTrue(s.adaptive_pulse)       # rides adaptive_background
        self.assertEqual(s.motion, "full")
        # The adoption scaffolding from earlier this launch is dropped —
        # a later flip to brutalist starts from its builtin defaults.
        self.assertNotIn("brutalist", s.preset_state)
        # Disk round-trip.
        disk = settings_module.load()
        self.assertEqual(disk.preset, "modern")
        self.assertTrue(disk.preset_chosen)
        self.assertEqual(disk.theme, "nord")
        self.assertTrue(disk.first_launch_complete)
        self.assertEqual(disk.preset_state["modern"]["theme"], "nord")

    def test_brutalist_pick_stashes_under_brutalist(self) -> None:
        _FakeWizard.accept = True
        _FakeWizard.result = OnboardingResult(
            completed=True, aesthetic="brutalist", theme_slug="gruvbox",
            adaptive_accent=False, adaptive_background=False, motion="off",
        )
        s = self._fresh_launch()
        self.assertTrue(app_module.run_onboarding_if_needed(s))
        self.assertEqual(s.preset, "brutalist")
        self.assertTrue(s.preset_chosen)
        self.assertEqual(s.theme, "gruvbox")
        self.assertEqual(s.preset_state["brutalist"]["theme"], "gruvbox")
        self.assertEqual(theming.manager().current().slug, "gruvbox")

    def test_cancelled_wizard_leaves_no_trace(self) -> None:
        _FakeWizard.accept = False
        _FakeWizard.result = None
        s = self._fresh_launch()
        self.assertFalse(app_module.run_onboarding_if_needed(s))
        self.assertFalse(s.preset_chosen)
        self.assertFalse(s.first_launch_complete)
        self.assertFalse(config.SETTINGS_FILE.exists())


if __name__ == "__main__":
    unittest.main()
