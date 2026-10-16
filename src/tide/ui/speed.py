"""Playback-speed UI.

A bracket-styled toggle that displays the current playback speed (e.g.
``[1.25×]``). Click it and the pill grows a speech bubble
(``ui/speed_bubble.py``, 2.3): the value big, a detent slider between
− and +, "reset" in the corner, the pill itself as the bubble's stem.
Right-click the button to reset to 1.0×.

One bubble serves both personalities — it reads the theme's aesthetic
at paint time (square + bracketed in brutalist, round in modern), so a
flip restyles it in place instead of rebuilding. The SpeedButton is the
authoritative store and syncs the bubble after each change so the
displayed value never drifts.

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
from PySide6.QtWidgets import QFrame, QWidget

from .widgets import BracketButton

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

    def _open_popover(self) -> None:
        if self._popover is None:
            # Parented to the window so it floats above the bar without
            # inheriting the button's layout constraints.
            from .speed_bubble import SpeedBubble
            self._popover = SpeedBubble(self.window())
            self._popover.speed_changed.connect(self.set_speed)
        self._popover.sync(self._speed)
        self._popover.show_above(self)
