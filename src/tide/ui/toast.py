"""Slide-up toast for non-modal feedback.

A small panel that slides in from the bottom-right of the parent window,
holds for a couple seconds, then fades out. Used in place of QMessageBox
for failures that don't need a click.

Optional action button on the right (e.g. `[view]` / `[re-import]`) — the
toast stays open until the user dismisses or clicks the action.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QTimer,
    Qt,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from .. import theming
from . import motion as motion_module


DEFAULT_LIFETIME_MS = 4500


def _color(theme, key: str, default: str) -> str:
    if theme is None:
        return default
    return theme.token(key, default)


class Toast(QFrame):
    """One toast. Auto-shows on construct, auto-hides on lifetime."""

    def __init__(
        self,
        parent: QWidget,
        message: str,
        *,
        action_label: str | None = None,
        on_action: Callable[[], None] | None = None,
        lifetime_ms: int = DEFAULT_LIFETIME_MS,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setFrameShape(QFrame.NoFrame)
        self.setWindowFlags(self.windowFlags() | Qt.SubWindow)
        self._lifetime_ms = lifetime_ms
        self._on_action = on_action
        self._dismissed = False

        theme = theming.manager().current()
        bg = _color(theme, "bg_alt", "#1a1a1a")
        fg = _color(theme, "fg", "#e6e6e6")
        border = _color(theme, "border_col", "#e6e6e6")
        accent = _color(theme, "accent", "#d4b95e")
        self.setStyleSheet(
            f"#Toast {{ background: {bg}; color: {fg}; border: 1px solid {border}; "
            f"border-radius: 4px; }}"
            f"QLabel {{ color: {fg}; background: transparent; }}"
            f"QPushButton {{ background: transparent; color: {accent}; "
            f"border: 1px solid {accent}; border-radius: 3px; padding: 3px 10px; }}"
            f"QPushButton:hover {{ background: {accent}; color: {bg}; }}"
            # The ✕ is square and glyph-only: the 10px side padding above
            # ate all but 4px of its fixed 24px width, so it rendered as a
            # clipped dot instead of a cross.
            f"QPushButton#ToastDismiss {{ padding: 0px; }}"
        )

        self._label = QLabel(theming.styled_case(message), self)
        self._label.setWordWrap(True)
        # heightForWidth is what makes a wrapped label report its REAL height
        # once the toast width is capped below. Without it the label keeps
        # advertising its single-line hint, the frame gets sized to one line,
        # and every message longer than ~50 chars renders clipped.
        label_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        label_policy.setHeightForWidth(True)
        self._label.setSizePolicy(label_policy)
        # Toast text can carry remote-derived error strings (e.g. a server
        # error message); plain-text it so markup never renders as HTML.
        self._label.setTextFormat(Qt.PlainText)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)
        layout.addWidget(self._label, stretch=1)

        if action_label and on_action:
            self._action_btn = QPushButton(theming.styled_case(action_label), self)
            self._action_btn.clicked.connect(self._on_action_clicked)
            layout.addWidget(self._action_btn)
            # Toasts with actions don't auto-dismiss.
            self._lifetime_ms = 0

        self._dismiss_btn = QPushButton("✕", self)
        self._dismiss_btn.setObjectName("ToastDismiss")
        self._dismiss_btn.setFixedSize(24, 24)
        self._dismiss_btn.setToolTip("dismiss")
        self._dismiss_btn.clicked.connect(self.dismiss)
        layout.addWidget(self._dismiss_btn)

        self.adjustSize()
        # Cap width so long messages wrap nicely.
        parent_w = parent.width() if parent else 800
        max_w = min(420, max(260, parent_w - 80))
        self.setMaximumWidth(max_w)
        # Settle on the final width first, then ask the layout how tall the
        # text actually needs to be AT that width. adjustSize() alone uses
        # sizeHint(), which for a wrapped label is the unwrapped one-liner.
        w = min(max_w, max(self.sizeHint().width(), self.minimumSizeHint().width()))
        h = self.heightForWidth(w)
        if h <= 0:
            h = self.sizeHint().height()
        self.resize(w, h)

        if parent is not None:
            parent.installEventFilter(self)

        self._slide_in()

        if self._lifetime_ms > 0:
            QTimer.singleShot(self._lifetime_ms, self.dismiss)

    # ---------- animation ----------
    # All through the motion module: at intensity OFF the toast appears at
    # its resting spot and vanishes on dismiss — zero animation objects.

    def _slide_in(self) -> None:
        target = self._target_position()
        start = QPoint(target.x() + self.width() + 40, target.y())
        # Not shown yet (motion.fade_in calls show), so this pre-placement
        # never paints — at OFF the slide immediately re-moves to target.
        self.move(start)
        # spring ease: mechanical lands decisively; springy pops past + settles.
        motion_module.slide(
            self, start, target, easing=motion_module.ease("spring"),
        )
        motion_module.fade_in(self)

    def _slide_out(self) -> None:
        motion_module.fade_out(
            self, hide_on_done=False, on_done=self.deleteLater,
        )

    def _target_position(self) -> QPoint:
        parent = self.parent()
        if parent is None:
            return QPoint(0, 0)
        pw = parent.width() if isinstance(parent, QWidget) else 800
        ph = parent.height() if isinstance(parent, QWidget) else 600
        margin = 18
        # Stack with any siblings above us.
        offset = 0
        for sibling in (parent.findChildren(Toast) if isinstance(parent, QWidget) else []):
            if sibling is self or sibling._dismissed:
                continue
            offset += sibling.height() + 8
        x = pw - self.width() - margin
        y = ph - self.height() - margin - offset
        return QPoint(max(margin, x), max(margin, y))

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Resize:
            self.move(self._target_position())
        return super().eventFilter(obj, event)

    # ---------- actions ----------

    def _on_action_clicked(self) -> None:
        if self._on_action is not None:
            try:
                self._on_action()
            except Exception:
                pass
        self.dismiss()

    def dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self._slide_out()


def show_toast(parent: QWidget, message: str, **kwargs) -> Toast:
    """Convenience: spawn a toast attached to ``parent``."""
    return Toast(parent, message, **kwargs)
