"""gesture detectors: one pose sample in, maybe a fire out.

no openvr here, so the same code runs live in the headset and offline
over recordings (``spike.py replay``). this is the seed of the plugin's
``tests/test_vr_gestures.py``: every gesture must fire on its own
recordings and never on recordings of normal play.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SIDES = ("left", "right")


@dataclass
class Hand:
    pose: np.ndarray | None = None       # controller, tracking space
    velocity: np.ndarray | None = None
    curl: list[float] | None = None      # thumb, index, middle, ring, pinky
    tips: dict | None = None             # model-space wrist / thumb / index
    apose: np.ndarray | None = None      # raw pose action (the skeleton's root)


@dataclass
class Sample:
    t: float
    hmd: np.ndarray | None
    hands: dict[str, Hand] = field(default_factory=dict)
    sticks: dict[str, bool] = field(default_factory=dict)

    def to_json(self) -> dict:
        def m(a):
            return None if a is None else [round(float(x), 4) for x in a.reshape(-1)[:12]]
        out = {"t": round(self.t, 4), "hmd": m(self.hmd),
               "sticks": [int(self.sticks.get(s, False)) for s in SIDES]}
        for s in SIDES:
            h = self.hands.get(s)
            if h is None:
                continue
            out[s[0].upper()] = {
                "pose": m(h.pose),
                "vel": None if h.velocity is None else [round(float(v), 3) for v in h.velocity],
                "curl": h.curl,
                "tips": h.tips,
                "apose": m(h.apose),
            }
        return out

    @classmethod
    def from_json(cls, d: dict) -> "Sample":
        def m(v):
            if v is None:
                return None
            a = np.eye(4)
            a[:3, :] = np.array(v, dtype=float).reshape(3, 4)
            return a
        hands = {}
        for s in SIDES:
            h = d.get(s[0].upper())
            if h:
                vel = h.get("vel")
                hands[s] = Hand(m(h.get("pose")), None if vel is None else np.array(vel),
                                h.get("curl"), h.get("tips"), m(h.get("apose")))
        sticks = d.get("sticks") or [0, 0]
        return cls(d["t"], m(d.get("hmd")), hands, {"left": bool(sticks[0]), "right": bool(sticks[1])})


class TripleClick:
    """both sticks clicked together, three times, quickly."""

    name = "stick triple-click"
    PAIR_S = 0.15      # the two clicks of one "both" land this close
    WINDOW_S = 1.2     # three pairs inside this

    def __init__(self) -> None:
        self.prev = {s: False for s in SIDES}
        self.down: dict[str, float | None] = {s: None for s in SIDES}
        self.pairs: list[float] = []

    def feed(self, s: Sample) -> bool:
        for side in SIDES:
            now = bool(s.sticks.get(side))
            if now and not self.prev[side]:
                self.down[side] = s.t
            self.prev[side] = now
        left, right = self.down["left"], self.down["right"]
        if left is not None and right is not None:
            if abs(left - right) <= self.PAIR_S:
                self.pairs.append(max(left, right))
            self.down = {side: None for side in SIDES}
        else:
            for side in SIDES:
                d = self.down[side]
                if d is not None and s.t - d > self.PAIR_S:
                    self.down[side] = None     # a lone click, not a pair
        self.pairs = [p for p in self.pairs if s.t - p <= self.WINDOW_S]
        if len(self.pairs) >= 3:
            self.pairs.clear()
            return True
        return False


class Headphones:
    """an open hand held next to the ear on the same side.

    "open" is the vivecraft bow guard: drawing a bow (trigger held) or
    holding anything curls the fingers; an open hand at the ear doesn't."""

    name = "headphones"
    EAR_SIDE_M = 0.08    # ear sits this far along the head's right axis
    EAR_DROP_M = 0.02
    RADIUS_M = 0.15      # controller origin to ear point
    OPEN_CURL = 0.3      # index..pinky all under this
    HOLD_S = 0.5
    COOLDOWN_S = 1.0

    def __init__(self) -> None:
        self.since: dict[str, float | None] = {s: None for s in SIDES}
        self.last_fire = -1e9

    def ear(self, hmd: np.ndarray, side: str) -> np.ndarray:
        sign = -1.0 if side == "left" else 1.0
        return hmd[:3, 3] + sign * self.EAR_SIDE_M * hmd[:3, 0] - self.EAR_DROP_M * hmd[:3, 1]

    def near(self, s: Sample, side: str) -> bool:
        h = s.hands.get(side)
        if s.hmd is None or h is None or h.pose is None or not h.curl:
            return False
        if any(c >= self.OPEN_CURL for c in h.curl[1:5]):
            return False
        return float(np.linalg.norm(h.pose[:3, 3] - self.ear(s.hmd, side))) <= self.RADIUS_M

    def feed(self, s: Sample) -> bool:
        fired = False
        for side in SIDES:
            if not self.near(s, side):
                self.since[side] = None
                continue
            if self.since[side] is None:
                self.since[side] = s.t
            elif (s.t - self.since[side] >= self.HOLD_S
                  and s.t - self.last_fire >= self.COOLDOWN_S):
                self.last_fire = s.t
                self.since[side] = None
                fired = True
        return fired


DETECTORS = (TripleClick, Headphones)


def replay(path: Path) -> dict[str, list[float]]:
    """fire times (seconds from the start) per detector for one recording."""
    detectors = [d() for d in DETECTORS]
    fires: dict[str, list[float]] = {d.name: [] for d in detectors}
    t0 = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if "t" not in d:
                continue    # header line
            s = Sample.from_json(d)
            if t0 is None:
                t0 = s.t
            for det in detectors:
                if det.feed(s):
                    fires[det.name].append(round(s.t - t0, 2))
    return fires
