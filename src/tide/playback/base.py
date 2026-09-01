"""PlaybackBackend abstraction.

A backend handles audio for one or more sources. v1.2.0 ships ``MpvBackend``
only (used by all 5 zero-DRM sources). v1.2.1 adds ``LibrespotBackend`` for
Spotify; v1.2.2 adds ``MusicKitBackend`` for Apple Music.

Backends expose the same signal surface as `tide.player.Player` so the
`PlaybackRouter` can re-emit transparently and existing window wiring keeps
working.
"""
from __future__ import annotations

from abc import abstractmethod

from PySide6.QtCore import QObject, Signal


class PlaybackBackend(QObject):
    state_changed = Signal(object)        # PlayState
    position_changed = Signal(float)      # seconds
    duration_changed = Signal(float)      # seconds
    ended = Signal()
    error = Signal(str)

    slug: str = ""

    # Whether ``set_speed`` is honored. Backends rendering audio outside
    # our pipeline (librespot streams server-decoded PCM) flip this to
    # False so the UI greys the speed control instead of no-opping silently.
    supports_speed: bool = True

    @abstractmethod
    def load(self, payload: str) -> None: ...

    @abstractmethod
    def play(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def seek(self, seconds: float) -> None: ...

    @abstractmethod
    def set_volume(self, percent: int) -> None: ...

    # Playback speed + pitch handling. Default no-ops so backends that don't
    # support variable speed (future Librespot / MusicKit) get a graceful
    # fallback — the UI's SpeedButton calls these unconditionally.
    def set_speed(self, value: float) -> None:
        return None

    def set_pitch_correction(self, enabled: bool) -> None:
        return None

    # Audio FX filter chain — backends that own their own audio path
    # (librespot streams pre-rendered to its sink, MusicKit ditto) just
    # ignore the chain string. Only MpvBackend actually applies it.
    def set_audio_filter_chain(self, chain: str) -> None:
        return None

    def capture_suspect_window(self) -> float:
        """Wall seconds of just-captured audio that may belong to the NEXT
        track. Nonzero only right after a poll-based backend noticed an
        external track change late; the ambient pulse recorder trims this
        much before saving, so the old track's map can't learn the new
        track's bass."""
        return 0.0

    def shutdown(self) -> None:
        return None

    @property
    @abstractmethod
    def state(self): ...

    @property
    @abstractmethod
    def duration(self) -> float: ...
