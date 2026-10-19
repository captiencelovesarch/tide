"""tide in vr: a watch on your wrist that opens a now-playing panel in
front of you. look away and it goes. design/steamvr.md is the plan; this
is its first slice (the watch, the panel, laser input, the fade).

The gui half lives here: it keeps the qt panel up to date, renders it
when something changed and posts the frames to the vr thread (vr.py),
which does everything that talks to steamvr.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QOffscreenSurface, QSurfaceFormat

from .panel import Forwarder, VrPanel, watch_face
from .vr import VrBridge, VrThread

FRAME_MS = 33          # at most ~30 redraws a second while open
TICK_MS = 500          # progress and the watch ring move this often


class SteamVr:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.panel_open = False
        self.watch_up = False
        # every qt object here is handed to ctx.adopt: tide deletes them
        # right after stop(), while qt is still whole
        self.panel = ctx.adopt(VrPanel(ctx))
        self.panel.show()          # WA_DontShowOnScreen keeps it off the desktop
        self.fwd = Forwarder(self.panel)

        self.bridge = ctx.adopt(VrBridge())
        self.bridge.status.connect(self._status)
        self.bridge.panel_mouse.connect(self._mouse)
        self.bridge.panel_opened.connect(self._opened)
        self.bridge.panel_closed.connect(self._closed)
        self.bridge.watch_visible.connect(self._watch_visible)

        # qt wants offscreen surfaces made on the gui thread; the vr thread
        # makes its opengl context over this one
        self.surface = ctx.adopt(QOffscreenSurface())
        self.surface.setFormat(QSurfaceFormat.defaultFormat())
        self.surface.create()

        self._redraw = ctx.adopt(QTimer())
        self._redraw.setSingleShot(True)
        self._redraw.setInterval(FRAME_MS)
        self._redraw.timeout.connect(self._send_panel)
        self._tick = ctx.adopt(QTimer())
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        pb = ctx.playback
        pb.track_changed.connect(lambda _t: self._refresh())
        pb.state_changed.connect(lambda _s: self._refresh())
        pb.liked_changed.connect(lambda _l: self._refresh())
        ctx.theme.changed.connect(self._restyle)
        ctx.settings_changed.connect(self._settings_changed)
        self.panel.changed.connect(self._dirty)
        self.panel.close_requested.connect(lambda: self.thread.post("close", "x"))

        self.thread = VrThread(self.bridge, self.surface, self._settings())
        self.thread.start()
        ctx.set_status("waiting for steamvr")

    def stop(self) -> None:
        self._tick.stop()
        self._redraw.stop()
        self.thread.stop()
        # the vr thread checks for stop every tick, but a dying compositor
        # can hold one call for about a second
        if not self.thread.wait(5000):
            self.ctx.log.warning("vr thread didn't stop in time")
            _STRAGGLERS.append(self.thread)   # destroying a running QThread aborts
        else:
            self.thread.dispose()
            self.ctx.adopt(self.thread)

    # ---- settings ----

    def _settings(self) -> dict:
        s = self.ctx.settings
        return {
            "watch_hand": s["watch_hand"],
            "size": s["size"],
            "linger": float(s["linger"]),
            "haptics": s["haptics"],
            "motion_off": self.ctx.motion.off(),
        }

    def _settings_changed(self, _keys) -> None:
        self.thread.post("settings", self._settings())

    # ---- from the vr thread ----

    def _status(self, text: str) -> None:
        self.ctx.set_status(text)

    def _mouse(self, kind: str, x: float, y: float) -> None:
        self.fwd.handle(kind, x, y)
        self._dirty()

    def _opened(self, how: str) -> None:
        self.panel_open = True
        self.panel.refresh()
        self._send_panel()

    def _closed(self, why: str) -> None:
        self.panel_open = False

    def _watch_visible(self, on: bool) -> None:
        self.watch_up = on
        if on:
            self._send_watch()

    # ---- frames ----

    def _refresh(self) -> None:
        self.panel.refresh()

    def _restyle(self) -> None:
        self.panel.restyle()
        if self.watch_up:
            self._send_watch()

    def _dirty(self) -> None:
        if self.panel_open and not self._redraw.isActive():
            self._redraw.start()

    def _send_panel(self) -> None:
        if self.panel_open:
            self.thread.post("panel_frame", self.panel.frame())

    def _send_watch(self) -> None:
        dur = self.ctx.playback.duration()
        progress = self.ctx.playback.position() / dur if dur > 0 else 0.0
        self.thread.post("watch_frame", watch_face(self.ctx, progress))

    def _on_tick(self) -> None:
        pb = self.ctx.playback
        if self.panel_open:
            self.panel.progress(pb.position(), pb.duration())
            self._dirty()
        if self.watch_up:
            self._send_watch()


# vr threads that wouldn't stop in time: kept referenced so python never
# destroys a running QThread (a fatal abort); they end with the process
_STRAGGLERS: list = []
