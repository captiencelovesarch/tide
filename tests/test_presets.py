"""v2.0 "two tides" preset core — builtin defs, stash/restore round-trip,
apply_preset's manager push order, and the silent 1.x adoption.

Managers are faked (recording call order) — these tests never need a
QApplication, a real theme on disk, or the concurrent agents' code.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import dataclasses
import tempfile
import types
import unittest
from pathlib import Path

from tide import config, presets
from tide import layout as layout_module
from tide import settings as settings_module
from tide import theming
from tide.presets import (
    BUILTINS,
    STASH_FIELDS,
    PresetDef,
    adopt_current,
    apply_preset,
    builtin,
    restore,
    stash,
)
from tide.settings import Settings
from tide.ui import motion as motion_module


class _FakeThemeManager:
    def __init__(self, log: list) -> None:
        self.log = log
        self.themes: list = []

    def apply_bundle(self, slug=None, *, font_family=None, font_size=None,
                     case=None):
        self.log.append(("apply_bundle", {
            "slug": slug, "font_family": font_family,
            "font_size": font_size, "case": case,
        }))

    def set_user_override(self, key, value) -> None:
        self.log.append(("set_user_override", key, value))

    def list_themes(self) -> list:
        return list(self.themes)


class _FakeLayoutManager:
    def __init__(self, log: list) -> None:
        self.log = log

    def apply(self, slug, overrides=None):
        self.log.append(("layout_apply", slug, dict(overrides or {})))


class _FakeWindow:
    def __init__(self, log: list) -> None:
        self.log = log

    def apply_preset_visuals(self) -> None:
        self.log.append(("window_visuals",))


class _PatchCase(unittest.TestCase):
    """Attribute-swap patching with guaranteed restore. presets.py looks
    the managers up at call time (lazy imports), so swapping the module
    attributes is all the seam we need."""

    def setUp(self) -> None:
        self._patched: list[tuple[object, str, object]] = []

    def tearDown(self) -> None:
        for obj, attr, orig in reversed(self._patched):
            setattr(obj, attr, orig)

    def _patch(self, obj, attr: str, repl) -> None:
        self._patched.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, repl)


class _FakeManagersCase(_PatchCase):
    """Fake theming/layout/motion managers recording into one ordered log."""

    def setUp(self) -> None:
        super().setUp()
        self.log: list = []
        self.theme_mgr = _FakeThemeManager(self.log)
        self.layout_mgr = _FakeLayoutManager(self.log)
        self._patch(theming, "manager", lambda: self.theme_mgr)
        self._patch(layout_module, "manager", lambda: self.layout_mgr)
        self._patch(motion_module, "set_intensity",
                    lambda v: self.log.append(("set_intensity", v)))
        # bind_preset is NOT faked (the dialect binding is pinned here)
        # but its global is restored so it can't decide another file's dialect
        self._bound_profile = motion_module._preset_profile
        self.addCleanup(
            setattr, motion_module, "_preset_profile", self._bound_profile)
        # Field-scoped persistence recorded, not written.
        self.saved: list = []
        self._real_save_fields = settings_module.save_fields
        self._patch(settings_module, "save_fields",
                    lambda s, *names: self.saved.append((s, names)))


def _brutalist_customized() -> Settings:
    """A settings object a long-time brutalist user might hold: builtin-ish
    but with personal tweaks on most stash fields."""
    s = Settings()
    s.preset = "brutalist"
    s.theme = "gruvbox-mono"
    s.layout = "classic"
    s.layout_overrides = {"progress": "blocks", "strip": "bottom"}
    s.motion = "off"
    s.corner_style = "sharp"
    s.nav_icon_set = "brutalist"
    s.text_case_override = "upper"
    s.font_family_override = "Terminus"
    s.font_size_override_pt = 11
    s.adaptive_accent = False
    s.adaptive_background = False
    s.adaptive_background_style = "field"
    s.adaptive_pulse = False
    s.mini_backdrop_style = "off"
    s.fullscreen_backdrop_style = "off"
    s.mini_pulse = False
    s.fullscreen_pulse = False
    s.ui_sounds_enabled = True
    s.show_thumbnails = "off"
    return s


class ContractShapeTests(unittest.TestCase):
    def test_builtin_ids(self) -> None:
        self.assertEqual(set(BUILTINS), {"brutalist", "modern"})
        self.assertIs(builtin("brutalist"), BUILTINS["brutalist"])
        with self.assertRaises(KeyError):
            builtin("vaporwave")

    def test_defs_are_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            BUILTINS["brutalist"].theme = "oops"

    def test_stash_fields_are_real_settings_fields(self) -> None:
        known = {f.name for f in dataclasses.fields(Settings)}
        for name in STASH_FIELDS:
            self.assertIn(name, known)
        self.assertEqual(len(STASH_FIELDS), len(set(STASH_FIELDS)))
        # the preset bookkeeping itself must never be stashed — a stash
        # containing "preset" would make restore() self-referential
        for meta in ("preset", "preset_chosen", "preset_state"):
            self.assertNotIn(meta, STASH_FIELDS)

    def test_brutalist_builtin_values(self) -> None:
        d = builtin("brutalist")
        self.assertEqual(d.theme, "brutalist-mono")
        self.assertEqual(d.layout, "classic")
        self.assertEqual(d.motion, "off")
        self.assertEqual(d.corner_style, "sharp")
        self.assertEqual(d.nav_icon_set, "off")
        self.assertFalse(d.adaptive_accent)
        self.assertFalse(d.adaptive_background)
        self.assertEqual(d.adaptive_background_style, "field")
        self.assertFalse(d.adaptive_pulse)
        self.assertEqual(d.mini_backdrop_style, "off")
        self.assertEqual(d.fullscreen_backdrop_style, "off")
        self.assertFalse(d.mini_pulse)
        self.assertFalse(d.fullscreen_pulse)
        self.assertFalse(d.ui_sounds_enabled)
        # mono + art: art is content, not chrome.
        self.assertEqual(d.show_thumbnails, "on")
        self.assertEqual(d.sound_pack, "default")
        self.assertEqual(d.glyph_pack, "default")

    def test_modern_builtin_values(self) -> None:
        d = builtin("modern")
        self.assertEqual(d.theme, "adaptive")
        self.assertEqual(d.layout, "classic")
        self.assertEqual(d.motion, "full")
        self.assertEqual(d.corner_style, "soft")
        self.assertEqual(d.nav_icon_set, "svg")
        self.assertTrue(d.adaptive_accent)
        self.assertTrue(d.adaptive_background)
        self.assertEqual(d.adaptive_background_style, "liquid")
        self.assertTrue(d.adaptive_pulse)
        self.assertEqual(d.mini_backdrop_style, "follow")
        self.assertEqual(d.fullscreen_backdrop_style, "follow")
        self.assertTrue(d.mini_pulse)
        self.assertTrue(d.fullscreen_pulse)
        self.assertFalse(d.ui_sounds_enabled)
        self.assertEqual(d.show_thumbnails, "theme")


class StashTests(unittest.TestCase):
    def test_noop_without_active_preset(self) -> None:
        s = Settings()   # preset defaults to ""
        stash(s)
        self.assertEqual(s.preset_state, {})

    def test_snapshots_every_stash_field(self) -> None:
        s = _brutalist_customized()
        stash(s)
        snap = s.preset_state["brutalist"]
        self.assertEqual(set(snap), set(STASH_FIELDS))
        for name in STASH_FIELDS:
            self.assertEqual(snap[name], getattr(s, name), name)

    def test_snapshot_is_a_deep_copy(self) -> None:
        s = _brutalist_customized()
        stash(s)
        s.layout_overrides["progress"] = "mutated-after-stash"
        self.assertEqual(
            s.preset_state["brutalist"]["layout_overrides"]["progress"],
            "blocks",
        )


class RestoreTests(unittest.TestCase):
    def test_restores_from_stash_and_activates(self) -> None:
        s = Settings()
        donor = _brutalist_customized()
        s.preset_state["brutalist"] = {
            name: getattr(donor, name) for name in STASH_FIELDS
        }
        restore(s, "brutalist")
        self.assertEqual(s.preset, "brutalist")
        self.assertEqual(s.theme, "gruvbox-mono")
        self.assertEqual(s.layout_overrides, {"progress": "blocks",
                                              "strip": "bottom"})
        self.assertEqual(s.font_family_override, "Terminus")
        self.assertEqual(s.font_size_override_pt, 11)
        self.assertTrue(s.ui_sounds_enabled)
        self.assertEqual(s.show_thumbnails, "off")

    def test_restored_dicts_do_not_alias_the_stash(self) -> None:
        s = Settings()
        s.preset_state["brutalist"] = {"layout_overrides": {"progress": "blocks"}}
        restore(s, "brutalist")
        s.layout_overrides["progress"] = "mutated-after-restore"
        self.assertEqual(
            s.preset_state["brutalist"]["layout_overrides"]["progress"],
            "blocks",
        )

    def test_first_visit_uses_builtin_defaults(self) -> None:
        s = _brutalist_customized()   # no stashes at all
        restore(s, "modern")
        self.assertEqual(s.preset, "modern")
        self.assertEqual(s.theme, "adaptive")
        self.assertEqual(s.motion, "full")
        self.assertEqual(s.corner_style, "soft")
        self.assertTrue(s.adaptive_background)
        self.assertEqual(s.adaptive_background_style, "liquid")
        # Fields a PresetDef doesn't carry reset to the Settings defaults.
        self.assertEqual(s.layout_overrides, {})
        self.assertEqual(s.font_family_override, "")
        self.assertEqual(s.font_size_override_pt, 0)

    def test_partial_stash_backfills_per_field(self) -> None:
        # a stash from an older build that predates two of the fields:
        # known fields restore, missing ones fall to builtin/defaults
        s = Settings()
        s.preset_state["modern"] = {"theme": "ambient", "motion": "lite"}
        restore(s, "modern")
        self.assertEqual(s.theme, "ambient")
        self.assertEqual(s.motion, "lite")
        self.assertEqual(s.corner_style, "soft")        # builtin backfill
        self.assertEqual(s.layout_overrides, {})        # Settings default

    def test_unknown_preset_without_stash_raises(self) -> None:
        with self.assertRaises(KeyError):
            restore(Settings(), "vaporwave")

    def test_unknown_preset_with_stash_restores(self) -> None:
        # custom presets ride the same stash path — only the
        # builtin-defaults fallback needs a known id
        s = Settings()
        s.preset_state["vaporwave"] = {
            name: getattr(Settings(), name) for name in STASH_FIELDS
        }
        s.preset_state["vaporwave"]["theme"] = "vhs"
        restore(s, "vaporwave")
        self.assertEqual(s.preset, "vaporwave")
        self.assertEqual(s.theme, "vhs")


class ApplyPresetTests(_FakeManagersCase):
    def test_manager_push_order_and_args(self) -> None:
        s = _brutalist_customized()
        window = _FakeWindow(self.log)
        apply_preset(s, "modern", window=window, persist=False)
        self.assertEqual(self.log, [
            ("apply_bundle", {"slug": "adaptive", "font_family": "",
                              "font_size": 0, "case": ""}),
            ("layout_apply", "classic", {}),
            ("set_intensity", "full"),
            ("set_user_override", "radius", "6px"),   # soft = 6px
            ("window_visuals",),
        ])

    def test_apply_binds_the_motion_dialect_to_the_personality(self) -> None:
        # the dialect follows the personality, not the intensity —
        # brutalist at motion full still gets mechanical curves
        s = _brutalist_customized()
        apply_preset(s, "modern", persist=False)
        self.assertEqual(motion_module.bound_profile(), "springy")
        apply_preset(s, "brutalist", persist=False)
        self.assertEqual(motion_module.bound_profile(), "mechanical")
        prev_reduced = motion_module._reduced_motion
        prev_override = motion_module._profile_override
        prev_intensity = motion_module._user_intensity
        self.addCleanup(setattr, motion_module, "_reduced_motion", prev_reduced)
        self.addCleanup(
            setattr, motion_module, "_profile_override", prev_override)
        self.addCleanup(
            setattr, motion_module, "_user_intensity", prev_intensity)
        motion_module._reduced_motion = False
        motion_module._profile_override = None
        motion_module._user_intensity = motion_module.Intensity.FULL
        self.assertEqual(motion_module.profile(), "mechanical")

    def test_sharp_corners_clear_the_radius_override(self) -> None:
        s = Settings()
        apply_preset(s, "brutalist", persist=False)
        self.assertIn(("set_user_override", "radius", None), self.log)

    def test_window_none_skips_the_visuals_hook(self) -> None:
        apply_preset(Settings(), "modern", persist=False)
        self.assertNotIn(("window_visuals",), self.log)

    def test_flip_roundtrip_restores_every_stashed_field(self) -> None:
        s = _brutalist_customized()
        original = {name: getattr(s, name) for name in STASH_FIELDS}
        apply_preset(s, "modern", persist=False)
        self.assertEqual(s.theme, "adaptive")   # sanity: flip landed
        apply_preset(s, "brutalist", persist=False)
        for name in STASH_FIELDS:
            self.assertEqual(getattr(s, name), original[name], name)

    def test_flip_keeps_incoming_presets_own_tweaks(self) -> None:
        s = _brutalist_customized()
        apply_preset(s, "modern", persist=False)
        s.adaptive_background_style = "stage"   # a modern-side tweak
        apply_preset(s, "brutalist", persist=False)
        apply_preset(s, "modern", persist=False)
        self.assertEqual(s.adaptive_background_style, "stage")

    def test_shared_fields_survive_a_flip(self) -> None:
        s = _brutalist_customized()
        s.volume = 63
        s.playback_speed = 1.25
        s.active_source = "soundcloud"
        apply_preset(s, "modern", persist=False)
        self.assertEqual(s.volume, 63)
        self.assertEqual(s.playback_speed, 1.25)
        self.assertEqual(s.active_source, "soundcloud")

    def test_preset_chosen_is_not_apply_presets_business(self) -> None:
        s = _brutalist_customized()
        apply_preset(s, "modern", persist=False)
        self.assertFalse(s.preset_chosen)

    def test_no_ghost_stash_for_the_empty_preset(self) -> None:
        # First-ever apply on a pre-2.0 config (preset "") must not file a
        # stash under "" — there is no outgoing personality yet.
        s = Settings()
        apply_preset(s, "modern", persist=False)
        self.assertNotIn("", s.preset_state)

    def test_reapplying_the_active_preset_keeps_live_values(self) -> None:
        # same-id apply (startup bootstrap) must NOT restore: restoring a
        # stale snapshot is what reverted every dialog customization on
        # the next launch. The stash refreshes from the live values.
        s = _brutalist_customized()
        stash(s)
        s.theme = "abyss"            # dialog-style edit, stash untouched
        s.motion = "full"
        apply_preset(s, "brutalist", persist=False)
        self.assertEqual(s.theme, "abyss")
        self.assertEqual(s.motion, "full")
        self.assertEqual(s.preset_state["brutalist"]["theme"], "abyss")
        self.assertEqual(s.preset_state["brutalist"]["motion"], "full")
        self.assertIn(
            ("apply_bundle", {"slug": "abyss", "font_family": "Terminus",
                              "font_size": 11, "case": "upper"}),
            self.log,
        )

    def test_same_id_reapply_of_unknown_preset_does_not_raise(self) -> None:
        # a downgrade's unknown ACTIVE id: startup re-apply pushes the
        # live fields, never consults restore() (which would KeyError)
        s = _brutalist_customized()
        s.preset = "vaporwave"
        s.preset_state.clear()
        apply_preset(s, "vaporwave", persist=False)   # must not raise
        self.assertEqual(s.preset, "vaporwave")
        self.assertEqual(s.theme, "gruvbox-mono")
        self.assertEqual(
            s.preset_state["vaporwave"]["theme"], "gruvbox-mono")

    def test_persist_saves_exactly_the_preset_fields(self) -> None:
        s = _brutalist_customized()
        apply_preset(s, "modern")
        self.assertEqual(len(self.saved), 1)
        saved_obj, names = self.saved[0]
        self.assertIs(saved_obj, s)
        self.assertEqual(
            names, ("preset", "preset_chosen", "preset_state") + STASH_FIELDS
        )

    def test_persist_false_never_touches_disk(self) -> None:
        apply_preset(_brutalist_customized(), "modern", persist=False)
        self.assertEqual(self.saved, [])

    def test_persist_failure_does_not_break_the_flip(self) -> None:
        def _boom(s, *names):
            raise OSError("disk full")
        self._patch(settings_module, "save_fields", _boom)
        s = _brutalist_customized()
        apply_preset(s, "modern")   # must not raise
        self.assertEqual(s.preset, "modern")


class ApplyPresetPersistIntegrationTests(_FakeManagersCase):
    """Same fake managers, REAL field-scoped persistence into a sandboxed
    settings.toml — proves a flip survives the disk round-trip (bools,
    nested stash tables and all)."""

    def setUp(self) -> None:
        super().setUp()
        # Undo the base class's save_fields recorder — this class wants
        # the real one, pointed at its own temp file.
        self._patch(settings_module, "save_fields", self._real_save_fields)
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-presets-")
        self.addCleanup(self._tmp.cleanup)
        self._patch(config, "SETTINGS_FILE",
                    Path(self._tmp.name) / "settings.toml")

    def test_flip_persists_and_reloads(self) -> None:
        settings_module.save(_brutalist_customized())
        s = settings_module.load()
        apply_preset(s, "modern")
        back = settings_module.load()
        self.assertEqual(back.preset, "modern")
        self.assertEqual(back.theme, "adaptive")
        self.assertTrue(back.adaptive_background)
        brut = back.preset_state["brutalist"]
        self.assertEqual(brut["theme"], "gruvbox-mono")
        self.assertEqual(brut["layout_overrides"],
                         {"progress": "blocks", "strip": "bottom"})
        self.assertIs(brut["ui_sounds_enabled"], True)
        self.assertIs(brut["mini_pulse"], False)
        apply_preset(back, "brutalist", persist=False)
        self.assertEqual(back.theme, "gruvbox-mono")
        self.assertEqual(back.font_family_override, "Terminus")


class AdoptCurrentTests(_FakeManagersCase):
    def _theme(self, slug: str, aesthetic: str):
        return types.SimpleNamespace(slug=slug, aesthetic=aesthetic)

    def test_adopts_the_themes_aesthetic(self) -> None:
        self.theme_mgr.themes = [
            self._theme("ambient", "modern"),
            self._theme("gruvbox-mono", "brutalist"),
        ]
        s = _brutalist_customized()
        s.preset = ""
        self.assertEqual(adopt_current(s), "brutalist")
        self.assertEqual(s.preset, "brutalist")

    def test_unknown_theme_falls_back_to_modern(self) -> None:
        self.theme_mgr.themes = [self._theme("ambient", "modern")]
        s = Settings()
        s.theme = "some-user-theme-not-installed"
        self.assertEqual(adopt_current(s), "modern")

    def test_manager_blowup_falls_back_to_modern(self) -> None:
        def _boom():
            raise RuntimeError("no qt")
        self._patch(theming, "manager", _boom)
        s = Settings()
        self.assertEqual(adopt_current(s), "modern")
        self.assertEqual(s.preset, "modern")

    def test_adoption_is_visually_lossless(self) -> None:
        # the migration invariant: nothing but the preset bookkeeping
        # moves; every visible field stays verbatim
        self.theme_mgr.themes = [self._theme("gruvbox-mono", "brutalist")]
        s = _brutalist_customized()
        s.preset = ""
        before = dataclasses.asdict(s)
        adopt_current(s)
        after = dataclasses.asdict(s)
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"preset", "preset_state"})
        self.assertEqual(
            s.preset_state["brutalist"],
            {name: before[name] for name in STASH_FIELDS},
        )
        # …and adoption is NOT a choice — the chooser can still offer
        # itself later
        self.assertFalse(s.preset_chosen)

    def test_weird_aesthetic_value_falls_back_to_modern(self) -> None:
        self.theme_mgr.themes = [self._theme("brutalist-mono", "cyberpunk")]
        s = Settings()
        self.assertEqual(adopt_current(s), "modern")


if __name__ == "__main__":
    unittest.main()
