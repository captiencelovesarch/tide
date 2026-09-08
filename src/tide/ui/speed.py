"""Playback-speed UI.

A bracket-styled toggle that displays the current playback speed (e.g.
``[1.25×]``) and opens a small popover for fine adjustment. Right-click the
button to reset to 1.0×.

The popover has two faces, picked from the window's active personality
when it's built: brutalist (default) — the original bracket popover;
modern — a SpringSlider over the same law with detents on the presets,
±0.05 chips, and a live readout. Both emit one ``speed_changed`` signal
— the SpeedButton is the authoritative store and syncs the popover after
each change so the displayed value never drifts. A personality flip
rebuilds the cached popover on the next open, never mid-show.

The button greys itself (``set_backend_supported``) when the backend
can't do variable speed (librespot renders Spotify's audio server-side);
greyed refuses every user-driven path, keyboard and MPRIS included —
only silent programmatic restores (``emit=False``) land.

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
from . import scale
from .spring_slider import SpringSlider
from .widgets import BracketButton, paint_popover_panel

# The speed law lives in speed_law.py so the player/router can share it
# without importing ui; window/mpris/shortcuts still import the names here.
from ..speed_law import (  # noqa: F401  (re-exports)
    SPEED_MAX,
    SPEED_MIN,
    SPEED_PRESETS,
    SPEED_STEP,
    format_speed,
)
from ..speed_law import clamp as _clamp  # noqa: F401  (re-export)


_TIP_NORMAL = "playback speed — right-click to reset to 1.0×"
_TIP_UNSUPPORTED = "speed n/a on this source"


class SpeedButton(BracketButton):
    """Speed indicator + entry point to the popover.

    Owns the authoritative ``_speed`` value; the popover only reflects it.
    Emits ``speed_changed(float)`` whenever the value actually changes (no
    re-emit on a no-op set, so listeners can wire freely)."""

    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(format_speed(1.0), parent=parent)
        self._speed: float = 1.0
        self._popover: QFrame | None = None
        self._popover_face_built: str = ""
        self._backend_supported: bool = True
        self.clicked.connect(self._open_popover)
        self.setToolTip(_TIP_NORMAL)

    def speed(self) -> float:
        return self._speed

    def set_speed(self, value: float, *, emit: bool = True) -> None:
        if emit and not self._backend_supported:
            # Greyed is inert on EVERY path: setEnabled(False) stops
            # clicks, but the [ ] \ keymap actions and MPRIS SetRate call
            # straight in — honoring them would walk the label to a speed
            # the audio isn't playing at. emit=False restores still land.
            return
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

    def set_backend_supported(self, supported: bool) -> None:
        """Grey the control: no popover, no reset, no user-driven
        ``set_speed`` at all. The stored speed is untouched — it
        re-applies when playback lands back on a capable backend."""
        supported = bool(supported)
        if supported == self._backend_supported:
            return
        self._backend_supported = supported
        self.setEnabled(supported)
        self.setToolTip(_TIP_NORMAL if supported else _TIP_UNSUPPORTED)
        if not supported and self._popover is not None \
                and self._popover.isVisible():
            self._popover.hide()

    def backend_supported(self) -> bool:
        return self._backend_supported

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.RightButton:
            self.reset()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def _popover_face(self) -> str:
        """``"spring"`` under the modern personality, else ``"bracket"``
        (unknown/absent settings, tests, third-party presets → bracket)."""
        settings = getattr(self.window(), "_settings", None)
        preset = str(getattr(settings, "preset", "") or "")
        return "spring" if preset == "modern" else "bracket"

    def _open_popover(self) -> None:
        face = self._popover_face()
        if self._popover is not None and self._popover_face_built != face:
            # Personality flipped since build — rebuild for this open
            # (the popover holds no state sync() doesn't push).
            self._popover.deleteLater()
            self._popover = None
        if self._popover is None:
            # Parent the popover to the main window so it floats above the
            # button without inheriting the button's layout constraints.
            cls = SpringSpeedPopover if face == "spring" else SpeedPopover
            self._popover = cls(self.window())
            self._popover.speed_changed.connect(self.set_speed)
            self._popover_face_built = face
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


class SpringSpeedPopover(QFrame):
    """The modern face: a magnetic-detent SpringSlider over the speed law.

    Same external surface as SpeedPopover (``speed_changed`` / ``sync`` /
    ``show_above``) so the SpeedButton can't tell the faces apart; the
    bracket popover stays untouched, so ``show_above`` / ``_apply_theme``
    are deliberate duplicates — keep them in sync by hand. The slider
    emits live during a drag (mpv's speed property is cheap); sync never
    yanks the handle out from under an in-progress drag.
    """

    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("SpeedPopover")
        # Rounded corners on a top-level popup need this, or the pixels
        # outside the radius are unpainted window buffer (mini-card pattern).
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._apply_theme(theming.manager().current())
        theming.manager().theme_changed.connect(self._apply_theme)

        self._display = QLabel(format_speed(1.0))
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setObjectName("SpeedPopoverDisplay")
        self._display.setStyleSheet("font-weight: 600;")

        self._slider = SpringSlider()
        self._slider.set_range(SPEED_MIN, SPEED_MAX, SPEED_STEP)
        self._slider.set_detents(SPEED_PRESETS)
        self._slider.set_formatter(format_speed)
        self._slider.setMinimumWidth(scale.px(220))
        self._slider.value_changed.connect(self._on_slider_changed)
        self._slider.value_committed.connect(self._on_slider_committed)

        self._minus_btn = BracketButton(f"−{SPEED_STEP:.2f}")
        self._plus_btn = BracketButton(f"+{SPEED_STEP:.2f}")
        self._minus_btn.clicked.connect(self._on_minus)
        self._plus_btn.clicked.connect(self._on_plus)

        slider_row = QHBoxLayout()
        slider_row.setSpacing(8)
        slider_row.addWidget(self._minus_btn)
        slider_row.addWidget(self._slider, stretch=1)
        slider_row.addWidget(self._plus_btn)

        self._reset_btn = BracketButton("reset")
        self._reset_btn.clicked.connect(lambda: self.speed_changed.emit(1.0))

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._display)
        root.addLayout(slider_row)
        root.addWidget(self._reset_btn, alignment=Qt.AlignRight)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._current: float = 1.0

    def sync(self, speed: float) -> None:
        self._current = float(speed)
        self._display.setText(format_speed(speed))
        self._minus_btn.setEnabled(speed > SPEED_MIN + 1e-4)
        self._plus_btn.setEnabled(speed < SPEED_MAX - 1e-4)
        # Move the handle only when the value really changed — during a
        # drag the slider originated this sync, and set_value would snap
        # the display out from under the finger. Pre-show sync snaps.
        if abs(self._slider.value() - float(speed)) > 1e-9:
            self._slider.set_value(float(speed), animate=self.isVisible())

    def show_above(self, anchor: QWidget) -> None:
        """Duplicate of SpeedPopover.show_above — see the class docstring."""
        self.adjustSize()
        anchor_top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        x = anchor_top_left.x() + (anchor.width() - self.width()) // 2
        y = anchor_top_left.y() - self.height() - 4
        screen = anchor.screen()
        if screen is not None:
            geom = screen.availableGeometry()
            if y < geom.top():
                y = anchor_top_left.y() + anchor.height() + 4
            x = max(geom.left() + 4, min(x, geom.right() - self.width() - 4))
        self.move(x, y)
        self.show()
        self.raise_()

    # ---------- internals ----------

    def _on_slider_changed(self, value: float) -> None:
        # Live: the logical value is already final (only the display
        # springs), so pushing it to the player now is honest.
        self._current = float(value)
        self._display.setText(format_speed(self._current))
        self.speed_changed.emit(self._current)

    def _on_slider_committed(self, value: float) -> None:
        # Live emits already delivered this value (set_speed dedupes),
        # but a release exactly where the drag began emits nothing live —
        # re-emit so the button's popover sync stays honest.
        self.speed_changed.emit(float(value))

    def _on_minus(self) -> None:
        self.speed_changed.emit(self._current - SPEED_STEP)

    def _on_plus(self) -> None:
        self.speed_changed.emit(self._current + SPEED_STEP)

    def _apply_theme(self, theme) -> None:
        # Modern face: the panel is painted (paintEvent) — a QSS background
        # on a translucent top-level never lands, which left the controls
        # floating bare over the backdrop. Only the repaint is needed here.
        self._theme = theme
        self.setStyleSheet("QFrame#SpeedPopover { background: transparent; border: 0; }")
        self.update()

    def paintEvent(self, ev) -> None:
        paint_popover_panel(self, getattr(self, "_theme", None))
        super().paintEvent(ev)
