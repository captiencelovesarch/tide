"""UI sound dispatcher — clicks, modal pops, toggle chirps.

A single ``UiSoundPlayer`` instance owns paths to known sound files. Call
sites do ``window.ui_sounds.play("nav")`` — the dispatch is short-circuited
when the master toggle is off, when music is playing (set via ``set_muted``),
or when the key has no WAV registered.

WAVs live in ``tide/sounds/`` inside the package and ship with the wheel.
Sound packs are subdirectories of that dir: ``set_pack("modern")`` resolves
each key against ``sounds/modern/`` first and falls back to the default
pack per key, so a pack only has to author the sounds it wants to change.
``"default"`` (the initial pack) is the sounds dir itself. The loader is
lenient: any missing file silently disables that key, so the feature
degrades cleanly on a fresh checkout where the user hasn't authored every
sound yet.

Music-playing detection: ``app.py`` connects the playback router's
``state_changed`` signal to ``set_muted(state == PlayState.PLAYING)``,
so UI sounds reappear automatically the moment playback pauses / ends.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Slot


# All known sound keys with their on-disk filenames. Extending this is the
# only place a new event type needs to be declared — call sites just pass
# the string key.
SOUND_KEYS: dict[str, str] = {
    "nav":         "nav.wav",
    "back":        "back.wav",
    "modal_open":  "modal_open.wav",
    "modal_close": "modal_close.wav",
    "toggle_on":   "toggle_on.wav",
    "toggle_off":  "toggle_off.wav",
}

# Quieter than the music itself — the sounds are feedback, not events
# the user is listening to. 0.20 gives an audible click at normal system
# volumes without startling when speakers are loud.
DEFAULT_VOLUME = 0.20


def _default_sounds_dir() -> Path:
    """The bundled sounds dir. As of v1.2.4 the WAVs live inside the
    package (``tide/sounds``) so installed wheels actually ship them —
    the old repo-root ``assets/sounds`` was never packaged, which made UI
    sounds silently dead on every pacman/pip install. The walk-up is kept
    as a fallback for older checkouts."""
    here = Path(__file__).resolve()
    packaged = here.parent / "sounds"
    if packaged.is_dir():
        return packaged
    for parent in [here.parent, *here.parents]:
        candidate = parent / "assets" / "sounds"
        if candidate.is_dir():
            return candidate
    # Nothing found — return the packaged path anyway; the loader just
    # finds no files and every key no-ops.
    return packaged


class UiSoundPlayer(QObject):
    """Tiny WAV dispatcher.

    This deliberately avoids ``QSoundEffect``. On some QtMultimedia/PipeWire
    stacks, constructing QSoundEffect can segfault the process before Python
    can catch anything. Spawning pw-play/paplay is less fancy, but it keeps UI
    sounds optional and cannot take Tide down at startup.
    """

    def __init__(self, sounds_dir: Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = False
        self._muted = False
        self._volume = DEFAULT_VOLUME
        self._pack = "default"
        self._sounds: dict[str, Path] = {}
        self._sounds_dir = Path(sounds_dir) if sounds_dir else _default_sounds_dir()
        self._player = self._find_player()
        self._load()

    # ---------- loading ----------

    def _find_player(self) -> str | None:
        for name in ("pw-play", "paplay", "aplay"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def _load(self) -> None:
        """Register each WAV present. The active pack's file wins per key,
        the default pack fills the gaps, missing files are skipped."""
        self._sounds = {}
        pack_dir = None
        if self._pack and self._pack != "default":
            pack_dir = self._sounds_dir / self._pack
        for key, filename in SOUND_KEYS.items():
            if pack_dir is not None:
                packed = pack_dir / filename
                if packed.is_file():
                    self._sounds[key] = packed
                    continue
            path = self._sounds_dir / filename
            if not path.is_file():
                continue
            self._sounds[key] = path

    # ---------- knobs ----------

    @Slot(bool)
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @Slot(bool)
    def set_muted(self, muted: bool) -> None:
        """``True`` while music is playing so UI sounds don't layer
        over the audio the user is actually listening to."""
        self._muted = bool(muted)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    @Slot(str)
    def set_pack(self, name: str) -> None:
        """Switch the active sound pack. ``"default"`` (or empty) is the
        bundled sounds dir itself; any other name resolves against
        ``sounds/<name>/`` with per-key fallback to default. Unknown pack
        names just fall back everywhere — lenient like the rest of the
        loader."""
        self._pack = str(name or "default")
        self._load()

    # ---------- the API call sites use ----------

    def _command(self, path: Path) -> list[str] | None:
        if self._player is None:
            return None
        name = Path(self._player).name
        if name == "pw-play":
            return [self._player, "--volume", f"{self._volume:.3f}", str(path)]
        if name == "paplay":
            volume = str(round(self._volume * 65536))
            return [self._player, "--volume", volume, str(path)]
        return [self._player, str(path)]

    @Slot(str)
    def play(self, key: str) -> None:
        if not self._enabled or self._muted:
            return
        path = self._sounds.get(key)
        if path is None:
            return
        cmd = self._command(path)
        if cmd is None:
            return
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

    # ---------- introspection ----------

    def available_keys(self) -> list[str]:
        return list(self._sounds.keys())

    @property
    def pack(self) -> str:
        return self._pack

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def muted(self) -> bool:
        return self._muted


__all__ = ["UiSoundPlayer", "SOUND_KEYS", "DEFAULT_VOLUME"]
