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
    def LoopStatus(self) -> str: return "None"

    @Property(float)
    def Rate(self) -> float: return 1.0
    @Rate.setter
    def Rate(self, _value: float) -> None: pass

    @Property(bool)
    def Shuffle(self) -> bool: return False
    @Shuffle.setter
    def Shuffle(self, _value: bool) -> None: pass

    @Property("QVariantMap")
    def Metadata(self) -> dict: return self._service.metadata

    @Property(float)
    def Volume(self) -> float: return 1.0
    @Volume.setter
    def Volume(self, value: float) -> None:
        self._service.on_set_volume(value)

    @Property("qlonglong")
    def Position(self) -> int: return self._service.position_us

    @Property(float)
    def MinimumRate(self) -> float: return 1.0

    @Property(float)
    def MaximumRate(self) -> float: return 1.0

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
        self._queue.current_changed.connect(self._on_current_changed)
        self._queue.rowsInserted.connect(self._on_queue_changed)
        self._queue.rowsRemoved.connect(self._on_queue_changed)
        self._queue.modelReset.connect(self._on_queue_changed)

    def _refresh_all(self) -> None:
        self._on_current_changed(self._queue.current)
        self._on_state_changed(self._player.state)

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
    def position_us(self) -> int:
        return self._position_us

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

    def _on_duration_changed(self, secs: float) -> None:
        new_dur = int(secs * 1_000_000)
        if new_dur != self._duration_us:
            self._duration_us = new_dur
            self._emit_props_changed({"Metadata": self.metadata})

    def _on_current_changed(self, track) -> None:
        self._current_track = track
        self._position_us = 0
        # Many things move at once when the track flips.
        changes = {
            "Metadata": self.metadata,
            "CanPlay": self.has_track,
            "CanPause": self.has_track,
            "CanSeek": self.has_track,
            "CanGoNext": self.can_go_next,
            "CanGoPrevious": self.can_go_previous,
        }
        self._emit_props_changed(changes)

    def _on_queue_changed(self, *_args) -> None:
        self._emit_props_changed({
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
        self._player.set_volume(int(max(0.0, min(1.0, value)) * 100))

    def raise_window(self) -> None:
        try:
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
