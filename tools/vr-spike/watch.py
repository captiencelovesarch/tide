"""the wrist watch: a small tide face that shows up on your wrist when you
look at it. poke it or laser it and the panel opens.

built only on input that reaches an overlay while a game has focus
(2026-10-19): tracking poses, and laser clicks on our own overlays. no
buttons.
"""
from __future__ import annotations

import math
import time

import numpy as np

# wrist position in the index skeleton's model space, read by `check` on
# 2026-10-18 (left hand; the right mirrors x). model space should line up
# with the controller's raw pose, so the watch rides the controller.
WRIST_LEFT = np.array([-0.034, 0.0365, 0.1647])
LIFT_M = 0.03          # sit above the wrist, not inside the forearm
NEAR_M = 0.6           # wrist at most this far from your head...
LOOK_DEG = 25.0        # ...and within this angle of where you're facing
SHOW_S = 0.15          # looked at this long before it appears
HIDE_S = 0.5           # looked away this long before it goes
POKE_M = 0.025         # fingertip this close to its center opens the panel
# where the index fingertip roughly is on a controller, if the skeleton
# isn't there: a little below and ahead of the tracking origin
FINGER_ON_CONTROLLER = np.array([0.0, -0.015, -0.09])


def wrist_point(ctrl: np.ndarray, side: str) -> np.ndarray:
    off = WRIST_LEFT.copy()
    if side == "right":
        off[0] = -off[0]
    p = (ctrl @ np.append(off, 1.0))[:3]
    p[1] += LIFT_M
    return p


def looking_at(hmd: np.ndarray, point: np.ndarray) -> tuple[bool, float, float]:
    """(near and in view, distance m, angle off your facing in degrees)."""
    head = hmd[:3, 3]
    forward = -hmd[:3, 2]
    v = point - head
    d = float(np.linalg.norm(v))
    if d < 1e-6:
        return False, d, 180.0
    ang = math.degrees(math.acos(float(np.clip(np.dot(forward, v / d), -1.0, 1.0))))
    return d <= NEAR_M and ang <= LOOK_DEG, d, ang


def fingertip(s, side: str) -> tuple[np.ndarray | None, str]:
    """the index fingertip in tracking space: from the hand skeleton when
    it's there, else a fixed point on the controller."""
    tips, root = s.bone_tips(side), s.action_pose(side)
    if tips and root is not None:
        return (root @ np.append(np.array(tips["index"]), 1.0))[:3], "skeleton"
    ctrl = s.controller(side)
    if ctrl is not None:
        return (ctrl[0] @ np.append(FINGER_ON_CONTROLLER, 1.0))[:3], "controller"
    return None, ""


def face(progress: float):
    """the watch face: a dark disc, the song's progress as a ring, "tide"."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
    px = Watch.PX
    img = QImage(px, px, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(10, 10, px - 20, px - 20)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(16, 16, 16, 235))
    p.drawEllipse(r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 60), 12))
    p.drawEllipse(r.adjusted(8, 8, -8, -8))
    pen = QPen(QColor("#ffd400"), 12)
    pen.setCapStyle(Qt.FlatCap)
    p.setPen(pen)
    p.drawArc(r.adjusted(8, 8, -8, -8), 90 * 16, -int(360 * 16 * progress))
    f = QFont("monospace")
    f.setPixelSize(54)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#f5f5f5"))
    p.drawText(r, Qt.AlignCenter, "tide")
    p.end()
    return img


class Watch:
    PX = 256
    WIDTH_M = 0.045

    def __init__(self, s, loop, side: str = "left") -> None:
        from vrcore import Overlay
        self.s, self.loop, self.side = s, loop, side
        self.other = "right" if side == "left" else "left"
        self.ov = Overlay(s, "watch", "tide spike watch", self.WIDTH_M)
        self.ov.mouse_input(self.PX, self.PX)
        self.shown = False
        self._seen_at: float | None = None
        self._lost_at: float | None = None
        self._last_draw = 0.0
        self._last_note = 0.0
        self.shows = 0
        self.closest: float | None = None
        self.closest_source = ""
        self.look_samples: list[tuple[float, float]] = []

    def _say(self, text: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)

    def _set(self, on: bool) -> None:
        if on == self.shown:
            return
        self.shown = on
        if on:
            self.shows += 1
            self._last_draw = 0.0
            self.ov.show()
        else:
            self.ov.hide()

    def tick(self) -> None:
        import openvr
        from vrcore import facing
        now = time.perf_counter()
        if self.loop.visible:
            self._set(False)
            self._seen_at = self._lost_at = None
            return
        hmd = self.s.hmd()
        ctrl = self.s.controller(self.side)
        if hmd is None or ctrl is None:
            self._set(False)
            return
        pos = wrist_point(ctrl[0], self.side)
        ok, dist, ang = looking_at(hmd, pos)
        if dist <= NEAR_M:
            self.look_samples.append((round(dist, 3), round(ang, 1)))
        if ok:
            self._lost_at = None
            self._seen_at = self._seen_at or now
            want = self.shown or now - self._seen_at >= SHOW_S
        else:
            self._seen_at = None
            self._lost_at = self._lost_at or now
            want = self.shown and now - self._lost_at < HIDE_S
        if want and not self.shown:
            self._say(f"watch up ({dist * 100:.0f} cm away, {ang:.0f}° off center)")
        self._set(want)
        if not self.shown:
            return

        self.ov.place(facing(pos, hmd[:3, 3]))
        if now - self._last_draw >= 0.25:
            self.ov.upload(face((time.time() % 30.0) / 30.0))
            self._last_draw = now

        for kind, _x, _y, _b in self.ov.events():
            if kind == openvr.VREvent_MouseButtonDown:
                self.open("laser")
                return

        tip, source = fingertip(self.s, self.other)
        if tip is None:
            return
        d = float(np.linalg.norm(tip - pos))
        if self.closest is None or d < self.closest:
            self.closest, self.closest_source = d, source
        if now - self._last_note >= 1.0:
            self._last_note = now
            self._say(f"watch: {source} fingertip {d * 100:.1f} cm away")
        if d <= POKE_M:
            self.open(f"poke ({source})")

    def open(self, how: str) -> None:
        self._set(False)
        self.loop.open_panel(f"watch {how}")

    def report(self) -> dict:
        looks = self.look_samples
        return {
            "watch_shows": self.shows,
            "closest_fingertip_cm": None if self.closest is None else round(self.closest * 100, 1),
            "fingertip_source": self.closest_source,
            # how far and how far off-center your wrist was whenever it came
            # within reach: what to tune NEAR_M and LOOK_DEG against
            "wrist_near_samples": len(looks),
            "wrist_angle_p10_p50": ([float(np.percentile([a for _, a in looks], q)) for q in (10, 50)]
                                    if looks else None),
        }
