"""The qt side of the vr panel: the now-playing widget (never shown on the
desktop, rendered into frames for steamvr), the bit that turns laser
events into qt mouse events, and the watch face.

Everything themed through ctx.theme so it looks like tide in both
personalities. No popups, menus or tooltips anywhere in here: those open
real desktop windows, which don't exist in the headset.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor, QEnterEvent, QFont, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from .vr import PANEL_PX, WATCH_PX

COVER_PX = 300


def _fmt(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class Forwarder:
    """laser events in, the qt events a real mouse would cause out. the
    pressed widget keeps getting moves until release, so a drag on the
    seek bar survives the laser drifting off it."""

    def __init__(self, root: QWidget) -> None:
        self.root = root
        self.pos = QPointF(-1, -1)
        self.hover: QWidget | None = None
        self.grab: QWidget | None = None

    def _at(self, p: QPointF) -> QWidget:
        return self.root.childAt(p.toPoint()) or self.root

    def _send(self, kind, target: QWidget, button, buttons) -> None:
        local = QPointF(target.mapFrom(self.root, self.pos.toPoint()))
        QApplication.sendEvent(target, QMouseEvent(kind, local, local, self.pos,
                                                   button, buttons, Qt.NoModifier))

    def _rehover(self) -> None:
        target = self._at(self.pos)
        if target is self.hover:
            return
        if self.hover is not None:
            QApplication.sendEvent(self.hover, QEvent(QEvent.Leave))
        self.hover = target
        local = QPointF(target.mapFrom(self.root, self.pos.toPoint()))
        QApplication.sendEvent(target, QEnterEvent(local, local, self.pos))

    def handle(self, kind: str, x: float, y: float) -> None:
        if kind == "leave":
            if self.grab is not None:
                self._send(QEvent.MouseButtonRelease, self.grab, Qt.LeftButton, Qt.NoButton)
                self.grab = None
            if self.hover is not None:
                QApplication.sendEvent(self.hover, QEvent(QEvent.Leave))
            self.hover = None
            return
        self.pos = QPointF(x, y)
        if kind == "move":
            if self.grab is not None:
                self._send(QEvent.MouseMove, self.grab, Qt.NoButton, Qt.LeftButton)
            else:
                self._rehover()
                self._send(QEvent.MouseMove, self.hover, Qt.NoButton, Qt.NoButton)
        elif kind == "down":
            self._rehover()
            self.grab = self._at(self.pos)
            self._send(QEvent.MouseButtonPress, self.grab, Qt.LeftButton, Qt.LeftButton)
        elif kind == "up":
            target = self.grab or self._at(self.pos)
            self.grab = None
            self._send(QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
            self._rehover()


class VrPanel(QWidget):
    """cover, title, artist, seek, prev / play / next / like, and an x."""

    close_requested = Signal()
    changed = Signal()          # something on it looks different now

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.setObjectName("vrpanel")
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(*PANEL_PX)
        self._dragging = False
        self._art_url = ""

        self.cover = QLabel()
        self.cover.setFixedSize(COVER_PX, COVER_PX)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setObjectName("cover")

        self.title = QLabel()
        self.title.setObjectName("title")
        self.meta = QLabel()
        self.meta.setObjectName("meta")
        for lab in (self.title, self.meta):
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.close_btn = QPushButton("x")
        self.close_btn.setObjectName("close")
        self.close_btn.clicked.connect(self.close_requested)

        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setFixedHeight(48)
        self.seek.sliderPressed.connect(self._seek_start)
        self.seek.sliderReleased.connect(self._seek_end)
        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("meta")

        self.prev_btn = QPushButton()
        self.play_btn = QPushButton()
        self.next_btn = QPushButton()
        self.like_btn = QPushButton()
        for b in (self.prev_btn, self.play_btn, self.next_btn, self.like_btn):
            b.setObjectName("control")
        self.prev_btn.clicked.connect(lambda: ctx.playback.previous())
        self.play_btn.clicked.connect(lambda: ctx.playback.toggle())
        self.next_btn.clicked.connect(lambda: ctx.playback.next())
        self.like_btn.clicked.connect(lambda: ctx.playback.like(not ctx.playback.liked()))

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.title, 1)
        top.addWidget(self.close_btn, 0, Qt.AlignTop)
        seek_row = QHBoxLayout()
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.time)
        controls = QHBoxLayout()
        controls.setSpacing(14)
        for b in (self.prev_btn, self.play_btn, self.next_btn, self.like_btn):
            controls.addWidget(b)
        controls.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addLayout(top)
        right.addWidget(self.meta)
        right.addStretch(1)
        right.addLayout(seek_row)
        right.addLayout(controls)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(30, 30, 30, 30)
        lay.setSpacing(28)
        lay.addWidget(self.cover, 0, Qt.AlignVCenter)
        lay.addLayout(right, 1)

        self.restyle()
        self.refresh()

    # ---- look ----

    def restyle(self) -> None:
        t = self.ctx.theme
        bg, fg = t.token("bg", "#0b0b0b"), t.token("fg", "#e6e6e6")
        dim, accent = t.token("dim", "#6f6f6f"), t.token("accent", "#d4b95e")
        border = t.token("border_col", fg)
        hover = t.token("hover_bg", "#1a1a1a")
        modern = t.aesthetic() == "modern"
        radius = 22 if modern else 0
        btn_radius = 14 if modern else 0
        family, _mono = t.font()
        self.setStyleSheet(f"""
            QWidget#vrpanel {{ background: {bg}; border: 3px solid {border};
                               border-radius: {radius}px; }}
            QLabel {{ color: {fg}; background: transparent; }}
            QLabel#meta {{ color: {dim}; }}
            QLabel#cover {{ background: {hover}; border-radius: {btn_radius}px; }}
            QPushButton {{ color: {fg}; background: transparent; border: 3px solid {border};
                           border-radius: {btn_radius}px; }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:pressed {{ background: {accent}; color: {bg}; border-color: {accent}; }}
            QPushButton#control {{ min-width: 104px; max-width: 104px;
                                   min-height: 84px; max-height: 84px; }}
            QPushButton#close {{ min-width: 64px; max-width: 64px;
                                 min-height: 64px; max-height: 64px; }}
            QSlider::groove:horizontal {{ height: 10px; background: {hover};
                                          border-radius: {5 if modern else 0}px; }}
            QSlider::sub-page:horizontal {{ background: {accent};
                                            border-radius: {5 if modern else 0}px; }}
            QSlider::handle:horizontal {{ width: 30px; margin: -14px 0; background: {fg};
                                          border-radius: {15 if modern else 0}px; }}
        """)

        def font(px: int, bold: bool = False) -> QFont:
            f = QFont(family) if family else QFont()
            f.setPixelSize(px)
            f.setBold(bold)
            return f
        self.title.setFont(font(40, True))
        self.meta.setFont(font(24))
        self.time.setFont(font(22))
        for b in (self.prev_btn, self.play_btn, self.next_btn, self.like_btn):
            b.setFont(font(34, True))
        self.close_btn.setFont(font(28, True))
        self.refresh()

    # ---- content ----

    def refresh(self) -> None:
        t, pb = self.ctx.theme, self.ctx.playback
        track = pb.current()
        if track is None:
            self.title.setText(t.case("nothing playing"))
            self.meta.setText("")
        else:
            self.title.setText(t.case(track.title))
            self.meta.setText(t.case(" · ".join(x for x in (track.artists, track.album) if x)))
            if track.art_url != self._art_url:
                self._art_url = track.art_url
                self.cover.clear()
                if track.art_url:
                    self.ctx.track.art(track.art_url, self._got_art)
        playing = pb.state() in ("playing", "loading")
        self.prev_btn.setText(t.glyph("prev"))
        self.play_btn.setText(t.glyph("pause" if playing else "play"))
        self.next_btn.setText(t.glyph("next"))
        self.like_btn.setText(t.glyph("like_on" if pb.liked() else "like_off"))
        self.progress(pb.position(), pb.duration())
        self.changed.emit()

    def _got_art(self, img) -> None:
        if img is None or img.isNull():
            return
        pix = QPixmap.fromImage(img).scaled(COVER_PX, COVER_PX, Qt.KeepAspectRatioByExpanding,
                                            Qt.SmoothTransformation)
        if self.ctx.theme.aesthetic() == "modern":
            rounded = QPixmap(COVER_PX, COVER_PX)
            rounded.fill(Qt.transparent)
            p = QPainter(rounded)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, COVER_PX, COVER_PX), 14, 14)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            pix = rounded
        self.cover.setPixmap(pix)
        self.changed.emit()

    def progress(self, position: float, duration: float) -> None:
        if not self._dragging:
            self.seek.blockSignals(True)
            self.seek.setValue(int(1000 * position / duration) if duration > 0 else 0)
            self.seek.blockSignals(False)
        self.time.setText(f"{_fmt(position)} / {_fmt(duration)}")

    def _seek_start(self) -> None:
        self._dragging = True

    def _seek_end(self) -> None:
        self._dragging = False
        dur = self.ctx.playback.duration()
        if dur > 0:
            self.ctx.playback.seek(self.seek.value() / 1000 * dur)
        self.changed.emit()

    def frame(self) -> QImage:
        img = QImage(*PANEL_PX, QImage.Format_RGBA8888)
        img.fill(Qt.transparent)
        self.render(img)
        return img


def watch_face(ctx, progress: float) -> QImage:
    """a dark disc, the song's progress as a ring in the accent, "tide"."""
    t = ctx.theme
    px = WATCH_PX
    img = QImage(px, px, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(10, 10, px - 20, px - 20)
    bg = QColor(t.token("bg", "#0b0b0b"))
    bg.setAlpha(235)
    p.setPen(Qt.NoPen)
    p.setBrush(bg)
    p.drawEllipse(r)
    p.setBrush(Qt.NoBrush)
    ring = QColor(t.token("fg", "#e6e6e6"))
    ring.setAlpha(60)
    p.setPen(QPen(ring, 12))
    p.drawEllipse(r.adjusted(8, 8, -8, -8))
    pen = QPen(QColor(t.token("accent", "#d4b95e")), 12)
    pen.setCapStyle(Qt.FlatCap)
    p.setPen(pen)
    p.drawArc(r.adjusted(8, 8, -8, -8), 90 * 16, -int(360 * 16 * max(0.0, min(1.0, progress))))
    family, _mono = t.font()
    f = QFont(family) if family else QFont()
    f.setPixelSize(54)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(t.token("fg", "#e6e6e6")))
    p.drawText(r, Qt.AlignCenter, t.case("tide"))
    p.end()
    return img
