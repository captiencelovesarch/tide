"""What the vr thread lays over the panel's frame: a window where each of
your controllers is, and a cursor where a controller tip is about to touch.

steamvr draws overlays on top of the game, and the game draws your
controllers, so a hand in front of the panel vanished behind it
(2026-10-19: "i can't see my controllers in front of the menu"). overlays
have no depth, so the panel thins out wherever a controller sits between
it and your eyes. it doesn't go fully clear: you still see what you're
about to press.

Drawn on the vr thread from the last frame the gui sent, so the window
keeps up with your hand without a round trip. numpy for the mask (at
1/GRID resolution, smoothed up), a QPainter for the cursor.
"""
from __future__ import annotations

import numpy as np

from . import geometry as geo

GRID = 8
FLOOR = 0.22               # how much panel is left over a controller
FEATHER_M = 0.02
CURSOR_M = 0.10            # a tip this close shows the cursor
CURSOR_FAR_PX = 26
CURSOR_NEAR_PX = 9


def holes(panel: np.ndarray, eye: np.ndarray, controllers: dict,
          width_m: float, px: tuple[int, int]) -> list[tuple]:
    """each controller in front of the panel as a capsule on it, in
    pixels: (x0, y0, r0, x1, y1, r1), head ring to grip."""
    height_m = width_m * px[1] / px[0]
    per_m = px[0] / width_m
    out = []
    for side, ctrl in controllers.items():
        if ctrl is None:
            continue
        ends = [geo.through_panel(panel, eye, geo.on_controller(ctrl, side, p), geo.BODY_RADIUS_M)
                for p in (geo.BODY_HEAD, geo.BODY_BASE)]
        if any(e is None for e in ends) or not any(e[3] for e in ends):
            continue
        cap = []
        for x, y, r, _front in ends:
            cap += [(x / width_m + 0.5) * px[0], (0.5 - y / height_m) * px[1], r * per_m]
        x0, y0, r0, x1, y1, r1 = cap
        reach = max(r0, r1) + FEATHER_M * per_m
        if (max(x0, x1) < -reach or min(x0, x1) > px[0] + reach
                or max(y0, y1) < -reach or min(y0, y1) > px[1] + reach):
            continue
        out.append(tuple(cap))
    return out


def cursor(tips: dict, width_m: float, px: tuple[int, int],
           pressed: str | None) -> tuple | None:
    """(x, y, closeness 0..1, pressed) in pixels for the tip nearest the
    panel, from ``tips``: each controller tip in the panel's frame."""
    height_m = width_m * px[1] / px[0]
    best = None
    for side, local in tips.items():
        x, y, z = (float(c) for c in local)
        if abs(x) > width_m / 2 or abs(y) > height_m / 2:
            continue
        if side != pressed and not (-0.01 < z < CURSOR_M):
            continue
        rank = -1.0 if side == pressed else z
        if best is None or rank < best[0]:
            close = 1.0 - min(1.0, max(0.0, z / CURSOR_M))
            best = (rank, ((x / width_m + 0.5) * px[0], (0.5 - y / height_m) * px[1],
                           close, side == pressed))
    return None if best is None else best[1]


def mask(caps: list[tuple], width_m: float, px: tuple[int, int]) -> np.ndarray:
    """how much panel to keep, 0..1, at 1/GRID resolution."""
    w, h = px[0] // GRID, px[1] // GRID
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    xs = (xs + 0.5) * GRID
    ys = (ys + 0.5) * GRID
    feather = FEATHER_M * px[0] / width_m
    keep = np.ones((h, w), dtype=np.float32)
    for x0, y0, r0, x1, y1, r1 in caps:
        dx, dy = x1 - x0, y1 - y0
        length2 = dx * dx + dy * dy or 1e-6
        t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / length2, 0.0, 1.0)
        d = np.hypot(xs - (x0 + t * dx), ys - (y0 + t * dy)) - (r0 + t * (r1 - r0))
        keep = np.minimum(keep, np.clip(d / feather, 0.0, 1.0))
    return FLOOR + (1.0 - FLOOR) * keep


def compose(base, caps: list[tuple], cur: tuple | None, width_m: float, color: str):
    """``base`` (an rgba8888 qimage) with the windows and the cursor."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen

    img = (base.copy() if base.format() == QImage.Format_RGBA8888
           else base.convertToFormat(QImage.Format_RGBA8888))
    w, h = img.width(), img.height()
    if caps:
        small = np.ascontiguousarray((mask(caps, width_m, (w, h)) * 255).astype(np.uint8))
        m = QImage(small.data, small.shape[1], small.shape[0], small.shape[1],
                   QImage.Format_Grayscale8).scaled(w, h, Qt.IgnoreAspectRatio,
                                                     Qt.SmoothTransformation)
        keep = np.frombuffer(m.constBits(), np.uint8).reshape(h, m.bytesPerLine())[:, :w]
        rgba = np.frombuffer(img.bits(), np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4]
        alpha = rgba[:, 3::4]
        alpha[...] = (alpha.astype(np.uint16) * keep // 255).astype(np.uint8)
    if cur is not None:
        x, y, close, pressed = cur
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(color)
        r = CURSOR_FAR_PX + (CURSOR_NEAR_PX - CURSOR_FAR_PX) * close
        fade = int(255 * min(1.0, 0.35 + close))
        shade = QColor(0, 0, 0, fade // 2)
        c.setAlpha(fade)
        # a dark halo under the ring reads on light and dark panels alike
        p.setPen(QPen(shade, 7))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(x, y), r, r)
        p.setPen(QPen(c, 3))
        if pressed:
            p.setBrush(c)
        p.drawEllipse(QPointF(x, y), r, r)
        p.end()
    return img


def same(a: tuple | list | None, b: tuple | list | None, px: float = 1.5) -> bool:
    """two cursors or capsule lists within ``px`` of each other: skip the
    redraw."""
    if a is None or b is None:
        return a is b
    fa = np.asarray(a if not isinstance(a, list) else [v for cap in a for v in cap], float)
    fb = np.asarray(b if not isinstance(b, list) else [v for cap in b for v in cap], float)
    return fa.shape == fb.shape and bool(np.all(np.abs(fa - fb) <= px))
