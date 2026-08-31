"""SpringSlider — the modern personality's signature value control.

A horizontal slider with magnetic detents and a springy settle. The
handle follows the pointer during a drag (unquantized, so it feels
connected to the finger), sticks to detents when it gets close, and on
release springs to the committed value. Click-jumps and keyboard/wheel
nudges settle the same way. The whole thing degrades correctly for the
brutalist personality: at motion OFF every settle is a synchronous snap
— no animation object is ever created.

Contracts kept here:
  * Two signals. ``value_changed(float)`` fires live during interaction
    whenever the logical value changes; ``value_committed(float)`` fires
    once when the interaction lands (release / keyboard / wheel settle
    completing). While there IS a settle (motion LITE/FULL) rapid
    re-nudges retarget it, so a burst of wheel ticks coalesces into ONE
    commit with the final value — but at motion OFF the settle window
    doesn't exist and every notch commits. A consumer whose commit
    handler is expensive must therefore carry its own rate limit and
    never assume coalescing (the fx rack does: audio_fx_view's
    ``_commit_fx_debounce`` shortens the debounce instead of pushing).
    ``value_committed`` always fires on release even if the drag ended
    where it began — consumers use it to flush pending debounce.
  * Programmatic ``set_value`` never emits; only the user does.
  * Painting is pure QPainter reading theming tokens AT PAINT TIME from
    ``theming.manager().current_effective()`` with hex fallbacks — no
    QSS, no cached palette going stale under the adaptive driver. The
    per-frame path is ``update()`` only (never tokens/QSS: theme_changed
    is a bus, not a frame clock).
  * All pixel sizes route through ``ui.scale.px`` and are recomputed per
    paint, so a scale flip lands on the next repaint.
  * The settle animation is a child of the widget (dies with it — no
    timer outlives destruction) and the finish handler is guarded
    against superseding retargets and mid-teardown delivery.

Motion integration: the settle rides ``motion.spring_settle`` — the
profile system picks the character (mechanical: decisive, no bounce;
springy: OutBack overshoot at intensity FULL) and OFF is handled by
construction: the helper snaps synchronously and never creates an
animation object.
"""
from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import (
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import theming
from . import motion, scale


# Magnet reach around each detent, in (unscaled) pixels along the groove.
# Converted to value-space per interaction so it stays ~6 physical px no
# matter the range or widget width.
DETENT_SNAP_PX = 6


def _tok(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))


class SpringSlider(QWidget):
    """Horizontal slider: magnetic detents, spring settle, motion-gated."""

    value_changed = Signal(float)     # live during drag / nudge
    value_committed = Signal(float)   # interaction landed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lo = 0.0
        self._hi = 100.0
        self._step = 1.0
        self._detents: list[float] = []
        self._value = 0.0
        self._display = 0.0          # visual handle position (value-space)
        self._formatter: Callable[[float], str] = lambda v: f"{v:g}"
        self._drag = False
        self._settle = None                 # in-flight QVariantAnimation
        self._settle_token: object = object()
        self._wheel_accum = 0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        # No setMinimumSize: minimumSizeHint carries the floor, so a host
        # that wants a wider groove (the speed popover's setMinimumWidth)
        # isn't clobbered the next time the theme ticks.
        self._metrics = self._metrics_key()
        theming.manager().theme_changed.connect(self._on_theme)

    # ------------------------------------------------------------------ API

    def set_range(self, lo: float, hi: float, step: float) -> None:
        lo, hi = float(lo), float(hi)
        if hi < lo:
            lo, hi = hi, lo
        self._lo, self._hi = lo, hi
        self._step = max(0.0, float(step))
        self._value = self._quantize(self._value)
        self._cancel_settle()
        self._display = self._value
        self.update()

    def set_detents(self, values: Iterable[float]) -> None:
        self._detents = sorted(float(v) for v in values)
        self.update()

    def value(self) -> float:
        return self._value

    def set_value(self, v: float, animate: bool = False) -> None:
        """Programmatic set — quantized, clamped, NO signals. ``animate``
        settles the handle springily (still snaps at motion OFF)."""
        q = self._quantize(v)
        self._value = q
        if animate:
            self._settle_to(q, commit=False)
        else:
            self._cancel_settle()
            self._display = q
            self.update()

    def set_formatter(self, fn: Callable[[float], str]) -> None:
        self._formatter = fn
        self.update()

    def _set_display(self, v: float) -> None:
        """Per-frame target for the settle animation — local repaint only
        (rule 7: animations never touch tokens/QSS)."""
        self._display = float(v)
        self.update()

    # ------------------------------------------------------------ interaction

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        ev.accept()
        self.setFocus(Qt.MouseFocusReason)
        self._drag = True
        v, _disp = self._pos_for_x(ev.position().x())
        self._apply_value(v)
        # Click-jump: spring the handle toward the press point. A drag
        # takes over direct tracking on the first move.
        self._settle_to(v, commit=False)

    def mouseMoveEvent(self, ev) -> None:
        if not self._drag:
            super().mouseMoveEvent(ev)
            return
        ev.accept()
        self._cancel_settle()   # the finger owns the handle now
        v, disp = self._pos_for_x(ev.position().x())
        self._apply_value(v)
        if disp != self._display:
            self._display = disp
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton or not self._drag:
            super().mouseReleaseEvent(ev)
            return
        ev.accept()
        self._drag = False
        self._settle_to(self._value, commit=True)

    def wheelEvent(self, ev) -> None:
        # Only swallow the notch when it actually moved the value. These
        # sliders live inside the fx rack's QScrollArea; accepting a
        # dead notch (sub-notch delta, or the handle already at the rail
        # end) would trap the page's scroll under the pointer — QSlider
        # ignores those too.
        self._wheel_accum += ev.angleDelta().y()
        steps = int(self._wheel_accum / 120)
        if steps == 0:
            ev.ignore()
            return
        self._wheel_accum -= steps * 120
        if self._nudge(steps):
            ev.accept()
        else:
            ev.ignore()

    def keyPressEvent(self, ev) -> None:
        key = ev.key()
        if key in (Qt.Key_Left, Qt.Key_Down):
            self._nudge(-1)
        elif key in (Qt.Key_Right, Qt.Key_Up):
            self._nudge(+1)
        elif key == Qt.Key_PageDown:
            self._nudge(-5)
        elif key == Qt.Key_PageUp:
            self._nudge(+5)
        elif key == Qt.Key_Home:
            self._interact_to(self._lo)
        elif key == Qt.Key_End:
            self._interact_to(self._hi)
        else:
            super().keyPressEvent(ev)
            return
        ev.accept()

    def _nudge(self, steps: int) -> bool:
        step = self._step if self._step > 0 else (self._hi - self._lo) / 20.0
        return self._interact_to(self._value + steps * step)

    def _interact_to(self, target: float) -> bool:
        """Keyboard / wheel path: quantize, emit live change, settle with
        commit. A no-op nudge (already at the bound) emits nothing and
        reports False, so the wheel can hand the event back."""
        q = self._quantize(target)
        if not self._apply_value(q):
            return False
        self._settle_to(q, commit=True)
        return True

    def _apply_value(self, v: float) -> bool:
        v = float(v)
        if abs(v - self._value) < 1e-9:
            return False
        self._value = v
        self.update()
        self.value_changed.emit(v)
        return True

    # ------------------------------------------------------------ settle

    def _cancel_settle(self) -> None:
        """Drop any in-flight settle WITHOUT its commit — the finger (or a
        programmatic snap) has taken over. Token first, then stop: even if
        Qt emitted finished from the stop, the stale on_done stands down."""
        self._settle_token = object()
        anim, self._settle = self._settle, None
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                pass

    def _settle_to(self, target: float, *, commit: bool) -> None:
        """Spring the handle to ``target`` (value-space) via
        ``motion.spring_settle`` — the profile picks the curve (mechanical
        vs springy overshoot), OFF snaps synchronously by construction.
        The commit (if any) fires when the settle lands; a newer settle
        supersedes an older one and takes its commit with it."""
        target = float(target)
        self._settle_token = token = object()

        def _land() -> None:
            if self._settle_token is not token:
                return   # superseded — the newer settle owns the commit
            self._settle = None
            try:
                self._display = target
                self.update()
                if commit:
                    self.value_committed.emit(self._value)
            except RuntimeError:
                pass     # widget torn down mid-delivery

        if abs(self._display - target) < 1e-6:
            # Zero-length trip (release right on a magnet) — nothing to
            # animate; land synchronously so the commit isn't delayed.
            self._cancel_settle()
            self._settle_token = token
            _land()
            return
        self._settle = motion.spring_settle(
            self._display,
            target,
            on_update=self._set_display,
            on_done=_land,
            owner=self,
            kind="spring/settle",
        )

    # ------------------------------------------------------------ mapping

    def _span(self) -> float:
        return max(self._hi - self._lo, 1e-9)

    def _quantize(self, v: float) -> float:
        v = min(self._hi, max(self._lo, float(v)))
        if self._step > 0:
            n = round((v - self._lo) / self._step)
            v = min(self._hi, max(self._lo, self._lo + n * self._step))
        return round(v, 9)

    def _handle_r(self) -> int:
        return scale.px(7)

    def _bubble_h(self) -> int:
        return QFontMetrics(self.font()).height() + scale.px(4)

    def _bubble_gap(self) -> int:
        return scale.px(4)

    def _groove_cy(self) -> float:
        """Vertical centre of the groove.

        The handle disc and the detent ticks own the bottom band and the
        bubble floats above them, so the groove sits exactly one bubble +
        gap + radius down from the top when the host gives us the height
        we asked for (``sizeHint``). Hosts that pin us shorter (the 26px
        volume seat) push the groove as low as the disc allows and simply
        don't get a bubble — see ``_bubble_rect``. Anything is better than
        painting the readout over the ticks it's meant to annotate."""
        hr = float(self._handle_r())
        roomy = float(self._bubble_h() + self._bubble_gap()) + hr
        cramped = max(hr, float(self.height()) - hr - float(scale.px(2)))
        return min(roomy, cramped)

    def _groove_rect(self) -> QRectF:
        m = float(self._handle_r() + scale.px(2))
        gh = float(scale.px(4, minimum=2))
        cy = self._groove_cy()
        return QRectF(m, cy - gh / 2.0, max(1.0, self.width() - 2 * m), gh)

    def _x_for_value(self, v: float) -> float:
        g = self._groove_rect()
        frac = (float(v) - self._lo) / self._span()
        return g.left() + min(1.0, max(0.0, frac)) * g.width()

    def _value_at_x(self, x: float) -> float:
        g = self._groove_rect()
        frac = (float(x) - g.left()) / max(g.width(), 1.0)
        return self._lo + min(1.0, max(0.0, frac)) * self._span()

    def _magnet(self, raw: float) -> float | None:
        """Nearest in-range detent within the snap radius, else None. The
        radius is DETENT_SNAP_PX physical px converted to value-space."""
        g = self._groove_rect()
        if g.width() <= 0:
            return None
        best: float | None = None
        best_d = scale.px(DETENT_SNAP_PX) * self._span() / g.width()
        for d in self._detents:
            if d < self._lo - 1e-9 or d > self._hi + 1e-9:
                continue
            dist = abs(raw - d)
            if dist <= best_d:
                best, best_d = d, dist
        return best

    def _pos_for_x(self, x: float) -> tuple[float, float]:
        """(logical value, display position) for a pointer x. A magnet hit
        returns the detent for both — the handle visibly sticks. Otherwise
        the value is step-quantized while the display follows the finger."""
        raw = self._value_at_x(x)
        d = self._magnet(raw)
        if d is not None:
            return d, d
        return self._quantize(raw), raw

    # ------------------------------------------------------------ painting

    def _bubble_text(self) -> str:
        try:
            return str(self._formatter(self._value))
        except Exception:
            return f"{self._value:g}"

    def _bubble_rect(self, text: str) -> QRectF | None:
        """Where the value bubble goes, or None when this widget is too
        short to float one clear of the handle and ticks. Never clamped
        into the handle band: a readout that covers what it annotates is
        worse than no readout (the volume seat is 26px and relies on
        this)."""
        fm = QFontMetrics(self.font())
        bw = float(fm.horizontalAdvance(text) + scale.px(10))
        bh = float(self._bubble_h())
        hx = self._x_for_value(self._display)
        top = self._groove_cy() - self._handle_r() - self._bubble_gap() - bh
        if top < 0.0:
            return None
        bx = min(max(0.0, hx - bw / 2.0), max(0.0, self.width() - bw))
        return QRectF(bx, top, bw, bh)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        theme = theming.manager().current_effective()
        accent = _tok(theme, "accent", "#d4b95e")
        fg = _tok(theme, "fg", "#e6e6e6")
        dim = _tok(theme, "dim", "#666666")
        track = _tok(theme, "border_dim", "#2a2a2a")
        bg_alt = _tok(theme, "bg_alt", "#141414")
        if not self.isEnabled():
            accent = dim
            fg = dim

        g = self._groove_rect()
        radius = g.height() / 2.0
        hx = self._x_for_value(self._display)
        hy = g.center().y()
        hr = float(self._handle_r())

        # track + fill up to the handle
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(g, radius, radius)
        fill_w = hx - g.left()
        if fill_w > 1.0:
            p.setBrush(accent)
            p.drawRoundedRect(
                QRectF(g.left(), g.top(), fill_w, g.height()), radius, radius)

        # detent ticks — subtle marks so the magnets are visible; the one
        # the value is sitting on picks up the accent.
        tick_h = float(scale.px(4, minimum=3))
        for d in self._detents:
            if d < self._lo - 1e-9 or d > self._hi + 1e-9:
                continue
            dx = self._x_for_value(d)
            on_it = abs(d - self._value) < 1e-9
            p.setPen(QPen(accent if on_it else dim, max(1.0, scale.px(1))))
            p.drawLine(QPointF(dx, g.top() - tick_h - 1.0),
                       QPointF(dx, g.top() - 1.0))

        # handle — accent disc with a bg ring so it reads over the fill
        p.setPen(QPen(bg_alt, max(1.0, float(scale.px(2)))))
        p.setBrush(accent)
        p.drawEllipse(QPointF(hx, hy), hr, hr)

        # value bubble while the user is interacting / the settle is live
        if (self._drag or self._settle is not None) and self.isEnabled():
            text = self._bubble_text()
            bubble = self._bubble_rect(text)
            if bubble is not None:
                p.setPen(QPen(track, 1.0))
                p.setBrush(bg_alt)
                p.drawRoundedRect(bubble, scale.px(3), scale.px(3))
                p.setPen(fg)
                p.drawText(bubble, Qt.AlignCenter, text)

    # ------------------------------------------------------------ plumbing

    def _metrics_key(self) -> tuple:
        """The inputs sizeHint is derived from. Cheap to compare, so a
        theme tick can tell "same size, just repaint" from a real
        scale/font flip."""
        return (scale.px(100), QFontMetrics(self.font()).height())

    def _on_theme(self, _theme) -> None:
        # Tokens are re-read at paint time, so a theme tick is JUST a
        # repaint. theme_changed is a ~10 Hz bus under the adaptive
        # driver and updateGeometry() invalidates the whole parent layout
        # chain — with a rack full of these that was ~80 layout
        # invalidations a second while the art palette glided. Geometry
        # re-derives only when the metrics behind sizeHint really moved
        # (a scale flip or a font swap).
        key = self._metrics_key()
        if key != self._metrics:
            self._metrics = key
            self.updateGeometry()
        self.update()

    def _preferred_h(self) -> int:
        # Tall enough for the value bubble to clear the handle disc and
        # the tick band — the bubble is the control's signature readout,
        # and _bubble_rect drops it entirely rather than overlap.
        return max(
            scale.px(34),
            self._bubble_h() + self._bubble_gap() + 2 * self._handle_r()
            + scale.px(2),
        )

    def sizeHint(self) -> QSize:
        return QSize(scale.px(220), self._preferred_h())

    def minimumSizeHint(self) -> QSize:
        return QSize(scale.px(80), scale.px(24))
