"""v2.0 phase 2 — the option-descriptor table (ui/settings_schema.py).

The headline is the coverage meta-test: every Settings field must be in
REGISTRY (it has a GUI surface) or in INTERNAL_FIELDS (a written reason
why not) — the "no option ever ships without a GUI again" guarantee.

Also pinned: descriptor shape sanity (kinds, tabs, choices resolve
against the live registries, defaults pickable), per-preset flags
matching presets.STASH_FIELDS, live-applier names resolving on
MainWindow (or sitting in the allowlist), the two new fields
(keymap / glyph_overrides), and the deleted spotify_client_id.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

from tide import config, presets
from tide import settings as settings_module
from tide.settings import Settings
from tide.ui import settings_schema as schema
from tide.ui.settings_schema import (
    INTERNAL_FIELDS,
    KINDS,
    PENDING_LIVE_APPLIERS,
    REGISTRY,
    TABS,
    OptionDesc,
    by_key,
    coverage_report,
    resolve_choices,
    stash_membership_consistent,
)


# ---------- coverage — the guarantee ----------

class CoverageTests(unittest.TestCase):
    def test_every_settings_field_is_covered(self) -> None:
        missing, orphaned = coverage_report()
        self.assertEqual(
            missing, [],
            "Settings field(s) with neither a descriptor nor an "
            f"INTERNAL_FIELDS reason: {missing} — every new option must "
            "get a GUI surface (or a written reason it doesn't need one)",
        )
        self.assertEqual(
            orphaned, [],
            f"registry key(s) that aren't Settings fields: {orphaned}",
        )

    def test_internal_fields_are_real_settings_fields(self) -> None:
        known = {f.name for f in fields(Settings)}
        stale = sorted(INTERNAL_FIELDS - known)
        self.assertEqual(
            stale, [],
            f"INTERNAL_FIELDS entries that aren't Settings fields: {stale}",
        )

    def test_no_overlap_between_registry_and_internal(self) -> None:
        overlap = sorted({d.key for d in REGISTRY} & INTERNAL_FIELDS)
        self.assertEqual(overlap, [])

    def test_no_duplicate_registry_keys(self) -> None:
        keys = [d.key for d in REGISTRY]
        self.assertEqual(len(keys), len(set(keys)))

    def test_by_key_maps_every_descriptor(self) -> None:
        mapping = by_key()
        self.assertEqual(len(mapping), len(REGISTRY))
        self.assertIs(mapping["theme"], REGISTRY[0])

    def test_hidden_v1_fields_now_have_descriptors(self) -> None:
        # the four options that shipped with no GUI at all in 1.x
        mapping = by_key()
        for key in ("local_auto_index", "spotify_bitrate",
                    "spotify_audio_device", "spotify_connect_enabled"):
            self.assertIn(key, mapping, key)
            self.assertEqual(mapping[key].tab, "sources", key)
            self.assertTrue(mapping[key].advanced, key)

    def test_spotify_client_id_is_gone(self) -> None:
        # grep verified nothing ever read it (the sign-in dialog keeps its
        # own field; auth_spotify resolves the effective id itself).
        self.assertNotIn(
            "spotify_client_id", {f.name for f in fields(Settings)}
        )


# ---------- descriptor sanity ----------

class DescriptorSanityTests(unittest.TestCase):
    def test_descriptors_are_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            REGISTRY[0].label = "oops"

    def test_kinds_and_tabs_valid(self) -> None:
        for desc in REGISTRY:
            self.assertIn(desc.kind, KINDS, desc.key)
            self.assertIn(desc.tab, TABS, desc.key)

    def test_labels_and_sections_nonempty(self) -> None:
        for desc in REGISTRY:
            self.assertTrue(desc.label.strip(), desc.key)
            self.assertTrue(desc.section.strip(), desc.key)

    def test_choice_descriptors_resolve(self) -> None:
        """Every choice descriptor yields (value, label) rows with unique
        values and non-empty labels. Providers hit the LIVE registries,
        so a registry drifting out from under its blurbs fails here, not
        at dialog-open time."""
        for desc in REGISTRY:
            if desc.kind != "choice":
                continue
            with self.subTest(key=desc.key):
                rows = resolve_choices(desc)
                self.assertTrue(rows, f"{desc.key}: empty choices")
                values = [value for value, _label in rows]
                self.assertEqual(
                    len(values), len(set(values)),
                    f"{desc.key}: duplicate choice values",
                )
                for value, label in rows:
                    self.assertIsInstance(label, str)
                    self.assertTrue(label.strip(), f"{desc.key}: {value!r}")

    def test_choice_kind_iff_choices_declared(self) -> None:
        for desc in REGISTRY:
            if desc.kind == "choice":
                self.assertIsNotNone(desc.choices, desc.key)
            else:
                self.assertIsNone(
                    desc.choices, f"{desc.key}: choices on kind={desc.kind}"
                )

    def test_provider_names_resolve_to_module_functions(self) -> None:
        for desc in REGISTRY:
            if not isinstance(desc.choices, str):
                continue
            provider = getattr(schema, desc.choices, None)
            self.assertTrue(
                callable(provider),
                f"{desc.key}: provider {desc.choices!r} missing",
            )

    def test_default_value_is_always_pickable(self) -> None:
        """The Settings default for every choice field must be one of its
        choice values, or a fresh install opens the dialog on a value the
        picker can't show."""
        defaults = Settings()
        for desc in REGISTRY:
            if desc.kind != "choice":
                continue
            with self.subTest(key=desc.key):
                values = [v for v, _l in resolve_choices(desc)]
                self.assertIn(getattr(defaults, desc.key), values)

    def test_preview_implies_live_applier(self) -> None:
        # a preview applies through its live applier — no applier, no way
        # to preview (previews never commit)
        for desc in REGISTRY:
            if desc.preview:
                self.assertIsNotNone(
                    desc.live, f"{desc.key}: preview=True without live"
                )

    def test_companion_backdrops_wrap_follow_and_off(self) -> None:
        # the mini/fullscreen pickers compose both sentinels around the
        # real styles: follow leads, off trails
        from tide import backdrops
        rows = schema.companion_backdrop_choices()
        self.assertEqual(rows[0][0], backdrops.FOLLOW)
        self.assertEqual(rows[-1][0], backdrops.OFF)
        self.assertEqual(
            [v for v, _l in rows[1:-1]], list(backdrops.SLUGS)
        )

    def test_audio_device_choices_always_have_auto(self) -> None:
        rows = schema.audio_device_choices()
        self.assertEqual(rows[0][0], "")


# ---------- per-preset flags vs presets.STASH_FIELDS ----------

class PerPresetTests(unittest.TestCase):
    def test_per_preset_matches_stash_membership(self) -> None:
        # per_preset=True exactly when the field flips with the
        # personality — the "·per personality·" markers depend on it
        self.assertEqual(stash_membership_consistent(), [])

    def test_glyph_overrides_joined_stash_fields(self) -> None:
        self.assertIn("glyph_overrides", presets.STASH_FIELDS)

    def test_keymap_stays_global(self) -> None:
        # bindings are muscle memory — they must NOT flip with the preset
        self.assertNotIn("keymap", presets.STASH_FIELDS)

    def test_stash_fields_still_all_real_settings_fields(self) -> None:
        known = {f.name for f in fields(Settings)}
        for name in presets.STASH_FIELDS:
            self.assertIn(name, known, name)

    def test_restore_seeds_fresh_glyph_overrides_empty(self) -> None:
        # a never-visited personality has no stash; glyph_overrides must
        # come back as the default ({}), not None (PresetDef carries no
        # glyph_overrides attr — falls through to the dataclass default)
        s = Settings()
        s.glyph_overrides = {"play": "!"}
        presets.restore(s, "modern")
        self.assertEqual(s.glyph_overrides, {})

    def test_flip_round_trips_glyph_overrides(self) -> None:
        s = Settings()
        s.preset = "brutalist"
        s.glyph_overrides = {"play": ">", "pause": "||"}
        presets.stash(s)
        presets.restore(s, "modern")
        self.assertEqual(s.glyph_overrides, {})
        s.glyph_overrides = {"like_on": "★"}
        presets.stash(s)
        presets.restore(s, "brutalist")
        self.assertEqual(s.glyph_overrides, {"play": ">", "pause": "||"})
        presets.restore(s, "modern")
        self.assertEqual(s.glyph_overrides, {"like_on": "★"})


# ---------- live appliers ----------

class LiveApplierTests(unittest.TestCase):
    def test_live_names_exist_on_mainwindow_or_are_pending(self) -> None:
        """Every ``live`` name must be a real MainWindow method or sit in
        the PENDING_LIVE_APPLIERS allowlist. Checked against the class —
        appliers are methods, no window build needed."""
        from tide.ui.window import MainWindow
        for desc in REGISTRY:
            if desc.live is None:
                continue
            with self.subTest(key=desc.key):
                self.assertTrue(desc.live.isidentifier(), desc.live)
                self.assertTrue(
                    hasattr(MainWindow, desc.live)
                    or desc.live in PENDING_LIVE_APPLIERS,
                    f"{desc.key}: live applier {desc.live!r} is neither a "
                    "MainWindow method nor in PENDING_LIVE_APPLIERS",
                )

    def test_no_dead_allowlist_entries(self) -> None:
        # every pending applier must be referenced — a stale entry means
        # the allowlist drifted
        used = {d.live for d in REGISTRY if d.live is not None}
        dead = sorted(set(PENDING_LIVE_APPLIERS) - used)
        self.assertEqual(dead, [])

    def test_pending_appliers_are_documented(self) -> None:
        for name, doc in PENDING_LIVE_APPLIERS.items():
            self.assertTrue(name.isidentifier(), name)
            self.assertTrue(doc.strip(), f"{name}: undocumented")


# ---------- the two new Settings fields ----------

class _SandboxedCase(unittest.TestCase):
    """Per-test settings.toml — the module reads config.SETTINGS_FILE at
    call time (same pattern as test_preset_settings.py)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-schema-")
        self._real_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"

    def tearDown(self) -> None:
        config.SETTINGS_FILE = self._real_file
        self._tmp.cleanup()


class NewFieldsTests(_SandboxedCase):
    def test_defaults(self) -> None:
        s = Settings()
        self.assertEqual(s.keymap, {})
        self.assertEqual(s.glyph_overrides, {})

    def test_dict_defaults_are_per_instance(self) -> None:
        a, b = Settings(), Settings()
        a.keymap["play_pause"] = "Space"
        a.glyph_overrides["play"] = ">"
        self.assertEqual(b.keymap, {})
        self.assertEqual(b.glyph_overrides, {})

    def test_round_trip_through_save_load(self) -> None:
        s = Settings()
        s.keymap = {"play_pause": "Space", "refresh_session": "Ctrl+Shift+R"}
        s.glyph_overrides = {"play": "▶", "pause": "||", "like_on": "★"}
        settings_module.save(s)
        back = settings_module.load()
        self.assertEqual(back.keymap, s.keymap)
        self.assertEqual(back.glyph_overrides, s.glyph_overrides)

    def test_save_fields_scopes_to_the_named_dict(self) -> None:
        base = Settings()
        base.volume = 55
        settings_module.save(base)
        s = settings_module.load()
        s.keymap = {"play_pause": "P"}
        s.volume = 99   # deliberately NOT named — must not land
        settings_module.save_fields(s, "keymap")
        back = settings_module.load()
        self.assertEqual(back.keymap, {"play_pause": "P"})
        self.assertEqual(back.volume, 55)

    def test_stale_spotify_client_id_key_is_dropped_quietly(self) -> None:
        # a 1.x file still carries the deleted key; load() must shrug it
        # off like any unknown key
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text(
            'theme = "gruvbox"\n'
            'volume = 33\n'
            'spotify_client_id = "abcdef123456"\n',
            encoding="utf-8",
        )
        back = settings_module.load()   # must not raise
        self.assertEqual(back.volume, 33)
        self.assertFalse(hasattr(back, "spotify_client_id"))


if __name__ == "__main__":
    unittest.main()
