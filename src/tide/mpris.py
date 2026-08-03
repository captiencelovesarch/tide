"""MPRIS2 service: org.mpris.MediaPlayer2.tide

Exposes the standard MediaPlayer2 + MediaPlayer2.Player interfaces over
the session bus so KDE Plasma / GNOME / waybar / playerctl / hardware
media keys can control tide and read its current state.

PySide6's QtDBus binding integrates with Qt's event loop natively, so no
threading or GLib mainloop is required. All callbacks run on the GUI
thread by default.

We register a single QObject at /org/mpris/MediaPlayer2 with two
adaptors attached. Each adaptor declares its D-Bus interface name via
the PySide6 ``ClassInfo`` decorator. Property changes are broadcast
manually via the standard ``org.freedesktop.DBus.Properties.PropertiesChanged``
signal whenever ``MprisService`` updates its state.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    ClassInfo,
    QObject,
    QTimer,
    Property,
    Signal,
    Slot,
)
from PySide6.QtDBus import (
    QDBusAbstractAdaptor,
    QDBusConnection,
    QDBusMessage,
    QDBusObjectPath,
    QDBusVariant,
)

if TYPE_CHECKING:
    from .api import Track
    from .player import Player
    from .queue import Queue

# The rate window we advertise over D-Bus mirrors what the in-app speed UI
# allows, so external clients can't push tide outside its own range. Guarded
# import: a UI-module failure must never take the MPRIS service down with it.
try:
    from .ui.speed import SPEED_MAX, SPEED_MIN
except Exception:  # pragma: no cover
    SPEED_MIN, SPEED_MAX = 0.5, 2.0


MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT_IFACE = "org.mpris.MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


# --------------- adaptors ---------------


@ClassInfo(**{"D-Bus Interface": MPRIS_ROOT_IFACE})
class _RootAdaptor(QDBusAbstractAdaptor):
    """org.mpris.MediaPlayer2 — application-level identity + control."""

    def __init__(self, service: "MprisService") -> None:
        super().__init__(service)
        self._service = service

    # ----- methods -----
    @Slot()
    def Raise(self) -> None:
        self._service.raise_window()

    @Slot()
    def Quit(self) -> None:
        self._service.quit_app()

    # ----- properties -----
    @Property(bool)
    def CanQuit(self) -> bool: return True

    @Property(bool)
    def CanRaise(self) -> bool: return True

    @Property(bool)
    def HasTrackList(self) -> bool: return False

    @Property(str)
    def Identity(self) -> str: return "tide"

    @Property(str)
    def DesktopEntry(self) -> str: return "tide"

    @Property("QStringList")
    def SupportedUriSchemes(self) -> list[str]: return ["http", "https"]

    @Property("QStringList")
    def SupportedMimeTypes(self) -> list[str]:
        return ["audio/mpeg", "audio/ogg", "audio/webm", "audio/aac", "audio/x-flac"]


@ClassInfo(**{"D-Bus Interface": MPRIS_PLAYER_IFACE})
class _PlayerAdaptor(QDBusAbstractAdaptor):
    """org.mpris.MediaPlayer2.Player — playback state + control."""

    Seeked = Signal("qlonglong")

    def __init__(self, service: "MprisService") -> None:
        super().__init__(service)
        self._service = service

    # ----- methods -----
    @Slot()
    def Next(self) -> None: self._service.on_next()

    @Slot()
    def Previous(self) -> None: self._service.on_previous()

    @Slot()
    def Pause(self) -> None: self._service.on_pause()

    @Slot()
    def PlayPause(self) -> None: self._service.on_play_pause()

    @Slot()
    def Stop(self) -> None: self._service.on_stop()

    @Slot()
    def Play(self) -> None: self._service.on_play()

    @Slot("qlonglong")
    def Seek(self, offset_us: int) -> None:
        self._service.on_seek_relative(offset_us / 1_000_000.0)

    @Slot(QDBusObjectPath, "qlonglong")
    def SetPosition(self, track_id: QDBusObjectPath, position_us: int) -> None:
        self._service.on_set_position(track_id.path(), position_us / 1_000_000.0)

    @Slot(str)
    def OpenUri(self, _uri: str) -> None:
        # tide doesn't accept external URIs; OpenUri is optional in the spec.
        pass

    # ----- properties -----
    @Property(str)
    def PlaybackStatus(self) -> str: return self._service.playback_status

    @Property(str)
    def LoopStatus(self) -> str: return self._service.loop_status
    @LoopStatus.setter
    def LoopStatus(self, value: str) -> None:
        self._service.on_set_loop_status(value)

    @Property(float)
    def Rate(self) -> float: return self._service.rate
    @Rate.setter
    def Rate(self, value: float) -> None:
        self._service.on_set_rate(value)

    @Property(bool)
    def Shuffle(self) -> bool: return self._service.shuffle
    @Shuffle.setter
    def Shuffle(self, value: bool) -> None:
        self._service.on_set_shuffle(value)

    @Property("QVariantMap")
    def Metadata(self) -> dict: return self._service.metadata

    @Property(float)
    def Volume(self) -> float: return self._service.volume
    @Volume.setter
    def Volume(self, value: float) -> None:
        self._service.on_set_volume(value)

    @Property("qlonglong")
    def Position(self) -> int: return self._service.position_us

    @Property(float)
    def MinimumRate(self) -> float: return SPEED_MIN

    @Property(float)
    def MaximumRate(self) -> float: return SPEED_MAX

    @Property(bool)
    def CanGoNext(self) -> bool: return self._service.can_go_next

    @Property(bool)
    def CanGoPrevious(self) -> bool: return self._service.can_go_previous

    @Property(bool)
    def CanPlay(self) -> bool: return self._service.has_track

    @Property(bool)
    def CanPause(self) -> bool: return self._service.has_track

    @Property(bool)
    def CanSeek(self) -> bool: return self._service.has_track

    @Property(bool)
    def CanControl(self) -> bool: return True


# --------------- service ---------------


def _safe_video_id_for_path(video_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in video_id) or "none"


class MprisService(QObject):
    """Root MPRIS2 object. Owns the two adaptors and tracks state."""

    def __init__(
        self,
        player: "Player",
        queue: "Queue",
        window,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._player = player
        self._queue = queue
        self._window = window

        # cached state we hand back to D-Bus
        self._current_track: Track | None = None
        self._duration_us: int = 0
        self._position_us: int = 0
        self._playback_status: str = "Stopped"
        # The window's volume widget we're currently hooked to (it can be
        # swapped at runtime when the user changes layout slots).
        self._volume_widget = None

        self._root = _RootAdaptor(self)
        self._player_adaptor = _PlayerAdaptor(self)
        self._bus = QDBusConnection.sessionBus()

        self._connected = False
        self._service_name = "org.mpris.MediaPlayer2.tide"

    # ---------- lifecycle ----------

    def start(self) -> bool:
        if not self._bus.isConnected():
            return False
        # Register the object first so methods are reachable the moment the
        # bus name is acquired.
        ok = self._bus.registerObject(
            MPRIS_PATH, self,
            QDBusConnection.ExportAdaptors,
        )
        if not ok:
            return False
        # Some desktops (KDE Plasma's mediacontroller widget) match service
        # names with an `instance{pid}` suffix when multiple players share an
        # identity. We use the bare name first and fall back to suffixed if
        # someone else has claimed it.
        for name in (self._service_name, f"{self._service_name}.instance{os.getpid()}"):
            if self._bus.registerService(name):
                self._service_name = name
                self._connected = True
                break
        if self._connected:
            self._wire_signals()
            self._refresh_all()
        return self._connected

    def stop(self) -> None:
        if not self._connected:
            return
        try:
            self._bus.unregisterService(self._service_name)
            self._bus.unregisterObject(MPRIS_PATH)
        finally:
            self._connected = False

    # ---------- wiring ----------

    def _wire_signals(self) -> None:
        self._player.state_changed.connect(self._on_state_changed)
        self._player.position_changed.connect(self._on_position_changed)
        self._player.duration_changed.connect(self._on_duration_changed)
        self._player.speed_changed.connect(self._on_speed_changed)
        self._hook_volume_widget()
        self._queue.current_changed.connect(self._on_current_changed)
        self._queue.rowsInserted.connect(self._on_queue_changed)
        self._queue.rowsRemoved.connect(self._on_queue_changed)
        self._queue.modelReset.connect(self._on_queue_changed)
        self._queue.modes_changed.connect(self._on_modes_changed)

    def _refresh_all(self) -> None:
        self._on_current_changed(self._queue.current)
        self._on_state_changed(self._player.state)

    def _hook_volume_widget(self) -> None:
        """Listen to the window's volume widget so external MPRIS clients
        see slider moves live. The router has no volume-changed signal of
        its own, and the widget is rebuilt on layout swaps — so we re-hook
        when the old one is destroyed."""
        widget = getattr(self._window, "volume", None)
        if widget is None or widget is self._volume_widget:
            return
        try:
            widget.volume_changed.connect(self._on_app_volume_changed)
            widget.destroyed.connect(self._on_volume_widget_destroyed)
        except (AttributeError, RuntimeError, TypeError):
            return
        self._volume_widget = widget

    def _on_volume_widget_destroyed(self, *_args) -> None:
        # Fired from inside the old widget's ~QObject. Re-hooking right here
        # is unsafe: ``window.volume`` often still points at the very widget
        # being destroyed, and calling connect() on a half-destructed QObject
        # segfaults inside PySide's SignalManager (the wrapper is only
        # invalidated once destruction *completes*, so the RuntimeError guard
        # in _hook_volume_widget cannot catch it). Defer to the next event
        # loop turn, by which point the widget is gone and the layout swap
        # has published its replacement.
        self._volume_widget = None
        QTimer.singleShot(0, self._hook_volume_widget)

    # ---------- state accessors used by adaptors ----------

    @property
    def playback_status(self) -> str:
        return self._playback_status

    @property
    def has_track(self) -> bool:
        return self._current_track is not None

    @property
    def can_go_next(self) -> bool:
        return self._queue.can_advance() or self._queue.radio_enabled

    @property
    def can_go_previous(self) -> bool:
        return self._queue.can_go_back()

    @property
    def shuffle(self) -> bool:
        return self._queue.shuffle_enabled

    @property
    def loop_status(self) -> str:
        from .queue import RepeatMode
        return {
            RepeatMode.OFF: "None",
            RepeatMode.ALL: "Playlist",
            RepeatMode.ONE: "Track",
        }[self._queue.repeat_mode]

    @property
    def position_us(self) -> int:
        return self._position_us

    @property
    def volume(self) -> float:
        """Live app volume as the MPRIS 0.0–1.0 float. The PlaybackRouter
        keeps the authoritative 0–100 int but exposes no public getter, so
        we read its private cache; fall back to the window's slider."""
        vol = getattr(self._player, "_volume", None)
        if vol is None:
            try:
                vol = self._window.volume.volume()
            except (AttributeError, RuntimeError):
                return 1.0
        try:
            return max(0.0, min(1.0, float(vol) / 100.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def rate(self) -> float:
        """Live playback rate (1.0 = normal) from the player/router."""
        try:
            return float(getattr(self._player, "speed", 1.0) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    @property
    def metadata(self) -> dict:
        tr = self._current_track
        if tr is None:
            return {}
        track_path = QDBusObjectPath(
            f"{MPRIS_PATH}/track/{_safe_video_id_for_path(tr.video_id)}"
        )
        meta: dict[str, object] = {
            "mpris:trackid": track_path,
            "xesam:title": tr.title or "",
            "xesam:artist": [a.strip() for a in (tr.artists or "").split(",") if a.strip()] or [""],
            "xesam:album": tr.album or "",
            "xesam:url": f"https://music.youtube.com/watch?v={tr.video_id}",
        }
        if self._duration_us > 0:
            meta["mpris:length"] = self._duration_us
        if tr.thumbnail:
            meta["mpris:artUrl"] = tr.thumbnail
        return meta

    # ---------- signal handlers ----------

    def _on_state_changed(self, state) -> None:
        # tide's PlayState -> MPRIS PlaybackStatus
        from .player import PlayState
        mapping = {
            PlayState.PLAYING: "Playing",
            PlayState.PAUSED: "Paused",
            PlayState.LOADING: "Playing",
            PlayState.IDLE: "Stopped",
        }
        new_status = mapping.get(state, "Stopped")
        if new_status != self._playback_status:
            self._playback_status = new_status
            self._emit_props_changed({"PlaybackStatus": new_status})

    def _on_position_changed(self, secs: float) -> None:
        self._position_us = int(secs * 1_000_000)
        # Position is *not* emitted on PropertiesChanged per spec — clients
        # poll it. We do emit Seeked on jumps which are handled in on_seek_*.

    def _on_speed_changed(self, rate: float) -> None:
        self._emit_props_changed({"Rate": float(rate)})

    def _on_app_volume_changed(self, percent: int) -> None:
        self._emit_props_changed({"Volume": max(0.0, min(1.0, percent / 100.0))})

    def _on_duration_changed(self, secs: float) -> None:
        new_dur = int(secs * 1_000_000)
        if new_dur != self._duration_us:
            self._duration_us = new_dur
            self._emit_props_changed({"Metadata": self.metadata})

    def _on_current_changed(self, track) -> None:
        # Reset duration FIRST so the first Metadata emit doesn't carry the
        # previous track's mpris:length (KDE Plasma's mediacontroller caches
        # by trackid and won't override a length once stored).
        self._duration_us = 0
        self._current_track = track
        self._position_us = 0
        # Many things move at once when the track flips. Volume/Rate are
        # rebroadcast too: the startup push of persisted volume/speed goes
        # through paths with no signal (``apply_initial_volume`` sets the
        # slider with emit=False), so the first track change is the earliest
        # reliable moment to correct clients that cached our defaults.
        changes = {
            "Metadata": self.metadata,
            "CanPlay": self.has_track,
            "CanPause": self.has_track,
            "CanSeek": self.has_track,
            "CanGoNext": self.can_go_next,
            "CanGoPrevious": self.can_go_previous,
            "Volume": self.volume,
            "Rate": self.rate,
        }
        self._emit_props_changed(changes)
        # Also signal that the position got reset, since position_us silently
        # went to 0 and clients shouldn't think we just scrubbed backward
        # within the SAME track.
        self._emit_seeked(0)

    def _on_queue_changed(self, *_args) -> None:
        self._emit_props_changed({
            "CanGoNext": self.can_go_next,
            "CanGoPrevious": self.can_go_previous,
        })

    def _on_modes_changed(self) -> None:
        # Shuffle/repeat also change whether a "next"/"previous" exists.
        self._emit_props_changed({
            "Shuffle": self.shuffle,
            "LoopStatus": self.loop_status,
            "CanGoNext": self.can_go_next,
            "CanGoPrevious": self.can_go_previous,
        })

    # ---------- D-Bus method handlers (called from adaptors) ----------

    def on_next(self) -> None:
        self._window._on_next_clicked()

    def on_previous(self) -> None:
        self._window._on_prev_clicked()

    def on_play(self) -> None:
        self._player.play()

    def on_pause(self) -> None:
        self._player.pause()

    def on_play_pause(self) -> None:
        self._player.toggle()

    def on_stop(self) -> None:
        self._player.stop()

    def on_seek_relative(self, offset_secs: float) -> None:
        target = max(0.0, (self._position_us / 1_000_000.0) + offset_secs)
        self._player.seek(target)
        self._emit_seeked(int(target * 1_000_000))

    def on_set_position(self, track_path: str, position_secs: float) -> None:
        # Per spec we should only honor SetPosition if the path matches the
        # current track; otherwise ignore.
        tr = self._current_track
        if not tr:
            return
        expected = f"{MPRIS_PATH}/track/{_safe_video_id_for_path(tr.video_id)}"
        if track_path != expected:
            return
        self._player.seek(max(0.0, position_secs))
        self._emit_seeked(int(position_secs * 1_000_000))

    def on_set_volume(self, value: float) -> None:
        percent = int(round(max(0.0, min(1.0, value)) * 100))
        self._player.set_volume(percent)
        # Keep tide's own slider in step (display-only; emit=False avoids a
        # feedback loop through the widget hook and the window's persist
        # handler — KDE sends a burst of sets while dragging).
        try:
            self._window.volume.setVolume(percent, emit=False)
        except (AttributeError, RuntimeError, TypeError):
            pass
        # Confirm the accepted value so clients don't snap their slider back.
        self._emit_props_changed({"Volume": percent / 100.0})

    def on_set_shuffle(self, value: bool) -> None:
        self._queue.set_shuffle(bool(value))
        # modes_changed already re-emits; nothing more to confirm here.

    def on_set_loop_status(self, value: str) -> None:
        from .queue import RepeatMode
        mode = {
            "none": RepeatMode.OFF,
            "playlist": RepeatMode.ALL,
            "track": RepeatMode.ONE,
        }.get(str(value or "").strip().lower())
        if mode is None:
            # Unknown value — per spec just ignore, but re-assert the real
            # state so the client's optimistic UI snaps back.
            self._emit_props_changed({"LoopStatus": self.loop_status})
            return
        self._queue.set_repeat(mode)

    def on_set_rate(self, value: float) -> None:
        """MPRIS clients writing the Rate property. Per spec a rate of 0.0
        must act as Pause, never be stored; out-of-range values are clamped
        to the UI's supported window. Route through the window's SpeedButton
        when available — it is the authoritative speed store (display +
        settings persistence) — falling back to the raw player."""
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return
        if rate <= 0.0:
            self._player.pause()
            return
        rate = max(SPEED_MIN, min(SPEED_MAX, rate))
        applied = False
        btn = getattr(self._window, "speed_btn", None)
        if btn is not None:
            try:
                btn.set_speed(rate)
                applied = True
            except (AttributeError, RuntimeError):
                pass
        if not applied:
            self._player.set_speed(rate)
        # Confirm the effective (clamped/snapped) rate; harmless duplicate
        # of the speed_changed hook when the value actually moved.
        self._emit_props_changed({"Rate": self.rate})

    def raise_window(self) -> None:
        try:
            # Raise the mini player instead when mini mode is active.
            present = getattr(self._window, "present_active", None)
            if present is not None:
                present()
            else:
                self._window.showNormal()
                self._window.raise_()
                self._window.activateWindow()
        except Exception:
            pass

    def quit_app(self) -> None:
        try:
            self._window.close()
        except Exception:
            pass

    # ---------- low-level signal emission ----------

    def _emit_props_changed(self, changes: dict) -> None:
        if not self._connected:
            return
        msg = QDBusMessage.createSignal(MPRIS_PATH, PROPERTIES_IFACE, "PropertiesChanged")
        # Args: interface name (string), changed_properties (a{sv}), invalidated_properties (as)
        msg.setArguments([MPRIS_PLAYER_IFACE, changes, []])
        self._bus.send(msg)

    def _emit_seeked(self, position_us: int) -> None:
        if not self._connected:
            return
        msg = QDBusMessage.createSignal(MPRIS_PATH, MPRIS_PLAYER_IFACE, "Seeked")
        msg.setArguments([position_us])
        self._bus.send(msg)
