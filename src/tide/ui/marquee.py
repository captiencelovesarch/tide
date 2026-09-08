"""Marquee: the scroll that rescues a title too long for its box.

A title that overran its box used to become ``i'll take care of you
(feat. y…`` and stay that way forever — the part you wanted was the part
that got cut. Marquee gives the overflow somewhere to go: the text
drifts left until its tail clears, holds, drifts back, holds, repeats.

Contracts:
  * Motion-gated, and the gate is the whole feature at OFF. At motion
    OFF — or under the reduced-motion clamp, which is exactly the signal
    that says "stop moving things at me" — nothing scrolls, no timer is
    ever created, and ``offset()`` is a flat 0. The host elides as it
    always did and the tooltip carries the rest.
  * Pull, don't push. The host asks for ``offset()`` inside its own
    paintEvent; the Marquee never repaints anybody. It emits ``tick``
    and the host decides what that's worth. One QTimer per marquee,
    stopped the moment the text fits, the host hides, or motion goes
    OFF — a fitting title costs nothing at all.
  * ``set_metrics`` is idempotent. Re-setting the same
    values never restarts the cycle, which is what lets a per-frame
    scramble write its target repeatedly without pinning the scroll at
    frame zero forever.
  * Geometry in, geometry out: the Marquee is told pixel widths and
    knows nothing about fonts, themes or QPainter. That keeps it usable
    from a custom paintEvent (the now-playing strip) and from a plain
    QLabel host alike.
"""
from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QObject, QRect, Qt, QTimer, Signal

from . import motion


# Scroll speed in logical px/sec, and the pause at each end. Slow enough
# to read a title at a glance rather than chase it; the dwell is what
# makes it readable at all — a marquee that turns around the instant it
# lands never shows you the end you were waiting for.
SPEED_PX_PER_S = 34.0
DWELL_MS = 1400
# Repaint cadence while scrolling. 30 Hz: the motion is a slow slide, and
# this rides the same "no per-frame layout" rule the SpringSlider settle
# does — a tick is a repaint, never a relayout.
TICK_MS = 33
# Overflow under this many px isn't worth animating — the tail is
# essentially visible already and the drift just reads as a wobble.
MIN_OVERFLOW_PX = 6.0


class Marquee(QObject):
    """Ping-pong scroll state for one run of text in one box.

    The host owns the pixels. It calls :meth:`set_metrics` with the
    full (unelided) text width and the width actually available, paints
    at ``-offset()`` when :meth:`active` and elides normally when not.
    """

    tick = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._text_w = 0.0
        self._avail_w = 0.0
        self._offset = 0.0
        self._timer: QTimer | None = None
        self._clock = QElapsedTimer()

    # ---------------------------------------------------------------- API

    def set_metrics(self, text_w: float, avail_w: float) -> None:
        """Push the measured full-text width and the box it has to fit.

        Idempotent: identical numbers leave the cycle untouched, so a
        host re-measuring on every paint (they all do) can call this
        unconditionally without ever resetting the scroll.
        """
        text_w = max(0.0, float(text_w))
        avail_w = max(0.0, float(avail_w))
        if text_w == self._text_w and avail_w == self._avail_w:
            self._sync_timer()
            return
        self._text_w = text_w
        self._avail_w = avail_w
        # New geometry is a new run of text as far as the eye is
        # concerned — restart from the head so the title's opening words
        # are what you see first.
        self._offset = 0.0
        self._clock.restart()
        self._sync_timer()

    def overflow(self) -> float:
        """How many px of text don't fit. 0 when it fits."""
        return max(0.0, self._text_w - self._avail_w)

    def active(self) -> bool:
        """True when the host should paint scrolled rather than elided."""
        return self._timer is not None

    def offset(self) -> float:
        """Current leftward shift in px. Always 0 when inactive."""
        return self._offset if self._timer is not None else 0.0

    def stop(self) -> None:
        """Park the scroll (host hidden, track cleared). Cheap to repeat."""
        self._kill_timer()
        self._offset = 0.0

    # ----------------------------------------------------------- internals

    def _wants_scroll(self) -> bool:
        if self.overflow() < MIN_OVERFLOW_PX:
            return False
        # reduced-motion is not merely a FULL→LITE clamp here: a marquee
        # runs forever, so it's the one thing the signal most clearly
        # asks us to drop. OFF and reduced both fall back to eliding.
        if motion.reduced_motion():
            return False
        return motion.intensity() != motion.Intensity.OFF

    def _sync_timer(self) -> None:
        if self._wants_scroll():
            self._ensure_timer()
        else:
            self.stop()

    def _ensure_timer(self) -> None:
        if self._timer is not None:
            return
        t = QTimer(self)
        t.setInterval(TICK_MS)
        t.timeout.connect(self._on_tick)
        self._timer = t
        self._clock.restart()
        t.start()

    def _kill_timer(self) -> None:
        if self._timer is None:
            return
        self._timer.stop()
        self._timer.deleteLater()
        self._timer = None

    def _on_tick(self) -> None:
        """Advance the ping-pong and ask the host to repaint.

        The phase is derived from the elapsed clock rather than
        accumulated per tick, so a dropped frame (or a host that was
        hidden for a while) resumes at the right place instead of
        drifting slowly out of sync with the dwell.
        """
        if not self._wants_scroll():
            # Motion was turned off, or the box grew enough to fit the
            # text, since the timer started.
            self.stop()
            self.tick.emit()
            return
        span = self.overflow()
        travel_ms = max(1.0, span / SPEED_PX_PER_S * 1000.0)
        cycle = 2.0 * (travel_ms + DWELL_MS)
        t = float(self._clock.elapsed()) % cycle
        if t < DWELL_MS:                                  # hold at the head
            offset = 0.0
        elif t < DWELL_MS + travel_ms:                    # drift out
            offset = span * (t - DWELL_MS) / travel_ms
        elif t < 2.0 * DWELL_MS + travel_ms:              # hold at the tail
            offset = span
        else:                                             # drift back
            back = t - (2.0 * DWELL_MS + travel_ms)
            offset = span * (1.0 - back / travel_ms)
        offset = min(span, max(0.0, offset))
        if abs(offset - self._offset) < 0.5:
            # Sub-pixel move — not worth a repaint of the whole strip.
            return
        self._offset = offset
        self.tick.emit()


def draw_text(painter, rect, text: str, fm, mq: Marquee, *,
              flags=Qt.AlignVCenter | Qt.AlignLeft) -> None:
    """Paint ``text`` into ``rect``, scrolling it when it overflows.

    The one call sites should use — it keeps the measure, the marquee
    hand-off and the two paint paths in one place so a host can't
    accidentally scroll text it never measured.

    While scrolling, alignment collapses to left regardless of ``flags``:
    a centred run of text that is also sliding is impossible to read, and
    the centred variants look right anyway because the text fills the box
    by definition once it's overflowing.
    """
    text_w = float(fm.horizontalAdvance(text))
    mq.set_metrics(text_w, float(rect.width()))
    if not mq.active():
        painter.drawText(
            rect, flags, fm.elidedText(text, Qt.ElideRight, rect.width()))
        return
    painter.save()
    painter.setClipRect(rect)
    painter.translate(-mq.offset(), 0.0)
    painter.drawText(
        QRect(rect.x(), rect.y(), int(text_w) + 2, rect.height()),
        Qt.AlignVCenter | Qt.AlignLeft, text)
    painter.restore()
