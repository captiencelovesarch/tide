"""The ring: tide's backdrop pulse moved into vr, a grainy glow on the
edge of your view that breathes over the game (design/steamvr.md).

This module only draws. The shape is each eye's real lens outline (the
hidden area mesh steamvr reports, nose cut and all), mapped onto one quad
locked to the headset; a side-by-side texture gives each eye its own
half. Drawn once per connection in white, in a few grain variants; the
vr thread then tints, fades and cross-fades them, never re-uploading.

QImage painting only, so this runs fine off the gui thread (ctx.jobs).
"""
from __future__ import annotations

import math
import random

DISTANCE_M = 2.0
VARIANTS = 4
HALF_W = 960
MAX_TEX_H = 1080


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


def _center_uv(eye: dict) -> tuple[float, float]:
    left, right, top, bottom = eye["proj"]
    hi, lo = max(top, bottom), min(top, bottom)
    return -left / (right - left), hi / (hi - lo)


def draw(eyes: dict, distance: float = DISTANCE_M, variants: int = VARIANTS):
    """(list of white side-by-side QImages, quad width in metres)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF

    quad = {name: [_to_quad(e, u, v, distance) for u, v in (e["uv"] or ellipse_uv())]
            for name, e in eyes.items()}
    centers = {name: _to_quad(e, *_center_uv(e), distance) for name, e in eyes.items()}
    qw = 2 * max(abs(x) for pts in quad.values() for x, _ in pts) * 1.04
    qh = 2 * max(abs(y) for pts in quad.values() for _, y in pts) * 1.04

    half_w = HALF_W
    tex_h = half_w * qh / qw
    if tex_h > MAX_TEX_H:
        half_w = int(half_w * MAX_TEX_H / tex_h)
        tex_h = MAX_TEX_H
    tex_h = int(round(tex_h))

    def px(x: float, y: float, offset: int) -> QPointF:
        return QPointF(offset + (x + qw / 2) / qw * half_w, (qh / 2 - y) / qh * tex_h)

    images = []
    for variant in range(variants):
        rng = random.Random(variant)
        img = QImage(half_w * 2, tex_h, QImage.Format_RGBA8888)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        for slot, name in enumerate(("left", "right")):
            off = slot * half_w
            pts = [px(x, y, off) for x, y in quad[name]]
            cx = px(*centers[name], off)
            path = QPainterPath()
            path.addPolygon(QPolygonF(pts))
            path.closeSubpath()
            p.save()
            p.setClipPath(path)
            # the glow: wide soft strokes, clipped to the inside of the edge
            for width, alpha in ((90, 18), (60, 28), (36, 45), (18, 70)):
                p.setPen(QPen(QColor(255, 255, 255, alpha), width))
                p.drawPath(path)
            # the grain: dots near the edge, thinning toward the middle and
            # never past ~16% of the way in
            p.setPen(Qt.NoPen)
            for _ in range(5000):
                j = rng.randrange(len(pts))
                a, b = pts[j], pts[(j + 1) % len(pts)]
                t = rng.random()
                ex, ey = a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t
                depth = rng.random() ** 2.2 * 0.16
                gx, gy = ex + (cx.x() - ex) * depth, ey + (cx.y() - ey) * depth
                p.setBrush(QColor(255, 255, 255, rng.randrange(120, 256)))
                r = rng.choice((1.0, 1.0, 1.5, 2.0))
                p.drawEllipse(QPointF(gx, gy), r, r)
            p.restore()
        p.end()
        images.append(img)
    return images, qw


MODES = ("off", "kick", "breathing")
STRENGTH = {"subtle": 0.45, "normal": 0.7, "loud": 1.0}


def level(mode: str, pulse: float, playing: bool) -> float:
    """how bright the ring is for a pulse level (0..1), before strength.
    kick: dark until a hit. breathing: always faintly there."""
    if mode == "off" or not playing:
        return 0.0
    pulse = max(0.0, min(1.0, pulse))
    if mode == "kick":
        return pulse
    return 0.25 + 0.75 * pulse
