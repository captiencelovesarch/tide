"""tide-drawn titlebar (client-side decorations) for the main window.

KWin's server-side titlebar colors follow the *system* color scheme, so a
warm light theme like golden-hour sat under a grey stranger's bar. With
`csd_titlebar` on (the default), the main window goes frameless and this
widget takes the decoration's place via `QMainWindow.setMenuWidget`:

- themed straight from the active theme's tokens (the QSS block lives in
  theming._TITLEBAR_QSS so every theme — including third-party dirs —
  gets it for free, and any theme can override it with its own rules);
- per-theme personality: brutalist themes get bracket buttons and the
  title runs through styled_case ("TIDE" in storm, l33t in synthwave);
- drag-to-move via startSystemMove (compositor-native, so KDE snapping
  and tiling keep working), double-click maximizes;
- edge resize via an application-level hit test → startSystemResize
  (frameless windows lose the compositor's resize borders; KDE's
  Meta+drag continues to work as the native fallback).

Dialogs and the settings window keep their native decorations — they're
transient, and the wins here are for the window you live in.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from .. import theming

_RESIZE_MARGIN_PX = 6

# Edge names → Qt.Edges for startSystemResize.
def _edges_at(window: QWidget, global_pos: QPoint) -> Qt.Edge:
    if window.isMaximized() or window.isFullScreen():
        return Qt.Edge(0)
    pos = window.mapFromGlobal(global_pos)
    w, h, m = window.width(), window.height(), _RESIZE_MARGIN_PX
    edges = Qt.Edge(0)
    if pos.x() <= m:
        edges |= Qt.LeftEdge
    elif pos.x() >= w - m:
        edges |= Qt.RightEdge
    if pos.y() <= m:
        edges |= Qt.TopEdge
    elif pos.y() >= h - m:
        edges |= Qt.BottomEdge
    return edges


class EdgeResizer(QObject):
    """App-level filter that turns border presses into system resizes.

    Children cover a frameless window wall to wall, so a filter on the
    window alone never sees presses near the edges — hit-testing every
    press at the application level does.
    """

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        QApplication.instance().installEventFilter(self)

    def detach(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.MouseButtonPress:
            return False
        if not isinstance(event, QMouseEvent) or event.button() != Qt.LeftButton:
            return False
        if not self._window.isVisible():
            return False
        # Only presses that land inside this window's frame concern us.
        if not isinstance(obj, QWidget) or obj.window() is not self._window:
            return False
        edges = _edges_at(self._window, event.globalPosition().toPoint())
        if not edges:
            return False
        handle = self._window.windowHandle()
        if handle is None:
            return False
        handle.startSystemResize(edges)
        return True


class _WinButton(QPushButton):
    """Window-control button that paints its own min/max/close glyph.

    Font glyphs proved unfixable here: –, □ and ✕ each sit at a different
    height within the line box (measured: the dash and box center a full
    1.5px below the cross in IBM Plex), so no character swap lines all
    three up. Painting puts every glyph on the same geometric center.

    Brutalist themes instead run in text mode ("[_]" and friends) — the
    QSS text path draws those, and this class stays out of the way.
    """

    GLYPH_PX = 9   # side of the square the glyph is drawn in (pre-scale)

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind            # "min" | "max" | "close"
        self._maxed = False
        self._text_mode = False

    def set_text_mode(self, on: bool) -> None:
        self._text_mode = bool(on)
        if not on:
            self.setText("")
        self.update()

    def set_maximized(self, maxed: bool) -> None:
        self._maxed = bool(maxed)
        self.update()

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)       # QSS background (incl. hover states)
        if self._text_mode:
            return                   # QSS already drew the bracket text
        theme = theming.manager().current()

        def tok(name: str, fallback: str) -> str:
            return theme.token(name, fallback) if theme else fallback

        hover = self.underMouse()
        if not self.isEnabled():
            color = tok("dim", "#666")
        elif hover and self._kind == "close":
            # QSS gives close an accent hover fill; ink flips to bg.
            color = tok("bg", "#0b0b0b")
        elif hover:
            color = tok("fg", "#e6e6e6")
        else:
            color = tok("dim", "#8a8a8a")

        from . import scale as _scale
        s = float(_scale.px(self.GLYPH_PX))
        c = QPointF(self.rect().center()) + QPointF(0.5, 0.5)
        half = s / 2.0
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(color)
        pen.setWidthF(1.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        if self._kind == "min":
            p.drawLine(QPointF(c.x() - half, c.y()), QPointF(c.x() + half, c.y()))
        elif self._kind == "max" and not self._maxed:
            p.drawRect(QRectF(c.x() - half, c.y() - half, s, s))
        elif self._kind == "max":
            # Restore: front square low-left, back square peeking up-right.
            off = s * 0.3
            front = QRectF(c.x() - half, c.y() - half + off, s - off, s - off)
            p.drawRect(front)
            p.drawLine(QPointF(front.left() + off, front.top() - off),
                       QPointF(front.right() + off, front.top() - off))
            p.drawLine(QPointF(front.right() + off, front.top() - off),
                       QPointF(front.right() + off, front.bottom() - off))
        else:   # close
            p.drawLine(QPointF(c.x() - half, c.y() - half), QPointF(c.x() + half, c.y() + half))
            p.drawLine(QPointF(c.x() - half, c.y() + half), QPointF(c.x() + half, c.y() - half))
        p.end()


class TitleBar(QWidget):
    """The bar itself: themed title on the left, window controls right."""

    HEIGHT_PX = 34

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._press_pos: QPoint | None = None
        self.setObjectName("TitleBar")
        # Plain QWidgets don't paint QSS backgrounds without this.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT_PX)

        self.title = QLabel("tide", self)
        self.title.setObjectName("TitleBarTitle")

        self.min_btn = _WinButton("min", self)
        self.max_btn = _WinButton("max", self)
        self.close_btn = _WinButton("close", self)
        for name, btn in (
            ("TitleBarMin", self.min_btn),
            ("TitleBarMax", self.max_btn),
            ("TitleBarClose", self.close_btn),
        ):
            btn.setObjectName(name)
            btn.setProperty("class", "TitleBarBtn")
            btn.setFixedSize(38, self.HEIGHT_PX - 6)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setCursor(Qt.PointingHandCursor)

        self.min_btn.clicked.connect(self._window.showMinimized)
        self.max_btn.clicked.connect(self.toggle_maximized)
        self.close_btn.clicked.connect(self._window.close)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(4)
        lay.addWidget(self.title)
        lay.addStretch(1)
        lay.addWidget(self.min_btn)
        lay.addWidget(self.max_btn)
        lay.addWidget(self.close_btn)

        theming.manager().theme_changed.connect(self._on_theme)
        self._window.installEventFilter(self)   # WindowStateChange → glyphs
        self._on_theme(theming.manager().current())

    # ---------- theming ----------

    def _on_theme(self, theme) -> None:
        brutalist = bool(theme is not None
                         and getattr(theme, "aesthetic", "") == "brutalist")
        maxed = self._window.isMaximized()
        for btn in (self.min_btn, self.max_btn, self.close_btn):
            btn.set_text_mode(brutalist)
        self.max_btn.set_maximized(maxed)
        if brutalist:
            # Text mode keeps the bracket personality; the deliberate
            # baseline-underscore look is part of the aesthetic there.
            self.min_btn.setText("[_]")
            self.max_btn.setText("[❐]" if maxed else "[□]")
            self.close_btn.setText("[✕]")
        try:
            self.title.setText(theming.styled_case("tide"))
        except Exception:
            self.title.setText("tide")

    # ---------- window plumbing ----------

    def toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._window and event.type() == QEvent.WindowStateChange:
            self._on_theme(theming.manager().current())
        return False

    # ---------- mouse: drag to move, double-click to maximize ----------

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._press_pos = ev.globalPosition().toPoint()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self._press_pos is not None:
            moved = (ev.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                handle = self._window.windowHandle()
                if handle is not None:
                    handle.startSystemMove()
                    ev.accept()
                    return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._press_pos = None
            self.toggle_maximized()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)
