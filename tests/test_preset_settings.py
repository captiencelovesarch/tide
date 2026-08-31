"""v2.0 "two tides" settings — preset fields, nested toml sub-tables,
field-scoped save_fields, and the one-shot v1 downgrade backup.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import tempfile
import tomllib
import unittest
from dataclasses import fields
from pathlib import Path

from tide import config
from tide.settings import (
    Settings,
    _backup_path,
    _to_toml,
    ensure_v1_backup,
    load,
    save,
    save_fields,
)


class _SandboxedCase(unittest.TestCase):
    """Every test gets its own settings.toml — the module reads
    config.SETTINGS_FILE at call time, so pointing it at a fresh temp dir
    keeps tests from leaking into each other (or into the run-wide
    conftest sandbox that other test files share)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tide-preset-settings-")
        self._real_file = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._tmp.name) / "settings.toml"

    def tearDown(self) -> None:
        config.SETTINGS_FILE = self._real_file
        self._tmp.cleanup()

    def _v1_path(self) -> Path:
        return config.SETTINGS_FILE.with_name("settings.toml.v1.bak")


class PresetFieldDefaultsTests(_SandboxedCase):
    def test_defaults(self) -> None:
        s = Settings()
        self.assertEqual(s.preset, "")
        self.assertFalse(s.preset_chosen)
        self.assertEqual(s.preset_state, {})
        self.assertEqual(s.window_sizes, {})

    def test_dict_defaults_are_per_instance(self) -> None:
        a, b = Settings(), Settings()
        a.preset_state["brutalist"] = {"theme": "brutalist-mono"}
        a.window_sizes["classic"] = [1280, 800]
        self.assertEqual(b.preset_state, {})
        self.assertEqual(b.window_sizes, {})

    def test_pre_20_config_gets_defaults(self) -> None:
        # A file written by 1.6 has none of the preset keys — load()'s
        # filter path must fall back to the dataclass defaults.
        raw = {"theme": "dark", "volume": 42}
        known = {f.name for f in fields(Settings)}
        back = Settings(**{k: v for k, v in raw.items() if k in known})
        self.assertEqual(back.preset, "")
        self.assertFalse(back.preset_chosen)
        self.assertEqual(back.preset_state, {})


class NestedTomlTests(_SandboxedCase):
    def test_preset_state_round_trips_through_save_load(self) -> None:
        s = Settings()
        s.preset = "modern"
        s.preset_chosen = True
        s.preset_state = {
            "brutalist": {
                "theme": "brutalist-mono",
                "layout": "classic",
                "layout_overrides": {"strip": "bottom", "rail_width": 168},
                "motion": "off",
                "font_family_override": "",
                "font_size_override_pt": 0,
                "adaptive_accent": False,
                "mini_pulse": False,
            },
            "modern": {
                "theme": "adaptive",
                "motion": "full",
                "layout_overrides": {},
                "adaptive_accent": True,
                "playback_speed_marks": [0.85, 1.0, 1.25],
            },
        }
        s.window_sizes = {"classic": [1280, 800], "focus": [900, 620]}
        save(s)
        back = load()
        self.assertEqual(back.preset, "modern")
        self.assertTrue(back.preset_chosen)
        self.assertEqual(back.preset_state, s.preset_state)
        self.assertEqual(back.window_sizes, s.window_sizes)
        # Ints stay ints through the trip (tomllib types, not strings).
        self.assertIsInstance(
            back.preset_state["brutalist"]["layout_overrides"]["rail_width"], int
        )
        self.assertIsInstance(back.window_sizes["classic"][0], int)

    def test_output_is_hand_readable_sub_tables(self) -> None:
        s = Settings()
        s.preset_state = {"brutalist": {"theme": "brutalist-mono"}}
        s.window_sizes = {"classic": [1280, 800]}
        text = _to_toml(s)
        self.assertIn("\n[preset_state.brutalist]\n", text)
        self.assertIn('theme = "brutalist-mono"', text)
        self.assertIn("classic = [1280, 800]", text)

    def test_empty_dict_fields_serialize_and_parse(self) -> None:
        s = Settings()   # preset_state and window_sizes both default {}
        text = _to_toml(s)
        self.assertIn("[preset_state]", text)
        self.assertIn("[window_sizes]", text)
        save(s)
        back = load()
        self.assertEqual(back.preset_state, {})
        self.assertEqual(back.window_sizes, {})

    def test_mixed_scalar_and_dict_entries_keep_valid_ordering(self) -> None:
        # Scalars inside a table must land BEFORE any [field.sub] header,
        # whatever the insertion order — toml assigns everything after a
        # sub-header to that sub-table.
        s = Settings()
        s.preset_state = {
            "brutalist": {"theme": "brutalist-mono"},   # dict first
            "note": "scalar-after-dict",
        }
        raw = tomllib.loads(_to_toml(s))
        self.assertEqual(raw["preset_state"]["note"], "scalar-after-dict")
        self.assertEqual(
            raw["preset_state"]["brutalist"], {"theme": "brutalist-mono"}
        )

    def test_awkward_keys_get_quoted(self) -> None:
        s = Settings()
        s.preset_state = {"my preset.v2": {"text case": "lower"}}
        s.layout_overrides = {"now playing": "wide"}
        raw = tomllib.loads(_to_toml(s))
        self.assertEqual(raw["preset_state"]["my preset.v2"]["text case"], "lower")
        self.assertEqual(raw["layout_overrides"]["now playing"], "wide")

    def test_nested_string_escaping_survives(self) -> None:
        s = Settings()
        s.preset_state = {"modern": {"font_family_override": 'Font "X" \\ Y'}}
        save(s)
        back = load()
        self.assertEqual(
            back.preset_state["modern"]["font_family_override"], 'Font "X" \\ Y'
        )


class SaveFieldsTests(_SandboxedCase):
    def test_does_not_clobber_unrelated_disk_changes(self) -> None:
        base = Settings()
        base.volume = 55
        save(base)
        # Session A loaded a while ago and is about to persist preset state.
        a = load()
        a.preset = "brutalist"
        a.preset_state = {"brutalist": {"theme": "brutalist-mono"}}
        # Meanwhile session B saved a different volume to disk.
        b = load()
        b.volume = 23
        save(b)
        # A writes only its own fields — B's volume must survive.
        save_fields(a, "preset", "preset_state")
        back = load()
        self.assertEqual(back.volume, 23)
        self.assertEqual(back.preset, "brutalist")
        self.assertEqual(
            back.preset_state, {"brutalist": {"theme": "brutalist-mono"}}
        )

    def test_falls_back_to_full_save_when_disk_missing(self) -> None:
        s = Settings()
        s.preset = "modern"
        save_fields(s, "preset")
        back = load()
        self.assertEqual(back.preset, "modern")
        self.assertEqual(back.volume, Settings().volume)

    def test_merges_from_backup_when_main_corrupt(self) -> None:
        base = Settings()
        base.volume = 55
        save(base)   # writes main + .bak
        config.SETTINGS_FILE.write_text("not [ valid toml", encoding="utf-8")
        a = Settings()
        a.preset = "brutalist"
        save_fields(a, "preset")
        back = load()
        self.assertEqual(back.volume, 55, "merge base must come from the .bak")
        self.assertEqual(back.preset, "brutalist")

    def test_backup_mirror_matches_main_after_merge(self) -> None:
        save(Settings())
        s = Settings()
        s.preset = "modern"
        save_fields(s, "preset")
        main = config.SETTINGS_FILE.read_text(encoding="utf-8")
        bak = _backup_path(config.SETTINGS_FILE).read_text(encoding="utf-8")
        self.assertEqual(main, bak)

    def test_unknown_field_name_raises(self) -> None:
        save(Settings())
        with self.assertRaises(ValueError):
            save_fields(Settings(), "preset", "not_a_field")
        # And the typo must not have half-written anything.
        self.assertEqual(load().preset, "")

    def test_merge_over_pre_wizard_file_keeps_launch_stamp(self) -> None:
        # A pre-wizard file has no first_launch_complete key; the merge
        # must stamp it True (like load() does), not write false and
        # re-onboard the user.
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text('theme = "dark"\n', encoding="utf-8")
        s = Settings()
        s.preset = "modern"
        save_fields(s, "preset")
        self.assertTrue(load().first_launch_complete)


class EnsureV1BackupTests(_SandboxedCase):
    def test_copies_pre_preset_file_once(self) -> None:
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        original = 'theme = "dark"\nvolume = 40\n'
        config.SETTINGS_FILE.write_text(original, encoding="utf-8")
        ensure_v1_backup()
        self.assertEqual(self._v1_path().read_text(encoding="utf-8"), original)

    def test_second_call_never_overwrites(self) -> None:
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        original = 'theme = "dark"\nvolume = 40\n'
        config.SETTINGS_FILE.write_text(original, encoding="utf-8")
        ensure_v1_backup()
        # The 2.0 build now rewrites settings with preset fields...
        s = load()
        s.preset = "modern"
        save(s)
        ensure_v1_backup()
        # ...but the archived v1 copy stays frozen.
        self.assertEqual(self._v1_path().read_text(encoding="utf-8"), original)

    def test_no_backup_when_preset_key_present(self) -> None:
        # save() always writes the preset key (even as ""), so a file from
        # a 2.0 build must never get archived as "v1".
        save(Settings())
        ensure_v1_backup()
        self.assertFalse(self._v1_path().exists())

    def test_missing_file_is_a_noop(self) -> None:
        ensure_v1_backup()   # must not raise
        self.assertFalse(self._v1_path().exists())

    def test_corrupt_file_is_a_noop(self) -> None:
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text("not [ valid toml", encoding="utf-8")
        ensure_v1_backup()   # must not raise
        self.assertFalse(self._v1_path().exists())


class UnknownKeyFilterTests(_SandboxedCase):
    def test_load_still_drops_unknown_keys(self) -> None:
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text(
            'theme = "brutalist-mono"\n'
            'volume = 33\n'
            'bogus_scalar = "future field"\n'
            '\n'
            '[bogus_table]\n'
            'x = 1\n'
            '\n'
            '[preset_state.brutalist]\n'
            'theme = "brutalist-mono"\n',
            encoding="utf-8",
        )
        back = load()   # must not raise on the unknown keys
        self.assertEqual(back.volume, 33)
        self.assertFalse(hasattr(back, "bogus_scalar"))
        self.assertFalse(hasattr(back, "bogus_table"))
        # The known nested table still lands.
        self.assertEqual(
            back.preset_state, {"brutalist": {"theme": "brutalist-mono"}}
        )


if __name__ == "__main__":
    unittest.main()
