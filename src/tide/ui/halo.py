"""The halo backdrop: a glow in the cover's colour around the OUTSIDE of
the window.

A window can't paint past its own surface, so the window grows the
surface instead: with halo on, the host reserves a transparent margin
(``MARGIN_PX``) around the app and this widget paints the glow into it.
GTK draws client-side shadows the same way. Three things make the
margin behave like it isn't there:

  * the window goes ARGB (``WA_TranslucentBackground``), so the margin is
    see-through wherever the glow fades out;
  * the input region (``QWindow.setMask``) is the app's own rect, so a
    click on the glow lands on whatever is behind it. On Wayland the mask
    is input only and the glow still renders;
  * edge resize (``titlebar._edges_at``) measures from the app's rect.

Maximized and fullscreen windows have no room around them, so the margin
and the glow go away there. Inside the app the halo style is a flat card
(``central_bg``), like the reference: all the colour lives outside.

The glow is a 9-slice: one small tile per (colour, margin, radius) holds
the four corners, and the edges are its middle row and column stretched,
which is exact because the glow is uniform along an edge. Bass both
brightens the glow and throws it further out: at rest it hugs the app,
a hit reaches the full margin. The reach is quantised to a few steps,
so a song's worth of hits reuses a handful of cached tiles.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from .. import theming
from . import motion as motion_module, scale

STYLE = "halo"
MARGIN_PX = 52            # room around the app the glow fades across
_REST = 0.35              # glow strength between hits
_BASS = 0.65              # what a full hit adds on top
_REACH_REST = 0.65        # how far out the glow reaches at rest, of margin
_REACH_STEP = 3           # px steps the reach moves in (tile cache key)
_FADE_MS = 1400           # the colour crossfade on a new song (= central_bg)


def _tile(color: QColor, margin: int, radius: int) -> QImage:
    """The four corners of the glow around a rounded rect, with a 1px
    middle row/column that stretches into the edges."""
    k = max(1, radius)
    side = 2 * (margin + k) + 1
    c = (side - 1) / 2.0
    half = k + 0.5                       # the card's half-size in the tile
    xs = np.abs(np.arange(side, dtype=np.float32) - c)
    qx = xs - (half - radius)
    qy = qx[:, None]
    qx = qx[None, :]
    out = np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2) \
        + np.minimum(np.maximum(qx, qy), 0) - radius
    d = np.maximum(out, 0.0)             # distance out from the card edge
    sigma = max(1.0, margin * 0.42)
    a = np.exp(-(d / sigma) ** 2)
    a[out < 0] = 0.0                     # nothing under the card itself
    buf = np.empty((side, side, 4), np.uint8)
    buf[..., 0] = (color.blue() * a).astype(np.uint8)       # BGRA, premul
    buf[..., 1] = (color.green() * a).astype(np.uint8)
    buf[..., 2] = (color.red() * a).astype(np.uint8)
    buf[..., 3] = (255 * a).astype(np.uint8)
    return QImage(np.ascontiguousarray(buf).data, side, side, side * 4,
                  QImage.Format_ARGB32_Premultiplied).copy()


class HaloGlow(QWidget):
    """Paints the glow around ``card()`` (a QRect in host coords).

    ``apply_margin(px)`` is how the host makes room: the main window sets
    its contents margins, the mini folds it into its gutter. The host
    turns it on with ``set_active``; the window state decides the rest.
    """

    def __init__(self, host: QWidget, central_bg, card, apply_margin) -> None:
        super().__init__(host)
        self._host = host
        self._bg = central_bg
        self._card = card
        self._apply_margin = apply_margin
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._active = False
        self._margin = 0
        self._pulse = 0.0
        self._color = self._accent(theming.manager().current_effective())
        self._from = QColor(self._color)
        self._blend = 1.0
        self._fade = None
        self._tiles: dict[tuple, QImage] = {}
        host.installEventFilter(self)
        theming.manager().theme_changed.connect(self._on_theme)
        # Bass rides central_bg's own envelope, so the pulse setting and
        # the backdrop's smoothing apply to the glow for free.
        central_bg.on_frame = self._on_bg_frame
        self.hide()

    # ------------------------------------------------------------ state

    def set_active(self, on: bool) -> None:
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        self.refresh()

    def active(self) -> bool:
        return self._active

    def margin(self) -> int:
        return self._margin

    def refresh(self) -> None:
        """Re-derive the margin from active + window state, then place."""
        h = self._host
        roomy = not (h.isMaximized() or h.isFullScreen())
        want = scale.px(MARGIN_PX) if self._active and roomy else 0
        if want != self._margin:
            self._margin = want
            self._apply_margin(want)
        self._place()

    def _place(self) -> None:
        h = self._host
        if self._margin <= 0:
            self.hide()
            self._set_input(None)
            return
        self.setGeometry(h.rect())
        self.lower()                     # under the app, never over it
        self.show()
        self._set_input(self._card())
        self.update()

    def _set_input(self, rect: QRect | None) -> None:
        handle = self._host.windowHandle()
        if handle is None:
            return
        handle.setMask(QRegion(rect) if rect is not None else QRegion())

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        if obj is self._host:
            t = ev.type()
            if t == QEvent.WindowStateChange:
                self.refresh()
            elif t in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
                if self._margin > 0:
                    self._place()
        return False

    # ------------------------------------------------------------ colour

    @staticmethod
    def _accent(theme) -> QColor:
        c = QColor(theme.token("accent", "#d4b95e")) if theme is not None \
            else QColor("#d4b95e")
        return c if c.isValid() else QColor("#d4b95e")

    def _on_theme(self, theme) -> None:
        new = self._accent(theme)
        if new.rgb() == self._color.rgb():
            return
        self._from = self._shown()
        self._color = new
        if (not self.isVisible()
                or motion_module.intensity() == motion_module.Intensity.OFF):
            self._blend = 1.0
            self.update()
            return
        self._blend = 0.0
        self._fade = motion_module.value_lerp(
            0.0, 1.0, on_update=self._on_fade, dur=_FADE_MS,
            owner=self, kind="halo_colour")

    def _on_fade(self, v) -> None:
        self._blend = float(v)
        self.update()

    def _shown(self) -> QColor:
        t = self._blend
        a, b = self._from, self._color
        return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t,
                               a.greenF() + (b.greenF() - a.greenF()) * t,
                               a.blueF() + (b.blueF() - a.blueF()) * t)

    def _on_bg_frame(self, pulse: float) -> None:
        if self._margin > 0 and abs(pulse - self._pulse) > 0.01:
            self._pulse = pulse
            self.update()

    # ------------------------------------------------------------ paint

    def paintEvent(self, _ev) -> None:
        m = self._margin
        if m <= 0:
            return
        card = self._card()
        radius = int(getattr(self._bg, "_radius", 0) or 0)
        col = self._shown()
        pulse = max(0.0, min(1.0, self._pulse))
        reach = m * (_REACH_REST + (1.0 - _REACH_REST) * pulse)
        reach = max(_REACH_STEP, int(reach) // _REACH_STEP * _REACH_STEP)
        # Quantised so a crossfade reuses a handful of tiles, not one
        # per frame.
        key = (col.red() >> 2, col.green() >> 2, col.blue() >> 2, reach,
               radius)
        tile = self._tiles.get(key)
        if tile is None:
            if len(self._tiles) > 96:
                self._tiles.clear()
            tile = self._tiles[key] = _tile(col, reach, radius)
        m = reach
        k = max(1, radius)
        e = m + k                         # corner tile size
        mid = e                           # the stretchable row/column
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setOpacity(min(1.0, _REST + _BASS * pulse))
        x0, y0 = card.left() - m, card.top() - m
        x1, y1 = card.right() + 1 + m, card.bottom() + 1 + m
        inner_w = max(0, (x1 - x0) - 2 * e)
        inner_h = max(0, (y1 - y0) - 2 * e)

        def put(tx, ty, tw, th, sx, sy, sw, sh):
            if tw > 0 and th > 0:
                p.drawImage(QRectF(tx, ty, tw, th), tile,
                            QRectF(sx, sy, sw, sh))

        s = tile.width()
        # corners
        put(x0, y0, e, e, 0, 0, e, e)
        put(x1 - e, y0, e, e, s - e, 0, e, e)
        put(x0, y1 - e, e, e, 0, s - e, e, e)
        put(x1 - e, y1 - e, e, e, s - e, s - e, e, e)
        # edges: the 1px middle stretched
        put(x0 + e, y0, inner_w, e, mid, 0, 1, e)
        put(x0 + e, y1 - e, inner_w, e, mid, s - e, 1, e)
        put(x0, y0 + e, e, inner_h, 0, mid, e, 1)
        put(x1 - e, y0 + e, e, inner_h, s - e, mid, e, 1)
        p.end()
