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

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QMouseEvent
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

        self.min_btn = QPushButton(self)
        self.max_btn = QPushButton(self)
        self.close_btn = QPushButton(self)
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
        if brutalist:
            self.min_btn.setText("[_]")
            self.max_btn.setText("[❐]" if maxed else "[□]")
            self.close_btn.setText("[✕]")
        else:
            self.min_btn.setText("–")
            self.max_btn.setText("❐" if maxed else "□")
            self.close_btn.setText("✕")
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
