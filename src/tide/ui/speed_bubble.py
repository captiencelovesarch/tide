"""The speed bubble (2.3): the speed pill grows a speech bubble.

One continuous outline: a rounded panel whose bottom edge flows, through
two concave fillets, into the pill itself — the pill is the bubble's
stem, drawn by the bubble (it covers the real pill exactly, so the
control never seems to move). Inside: the value big ("1×"), "reset" in
the top-right corner, and a row of − · detent slider · +.

brutalist draws the same silhouette with square corners and a hard
notch where the stem meets the panel, a 1px fg rule, [−] [+] [reset]
and a square-faced slider. modern rounds everything and opens with the
panel growing up out of the pill (motion OFF: it just appears).

Same outward surface as the old popovers (``speed_changed`` /
``sync`` / ``show_above``) so SpeedButton can't tell them apart. A press
on the stem closes the bubble, like clicking the pill again would.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from ..speed_law import SPEED_MAX, SPEED_MIN, SPEED_PRESETS, SPEED_STEP, format_speed
from . import scale
from .spring_slider import SpringSlider
from .widgets import BracketButton

NECK_PX = 6          # panel → pill gap the fillets bridge
FILLET_PX = 10       # concave corner radius where the stem meets the panel
PANEL_MIN_W = 300


def _brutalist(theme) -> bool:
    return getattr(theme, "aesthetic", "") != "modern"


def bubble_path(panel: QRectF, stem: QRectF, *, radius: float,
                stem_radius: float, fillet: float, down: bool = True) -> QPainterPath:
    """The silhouette: ``panel`` ∪ ``stem`` with concave fillets where
    they meet. ``down`` = the stem hangs below the panel (the bar case);
    False mirrors it for a bubble that has to open downward."""
    path = QPainterPath()
    path.addRoundedRect(panel, radius, radius)
    s = QPainterPath()
    s.addRoundedRect(stem, stem_radius, stem_radius)
    path = path.united(s)
    # Bridge the neck: the stem's full width from the panel edge into
    # the stem past its rounded cap, so the two read as one shape.
    if down:
        bridge = QRectF(stem.left(), panel.bottom() - 1,
                        stem.width(), stem.top() - panel.bottom() + stem_radius + 1)
    else:
        bridge = QRectF(stem.left(), stem.bottom() - stem_radius,
                        stem.width(), panel.top() - stem.bottom() + stem_radius + 1)
    b = QPainterPath()
    b.addRect(bridge)
    path = path.united(b)
    if fillet > 0.5:
        for side in (-1, 1):
            x = stem.left() if side < 0 else stem.right()
            # the fillet square hugs the junction outside the stem
            fx = x - fillet if side < 0 else x
            if fx < panel.left() + radius or fx + fillet > panel.right() - radius:
                continue
            fy = panel.bottom() if down else panel.top() - fillet
            sq = QPainterPath()
            sq.addRect(QRectF(fx, fy, fillet, fillet))
            cx = x - fillet if side < 0 else x + fillet
            cy = panel.bottom() + fillet if down else panel.top() - fillet
            hole = QPainterPath()
            hole.addEllipse(QPointF(cx, cy), fillet, fillet)
            path = path.united(sq.subtracted(hole))
    return path.simplified()


class _Content(QWidget):
    """The panel's contents. Transparent: the bubble paints the panel."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)


class SpeedBubble(QFrame):
    """The speed control as a bubble growing out of its pill."""

    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("SpeedBubble")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._theme = theming.manager().current_effective()
        theming.manager().theme_changed.connect(self._on_theme)
        self._current = 1.0
        self._down = True
        self._panel = QRectF()
        self._stem = QRectF()
        self._grow = 1.0                  # 0 → panel collapsed onto the stem
        self._anim: QVariantAnimation | None = None
        self._pill_text = format_speed(1.0)
        self._pill_font: QFont | None = None

        self.content = _Content(self)
        self.display = QLabel(format_speed(1.0), self.content)
        self.display.setObjectName("SpeedBubbleValue")
        self.display.setAlignment(Qt.AlignCenter)
        from . import typefaces
        typefaces.tag(self.display, "numbers")

        self.reset_btn = BracketButton("reset", parent=self.content)
        self.reset_btn.setToolTip(theming.styled_case("back to 1×"))
        self.reset_btn.clicked.connect(lambda: self.speed_changed.emit(1.0))

        self.minus_btn = BracketButton("−", parent=self.content)
        self.plus_btn = BracketButton("+", parent=self.content)
        self.minus_btn.setToolTip(f"−{SPEED_STEP:.2f}")
        self.plus_btn.setToolTip(f"+{SPEED_STEP:.2f}")
        self.minus_btn.setAutoRepeat(True)
        self.plus_btn.setAutoRepeat(True)
        self.minus_btn.clicked.connect(self._on_minus)
        self.plus_btn.clicked.connect(self._on_plus)

        self.slider = SpringSlider(self.content)
        self.slider.set_range(SPEED_MIN, SPEED_MAX, SPEED_STEP)
        self.slider.set_detents(SPEED_PRESETS)
        self.slider.set_formatter(format_speed)
        self.slider.set_value_bubble(False)
        self.slider.setMinimumWidth(scale.px(180))
        self.slider.value_changed.connect(self._on_slider_changed)
        self.slider.value_committed.connect(
            lambda v: self.speed_changed.emit(float(v)))

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        # A spacer the width of reset keeps the value truly centred.
        self._balance = QWidget(self.content)
        self._balance.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # The app sheet paints every QWidget @bg — a spacer must not.
        self._balance.setStyleSheet("background: transparent;")
        top.addWidget(self._balance)
        top.addWidget(self.display, stretch=1)
        top.addWidget(self.reset_btn, alignment=Qt.AlignTop)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(scale.px(8))
        row.addWidget(self.minus_btn)
        row.addWidget(self.slider, stretch=1)
        row.addWidget(self.plus_btn)

        col = QVBoxLayout(self.content)
        col.setContentsMargins(scale.px(12), scale.px(8), scale.px(10), scale.px(12))
        col.setSpacing(scale.px(6))
        col.addLayout(top)
        col.addLayout(row)

        # The old popovers' names — mpris/keymap tests and tools poke these.
        self._display = self.display
        self._slider = self.slider
        self._minus_btn = self.minus_btn
        self._plus_btn = self.plus_btn
        self._reset_btn = self.reset_btn

        self._fade = QGraphicsOpacityEffect(self.content)
        self._fade.setOpacity(1.0)
        self.content.setGraphicsEffect(self._fade)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._restyle()

    # ------------------------------------------------------------- surface

    def sync(self, speed: float) -> None:
        self._current = float(speed)
        text = format_speed(speed)
        self.display.setText(text)
        self._pill_text = text
        self.minus_btn.setEnabled(speed > SPEED_MIN + 1e-4)
        self.plus_btn.setEnabled(speed < SPEED_MAX - 1e-4)
        self.reset_btn.setEnabled(abs(speed - 1.0) > 1e-4)
        if abs(self.slider.value() - float(speed)) > 1e-9:
            self.slider.set_value(float(speed), animate=self.isVisible())
        self.update()

    def show_above(self, anchor: QWidget) -> None:
        """Grow out of ``anchor`` (the pill): the stem lands exactly on
        it. Opens downward instead when the screen has no room above."""
        self._restyle()
        self._pill_font = QFont(anchor.font())
        self._pill_text = anchor.text() if hasattr(anchor, "text") and \
            anchor.text() else format_speed(self._current)
        pill = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        hint = self.content.sizeHint()
        pw = max(scale.px(PANEL_MIN_W), hint.width(), pill.width() + 4 * FILLET_PX)
        ph = hint.height()
        neck = scale.px(NECK_PX)
        total_h = ph + neck + pill.height()
        screen = anchor.screen()
        geom = screen.availableGeometry() if screen is not None else None

        self._down = True
        y = pill.top() - neck - ph
        if geom is not None and y < geom.top():
            self._down = False
            y = pill.top()
        x = pill.center().x() - pw // 2
        if geom is not None:
            x = max(geom.left() + 4, min(x, geom.right() - pw - 4))
        # The window spans the panel and the stem both.
        win = QRect(x, y, pw, total_h)
        self.setGeometry(win)
        sx = float(pill.left() - x)
        if self._down:
            self._panel = QRectF(0.5, 0.5, pw - 1.0, ph - 1.0)
            self._stem = QRectF(sx + 0.5, ph + neck, pill.width() - 1.0,
                                pill.height() - 0.5)
            self.content.setGeometry(0, 0, pw, ph)
        else:
            self._stem = QRectF(sx + 0.5, 0.5, pill.width() - 1.0, pill.height() - 1.0)
            self._panel = QRectF(0.5, pill.height() + neck, pw - 1.0, ph - 1.0)
            self.content.setGeometry(0, pill.height() + neck, pw, ph)
        self._balance.setFixedWidth(self.reset_btn.sizeHint().width())
        self.sync(self._current)
        self.show()
        self.raise_()
        self._play_grow()

    # ------------------------------------------------------------- motion

    def _play_grow(self) -> None:
        from . import motion as motion_module
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if motion_module.intensity() == motion_module.Intensity.OFF:
            self._set_grow(1.0)
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(motion_module.dur("short"))
        anim.setEasingCurve(motion_module.ease("out"))
        anim.valueChanged.connect(lambda v: self._set_grow(float(v)))
        anim.finished.connect(lambda: self._set_grow(1.0))
        self._anim = anim
        self._set_grow(0.0)
        anim.start()

    def _set_grow(self, t: float) -> None:
        self._grow = max(0.0, min(1.0, t))
        # Contents arrive in the second half, once there's a panel to hold them.
        self._fade.setOpacity(max(0.0, (self._grow - 0.45) / 0.55))
        self.update()

    def _grown_panel(self) -> QRectF:
        """The panel at the current grow step: from the stem's footprint
        out to the full panel."""
        t = self._grow
        if t >= 1.0:
            return self._panel
        p, s = self._panel, self._stem
        if self._down:
            start = QRectF(s.left(), p.bottom() - s.height(), s.width(), s.height())
        else:
            start = QRectF(s.left(), p.top(), s.width(), s.height())

        def lerp(a: float, b: float) -> float:
            return a + (b - a) * t
        return QRectF(QPointF(lerp(start.left(), p.left()), lerp(start.top(), p.top())),
                      QPointF(lerp(start.right(), p.right()),
                              lerp(start.bottom(), p.bottom())))

    # ------------------------------------------------------------- input

    def mousePressEvent(self, ev) -> None:
        if self._stem.contains(QPointF(ev.position())):
            # The pill again: close, like a second click on it.
            self.close()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def _on_slider_changed(self, value: float) -> None:
        self._current = float(value)
        self.display.setText(format_speed(self._current))
        self._pill_text = format_speed(self._current)
        self.update()
        self.speed_changed.emit(self._current)

    def _on_minus(self) -> None:
        self.speed_changed.emit(self._current - SPEED_STEP)

    def _on_plus(self) -> None:
        self.speed_changed.emit(self._current + SPEED_STEP)

    # ------------------------------------------------------------- look

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self._restyle()
        self.update()

    def _restyle(self) -> None:
        brutal = _brutalist(self._theme)
        self.slider.set_square(brutal)
        # Square − / + buttons (the sketch's ends), the value big.
        side = scale.px(30)
        for b in (self.minus_btn, self.plus_btn):
            b.setFixedSize(side + (scale.px(10) if brutal else 0), side)
            if not brutal:
                b.setRole("pill")
        f = QFont(self.font())
        base = f.pointSizeF() if f.pointSizeF() > 0 else 10.0
        f.setPointSizeF(base * 2.4)
        f.setWeight(QFont.Weight.DemiBold if not brutal else QFont.Weight.Bold)
        from . import typefaces
        self.display.setFont(typefaces.font_for("numbers", f))
        self.display.setStyleSheet(
            f"font-size: {base * 2.4 * typefaces.role('numbers')['size'] / 100:.1f}pt;")
        self.reset_btn.setSizeFactor(0.85)

    def _colors(self) -> tuple[QColor, QColor, QColor]:
        theme = self._theme
        if not _brutalist(theme):
            from .. import material
            fill, line = material.panel_colors(theme)
            fg = QColor(theme.token("fg", "#e6e6e6")) if theme else QColor("#e6e6e6")
            return fill, line, fg
        bg = QColor(theme.token("bg", "#0b0b0b")) if theme else QColor("#0b0b0b")
        fg = QColor(theme.token("fg", "#e6e6e6")) if theme else QColor("#e6e6e6")
        return bg, fg, fg

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        brutal = _brutalist(self._theme)
        fill, line, fg = self._colors()
        radius = 0.0 if brutal else float(max(8, theming.effective_radius_px(self._theme) * 2))
        stem_r = 0.0 if brutal else self._stem.height() / 2.0
        fillet = 0.0 if brutal else float(scale.px(FILLET_PX)) * self._grow
        path = bubble_path(self._grown_panel(), self._stem, radius=radius,
                           stem_radius=stem_r, fillet=fillet, down=self._down)
        p.setPen(QPen(line, 1.0))
        p.setBrush(fill)
        p.drawPath(path)
        # The pill's own face, in the stem.
        if self._pill_font is not None:
            p.setFont(self._pill_font)
        p.setPen(fg)
        text = self._pill_text
        if brutal and not text.startswith("["):
            text = f"[{text}]"
        p.drawText(self._stem, Qt.AlignCenter, text)
        p.end()
