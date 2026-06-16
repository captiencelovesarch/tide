"""Status-bar loading indicator.

Renders progress for the resolve-then-buffer phase that happens between
clicking a track and audio starting. There's no real progress signal — the
resolve worker is a blocking network call and mpv doesn't report buffer
percent reliably across stream backends — so the bar follows an asymptotic
elapsed-time curve (1 − e^(−t/τ)) capped at 95 % until the caller explicitly
``finish()``es it. The user sees the bar fill quickly at first and slow as it
approaches the end, which matches the felt experience of "almost there".

Styles available: ``off``, ``numbers``, ``blocks``, ``dots``, ``ascii``.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QObject, QTimer, Signal


# Bar width — 10 cells = each cell is one decile, easy to read at a glance.
_BAR_CELLS = 10
# Asymptote time constant. With τ=1.5s the bar is ~50% at 1s, ~75% at 2s,
# ~90% at 3s. Tuned against typical ytmusic resolve times.
_TIME_CONSTANT_S = 1.5
# Tick period — fast enough to feel smooth, slow enough not to spam paints.
_TICK_MS = 80

# (filled, empty) glyphs for each bar style.
_BAR_GLYPHS: dict[str, tuple[str, str]] = {
    "blocks": ("█", "░"),
    "dots":   ("●", "○"),
    "ascii":  ("#", "-"),
}

VALID_STYLES = ("off", "numbers", "blocks", "dots", "ascii")


def _render(style: str, msg: str, progress: float) -> str:
    """Compose the status-bar string for a given style and progress (0..1)."""
    if style == "off":
        return msg
    if style == "numbers":
        return f"{msg}  {int(progress * 100):d}%"
    filled, empty = _BAR_GLYPHS.get(style, _BAR_GLYPHS["blocks"])
    n = int(round(progress * _BAR_CELLS))
    n = max(0, min(_BAR_CELLS, n))
    bar = filled * n + empty * (_BAR_CELLS - n)
    if style == "ascii":
        return f"{msg}  [{bar}]"
    return f"{msg}  {bar}"


class LoadingIndicator(QObject):
    """Drives a progress string into a Qt status bar.

    Caller wires ``updated`` → ``statusBar().showMessage`` and toggles
    ``start()`` / ``update_message()`` / ``finish()`` / ``cancel()``.
    """

    updated = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._start_t: float = 0.0
        self._msg: str = ""
        self._style: str = "blocks"
        self._progress: float = 0.0
        self._active: bool = False

    def set_style(self, style: str) -> None:
        self._style = style if style in VALID_STYLES else "blocks"
        if self._active:
            self._emit()

    def start(self, msg: str) -> None:
        self._msg = msg
        self._start_t = time.monotonic()
        self._progress = 0.0
        self._active = True
        self._emit()
        self._timer.start()

    def update_message(self, msg: str) -> None:
        if not self._active:
            return
        self._msg = msg
        self._emit()

    def finish(self, final_msg: str) -> None:
        was_active = self._active
        self._timer.stop()
        self._active = False
        if was_active:
            self.updated.emit(final_msg)

    def cancel(self) -> None:
        self._timer.stop()
        self._active = False

    def is_active(self) -> bool:
        return self._active

    # ---------- internals ----------

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._start_t
        # 1 − e^(−t/τ), but capped at 95% so the bar visibly waits on real
        # completion rather than claiming we're done.
        self._progress = min(1.0 - math.exp(-elapsed / _TIME_CONSTANT_S), 0.95)
        self._emit()

    def _emit(self) -> None:
        self.updated.emit(_render(self._style, self._msg, self._progress))
