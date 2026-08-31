"""Phase 3 asset drop — bundled IBM Plex Sans, the modern sound pack,
sound-pack selection in UiSoundPlayer, and the recursive packaging globs.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/

The wheel-build test carries the ``packaging`` marker (it shells out to
python -m build) and skips itself when python-build isn't installed.
"""
import contextlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "tide"
SOUND_FILES = (
    "nav.wav", "back.wav", "modal_open.wav", "modal_close.wav",
    "toggle_on.wav", "toggle_off.wav",
)
PLEX_SANS_WEIGHTS = ("Regular", "Medium", "SemiBold", "Bold")


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


# ---------- fonts ----------


class PlexSansBundleTests(unittest.TestCase):
    def test_exactly_the_four_latin_weights_bundled(self) -> None:
        """The plain latin family only — no Hebrew/Arabic/Condensed/Italic
        variants sneaking the wheel size up."""
        bundled = sorted(p.name for p in PKG.glob("fonts/IBMPlexSans-*.ttf"))
        self.assertEqual(
            bundled,
            sorted(f"IBMPlexSans-{w}.ttf" for w in PLEX_SANS_WEIGHTS),
        )

    def test_register_bundled_fonts_resolves_plex_sans(self) -> None:
        _app()
        from tide import theming
        theming.register_bundled_fonts()
        self.assertIn("IBM Plex Sans", QFontDatabase.families())

    def test_bundled_files_carry_the_family_name(self) -> None:
        """System-independent: each bundled TTF itself must report the
        'IBM Plex Sans' typographic family when registered — the resolve
        test above could otherwise pass off a system ttf-ibm-plex."""
        _app()
        for weight in PLEX_SANS_WEIGHTS:
            path = PKG / "fonts" / f"IBMPlexSans-{weight}.ttf"
            fid = QFontDatabase.addApplicationFont(str(path))
            self.assertGreaterEqual(fid, 0, path.name)
            self.assertIn(
                "IBM Plex Sans",
                QFontDatabase.applicationFontFamilies(fid),
                path.name,
            )


# ---------- the modern sound pack ----------


class ModernSoundPackTests(unittest.TestCase):
    """Every clip honors the sounds/README.md format contract."""

    def _read(self, name: str):
        path = PKG / "sounds" / "modern" / name
        self.assertTrue(path.is_file(), f"missing {path}")
        with contextlib.closing(wave.open(str(path))) as w:
            meta = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            frames = w.getnframes()
            raw = np.frombuffer(w.readframes(frames), dtype="<i2")
        return meta, frames, raw.astype(float) / 32768.0

    def test_all_six_keys_present(self) -> None:
        present = sorted(p.name for p in PKG.glob("sounds/modern/*.wav"))
        self.assertEqual(present, sorted(SOUND_FILES))

    def test_format_contract(self) -> None:
        for name in SOUND_FILES:
            with self.subTest(sound=name):
                (channels, sampwidth, rate), frames, data = self._read(name)
                self.assertEqual(channels, 1, "mono")
                self.assertEqual(sampwidth, 2, "16-bit")
                self.assertEqual(rate, 44100)
                dur_ms = frames / rate * 1000
                self.assertGreaterEqual(dur_ms, 30)
                self.assertLessEqual(dur_ms, 150)
                peak_db = 20 * np.log10(np.abs(data).max())
                self.assertGreaterEqual(peak_db, -6.5, "peak ~-6 dBFS")
                self.assertLessEqual(peak_db, -5.5, "peak ~-6 dBFS")

    def test_no_click_at_the_edges(self) -> None:
        """First/last samples at (near) zero — a hard edge through pw-play
        is exactly the kind of tick this pack exists to avoid."""
        for name in SOUND_FILES:
            with self.subTest(sound=name):
                _meta, _frames, data = self._read(name)
                self.assertLess(abs(data[0]), 0.01)
                self.assertLess(abs(data[-1]), 0.01)


# ---------- pack selection ----------


class SetPackTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def _player(self, sounds_dir=None):
        from tide.ui_sounds import UiSoundPlayer
        return UiSoundPlayer(sounds_dir=sounds_dir)

    def test_default_pack_resolves_package_dir(self) -> None:
        player = self._player()
        self.assertEqual(player.pack, "default")
        self.assertEqual(player._sounds["nav"], PKG / "sounds" / "nav.wav")

    def test_modern_pack_resolves_every_key(self) -> None:
        player = self._player()
        player.set_pack("modern")
        self.assertEqual(player.pack, "modern")
        for key in ("nav", "back", "modal_open", "modal_close",
                    "toggle_on", "toggle_off"):
            self.assertEqual(
                player._sounds[key], PKG / "sounds" / "modern" / f"{key}.wav",
                key,
            )

    def test_partial_pack_falls_back_per_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "nav.wav").write_bytes(b"x")
            (base / "back.wav").write_bytes(b"x")
            (base / "half").mkdir()
            (base / "half" / "back.wav").write_bytes(b"x")
            player = self._player(sounds_dir=base)
            player.set_pack("half")
            self.assertEqual(player._sounds["back"], base / "half" / "back.wav")
            self.assertEqual(player._sounds["nav"], base / "nav.wav")
            self.assertNotIn("toggle_on", player._sounds)

    def test_unknown_pack_falls_back_entirely(self) -> None:
        player = self._player()
        player.set_pack("no-such-pack")
        self.assertEqual(player._sounds["nav"], PKG / "sounds" / "nav.wav")

    def test_switching_back_to_default(self) -> None:
        player = self._player()
        player.set_pack("modern")
        player.set_pack("default")
        self.assertEqual(player.pack, "default")
        self.assertEqual(player._sounds["nav"], PKG / "sounds" / "nav.wav")

    def test_empty_name_means_default(self) -> None:
        player = self._player()
        player.set_pack("")
        self.assertEqual(player.pack, "default")


class PresetSoundPackTests(unittest.TestCase):
    def test_modern_builtin_uses_modern_pack(self) -> None:
        from tide.presets import BUILTINS
        self.assertEqual(BUILTINS["modern"].sound_pack, "modern")

    def test_brutalist_stays_default(self) -> None:
        from tide.presets import BUILTINS
        self.assertEqual(BUILTINS["brutalist"].sound_pack, "default")


# ---------- the wheel ----------


@pytest.mark.packaging
@unittest.skipUnless(
    importlib.util.find_spec("build") is not None,
    "python-build not installed",
)
class WheelContentsTests(unittest.TestCase):
    """Build a real wheel and assert the assets actually land in it —
    the 1.2.4 lesson (UI sounds silently dead on every install because
    assets/ was never packaged) gets a permanent regression bar."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="tide-wheel-")
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--no-isolation",
             "--outdir", cls._tmp.name],
            cwd=REPO, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            cls._tmp.cleanup()
            raise AssertionError(
                f"wheel build failed:\n{result.stdout}\n{result.stderr}"
            )
        wheels = list(Path(cls._tmp.name).glob("*.whl"))
        assert len(wheels) == 1, wheels
        with zipfile.ZipFile(wheels[0]) as zf:
            cls.names = zf.namelist()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_plex_sans_fonts_in_wheel(self) -> None:
        for weight in PLEX_SANS_WEIGHTS:
            self.assertIn(f"tide/fonts/IBMPlexSans-{weight}.ttf", self.names)

    def test_preexisting_fonts_still_in_wheel(self) -> None:
        for name in ("IBMPlexMono-Regular.ttf", "Inter-Variable.ttf",
                     "JetBrainsMono-Regular.ttf"):
            self.assertIn(f"tide/fonts/{name}", self.names)

    def test_modern_sounds_in_wheel(self) -> None:
        for name in SOUND_FILES:
            self.assertIn(f"tide/sounds/modern/{name}", self.names)

    def test_default_sounds_still_in_wheel(self) -> None:
        for name in SOUND_FILES:
            self.assertIn(f"tide/sounds/{name}", self.names)

    def test_sound_readmes_in_wheel(self) -> None:
        self.assertIn("tide/sounds/README.md", self.names)
        self.assertIn("tide/sounds/modern/README.md", self.names)

    def test_themes_dir_nonempty(self) -> None:
        themed = [n for n in self.names if n.startswith("tide/themes/")]
        self.assertTrue(themed, "themes/ must ship in the wheel")

    def test_shared_base_qss_in_wheel(self) -> None:
        # After the C split every bundled theme.qss is gone and the whole
        # stylesheet lives in this one file — at a depth (themes/_base.qss)
        # the 16 theme.toml files would happily satisfy a "non-empty"
        # check without. Losing it ships 16 themes with no QSS at all.
        self.assertIn("tide/themes/_base.qss", self.names)

    def test_every_bundled_theme_ships_its_toml(self) -> None:
        tomls = [n for n in self.names
                 if n.startswith("tide/themes/") and n.endswith("/theme.toml")]
        self.assertGreaterEqual(len(tomls), 16, "bundled themes went missing")


if __name__ == "__main__":
    unittest.main()
