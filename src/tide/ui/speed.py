"""Playback-speed UI.

A bracket-styled toggle that displays the current playback speed (e.g.
``[1.25×]``) and opens a small popover for fine adjustment. Right-click the
button to reset to 1.0×.

The popover offers three ways to change speed: a −/+ row that nudges by
0.05, a preset row of common TikTok values (0.5 / 0.75 / 1.0 / 1.25 / 1.5 /
2.0), and an explicit "reset" button. All actions emit a single
``speed_changed`` signal — the SpeedButton is the authoritative store and
syncs the popover after each change so the displayed value never drifts.

Speed range is clamped to [0.5, 2.0]. mpv accepts wider but anything outside
this range is more "novelty" than "audible," so the UI doesn't expose it.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from .widgets import BracketButton


# Public range — kept module-level so window.py / shortcuts share the same
# clamp as the UI.
SPEED_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
SPEED_MIN = 0.5
SPEED_MAX = 2.0
SPEED_STEP = 0.05


def format_speed(speed: float) -> str:
    """Render a speed value for the UI. Prefers a single decimal when the
    value is "round" so we get ``"1.0×"`` not ``"1.00×"`` or ``"1×"``; for
    in-between values like 1.25 we keep the two decimals so the user can
    tell the difference between similar nudges."""
    if abs(speed * 10 - round(speed * 10)) < 1e-3:
        return f"{speed:.1f}×"
    return f"{speed:.2f}×"


def _clamp(value: float) -> float:
    # Quantize to the step grid so floating math doesn't accumulate (e.g. a
    # chain of −0.05 nudges shouldn't drift off to 1.0500000004×).
    snapped = round(float(value) / SPEED_STEP) * SPEED_STEP
    return max(SPEED_MIN, min(SPEED_MAX, round(snapped, 2)))


class SpeedButton(BracketButton):
    """Speed indicator + entry point to the popover.

    Owns the authoritative ``_speed`` value; the popover only reflects it.
    Emits ``speed_changed(float)`` whenever the value actually changes (no
    re-emit on a no-op set, so listeners can wire freely)."""

    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(format_speed(1.0), parent=parent)
        self._speed: float = 1.0
        self._popover: SpeedPopover | None = None
        self.clicked.connect(self._open_popover)
        self.setToolTip("playback speed — right-click to reset to 1.0×")

    def speed(self) -> float:
        return self._speed

    def set_speed(self, value: float, *, emit: bool = True) -> None:
        clamped = _clamp(value)
        if abs(clamped - self._speed) < 1e-4:
            # Even on no-op, keep the popover's display in sync — the user
            # may have clicked a preset that snapped to the current value.
            if self._popover is not None and self._popover.isVisible():
                self._popover.sync(clamped)
            return
        self._speed = clamped
        self.setLabel(format_speed(clamped))
        if self._popover is not None and self._popover.isVisible():
            self._popover.sync(clamped)
        if emit:
            self.speed_changed.emit(self._speed)

    def reset(self) -> None:
        self.set_speed(1.0)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.RightButton:
            self.reset()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def _open_popover(self) -> None:
        if self._popover is None:
            # Parent the popover to the main window so it floats above the
            # button without inheriting the button's layout constraints.
            self._popover = SpeedPopover(self.window())
            self._popover.speed_changed.connect(self.set_speed)
        self._popover.sync(self._speed)
        self._popover.show_above(self)


class SpeedPopover(QFrame):
    """Compact popup with the current value, ± nudges, presets, and reset.

    Uses ``Qt.Popup`` so Qt closes it automatically when the user clicks
    anywhere outside (including the SpeedButton itself, which means
    second-click on the button closes it — natural toggle feel).
    """

    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("SpeedPopover")
        self._apply_theme(theming.manager().current())
        theming.manager().theme_changed.connect(self._apply_theme)

        # Current value, big & centered.
        self._display = QLabel(format_speed(1.0))
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setObjectName("SpeedPopoverDisplay")
        # Inline style for the bigger font — the global QSS doesn't know
        # about this widget specifically and we don't want to plumb a token
        # for one display.
        self._display.setStyleSheet("font-weight: 600;")

        # ± row.
        self._minus_btn = BracketButton(f"−{SPEED_STEP:.2f}")
        self._plus_btn = BracketButton(f"+{SPEED_STEP:.2f}")
        self._minus_btn.clicked.connect(self._on_minus)
        self._plus_btn.clicked.connect(self._on_plus)

        adjust_row = QHBoxLayout()
        adjust_row.setSpacing(8)
        adjust_row.addWidget(self._minus_btn)
        adjust_row.addWidget(self._display, stretch=1)
        adjust_row.addWidget(self._plus_btn)

        # Preset row.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        self._preset_btns: list[BracketButton] = []
        for p in SPEED_PRESETS:
            btn = BracketButton(format_speed(p))
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, v=p: self.speed_changed.emit(v))
            self._preset_btns.append(btn)
            preset_row.addWidget(btn)

        # Reset.
        self._reset_btn = BracketButton("reset")
        self._reset_btn.clicked.connect(lambda: self.speed_changed.emit(1.0))

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addLayout(adjust_row)
        root.addLayout(preset_row)
        root.addWidget(self._reset_btn, alignment=Qt.AlignRight)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._current: float = 1.0

    def sync(self, speed: float) -> None:
        self._current = speed
        self._display.setText(format_speed(speed))
        # Disable ± when at extremes so the user gets a hint that this is
        # the edge of the range.
        self._minus_btn.setEnabled(speed > SPEED_MIN + 1e-4)
        self._plus_btn.setEnabled(speed < SPEED_MAX - 1e-4)

    def show_above(self, anchor: QWidget) -> None:
        """Place the popover horizontally centered on ``anchor`` and just
        above it. Falls back to below if the anchor is too close to the
        screen top."""
        self.adjustSize()
        anchor_top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        x = anchor_top_left.x() + (anchor.width() - self.width()) // 2
        y = anchor_top_left.y() - self.height() - 4
        screen = anchor.screen()
        if screen is not None:
            geom = screen.availableGeometry()
            if y < geom.top():
                # Not enough room above — flip below.
                y = anchor_top_left.y() + anchor.height() + 4
            # Keep within horizontal screen bounds too.
            x = max(geom.left() + 4, min(x, geom.right() - self.width() - 4))
        self.move(x, y)
        self.show()
        self.raise_()

    # ---------- internals ----------

    def _on_minus(self) -> None:
        self.speed_changed.emit(self._current - SPEED_STEP)

    def _on_plus(self) -> None:
        self.speed_changed.emit(self._current + SPEED_STEP)

    def _apply_theme(self, theme) -> None:
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        self.setStyleSheet(
            f"QFrame#SpeedPopover {{ background: {bg}; border: 1px solid {fg}; }}"
        )
