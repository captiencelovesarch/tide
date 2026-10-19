"""The plugin's spatial logic as plain numpy: where things go, whether
you're looking, when the panel fades. No openvr here, so all of it is
testable offline. Poses are 4x4 matrices in steamvr's standing space
(metres, +y up, a device looks down its -z).
"""
from __future__ import annotations

import math

import numpy as np

# the wrist in the index hand skeleton's model space, read from the
# spike's `check` (2026-10-18). left hand; the right mirrors x. model
# space lines up with the controller's raw pose, so the watch rides it.
WRIST_LEFT = np.array([-0.034, 0.0365, 0.1647])
WATCH_LIFT_M = 0.03        # above the wrist, not inside the forearm
WATCH_NEAR_M = 0.6         # wrist at most this far from your head...
WATCH_LOOK_DEG = 25.0      # ...and within this angle of your facing
WATCH_SHOW_S = 0.15
WATCH_HIDE_S = 0.5
POKE_M = 0.025             # fingertip this close to the watch opens the panel
# roughly where the index fingertip sits on a controller, for when the
# hand skeleton isn't available
FINGER_ON_CONTROLLER = np.array([0.0, -0.015, -0.09])

PANEL_DISTANCE_M = 0.55
PANEL_DROP_DEG = 15.0


def translate(x: float, y: float, z: float) -> np.ndarray:
    a = np.eye(4)
    a[:3, 3] = (x, y, z)
    return a


def facing(pos: np.ndarray, viewer: np.ndarray) -> np.ndarray:
    """a pose at ``pos`` whose +z points at ``viewer`` (an overlay is seen
    from its +z side), kept upright."""
    z = np.asarray(viewer, float) - pos
    n = np.linalg.norm(z)
    z = z / n if n > 1e-9 else np.array([0.0, 0.0, 1.0])
    x = np.cross((0.0, 1.0, 0.0), z)
    nx = np.linalg.norm(x)
    x = x / nx if nx > 1e-9 else np.array([1.0, 0.0, 0.0])
    y = np.cross(z, x)
    a = np.eye(4)
    a[:3, 0], a[:3, 1], a[:3, 2], a[:3, 3] = x, y, z, pos
    return a


def spawn_in_front(hmd: np.ndarray, distance: float = PANEL_DISTANCE_M,
                   drop_deg: float = PANEL_DROP_DEG) -> np.ndarray:
    """ahead along your heading (yaw only), a little below eye line,
    turned to face your eyes: where you'd hold a phone."""
    head = hmd[:3, 3]
    fwd = -hmd[:3, 2].copy()
    fwd[1] = 0.0
    n = np.linalg.norm(fwd)
    fwd = fwd / n if n > 1e-6 else np.array([0.0, 0.0, -1.0])
    pos = head + fwd * distance
    pos[1] -= distance * math.tan(math.radians(drop_deg))
    return facing(pos, head)


def wrist_point(controller: np.ndarray, side: str) -> np.ndarray:
    off = WRIST_LEFT.copy()
    if side == "right":
        off[0] = -off[0]
    p = (controller @ np.append(off, 1.0))[:3]
    p[1] += WATCH_LIFT_M
    return p


def off_center_deg(hmd: np.ndarray, point: np.ndarray) -> tuple[float, float]:
    """(angle between your facing and ``point`` in degrees, distance m)."""
    head = hmd[:3, 3]
    v = np.asarray(point, float) - head
    d = float(np.linalg.norm(v))
    if d < 1e-6:
        return 180.0, d
    fwd = -hmd[:3, 2]
    cos = float(np.clip(np.dot(fwd, v / d), -1.0, 1.0))
    return math.degrees(math.acos(cos)), d


def looking_at_wrist(hmd: np.ndarray, point: np.ndarray) -> bool:
    ang, d = off_center_deg(hmd, point)
    return d <= WATCH_NEAR_M and ang <= WATCH_LOOK_DEG


def fingertip_from_controller(controller: np.ndarray) -> np.ndarray:
    return (controller @ np.append(FINGER_ON_CONTROLLER, 1.0))[:3]


def fingertip_from_skeleton(root: np.ndarray, tip_model: list[float]) -> np.ndarray:
    return (root @ np.append(np.asarray(tip_model, float), 1.0))[:3]


class Shown:
    """a show/hide decision with a delay each way, so a glance doesn't
    flicker it."""

    def __init__(self, show_s: float, hide_s: float) -> None:
        self.show_s, self.hide_s = show_s, hide_s
        self.on = False
        self._since: float | None = None

    def update(self, now: float, wanted: bool) -> bool:
        if wanted == self.on:
            self._since = None
            return self.on
        if self._since is None:
            self._since = now
        if now - self._since >= (self.show_s if wanted else self.hide_s):
            self.on = wanted
            self._since = None
        return self.on

    def reset(self) -> None:
        self.on = False
        self._since = None


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


class Attention:
    """eyes keep it, hands open it (design/steamvr.md).

    each tick gets where you're looking, how close your hands are and when
    you last touched the panel, and answers how visible the panel should
    be. looking away for ``linger`` seconds starts a fade; looking back
    during the fade brings it back. the fade reaching zero means close.
    """

    GAZE_FULL_DEG = 20.0      # looking within this: full attention
    GAZE_NONE_DEG = 40.0      # past this: none
    HAND_NEAR_M = 0.30
    TOUCH_KEEPS_S = 3.0
    DIM_ALPHA = 0.8           # looking near it, not at it: a hint it may go

    def __init__(self, linger_s: float = 2.5, fade_s: float = 0.6, animate: bool = True) -> None:
        self.linger_s, self.fade_s, self.animate = linger_s, fade_s, animate
        self.alpha = 1.0
        self._low_since: float | None = None
        self._last = None

    def reset(self, now: float) -> None:
        self.alpha = 1.0
        self._low_since = None
        self._last = now

    def score(self, gaze_deg: float, hand_m: float, since_touch_s: float) -> float:
        span = self.GAZE_NONE_DEG - self.GAZE_FULL_DEG
        gaze = min(1.0, max(0.0, (self.GAZE_NONE_DEG - gaze_deg) / span))
        hand = 1.0 if hand_m <= self.HAND_NEAR_M else 0.0
        touch = 1.0 if since_touch_s <= self.TOUCH_KEEPS_S else 0.0
        return max(gaze, hand, touch)

    def update(self, now: float, gaze_deg: float, hand_m: float,
               since_touch_s: float) -> tuple[float, bool]:
        """(alpha to show the panel at, whether to close it now)."""
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        s = self.score(gaze_deg, hand_m, since_touch_s)
        if s >= 0.5:
            self._low_since = None
            target = 1.0 if s >= 1.0 else self.DIM_ALPHA
        else:
            if self._low_since is None:
                self._low_since = now
            fading = now - self._low_since >= self.linger_s
            target = 0.0 if fading else self.DIM_ALPHA
        if not self.animate:
            self.alpha = target
        else:
            step = dt / self.fade_s if self.fade_s > 0 else 1.0
            if self.alpha < target:
                self.alpha = min(target, self.alpha + step)
            else:
                self.alpha = max(target, self.alpha - step)
        return self.alpha, self.alpha <= 0.0
