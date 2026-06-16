"""Headless mpv wrapper exposing Qt signals.

mpv's callbacks fire on its own threads; we marshal everything to the GUI
thread via QueuedConnection-friendly Signals so widgets can bind directly.
"""
from __future__ import annotations

import locale
from enum import Enum

import mpv
from PySide6.QtCore import QObject, Signal, Slot


def _force_c_numeric_locale() -> None:
    """mpv aborts if LC_NUMERIC is non-C. Qt and ytmusicapi both reset it,
    so we defensively re-pin it right before constructing libmpv."""
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass


class PlayState(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


class Player(QObject):
    state_changed = Signal(PlayState)
    position_changed = Signal(float)     # seconds
    duration_changed = Signal(float)     # seconds
    ended = Signal()
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        _force_c_numeric_locale()
        self._state = PlayState.IDLE
        self._duration = 0.0
        self._mpv = mpv.MPV(
            ytdl=False,
            video=False,
            audio_display=False,
            vid="no",
            input_default_bindings=False,
            input_vo_keyboard=False,
            cache="yes",
            demuxer_max_bytes=str(50 * 1024 * 1024),
            audio_client_name="tide",
        )

        # property observers — mpv calls these on its own thread
        @self._mpv.property_observer("time-pos")
        def _on_time(_name, value):
            if value is not None:
                self.position_changed.emit(float(value))

        @self._mpv.property_observer("duration")
        def _on_dur(_name, value):
            if value is not None and value != self._duration:
                self._duration = float(value)
                self.duration_changed.emit(self._duration)

        @self._mpv.property_observer("pause")
        def _on_pause(_name, value):
            if self._state in (PlayState.PLAYING, PlayState.PAUSED):
                new = PlayState.PAUSED if value else PlayState.PLAYING
                self._set_state(new)

        @self._mpv.property_observer("core-idle")
        def _on_idle(_name, value):
            # core-idle goes False when actually playing audio
            if self._state == PlayState.LOADING and value is False:
                self._set_state(PlayState.PLAYING)

        # MpvEventEndFile reasons: EOF=0, RESTARTED=1, ABORTED=2, QUIT=3, ERROR=4, REDIRECT=5
        _END_EOF = mpv.MpvEventEndFile.EOF
        _END_ERROR = mpv.MpvEventEndFile.ERROR

        @self._mpv.event_callback("end-file")
        def _on_end(event):
            reason = None
            try:
                reason = int(event.data.reason)
            except Exception:
                pass
            if reason == _END_EOF:
                self._set_state(PlayState.IDLE)
                self.ended.emit()
            elif reason == _END_ERROR:
                self._set_state(PlayState.IDLE)
                self.error.emit("playback error")
            # Other reasons (RESTARTED, ABORTED, QUIT, REDIRECT) are bookkeeping
            # noise from loadfile-replace or shutdown — don't signal end-of-track.

    # ---------- public api ----------

    @Slot(str)
    def load_url(self, url: str) -> None:
        self._duration = 0.0
        self._set_state(PlayState.LOADING)
        # Use the high-level helper — mpv >=0.38 needs 4 positional args
        # (filename, mode, index, options) which python-mpv handles internally.
        self._mpv.loadfile(url, mode="replace")
        self._mpv["pause"] = False

    @Slot()
    def play(self) -> None:
        if self._state == PlayState.PAUSED:
            self._mpv["pause"] = False

    @Slot()
    def pause(self) -> None:
        if self._state == PlayState.PLAYING:
            self._mpv["pause"] = True

    @Slot()
    def toggle(self) -> None:
        if self._state == PlayState.PLAYING:
            self.pause()
        elif self._state == PlayState.PAUSED:
            self.play()

    @Slot()
    def stop(self) -> None:
        self._mpv.command("stop")
        self._set_state(PlayState.IDLE)

    @Slot(float)
    def seek(self, seconds: float) -> None:
        try:
            self._mpv.command("seek", str(seconds), "absolute")
        except Exception:
            pass

    @Slot(int)
    def set_volume(self, percent: int) -> None:
        self._mpv["volume"] = max(0, min(100, percent))

    @Slot(float)
    def set_speed(self, value: float) -> None:
        """Change playback speed. With pitch-correction disabled (the
        default for tide's slowed/sped aesthetic), this also shifts pitch.
        Clamped to 0.25–4.0 — mpv accepts wider but anything outside this
        is unintelligible."""
        try:
            self._mpv["speed"] = max(0.25, min(4.0, float(value)))
        except Exception:
            pass

    @Slot(bool)
    def set_pitch_correction(self, enabled: bool) -> None:
        """Toggle mpv's scaletempo audio filter. When False (the default),
        speed changes shift pitch — the 'slowed + reverb' / 'nightcore' look.
        When True, pitch is preserved (utility mode for audiobooks etc.)."""
        try:
            self._mpv["audio-pitch-correction"] = bool(enabled)
        except Exception:
            pass

    def shutdown(self) -> None:
        try:
            self._mpv.terminate()
        except Exception:
            pass

    # ---------- state ----------

    @property
    def state(self) -> PlayState:
        return self._state

    @property
    def duration(self) -> float:
        return self._duration

    def _set_state(self, s: PlayState) -> None:
        if s != self._state:
            self._state = s
            self.state_changed.emit(s)
