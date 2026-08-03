"""Widget variants — the things layouts choose between.

Each slot in the now-playing strip has a default ``v1`` choice (re-export
of the existing widget in ``widgets.py``) and one or more alternative
implementations alongside. A factory per slot returns a concrete widget
based on a slug, all exposing the same minimum surface so the window
doesn't care which it got.

Slot factories:
    make_progress(slug)          → progress widget
    make_volume(slug)            → volume widget
    make_album_art(slug, size)   → album art widget
    make_controls(slug)          → ControlsBundle (prev / play / next / like)
    make_now_label(slug)         → label widget

Adding a new variant: subclass an existing widget (or write fresh), make
sure it exposes the right surface, and register the slug in the matching
factory below.
"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import (
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QWidget,
)

from .. import theming
from .widgets import (
    AlbumArt,
    BracketButton,
    MonoProgress,
    MonoVolume,
    NowPlayingLabel,
)


def _qcolor(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))


# ============================================================================
# PROGRESS
# ============================================================================


class BarProgress(QWidget):
    """Smooth filled rectangle — accent fill on dim track."""

    seek_requested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self._theme = theming.manager().current()
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setDuration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self._enabled = self._duration > 0
        self.update()

    def setPosition(self, seconds: float) -> None:
        self._position = max(0.0, min(seconds, self._duration or seconds))
        self.update()

    def reset(self) -> None:
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self.update()

    def mousePressEvent(self, ev) -> None:
        if not self._enabled or self._duration <= 0 or self.width() <= 0:
            return
        frac = max(0.0, min(1.0, ev.position().x() / self.width()))
        self.seek_requested.emit(frac * self._duration)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        accent = _qcolor(self._theme, "accent", "#d4b95e")
        dim = _qcolor(self._theme, "border_dim", "#2a2a2a")
        radius = self.height() / 2.0
        rect = self.rect()
        # track
        p.setPen(Qt.NoPen)
        p.setBrush(dim)
        p.drawRoundedRect(rect, radius, radius)
        if self._duration > 0:
            frac = self._position / self._duration
            fw = int(frac * self.width())
            if fw > 0:
                p.setBrush(accent)
                p.drawRoundedRect(QRect(0, 0, fw, self.height()), radius, radius)


class ThinProgress(QWidget):
    """1px line + circle handle at position. Cleanest minimal look."""

    seek_requested = Signal(float)

    HANDLE_R = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self._theme = theming.manager().current()
        self.setFixedHeight(self.HANDLE_R * 2 + 4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setDuration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self._enabled = self._duration > 0
        self.update()

    def setPosition(self, seconds: float) -> None:
        self._position = max(0.0, min(seconds, self._duration or seconds))
        self.update()

    def reset(self) -> None:
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self.update()

    def mousePressEvent(self, ev) -> None:
        if not self._enabled or self._duration <= 0 or self.width() <= 0:
            return
        frac = max(0.0, min(1.0, ev.position().x() / self.width()))
        self.seek_requested.emit(frac * self._duration)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        accent = _qcolor(self._theme, "accent", "#d4b95e")
        dim = _qcolor(self._theme, "dim", "#666")
        cy = self.height() // 2
        # line
        p.setPen(QPen(dim, 1.0))
        p.drawLine(0, cy, self.width(), cy)
        if self._duration <= 0:
            return
        frac = self._position / self._duration
        x = int(frac * self.width())
        # filled portion in accent
        p.setPen(QPen(accent, 1.6))
        p.drawLine(0, cy, x, cy)
        # handle
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QPointF(x, cy), self.HANDLE_R, self.HANDLE_R)


class DottedProgress(QWidget):
    """Text-rendered dotted bar: `·····●·····`. Brutalist alt to blocks."""

    seek_requested = Signal(float)

    CELLS = 32

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self._theme = theming.manager().current()
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setDuration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self._enabled = self._duration > 0
        self.update()

    def setPosition(self, seconds: float) -> None:
        self._position = max(0.0, min(seconds, self._duration or seconds))
        self.update()

    def reset(self) -> None:
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self.update()

    def mousePressEvent(self, ev) -> None:
        if not self._enabled or self._duration <= 0 or self.width() <= 0:
            return
        frac = max(0.0, min(1.0, ev.position().x() / self.width()))
        self.seek_requested.emit(frac * self._duration)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        accent = _qcolor(self._theme, "accent", "#d4b95e")
        dim = _qcolor(self._theme, "dim", "#666")
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        cell_w = max(1, self.width() // self.CELLS)
        idx = -1
        if self._duration > 0:
            idx = int((self._position / self._duration) * (self.CELLS - 1))
        for i in range(self.CELLS):
            x = i * cell_w + cell_w // 2
            if i == idx:
                p.setPen(accent)
                ch = "●"
            else:
                p.setPen(dim)
                ch = "·"
            p.drawText(QRect(x - fm.horizontalAdvance(ch) // 2, 0,
                             fm.horizontalAdvance(ch), self.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, ch)


def make_progress(slug: str) -> QWidget:
    slug = (slug or "blocks").lower()
    if slug == "bar":
        return BarProgress()
    if slug == "thin":
        return ThinProgress()
    if slug == "dotted":
        return DottedProgress()
    return MonoProgress()   # "blocks" / default


# ============================================================================
# VOLUME
# ============================================================================


class SliderVolume(QWidget):
    """Standard Qt slider in disguise."""

    volume_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._slider = QSlider(Qt.Horizontal, self)
        self._slider.setRange(0, 100)
        self._slider.setFixedWidth(140)
        self._slider.setFixedHeight(20)
        self._slider.valueChanged.connect(self.volume_changed.emit)
        self.setFixedSize(150, 22)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._slider)

    def setVolume(self, value: int, *, emit: bool = True) -> None:
        v = max(0, min(100, int(value)))
        if not emit:
            self._slider.blockSignals(True)
        self._slider.setValue(v)
        if not emit:
            self._slider.blockSignals(False)

    def volume(self) -> int:
        return int(self._slider.value())

    def wheelEvent(self, ev) -> None:
        step = 5 if ev.angleDelta().y() > 0 else -5
        self.setVolume(self.volume() + step)
        ev.accept()


class KnobVolume(QWidget):
    """Round dial. Drag rotates, scroll adjusts."""

    volume_changed = Signal(int)

    SIZE = 48

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._volume = 80
        self._theme = theming.manager().current()
        self.setFixedSize(self.SIZE + 4, self.SIZE + 4)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("scroll / drag to adjust volume")
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setVolume(self, value: int, *, emit: bool = True) -> None:
        v = max(0, min(100, int(value)))
        if v == self._volume:
            return
        self._volume = v
        self.update()
        if emit:
            self.volume_changed.emit(v)

    def volume(self) -> int:
        return self._volume

    def wheelEvent(self, ev) -> None:
        step = 5 if ev.angleDelta().y() > 0 else -5
        self.setVolume(self._volume + step)
        ev.accept()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._update_from_event(ev)

    def mouseMoveEvent(self, ev) -> None:
        if ev.buttons() & Qt.LeftButton:
            self._update_from_event(ev)

    def _update_from_event(self, ev) -> None:
        cx, cy = self.width() / 2, self.height() / 2
        dx = ev.position().x() - cx
        dy = ev.position().y() - cy
        # Angle from -135° (left-bottom, volume=0) clockwise to +135° (right-bottom, volume=100).
        angle = math.degrees(math.atan2(dy, dx))   # -180..180
        # Map: angle -135 (=225 mod 360) → 0, angle 135 → 100.
        # We're projecting a 270° arc.
        if angle < -135:
            angle = -135
        if angle > 135:
            angle = 135
        frac = (angle + 135) / 270.0
        self.setVolume(round(frac * 100))

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        theme = self._theme
        bg = _qcolor(theme, "bg_alt", "#141414")
        fg = _qcolor(theme, "fg", "#e6e6e6")
        dim = _qcolor(theme, "dim", "#666")
        accent = _qcolor(theme, "accent", "#d4b95e")

        cx, cy = self.width() / 2, self.height() / 2
        r_outer = self.SIZE / 2
        r_inner = r_outer * 0.65

        # Outer ring (dim track)
        p.setPen(QPen(dim, 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(cx - r_outer, cy - r_outer, r_outer * 2, r_outer * 2),
                  225 * 16, -270 * 16)
        # Filled arc (accent)
        frac = self._volume / 100.0
        p.setPen(QPen(accent, 2.4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(cx - r_outer, cy - r_outer, r_outer * 2, r_outer * 2),
                  225 * 16, int(-270 * 16 * frac))
        # Inner disc
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawEllipse(QPointF(cx, cy), r_inner, r_inner)
        # Pointer
        angle_deg = -135 + 270 * frac
        rad = math.radians(angle_deg)
        x2 = cx + math.cos(rad) * (r_inner * 0.85)
        y2 = cy + math.sin(rad) * (r_inner * 0.85)
        p.setPen(QPen(fg, 2.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx, cy), QPointF(x2, y2))


class WedgeVolume(QWidget):
    """Triangle that fills left-to-right with volume level."""

    volume_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._volume = 80
        self._theme = theming.manager().current()
        self.setFixedSize(120, 22)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setVolume(self, value: int, *, emit: bool = True) -> None:
        v = max(0, min(100, int(value)))
        if v == self._volume:
            return
        self._volume = v
        self.update()
        if emit:
            self.volume_changed.emit(v)

    def volume(self) -> int:
        return self._volume

    def wheelEvent(self, ev) -> None:
        step = 5 if ev.angleDelta().y() > 0 else -5
        self.setVolume(self._volume + step)
        ev.accept()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._set_from_x(ev.position().x())

    def mouseMoveEvent(self, ev) -> None:
        if ev.buttons() & Qt.LeftButton:
            self._set_from_x(ev.position().x())

    def _set_from_x(self, x: float) -> None:
        frac = max(0.0, min(1.0, x / max(1, self.width())))
        self.setVolume(round(frac * 100))

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        theme = self._theme
        dim = _qcolor(theme, "dim", "#444")
        accent = _qcolor(theme, "accent", "#d4b95e")
        # Triangle outline (rises left → right)
        outline = QPainterPath()
        outline.moveTo(0, self.height() - 2)
        outline.lineTo(self.width(), 2)
        outline.lineTo(self.width(), self.height() - 2)
        outline.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(dim)
        p.drawPath(outline)
        # Filled portion
        frac = self._volume / 100.0
        fill_w = int(frac * self.width())
        if fill_w > 0:
            clip = QPainterPath()
            clip.addRect(QRectF(0, 0, fill_w, self.height()))
            p.setClipPath(clip)
            p.setBrush(accent)
            p.drawPath(outline)


def make_volume(slug: str) -> QWidget:
    slug = (slug or "blocks").lower()
    if slug == "slider":
        return SliderVolume()
    if slug == "knob":
        return KnobVolume()
    if slug == "wedge":
        return WedgeVolume()
    return MonoVolume()


# ============================================================================
# ALBUM ART
# ============================================================================


class CircleAlbumArt(AlbumArt):
    """Round-masked album art."""

    def _render(self, pix: QPixmap) -> None:
        scaled = pix.scaled(
            self._size, self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        # Apply circular mask via a transparent QPixmap canvas.
        out = QPixmap(self._size, self._size)
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(0, 0, self._size, self._size)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        self.setPixmap(out)

    def _apply_theme(self, theme) -> None:
        # Override: no border on circle; ring is implicit.
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        self.setStyleSheet(f"QLabel {{ background: {bg}; color: {theme.token('fg', '#e6e6e6') if theme else '#e6e6e6'}; }}")
        if self._pixmap_raw is None:
            self._render_empty()
        else:
            self._render(self._pixmap_raw)


class PolaroidAlbumArt(AlbumArt):
    """White border + slight rotation. Looks like a polaroid stuck on the wall."""

    BORDER = 6
    ROT_DEG = -3.0

    def _render(self, pix: QPixmap) -> None:
        inner = self._size - self.BORDER * 2
        if inner <= 0:
            return
        scaled = pix.scaled(
            inner, inner,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        out = QPixmap(self._size, self._size)
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # White border
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#f4f1e6"))
        painter.drawRoundedRect(0, 0, self._size, self._size, 3, 3)
        painter.drawPixmap(self.BORDER, self.BORDER, scaled)
        painter.end()
        # Rotate around center.
        t = QTransform()
        t.translate(self._size / 2, self._size / 2)
        t.rotate(self.ROT_DEG)
        t.translate(-self._size / 2, -self._size / 2)
        rotated = out.transformed(t, Qt.SmoothTransformation)
        self.setPixmap(rotated)

    def _apply_theme(self, theme) -> None:
        # No frame — the polaroid speaks for itself.
        self.setStyleSheet("QLabel { background: transparent; }")
        if self._pixmap_raw is None:
            self._render_empty()
        else:
            self._render(self._pixmap_raw)


def make_album_art(slug: str, size: int = 96) -> AlbumArt:
    slug = (slug or "square").lower()
    if slug == "circle":
        return CircleAlbumArt(size)
    if slug == "polaroid":
        return PolaroidAlbumArt(size)
    # "ambient" backdrop handled at strip-layout level — falls back to square here.
    return AlbumArt(size)


# ============================================================================
# CONTROLS
# ============================================================================


class ControlsBundle(QWidget):
    """Container for shuffle / prev / play / next / repeat / like buttons.

    The window wires .clicked on each. Visual style depends on the variant
    used at construction time. The bundle exposes the six buttons as
    public attributes so existing wiring stays unchanged.
    """

    def __init__(self, *, variant: str = "bracket", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.variant = variant
        cls = {"large": LargeButton, "compact": CompactButton}.get(variant, BracketButton)
        self.shuffle_btn = cls("shuffle", glyph="⇋")
        self.prev_btn = cls("prev", glyph="◂◂")
        self.play_btn = cls("play", glyph="▶")
        self.next_btn = cls("next", glyph="▸▸")
        self.repeat_btn = cls("repeat", glyph="↻")
        self.like_btn = cls("♡", glyph="♡")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2 if variant != "large" else 6)
        row.addWidget(self.shuffle_btn)
        row.addWidget(self.prev_btn)
        row.addWidget(self.play_btn)
        row.addWidget(self.next_btn)
        row.addWidget(self.repeat_btn)
        row.addWidget(self.like_btn)


class LargeButton(BracketButton):
    """Big soft glyphs for the walkman / focused layouts.

    Used to be bordered 16pt boxes with a full accent fill on hover — a
    wall of chunky form buttons that fought the "soft controls" brief (and
    six of them no longer fit walkman's 480px window). Now: transparent,
    borderless, glyph-forward, accent text on hover/active.
    """

    def _apply_theme(self, theme) -> None:
        super()._apply_theme(theme)
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#666") if theme else "#666"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        self.setStyleSheet(
            f"QPushButton#BracketButton {{"
            f"  background: transparent; color: {fg};"
            f"  border: none; padding: 6px 10px; font-size: 15pt;"
            f"}}"
            f'QPushButton#BracketButton[activeState="true"] {{ color: {accent}; }}'
            f"QPushButton#BracketButton:hover {{ color: {accent}; }}"
            f"QPushButton#BracketButton:disabled {{ color: {dim}; }}"
        )

    def _update_text(self) -> None:
        # Large is glyph-forward: the size carries the weight, not a box.
        self.setText(self._glyph or self._label)


class CompactButton(BracketButton):
    """Tiny icon-only buttons for the dj-deck / dense layouts."""

    def _apply_theme(self, theme) -> None:
        super()._apply_theme(theme)
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#666") if theme else "#666"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        self.setStyleSheet(
            f"QPushButton#BracketButton {{"
            f"  background: transparent; color: {fg};"
            f"  border: none; padding: 4px 6px; font-size: 12pt;"
            f"}}"
            f'QPushButton#BracketButton[activeState="true"] {{ color: {accent}; }}'
            f"QPushButton#BracketButton:hover {{ color: {accent}; }}"
            f"QPushButton#BracketButton:disabled {{ color: {dim}; }}"
        )

    def _update_text(self) -> None:
        # Compact always uses glyph (no brackets).
        self.setText(self._glyph or self._label)


def make_controls(slug: str) -> ControlsBundle:
    return ControlsBundle(variant=(slug or "bracket").lower())


# ============================================================================
# NOW LABEL
# ============================================================================


class InlineNowLabel(NowPlayingLabel):
    """Single-line 'artist — title' rendering."""

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        fg = _qcolor(self._theme, "fg", "#e6e6e6")
        dim = _qcolor(self._theme, "dim", "#666")
        rect = self.rect().adjusted(0, 4, -8, -4)
        fm = QFontMetrics(self.font())
        if not self._title and not self._status:
            p.setPen(dim)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft,
                       theming.styled_case("nothing playing", self._theme))
            return
        primary = (
            f"{self._artist} — {self._title}" if (self._artist and self._title)
            else (self._title or self._artist)
        )
        primary = fm.elidedText(theming.styled_case(primary, self._theme),
                                Qt.ElideRight, rect.width())
        p.setPen(fg)
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, primary)


class CenteredNowLabel(NowPlayingLabel):
    """Stacked but center-aligned. Good for compact / walkman layouts."""

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        fg = _qcolor(self._theme, "fg", "#e6e6e6")
        dim = _qcolor(self._theme, "dim", "#666")
        rect = self.rect().adjusted(0, 4, 0, -4)
        fm = QFontMetrics(self.font())
        if not self._title and not self._status:
            p.setPen(dim)
            p.drawText(rect, Qt.AlignCenter,
                       theming.styled_case("nothing playing", self._theme))
            return
        line1 = theming.styled_case(self._title or self._artist, self._theme)
        line1 = fm.elidedText(line1, Qt.ElideRight, rect.width())
        line1_rect = QRect(rect.x(), rect.y(), rect.width(), fm.height())
        p.setPen(fg)
        p.drawText(line1_rect, Qt.AlignHCenter | Qt.AlignVCenter, line1)
        # line2: artist · album
        parts = []
        if self._artist:
            parts.append(theming.styled_case(self._artist, self._theme))
        if self._album:
            parts.append(theming.styled_case(self._album, self._theme))
        line2 = "  ·  ".join(parts)
        if line2:
            line2 = fm.elidedText(line2, Qt.ElideRight, rect.width())
            line2_rect = QRect(rect.x(), rect.y() + fm.height() + 2,
                               rect.width(), fm.height())
            p.setPen(dim)
            p.drawText(line2_rect, Qt.AlignHCenter | Qt.AlignVCenter, line2)


def make_now_label(slug: str) -> NowPlayingLabel:
    slug = (slug or "stacked").lower()
    if slug == "inline":
        return InlineNowLabel()
    if slug == "centered":
        return CenteredNowLabel()
    return NowPlayingLabel()


# ============================================================================
# slug enumeration (for settings UI)
# ============================================================================


PROGRESS_VARIANTS = ["blocks", "bar", "thin", "dotted"]
VOLUME_VARIANTS = ["blocks", "slider", "knob", "wedge"]
ALBUM_ART_VARIANTS = ["square", "circle", "polaroid"]
CONTROLS_VARIANTS = ["bracket", "large", "compact"]
NOW_LABEL_VARIANTS = ["stacked", "inline", "centered"]


def all_variant_slugs() -> dict[str, list[str]]:
    return {
        "progress":  PROGRESS_VARIANTS,
        "volume":    VOLUME_VARIANTS,
        "album_art": ALBUM_ART_VARIANTS,
        "controls":  CONTROLS_VARIANTS,
        "now_label": NOW_LABEL_VARIANTS,
    }
