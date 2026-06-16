"""Motion / animation system.

The single source of truth for animation durations, easing curves, and the
runtime intensity. Every animation in tide should go through one of the
helpers here so the feel stays coherent across surfaces and the user can
turn things up / down / off from one switch.

Design contract:
  * GUI thread only. Qt animation objects must be constructed + started on
    the thread that owns the target widget. Don't call any of this from a
    QRunnable or QThread.
  * Three intensity levels — ``OFF`` snaps every helper to end-state
    synchronously (no QObject created, no timer scheduled, ``on_done``
    fires immediately). ``LITE`` runs signature + everyday helpers. ``FULL``
    additionally enables atmospheric helpers (none ship in P1; intensity
    gates them at the call site in later phases).
  * Reduced-motion detection: env var ``QT_REDUCED_MOTION=1`` or Qt's
    ``QStyleHints.uiEffectsEnabled() == False`` clamps FULL to LITE. An
    explicit OFF is never overridden — the user always wins downward.
  * Idempotent helpers. Each helper takes (or implicitly uses) a target
    widget and registers its in-flight animation under
    ``target._motion_anims[kind]``. A subsequent call with the same
    ``kind`` cancels the prior animation cleanly; the prior's ``on_done``
    is NOT fired (the new caller has taken over). Lifecycle quirks
    specific to ``QGraphicsOpacityEffect`` (which leaves a side-effect
    on the widget) are handled too.
  * Stable callsite shape: ``helper(target, *required, kwargs)``. Every
    optional behavior is keyword-only. The return value is the animation
    handle (or ``None`` if OFF / no-op), in case the caller wants to wire
    additional signals.
"""
from __future__ import annotations

import os
import random
from enum import Enum
from typing import Callable, Optional

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QStackedWidget,
    QWidget,
)


# ---------- intensity ----------


class Intensity(str, Enum):
    OFF = "off"
    LITE = "lite"
    FULL = "full"

    @classmethod
    def parse(cls, value) -> "Intensity":
        """Best-effort parse from a string or existing Intensity. Falls back
        to LITE when input is unrecognized, since LITE is the safe default."""
        if isinstance(value, cls):
            return value
        if not value:
            return cls.LITE
        v = str(value).strip().lower()
        for it in cls:
            if it.value == v:
                return it
        return cls.LITE


# Module-cached intensity. Reads are cheap (no settings lookup per frame).
_user_intensity: Intensity = Intensity.LITE
_reduced_motion: bool = False
_initialized: bool = False


def initialize(value=Intensity.LITE) -> None:
    """One-time setup. Caches intensity and detects reduced-motion. Safe to
    call multiple times — re-runs detection so an env change between launches
    is picked up.
    """
    global _initialized
    set_intensity(value)
    _refresh_reduced_motion()
    _initialized = True


def set_intensity(value) -> None:
    """Push a new user-selected intensity. In-flight animations finish at
    their current target; only subsequent helper calls observe the new value.
    """
    global _user_intensity
    _user_intensity = Intensity.parse(value)


def intensity() -> Intensity:
    """Effective intensity after the reduced-motion clamp. FULL is downgraded
    to LITE if the user / system has signalled reduced-motion; an explicit
    OFF is never raised.
    """
    if _reduced_motion and _user_intensity == Intensity.FULL:
        return Intensity.LITE
    return _user_intensity


def reduced_motion() -> bool:
    """Whether the reduced-motion clamp is active. Surfaced for callers that
    want to gate their own decorative work (e.g. atmospheric tier)."""
    return _reduced_motion


def _refresh_reduced_motion() -> None:
    global _reduced_motion
    _reduced_motion = _detect_reduced_motion()


def _detect_reduced_motion() -> bool:
    # Explicit env opt-in — standard across many Linux tools.
    if os.environ.get("QT_REDUCED_MOTION") == "1":
        return True
    # Qt's style hint. When KDE / GNOME / a window manager has effects
    # disabled system-wide, this returns False — we honor that as a
    # reduced-motion signal.
    try:
        app = QGuiApplication.instance()
        if app is not None:
            hints = app.styleHints()
            if hints is not None and hasattr(hints, "uiEffectsEnabled"):
                if not hints.uiEffectsEnabled():
                    return True
    except Exception:
        pass
    return False


# ---------- standard durations + easings ----------


# Milliseconds. Chosen for a brutalist/mechanical feel — short and decisive.
# Anything longer drifts into "soft / decorative" territory.
DUR_MICRO = 120   # hover, focus, button micro
DUR_SHORT = 200   # toast, dialog fade, view crossfade, basic slides
DUR_MED = 350     # signature: album art crossfade, title scramble
DUR_LONG = 600    # rarely used directly — reserved for special atmospherics


EASE_LINEAR = QEasingCurve(QEasingCurve.Linear)
EASE_OUT_QUAD = QEasingCurve(QEasingCurve.OutQuad)
EASE_OUT_CUBIC = QEasingCurve(QEasingCurve.OutCubic)
EASE_IN_OUT_QUAD = QEasingCurve(QEasingCurve.InOutQuad)


# Default monospace-safe glyph pool for scramble_text. Mixes block fills
# with brutalist punctuation so the decode feels alive without straying
# into "soft" characters (no letters / digits — they'd look like a typo).
DEFAULT_SCRAMBLE_GLYPHS = "█▓▒░#@*/?+"


# ---------- lifecycle book-keeping ----------


def _anim_table(target: QObject) -> dict:
    table = getattr(target, "_motion_anims", None)
    if table is None:
        table = {}
        target._motion_anims = table
    return table


def _cancel_prior(target: Optional[QObject], kind: str) -> None:
    """If there's an in-flight animation of the same kind on ``target``,
    stop it. Also tears down any QGraphicsOpacityEffect we may have attached.
    """
    if target is None:
        return
    table = getattr(target, "_motion_anims", None)
    if table is None:
        return
    prior = table.pop(kind, None)
    if prior is not None:
        try:
            prior.stop()
        except Exception:
            pass
    eff_key = f"_motion_effect_{kind}"
    eff = getattr(target, eff_key, None)
    if eff is not None:
        try:
            # The effect was the widget's only effect — clearing it is safe.
            if isinstance(target, QWidget):
                target.setGraphicsEffect(None)
        except Exception:
            pass
        setattr(target, eff_key, None)


def _register(target: Optional[QObject], kind: str, anim) -> None:
    if target is None:
        return
    _anim_table(target)[kind] = anim


# ---------- fade ----------


def _read_current_opacity(target: QWidget) -> float:
    """Read the widget's current effective opacity. Lets fade_in / fade_out
    pick up gracefully if a prior fade was cancelled mid-flight rather than
    jumping back to 0 / 1."""
    eff = target.graphicsEffect()
    if isinstance(eff, QGraphicsOpacityEffect):
        return float(eff.opacity())
    return 1.0


def fade_in(
    widget: QWidget,
    *,
    dur: int = DUR_SHORT,
    easing: QEasingCurve = EASE_OUT_QUAD,
    on_done: Optional[Callable[[], None]] = None,
) -> Optional[QPropertyAnimation]:
    """Fade ``widget`` to fully opaque. Calls ``widget.show()`` first so the
    caller doesn't have to. Uses a ``QGraphicsOpacityEffect`` which is
    detached when the animation finishes (otherwise the widget pays a
    permanent offscreen-pixmap cost)."""
    _cancel_prior(widget, "fade")
    if intensity() == Intensity.OFF:
        widget.show()
        if on_done:
            on_done()
        return None
    start_opacity = _read_current_opacity(widget) if widget.graphicsEffect() else 0.0
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(start_opacity)
    widget.setGraphicsEffect(eff)
    widget._motion_effect_fade = eff
    widget.show()
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(dur)
    anim.setStartValue(start_opacity)
    anim.setEndValue(1.0)
    anim.setEasingCurve(easing)

    def _finish() -> None:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        widget._motion_effect_fade = None
        # Drop our registration so a subsequent fade isn't blocked by us.
        table = getattr(widget, "_motion_anims", None)
        if table is not None and table.get("fade") is anim:
            table.pop("fade", None)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _register(widget, "fade", anim)
    anim.start()
    return anim


def fade_out(
    widget: QWidget,
    *,
    dur: int = DUR_SHORT,
    easing: QEasingCurve = EASE_OUT_QUAD,
    hide_on_done: bool = True,
    on_done: Optional[Callable[[], None]] = None,
) -> Optional[QPropertyAnimation]:
    """Fade ``widget`` to transparent. ``hide_on_done`` hides it after the
    animation so it stops eating mouse events — set False if the caller is
    about to destroy the widget itself."""
    _cancel_prior(widget, "fade")
    if intensity() == Intensity.OFF:
        if hide_on_done:
            widget.hide()
        if on_done:
            on_done()
        return None
    start_opacity = _read_current_opacity(widget)
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(start_opacity)
    widget.setGraphicsEffect(eff)
    widget._motion_effect_fade = eff
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(dur)
    anim.setStartValue(start_opacity)
    anim.setEndValue(0.0)
    anim.setEasingCurve(easing)

    def _finish() -> None:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        widget._motion_effect_fade = None
        if hide_on_done:
            widget.hide()
        table = getattr(widget, "_motion_anims", None)
        if table is not None and table.get("fade") is anim:
            table.pop("fade", None)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _register(widget, "fade", anim)
    anim.start()
    return anim


# ---------- slide ----------


def slide(
    widget: QWidget,
    from_pos: QPoint,
    to_pos: QPoint,
    *,
    dur: int = DUR_SHORT,
    easing: QEasingCurve = EASE_OUT_QUAD,
    on_done: Optional[Callable[[], None]] = None,
) -> Optional[QPropertyAnimation]:
    """Animate ``widget.pos`` between two points. Caller is responsible for
    ensuring the widget's parent layout doesn't override the position
    (typically: call ``widget.move(from_pos)`` first or use absolute
    positioning)."""
    _cancel_prior(widget, "slide")
    if intensity() == Intensity.OFF:
        widget.move(to_pos)
        if on_done:
            on_done()
        return None
    anim = QPropertyAnimation(widget, b"pos", widget)
    anim.setDuration(dur)
    anim.setStartValue(from_pos)
    anim.setEndValue(to_pos)
    anim.setEasingCurve(easing)

    def _finish() -> None:
        table = getattr(widget, "_motion_anims", None)
        if table is not None and table.get("slide") is anim:
            table.pop("slide", None)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _register(widget, "slide", anim)
    anim.start()
    return anim


# ---------- color ----------


def color_lerp(
    start: QColor,
    end: QColor,
    *,
    on_update: Callable[[QColor], None],
    dur: int = DUR_MED,
    easing: QEasingCurve = EASE_OUT_CUBIC,
    on_done: Optional[Callable[[], None]] = None,
    owner: Optional[QObject] = None,
    kind: str = "color",
) -> Optional[QVariantAnimation]:
    """Interpolate from ``start`` to ``end`` QColor, calling ``on_update`` per
    frame with the interpolated value. ``owner`` is required for cancellation
    of a prior in-flight color animation of the same ``kind``; multiple
    color animations on the same owner can coexist by using distinct kinds
    (e.g. ``"color/accent"`` vs ``"color/bg_alt"``)."""
    _cancel_prior(owner, kind)
    if intensity() == Intensity.OFF:
        on_update(QColor(end))
        if on_done:
            on_done()
        return None
    anim = QVariantAnimation(owner)
    anim.setDuration(dur)
    anim.setStartValue(QColor(start))
    anim.setEndValue(QColor(end))
    anim.setEasingCurve(easing)

    def _frame(v) -> None:
        if isinstance(v, QColor):
            on_update(v)

    anim.valueChanged.connect(_frame)

    def _finish() -> None:
        if owner is not None:
            table = getattr(owner, "_motion_anims", None)
            if table is not None and table.get(kind) is anim:
                table.pop(kind, None)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _register(owner, kind, anim)
    anim.start()
    return anim


# ---------- pixmap crossfade ----------


def crossfade_pixmap(
    setter: Callable[[QPixmap], None],
    old_pixmap: Optional[QPixmap],
    new_pixmap: QPixmap,
    *,
    dur: int = DUR_MED,
    easing: QEasingCurve = EASE_OUT_QUAD,
    on_done: Optional[Callable[[], None]] = None,
    owner: Optional[QObject] = None,
) -> Optional[QVariantAnimation]:
    """Alpha-blend from ``old_pixmap`` to ``new_pixmap``, calling
    ``setter(frame)`` each tick with a freshly composited pixmap. Falls back
    to a clean swap when intensity is OFF, no old pixmap is provided, or
    either pixmap is null."""
    _cancel_prior(owner, "crossfade_pixmap")
    if (
        intensity() == Intensity.OFF
        or old_pixmap is None
        or old_pixmap.isNull()
        or new_pixmap.isNull()
    ):
        setter(new_pixmap)
        if on_done:
            on_done()
        return None
    # Match sizes so the blend lines up. Scale the older to the new size —
    # the new pixmap defines the final visual.
    if old_pixmap.size() != new_pixmap.size():
        old_pixmap = old_pixmap.scaled(
            new_pixmap.size(),
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )
    anim = QVariantAnimation(owner)
    anim.setDuration(dur)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(easing)

    def _frame(t) -> None:
        try:
            tf = float(t)
        except (TypeError, ValueError):
            return
        result = QPixmap(new_pixmap.size())
        result.fill(Qt.transparent)
        p = QPainter(result)
        try:
            p.setOpacity(1.0 - tf)
            p.drawPixmap(0, 0, old_pixmap)
            p.setOpacity(tf)
            p.drawPixmap(0, 0, new_pixmap)
        finally:
            p.end()
        setter(result)

    anim.valueChanged.connect(_frame)

    def _finish() -> None:
        # Force the final frame to the exact new pixmap (avoids any rounding
        # residue from compositing).
        setter(new_pixmap)
        if owner is not None:
            table = getattr(owner, "_motion_anims", None)
            if table is not None and table.get("crossfade_pixmap") is anim:
                table.pop("crossfade_pixmap", None)
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    _register(owner, "crossfade_pixmap", anim)
    anim.start()
    return anim


# ---------- stacked-widget crossfade ----------


def crossfade_stack(
    stack: QStackedWidget,
    target_idx: int,
    *,
    dur: int = DUR_SHORT,
    easing: QEasingCurve = EASE_OUT_QUAD,
    on_done: Optional[Callable[[], None]] = None,
) -> Optional[QPropertyAnimation]:
    """Crossfade between two pages of a ``QStackedWidget``. The current page
    is grabbed into a snapshot pixmap, the stack switches to the target
    page, and the snapshot fades out over it. Cheap (one snapshot, one
    opacity animation) and works for any QStackedWidget children — no
    requirement that pages implement a paint-friendly base class.
    """
    if stack.currentIndex() == target_idx:
        if on_done:
            on_done()
        return None
    if intensity() == Intensity.OFF:
        stack.setCurrentIndex(target_idx)
        if on_done:
            on_done()
        return None
    current = stack.currentWidget()
    target = stack.widget(target_idx)
    if current is None or target is None:
        stack.setCurrentIndex(target_idx)
        if on_done:
            on_done()
        return None
    snap = current.grab()
    stack.setCurrentIndex(target_idx)
    overlay = QLabel(target)
    overlay.setPixmap(snap)
    overlay.setGeometry(target.rect())
    overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    overlay.show()
    overlay.raise_()
    eff = QGraphicsOpacityEffect(overlay)
    eff.setOpacity(1.0)
    overlay.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", overlay)
    anim.setDuration(dur)
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(easing)

    def _finish() -> None:
        overlay.hide()
        overlay.deleteLater()
        if on_done:
            on_done()

    anim.finished.connect(_finish)
    anim.start()
    return anim


# ---------- text scramble ----------


class _ScrambleAnim(QObject):
    """Drives a text-scramble decode via a QTimer.

    The target text's characters are revealed left-to-right on a linear
    stagger over the first 75% of the duration; the remaining 25% is just
    settling time. Non-revealed positions show a random glyph from the
    pool, re-rolled every tick so the unsolved text feels alive. Whitespace
    is preserved (never scrambled) — keeps word boundaries readable.
    """

    _TICK_MS = 16  # ~60 fps

    def __init__(
        self,
        setter: Callable[[str], None],
        target_text: str,
        dur: int,
        glyphs: str,
        on_done: Optional[Callable[[], None]],
        parent: Optional[QObject],
    ) -> None:
        super().__init__(parent)
        self._setter = setter
        self._target = target_text
        self._glyphs = glyphs or DEFAULT_SCRAMBLE_GLYPHS
        self._on_done = on_done
        self._dur = max(int(dur), self._TICK_MS)
        self._t = 0
        n = len(target_text)
        if n == 0:
            self._reveal_at: list[int] = []
        else:
            # Linear stagger across 75% of the duration. The +1 guarantees
            # the last character only reveals at 0.75·dur, never at 0.
            scaled = self._dur * 0.75
            self._reveal_at = [int((i + 1) * scaled / n) for i in range(n)]
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)

    # --- public API expected by _cancel_prior ---

    def start(self) -> None:
        if not self._target:
            self._setter("")
            if self._on_done:
                self._on_done()
            self.deleteLater()
            return
        # Paint frame 0 immediately so there's no flicker between start and
        # the first timer tick.
        self._paint(0)
        self._t = 0
        self._timer.start()

    def stop(self) -> None:
        """Cancel mid-flight. Does NOT call on_done — caller superseded."""
        self._timer.stop()
        self.deleteLater()

    # --- internals ---

    def _tick(self) -> None:
        self._t += self._TICK_MS
        if self._t >= self._dur:
            self._timer.stop()
            self._setter(self._target)
            if self._on_done:
                self._on_done()
            self.deleteLater()
            return
        self._paint(self._t)

    def _paint(self, t_ms: int) -> None:
        chars = []
        for i, ch in enumerate(self._target):
            if ch.isspace() or t_ms >= self._reveal_at[i]:
                chars.append(ch)
            else:
                chars.append(random.choice(self._glyphs))
        self._setter("".join(chars))


def scramble_text(
    setter: Callable[[str], None],
    new_text: str,
    *,
    dur: int = DUR_MED,
    glyphs: str = DEFAULT_SCRAMBLE_GLYPHS,
    on_done: Optional[Callable[[], None]] = None,
    owner: Optional[QObject] = None,
    kind: str = "scramble",
) -> Optional[_ScrambleAnim]:
    """Reveal ``new_text`` character by character with random glyphs filling
    the unrevealed positions. ``setter`` receives the current frame's text
    string (length == len(new_text)). Designed for single-line monospace
    labels — passing a multi-line target works but column alignment isn't
    preserved across the decode.

    ``kind`` distinguishes co-existing scrambles on the same owner — pass
    ``"scramble/title"``, ``"scramble/artist"``, etc. so concurrent scrambles
    don't cancel each other.
    """
    _cancel_prior(owner, kind)
    if intensity() == Intensity.OFF or not new_text:
        setter(new_text)
        if on_done:
            on_done()
        return None
    anim = _ScrambleAnim(setter, new_text, dur, glyphs, on_done, owner)
    _register(owner, kind, anim)
    anim.start()
    return anim
