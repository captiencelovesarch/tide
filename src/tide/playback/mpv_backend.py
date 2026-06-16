"""MpvBackend — wraps the existing `Player` so URL/file-path payloads play
through libmpv. Used by all yt-dlp-backed sources (YT Music, SoundCloud,
Bandcamp, Mixcloud) and Local.
"""
from __future__ import annotations

from PySide6.QtCore import QObject

from ..player import PlayState, Player
from .base import PlaybackBackend


class MpvBackend(PlaybackBackend):
    slug = "mpv"

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = Player(parent=self)
        # Forward all signals — same surface, same payload types.
        self._player.state_changed.connect(self.state_changed)
        self._player.position_changed.connect(self.position_changed)
        self._player.duration_changed.connect(self.duration_changed)
        self._player.ended.connect(self.ended)
        self._player.error.connect(self.error)

    def load(self, payload: str) -> None:
        self._player.load_url(payload)

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle(self) -> None:
        self._player.toggle()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, seconds: float) -> None:
        self._player.seek(seconds)

    def set_volume(self, percent: int) -> None:
        self._player.set_volume(percent)

    def set_speed(self, value: float) -> None:
        self._player.set_speed(value)

    def set_pitch_correction(self, enabled: bool) -> None:
        self._player.set_pitch_correction(enabled)

    def shutdown(self) -> None:
        self._player.shutdown()

    @property
    def state(self) -> PlayState:
        return self._player.state

    @property
    def duration(self) -> float:
        return self._player.duration

    @property
    def player(self) -> Player:
        """Escape hatch for code that still talks to mpv directly (e.g. the
        audio capture / visualizer wiring). Should fade out in v1.3."""
        return self._player
