"""The ring: tide's bass pulse on the edge of your view, over the game.

Two layers, both following each eye's real lens outline (steamvr's hidden
area mesh, so the index's nose cut too), on one quad locked to the
headset with a side-by-side texture (each eye gets its own half):

* glow: a soft light that fades from near the edge of your vision toward
  the middle, in two colours from the cover blending round the rim. it
  breathes with the bass.
* rim: a thin bright band. on a kick it flashes and ripples a little way
  inward (the vr thread shrinks it as it fades).

Both sit on the lens outline pulled in by INSET. right on the outline
they were at the very edge of what the lens shows, mostly out of sight
(2026-10-19); a soft halo still runs out to the real edge.

Drawn per song (the colours change), at half resolution with numpy (a
glow has no detail to lose) and upscaled smoothly; a hair of noise keeps
8-bit gradients from banding. The vr thread only fades and scales the
two layers, it never redraws them.

QImage painting and numpy only, so this runs fine off the gui thread.
"""
from __future__ import annotations

import math

import numpy as np

DISTANCE_M = 2.0
HALF_W = 960
MAX_TEX_H = 1080
SCALE = 3                 # drawn at 1/SCALE resolution, upscaled
# how far in the glow reaches, as a share of the eye's view width. the
# middle of your view stays the game's
GLOW_REACH = {"modern": 0.05, "brutalist": 0.032}
RIM_REACH = 0.009
# the outline the ring follows, scaled toward the middle of each eye's
# view. 1.0 is the lens edge
INSET = 0.86
HALO = 0.45               # how far the glow spills outward, as a share of its reach
SMOOTHING = 2             # rounds of corner cutting on the outline

MODES = ("off", "kick", "breathing")
STRENGTH = {"subtle": 0.45, "normal": 0.7, "loud": 1.0}


def ellipse_uv(n: int = 96) -> list[tuple[float, float]]:
    """for a headset that reports no outline"""
    return [(0.5 + 0.47 * math.cos(2 * math.pi * i / n), 0.5 + 0.47 * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def _to_quad(eye: dict, u: float, v: float, distance: float) -> tuple[float, float]:
    """an eye's viewport uv → metres on a quad ``distance`` out, centred
    on the head. viewport v grows downward; the quad's y goes up."""
    left, right, top, bottom = eye["proj"]
    hi, lo = max(top, bottom), min(top, bottom)
    tan_x = left + u * (right - left)
    tan_up = hi - v * (hi - lo)
    return eye["eye_x"] + tan_x * distance, tan_up * distance


def smooth(points: list, rounds: int = SMOOTHING) -> list:
    """chaikin corner cutting: steamvr's outline is ~27 points, and a
    bright rim traced every facet. each round doubles the points."""
    for _ in range(rounds):
        out = []
        n = len(points)
        for i in range(n):
            (x0, y0), (x1, y1) = points[i], points[(i + 1) % n]
            out.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            out.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        points = out
    return points


def inset(points: np.ndarray, center: tuple, k: float = INSET) -> np.ndarray:
    """an outline pulled toward ``center``."""
    c = np.asarray(center, dtype=np.float32)
    return c + (np.asarray(points, dtype=np.float32) - c) * k


def _inside(px: np.ndarray, py: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """even-odd point in polygon, vectorised over a pixel grid."""
    inside = np.zeros(px.shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        crosses = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (px < xs)
    return inside


def _edge_distance(px: np.ndarray, py: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """distance from each pixel to the nearest outline segment."""
    best = np.full(px.shape, np.inf)
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy or 1e-9
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / length2, 0.0, 1.0)
        best = np.minimum(best, np.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def _layer(distances: list, insides: list, visibles: list, angles: list, reach_px: float,
           colors: tuple, rng: np.random.Generator, hard: bool) -> np.ndarray:
    """rgba for both eye halves side by side. ``insides``: inside the
    pulled-in outline; ``visibles``: inside the lens at all."""
    halves = []
    c1 = np.array(colors[0], dtype=np.float32)
    c2 = np.array(colors[1], dtype=np.float32)
    for d, inside, visible, ang in zip(distances, insides, visibles, angles):
        if hard:
            a = np.clip(1.0 - d / reach_px, 0.0, 1.0) ** 0.6
            out = np.clip(1.0 - d / (reach_px * HALO), 0.0, 1.0)
        else:
            a = np.exp(-(d / reach_px) ** 1.4)
            out = np.exp(-(d / (reach_px * HALO)) ** 2)
        a = np.where(inside, a, np.where(visible, out, 0.0)).astype(np.float32)
        # two colours round the rim: c1 at the top, c2 at the bottom
        mix = (0.5 - 0.5 * np.cos(ang))[..., None].astype(np.float32)
        rgb = c1 * (1.0 - mix) + c2 * mix
        a = np.clip(a + rng.uniform(-1.5 / 255, 1.5 / 255, a.shape).astype(np.float32), 0, 1)
        halves.append(np.dstack([rgb, a[..., None]]))
    return np.hstack(halves)


def draw(eyes: dict, colors: tuple = ((1.0, 0.83, 0.0), (1.0, 0.45, 0.2)),
         style: str = "modern", distance: float = DISTANCE_M):
    """([glow, rim] as QImages, quad width in metres).

    ``colors``: two rgb tuples 0..1 (top of the rim, bottom). ``style``:
    modern (soft, two colours) or brutalist (a crisper band)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    quad = {name: smooth([_to_quad(e, u, v, distance) for u, v in (e["uv"] or ellipse_uv())])
            for name, e in eyes.items()}
    centers = {}
    for name, e in eyes.items():
        left, right, top, bottom = e["proj"]
        hi, lo = max(top, bottom), min(top, bottom)
        centers[name] = _to_quad(e, -left / (right - left), hi / (hi - lo), distance)
    qw = 2 * max(abs(x) for pts in quad.values() for x, _ in pts) * 1.04
    qh = 2 * max(abs(y) for pts in quad.values() for _, y in pts) * 1.04

    half_w = HALF_W
    tex_h = half_w * qh / qw
    if tex_h > MAX_TEX_H:
        half_w = int(half_w * MAX_TEX_H / tex_h)
        tex_h = MAX_TEX_H
    tex_h = int(round(tex_h))
    sw, sh = half_w // SCALE, tex_h // SCALE

    ys, xs = np.mgrid[0:sh, 0:sw].astype(np.float32) + 0.5
    distances, insides, visibles, angles = [], [], [], []
    for name in ("left", "right"):
        edge = np.array([((x + qw / 2) / qw * sw, (qh / 2 - y) / qh * sh) for x, y in quad[name]],
                        dtype=np.float32)
        cx, cy = ((centers[name][0] + qw / 2) / qw * sw, (qh / 2 - centers[name][1]) / qh * sh)
        poly = inset(edge, (cx, cy))
        insides.append(_inside(xs, ys, poly))
        visibles.append(_inside(xs, ys, edge))
        distances.append(_edge_distance(xs, ys, poly))
        angles.append(np.arctan2(xs - cx, -(ys - cy)))     # 0 at the top

    rng = np.random.default_rng(7)
    hard = style == "brutalist"
    if hard:
        colors = (colors[0], colors[0])
    glow_reach = GLOW_REACH["brutalist" if hard else "modern"] * sw
    layers = [
        _layer(distances, insides, visibles, angles, glow_reach, colors, rng, hard),
        _layer(distances, insides, visibles, angles, RIM_REACH * sw, ((1, 1, 1), (1, 1, 1)) if hard
               else tuple(tuple(min(1.0, c * 0.4 + 0.6) for c in col) for col in colors), rng, hard),
    ]
    images = []
    for rgba in layers:
        data = np.ascontiguousarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8))
        small = QImage(data.data, sw * 2, sh, sw * 2 * 4, QImage.Format_RGBA8888).copy()
        images.append(small.scaled(half_w * 2, tex_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    return images, qw


# the panel's own glow: a soft light round its edge, beating with the ring
PANEL_MARGIN = 0.12            # glow room on each side, as a share of the panel's width
PANEL_REACH = 0.045


def panel_glow(size: tuple[int, int], radius: int, colors: tuple, style: str = "modern"):
    """(the glow round a ``size`` panel with ``radius`` corners, as a
    QImage, and how much wider than the panel it is). nothing inside the
    panel's shape; the vr thread fades it with the beat."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    w, h = size
    m = int(round(w * PANEL_MARGIN))
    tw, th = w + 2 * m, h + 2 * m
    sw, sh = tw // SCALE, th // SCALE
    ys, xs = np.mgrid[0:sh, 0:sw].astype(np.float32) + 0.5
    xs, ys = xs * SCALE - tw / 2, ys * SCALE - th / 2
    # signed distance to a rounded rectangle, in panel pixels
    r = float(max(0, min(radius, w // 2, h // 2)))
    qx = np.abs(xs) - (w / 2 - r)
    qy = np.abs(ys) - (h / 2 - r)
    d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
    reach = PANEL_REACH * w
    hard = style == "brutalist"
    if hard:
        a = np.clip(1.0 - d / reach, 0.0, 1.0) ** 0.8
    else:
        a = np.exp(-(np.maximum(d, 0) / reach) ** 1.3)
    a = np.where(d > 0, a, 0.0).astype(np.float32)
    c1 = np.array(colors[0], dtype=np.float32)
    c2 = np.array(colors[1 if not hard else 0], dtype=np.float32)
    mix = (0.5 - 0.5 * np.cos(np.arctan2(xs, -ys)))[..., None].astype(np.float32)
    rgb = c1 * (1.0 - mix) + c2 * mix
    rng = np.random.default_rng(11)
    a = np.clip(a + rng.uniform(-1.5 / 255, 1.5 / 255, a.shape).astype(np.float32), 0, 1)
    a = np.where(d > 0, a, 0.0)
    data = np.ascontiguousarray((np.dstack([rgb, a[..., None]]) * 255).astype(np.uint8))
    small = QImage(data.data, sw, sh, sw * 4, QImage.Format_RGBA8888).copy()
    return small.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation), tw / w


def cover_colors(img) -> tuple | None:
    """two colours for the rim from a cover: its most vivid colour and the
    next most vivid one of a different hue. None for a grey cover."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    if img is None or img.isNull():
        return None
    small = img.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    picks = []
    for y in range(small.height()):
        for x in range(small.width()):
            c = QColor(small.pixel(x, y))
            h, s, v, _a = c.getHsvF()
            if s > 0.25 and v > 0.3:
                picks.append((s * v, h, c))
    if not picks:
        return None
    picks.sort(key=lambda p: -p[0])
    first = picks[0]
    second = next((p for p in picks[1:] if min(abs(p[1] - first[1]), 1 - abs(p[1] - first[1])) > 0.08),
                  first)

    def lift(c: QColor) -> tuple:
        # glows want light, saturated colour: push value up, keep the hue
        h, s, v, _a = c.getHsvF()
        out = QColor.fromHsvF(max(0.0, h), min(1.0, s * 1.1), max(0.85, v))
        return (out.redF(), out.greenF(), out.blueF())
    return lift(first[2]), lift(second[2])


def glow_level(mode: str, pulse: float, playing: bool) -> float:
    """how bright the glow is for a pulse level (0..1), before strength.
    kick: barely there between hits. breathing: always softly there."""
    if mode == "off" or not playing:
        return 0.0
    pulse = max(0.0, min(1.0, pulse))
    if mode == "kick":
        return 0.12 + 0.35 * pulse
    return 0.3 + 0.7 * pulse


# old name, kept for the tests and anything poking at it
level = glow_level


class Envelope:
    """the pulse at the headset's rate, shaped like tide's own backdrop
    (central_bg): straight up on a hit, a 30 ms glide down. tide's beat
    clock ticks at 60 Hz from the gui thread; a 144 Hz headset showed
    each step (2026-10-19: "ever so slightly choppy"), and a slower
    release blurred quick beats together."""

    RELEASE_S = 0.030

    def __init__(self) -> None:
        self.level = 0.0
        self._last: float | None = None

    def update(self, now: float, target: float) -> float:
        dt = 0.0 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now
        target = float(target)
        if target >= self.level:
            self.level = target
        else:
            self.level += (target - self.level) * (1.0 - math.exp(-dt / self.RELEASE_S))
        return self.level


class Ripple:
    """the rim on a kick: a flash that travels a little way inward and
    fades.

    a kick is the pulse rising ``RISE`` above its recent low (a trough
    that follows the pulse straight down and only slowly up, so a rise
    spread over a few frames still counts), the way tide's backdrop shows
    one (it used to need the pulse to cross
    0.62 after dipping under 0.4, which missed soft kicks and fast songs
    that never dip, 2026-10-19). ``per_second`` full flashes a second at
    most (photosensitivity: always on); a kick past that still ripples,
    softer."""

    RISE = 0.12
    MIN = 0.22                # quieter than this isn't a kick
    GAP_S = 0.07              # one kick, not two, for a ragged attack
    LIFT_S = 0.12             # how slowly the trough follows the pulse up
    SOFT = 0.45
    FADE_S = 0.38
    TRAVEL = 0.07             # how far inward it ripples, as a share of the quad
    per_second = 3

    def __init__(self) -> None:
        self.started = -1e9
        self.strength = 1.0
        self._trough = 0.0
        self._rising = False          # still this kick's attack
        self._last: float | None = None
        self._starts: list[float] = []

    def update(self, now: float, pulse: float) -> tuple[float, float]:
        """(alpha 0..1, scale 0..1 of the quad) for the rim now."""
        dt = 0.0 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now
        pulse = max(0.0, min(1.0, float(pulse)))
        self._starts = [t for t in self._starts if now - t < 1.0]
        if self._rising and pulse >= self._trough:
            # still this kick's attack, however long it takes to peak: it
            # can't count twice
            self._trough = pulse
        elif now - self.started < self.GAP_S:
            self._rising = False
            self._trough = max(self._trough, pulse)
        elif pulse - self._trough >= self.RISE and pulse >= self.MIN:
            full = len(self._starts) < self.per_second
            if full:
                self._starts.append(now)
            self.started = now
            self.strength = 1.0 if full else self.SOFT
            self._trough = pulse
            self._rising = True
        elif pulse <= self._trough:
            self._rising = False
            self._trough = pulse
        else:
            self._trough += (pulse - self._trough) * (1.0 - math.exp(-dt / self.LIFT_S))
        age = now - self.started
        if age > self.FADE_S * 4:
            return 0.0, 1.0
        alpha = math.exp(-age / self.FADE_S) * self.strength
        scale = 1.0 - self.TRAVEL * (1.0 - math.exp(-age / (self.FADE_S * 0.7)))
        return alpha, scale
