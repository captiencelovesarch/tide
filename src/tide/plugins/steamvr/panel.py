"""The qt side of the vr panel: the panel widget (never shown on the
desktop, rendered into frames for steamvr), the bit that turns laser
events into qt mouse events, and the watch face.

Four tabs (the user's drawing, cleaned up): now playing, search, library,
settings. Lists are big cover grids and song rows you scroll by grabbing
them with the laser (or the stick while pointing).

Everything is themed through ctx.theme so it looks like tide in both
personalities. No popups, menus, combo boxes or tooltips anywhere in
here: those open real desktop windows, which don't exist in the headset.
Typing goes through steamvr's own keyboard (the vr thread opens it).
"""
from __future__ import annotations

import json
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QEnterEvent, QFont, QFontMetrics, QIcon, QImage, QMouseEvent, QPainter,
    QPainterPath, QPen, QPixmap, QRegion,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea, QApplication, QButtonGroup, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import icons
from .vr import PANEL_PX, WATCH_PX

COVER_PX = 250
TEXT_W = 590          # the column right of the cover, for eliding
CONTROL_PX = 84
PLAY_PX = 112
TILE_W = 168
TILE_COVER = 150
ROW_H = 78
ROW_COVER = 60
TABS = (("now", "note", "now playing"),
        ("search", "search", "search"),
        ("library", "library", "library"),
        ("settings", "settings", "settings"))
# the settings page, in order: plugin.toml keys
# two columns, each label over its buttons: one long list squeezed the
# buttons too small to touch
VR_SETTINGS_COLUMNS = (
    ("watch_hand", "open_by_looking", "touch", "size", "linger"),
    ("ring", "ring_strength", "panel_glow", "lyrics", "haptics", "voice_model"),
)
VR_SETTINGS = tuple(k for col in VR_SETTINGS_COLUMNS for k in col)
RECENT_MAX = 8
ROW_TEXT_W = 430           # a song row's title, beside its radio and next buttons


def _fmt(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _elide(label: QLabel, text: str, width: int) -> None:
    label.setText(QFontMetrics(label.font()).elidedText(text, Qt.ElideRight, width))


def _ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def _passive(*widgets: QWidget) -> None:
    """let clicks fall through to the row or tile underneath, so the whole
    thing is one target and its hover shows."""
    for w in widgets:
        w.setAttribute(Qt.WA_TransparentForMouseEvents, True)


# ---------------------------------------------------------------------------
# laser → qt
# ---------------------------------------------------------------------------

class Forwarder:
    """laser events in, the qt events a real mouse would cause out.

    the pressed widget keeps getting moves until release, so a drag on the
    seek bar survives the laser drifting off it. a press inside a scroll
    area that moves more than DRAG_PX turns into scrolling instead (the
    press is cancelled, so it never counts as a click), and it coasts a
    little when you let go."""

    DRAG_PX = 14
    STICK_PX = 70           # one stick notch of smooth scroll

    def __init__(self, root: QWidget, on_change=None) -> None:
        self.root = root
        self.on_change = on_change or (lambda: None)
        self.pos = QPointF(-1, -1)
        self.hover: QWidget | None = None
        self.grab: QWidget | None = None
        self._area: QAbstractScrollArea | None = None
        self._press = QPointF()
        self._start_value = 0
        self.dragging = False
        self._trail: list[tuple[float, float]] = []
        self._velocity = 0.0
        self._coast = QTimer(root)
        self._coast.setInterval(16)
        self._coast.timeout.connect(self._coast_step)

    def _at(self, p: QPointF) -> QWidget:
        return self.root.childAt(p.toPoint()) or self.root

    @staticmethod
    def _scroll_area(w: QWidget | None) -> QAbstractScrollArea | None:
        while w is not None:
            if isinstance(w, QAbstractScrollArea):
                return w
            w = w.parentWidget()
        return None

    def _send(self, kind, target: QWidget, button, buttons, local: QPointF | None = None) -> None:
        if local is None:
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

    def _cancel_press(self) -> None:
        """release the pressed widget somewhere far outside it: no click."""
        if self.grab is not None:
            self._send(QEvent.MouseButtonRelease, self.grab, Qt.LeftButton, Qt.NoButton,
                       QPointF(-10000, -10000))
            QApplication.sendEvent(self.grab, QEvent(QEvent.Leave))
            self.grab = None

    def handle(self, kind: str, x: float, y: float) -> None:
        if kind == "leave":
            self._cancel_press()
            self.dragging = False
            if self.hover is not None:
                QApplication.sendEvent(self.hover, QEvent(QEvent.Leave))
            self.hover = None
            return
        self.pos = QPointF(x, y)
        now = time.monotonic()
        if kind == "move":
            if self.dragging:
                self._drag_to(now)
            elif self.grab is not None:
                if self._area is not None and abs(y - self._press.y()) > self.DRAG_PX:
                    self._cancel_press()
                    self.dragging = True
                    self._trail = [(now, y)]
                    self._drag_to(now)
                else:
                    self._send(QEvent.MouseMove, self.grab, Qt.NoButton, Qt.LeftButton)
            else:
                self._rehover()
                self._send(QEvent.MouseMove, self.hover, Qt.NoButton, Qt.NoButton)
        elif kind == "down":
            self._coast.stop()
            self._rehover()
            ripple = getattr(self.root, "ripple", None)
            if ripple is not None:
                ripple(QPointF(self.pos))
            self.grab = self._at(self.pos)
            self._press = QPointF(self.pos)
            self._area = self._scroll_area(self.grab)
            if self._area is not None:
                self._start_value = self._area.verticalScrollBar().value()
            self._send(QEvent.MouseButtonPress, self.grab, Qt.LeftButton, Qt.LeftButton)
        elif kind == "up":
            if self.dragging:
                self.dragging = False
                self._start_coast(now)
            else:
                target = self.grab or self._at(self.pos)
                self.grab = None
                self._send(QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
            self._rehover()
        self.on_change()

    def _drag_to(self, now: float) -> None:
        bar = self._area.verticalScrollBar()
        bar.setValue(int(self._start_value - (self.pos.y() - self._press.y())))
        self._trail = [(t, yy) for t, yy in self._trail if now - t < 0.12] + [(now, self.pos.y())]

    def _start_coast(self, now: float) -> None:
        if len(self._trail) >= 2:
            (t0, y0), (t1, y1) = self._trail[0], self._trail[-1]
            if t1 - t0 > 1e-3:
                self._velocity = -(y1 - y0) / (t1 - t0)       # px a second, scroll direction
                if abs(self._velocity) > 200:
                    self._coast.start()

    def _coast_step(self) -> None:
        if self._area is None:
            self._coast.stop()
            return
        bar = self._area.verticalScrollBar()
        bar.setValue(int(bar.value() + self._velocity * 0.016))
        self._velocity *= 0.9
        if abs(self._velocity) < 60 or bar.value() in (bar.minimum(), bar.maximum()):
            self._coast.stop()
        self.on_change()

    def scroll(self, notches: float) -> None:
        """the stick, while pointing at the panel."""
        area = self._scroll_area(self._at(self.pos))
        if area is not None:
            bar = area.verticalScrollBar()
            bar.setValue(int(bar.value() - notches * self.STICK_PX))
            self.on_change()


# ---------------------------------------------------------------------------
# look
# ---------------------------------------------------------------------------

class _Look:
    """the theme, read once per restyle."""

    def __init__(self, ctx) -> None:
        t = ctx.theme
        self.ctx = ctx
        self.bg = t.token("bg", "#0b0b0b")
        self.bg_alt = t.token("bg_alt", self.bg)
        self.fg = t.token("fg", "#e6e6e6")
        self.dim = t.token("dim", "#6f6f6f")
        self.accent = t.token("accent", "#d4b95e")
        self.border = t.token("border_col", self.fg)
        self.hover = t.token("hover_bg", "#1a1a1a")
        self.modern = t.aesthetic() == "modern"
        self.family, self.mono = t.font()
        # the user's type roles: body, titles, numbers (2026-10-19: a
        # title font and a body font set in tide didn't come across)
        fonts = t.fonts() if hasattr(t, "fonts") else {}
        self.roles = {name: fonts.get(name) or {"family": self.family, "weight": 0,
                                                "size": 100, "spacing": 0}
                      for name in ("ui", "title", "numbers")}

    def _spec(self, px: int, bold: bool, role: str) -> tuple[str, int, int, int]:
        """(family, px, weight, spacing) for text in ``role``."""
        r = self.roles.get(role) or self.roles["ui"]
        size = int(r.get("size") or 100) if role != "ui" else 100
        px = max(1, round(px * size / 100))
        weight = int(r.get("weight") or 0)
        if not weight or (role == "ui" and bold):
            weight = max(weight, 700 if bold else 400)
        return str(r.get("family") or self.family or ""), px, weight, int(r.get("spacing") or 0)

    def font(self, px: int, bold: bool = False, role: str = "ui") -> QFont:
        family, px, weight, spacing = self._spec(px, bold, role)
        f = QFont(family) if family else QFont()
        f.setPixelSize(px)
        f.setWeight(QFont.Weight(weight))
        if spacing:
            f.setLetterSpacing(QFont.PercentageSpacing, 100.0 + spacing)
        return f

    def apply(self, widget: QWidget, px: int, bold: bool = False, extra: str = "",
              role: str = "ui") -> None:
        """a font on ``widget`` that survives tide's app-wide stylesheet:
        once a widget is polished, the theme's qss beats setFont, but a
        widget's own stylesheet beats the theme's. letter spacing can't
        be said in qss; it rides the widget's own font, which the sheet's
        family and size merge over."""
        family, size, weight, _spacing = self._spec(px, bold, role)
        widget.setFont(self.font(px, bold, role))
        fam = f"font-family: '{family}'; " if family else ""
        widget.setStyleSheet(f"{fam}font-size: {size}px; font-weight: {weight};{extra}")

    def icon(self, key: str, px: int, color: str) -> QPixmap | None:
        if key in icons.DRAWN:
            return icons.drawn(key, px, color, square=not self.modern)
        return self.ctx.theme.icon(key, px, color)

    def radius(self, px: int) -> int:
        return px if self.modern else 0


class _PlayLook(_Look):
    """modern's play button: icon in the background color on a fg disc."""

    def __init__(self, base: _Look) -> None:
        self.__dict__.update(base.__dict__)
        self.fg = base.bg
        self.accent = base.bg


class _IconButton(QPushButton):
    """a control: an icon from the set (modern) or tide's glyph
    (brutalist), with an "on" state drawn in the accent."""

    def __init__(self, icon_key: str, glyph_key: str | None, px: int, round_: bool = False,
                 h: int | None = None) -> None:
        super().__init__()
        self.icon_key, self.glyph_key = icon_key, glyph_key
        self.px = px
        self.on = False
        self.setObjectName("round" if round_ else "control")
        self.setFixedSize(px, h or px)
        self.look: _Look | None = None

    def set_keys(self, icon_key: str, glyph_key: str | None) -> None:
        self.icon_key, self.glyph_key = icon_key, glyph_key
        self.paint_icon()

    def set_on(self, on: bool) -> None:
        if on == self.on and self.property("on") is not None:
            return
        self.on = on
        self.setProperty("on", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.paint_icon()

    def paint_icon(self) -> None:
        look = self.look
        if look is None:
            return
        color = look.accent if self.on else look.fg
        use_glyph = not look.modern and self.glyph_key
        size = int(min(self.width(), self.height()) * 0.46)
        pix = None if use_glyph else look.icon(self.icon_key, size, color)
        if pix is not None:
            self.setIcon(QIcon(pix))
            self.setIconSize(QSize(pix.width(), pix.height()))
            self.setText("")
        else:
            self.setIcon(QIcon())
            self.setText(look.ctx.theme.glyph(self.glyph_key) if self.glyph_key else self.icon_key)
            look.apply(self, int(min(self.width(), self.height()) * 0.36), True)


class _Cover(QLabel):
    """a cover from a url, loaded through tide's art cache; round for
    artists on modern."""

    def __init__(self, ctx, px: int) -> None:
        super().__init__()
        self.ctx, self.px = ctx, px
        self.setObjectName("cover")
        self.setFixedSize(px, px)
        self._url = ""
        self.round = False
        self.radius = 0
        _passive(self)

    animate = False         # modern crossfades a new cover over the old one
    FADE_S = 0.42

    def load(self, url: str) -> None:
        if url == self._url:
            return
        self._url = url
        old = self.pixmap()
        self._old = QPixmap(old) if self.animate and old is not None and not old.isNull() else None
        self._swapped_at = None
        self.clear()
        if url:
            self.ctx.track.art(url, lambda img, u=url: self._got(u, img))

    def fading(self, now: float) -> bool:
        return (getattr(self, "_old", None) is not None and self._swapped_at is not None
                and now - self._swapped_at < self.FADE_S)

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        old = getattr(self, "_old", None)
        if old is None:
            return
        swapped = getattr(self, "_swapped_at", None)
        t = 0.0 if swapped is None else (time.monotonic() - swapped) / self.FADE_S
        if t >= 1.0:
            self._old = None
            return
        p = QPainter(self)
        p.setOpacity(1.0 - _ease_out(t))
        p.drawPixmap(0, 0, old)
        p.end()

    def _got(self, url: str, img) -> None:
        if url != self._url or img is None or img.isNull():
            return
        px = self.px
        pix = QPixmap.fromImage(img).scaled(px, px, Qt.KeepAspectRatioByExpanding,
                                            Qt.SmoothTransformation)
        if self.round or self.radius:
            shaped = QPixmap(px, px)
            shaped.fill(Qt.transparent)
            p = QPainter(shaped)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            if self.round:
                path.addEllipse(QRectF(0, 0, px, px))
            else:
                path.addRoundedRect(QRectF(0, 0, px, px), self.radius, self.radius)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            pix = shaped
        self.setPixmap(pix)
        self._swapped_at = time.monotonic()
        win = self.window()
        if hasattr(win, "changed"):
            win.changed.emit()


class _Segments(QWidget):
    """a row of big buttons, one of them picked."""

    picked = Signal(object)

    def __init__(self, ctx, values: list[tuple[object, str]], height: int = 50) -> None:
        super().__init__()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[tuple[object, QPushButton]] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for value, text in values:
            b = QPushButton(ctx.theme.case(text))
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setFixedHeight(height)
            b.clicked.connect(lambda _c=False, v=value: self.picked.emit(v))
            self.group.addButton(b)
            row.addWidget(b)
            self.buttons.append((value, b))
        row.addStretch(1)

    def select(self, value) -> None:
        for v, b in self.buttons:
            b.setChecked(v == value)

    def restyle(self, look: _Look, px: int = 19) -> None:
        for _v, b in self.buttons:
            look.apply(b, px, True)


# ---------------------------------------------------------------------------
# rows, tiles, lists
# ---------------------------------------------------------------------------

class _SongRow(QFrame):
    """cover, title, artist, length; tap plays it now. "radio" starts a
    radio from it, "next" queues it after the current song."""

    activated = Signal(object)
    queued = Signal(object)
    radioed = Signal(object)

    def __init__(self, ctx, track, look: _Look) -> None:
        super().__init__()
        self.track = track
        self.setObjectName("row")
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedHeight(ROW_H)
        self.cover = _Cover(ctx, ROW_COVER)
        self.cover.radius = look.radius(8)
        self.cover.load(track.art_url)
        title, sub = QLabel(), QLabel()
        sub.setObjectName("dim")
        look.apply(title, 22, True)
        look.apply(sub, 18)
        length = QLabel(_fmt(track.duration) if track.duration else "")
        length.setObjectName("dim")
        look.apply(length, 18, role="numbers")
        _passive(title, sub, length)
        _elide(title, ctx.theme.case(track.title), ROW_TEXT_W)
        _elide(sub, ctx.theme.case(" · ".join(x for x in (track.artists, track.album) if x)),
               ROW_TEXT_W)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(title)
        text.addWidget(sub)
        nxt = QPushButton(ctx.theme.case("next"))
        nxt.setObjectName("small")
        look.apply(nxt, 18, True)
        nxt.setFixedSize(96, 52)
        nxt.clicked.connect(lambda: self.queued.emit(self.track))
        radio = QPushButton(ctx.theme.case("radio"))
        radio.setObjectName("small")
        look.apply(radio, 18, True)
        radio.setFixedSize(96, 52)
        radio.clicked.connect(lambda: self.radioed.emit(self.track))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(16)
        lay.addWidget(self.cover)
        lay.addLayout(text, 1)
        lay.addWidget(length)
        lay.addWidget(radio)
        lay.addWidget(nxt)
        self._pressed = False

    def mousePressEvent(self, e) -> None:
        self._pressed = True

    def mouseReleaseEvent(self, e) -> None:
        if self._pressed and self.rect().contains(e.position().toPoint()):
            self.activated.emit(self.track)
        self._pressed = False


class _Tile(QFrame):
    """a cover and two lines; tap opens it."""

    activated = Signal(object)

    def __init__(self, ctx, item, look: _Look) -> None:
        super().__init__()
        self.item = item
        self.setObjectName("tile")
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedWidth(TILE_W)
        self.cover = _Cover(ctx, TILE_COVER)
        self.cover.round = item.kind == "artist" and look.modern
        self.cover.radius = look.radius(12)
        self.cover.load(item.art_url)
        title, sub = QLabel(), QLabel()
        sub.setObjectName("dim")
        look.apply(title, 19, True)
        look.apply(sub, 16)
        _passive(title, sub)
        _elide(title, ctx.theme.case(item.title), TILE_COVER)
        _elide(sub, ctx.theme.case(item.subtitle), TILE_COVER)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 9, 9, 9)
        lay.setSpacing(4)
        lay.addWidget(self.cover, 0, Qt.AlignHCenter)
        lay.addWidget(title)
        lay.addWidget(sub)
        self._pressed = False

    def mousePressEvent(self, e) -> None:
        self._pressed = True

    def mouseReleaseEvent(self, e) -> None:
        if self._pressed and self.rect().contains(e.position().toPoint()):
            self.activated.emit(self.item)
        self._pressed = False


class _List(QScrollArea):
    """a vertical list of song rows, or a grid of tiles; scrolled by
    grabbing it (see Forwarder) or with the stick."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._content: QWidget | None = None

    def _fresh(self) -> QWidget:
        content = QWidget()
        content.setObjectName("listcontent")
        self.setWidget(content)       # the old content goes with it
        self._content = content
        self.verticalScrollBar().setValue(0)
        return content

    def songs(self, ctx, tracks, look: _Look, on_play, on_next, on_radio=None) -> None:
        col = QVBoxLayout(self._fresh())
        col.setContentsMargins(0, 0, 14, 0)
        col.setSpacing(6)
        for i, t in enumerate(tracks):
            row = _SongRow(ctx, t, look)
            row.activated.connect(lambda _t, i=i: on_play(i))
            row.queued.connect(on_next)
            row.radioed.connect(on_radio or ctx.music.radio)
            col.addWidget(row)
        col.addStretch(1)

    def tiles(self, ctx, items, look: _Look, on_open, columns: int = 5) -> None:
        grid = QGridLayout(self._fresh())
        grid.setContentsMargins(0, 0, 14, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for i, item in enumerate(items):
            tile = _Tile(ctx, item, look)
            tile.activated.connect(on_open)
            grid.addWidget(tile, i // columns, i % columns, Qt.AlignTop | Qt.AlignLeft)
        grid.setRowStretch(len(items) // columns + 1, 1)
        grid.setColumnStretch(columns, 1)

    def message(self, text: str) -> None:
        col = QVBoxLayout(self._fresh())
        label = QLabel(text)
        label.setObjectName("dim")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        win = self.window()
        look = getattr(win, "look", None)
        if look is not None:
            look.apply(label, 22)
        col.addStretch(1)
        col.addWidget(label)
        col.addStretch(2)


class _Detail(QWidget):
    """an album, playlist or artist opened: back, its cover and name, play,
    shuffle, then its songs."""

    back = Signal()

    def __init__(self, ctx, owner) -> None:
        super().__init__()
        self.ctx, self.owner = ctx, owner
        self.tracks: list = []
        self._gen = 0
        self.back_btn = QPushButton()
        self.back_btn.setObjectName("small")
        self.back_btn.setFixedSize(110, 56)
        self.back_btn.clicked.connect(self.back)
        self.cover = _Cover(ctx, 96)
        self.title = QLabel()
        self.sub = QLabel()
        self.sub.setObjectName("dim")
        self.play_btn = QPushButton()
        self.shuffle_btn = QPushButton()
        for b in (self.play_btn, self.shuffle_btn):
            b.setObjectName("small")
            b.setFixedSize(130, 56)
        self.play_btn.clicked.connect(lambda: ctx.music.play(self.tracks))
        self.shuffle_btn.clicked.connect(lambda: ctx.music.play(self.tracks, shuffle=True))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.title)
        text.addWidget(self.sub)
        head = QHBoxLayout()
        head.setSpacing(16)
        head.addWidget(self.back_btn, 0, Qt.AlignVCenter)
        head.addWidget(self.cover)
        head.addLayout(text, 1)
        head.addWidget(self.play_btn, 0, Qt.AlignVCenter)
        head.addWidget(self.shuffle_btn, 0, Qt.AlignVCenter)
        self.list = _List()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addLayout(head)
        lay.addWidget(self.list, 1)

    def restyle(self, look: _Look) -> None:
        t = self.ctx.theme
        self.back_btn.setText(t.case("back"))
        self.play_btn.setText(t.case("play"))
        self.shuffle_btn.setText(t.case("shuffle"))
        for b in (self.back_btn, self.play_btn, self.shuffle_btn):
            look.apply(b, 19, True)
        look.apply(self.title, 30, True, role="title")
        look.apply(self.sub, 19)

    def show_item(self, item, look: _Look) -> None:
        self._gen += 1
        gen = self._gen
        t = self.ctx.theme
        self.cover.round = item.kind == "artist" and look.modern
        self.cover.radius = look.radius(10)
        self.cover.load(item.art_url)
        _elide(self.title, t.case(item.title), 420)
        _elide(self.sub, t.case(item.subtitle or item.kind), 420)
        self.tracks = []
        self.list.message(t.case("loading"))
        self.ctx.music.open(item, lambda tracks: self._got(gen, tracks, look),
                            lambda exc: self._failed(gen, exc))

    def show_tracks(self, title: str, tracks: list, look: _Look, back: bool = False) -> None:
        """a song list with no item behind it (liked songs)."""
        self._gen += 1
        self.back_btn.setVisible(back)
        self.cover.setVisible(False)
        _elide(self.title, self.ctx.theme.case(title), 420)
        _elide(self.sub, self.ctx.theme.case(f"{len(tracks)} songs"), 420)
        self._got(self._gen, tracks, look)

    def _got(self, gen: int, tracks: list, look: _Look) -> None:
        if gen != self._gen:
            return
        self.tracks = tracks
        if not tracks:
            self.list.message(self.ctx.theme.case("no songs here"))
        else:
            self.list.songs(self.ctx, tracks, look,
                            on_play=lambda i: self.ctx.music.play(self.tracks, start=i),
                            on_next=self.ctx.music.play_next)
        self.owner.changed.emit()

    def _failed(self, gen: int, exc) -> None:
        if gen == self._gen:
            self.list.message(self.ctx.theme.case(f"couldn't load it: {exc}"[:120]))
            self.owner.changed.emit()


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

class _FadeLabel(QLabel):
    """a label that can fade. a QGraphicsOpacityEffect did this before,
    but rendered into the panel's image on a scaled (hidpi) desktop the
    effect drew at twice the label's position: the sung line showed up
    on the repeat button (2026-10-19)."""

    def __init__(self) -> None:
        super().__init__()
        self.opacity = 1.0

    def set_opacity(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self.opacity) > 1e-3:
            self.opacity = value
            self.update()

    def paintEvent(self, e) -> None:
        if self.opacity >= 0.999:
            super().paintEvent(e)
            return
        p = QPainter(self)
        p.setOpacity(self.opacity)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(self.contentsRect(), int(self.alignment()), self.text())
        p.end()


class _NowPlaying(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self._dragging = False

        self.cover = _Cover(ctx, COVER_PX)
        self.title = QLabel()
        self.meta = QLabel()
        self.meta.setObjectName("dim")
        self.next = QLabel()
        self.next.setObjectName("dim")
        self.lyric_prev = QLabel()
        self.lyric_prev.setObjectName("dim")
        self.lyric_now = _FadeLabel()
        self.lyric_next = QLabel()
        self.lyric_next.setObjectName("dim")
        for lab in (self.title, self.meta, self.next, self.lyric_prev, self.lyric_now,
                    self.lyric_next):
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        text = QVBoxLayout()
        text.setSpacing(6)
        text.addWidget(self.title)
        text.addWidget(self.meta)
        text.addWidget(self.next)
        text.addStretch(1)
        text.addWidget(self.lyric_prev)
        text.addWidget(self.lyric_now)
        text.addWidget(self.lyric_next)
        top = QHBoxLayout()
        top.setSpacing(28)
        top.addWidget(self.cover, 0, Qt.AlignTop)
        top.addLayout(text, 1)

        self.elapsed = QLabel("0:00")
        self.total = QLabel("0:00")
        for lab in (self.elapsed, self.total):
            lab.setObjectName("dim")
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setFixedHeight(44)
        self.seek.sliderPressed.connect(self._seek_start)
        self.seek.sliderReleased.connect(self._seek_end)
        self.vol_icon = QLabel()
        self.vol_icon.setFixedSize(34, 34)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedHeight(44)
        self.volume.valueChanged.connect(lambda v: ctx.playback.set_volume(v))
        # speed: a slider over tide's range in its 0.05 steps (0.5×–2.0×);
        # the number beside it resets to 1.0×
        self.speed_icon = QLabel()
        self.speed_icon.setFixedSize(34, 34)
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(10, 40)
        self.speed_slider.setFixedHeight(44)
        self.speed_slider.valueChanged.connect(lambda v: ctx.playback.set_speed(v * 0.05))
        self.speed = QPushButton("1.0×")
        self.speed.setObjectName("speed")
        self.speed.setFixedSize(84, 48)
        self.speed.clicked.connect(lambda: ctx.playback.set_speed(1.0))
        seek_row = QHBoxLayout()
        seek_row.setSpacing(12)
        seek_row.addWidget(self.elapsed)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.total)
        mix_row = QHBoxLayout()
        mix_row.setSpacing(12)
        mix_row.addWidget(self.vol_icon)
        mix_row.addWidget(self.volume, 1)
        mix_row.addSpacing(30)
        mix_row.addWidget(self.speed_icon)
        mix_row.addWidget(self.speed_slider, 1)
        mix_row.addWidget(self.speed)

        self.shuffle = _IconButton("shuffle", "shuffle", CONTROL_PX)
        self.prev = _IconButton("prev", "prev", CONTROL_PX)
        self.play = _IconButton("play", "play", PLAY_PX, round_=True)
        self.nxt = _IconButton("next", "next", CONTROL_PX)
        self.repeat = _IconButton("repeat", "repeat", CONTROL_PX)
        self.like = _IconButton("like_off", "like_off", CONTROL_PX)
        self.ring = _IconButton("ring", None, CONTROL_PX)
        self.buttons = (self.shuffle, self.prev, self.play, self.nxt, self.repeat, self.like,
                        self.ring)
        pb, q = ctx.playback, ctx.queue
        self.shuffle.clicked.connect(lambda: q.set_shuffle(not q.shuffle()))
        self.prev.clicked.connect(pb.previous)
        self.play.clicked.connect(pb.toggle)
        self.nxt.clicked.connect(pb.next)
        self.repeat.clicked.connect(q.cycle_repeat)
        self.like.clicked.connect(lambda: pb.like(not pb.liked()))
        controls = QHBoxLayout()
        controls.setSpacing(22)
        controls.addStretch(1)
        for b in self.buttons:
            controls.addWidget(b, 0, Qt.AlignVCenter)
        controls.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addLayout(seek_row)
        lay.addLayout(mix_row)
        lay.addLayout(controls)

        # modern: the sung line fades in when it changes
        self.lyric_changed_at = -1e9
        self.animate = False

    def restyle(self, look: _Look) -> None:
        self.cover.radius = look.radius(16)
        look.apply(self.title, 38, True, role="title")
        look.apply(self.meta, 24)
        look.apply(self.next, 19)
        look.apply(self.lyric_prev, 21)
        look.apply(self.lyric_now, 27, True)
        look.apply(self.lyric_next, 21)
        for lab in (self.elapsed, self.total):
            look.apply(lab, 20, role="numbers")
        look.apply(self.speed, 22, True, role="numbers")
        self.vol_icon.setPixmap(look.icon("volume", 34, look.dim))
        self.speed_icon.setPixmap(look.icon("speed", 34, look.dim))
        for b in self.buttons:
            b.look = look
            b.paint_icon()

    def refresh(self, ring_on: bool, lyrics_on: bool) -> None:
        t, pb, q = self.ctx.theme, self.ctx.playback, self.ctx.queue
        track = pb.current()
        if track is None:
            _elide(self.title, t.case("nothing playing"), TEXT_W)
            self.meta.setText("")
            self.cover.load("")
        else:
            _elide(self.title, t.case(track.title), TEXT_W)
            _elide(self.meta, t.case(" · ".join(x for x in (track.artists, track.album) if x)),
                   TEXT_W)
            self.cover.load(track.art_url)
        up = q.up_next()
        _elide(self.next, t.case(f"next: {up.title} · {up.artists}") if up else "", TEXT_W)
        playing = pb.state() in ("playing", "loading")
        self.play.set_keys("pause" if playing else "play", "pause" if playing else "play")
        self.shuffle.set_on(q.shuffle())
        mode = q.repeat()
        key = "repeat_one" if mode == "one" else "repeat"
        self.repeat.set_keys(key, key)
        self.repeat.set_on(mode != "off")
        liked = pb.liked()
        self.like.set_keys("like_on" if liked else "like_off", "like_on" if liked else "like_off")
        self.like.set_on(liked)
        self.ring.set_on(ring_on)
        if not self.volume.isSliderDown():
            self.volume.blockSignals(True)
            self.volume.setValue(pb.volume())
            self.volume.blockSignals(False)
        rate = pb.speed()
        self.speed.setText(f"{rate:.1f}×" if abs(rate * 10 - round(rate * 10)) < 1e-3
                           else f"{rate:.2f}×")
        if not self.speed_slider.isSliderDown():
            self.speed_slider.blockSignals(True)
            self.speed_slider.setValue(int(round(rate / 0.05)))
            self.speed_slider.blockSignals(False)
        can = pb.speed_supported()
        for w in (self.speed_slider, self.speed):
            w.setEnabled(can)
        self.progress(pb.position(), pb.duration(), lyrics_on)

    def progress(self, position: float, duration: float, lyrics_on: bool) -> bool:
        """update the seek bar, times and lyric lines; True if the lyric
        lines changed (worth a redraw on their own)."""
        if not self._dragging:
            self.seek.blockSignals(True)
            self.seek.setValue(int(1000 * position / duration) if duration > 0 else 0)
            self.seek.blockSignals(False)
        self.elapsed.setText(_fmt(position))
        self.total.setText(_fmt(duration))
        t = self.ctx.theme
        prev, now, nxt = self.ctx.lyrics.lines_around(position) if lyrics_on else (None,) * 3
        before = (self.lyric_prev.text(), self.lyric_now.text(), self.lyric_next.text())
        _elide(self.lyric_prev, t.case(prev or ""), TEXT_W)
        _elide(self.lyric_now, t.case(now or ""), TEXT_W)
        _elide(self.lyric_next, t.case(nxt or ""), TEXT_W)
        changed = before != (self.lyric_prev.text(), self.lyric_now.text(),
                             self.lyric_next.text())
        if changed and self.animate and before[1] != self.lyric_now.text():
            self.lyric_changed_at = time.monotonic()
        return changed

    LYRIC_FADE_S = 0.26

    def tick(self, now: float) -> bool:
        """modern's lyric fade; True while it's still moving."""
        t = (now - self.lyric_changed_at) / self.LYRIC_FADE_S
        self.lyric_now.set_opacity(0.15 + 0.85 * _ease_out(t) if t < 1 else 1.0)
        return t < 1

    def _seek_start(self) -> None:
        self._dragging = True

    def _seek_end(self) -> None:
        self._dragging = False
        dur = self.ctx.playback.duration()
        if dur > 0:
            self.ctx.playback.seek(self.seek.value() / 1000 * dur)


class _Search(QWidget):
    """a search field (steamvr's keyboard), a hold-to-talk mic, what to
    look for, recent searches, results."""

    wants_keyboard = Signal(str)
    mic_down = Signal()
    mic_up = Signal()

    KINDS = [("songs", "songs"), ("albums", "albums"), ("artists", "artists"),
             ("playlists", "playlists")]

    def __init__(self, ctx, owner) -> None:
        super().__init__()
        self.ctx, self.owner = ctx, owner
        self.query = ""
        self.kind = "songs"
        self._gen = 0
        self.recent: list[str] = self._load_recent()

        self.field = QPushButton()
        self.field.setObjectName("field")
        self.field.setFixedHeight(66)
        self.field.clicked.connect(lambda: self.wants_keyboard.emit(self.query))
        self.mic = _IconButton("mic", None, 66)
        self.mic.setObjectName("control")
        self.mic.pressed.connect(self.mic_down)
        self.mic.released.connect(self.mic_up)
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.field, 1)
        top.addWidget(self.mic)
        top.addSpacing(76)                        # the x sits here

        self.kinds = _Segments(ctx, self.KINDS)
        self.kinds.select(self.kind)
        self.kinds.picked.connect(self._pick_kind)
        self.status = QLabel()
        self.status.setObjectName("dim")

        self.chips = QWidget()
        self.chips_row = QHBoxLayout(self.chips)
        self.chips_row.setContentsMargins(0, 0, 0, 0)
        self.chips_row.setSpacing(8)

        self.list = _List()
        self.detail = _Detail(ctx, owner)
        self.detail.back.connect(lambda: self.stack.setCurrentIndex(0) or self.owner.changed.emit())
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        rl.addWidget(self.kinds)
        rl.addWidget(self.status)
        rl.addWidget(self.chips)
        rl.addWidget(self.list, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(results)
        self.stack.addWidget(self.detail)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addLayout(top)
        lay.addWidget(self.stack, 1)

    # ---- look ----

    def restyle(self, look: _Look) -> None:
        self.look = look
        look.apply(self.field, 24)
        self.mic.look = look
        self.mic.paint_icon()
        self.kinds.restyle(look)
        look.apply(self.status, 19)
        self.detail.restyle(look)
        self._show_field()
        self._show_chips()

    def _show_field(self) -> None:
        t = self.ctx.theme
        text = self.query or t.case("search songs, albums, artists")
        self.field.setProperty("empty", "false" if self.query else "true")
        self.field.style().unpolish(self.field)
        self.field.style().polish(self.field)
        self.field.setText("  " + text)

    def _show_chips(self) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        look = getattr(self, "look", None)
        show = not self.query and bool(self.recent)
        self.chips.setVisible(show)
        if not show:
            return
        for q in self.recent[:5]:
            b = QPushButton(self.ctx.theme.case(q if len(q) <= 22 else q[:21] + "…"))
            b.setObjectName("chip")
            b.setFixedHeight(48)
            if look is not None:
                look.apply(b, 18, True)
            b.clicked.connect(lambda _c=False, q=q: self.submit(q))
            self.chips_row.addWidget(b)
        self.chips_row.addStretch(1)

    # ---- recent ----

    def _recent_path(self):
        return self.ctx.paths.data_dir / "recent-searches.json"

    def _load_recent(self) -> list[str]:
        try:
            data = json.loads(self._recent_path().read_text(encoding="utf-8"))
            # "[blank_audio]": silence once got searched for (2026-10-19)
            return [str(q) for q in data if not str(q).startswith("[")][:RECENT_MAX]
        except Exception:  # noqa: BLE001 - none yet
            return []

    def _remember(self, query: str) -> None:
        self.recent = [query] + [q for q in self.recent if q != query]
        self.recent = self.recent[:RECENT_MAX]
        try:
            self._recent_path().write_text(json.dumps(self.recent), encoding="utf-8")
        except OSError:
            pass

    # ---- searching ----

    def typing(self, text: str) -> None:
        """live text from steamvr's keyboard, before enter."""
        self.query = text
        self._show_field()
        self.owner.changed.emit()

    def say(self, text: str) -> None:
        self.status.setText(self.ctx.theme.case(text))
        self.owner.changed.emit()

    def _pick_kind(self, kind: str) -> None:
        self.kind = kind
        if self.query:
            self.submit(self.query)

    def submit(self, query: str) -> None:
        query = " ".join(query.split())
        self.stack.setCurrentIndex(0)
        self.query = query
        self._show_field()
        self._show_chips()
        if not query:
            self.list.message("")
            self.say("")
            return
        self._remember(query)
        self._gen += 1
        gen = self._gen
        self.say("searching")
        self.list.message("")
        self.ctx.music.search(query, self.kind, lambda res: self._got(gen, res),
                              lambda exc: self._failed(gen, exc))

    def _got(self, gen: int, results: list) -> None:
        if gen != self._gen:
            return
        results = [r for r in results if r is not None]
        look = self.look
        if not results:
            self.say("")
            self.list.message(self.ctx.theme.case("nothing found"))
        elif self.kind == "songs":
            self.say("")
            self.list.songs(self.ctx, results, look,
                            on_play=lambda i: self.ctx.music.play_now(results[i]),
                            on_next=self.ctx.music.play_next)
        else:
            self.say("")
            self.list.tiles(self.ctx, results, look, on_open=self._open)
        self.owner.changed.emit()

    def _failed(self, gen: int, exc) -> None:
        if gen == self._gen:
            self.say(f"couldn't search: {exc}"[:110])
            self.list.message("")

    def _open(self, item) -> None:
        self.detail.back_btn.setVisible(True)
        self.detail.cover.setVisible(True)
        self.detail.show_item(item, self.look)
        self.stack.setCurrentIndex(1)
        self.owner.changed.emit()


class _Library(QWidget):
    """playlists, liked songs, albums, artists."""

    SECTIONS = [("playlists", "playlists"), ("liked", "liked"), ("albums", "albums"),
                ("artists", "artists")]
    FRESH_S = 300.0

    def __init__(self, ctx, owner) -> None:
        super().__init__()
        self.ctx, self.owner = ctx, owner
        self.section = "playlists"
        self._cache: dict[str, tuple[float, list]] = {}
        self._gen = 0
        self.sections = _Segments(ctx, self.SECTIONS)
        self.sections.select(self.section)
        self.sections.picked.connect(self.show_section)
        self.status = QLabel()
        self.status.setObjectName("dim")
        self.list = _List()
        self.liked = _Detail(ctx, owner)
        self.detail = _Detail(ctx, owner)
        self.detail.back.connect(self._back)
        self.body = QStackedWidget()
        self.body.addWidget(self.list)       # 0: grids
        self.body.addWidget(self.liked)      # 1: liked songs
        self.body.addWidget(self.detail)     # 2: something opened
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(self.sections)
        lay.addWidget(self.status)
        lay.addWidget(self.body, 1)
        self._loaded_once = False

    def restyle(self, look: _Look) -> None:
        self.look = look
        self.sections.restyle(look)
        look.apply(self.status, 19)
        self.liked.restyle(look)
        self.detail.restyle(look)

    def opened(self) -> None:
        """the tab came up: load what's showing if it's not fresh."""
        if not self._loaded_once:
            self._loaded_once = True
            self.show_section(self.section)

    def _back(self) -> None:
        self.body.setCurrentIndex(1 if self.section == "liked" else 0)
        self.owner.changed.emit()

    def show_section(self, section: str) -> None:
        self.section = section
        self.sections.select(section)
        self._gen += 1
        gen = self._gen
        cached = self._cache.get(section)
        if cached is not None and time.monotonic() - cached[0] < self.FRESH_S:
            self._show(gen, cached[1])
            return
        self.status.setText(self.ctx.theme.case("loading"))
        self.body.setCurrentIndex(0)
        self.list.message("")
        self.ctx.music.library(section, lambda items: self._got(gen, section, items),
                               lambda exc: self._failed(gen, exc))
        self.owner.changed.emit()

    def _got(self, gen: int, section: str, items: list) -> None:
        self._cache[section] = (time.monotonic(), [i for i in items if i is not None])
        if gen == self._gen:
            self._show(gen, self._cache[section][1])

    def _show(self, gen: int, items: list) -> None:
        t = self.ctx.theme
        self.status.setText("")
        if self.section == "liked":
            self.liked.show_tracks("liked songs", items, self.look)
            self.body.setCurrentIndex(1)
        elif not items:
            self.body.setCurrentIndex(0)
            self.list.message(t.case(f"no {self.section} in your library yet"))
        else:
            self.body.setCurrentIndex(0)
            self.list.tiles(self.ctx, items, self.look, on_open=self._open)
        self.owner.changed.emit()

    def _failed(self, gen: int, exc) -> None:
        if gen == self._gen:
            self.status.setText(self.ctx.theme.case(f"couldn't load it: {exc}"[:110]))
            self.owner.changed.emit()

    def _open(self, item) -> None:
        self.detail.back_btn.setVisible(True)
        self.detail.cover.setVisible(True)
        self.detail.show_item(item, self.look)
        self.body.setCurrentIndex(2)
        self.owner.changed.emit()


class _Settings(QWidget):
    """the plugin's own settings as big segmented buttons, built from
    plugin.toml so it always matches the desktop dialog."""

    changed = Signal(str, object)     # key, value

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.rows: dict[str, tuple[QLabel, _Segments]] = {}
        cols = QHBoxLayout(self)
        cols.setContentsMargins(0, 0, 90, 0)
        cols.setSpacing(36)
        for keys in VR_SETTINGS_COLUMNS:
            col = QVBoxLayout()
            col.setSpacing(10)
            for key in keys:
                self._row(col, key)
            col.addStretch(1)
            cols.addLayout(col)
        cols.addStretch(1)

    def _row(self, col: QVBoxLayout, key: str) -> None:
        ctx = self.ctx
        d = ctx.settings.desc(key)
        label = QLabel(ctx.theme.case(d.label))
        label.setObjectName("dim")
        if d.kind == "action":
            # a single button: values of (None, its label)
            label.setText("")
            values = [(None, d.label)]
        elif d.kind == "bool":
            values = [(True, "on"), (False, "off")]
        else:
            values = [(v, lab.replace(" of looking away", "")) for v, lab in d.choices]
        seg = _Segments(ctx, values, height=46)
        if d.kind == "action":
            for _v, b in seg.buttons:
                b.setCheckable(False)
        seg.picked.connect(lambda v, k=key: self.changed.emit(k, v))
        item = QVBoxLayout()
        item.setSpacing(2)
        if label.text():
            item.addWidget(label)
        item.addWidget(seg, 0, Qt.AlignLeft)
        col.addLayout(item)
        self.rows[key] = (label, seg)

    def restyle(self, look: _Look) -> None:
        for label, seg in self.rows.values():
            look.apply(label, 21)
            seg.restyle(look)

    def refresh(self) -> None:
        for key, (_l, seg) in self.rows.items():
            if self.ctx.settings.desc(key).kind != "action":
                seg.select(self.ctx.settings[key])


# ---------------------------------------------------------------------------
# the panel
# ---------------------------------------------------------------------------

class VrPanel(QWidget):
    """the whole panel: four pages, the tab bar, the x."""

    close_requested = Signal()
    changed = Signal()                 # something on it looks different now
    setting_changed = Signal(str, object)

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.ring_on = False
        self.lyrics_on = True
        self.animate = False          # modern with motion on: see set_animate
        self._ripples: list[tuple[QPointF, float]] = []
        self._slide: tuple[QPixmap, float, int] | None = None
        self.setObjectName("vrpanel")
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(*PANEL_PX)

        self.now = _NowPlaying(ctx)
        self.search = _Search(ctx, self)
        self.library = _Library(ctx, self)
        self.settings_page = _Settings(ctx)
        self.settings_page.changed.connect(self.setting_changed)
        self.pages = {"now": self.now, "search": self.search, "library": self.library,
                      "settings": self.settings_page}
        self.stack = QStackedWidget()
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.tabs: dict[str, QPushButton] = {}
        self._tab_icons: dict[str, str] = {}
        bar = QHBoxLayout()
        bar.setSpacing(14)
        for key, icon_key, label in TABS:
            b = QPushButton(ctx.theme.case(label))
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setFixedHeight(72)
            b.clicked.connect(lambda _c=False, k=key: self.show_tab(k))
            self.tabs[key] = b
            self._tab_icons[key] = icon_key
            bar.addWidget(b, 1)

        self.close_btn = QPushButton("x")
        self.close_btn.setObjectName("close")
        self.close_btn.setParent(self)
        self.close_btn.clicked.connect(self.close_requested)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(34, 30, 34, 24)
        lay.setSpacing(16)
        lay.addWidget(self.stack, 1)
        lay.addLayout(bar)

        self.restyle()
        self.show_tab("now")

    def current_tab(self) -> str:
        for key, page in self.pages.items():
            if self.stack.currentWidget() is page:
                return key
        return "now"

    def set_animate(self, on: bool, durations: dict | None = None) -> None:
        """modern's motion (and only with motion on): pages slide, presses
        ripple, covers and lyric lines fade. ``durations`` in seconds from
        tide's motion profile, so vr and the desktop share one timing."""
        self.animate = on
        d = durations or {}
        self.SLIDE_S = d.get("short", 0.26)
        self.RIPPLE_S = d.get("med", 0.42)
        for cover in self.findChildren(_Cover):
            cover.animate = on
        _Cover.animate = on
        self.now.animate = on
        if not on:
            self._ripples.clear()
            self._slide = None

    SLIDE_S = 0.26
    RIPPLE_S = 0.42

    def ripple(self, pos: QPointF) -> None:
        if self.animate:
            self._ripples.append((pos, time.monotonic()))

    def animating(self) -> bool:
        """still moving: keep sending frames."""
        now = time.monotonic()
        moving = self._slide is not None and now - self._slide[1] < self.SLIDE_S
        moving |= any(now - t < self.RIPPLE_S for _p, t in self._ripples)
        moving |= self.now.tick(now)
        moving |= any(c.fading(now) for c in self.findChildren(_Cover))
        return moving

    def show_tab(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        old = self.stack.currentWidget()
        if self.animate and old is not page and self.isVisible():
            order = list(self.pages.values())
            direction = 1 if order.index(page) > order.index(old) else -1
            self._slide = (self.stack.grab(), time.monotonic(), direction)
        self.stack.setCurrentWidget(page)
        for k, b in self.tabs.items():
            b.setChecked(k == key)
        if key == "settings":
            self.settings_page.refresh()
        elif key == "library":
            self.library.opened()
        self.changed.emit()

    # ---- look ----

    def restyle(self) -> None:
        look = _Look(self.ctx)
        self.look = look
        r = look.radius(26)
        br = look.radius(16)
        self.setStyleSheet(f"""
            QWidget#vrpanel {{ background: {look.bg}; border: 3px solid {look.border};
                               border-radius: {r}px; }}
            QWidget#listcontent, QScrollArea, QScrollArea > QWidget > QWidget {{
                background: transparent; }}
            QLabel {{ color: {look.fg}; background: transparent; }}
            QLabel#dim {{ color: {look.dim}; }}
            QLabel#cover {{ background: {look.hover}; border-radius: {br}px; }}
            QPushButton {{ color: {look.fg}; background: transparent; border: 3px solid {look.border};
                           border-radius: {br}px; }}
            QPushButton:hover {{ background: {look.hover}; }}
            QPushButton:pressed {{ background: {look.accent}; color: {look.bg};
                                   border-color: {look.accent}; }}
            QPushButton:disabled {{ border-color: {look.hover}; color: {look.hover}; }}
            QPushButton[on="true"] {{ border-color: {look.accent}; }}
            QPushButton#round {{ border-radius: {PLAY_PX // 2 if look.modern else 0}px;
                                 background: {look.fg if look.modern else 'transparent'}; }}
            QPushButton#close {{ min-width: 60px; max-width: 60px; min-height: 60px;
                                 max-height: 60px; }}
            QPushButton#small, QPushButton#chip {{ border-color: {look.dim}; padding: 0 14px; }}
            QPushButton#speed {{ border-color: transparent; }}
            QPushButton#field {{ text-align: left; border-color: {look.dim};
                                 background: {look.bg_alt}; }}
            QPushButton#field[empty="true"] {{ color: {look.dim}; }}
            QPushButton#tab {{ border-color: {look.dim}; color: {look.dim}; }}
            QPushButton#tab:checked {{ border-color: {look.accent}; color: {look.fg};
                                       background: {look.hover}; }}
            QPushButton#segment {{ padding: 0 18px; border-color: {look.dim}; color: {look.dim}; }}
            QPushButton#segment:checked {{ border-color: {look.accent}; color: {look.fg};
                                           background: {look.hover}; }}
            QFrame#row, QFrame#tile {{ background: transparent; border: 2px solid transparent;
                                       border-radius: {look.radius(12)}px; }}
            QFrame#row:hover, QFrame#tile:hover {{ background: {look.hover};
                                                   border-color: {look.dim}; }}
            QSlider::groove:horizontal {{ height: 10px; background: {look.hover};
                                          border-radius: {look.radius(5)}px; }}
            QSlider::sub-page:horizontal {{ background: {look.accent};
                                            border-radius: {look.radius(5)}px; }}
            QSlider::handle:horizontal {{ width: 28px; margin: -13px 0; background: {look.fg};
                                          border-radius: {look.radius(14)}px; }}
            QScrollBar:vertical {{ width: 8px; background: transparent; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {look.dim}; min-height: 40px;
                                           border-radius: {look.radius(4)}px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
        """)
        # its size lives in its own stylesheet: tide's theme padding beats
        # setFixedSize on a polished button
        look.apply(self.close_btn, 26, True, "min-width: 60px; max-width: 60px; "
                   "min-height: 60px; max-height: 60px; padding: 0;")
        self.close_btn.setFixedSize(60, 60)
        self.close_btn.move(self.width() - 34 - 60, 30)
        self.close_btn.raise_()
        self.now.restyle(look)
        self.now.play.look = _PlayLook(look) if look.modern else look
        self.now.play.paint_icon()
        self.search.restyle(look)
        self.library.restyle(look)
        self.settings_page.restyle(look)
        for key, b in self.tabs.items():
            look.apply(b, 20, True)
            pix = look.icon(self._tab_icons[key], 30, look.fg)
            if pix is not None:
                b.setIcon(QIcon(pix))
                b.setIconSize(QSize(30, 30))
        self.refresh()

    # ---- content ----

    def refresh(self) -> None:
        self.now.refresh(self.ring_on, self.lyrics_on)
        if self.stack.currentWidget() is self.settings_page:
            self.settings_page.refresh()
        self.changed.emit()

    def progress(self) -> bool:
        pb = self.ctx.playback
        return self.now.progress(pb.position(), pb.duration(), self.lyrics_on)

    def frame(self) -> QImage:
        """the panel as an image, see-through outside its border. render()
        fills the window background first by default, which painted the
        rounded corners solid black."""
        img = QImage(*PANEL_PX, QImage.Format_RGBA8888)
        img.fill(Qt.transparent)
        self.render(img, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        if self.animate:
            self._paint_motion(img)
        return img

    def _paint_motion(self, img: QImage) -> None:
        now = time.monotonic()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        if self._slide is not None:
            old, start, direction = self._slide
            t = (now - start) / self.SLIDE_S
            if t >= 1.0:
                self._slide = None
            else:
                e = _ease_out(t)
                r = self.stack.geometry()
                new = self.stack.grab()
                shift = 70
                p.save()
                p.setClipRect(r)
                p.fillRect(r, QColor(self.look.bg))
                p.setOpacity(1.0 - e)
                p.drawPixmap(r.x() - int(direction * shift * e), r.y(), old)
                p.setOpacity(e)
                p.drawPixmap(r.x() + int(direction * shift * (1.0 - e)), r.y(), new)
                p.restore()
        alive = []
        accent = QColor(self.look.accent)
        for pos, start in self._ripples:
            t = (now - start) / self.RIPPLE_S
            if t >= 1.0:
                continue
            alive.append((pos, start))
            e = _ease_out(t)
            c = QColor(accent)
            c.setAlphaF(0.38 * (1.0 - t))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(pos, 14 + 60 * e, 14 + 60 * e)
        self._ripples = alive
        p.end()


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
    p.setFont(_Look(ctx).font(54, True, "title"))
    p.setPen(QColor(t.token("fg", "#e6e6e6")))
    p.drawText(r, Qt.AlignCenter, t.case("tide"))
    p.end()
    return img


def back_face(ctx) -> QImage:
    """the back of the panel, for when you walk round it: tide's name in
    the title font and the versions, on the panel's own frame."""
    look = _Look(ctx)
    t = ctx.theme
    w, h = PANEL_PX
    img = QImage(w, h, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    r = look.radius(26)
    p.setPen(QPen(QColor(look.border), 3))
    p.setBrush(QColor(look.bg))
    p.drawRoundedRect(QRectF(1.5, 1.5, w - 3, h - 3), r, r)
    name = QRectF(0, h * 0.24, w, h * 0.36)
    p.setFont(look.font(150, True, "title"))
    p.setPen(QColor(look.fg))
    p.drawText(name, Qt.AlignHCenter | Qt.AlignBottom, t.case("tide"))
    # a short accent stroke under the name
    p.setPen(QPen(QColor(look.accent), 6, Qt.SolidLine, Qt.RoundCap if look.modern else Qt.FlatCap))
    y = name.bottom() + 26
    p.drawLine(int(w / 2 - 46), int(y), int(w / 2 + 46), int(y))
    p.setFont(look.font(26, False, "numbers"))
    p.setPen(QColor(look.dim))
    tide_v = getattr(ctx, "tide_version", "") or ""
    lines = [f"tide {tide_v}".strip(), f"steamvr plugin {ctx.manifest.version}"]
    p.drawText(QRectF(0, y + 30, w, 90), Qt.AlignHCenter | Qt.AlignTop,
               t.case("\n".join(lines)))
    p.end()
    return img
