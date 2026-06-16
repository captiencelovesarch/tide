"""Playback router: dispatch audio commands to whichever backend handles
the active track.

The router presents the same surface as `tide.player.Player` (load_url,
play, pause, toggle, seek, set_volume + signals) so existing window wiring
keeps compiling. Internally each registered backend's signals are wired to
the router's own, so position/duration/state events bubble up regardless of
which backend is active.

For v1.2.0 only `MpvBackend` is registered. The structure is in place for
v1.2.1 (LibrespotBackend) and v1.2.2 (MusicKitBackend) without further
changes to call sites.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from ..player import PlayState
from .base import PlaybackBackend
from .mpv_backend import MpvBackend


class PlaybackRouter(QObject):
    state_changed = Signal(object)
    position_changed = Signal(float)
    duration_changed = Signal(float)
    ended = Signal()
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backends: dict[str, PlaybackBackend] = {}
        self._active: PlaybackBackend | None = None
        self._volume: int = 80
        # Playback speed (mpv `speed` property) + whether mpv's scaletempo
        # filter preserves pitch. Initialized to defaults; app startup pushes
        # the persisted values from Settings after construction.
        self._speed: float = 1.0
        self._pitch_correction: bool = False

    def register(self, backend: PlaybackBackend) -> None:
        slug = backend.slug
        if not slug:
            raise ValueError("backend missing slug")
        self._backends[slug] = backend
        backend.state_changed.connect(self._on_state)
        backend.position_changed.connect(self._on_position)
        backend.duration_changed.connect(self._on_duration)
        backend.ended.connect(self._on_ended)
        backend.error.connect(self._on_error)
        try:
            backend.set_volume(self._volume)
        except Exception:
            pass
        # Push the current speed + pitch policy so backends registered after
        # app startup (rare but possible — e.g. a hot-plugged source) match
        # the user's settings without a manual nudge.
        try:
            backend.set_speed(self._speed)
        except Exception:
            pass
        try:
            backend.set_pitch_correction(self._pitch_correction)
        except Exception:
            pass
        # First registered backend becomes the implicit default (mpv).
        if self._active is None:
            self._active = backend

    def backend(self, slug: str) -> PlaybackBackend | None:
        return self._backends.get(slug)

    # ---------- Player-compatible surface ----------

    @Slot(str)
    def load_url(self, url: str) -> None:
        """Back-compat load. v1.1 code paths assume mpv + URL — keep working."""
        self._activate("mpv")
        backend = self._backends.get("mpv")
        if backend is None:
            self.error.emit("no mpv backend registered")
            return
        backend.load(url)

    def load_ref(self, ref) -> None:
        """Load a `StreamRef` — dispatches to the named backend."""
        backend = self._backends.get(ref.backend)
        if backend is None:
            self.error.emit(f"no backend for {ref.backend!r}")
            return
        self._activate(ref.backend)
        backend.load(ref.payload)

    @Slot()
    def play(self) -> None:
        if self._active is not None:
            self._active.play()

    @Slot()
    def pause(self) -> None:
        if self._active is not None:
            self._active.pause()

    @Slot()
    def toggle(self) -> None:
        if self._active is None:
            return
        st = self._active.state
        if st == PlayState.PLAYING:
            self._active.pause()
        elif st == PlayState.PAUSED:
            self._active.play()
        else:
            self._active.play()

    @Slot()
    def stop(self) -> None:
        if self._active is not None:
            self._active.stop()

    @Slot(float)
    def seek(self, seconds: float) -> None:
        if self._active is not None:
            self._active.seek(seconds)

    @Slot(int)
    def set_volume(self, percent: int) -> None:
        self._volume = max(0, min(100, percent))
        for b in self._backends.values():
            try:
                b.set_volume(self._volume)
            except Exception:
                pass

    @Slot(float)
    def set_speed(self, value: float) -> None:
        """Apply to every backend so a speed-change made before a backend
        becomes active still takes effect when it does. Backends that don't
        support speed simply no-op."""
        self._speed = max(0.25, min(4.0, float(value)))
        for b in self._backends.values():
            try:
                b.set_speed(self._speed)
            except Exception:
                pass

    @Slot(bool)
    def set_pitch_correction(self, enabled: bool) -> None:
        self._pitch_correction = bool(enabled)
        for b in self._backends.values():
            try:
                b.set_pitch_correction(self._pitch_correction)
            except Exception:
                pass

    def shutdown(self) -> None:
        for b in self._backends.values():
            try:
                b.shutdown()
            except Exception:
                pass

    # ---------- introspection ----------

    @property
    def state(self) -> PlayState:
        return self._active.state if self._active is not None else PlayState.IDLE

    @property
    def duration(self) -> float:
        return self._active.duration if self._active is not None else 0.0

    @property
    def active_slug(self) -> str:
        for slug, b in self._backends.items():
            if b is self._active:
                return slug
        return ""

    # ---------- internals ----------

    def _activate(self, slug: str) -> None:
        new = self._backends.get(slug)
        if new is None or new is self._active:
            return
        # Pause whoever was playing so we don't get double audio when the
        # other backend keeps streaming silently.
        if self._active is not None:
            try:
                self._active.stop()
            except Exception:
                pass
        self._active = new

    def _on_state(self, st) -> None:
        if self.sender() is self._active:
            self.state_changed.emit(st)

    def _on_position(self, secs: float) -> None:
        if self.sender() is self._active:
            self.position_changed.emit(secs)

    def _on_duration(self, secs: float) -> None:
        if self.sender() is self._active:
            self.duration_changed.emit(secs)

    def _on_ended(self) -> None:
        if self.sender() is self._active:
            self.ended.emit()

    def _on_error(self, msg: str) -> None:
        if self.sender() is self._active:
            self.error.emit(msg)


__all__ = ["MpvBackend", "PlaybackBackend", "PlaybackRouter"]
