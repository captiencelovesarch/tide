"""Central-area background.

A wrapper widget that sits between the QStackedWidget and the rest of the
window. By itself it paints whatever the theme says ``bg`` is; when
``adaptive_background`` is on, it paints a soft vertical gradient from
``bg`` (top) to ``bg_alt`` (bottom). The adaptive driver supplies
``bg_alt`` via the theming manager's runtime overrides, so the gradient
shifts with album art automatically without the wrapper needing to know
anything about palette extraction.

Corners obey ``corner_style`` (sharp / soft / rounded). The radius is
applied to both the gradient draw and the clipping mask, so the gradient
stops *inside* the rounded shape — the window's bg shows through the
corners cleanly.

Child widgets (the QStackedWidget and its pages) keep their own
QSS-defined backgrounds; this widget paints *underneath* whatever opaque
content the views render, so the gradient is most visible in views with
lots of negative space (visualizer, lyrics, the gaps around lists).
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QWidget

from .. import theming


# Maps the corner_style setting to a pixel radius. Kept here so the dialog
# and the painter share one source of truth.
CORNER_RADII: dict[str, int] = {
    "sharp": 0,
    "soft": 6,
    "rounded": 12,
}


def corner_radius(style: str) -> int:
    return CORNER_RADII.get(style or "sharp", 0)


class CentralBg(QWidget):
    """Wraps the central QStackedWidget. Paints a gradient when enabled."""

    def __init__(self, child: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # WA_StyledBackground=False so QSS doesn't override our paintEvent
        # (the brutalist theme sets `QWidget { background: @bg }` globally).
        self.setAttribute(Qt.WA_StyledBackground, False)
        # We DO want a backing buffer so children compose against our paint
        # rather than the window's bg, which prevents flicker on resize.
        self.setAutoFillBackground(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(child)

        self._enabled: bool = False
        self._radius: int = 0
        self._bg = QColor("#0b0b0b")
        self._bg_alt = QColor("#141414")

        theming.manager().theme_changed.connect(self._on_theme)
        self._on_theme(theming.manager().current())

    # ---------- public API ----------

    def set_enabled(self, on: bool) -> None:
        if on == self._enabled:
            return
        self._enabled = on
        self.update()

    def set_radius(self, radius: int) -> None:
        r = max(0, int(radius))
        if r == self._radius:
            return
        self._radius = r
        self.update()

    # ---------- theme tracking ----------

    def _on_theme(self, theme) -> None:
        if theme is None:
            return
        # The adaptive driver pushes bg_alt as a dynamic override; the
        # theming manager re-emits theme_changed when that happens, so we
        # repaint with the new tinted color without any additional wiring.
        self._bg = QColor(theme.token("bg", "#0b0b0b"))
        self._bg_alt = QColor(theme.token("bg_alt", "#141414"))
        self.update()

    # ---------- paint ----------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            rect = self.rect()
            if self._radius > 0:
                # Build a rounded clip path so the gradient stops inside it,
                # leaving sharp window corners as the bg of whatever's behind.
                path = QPainterPath()
                path.addRoundedRect(
                    float(rect.left()), float(rect.top()),
                    float(rect.width()), float(rect.height()),
                    float(self._radius), float(self._radius),
                )
                p.setClipPath(path)

            if self._enabled:
                grad = QLinearGradient(rect.left(), rect.top(),
                                       rect.left(), rect.bottom())
                # Three stops give a softer landing than a 2-stop linear:
                # the bg holds for the first 35% then eases into bg_alt.
                grad.setColorAt(0.0, self._bg)
                grad.setColorAt(0.35, self._bg)
                grad.setColorAt(1.0, self._bg_alt)
                p.fillRect(rect, QBrush(grad))
            else:
                p.fillRect(rect, self._bg)
        finally:
            p.end()
