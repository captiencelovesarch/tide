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
* an interactive overlay (one the laser can click) takes the controllers
  from the game while you point at it. so only the open panel is
  interactive; the watch and the ring never are (h3vr froze when the
  watch came up mid-aim). touch goes by controller poses, which keep
  coming either way.
* overlays draw over the game's controllers. seethrough.py thins the
  panel where they are.

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
from . import ring as ringmod
from . import seethrough

log = logging.getLogger("tide.plugin.steamvr")

HERE = Path(__file__).resolve().parent
RETRY_S = 10.0             # reconnect backoff goes 2 → 4 → 8 → 10 s
STALL_S = 0.5
STALLS_TO_GIVE_UP = 3      # in a row: one slow upload (a big first texture) isn't a dead compositor
GROW_S = 0.25
SPRING_S = 0.42            # modern's springy open
CLOSE_S = 0.22
WATCH_POP_S = 0.3
SEE_THROUGH_HZ = 45        # redraws of the windows over your controllers, at most
PANEL_PX = (1000, 640)
PANEL_WIDTH_M = {"small": 0.34, "medium": 0.42, "large": 0.50}
WATCH_PX = 256
WATCH_WIDTH_M = 0.045
WATCH_SWELL = 0.35         # how much bigger the watch grows as look-to-open fills
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
    lens = Signal(object)                     # each eye's outline, for the ring
    panel_scroll = Signal(float)              # stick up / down while pointing at the panel
    keyboard_text = Signal(str)               # what's typed so far
    keyboard_done = Signal(str)               # enter on steamvr's keyboard
    keyboard_closed = Signal()


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
    def __init__(self, ov, gl: _Gl, key: str, name: str, width_m: float,
                 px: tuple[int, int], *, interactive: bool = False) -> None:
        import openvr
        self.ov, self.gl = ov, gl
        self.h = ov.createOverlay(key, name)
        ov.setOverlayWidthInMeters(self.h, width_m)
        self.interactive = False
        if interactive:
            scale = openvr.HmdVector2_t()
            scale.v[0], scale.v[1] = float(px[0]), float(px[1])
            ov.setOverlayMouseScale(self.h, scale)
            ov.setOverlayFlag(self.h, openvr.VROverlayFlags_SendVRSmoothScrollEvents, True)
            self.set_interactive(True)
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

    def set_interactive(self, on: bool) -> None:
        """a laser target or not. while it is one, steamvr takes the
        controllers while you point at it and our hand-skeleton reads stop,
        so poking needs it off."""
        import openvr
        if on == self.interactive:
            return
        self.interactive = on
        self.ov.setOverlayInputMethod(
            self.h, openvr.VROverlayInputMethod_Mouse if on else openvr.VROverlayInputMethod_None)
        # steamvr draws its laser at it outside the dashboard
        self.ov.setOverlayFlag(self.h, openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible, on)

    def place(self, pose: np.ndarray) -> None:
        import openvr
        self.ov.setOverlayTransformAbsolute(self.h, openvr.TrackingUniverseStanding, to_m34(pose))

    def place_on(self, device: int, offset: np.ndarray) -> None:
        self.ov.setOverlayTransformTrackedDeviceRelative(self.h, device, to_m34(offset))

    def width(self, metres: float) -> None:
        self.ov.setOverlayWidthInMeters(self.h, float(metres))

    def alpha(self, a: float) -> None:
        self.ov.setOverlayAlpha(self.h, float(a))

    def show(self, on: bool) -> None:
        if on == self.visible:
            return
        self.visible = on
        (self.ov.showOverlay if on else self.ov.hideOverlay)(self.h)

    def events(self):
        """(type, the event) for each queued overlay event."""
        import openvr
        ev = openvr.VREvent_t()
        while True:
            ok, ev = self.ov.pollNextOverlayEvent(self.h, ev)
            if not ok:
                return
            yield ev.eventType, ev

    def destroy(self) -> None:
        try:
            self.ov.destroyOverlay(self.h)
        except Exception:  # noqa: BLE001 - steamvr may already be gone
            pass


class _Lost(Exception):
    """steamvr went away; reconnect later."""


class VrThread(QThread):
    """connects to steamvr when it's running, runs the watch, the panel and
    the ring, reconnects after steamvr restarts. ``post`` is the only way
    in."""

    def __init__(self, bridge: VrBridge, surface, settings: dict) -> None:
        super().__init__()
        self.bridge = bridge
        self.surface = surface
        self.settings = dict(settings)
        self._cmds: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = False
        self.panel_open = False
        self.keyboard_open = False
        self.input = None
        self.pulse = 0.0
        self.playing = False
        self.rings: list[_Overlay] = []
        self.ring_width = 0.0
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

    def _note(self, text: str) -> None:
        """a line in sessions.log (the plugin's data dir): every session's
        start and end, and each kind of per-tick error once. the first
        "it keeps disconnecting" report had nothing to go on."""
        path = self.settings.get("log_path")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    def run(self) -> None:
        retry = 2.0
        while not self._stop:
            if not self._steamvr_running():
                self.bridge.status.emit("waiting for steamvr")
                self._sleep(RETRY_S)
                continue
            started = time.monotonic()
            try:
                self._session()
                why = "stopped"
            except _Lost as lost:
                why = str(lost)
            except Exception as exc:  # noqa: BLE001 - never let the thread die quietly
                log.exception("vr session failed")
                import traceback
                why = f"error: {exc}"
                self._note("".join(traceback.format_exception(exc)).rstrip())
            self._note(f"session ended after {time.monotonic() - started:.0f} s: {why}")
            if self._stop:
                return
            self.bridge.status.emit(f"{why} · reconnecting"[:120])
            # a session that ran a while gets a quick retry; one that keeps
            # failing backs off
            retry = 2.0 if time.monotonic() - started > 60 else min(RETRY_S, retry * 2)
            self._sleep(retry)

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
        try:
            if self._gl is None:
                self._gl = _Gl(self.surface)
            self.ov = openvr.VROverlay()
            self.panel = _Overlay(self.ov, self._gl, "tide.vr.panel", "tide", self._panel_width(),
                                  PANEL_PX, interactive=True)
            self.watch = _Overlay(self.ov, self._gl, "tide.vr.watch", "tide watch", WATCH_WIDTH_M,
                                  (WATCH_PX, WATCH_PX))
            overlays = [self.panel, self.watch]
            self._input()
            self.bridge.lens.emit(self._lens())
            self.bridge.status.emit("connected")
            self._note("connected")
            self._loop()
        finally:
            for o in overlays + self.rings:
                o.destroy()
            self.rings = []
            if self._gl is not None:
                self._gl.release()
            openvr.shutdown()
            if self.panel_open:
                self.panel_open = False
                self.bridge.panel_closed.emit("steamvr")
            self.bridge.watch_visible.emit(False)

    def _input(self) -> None:
        """haptics. no buttons: an overlay never gets them while a game
        has focus."""
        import openvr
        self.input = None
        try:
            inp = openvr.VRInput()
            inp.setActionManifestPath(str(HERE / "actions.json"))
            self.act = {name: inp.getActionHandle(f"/actions/tide/in/{name}")
                        for name in ("hand_left", "hand_right")}
            self.haptic = inp.getActionHandle("/actions/tide/out/haptic")
            self.sources = {s: inp.getInputSourceHandle(f"/user/hand/{s}") for s in SIDES}
            self.sets = (openvr.VRActiveActionSet_t * 1)()
            self.sets[0].ulActionSet = inp.getActionSetHandle("/actions/tide")
            self.input = inp
        except openvr.OpenVRError as exc:
            log.warning("no hand input: %s", exc)

    def _lens(self) -> dict:
        """each eye's outline (viewport uv), projection and offset."""
        import openvr
        eyes = {}
        for eye, name in ((openvr.Eye_Left, "left"), (openvr.Eye_Right, "right")):
            uv = []
            try:
                mesh = self.system.getHiddenAreaMesh(eye, openvr.k_eHiddenAreaMesh_LineLoop)
                n = int(mesh.unTriangleCount)
                if n and mesh.pVertexData:
                    uv = [(float(mesh.pVertexData[i].v[0]), float(mesh.pVertexData[i].v[1]))
                          for i in range(n)]
            except Exception:  # noqa: BLE001 - an ellipse will do
                uv = []
            eyes[name] = {
                "uv": uv,
                "proj": list(self.system.getProjectionRaw(eye)),
                "eye_x": float(to_np(self.system.getEyeToHeadTransform(eye))[0, 3]),
            }
        return eyes

    def _panel_width(self) -> float:
        return PANEL_WIDTH_M.get(self.settings.get("size"), PANEL_WIDTH_M["medium"])

    def _animate(self) -> bool:
        return not self.settings.get("motion_off", False)

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
                                       animate=self._animate())
        self.watch_shown = geo.Shown(geo.WATCH_SHOW_S, geo.WATCH_HIDE_S)
        self.dwell = geo.Dwell()
        self.flash = geo.FlashLimiter()
        self.ripple = ringmod.Ripple()
        self.envelope = ringmod.Envelope()
        self.pokes = {side: geo.Poke() for side in SIDES}
        self.poke_px = (-1.0, -1.0)
        self.touch = geo.TouchMode()
        self.panel_base = None           # the gui's last frame, before seethrough
        self.see_caps: list = []
        self.see_cursor = None
        self.see_width = self._panel_width()
        self.drawn = ([], None)
        self.drawn_at = -1e9
        self.closing_at: float | None = None
        self.closing_from = 1.0
        self.watch_popped_at = -1e9
        self.stalls = 0
        self._errors_seen: set[str] = set()
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
            self._guard("commands", self._commands, frames)
            if self.panel_open:
                self._guard("panel", self._panel_tick, now)
            else:
                self._guard("watch", self._watch_tick, now)
            self._guard("closing", self._closing_tick, now)
            self._guard("ring", self._ring_tick, now)
            img = frames.pop("watch", None)
            if img is not None:
                self._upload(self.watch, img)
            fresh = frames.pop("panel", None)
            if fresh is not None:
                self.panel_base = fresh
            img = self._panel_image(now, fresh is not None)
            if img is not None:
                self._upload(self.panel, img)
            next_t += 1.0 / hz
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()

    def _upload(self, ov: _Overlay, img) -> None:
        if ov.upload(img) > STALL_S:
            self.stalls += 1
            if self.stalls >= STALLS_TO_GIVE_UP:
                raise _Lost("steamvr stopped answering")
        else:
            self.stalls = 0

    def _panel_image(self, now: float, fresh: bool):
        """the frame to upload this tick, if any: the gui's last one with
        the windows over your controllers and the cursor on top."""
        if self.panel_base is None:
            return None
        caps, cur = self.see_caps, self.see_cursor
        if not fresh:
            if seethrough.same(caps, self.drawn[0]) and seethrough.same(cur, self.drawn[1]):
                return None
            if now - self.drawn_at < 1.0 / SEE_THROUGH_HZ:
                return None
        self.drawn = (caps, cur)
        self.drawn_at = now
        if not caps and cur is None:
            return self.panel_base
        return seethrough.compose(self.panel_base, caps, cur, self.see_width,
                                  self.settings.get("cursor", "#ffffff"))

    def _guard(self, where: str, fn, *args) -> None:
        """one tick of one part. an error there is noted once and skipped;
        it no longer ends the whole session (a stray bad pose or a failed
        read used to disconnect you). steamvr really going away still
        does (_Lost)."""
        try:
            fn(*args)
        except _Lost:
            raise
        except Exception as exc:  # noqa: BLE001 - see above
            key = f"{where}:{type(exc).__name__}:{exc}"
            if key not in self._errors_seen:
                self._errors_seen.add(key)
                log.exception("vr %s tick failed", where)
                import traceback
                self._note(f"{where} tick failed (once noted, carrying on): "
                           + "".join(traceback.format_exception(exc)).rstrip())

    def _springy(self) -> bool:
        return self._animate() and self.settings.get("springy", False)

    def _events(self) -> None:
        import openvr
        ev = openvr.VREvent_t()
        while self.system.pollNextEvent(ev):
            self._keyboard_event(ev.eventType)
            if ev.eventType in (openvr.VREvent_Quit, openvr.VREvent_DriverRequestedQuit):
                # without the ack steamvr waits on us at shutdown
                self.system.acknowledgeQuit_Exiting()
                raise _Lost("steamvr quit")

    def _commands(self, frames: dict) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            kind = cmd[0]
            if kind == "panel_frame":
                frames["panel"] = cmd[1]          # newest wins
            elif kind == "watch_frame":
                frames["watch"] = cmd[1]
            elif kind == "close":
                self._close(cmd[1])
            elif kind == "keyboard":
                if self.panel_open:
                    self._keyboard(cmd[1])
            elif kind == "pulse":
                self.pulse = float(cmd[1])
            elif kind == "playing":
                self.playing = bool(cmd[1])
            elif kind == "ring_frames":
                self._ring_setup(cmd[1], cmd[2])
            elif kind == "settings":
                self.settings.update(cmd[1])
                self.panel.width(self._panel_width())
                self.attention.linger_s = float(self.settings.get("linger", 2.5))
                self.attention.animate = self._animate()

    # ---- steamvr's keyboard ----

    def _keyboard_text(self) -> str:
        """pyopenvr's getKeyboardText passes its buffer where the length
        goes; call the function table directly."""
        import ctypes
        fn = self.ov.function_table.getKeyboardText
        n = int(fn(None, 0))
        buf = ctypes.create_string_buffer(max(1, n))
        fn(buf, max(1, n))
        return buf.value.decode("utf-8", "replace")

    def _keyboard(self, text: str) -> None:
        import openvr
        try:
            self.ov.showKeyboardForOverlay(
                self.panel.h, openvr.k_EGamepadTextInputModeNormal,
                openvr.k_EGamepadTextInputLineModeSingleLine, openvr.KeyboardFlag_Modal,
                "search", 120, text, 0)
            self.keyboard_open = True
        except openvr.OpenVRError as exc:
            log.warning("no keyboard: %s", exc)
            self.bridge.keyboard_closed.emit()

    def _keyboard_event(self, kind: int) -> None:
        import openvr
        if not self.keyboard_open:
            return
        if kind == openvr.VREvent_KeyboardCharInput:
            self.bridge.keyboard_text.emit(self._keyboard_text())
        elif kind == openvr.VREvent_KeyboardDone:
            self.keyboard_open = False
            self.bridge.keyboard_done.emit(self._keyboard_text())
        elif kind == openvr.VREvent_KeyboardClosed:
            self.keyboard_open = False
            self.bridge.keyboard_closed.emit()

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

    def _tip(self, side: str) -> np.ndarray | None:
        """the front tip of a controller, what you touch things with."""
        ctrl = self._controller(side)
        return None if ctrl is None else geo.controller_tip(ctrl, side)

    def _buzz_hand(self, side: str, seconds: float, freq: float, amp: float) -> None:
        if self.input is None or not self.settings.get("haptics", True):
            return
        try:
            self.input.triggerHapticVibrationAction(
                self.haptic, 0.0, seconds, freq, amp, self.sources[side])
        except Exception:  # noqa: BLE001 - haptics are a nicety
            pass

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
        side = self.settings.get("watch_hand", "left")
        hmd, ctrl = self._hmd(), self._controller(side)
        wanted = hmd is not None and ctrl is not None and geo.checking_watch(hmd, ctrl, side)
        was = self.watch_shown.on
        shown = self.watch_shown.update(now, wanted)
        if shown != was:
            self.bridge.watch_visible.emit(shown)
            self.dwell.reset()
            self.watch_popped_at = now
        self.watch.show(shown and self.watch.has_frame)
        if not shown or hmd is None or ctrl is None:
            return
        pos = geo.wrist_point(ctrl, side)
        self.watch.place(geo.facing(pos, hmd[:3, 3]))
        # modern: it pops in with a little overshoot
        pop = (geo.ease_out_back((now - self.watch_popped_at) / WATCH_POP_S)
               if self._springy() else 1.0)

        fill = 0.0
        if self.settings.get("open_by_looking", True):
            ang, _d = geo.off_center_deg(hmd, pos)
            fill = self.dwell.update(now, ang <= geo.DWELL_LOOK_DEG)
        # it swells as you look, so you see it's about to open
        self.watch.width(WATCH_WIDTH_M * max(0.05, pop) * (1.0 + WATCH_SWELL * fill))
        if fill >= 1.0:
            self._open("watch (looked)", now)
            return
        other = "right" if side == "left" else "left"
        tip = self._tip(other)
        if tip is not None and float(np.linalg.norm(tip - pos)) <= geo.POKE_M:
            self._open("watch (poked)", now)

    # ---- panel ----

    def _open(self, how: str, now: float) -> None:
        if self.panel_open:
            return
        hmd = self._hmd()
        if hmd is None:
            return
        self.panel_open = True
        self.closing_at = None
        for p in self.pokes.values():
            p.reset()
        self.touch.reset()
        self.panel.set_interactive(True)
        self.panel_pose = geo.spawn_in_front(hmd)
        self.panel.place(self.panel_pose)
        self.opened_at = now
        self.last_touch = now
        self.attention.reset(now)
        self.watch_shown.reset()
        self.dwell.reset()
        self.watch.show(False)
        self.watch.width(WATCH_WIDTH_M)
        self.bridge.watch_visible.emit(False)
        self.panel.alpha(0.0 if self._animate() else 1.0)
        self.panel.show(True)
        self._buzz(0.08, 120, 0.9)
        self.bridge.panel_opened.emit(how)

    def _close(self, why: str) -> None:
        if not self.panel_open:
            return
        if self.keyboard_open:
            self.keyboard_open = False
            try:
                self.ov.hideKeyboard()
            except Exception:  # noqa: BLE001 - it may be gone already
                pass
            self.bridge.keyboard_closed.emit()
        self.panel_open = False
        for p in self.pokes.values():
            p.reset()
        self.touch.reset()
        self.see_caps, self.see_cursor = [], None
        if self._springy() and self.panel.visible:
            # modern: a quick shrink and fade instead of vanishing
            self.closing_at = time.perf_counter()
        else:
            self.panel.show(False)
        self.bridge.panel_mouse.emit("leave", 0.0, 0.0)
        self._buzz(0.03, 200, 0.6)
        self.bridge.panel_closed.emit(why)

    def _closing_tick(self, now: float) -> None:
        if self.closing_at is None:
            return
        t = (now - self.closing_at) / CLOSE_S
        if t >= 1.0:
            self.closing_at = None
            self.panel.show(False)
            return
        k = 1.0 - t * t
        self.panel.alpha(self.closing_from * k)
        self.panel.width(self._panel_width() * (1.0 - 0.08 * t))

    def _panel_tick(self, now: float) -> None:
        import openvr
        mouse = {openvr.VREvent_MouseMove: "move", openvr.VREvent_MouseButtonDown: "down",
                 openvr.VREvent_MouseButtonUp: "up"}
        for kind, ev in self.panel.events():
            if kind in mouse:
                self.last_touch = now
                self.bridge.panel_mouse.emit(mouse[kind], float(ev.data.mouse.x),
                                             float(ev.data.mouse.y))
            elif kind == openvr.VREvent_ScrollSmooth:
                self.last_touch = now
                self.bridge.panel_scroll.emit(float(ev.data.scroll.ydelta))
            else:
                self._keyboard_event(kind)
        if self.keyboard_open:
            # you're looking at the keyboard, not the panel: keep it
            self.last_touch = now
        hmd = self._hmd()
        if hmd is None or self.panel_pose is None:
            return
        center = self.panel_pose[:3, 3]
        gaze, _d = geo.off_center_deg(hmd, center)
        hands = {s: self._controller(s) for s in SIDES}
        hand_m = min((float(np.linalg.norm(c[:3, 3] - center)) for c in hands.values()
                      if c is not None), default=99.0)
        # opening: the panel grows to size as it fades in; modern springs
        grow, fade_in = 1.0, 1.0
        if self.attention.animate:
            age = now - self.opened_at
            if self._springy():
                grow = 0.6 + 0.4 * geo.ease_out_back(age / SPRING_S)
                fade_in = geo.ease_out(age / 0.18)
            else:
                fade_in = geo.ease_out(age / GROW_S)
                grow = 0.3 + 0.7 * fade_in
        width = self._panel_width() * grow
        self.see_width = width
        self._poke(now, hands, width)
        self.see_caps = seethrough.holes(self.panel_pose, hmd[:3, 3], hands, width, PANEL_PX)
        alpha, close = self.attention.update(now, gaze, hand_m, now - self.last_touch)
        alpha = min(alpha, fade_in)
        self.closing_from = alpha
        self.panel.width(width)
        self.panel.alpha(alpha)
        if close:
            self._close("looked away")

    def _poke(self, now: float, hands: dict, width: float) -> None:
        """a controller tip on the panel, as mouse events. with a tip right
        at it the panel stops being a laser target, so the laser and your
        hand don't both drive the cursor; pull away and the laser's back."""
        height = width * PANEL_PX[1] / PANEL_PX[0]
        local = {side: geo.to_local(self.panel_pose, geo.controller_tip(c, side))
                 for side, c in hands.items() if c is not None}
        pressing = next((s for s, p in self.pokes.items() if p.pressed), None)
        was = self.touch.touching
        touching = self.touch.update(list(local.values()), width, height, pressing is not None)
        self.panel.set_interactive(not touching)
        if touching:
            self._touch(now, local, width, height, pressing)
        elif was:
            for p in self.pokes.values():
                p.reset()
            self.poke_px = (-1.0, -1.0)
            self.bridge.panel_mouse.emit("leave", 0.0, 0.0)
        pressing = next((s for s, p in self.pokes.items() if p.pressed), None)
        self.see_cursor = seethrough.cursor(local, width, PANEL_PX, pressing)

    def _touch(self, now: float, local: dict, width: float, height: float,
               pressing: str | None) -> None:
        order = [pressing] if pressing else list(SIDES)
        for side in order:
            if side not in local:
                continue
            for kind, u, v in self.pokes[side].update(local[side], width, height):
                x, y = u * PANEL_PX[0], v * PANEL_PX[1]
                if kind == "move" and abs(x - self.poke_px[0]) < 0.5 and abs(y - self.poke_px[1]) < 0.5:
                    continue
                self.poke_px = (x, y)
                self.bridge.panel_mouse.emit(kind, x, y)
                if kind == "down":
                    self._buzz_hand(side, 0.02, 220, 0.8)
                elif kind == "up":
                    self._buzz_hand(side, 0.012, 260, 0.4)
            if self.pokes[side].hovering:
                self.last_touch = now
                break

    # ---- ring ----

    def _ring_setup(self, images: list, width_m: float) -> None:
        """[glow, rim] for this song. same layers again (a new song's
        colours) upload in place; anything else rebuilds them."""
        import openvr
        if len(self.rings) != len(images) or abs(self.ring_width - width_m) > 1e-6:
            for o in self.rings:
                o.destroy()
            self.rings = []
            for i, img in enumerate(images):
                o = _Overlay(self.ov, self._gl, f"tide.vr.ring{i}", f"tide ring {i}", width_m,
                             (img.width(), img.height()))
                # each eye gets its own half: its own lens outline
                self.ov.setOverlayFlag(o.h, openvr.VROverlayFlags_SideBySide_Parallel, True)
                o.place_on(openvr.k_unTrackedDeviceIndex_Hmd,
                           geo.translate(0.0, 0.0, -ringmod.DISTANCE_M))
                o.alpha(0.0)
                self.rings.append(o)
            self.ring_width = width_m
        for o, img in zip(self.rings, images):
            o.upload(img)

    def _ring_tick(self, now: float) -> None:
        if not self.rings:
            return
        mode = self.settings.get("ring", "off")
        if mode == "off" or not self._animate():
            for o in self.rings:
                o.show(False)
            return
        strength = ringmod.STRENGTH.get(self.settings.get("ring_strength", "normal"), 0.7)
        pulse = self.pulse if self.playing else 0.0
        # the flash limit judges the real jumps; the envelope then smooths
        # what's left to the headset's frame rate
        limited = self.flash.update(now, ringmod.glow_level(mode, pulse, self.playing))
        glow = self.envelope.update(now, limited) * strength
        rim_alpha, rim_scale = self.ripple.update(now, pulse)
        rim = rim_alpha * strength * (1.0 if mode == "kick" else 0.6)
        glow_o, rim_o = self.rings[0], self.rings[-1]
        glow_o.show(glow > 0.003)
        glow_o.alpha(glow)
        rim_o.show(rim > 0.003)
        rim_o.alpha(rim)
        # the ripple travels inward: the head-locked quad shrinks round
        # the middle of your view
        rim_o.width(self.ring_width * rim_scale)
