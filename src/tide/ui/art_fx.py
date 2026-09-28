"""Album-art transitions: how a cover becomes the next one.

Six styles, picked in settings → appearance → motion & sound:

    flip    — the cover turns over like a card and the next one is on
              its back (springy lands a few degrees past and settles)
    slide   — the next cover pushes the last one out to the left
    pop     — the old cover sinks away, the new one pops up in its place
    blocks  — the cover breaks into squares and rebuilds as the next one
    fade    — a plain crossfade (the 2.x behaviour)
    off     — the cover just changes

Motion "off" makes every style instant, same contract as text_fx.
Every AlbumArt (the strip, the mini, fullscreen, and the circle and
polaroid variants) goes through ``play``; hosts never touch the styles.

Frames are composed into a pixmap the size of the new cover and handed to
the label's setPixmap, so the label keeps drawing its own frame and
background. The clock is ``motion.tween``: a song change also restyles
the whole app, and a wall-clock animation lost the first part of the
move to that stall.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap, QTransform

from . import motion


STYLES: tuple[str, ...] = ("flip", "slide", "pop", "blocks", "fade", "off")
BLURBS: dict[str, str] = {
    "flip": "flip · the cover turns over",
    "slide": "slide · the next cover pushes the last one out",
    "pop": "pop · the new cover pops up in place",
    "blocks": "blocks · breaks into squares and rebuilds",
    "fade": "fade · a plain crossfade",
    "off": "off · the cover just changes",
}
DEFAULT_STYLE = "flip"

# Overshoot for the flip under the springy profile. Qt's default (1.70)
# swings about 18 degrees past; this lands around 11.
_FLIP_OVERSHOOT = 1.3
# Camera distance as a multiple of the cover's side. Closer reads as more
# 3D but the near edge grows past the tile and gets cut off.
_FLIP_CAMERA = 3.5
# How far the card lifts away (shrinks) at edge-on, which also keeps the
# perspective-grown near edge inside the tile.
_FLIP_LIFT = 0.16
# The pixelation ladder, in cells per side (0 = the cover as it is).
_BLOCK_LADDER = (0, 48, 24, 12, 6)

_style: str = DEFAULT_STYLE


def set_style(name: str) -> None:
    """Pick a style. Unknown names fall back to the default so a stale
    settings value can't wedge the art."""
    global _style
    name = str(name or "").strip().lower()
    _style = name if name in STYLES else DEFAULT_STYLE


def style() -> str:
    return _style


def effective_style() -> str:
    """The style after the motion switch: OFF intensity means instant."""
    if motion.intensity() == motion.Intensity.OFF:
        return "off"
    return _style


def choices() -> tuple[tuple[str, str], ...]:
    return tuple((s, BLURBS[s]) for s in STYLES)


def duration(name: str) -> int:
    if name in ("flip", "blocks"):
        return motion.dur("long")
    return motion.dur("med")


# ---------------------------------------------------------------- frames

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _flip_curve() -> QEasingCurve:
    curve = motion.ease("spring")
    if curve.type() == QEasingCurve.OutBack:
        curve.setOvershoot(_FLIP_OVERSHOOT)
    return curve


def _canvas(new: QPixmap) -> tuple[QPixmap, QPainter]:
    out = QPixmap(new.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.Antialiasing, True)
    return out, p


def _draw_scaled(p: QPainter, pix: QPixmap, w: int, h: int,
                 k: float, opacity: float) -> None:
    if opacity <= 0.0 or k <= 0.0:
        return
    p.save()
    p.setOpacity(_clamp01(opacity))
    p.translate(w / 2.0, h / 2.0)
    p.scale(k, k)
    p.drawPixmap(QPointF(-pix.width() / 2.0, -pix.height() / 2.0), pix)
    p.restore()


def _frame_flip(old: QPixmap, new: QPixmap, prog: float) -> QPixmap:
    # Squaring the progress first gives the turn a start from rest: a bare
    # OutBack leaves at full speed and the old cover was gone in the first
    # tenth of the flip, the rest spent wobbling.
    angle = 180.0 * _flip_curve().valueForProgress(prog * prog)
    # Front face until edge-on, then the back face. Past 180 (the
    # overshoot) the back face tips the other way and settles.
    face, a = (old, angle) if angle < 90.0 else (new, angle - 180.0)
    w, h = new.width(), new.height()
    out, p = _canvas(new)
    try:
        edge = abs(math.sin(math.radians(a)))
        lift = 1.0 - _FLIP_LIFT * edge
        tr = QTransform()
        tr.translate(w / 2.0, h / 2.0)
        tr.rotate(a, Qt.YAxis, max(w, h) * _FLIP_CAMERA)
        tr.scale(lift, lift)
        tr.translate(-face.width() / 2.0, -face.height() / 2.0)
        p.setTransform(tr)
        p.drawPixmap(0, 0, face)
        if edge > 0.01:
            # The face darkens as it turns away from the light. SourceAtop
            # keeps the shade inside the cover's own shape (rounded
            # corners, the circle variant).
            p.setCompositionMode(QPainter.CompositionMode_SourceAtop)
            p.fillRect(QRectF(face.rect()), QColor(0, 0, 0, int(150 * edge)))
    finally:
        p.end()
    return out


def _frame_slide(old: QPixmap, new: QPixmap, prog: float) -> QPixmap:
    t = motion.ease("out").valueForProgress(prog)
    w = new.width()
    gap = w * 0.06
    shift = (w + gap) * t
    out, p = _canvas(new)
    try:
        p.drawPixmap(QPointF(-shift, 0.0), old)
        p.drawPixmap(QPointF(w + gap - shift, 0.0), new)
    finally:
        p.end()
    return out


def _frame_pop(old: QPixmap, new: QPixmap, prog: float) -> QPixmap:
    w, h = new.width(), new.height()
    out, p = _canvas(new)
    try:
        # The old cover sinks and fades over the first half...
        q_old = motion.ease("out").valueForProgress(_clamp01(prog / 0.45))
        _draw_scaled(p, old, w, h, 1.0 - 0.18 * q_old, 1.0 - q_old)
        # ...the new one starts small a beat in and springs to size
        # (a few percent past it under springy; opacity never overshoots).
        q_new = _clamp01((prog - 0.15) / 0.85)
        grow = motion.ease("spring").valueForProgress(q_new)
        _draw_scaled(p, new, w, h, 0.72 + 0.28 * grow,
                     motion.ease("out").valueForProgress(_clamp01(q_new * 2.2)))
    finally:
        p.end()
    return out


def _pixelate(pix: QPixmap, cells: int) -> QPixmap:
    if cells <= 0:
        return pix
    w, h = pix.width(), pix.height()
    cw = max(1, min(w, cells))
    ch = max(1, min(h, round(cells * h / max(1, w))))
    # Smooth down so each block is its patch's average colour, then
    # nearest-neighbour back up so the blocks stay hard-edged.
    small = pix.scaled(cw, ch, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.FastTransformation)


def _frame_blocks(old: QPixmap, new: QPixmap, prog: float) -> QPixmap:
    # Stepped on purpose: coarser rungs down on the old cover, the swap at
    # the coarsest, finer rungs back up on the new one.
    rungs = len(_BLOCK_LADDER)
    step = min(2 * rungs - 1, int(_clamp01(prog) * 2 * rungs))
    if step < rungs:
        return _pixelate(old, _BLOCK_LADDER[step])
    return _pixelate(new, _BLOCK_LADDER[2 * rungs - 1 - step])


def _frame_fade(old: QPixmap, new: QPixmap, prog: float) -> QPixmap:
    t = motion.ease("out").valueForProgress(prog)
    out, p = _canvas(new)
    try:
        p.setOpacity(1.0 - t)
        p.drawPixmap(0, 0, old)
        p.setOpacity(t)
        p.drawPixmap(0, 0, new)
    finally:
        p.end()
    return out


_FRAMES: dict[str, Callable[[QPixmap, QPixmap, float], QPixmap]] = {
    "flip": _frame_flip,
    "slide": _frame_slide,
    "pop": _frame_pop,
    "blocks": _frame_blocks,
    "fade": _frame_fade,
}


def frame(name: str, old: QPixmap, new: QPixmap, prog: float) -> QPixmap:
    """One frame of ``name`` at linear progress ``prog`` (0..1). Each
    style applies its own easing, so the profile decides the feel."""
    fn = _FRAMES.get(name)
    if fn is None or prog >= 1.0:
        return new
    return fn(old, new, _clamp01(prog))


# ---------------------------------------------------------------- driver

_KIND = "art"


def running(target) -> bool:
    table = getattr(target, "_motion_anims", None)
    return bool(table) and table.get(_KIND) is not None


def cancel(target) -> None:
    """Drop an in-flight transition without landing it (the art went
    away, or the host is about to paint something else)."""
    motion._cancel_prior(target, _KIND)
    target._art_fx_to = None


def play(target, old: Optional[QPixmap], new: QPixmap, *,
         on_done: Optional[Callable[[], None]] = None) -> None:
    """Move ``target`` (a QLabel showing a cover) from ``old`` to ``new``
    in the current style. Snaps when motion is off, the style is off, or
    there is nothing on screen to move away from (first load, the empty
    state). A second call mid-flight takes over: fade and blocks start
    from the frame on screen, the moving styles from the cover the first
    call was heading to (a half-turned card is a bad place to start a
    flip)."""
    name = effective_style()
    in_flight = getattr(target, "_art_fx_to", None) if running(target) else None
    if name not in ("fade", "blocks") and in_flight is not None:
        old = in_flight

    def _finish() -> None:
        target._art_fx_to = None
        if on_done is not None:
            on_done()
        else:
            target.setPixmap(new)

    if (name == "off" or old is None or old.isNull() or new.isNull()):
        cancel(target)
        _finish()
        return
    if old.size() != new.size():
        old = old.scaled(new.size(), Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation)
    target._art_fx_to = new

    def _update(prog: float, old=old, new=new, name=name) -> None:
        target.setPixmap(frame(name, old, new, prog))

    motion.tween(
        0.0, 1.0,
        on_update=_update,
        dur=duration(name),
        easing=QEasingCurve(QEasingCurve.Linear),
        on_done=_finish,
        owner=target,
        kind=_KIND,
    )
