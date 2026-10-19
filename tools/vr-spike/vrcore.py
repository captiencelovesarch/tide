"""openvr plumbing for the spike: one session, matrices, actions, overlays.

everything here runs on one thread (the caller's). the real plugin splits
a vr thread from the gui thread; the spike measures whether it has to.
"""
from __future__ import annotations

import ctypes
import os
import tempfile
import time
from pathlib import Path

import numpy as np
import openvr

from paths import OUT, out_dir  # noqa: F401  (re-exported for spike.py)

HERE = Path(__file__).resolve().parent
NO_DEVICE = openvr.k_ulInvalidInputValueHandle
SIDES = ("left", "right")

# steamvr hand skeleton bone indices (same on every skeleton action)
BONE_WRIST = 1
BONE_THUMB_TIP = 5
BONE_INDEX_TIP = 10
BONE_COUNT = 31


# ---------- matrices ----------

def to_np(m34) -> np.ndarray:
    a = np.eye(4)
    for i in range(3):
        for j in range(4):
            a[i, j] = m34.m[i][j]
    return a


def to_m34(a: np.ndarray) -> openvr.HmdMatrix34_t:
    m = openvr.HmdMatrix34_t()
    for i in range(3):
        for j in range(4):
            m.m[i][j] = float(a[i, j])
    return m


def translate(x: float, y: float, z: float) -> np.ndarray:
    a = np.eye(4)
    a[:3, 3] = (x, y, z)
    return a


def rot_x(deg: float) -> np.ndarray:
    r = np.radians(deg)
    a = np.eye(4)
    a[1, 1], a[1, 2], a[2, 1], a[2, 2] = np.cos(r), -np.sin(r), np.sin(r), np.cos(r)
    return a


def facing(pos: np.ndarray, viewer: np.ndarray) -> np.ndarray:
    """a pose at ``pos`` whose +z points at ``viewer``: an overlay quad is
    seen from its +z side, so this one faces you. kept upright."""
    z = viewer - pos
    z /= np.linalg.norm(z)
    x = np.cross((0.0, 1.0, 0.0), z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    a = np.eye(4)
    a[:3, 0], a[:3, 1], a[:3, 2], a[:3, 3] = x, y, z, pos
    return a


def spawn_in_front(hmd: np.ndarray, distance: float = 0.55, drop_deg: float = 15.0) -> np.ndarray:
    """where the design puts the panel: ahead of you along your heading
    (yaw only), a little below eye line, turned to face your eyes."""
    head = hmd[:3, 3]
    fwd = -hmd[:3, 2]
    fwd[1] = 0.0
    n = np.linalg.norm(fwd)
    fwd = fwd / n if n > 1e-6 else np.array([0.0, 0.0, -1.0])
    pos = head + fwd * distance
    pos[1] -= distance * np.tan(np.radians(drop_deg))
    return facing(pos, head)


# ---------- session ----------

class Session:
    """openvr as an overlay app, plus our action set."""

    def __init__(self, *, with_input: bool = True) -> None:
        if not openvr.isRuntimeInstalled():
            raise SystemExit("steamvr isn't installed")
        try:
            self.system = openvr.init(openvr.VRApplication_Overlay)
        except openvr.OpenVRError as exc:
            raise SystemExit(f"couldn't start openvr ({exc}). is steamvr running?")
        self.overlay = openvr.VROverlay()
        self.poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()
        self.quit = False
        self.input = None
        self.input_error = ""
        self.gl: "GlUploader | None" = None
        # last error per input read ("curl left": "InputError_NoData"), so a
        # null in a report comes with a reason
        self.input_errors: dict[str, str] = {}
        if with_input:
            try:
                self._init_input()
            except openvr.OpenVRError as exc:
                self.input = None
                self.input_error = str(exc)

    def _init_input(self) -> None:
        inp = openvr.VRInput()
        inp.setActionManifestPath(str(HERE / "actions.json"))
        self.actions = {
            name: inp.getActionHandle(f"/actions/tide/in/{name}")
            for name in ("stick_left", "stick_right", "hand_left", "hand_right",
                         "pose_left", "pose_right")
        }
        self.haptic = inp.getActionHandle("/actions/tide/out/haptic")
        self.sources = {side: inp.getInputSourceHandle(f"/user/hand/{side}") for side in SIDES}
        self.sets = (openvr.VRActiveActionSet_t * 1)()
        self.sets[0].ulActionSet = inp.getActionSetHandle("/actions/tide")
        self.bones = (openvr.VRBoneTransform_t * BONE_COUNT)()
        self.input = inp

    def update(self) -> None:
        """once per tick: device poses, system events, action state."""
        self.system.getDeviceToAbsoluteTrackingPose(
            openvr.TrackingUniverseStanding, 0.0, self.poses)
        ev = openvr.VREvent_t()
        while self.system.pollNextEvent(ev):
            if ev.eventType in (openvr.VREvent_Quit, openvr.VREvent_DriverRequestedQuit):
                # without the ack steamvr waits on us at shutdown
                self.system.acknowledgeQuit_Exiting()
                self.quit = True
        if self.input is not None:
            try:
                self.input.updateActionState(self.sets)
            except openvr.OpenVRError:
                pass

    def shutdown(self) -> None:
        if self.gl is not None:
            self.gl.release()
            self.gl = None
        openvr.shutdown()

    # ---- reading ----

    def hmd(self) -> np.ndarray | None:
        p = self.poses[openvr.k_unTrackedDeviceIndex_Hmd]
        return to_np(p.mDeviceToAbsoluteTracking) if p.bPoseIsValid else None

    def controller_index(self, side: str) -> int | None:
        role = (openvr.TrackedControllerRole_LeftHand if side == "left"
                else openvr.TrackedControllerRole_RightHand)
        idx = self.system.getTrackedDeviceIndexForControllerRole(role)
        return None if idx == openvr.k_unTrackedDeviceIndexInvalid else idx

    def controller(self, side: str) -> tuple[np.ndarray, np.ndarray] | None:
        """(pose, velocity) from the tracking system."""
        idx = self.controller_index(side)
        if idx is None:
            return None
        p = self.poses[idx]
        if not p.bPoseIsValid:
            return None
        return to_np(p.mDeviceToAbsoluteTracking), np.array(p.vVelocity.v[:3])

    def stick(self, side: str) -> bool:
        if self.input is None:
            return False
        try:
            d = self.input.getDigitalActionData(self.actions[f"stick_{side}"], NO_DEVICE)
        except openvr.OpenVRError as exc:
            self.input_errors[f"stick {side}"] = type(exc).__name__
            return False
        if not d.bActive:
            # inactive = no binding drives it (yet)
            self.input_errors[f"stick {side}"] = "not active (no binding loaded)"
        else:
            self.input_errors.pop(f"stick {side}", None)
        return bool(d.bActive and d.bState)

    def curls(self, side: str) -> list[float] | None:
        """thumb, index, middle, ring, pinky. 0 = straight, 1 = fist."""
        if self.input is None:
            return None
        try:
            s = self.input.getSkeletalSummaryData(
                self.actions[f"hand_{side}"], openvr.VRSummaryType_FromDevice)
        except openvr.OpenVRError as exc:
            self.input_errors[f"curl {side}"] = type(exc).__name__
            return None
        self.input_errors.pop(f"curl {side}", None)
        return [round(float(c), 3) for c in s.flFingerCurl]

    def action_pose(self, side: str) -> np.ndarray | None:
        if self.input is None:
            return None
        try:
            d = self.input.getPoseActionDataRelativeToNow(
                self.actions[f"pose_{side}"], openvr.TrackingUniverseStanding, 0.0, NO_DEVICE)
        except openvr.OpenVRError:
            return None
        if not (d.bActive and d.pose.bPoseIsValid):
            return None
        return to_np(d.pose.mDeviceToAbsoluteTracking)

    def bone_tips(self, side: str) -> dict[str, list[float]] | None:
        """wrist, thumb tip, index tip in the skeleton's model space.

        model space is relative to the skeleton root, which should line up
        with the raw pose action. the recordings keep both so that
        assumption can be checked offline."""
        if self.input is None:
            return None
        try:
            self.input.getSkeletalBoneData(
                self.actions[f"hand_{side}"], openvr.VRSkeletalTransformSpace_Model,
                openvr.VRSkeletalMotionRange_WithoutController, self.bones)
        except openvr.OpenVRError as exc:
            self.input_errors[f"bones {side}"] = type(exc).__name__
            return None
        self.input_errors.pop(f"bones {side}", None)
        out = {}
        for name, i in (("wrist", BONE_WRIST), ("thumb", BONE_THUMB_TIP), ("index", BONE_INDEX_TIP)):
            v = self.bones[i].position.v
            out[name] = [round(float(v[0]), 4), round(float(v[1]), 4), round(float(v[2]), 4)]
        return out

    def buzz(self, side: str, seconds: float = 0.03, freq: float = 160.0, amp: float = 0.6) -> None:
        if self.input is None:
            return
        try:
            self.input.triggerHapticVibrationAction(
                self.haptic, 0.0, seconds, freq, amp, self.sources[side])
        except openvr.OpenVRError:
            pass

    def display_hz(self) -> float:
        hz = self.system.getFloatTrackedDeviceProperty(
            openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_DisplayFrequency_Float)
        return float(hz) if hz and hz > 10 else 90.0

    def hmd_model(self) -> str:
        try:
            return self.system.getStringTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_ModelNumber_String)
        except openvr.OpenVRError:
            return "?"

    def focus_app(self) -> str:
        """the app key of whatever game has focus, for per-game pinned spots."""
        try:
            pid = openvr.VRCompositor().getCurrentSceneFocusProcess()
            if not pid:
                return "(none)"
            return openvr.VRApplications().getApplicationKeyByProcessId(pid) or f"pid {pid}"
        except Exception as exc:  # noqa: BLE001 - this is a probe
            return f"error: {exc}"


# ---------- overlays ----------

# how a QImage gets to steamvr:
#   gl    an opengl texture handed over with setOverlayTexture. vrclient
#         copies it into a shared vulkan image in our process
#         (CVRSharedGLTexture, GL_EXT_memory_object_fd), the path wayvr's
#         constant 2560x1440 updates take without trouble. the plan.
#   file  setOverlayFromFile on a png. one upload is fine (`png`), but at
#         10 fps it wrapped the compositor's transfer ring and vrcompositor
#         segfaulted (2026-10-19). capped at 1 fps, ask for it explicitly.
#   raw   setOverlayRaw. crashed vrcompositor on the first panel run
#         (2026-10-18, "Vulkan transfer ring buffer overflow"). re-test only.
UPLOAD_MODES = ("gl", "file", "raw")
MAX_FPS = {"gl": 30, "file": 1, "raw": 1}
# an upload that blocks this long means the compositor is gone: after it
# crashed, every setOverlayFromFile waited ~1 s for a reply that never came
STALL_S = 0.5
GL_INTEROP_EXTENSIONS = ("GL_EXT_memory_object", "GL_EXT_memory_object_fd",
                         "GL_EXT_semaphore", "GL_EXT_semaphore_fd")


class GlUploader:
    """one offscreen opengl context per session, kept current on this thread."""

    def __init__(self) -> None:
        from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext
        self.ctx = QOpenGLContext()
        if not self.ctx.create():
            raise RuntimeError("couldn't create an opengl context")
        self.surface = QOffscreenSurface()
        self.surface.setFormat(self.ctx.format())
        self.surface.create()
        if not self.ctx.makeCurrent(self.surface):
            raise RuntimeError("couldn't make the opengl context current")
        fmt = self.ctx.format()
        self.info = (f"{QGuiApplication.platformName()} · "
                     f"{'gles' if self.ctx.isOpenGLES() else 'gl'} "
                     f"{fmt.majorVersion()}.{fmt.minorVersion()}")
        # vrclient needs these to share our texture with its vulkan side;
        # without them it falls back or fails (see CVRSharedGLTexture)
        self.missing = [e for e in GL_INTEROP_EXTENSIONS if not self.ctx.hasExtension(e.encode())]
        if self.missing:
            self.info += f" · missing {', '.join(self.missing)}"
        self.textures: list = []

    def texture(self, img, old=None):
        """upload ``img`` into ``old`` (same size) or a new texture."""
        from PySide6.QtGui import QImage
        from PySide6.QtOpenGL import QOpenGLTexture
        self.ctx.makeCurrent(self.surface)
        img = img.convertToFormat(QImage.Format_RGBA8888)
        tex = old
        if tex is None or tex.width() != img.width() or tex.height() != img.height():
            if tex is not None:
                tex.destroy()
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            self.textures.append(tex)
            tex.setData(img, QOpenGLTexture.DontGenerateMipMaps)
        else:
            # the QImage overload re-runs format/size/mip setup, which an
            # allocated texture refuses (a warning and GL_INVALID_OPERATION
            # per frame); a plain pixel upload is all a redraw needs
            tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, img.constBits())
        self.ctx.functions().glFlush()
        return tex

    def release(self) -> None:
        if self.ctx.makeCurrent(self.surface):
            for tex in self.textures:
                tex.destroy()
            self.ctx.doneCurrent()
        self.textures.clear()


class Overlay:
    def __init__(self, session: Session, key: str, name: str, width_m: float,
                 upload: str = "gl") -> None:
        if upload not in UPLOAD_MODES:
            raise ValueError(upload)
        self.s = session
        self.ov = session.overlay
        self.h = self.ov.createOverlay(f"tide.spike.{key}", name)
        self.ov.setOverlayWidthInMeters(self.h, width_m)
        self.mode = upload
        self.error = ""
        self._tex = None
        self._flip_set = False
        self.gl_flip = False
        # two files, alternated, so steamvr never reads one we're rewriting
        self._files = [Path(tempfile.gettempdir()) / f"tide-spike-{os.getpid()}-{key}-{n}.png"
                       for n in (0, 1)]
        self._n = 0

    def show(self) -> None:
        self.ov.showOverlay(self.h)

    def hide(self) -> None:
        self.ov.hideOverlay(self.h)

    def place(self, pose: np.ndarray) -> None:
        self.ov.setOverlayTransformAbsolute(self.h, openvr.TrackingUniverseStanding, to_m34(pose))

    def place_on(self, device: int, offset: np.ndarray) -> None:
        self.ov.setOverlayTransformTrackedDeviceRelative(self.h, device, to_m34(offset))

    def width(self, meters: float) -> None:
        self.ov.setOverlayWidthInMeters(self.h, meters)

    def alpha(self, a: float) -> None:
        self.ov.setOverlayAlpha(self.h, float(a))

    def color(self, r: float, g: float, b: float) -> None:
        self.ov.setOverlayColor(self.h, float(r), float(g), float(b))

    def flag(self, flag: int, on: bool = True) -> None:
        self.ov.setOverlayFlag(self.h, flag, on)

    def from_file(self, path: str | Path) -> None:
        self.ov.setOverlayFromFile(self.h, str(Path(path).resolve()))

    def upload(self, img) -> float:
        """push a QImage the way ``self.mode`` says. returns milliseconds
        spent on our side (steamvr's own copy isn't in it)."""
        t0 = time.perf_counter()
        if self.mode == "gl":
            self._upload_gl(img)
        elif self.mode == "raw":
            self._upload_raw(img)
        else:
            path = self._files[self._n % 2]
            self._n += 1
            # png "quality" is compression in qt: 100 = none, fastest
            img.save(str(path), "PNG", 100)
            self.from_file(path)
        return (time.perf_counter() - t0) * 1000.0

    def _upload_gl(self, img) -> None:
        if self.s.gl is None:
            self.s.gl = GlUploader()
            print(f"opengl uploads: {self.s.gl.info}")
        self._tex = self.s.gl.texture(img, self._tex)
        t = openvr.Texture_t()
        t.handle = int(self._tex.textureId())
        t.eType = openvr.TextureType_OpenGL
        t.eColorSpace = openvr.ColorSpace_Auto
        self.ov.setOverlayTexture(self.h, t)
        if self.gl_flip and not self._flip_set:
            bounds = openvr.VRTextureBounds_t()
            bounds.uMin, bounds.uMax, bounds.vMin, bounds.vMax = 0.0, 1.0, 1.0, 0.0
            self.ov.setOverlayTextureBounds(self.h, bounds)
            self._flip_set = True

    def _upload_raw(self, img) -> None:
        from PySide6.QtGui import QImage
        rgba = img.convertToFormat(QImage.Format_RGBA8888)
        data = bytes(rgba.constBits())
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        self.ov.setOverlayRaw(self.h, buf, rgba.width(), rgba.height(), 4)

    def mouse_input(self, w: int, h: int) -> None:
        self.ov.setOverlayInputMethod(self.h, openvr.VROverlayInputMethod_Mouse)
        scale = openvr.HmdVector2_t()
        scale.v[0], scale.v[1] = float(w), float(h)
        self.ov.setOverlayMouseScale(self.h, scale)
        # steamvr draws its own laser at the panel outside the dashboard
        self.flag(openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible)
        self.flag(openvr.VROverlayFlags_SendVRSmoothScrollEvents)

    def events(self):
        """(type, x, y, button) for each queued overlay event. y comes in gl
        space, origin bottom left; callers flip it."""
        ev = openvr.VREvent_t()
        while True:
            ok, ev = self.ov.pollNextOverlayEvent(self.h, ev)
            if not ok:
                return
            m = ev.data.mouse
            yield ev.eventType, float(m.x), float(m.y), int(m.button)

    def destroy(self) -> None:
        try:
            self.ov.destroyOverlay(self.h)
        except openvr.OpenVRError:
            pass
        for path in self._files:
            path.unlink(missing_ok=True)


class Stats:
    """p50 / p95 / max of a stream of numbers, for the spike's report."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def add(self, v: float) -> None:
        self.values.append(v)

    def summary(self) -> dict:
        if not self.values:
            return {"n": 0}
        a = np.array(self.values)
        return {"n": len(a), "p50": round(float(np.percentile(a, 50)), 2),
                "p95": round(float(np.percentile(a, 95)), 2), "max": round(float(a.max()), 2)}
