"""Track-change text transitions: how a title becomes the next one.

Four styles, picked in settings → appearance → motion & sound:

    scramble — the letters decode out of block glyphs (1.x's signature,
               ``motion.scramble_text``; kept exactly as it was)
    sweep    — a glowing edge wipes the new title in left to right, the
               old one wiping out ahead of it
    rise     — each letter lifts into place a beat after the last; the
               old line sinks and fades
    off      — the text just changes

Motion "off" makes every style instant: this is motion, so it obeys the
switch that gates motion. ``effective_style`` is the one to consult.

Two ways in. Custom-painted labels (the strip's now-playing variants)
keep a ``TextReveal`` per line and hand their paint to it while it runs.
Plain labels (the mini and fullscreen title / artist / ticker) are
``RevealLabel``s, a QLabel that paints the transition itself; hosts call
``animate_label`` and never touch the styles.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFontMetrics, QLinearGradient, QPainter, QPalette, QPen,
)
from PySide6.QtWidgets import QLabel, QWidget

from .. import theming
from . import motion, scale


STYLES: tuple[str, ...] = ("scramble", "sweep", "rise", "off")
BLURBS: dict[str, str] = {
    "scramble": "scramble · the letters decode",
    "sweep": "sweep · a glow wipes the new title in",
    "rise": "rise · the letters lift into place",
    "off": "off · the text just changes",
}
DEFAULT_STYLE = "scramble"

_style: str = DEFAULT_STYLE


def set_style(name: str) -> None:
    """Pick a style. Unknown names fall back to the default so a stale
    settings value can't wedge the strip."""
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


# ---------------------------------------------------------------- painting

def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _text_left(rect: QRectF, width: float, flags) -> float:
    if flags & Qt.AlignHCenter:
        return rect.left() + (rect.width() - width) / 2.0
    if flags & Qt.AlignRight:
        return rect.right() - width
    return rect.left()


def paint_sweep(p: QPainter, rect: QRectF, old: str, new: str, prog: float,
                fm: QFontMetrics, fg: QColor, accent: QColor, flags) -> None:
    """A lit edge crosses the line left to right: the old text survives
    to its right and fades, the new text is revealed to its left, and
    the letters just behind the edge glow in the accent."""
    band = float(scale.px(56))
    vflags = Qt.AlignVCenter | (flags & (Qt.AlignLeft | Qt.AlignHCenter | Qt.AlignRight))
    shown = fm.elidedText(new, Qt.ElideRight, int(rect.width()))
    text_w = float(fm.horizontalAdvance(shown))
    x0 = _text_left(rect, text_w, flags)
    x1 = x0 + text_w
    # The edge travels the text, plus one band so the glow clears the
    # last letter; it is never drawn past the text itself.
    edge = x0 + prog * (text_w + band)
    p.save()
    p.setRenderHint(QPainter.TextAntialiasing, True)
    # old: right of the edge, fading as the sweep advances
    if old and edge < rect.right():
        p.save()
        p.setClipRect(QRectF(edge, rect.top(), rect.right() - edge, rect.height()))
        p.setOpacity(max(0.0, 1.0 - prog * 1.4))
        p.setPen(fg)
        p.drawText(rect, vflags, fm.elidedText(old, Qt.ElideRight, int(rect.width())))
        p.restore()
    # new: left of the edge
    p.save()
    p.setClipRect(QRectF(rect.left(), rect.top(), max(0.0, edge - rect.left()), rect.height()))
    p.setPen(fg)
    p.drawText(rect, vflags, shown)
    p.restore()
    # the glow: a soft beam and the same letters again, in a gradient of
    # accent that peaks at the edge. Both stay inside the text's run and
    # fade out as the edge crosses its last letter.
    lo = max(x0, edge - band)
    hi = min(edge, x1)
    fade = 1.0 - max(0.0, min(1.0, (edge - x1) / band))
    if hi > lo and fade > 0.0:
        tip = QColor(min(255, accent.red() + 70), min(255, accent.green() + 70),
                     min(255, accent.blue() + 70))
        p.save()
        p.setOpacity(fade)
        p.setClipRect(QRectF(lo, rect.top(), hi - lo, rect.height()))
        beam = QLinearGradient(QPointF(edge - band, 0), QPointF(edge, 0))
        b0 = QColor(accent); b0.setAlphaF(0.0)
        b1 = QColor(accent); b1.setAlphaF(0.10)
        beam.setColorAt(0.0, b0)
        beam.setColorAt(1.0, b1)
        p.fillRect(QRectF(lo, rect.top(), hi - lo, rect.height()), QBrush(beam))
        ink = QLinearGradient(QPointF(edge - band, 0), QPointF(edge, 0))
        i0 = QColor(accent); i0.setAlphaF(0.0)
        ink.setColorAt(0.0, i0)
        ink.setColorAt(0.55, accent)
        ink.setColorAt(1.0, tip)
        p.setPen(QPen(QBrush(ink), 1.0))
        p.drawText(rect, vflags, shown)
        p.restore()
    p.restore()


def paint_rise(p: QPainter, rect: QRectF, old: str, new: str, prog: float,
               fm: QFontMetrics, fg: QColor, flags) -> None:
    """The old line sinks and fades; the new one arrives a letter at a
    time, each lifting into place from just below the baseline."""
    rise = max(4.0, fm.height() * 0.45)
    vflags = Qt.AlignVCenter | Qt.AlignLeft
    p.save()
    p.setRenderHint(QPainter.TextAntialiasing, True)
    if old:
        p.save()
        p.setOpacity(max(0.0, 1.0 - prog * 2.2))
        p.setPen(fg)
        p.drawText(rect.translated(0, prog * rise * 0.6),
                   Qt.AlignVCenter | (flags & (Qt.AlignLeft | Qt.AlignHCenter | Qt.AlignRight)),
                   fm.elidedText(old, Qt.ElideRight, int(rect.width())))
        p.restore()
    shown = fm.elidedText(new, Qt.ElideRight, int(rect.width()))
    if not shown:
        p.restore()
        return
    # Group very long lines so a 90-character title isn't 90 draw calls
    # per frame; the stagger reads the same in pairs.
    step = 1 if len(shown) <= 48 else 2
    pieces = [shown[i:i + step] for i in range(0, len(shown), step)]
    total_w = float(fm.horizontalAdvance(shown))
    x = _text_left(rect, total_w, flags)
    n = len(pieces)
    span = 0.55                                  # of the run, spent staggering starts
    p.setPen(fg)
    for i, piece in enumerate(pieces):
        adv = float(fm.horizontalAdvance(piece))
        start = span * (i / max(1, n - 1)) if n > 1 else 0.0
        t = _ease_out((prog - start) / max(1e-6, 1.0 - span))
        if t > 0.0:
            p.setOpacity(t)
            dy = (1.0 - t) * rise
            p.drawText(QRectF(x, rect.top() + dy, adv + 2.0, rect.height()), vflags, piece)
        x += adv
    p.restore()


class TextReveal:
    """One line's transition: progress 0→1 through the motion profile.
    ``paint`` draws the line for the current progress; hosts fall back
    to their normal paint once ``active`` is false."""

    def __init__(self, owner: QWidget, kind: str) -> None:
        self._owner = owner
        self._kind = kind
        self._old = ""
        self._p = 1.0
        self._style = "off"

    def start(self, old_text: str, *, dur: int | None = None) -> None:
        st = effective_style()
        if st in ("off", "scramble"):
            # scramble is driven by text frames elsewhere; off is instant
            self._p = 1.0
            self._old = ""
            return
        self._style = st
        self._old = str(old_text or "")
        self._p = 0.0
        motion.value_lerp(
            0.0, 1.0, on_update=self._frame,
            dur=dur if dur is not None else motion.dur("med"),
            easing=motion.ease("out"), owner=self._owner,
            kind=f"reveal/{self._kind}")

    def _frame(self, v: float) -> None:
        self._p = max(0.0, min(1.0, float(v)))
        try:
            self._owner.update()
        except RuntimeError:
            pass

    def active(self) -> bool:
        return self._p < 1.0

    def progress(self) -> float:
        return self._p

    def paint(self, p: QPainter, rect, text: str, fm: QFontMetrics, *,
              fg: QColor, accent: QColor, flags=Qt.AlignVCenter | Qt.AlignLeft) -> None:
        r = QRectF(rect)
        if self._style == "sweep":
            paint_sweep(p, r, self._old, text, self._p, fm, fg, accent, flags)
        else:
            paint_rise(p, r, self._old, text, self._p, fm, fg, flags)


# ---------------------------------------------------------------- labels

class RevealLabel(QLabel):
    """A QLabel whose text can arrive by transition. Alignment, font and
    the sheet's colour are honoured; while a reveal runs the label paints
    itself, then hands back to QLabel."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._reveal = TextReveal(self, f"label/{id(self)}")

    def set_text_animated(self, text: str, *, owner: QWidget | None = None,
                          kind: str = "label") -> None:
        st = effective_style()
        if st == "scramble":
            motion.scramble_text(self.setText, text, owner=owner or self, kind=kind)
            return
        if st == "off":
            self.setText(text)
            return
        self._reveal.start(self.text())
        self.setText(text)

    def paintEvent(self, ev) -> None:
        if not self._reveal.active():
            super().paintEvent(ev)
            return
        p = QPainter(self)
        theme = theming.manager().current_effective()
        fg = self.palette().color(QPalette.WindowText)
        accent = QColor(theme.token("accent", "#d4b95e")) if theme is not None else QColor("#d4b95e")
        p.setFont(self.font())
        self._reveal.paint(p, self.contentsRect(), self.text(), QFontMetrics(self.font()),
                           fg=fg, accent=accent, flags=self.alignment() | Qt.AlignVCenter)
        p.end()


def animate_label(label: QLabel, text: str, *, owner: QWidget, kind: str) -> None:
    """Set ``text`` on ``label`` in the picked style. A plain QLabel can
    only scramble or snap; a RevealLabel gets every style."""
    if isinstance(label, RevealLabel):
        label.set_text_animated(text, owner=owner, kind=kind)
        return
    if effective_style() == "scramble":
        motion.scramble_text(label.setText, text, owner=owner, kind=kind)
    else:
        label.setText(text)
