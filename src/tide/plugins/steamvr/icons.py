"""Icons tide's icon sets don't carry (search, volume, the now-playing
note, the ring), drawn as plain strokes so they sit next to the set's
outlines in either personality. Everything else comes from
ctx.theme.icon, i.e. the user's icon set."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap

DRAWN = ("note", "search", "volume", "ring")


def drawn(key: str, px: int, color: str, *, square: bool = False) -> QPixmap:
    """``key`` at ``px`` square in ``color``. ``square`` gives brutalist
    flat caps and sharp joins."""
    pix = QPixmap(px, px)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), max(2.0, px * 0.09))
    pen.setCapStyle(Qt.SquareCap if square else Qt.RoundCap)
    pen.setJoinStyle(Qt.MiterJoin if square else Qt.RoundJoin)
    p.setPen(pen)
    s = px / 24.0                                 # draw on a 24 grid
    if key == "note":
        p.drawLine(QPointF(9 * s, 18 * s), QPointF(9 * s, 5 * s))
        p.drawLine(QPointF(9 * s, 5 * s), QPointF(19 * s, 3 * s))
        p.drawLine(QPointF(19 * s, 3 * s), QPointF(19 * s, 16 * s))
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(6.5 * s, 18 * s), 2.6 * s, 2.2 * s)
        p.drawEllipse(QPointF(16.5 * s, 16 * s), 2.6 * s, 2.2 * s)
    elif key == "search":
        p.drawEllipse(QPointF(10 * s, 10 * s), 6 * s, 6 * s)
        p.drawLine(QPointF(14.5 * s, 14.5 * s), QPointF(20.5 * s, 20.5 * s))
    elif key == "volume":
        path = QPainterPath(QPointF(4 * s, 9.5 * s))
        for x, y in ((8 * s, 9.5 * s), (13 * s, 5 * s), (13 * s, 19 * s), (8 * s, 14.5 * s),
                     (4 * s, 14.5 * s)):
            path.lineTo(x, y)
        path.closeSubpath()
        p.drawPath(path)
        p.drawArc(QRectF(11 * s, 7 * s, 8 * s, 10 * s), -50 * 16, 100 * 16)
        p.drawArc(QRectF(11 * s, 3.5 * s, 12 * s, 17 * s), -50 * 16, 100 * 16)
    elif key == "ring":
        p.drawEllipse(QPointF(12 * s, 12 * s), 8.5 * s, 8.5 * s)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for x, y in ((12, 1.8), (21.6, 9), (19, 20.5), (5, 20.5), (2.4, 9)):
            p.drawEllipse(QPointF(x * s, y * s), 1.1 * s, 1.1 * s)
    p.end()
    return pix
