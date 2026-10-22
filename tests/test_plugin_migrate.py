"""Discord presence left core for the store in 3.0. Someone who had it set
up gets their settings carried into the plugin's file and an install
queued, once; a fresh config queues nothing; and uninstalling later
sticks. listenbrainz was dropped, not moved: its token queues nothing.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_plugin_migrate.py
"""
import tempfile
import tomllib
import unittest
from pathlib import Path

from tide import plugin_migrate

OLD = """
theme = "nord"
discord_enabled = true
discord_app_id = "123456"
discord_lyrics_enabled = true
discord_hidden = true
discord_activity_type = "playing"
listenbrainz_enabled = true
listenbrainz_token = "tok"
"""


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tide-migrate-"))
        self.settings = self.tmp / "settings.toml"
        self.sdir = self.tmp / "plugin-settings"
        self.pending = self.tmp / "plugins-pending.toml"

    def collect(self):
        return plugin_migrate.collect(self.settings, self.sdir, self.pending)

    def plugin_settings(self, slug):
        with open(self.sdir / f"{slug}.toml", "rb") as f:
            return tomllib.load(f)

    def test_a_discord_user_keeps_their_setup(self):
        self.settings.write_text(OLD)
        self.assertEqual(self.collect(), ["discord"])
        self.assertEqual(self.plugin_settings("discord"), {
            "app_id": "123456", "lyrics": True, "hidden": True, "activity_type": "playing"})
        p = plugin_migrate.pending(self.pending)
        self.assertEqual((p.install, p.moved), (["discord"], ["discord"]))
        # listenbrainz is gone for good, not a plugin: nothing to install
        self.assertFalse((self.sdir / "listenbrainz.toml").exists())

    def test_only_once(self):
        self.settings.write_text(OLD)
        self.collect()
        plugin_migrate.finished("discord", self.pending)
        # still in settings.toml (nothing saved it yet), but it's done
        self.assertEqual(self.collect(), [])
        self.assertEqual(plugin_migrate.pending(self.pending).install, [])

    def test_a_fresh_config_queues_nothing(self):
        self.settings.write_text('theme = "nord"\n')
        self.assertEqual(self.collect(), [])
        self.assertEqual(plugin_migrate.pending(self.pending).install, [])

    def test_existing_plugin_settings_win(self):
        self.settings.write_text(OLD)
        self.sdir.mkdir()
        (self.sdir / "discord.toml").write_text('app_id = "newer"\n')
        self.collect()
        self.assertEqual(self.plugin_settings("discord"), {"app_id": "newer"})

    def test_onboarding_queues_an_install(self):
        plugin_migrate.queue_install("discord", {"app_id": "123"},
                                     settings_dir=self.sdir, pending_file=self.pending)
        p = plugin_migrate.pending(self.pending)
        self.assertEqual((p.install, p.moved), (["discord"], []))
        self.assertEqual(self.plugin_settings("discord"), {"app_id": "123"})

    def test_sources_that_moved(self):
        self.settings.write_text('''
local_music_dir = "/home/me/Music"
local_auto_index = false

[sources_enabled]
ytmusic = true
soundcloud = true
local = true
''')
        self.assertEqual(self.collect(), ["extra-sources", "local-files"])
        self.assertEqual(self.plugin_settings("local-files"),
                         {"folder": "/home/me/Music", "watch": False})

    def test_sources_left_off_queue_nothing(self):
        self.settings.write_text('''
[sources_enabled]
ytmusic = true
soundcloud = false
local = true
''')
        # local was on but had no folder and wasn't the one you used
        self.assertEqual(self.collect(), [])

    def test_local_on_the_default_folder_counts_when_active(self):
        self.settings.write_text('''
active_source = "local"

[sources_enabled]
local = true
''')
        self.assertEqual(self.collect(), ["local-files"])

    def test_a_tile_you_could_see(self):
        self.settings.write_text('''
visualizer_layout = "split"
visualizer_tile_1 = "spectrum"
visualizer_tile_2 = "tapper"
visualizer_tapper_offset = 30
''')
        self.assertEqual(self.collect(), ["vis-tiles"])
        self.assertEqual(self.plugin_settings("vis-tiles"), {"tapper_offset": 30})

    def test_a_tile_off_screen_isnt_a_pick(self):
        # tide 2.3's default fourth slot was the halo; a stack never shows it
        self.settings.write_text('''
visualizer_layout = "stack"
visualizer_tile_4 = "halo"
''')
        self.assertEqual(self.collect(), [])

    def test_precise_timing(self):
        self.settings.write_text("lyrics_precise = true\n")
        self.assertEqual(self.collect(), ["lyric-timing"])

    def test_powertools_when_you_made_something(self):
        self.settings.write_text("[keymap]\nplay_pause = \"Ctrl+P\"\n")
        self.assertEqual(self.collect(), ["powertools"])

    def test_toasts(self):
        self.assertEqual(plugin_migrate.toast_for("discord", True, "discord presence"),
                         "discord presence is a plugin now. we installed it for you.")
        self.assertEqual(plugin_migrate.toast_for("discord", False, "discord presence"),
                         "installed discord presence")


if __name__ == "__main__":
    unittest.main()
