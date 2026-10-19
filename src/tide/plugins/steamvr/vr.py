"""The vr thread: every openvr call happens here, never on tide's gui
thread. Learned in the spike (tools/vr-spike, design/steamvr.md):

* when steamvr's compositor dies, every overlay call blocks about a second.
  on the gui thread that would freeze tide.
* frames go up as opengl textures. vrclient shares them with its vulkan
  side; setOverlayRaw and png files crashed the compositor on linux.
* a qimage is top-down and a gl texture bottom-up, so the texture bounds
  are flipped. steamvr then reports laser y top-down too: flip one, never
  both.
* overlays get no button input while a game has focus. tracking and laser
  clicks on our own overlays do arrive, so that's all this uses.

The gui thread renders the qt panel and posts frames in; this thread
posts what happened back out through VrBridge's signals.
"""
from __future__ import annotations

import logging
import os
import queue
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from . import geometry as geo

log = logging.getLogger("tide.plugin.steamvr")

HERE = Path(__file__).resolve().parent
RETRY_S = 10.0
STALL_S = 0.5
GROW_S = 0.25
PANEL_PX = (900, 420)
WATCH_PX = 256
WATCH_WIDTH_M = 0.045
SIDES = ("left", "right")


def to_np(m34) -> np.ndarray:
    a = np.eye(4)
    for i in range(3):
        for j in range(4):
            a[i, j] = m34.m[i][j]
    return a


def to_m34(a: np.ndarray):
    import openvr
    m = openvr.HmdMatrix34_t()
    for i in range(3):
        for j in range(4):
            m.m[i][j] = float(a[i, j])
    return m


class VrBridge(QObject):
    """lives on the gui thread; the vr thread emits, qt queues it over."""
    status = Signal(str)
    panel_mouse = Signal(str, float, float)   # move / down / up / leave, x, y
    panel_opened = Signal(str)                # how
    panel_closed = Signal(str)                # why
    watch_visible = Signal(bool)


class _Gl:
    """an opengl context on the vr thread, over a surface the gui thread
    made (qt wants offscreen surfaces created there)."""

    def __init__(self, surface) -> None:
        from PySide6.QtGui import QOpenGLContext
        self.surface = surface
        self.ctx = QOpenGLContext()
        if not self.ctx.create() or not self.ctx.makeCurrent(surface):
            raise RuntimeError("couldn't get an opengl context")
        self.textures: list = []

    def upload(self, img, tex=None):
        from PySide6.QtGui import QImage
        from PySide6.QtOpenGL import QOpenGLTexture
        self.ctx.makeCurrent(self.surface)
        img = img.convertToFormat(QImage.Format_RGBA8888)
        if tex is None or tex.width() != img.width() or tex.height() != img.height():
            if tex is not None:
                tex.destroy()
                self.textures.remove(tex)
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            tex.setData(img, QOpenGLTexture.DontGenerateMipMaps)
            self.textures.append(tex)
        else:
            # the qimage overload re-runs texture setup, which an allocated
            # texture refuses with a warning per frame
            tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, img.constBits())
        self.ctx.functions().glFlush()
        return tex

    def release(self) -> None:
        """drop this session's textures; the context stays for the next."""
        if self.ctx.makeCurrent(self.surface):
            for tex in self.textures:
                tex.destroy()
            self.ctx.doneCurrent()
        self.textures.clear()


class _Overlay:
    def __init__(self, ov, gl: _Gl, key: str, name: str, width_m: float, px: tuple[int, int]) -> None:
        import openvr
        self.ov, self.gl = ov, gl
        self.h = ov.createOverlay(key, name)
        ov.setOverlayWidthInMeters(self.h, width_m)
        ov.setOverlayInputMethod(self.h, openvr.VROverlayInputMethod_Mouse)
        scale = openvr.HmdVector2_t()
        scale.v[0], scale.v[1] = float(px[0]), float(px[1])
        ov.setOverlayMouseScale(self.h, scale)
        # steamvr draws its laser at our overlays outside the dashboard
        ov.setOverlayFlag(self.h, openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible, True)
        ov.setOverlayFlag(self.h, openvr.VROverlayFlags_SendVRSmoothScrollEvents, True)
        bounds = openvr.VRTextureBounds_t()
        bounds.uMin, bounds.uMax, bounds.vMin, bounds.vMax = 0.0, 1.0, 1.0, 0.0
        ov.setOverlayTextureBounds(self.h, bounds)
        self.tex = None
        self.has_frame = False
        self.visible = False

    def upload(self, img) -> float:
        import openvr
        t0 = time.perf_counter()
        self.tex = self.gl.upload(img, self.tex)
        t = openvr.Texture_t()
        t.handle = int(self.tex.textureId())
        t.eType = openvr.TextureType_OpenGL
        t.eColorSpace = openvr.ColorSpace_Auto
        self.ov.setOverlayTexture(self.h, t)
        self.has_frame = True
        return time.perf_counter() - t0

    def place(self, pose: np.ndarray) -> None:
        import openvr
        self.ov.setOverlayTransformAbsolute(self.h, openvr.TrackingUniverseStanding, to_m34(pose))

    def show(self, on: bool) -> None:
        if on == self.visible:
            return
        self.visible = on
        (self.ov.showOverlay if on else self.ov.hideOverlay)(self.h)

    def events(self):
        import openvr
        ev = openvr.VREvent_t()
        while True:
            ok, ev = self.ov.pollNextOverlayEvent(self.h, ev)
            if not ok:
                return
            m = ev.data.mouse
            yield ev.eventType, float(m.x), float(m.y)

    def destroy(self) -> None:
        try:
            self.ov.destroyOverlay(self.h)
        except Exception:  # noqa: BLE001 - steamvr may already be gone
            pass


class _Lost(Exception):
    """steamvr went away; reconnect later."""


class VrThread(QThread):
    """connects to steamvr when it's running, runs the watch and the panel,
    reconnects after steamvr restarts. ``post`` is the only way in."""

    def __init__(self, bridge: VrBridge, surface, settings: dict) -> None:
        super().__init__()
        self.bridge = bridge
        self.surface = surface
        self.settings = dict(settings)
        self._cmds: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = False
        self.panel_open = False
        self.input = None
        # one gl context for the thread's life: dropping it here would
        # destroy a qt object off the gui thread (the deadlock qthreads.py
        # describes). it goes with this QThread object, after run() ends.
        self._gl: _Gl | None = None

    # ---- from the gui thread ----

    def post(self, *cmd) -> None:
        self._cmds.put(cmd)

    def stop(self) -> None:
        self._stop = True

    def dispose(self) -> None:
        """gui thread, after run() returned: drop the gl context here."""
        if self._gl is not None and not self.isRunning():
            import shiboken6
            if shiboken6.isValid(self._gl.ctx):
                shiboken6.delete(self._gl.ctx)
            self._gl = None

    # ---- the thread ----

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(0.1)

    def run(self) -> None:
        while not self._stop:
            if not self._steamvr_running():
                self.bridge.status.emit("waiting for steamvr")
                self._sleep(RETRY_S)
                continue
            try:
                self._session()
            except _Lost as why:
                self.bridge.status.emit(str(why))
                self._sleep(RETRY_S)
            except Exception as exc:  # noqa: BLE001 - never let the thread die quietly
                log.exception("vr session failed")
                self.bridge.status.emit(f"error: {exc}"[:120])
                self._sleep(RETRY_S)

    def _steamvr_running(self) -> bool:
        """a background-type init fails instead of launching steamvr, so
        switching the plugin on never starts steamvr by itself.
        TIDE_VR_OFFLINE=1 pretends steamvr is never running: tests, and
        working on the panel with a headset that shouldn't be touched."""
        if os.environ.get("TIDE_VR_OFFLINE") == "1":
            return False
        import openvr
        try:
            openvr.init(openvr.VRApplication_Background)
        except openvr.OpenVRError:
            return False
        openvr.shutdown()
        return True

    def _session(self) -> None:
        import openvr
        try:
            self.system = openvr.init(openvr.VRApplication_Overlay)
        except openvr.OpenVRError as exc:
            raise _Lost(f"couldn't connect: {exc}") from None
        overlays: list[_Overlay] = []
        gl = None
        try:
            if self._gl is None:
                self._gl = _Gl(self.surface)
            gl = self._gl
            ov = openvr.VROverlay()
            self.panel = _Overlay(ov, gl, "tide.vr.panel", "tide", self._panel_width(), PANEL_PX)
            self.watch = _Overlay(ov, gl, "tide.vr.watch", "tide watch", WATCH_WIDTH_M,
                                  (WATCH_PX, WATCH_PX))
            overlays = [self.panel, self.watch]
            self._input()
            self.bridge.status.emit("connected")
            self._loop()
        finally:
            for o in overlays:
                o.destroy()
            if gl is not None:
                gl.release()
            openvr.shutdown()
            if self.panel_open:
                self.bridge.panel_closed.emit("steamvr")
            self.bridge.watch_visible.emit(False)

    def _input(self) -> None:
        """hand skeletons, raw hand poses, haptics. no buttons: an overlay
        never gets them while a game has focus."""
        import openvr
        self.input = None
        try:
            inp = openvr.VRInput()
            inp.setActionManifestPath(str(HERE / "actions.json"))
            self.act = {name: inp.getActionHandle(f"/actions/tide/in/{name}")
                        for name in ("hand_left", "hand_right", "pose_left", "pose_right")}
            self.haptic = inp.getActionHandle("/actions/tide/out/haptic")
            self.sources = {s: inp.getInputSourceHandle(f"/user/hand/{s}") for s in SIDES}
            self.sets = (openvr.VRActiveActionSet_t * 1)()
            self.sets[0].ulActionSet = inp.getActionSetHandle("/actions/tide")
            self.bones = (openvr.VRBoneTransform_t * 31)()
            self.input = inp
        except openvr.OpenVRError as exc:
            log.warning("no hand input: %s", exc)

    def _panel_width(self) -> float:
        return {"small": 0.28, "medium": 0.34, "large": 0.42}.get(self.settings.get("size"), 0.34)

    # ---- per tick ----

    def _loop(self) -> None:
        import openvr
        try:
            hz = float(self.system.getFloatTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_DisplayFrequency_Float) or 0.0)
        except openvr.OpenVRError:
            hz = 0.0
        hz = hz if hz > 10 else 90.0
        self.poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()
        self.panel_open = False
        self.panel_pose = None
        self.opened_at = 0.0
        self.last_touch = -1e9
        self.attention = geo.Attention(float(self.settings.get("linger", 2.5)),
                                       animate=not self.settings.get("motion_off", False))
        self.watch_shown = geo.Shown(geo.WATCH_SHOW_S, geo.WATCH_HIDE_S)
        frames: dict[str, object] = {}
        next_t = time.perf_counter()
        while not self._stop:
            now = time.perf_counter()
            self.system.getDeviceToAbsoluteTrackingPose(
                openvr.TrackingUniverseStanding, 0.0, self.poses)
            self._events()
            if self.input is not None:
                try:
                    self.input.updateActionState(self.sets)
                except openvr.OpenVRError:
                    pass
            self._commands(frames, now)
            if self.panel_open:
                self._panel_tick(now)
            else:
                self._watch_tick(now)
            for name, ov in (("panel", self.panel), ("watch", self.watch)):
                img = frames.pop(name, None)
                if img is not None and self._stall_check(ov.upload(img)):
                    raise _Lost("steamvr stopped answering")
            next_t += 1.0 / hz
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()

    def _stall_check(self, seconds: float) -> bool:
        return seconds > STALL_S

    def _events(self) -> None:
        import openvr
        ev = openvr.VREvent_t()
        while self.system.pollNextEvent(ev):
            if ev.eventType in (openvr.VREvent_Quit, openvr.VREvent_DriverRequestedQuit):
                # without the ack steamvr waits on us at shutdown
                self.system.acknowledgeQuit_Exiting()
                raise _Lost("steamvr quit")

    def _commands(self, frames: dict, now: float) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            kind = cmd[0]
            if kind in ("panel_frame", "watch_frame"):
                frames[kind.split("_")[0]] = cmd[1]      # newest wins
            elif kind == "close":
                self._close(cmd[1])
            elif kind == "open":
                self._open(cmd[1], now)
            elif kind == "settings":
                self.settings.update(cmd[1])
                self.panel.ov.setOverlayWidthInMeters(self.panel.h, self._panel_width())
                self.attention.linger_s = float(self.settings.get("linger", 2.5))
                self.attention.animate = not self.settings.get("motion_off", False)

    # ---- device reads ----

    def _hmd(self) -> np.ndarray | None:
        import openvr
        p = self.poses[openvr.k_unTrackedDeviceIndex_Hmd]
        return to_np(p.mDeviceToAbsoluteTracking) if p.bPoseIsValid else None

    def _controller(self, side: str) -> np.ndarray | None:
        import openvr
        role = (openvr.TrackedControllerRole_LeftHand if side == "left"
                else openvr.TrackedControllerRole_RightHand)
        idx = self.system.getTrackedDeviceIndexForControllerRole(role)
        if idx == openvr.k_unTrackedDeviceIndexInvalid:
            return None
        p = self.poses[idx]
        return to_np(p.mDeviceToAbsoluteTracking) if p.bPoseIsValid else None

    def _fingertip(self, side: str) -> np.ndarray | None:
        import openvr
        if self.input is not None:
            try:
                pose = self.input.getPoseActionDataRelativeToNow(
                    self.act[f"pose_{side}"], openvr.TrackingUniverseStanding, 0.0,
                    openvr.k_ulInvalidInputValueHandle)
                self.input.getSkeletalBoneData(
                    self.act[f"hand_{side}"], openvr.VRSkeletalTransformSpace_Model,
                    openvr.VRSkeletalMotionRange_WithoutController, self.bones)
                if pose.bActive and pose.pose.bPoseIsValid:
                    tip = self.bones[10].position.v       # index finger tip bone
                    return geo.fingertip_from_skeleton(
                        to_np(pose.pose.mDeviceToAbsoluteTracking), [tip[0], tip[1], tip[2]])
            except openvr.OpenVRError:
                pass
        ctrl = self._controller(side)
        return geo.fingertip_from_controller(ctrl) if ctrl is not None else None

    def _buzz(self, seconds: float, freq: float, amp: float) -> None:
        if self.input is None or not self.settings.get("haptics", True):
            return
        for side in SIDES:
            try:
                self.input.triggerHapticVibrationAction(
                    self.haptic, 0.0, seconds, freq, amp, self.sources[side])
            except Exception:  # noqa: BLE001 - haptics are a nicety
                pass

    # ---- watch ----

    def _watch_tick(self, now: float) -> None:
        import openvr
        side = self.settings.get("watch_hand", "left")
        hmd, ctrl = self._hmd(), self._controller(side)
        pos = geo.wrist_point(ctrl, side) if ctrl is not None else None
        wanted = hmd is not None and pos is not None and geo.looking_at_wrist(hmd, pos)
        was = self.watch_shown.on
        shown = self.watch_shown.update(now, wanted)
        if shown != was:
            self.bridge.watch_visible.emit(shown)
        self.watch.show(shown and self.watch.has_frame)
        if not shown or pos is None:
            return
        self.watch.place(geo.facing(pos, hmd[:3, 3]))
        for kind, _x, _y in self.watch.events():
            if kind == openvr.VREvent_MouseButtonDown:
                self._open("watch", now)
                return
        other = "right" if side == "left" else "left"
        tip = self._fingertip(other)
        if tip is not None and float(np.linalg.norm(tip - pos)) <= geo.POKE_M:
            self._open("watch", now)

    # ---- panel ----

    def _open(self, how: str, now: float) -> None:
        if self.panel_open:
            return
        hmd = self._hmd()
        if hmd is None:
            return
        self.panel_open = True
        self.panel_pose = geo.spawn_in_front(hmd)
        self.panel.place(self.panel_pose)
        self.opened_at = now
        self.last_touch = now
        self.attention.reset(now)
        self.watch_shown.reset()
        self.watch.show(False)
        self.bridge.watch_visible.emit(False)
        animate = not self.settings.get("motion_off", False)
        self.panel.ov.setOverlayAlpha(self.panel.h, 0.0 if animate else 1.0)
        self.panel.show(True)
        self._buzz(0.08, 120, 0.9)
        self.bridge.panel_opened.emit(how)

    def _close(self, why: str) -> None:
        if not self.panel_open:
            return
        self.panel_open = False
        self.panel.show(False)
        self.bridge.panel_mouse.emit("leave", 0.0, 0.0)
        self._buzz(0.03, 200, 0.6)
        self.bridge.panel_closed.emit(why)

    def _panel_tick(self, now: float) -> None:
        import openvr
        for kind, x, y in self.panel.events():
            name = {openvr.VREvent_MouseMove: "move", openvr.VREvent_MouseButtonDown: "down",
                    openvr.VREvent_MouseButtonUp: "up"}.get(kind)
            if name is not None:
                self.last_touch = now
                self.bridge.panel_mouse.emit(name, x, y)
        hmd = self._hmd()
        if hmd is None or self.panel_pose is None:
            return
        center = self.panel_pose[:3, 3]
        gaze, _d = geo.off_center_deg(hmd, center)
        hands = [self._controller(s) for s in SIDES]
        hand_m = min((float(np.linalg.norm(c[:3, 3] - center)) for c in hands if c is not None),
                     default=99.0)
        alpha, close = self.attention.update(now, gaze, hand_m, now - self.last_touch)
        # opening: the panel grows to size as it fades in
        grow = 1.0
        if self.attention.animate:
            grow = 0.3 + 0.7 * geo.ease_out((now - self.opened_at) / GROW_S)
            alpha = min(alpha, geo.ease_out((now - self.opened_at) / GROW_S))
        self.panel.ov.setOverlayWidthInMeters(self.panel.h, self._panel_width() * grow)
        self.panel.ov.setOverlayAlpha(self.panel.h, float(alpha))
        if close:
            self._close("looked away")
