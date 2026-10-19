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
WATCH_SHOW_S = 0.3
WATCH_HIDE_S = 0.4
# aiming points the controller where you look (h3vr, 2026-10-19: aiming
# brought the watch up and froze the game). checking a watch holds the
# forearm across you, the controller pointing well off your gaze.
WATCH_AIM_CLEAR_DEG = 55.0
# look-to-open: keep the watch near the middle of your view this long
DWELL_S = 1.0
DWELL_LOOK_DEG = 15.0
POKE_M = 0.03              # a controller tip this close to the watch opens the panel
# the front tip of an index controller, in its own frame (right hand; the
# left mirrors x). steamvr's model calls it "tip" and the mesh ends there.
# touch used to come from the skeleton's index fingertip, which floats
# ~11 cm past the controller (2026-10-19: "move the touch point to the tip
# of the index controller instead of ahead of it").
TIP_ON_CONTROLLER = (-0.006, -0.015, 0.02)
# the controller and the hand round it, as a capsule from the head ring
# to the bottom of the grip (fitted to the index mesh), for seeing it
# through the panel
BODY_HEAD = (0.011, 0.0, 0.054)
BODY_BASE = (0.005, -0.028, 0.178)
BODY_RADIUS_M = 0.055

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


def aim_deg(hmd: np.ndarray, controller: np.ndarray) -> float:
    """angle between where the controller points (its -z) and where you
    look (the headset's -z). small means aiming down sights."""
    a, b = -controller[:3, 2], -hmd[:3, 2]
    cos = float(np.clip(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)), -1.0, 1.0))
    return math.degrees(math.acos(cos))


def checking_watch(hmd: np.ndarray, controller: np.ndarray, side: str) -> bool:
    """wrist close and in view, and not aimed along your gaze."""
    pos = wrist_point(controller, side)
    return looking_at_wrist(hmd, pos) and aim_deg(hmd, controller) >= WATCH_AIM_CLEAR_DEG


class Dwell:
    """look-to-open: fills while you look near the middle of your view,
    drains twice as fast when you don't. ``update`` returns 0..1."""

    def __init__(self, seconds: float = DWELL_S) -> None:
        self.seconds = seconds
        self.value = 0.0
        self._last: float | None = None

    def reset(self) -> None:
        self.value = 0.0
        self._last = None

    def update(self, now: float, looking: bool) -> float:
        dt = 0.0 if self._last is None else max(0.0, min(0.1, now - self._last))
        self._last = now
        rate = dt / self.seconds if self.seconds > 0 else 1.0
        self.value = min(1.0, self.value + rate) if looking else max(0.0, self.value - 2 * rate)
        return self.value


class FlashLimiter:
    """no more than ``per_second`` flashes a second, whatever the music
    does. a flash is the level jumping by ``jump`` or more; past the limit
    the level may only rise slowly, so fast hits blur into a swell.
    photosensitive seizure guidance draws the line at three a second, and
    in a headset you can't look away from it. always on, not a setting."""

    def __init__(self, per_second: int = 3, jump: float = 0.3, slow_rise_per_s: float = 1.5) -> None:
        self.per_second, self.jump, self.slow = per_second, jump, slow_rise_per_s
        self.level = 0.0
        self._flashes: list[float] = []
        self._last: float | None = None

    def update(self, now: float, target: float) -> float:
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        self._flashes = [t for t in self._flashes if now - t < 1.0]
        target = max(0.0, min(1.0, float(target)))
        if target - self.level >= self.jump:
            if len(self._flashes) < self.per_second:
                self._flashes.append(now)
                self.level = target
            else:
                self.level = min(target, self.level + self.slow * dt)
        elif target > self.level:
            self.level = target
        else:
            self.level = target          # falling is never a flash
        return self.level

def on_controller(controller: np.ndarray, side: str, point) -> np.ndarray:
    """a point given for the right controller, on either one."""
    x, y, z = point
    if side == "left":
        x = -x
    return (controller @ np.array([x, y, z, 1.0]))[:3]


def controller_tip(controller: np.ndarray, side: str) -> np.ndarray:
    return on_controller(controller, side, TIP_ON_CONTROLLER)


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


def ease_out_back(t: float, overshoot: float = 1.6) -> float:
    """modern's springy ease: past the target a little, then settles."""
    t = min(1.0, max(0.0, t)) - 1.0
    return 1.0 + t * t * ((overshoot + 1.0) * t + overshoot)


def to_local(pose: np.ndarray, point: np.ndarray) -> np.ndarray:
    """``point`` in ``pose``'s own frame: x right, y up, z out of its face."""
    return (np.linalg.inv(pose) @ np.append(np.asarray(point, float), 1.0))[:3]


class Poke:
    """a fingertip against a flat panel, as mouse events.

    the finger hovers within HOVER_M in front of the surface, presses when
    it reaches PRESS_M coming from the front (not when it starts behind
    the panel), drags while pressed, and lets go when it pulls back past
    RELEASE_M (the gap stops a shaky finger from double-clicking), slides
    off the edge, or goes right through (THROUGH_M)."""

    HOVER_M = 0.04
    PRESS_M = 0.006
    RELEASE_M = 0.016
    THROUGH_M = -0.06
    MARGIN_M = 0.01

    def __init__(self) -> None:
        self.pressed = False
        self.hovering = False
        self._last_z: float | None = None

    def reset(self) -> None:
        self.pressed = False
        self.hovering = False
        self._last_z = None

    def update(self, local: np.ndarray, width: float, height: float) -> list[tuple[str, float, float]]:
        """events as (kind, u, v), u and v 0..1 from the panel's top left."""
        x, y, z = (float(c) for c in local)
        inside = abs(x) <= width / 2 + self.MARGIN_M and abs(y) <= height / 2 + self.MARGIN_M
        u = min(1.0, max(0.0, x / width + 0.5))
        v = min(1.0, max(0.0, 0.5 - y / height))
        came_from_front = self._last_z is not None and self._last_z > self.PRESS_M
        self._last_z = z
        out: list[tuple[str, float, float]] = []
        if self.pressed:
            if z > self.RELEASE_M or z < self.THROUGH_M or not inside:
                self.pressed = False
                out.append(("up", u, v))
            else:
                out.append(("move", u, v))
        elif inside and z <= self.PRESS_M and z > self.THROUGH_M and came_from_front:
            self.pressed = True
            out += [("move", u, v), ("down", u, v)]
        elif inside and 0.0 < z < self.HOVER_M:
            out.append(("move", u, v))
        self.hovering = self.pressed or (inside and 0.0 < z < self.HOVER_M)
        return out


class TouchMode:
    """laser or touch, for the open panel. a controller tip right at the
    panel turns touch on (no laser, so the two don't fight over the
    cursor); it goes back to the laser once every tip has pulled away.

    this used to go by how far your hands were from the panel's middle,
    and the panel sits close enough that resting hands kept it in touch
    mode for good (2026-10-19: "laser doesn't work anymore")."""

    NEAR_M = 0.08              # a tip this close in front: touch
    FAR_M = 0.14               # every tip this far (or off to the side): laser again
    EDGE_M = 0.05              # how far past the panel's edge still counts
    BEHIND_M = -0.08

    def __init__(self) -> None:
        self.touching = False

    def reset(self) -> None:
        self.touching = False

    def _near(self, local, reach: float, width: float, height: float) -> bool:
        x, y, z = (float(c) for c in local)
        return (abs(x) <= width / 2 + self.EDGE_M and abs(y) <= height / 2 + self.EDGE_M
                and self.BEHIND_M < z < reach)

    def update(self, tips: list, width: float, height: float, pressed: bool) -> bool:
        """``tips``: each controller tip in the panel's frame."""
        if not self.touching:
            self.touching = any(self._near(t, self.NEAR_M, width, height) for t in tips)
        elif not pressed and not any(self._near(t, self.FAR_M, width, height) for t in tips):
            self.touching = False
        return self.touching


def through_panel(panel: np.ndarray, eye: np.ndarray, point: np.ndarray,
                  radius: float) -> tuple[float, float, float, bool] | None:
    """where a ball at ``point`` lands on the panel as seen from ``eye``:
    (x, y, radius) in the panel's frame, and whether it's in front of the
    panel. None when you're behind the panel yourself."""
    e = to_local(panel, eye)
    p = to_local(panel, point)
    if e[2] <= 0.05:
        return None
    in_front = p[2] > 0.0
    # behind the panel it lands where it is; right at your eye it'd cover
    # everything, so hold it a little way out
    pz = min(max(float(p[2]), 0.0), float(e[2]) - 0.05)
    k = float(e[2]) / (float(e[2]) - pz)
    return (float(e[0] + (p[0] - e[0]) * k), float(e[1] + (p[1] - e[1]) * k),
            radius * k, in_front)
