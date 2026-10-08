"""Text that reads against whatever part of the backdrop it lands on.

The adaptive backdrop is not one colour. A cover can light one corner of
the window saturated red while the rest stays near black, and a dim grey
that reads on the dark side is gone on the red one. Theme tokens can't
fix that: there is one ``dim`` for the whole window. So each run of text
asks the backdrop what is behind *it* (``CentralBg.ink_range``) and moves
its own ink just far enough to read there (``contrast.legible_ink``).
Text on a patch the theme's colours already work on is left exactly as
the theme drew it.

Two ways in:

* ``InkStyle``, a proxy over the app style. Every plain-text QLabel and
  the transparent buttons draw their text through ``drawItemText``, which
  hands over the painter (so the widget) and the QSS colour. One override
  covers them all without touching a call site.
* ``ink()`` / ``text_ink()`` for widgets that paint their own text (track
  rows, cards, the now-playing line): they ask for the pen they're about
  to use.

Gated on the "keep text readable over the backdrop" setting, which the
theming manager carries (``text_contrast()``).
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QLabel,
    QProxyStyle,
    QRadioButton,
    QStatusBar,
    QStyle,
    QStyleOption,
    QWidget,
)

from .. import contrast, theming
from . import motion
from .central_bg import CentralBg

# Set this property (truthy) on a widget that paints an opaque panel of its
# own inside the backdrop shell. Text under it is on that panel, not on the
# backdrop, so the lookup stops there.
OPAQUE_PROPERTY = "inkOpaque"

# Probe quantisation for the solve cache: luminance in sqrt space, where
# equal steps are roughly equal steps of perceived lightness.
_Q = 320.0

_CACHE_MAX = 4096

# widget id -> CentralBg the widget sits on (validated on every hit).
_backdrop_of: dict[int, QWidget] = {}
# (widget id, ink rgb, coarse position) -> last side the ink moved to.
_last_side: dict[tuple, int] = {}
# (ink, quantised probe, side memory) -> (ink rgb, side).
_solved: dict[tuple, tuple[int, int]] = {}
# The proxy installed on the app, held so the wrapper outlives install().
_installed: list[QProxyStyle] = []

# Fades. A run's ink eases toward its target instead of jumping to it:
# per run (same key as _last_side), the ink on screen as float rgb and when
# it last stepped. Runs settled on the theme's own ink hold no entry.
_shown: dict[tuple, list[float]] = {}
# Widgets mid-fade, repainted on the next tick so a still scene (nothing
# else repainting them) keeps stepping until the fade lands.
_fading: dict[int, QWidget] = {}
_tick_armed: list[bool] = [False]
# Settled answers: (widget id, ink rgb, rect) -> (backdrop, its ink_version,
# rgb drawn). Every frame of an animated backdrop repaints every run of
# text on it, and between grid rebuilds the answer can't have moved.
_settled: dict[tuple, tuple] = {}
_TICK_MS = 16
# A step never covers more than this much time, so a run that hasn't been
# painted in a while still fades from where it was instead of snapping.
_MAX_STEP_S = 0.05


class _ThemeFacts:
    """What the per-paint path needs from the theme, refreshed on
    theme_changed rather than rebuilt from current_effective() per call.

    ``on_fill``: inks drawn on a filled surface (selection, brutalist
    hover, text cut out of an accent pill). The fill is behind them, not
    the backdrop, so they are left alone. ``modern``: generic buttons
    have a translucent face there and an opaque one in brutalist."""

    def __init__(self) -> None:
        self.ready = False
        self.on_fill: set[int] = set()
        self.modern = False

    def refresh(self, theme=None) -> None:
        mgr = theming.manager()
        if not self.ready:
            mgr.theme_changed.connect(self.refresh)
            self.ready = True
        if theme is None:
            theme = mgr.current_effective()
        self.on_fill = set()
        self.modern = getattr(theme, "aesthetic", "") == "modern"
        if theme is None:
            return
        keep = set()
        for k in ("fg", "dim", "accent"):
            c = QColor(theme.token(k, ""))
            if c.isValid():
                keep.add(c.rgb())
        for k in ("bg", "sel_fg", "hover_fg"):
            c = QColor(theme.token(k, ""))
            if c.isValid() and c.rgb() not in keep:
                self.on_fill.add(c.rgb())


_facts = _ThemeFacts()


def _theme_facts() -> _ThemeFacts:
    if not _facts.ready:
        _facts.refresh()
    return _facts


def active() -> bool:
    return theming.manager().text_contrast()


def _backdrop(widget: QWidget):
    """The CentralBg ``widget`` is painted over, or None (another window,
    a dialog, or a panel marked opaque in between)."""
    key = id(widget)
    bg = _backdrop_of.get(key)
    if bg is not None:
        try:
            if bg.isAncestorOf(widget):
                return bg
        except RuntimeError:            # the backdrop was deleted
            pass
        _backdrop_of.pop(key, None)
    w = widget
    while w is not None:
        if isinstance(w, CentralBg):
            if len(_backdrop_of) >= _CACHE_MAX:
                _backdrop_of.clear()
            _backdrop_of[key] = w
            return w
        if w.property(OPAQUE_PROPERTY) or w.isWindow():
            return None
        w = w.parentWidget()
    return None


def _q(y: float) -> int:
    return int(round(max(0.0, y) ** 0.5 * _Q))


def _solve(rgb: int, qlo: int, qhi: int, qref: int, prefer: int
           ) -> tuple[int, int]:
    key = (rgb, qlo, qhi, qref, prefer)
    hit = _solved.get(key)
    if hit is not None:
        return hit
    col, side = contrast.legible_ink(
        QColor.fromRgb(rgb), (qlo / _Q) ** 2, (qhi / _Q) ** 2,
        (qref / _Q) ** 2, prefer)
    out = (col.rgb(), side)
    if len(_solved) >= _CACHE_MAX:
        _solved.clear()
    _solved[key] = out
    return out


def ink(widget: QWidget, color: QColor, rect: QRect | None = None) -> QColor:
    """``color`` adjusted for the backdrop under ``rect`` (in ``widget``
    coordinates; the whole widget when None). Returns ``color`` itself
    when there is nothing to adjust."""
    if not active():
        return color
    bg = _backdrop(widget)
    if bg is None:
        return color
    rgb = color.rgb()
    if rgb in _theme_facts().on_fill:
        return color
    r = widget.rect() if rect is None else rect
    if r.isEmpty():
        return color
    origin = widget.mapTo(bg, QPoint(0, 0))
    memo = (id(widget), rgb, r.x() + origin.x(), r.y() + origin.y(),
            r.width(), r.height())
    hit = _settled.get(memo)
    if hit is not None and hit[0] is bg and hit[1] == bg.ink_version():
        if hit[2] == rgb:
            return color
        out = QColor.fromRgb(hit[2])
        out.setAlpha(color.alpha())
        return out
    span = bg.ink_range(r.translated(origin))
    if span is None:
        return color
    version = bg.ink_version()
    mem = (id(widget), rgb, r.top() // 16, r.left() // 128)
    prefer = _last_side.get(mem, 0)
    out_rgb, side = _solve(rgb, _q(span[0]), _q(span[1]),
                           _q(bg.ink_reference()), prefer)
    if side != prefer:
        if len(_last_side) >= _CACHE_MAX:
            _last_side.clear()
        _last_side[mem] = side
    target = out_rgb if side else rgb
    if not side and mem not in _shown:
        _settle(memo, bg, version, rgb)
        return color
    shown = _fade(mem, widget, target, rgb)
    if shown == target:
        _settle(memo, bg, version, shown)
    else:
        _settled.pop(memo, None)
    if shown == rgb:
        return color
    out = QColor.fromRgb(shown)
    out.setAlpha(color.alpha())
    return out


def _settle(memo: tuple, bg, version: int, rgb: int) -> None:
    if len(_settled) >= _CACHE_MAX:
        _settled.clear()
    _settled[memo] = (bg, version, rgb)


def _fade_s() -> float:
    """Time constant of the ink fade: tide's "med" duration under the
    active motion profile, a third of it so the ease is all but done by
    then. 0 with motion off, where every change lands at once."""
    if motion.intensity() == motion.Intensity.OFF:
        return 0.0
    return motion.dur("med") / 3000.0


def _fade(mem: tuple, widget: QWidget, target: int, base: int) -> int:
    """Step the run ``mem`` toward ``target`` and return what to draw."""
    tau = _fade_s()
    st = _shown.get(mem)
    if st is None or tau <= 0.0:
        # First sight of this run (a row that just appeared lands on the
        # right ink, it doesn't fade in from the theme's), or motion off.
        if target == base:
            _shown.pop(mem, None)
        else:
            if len(_shown) >= _CACHE_MAX:
                _shown.clear()
            _shown[mem] = [(target >> 16) & 255, (target >> 8) & 255,
                           target & 255, time.monotonic()]
        return target
    now = time.monotonic()
    dt = min(_MAX_STEP_S, max(0.0, now - st[3]))
    st[3] = now
    k = 1.0 - math.exp(-dt / tau)
    goal = ((target >> 16) & 255, (target >> 8) & 255, target & 255)
    done = True
    for i in range(3):
        st[i] += (goal[i] - st[i]) * k
        if abs(goal[i] - st[i]) >= 1.0:
            done = False
    if done:
        if target == base:
            del _shown[mem]
        else:
            st[0], st[1], st[2] = goal
        return target
    _fading[id(widget)] = widget
    if not _tick_armed[0]:
        _tick_armed[0] = True
        QTimer.singleShot(_TICK_MS, _tick)
    return (0xFF000000 | (int(round(st[0])) << 16)
            | (int(round(st[1])) << 8) | int(round(st[2])))


def _tick() -> None:
    _tick_armed[0] = False
    widgets = list(_fading.values())
    _fading.clear()
    for w in widgets:
        try:
            w.update()
        except RuntimeError:                 # deleted mid-fade
            pass


def text_ink(painter: QPainter, color: QColor, rect, flags: int = 0,
             text: str = "") -> QColor:
    """``ink`` for text about to be drawn with ``painter`` into ``rect``.
    With ``text`` (and the painter's font) the probe shrinks to where the
    glyphs actually land, so a wide, mostly empty label only answers for
    the patch its words sit on."""
    widget = painter.device()
    if not isinstance(widget, QWidget) or not active():
        return color
    area = QRect(rect) if isinstance(rect, QRect) else QRectF(rect).toAlignedRect()
    if text:
        tight = painter.fontMetrics().boundingRect(area, int(flags), text)
        tight = tight.intersected(area)
        if not tight.isEmpty():
            area = tight
    tf = painter.combinedTransform()
    if not tf.isIdentity():
        area = tf.mapRect(QRectF(area)).toAlignedRect()
    return ink(widget, color, area)


def _reads_backdrop(widget: QWidget) -> bool:
    """Whether a widget's ``drawItemText`` lands on the backdrop rather
    than on a face of its own. Labels do. Buttons do while their face is
    clear: the BracketButton (not the filled play circle), check and
    radio labels, and modern's translucent generic buttons (not while
    pressed, when they fill). Combos, tabs and headers have opaque faces
    and are left to the theme."""
    if isinstance(widget, QLabel):
        return True
    if isinstance(widget, QAbstractButton):
        if widget.objectName() == "BracketButton":
            # Pressed and hovered faces are translucent in modern, and the
            # filled brutalist ones draw in on-fill inks, which ink() skips.
            return not widget.property("primary")
        if widget.isDown():
            return False
        if isinstance(widget, (QCheckBox, QRadioButton)):
            return True
        return _theme_facts().modern
    return False


class InkStyle(QProxyStyle):
    """App style proxy: plain text drawn through the style reads the
    backdrop behind it. Everything else passes straight to the base."""

    def drawItemText(self, painter, rect, flags, pal, enabled, text,
                     textRole=QPalette.NoRole):
        if text and textRole != QPalette.NoRole and active():
            widget = painter.device()
            if isinstance(widget, QWidget) and _reads_backdrop(widget):
                base = pal.color(textRole)
                new = text_ink(painter, base, rect, flags, text)
                if new.rgba() != base.rgba():
                    pal = QPalette(pal)
                    pal.setColor(pal.currentColorGroup(), textRole, new)
        super().drawItemText(painter, rect, flags, pal, enabled, text,
                             textRole)


def near(a: QColor, b: QColor, tol: int) -> bool:
    """Whether two inks are within ``tol`` on every channel: close enough
    that redrawing for the difference isn't worth it."""
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()),
               abs(a.blue() - b.blue())) < tol


class InkLabel(QLabel):
    """For labels Qt paints through its text document: selectable text
    and rich text (lyrics, karaoke). That path never calls drawItemText,
    so the proxy can't reach it, and QLabel re-reads the sheet's colour
    at paint time, so a palette can't either. What does reach it is the
    label's own sheet: when the ink has to move, a ``color:`` goes on the
    end of whatever sheet the caller set, and comes off again when the
    patch behind is fine.

    The sheet change lands right after the paint that asked for it (a
    restyle mid-paint is asking for trouble), and only for moves the eye
    can see, so a drifting backdrop doesn't restyle every frame."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ink_sheet = ""                  # the caller's own sheet
        self._ink_base: QColor | None = None  # the ink that sheet gives
        self._ink_shown: QColor | None = None
        self._ink_pending: QColor | None = None
        theming.manager().theme_changed.connect(self._ink_theme_changed)

    def setStyleSheet(self, sheet: str) -> None:
        self._ink_sheet = sheet or ""
        self._ink_base = self._ink_shown = None
        super().setStyleSheet(self._ink_sheet)

    def _ink_theme_changed(self, _theme=None) -> None:
        # The app sheet is about to move (its restyle is queued for the
        # next turn): drop the override after it lands, so the next paint
        # reads the theme's new colour as the base.
        if self._ink_shown is not None:
            QTimer.singleShot(0, self._ink_reset)

    def _ink_reset(self) -> None:
        if self._ink_shown is None:
            return
        self._ink_shown = None
        try:
            super().setStyleSheet(self._ink_sheet)
        except RuntimeError:                 # the label is gone
            pass

    def paintEvent(self, event) -> None:
        self._check_ink()
        super().paintEvent(event)

    def _check_ink(self) -> None:
        if self._ink_shown is None:
            # No override on: the palette is the sheet's (or the theme's)
            # own colour, and that is the base.
            self._ink_base = QColor(self.palette().color(self.foregroundRole()))
        base = self._ink_base
        shown = self._ink_shown or base
        want = ink(self, base, self.contentsRect())
        if want.rgba() == shown.rgba():
            return
        if want.rgba() != base.rgba() and shown.rgba() != base.rgba() \
                and near(want, shown, 8):
            return
        if self._ink_pending is None:
            QTimer.singleShot(0, self._apply_ink)
        self._ink_pending = want

    def _apply_ink(self) -> None:
        want, self._ink_pending = self._ink_pending, None
        if want is None or self._ink_base is None:
            return
        sheet = self._ink_sheet
        if want.rgba() == self._ink_base.rgba():
            self._ink_shown = None
        else:
            self._ink_shown = QColor(want)
            if "{" in sheet:
                sheet = f"{sheet}\n* {{ color: {want.name()}; }}"
            else:
                sheet = f"{sheet.rstrip().rstrip(';')}; color: {want.name()};" \
                    if sheet.strip() else f"color: {want.name()};"
        super().setStyleSheet(sheet)


class InkStatusBar(QStatusBar):
    """QStatusBar paints its message with a bare drawText, past the style,
    so the proxy never sees it. With no item widgets showing, the panel
    and the message are all it draws; this draws the same two, with an
    ink that reads on the backdrop. Anything else goes to Qt's own."""

    def paintEvent(self, event) -> None:
        msg = self.currentMessage()
        if not msg or not active() or any(
                w.isVisible() for w in self.findChildren(
                    QWidget, "", Qt.FindDirectChildrenOnly)):
            super().paintEvent(event)
            return
        p = QPainter(self)
        opt = QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_PanelStatusBar, opt, p, self)
        # Qt's message rect when nothing else sits in the bar.
        rect = self.rect().adjusted(6, 0, -12, 0)
        flags = Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine
        p.setPen(text_ink(p, self.palette().windowText().color(), rect,
                          flags, msg))
        p.drawText(rect, flags, msg)
        p.end()


def install(app, base_style) -> None:
    """Make ``base_style`` the app style behind an InkStyle proxy."""
    proxy = InkStyle(base_style)
    _installed[:] = [proxy]
    app.setStyle(proxy)


def reset_caches() -> None:
    """Tests: forget every remembered backdrop, side and solve."""
    _backdrop_of.clear()
    _last_side.clear()
    _solved.clear()
    _shown.clear()
    _settled.clear()
    _fading.clear()
    if _facts.ready:
        _facts.refresh()
