"""The qt side of the vr panel: the panel widget (never shown on the
desktop, rendered into frames for steamvr), the bit that turns laser
events into qt mouse events, and the watch face.

Layout (from the user's drawing, cleaned up):

    ┌──────────────────────────────────────────────────────────────┐
    │ ┌─────────┐ title                                         [x] │
    │ │  cover  │ artist · album                                    │
    │ │         │ next: next title · artist                         │
    │ │         │   the line before                                 │
    │ │         │   the line being sung                             │
    │ └─────────┘   the next line                                   │
    │ 1:10 ━━━━━━━━━━●──────────────────── 5:20    🔈 ━━━━━●──      │
    │       ⇋     ◂◂     ( ▮▮ )     ▸▸     ↻     ♡     ◯            │
    │ ───────────────────────────────────────────────────────────── │
    │  ♪ now playing   ⌕ search   ▥ library   ⚙ settings            │
    └──────────────────────────────────────────────────────────────┘

Everything is themed through ctx.theme so it looks like tide in both
personalities. No popups, menus, combo boxes or tooltips anywhere in
here: those open real desktop windows, which don't exist in the headset.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QEnterEvent, QFont, QFontMetrics, QIcon, QImage, QMouseEvent, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from . import icons
from .vr import PANEL_PX, WATCH_PX

COVER_PX = 280
TEXT_W = 560          # the column right of the cover, for eliding
CONTROL_PX = 84
PLAY_PX = 112
ICON_PX = 44
TABS = (("now", "note", "now playing", True),
        ("search", "search", "search", False),
        ("library", "library", "library", False),
        ("settings", "settings", "settings", True))
# the settings page, in order: plugin.toml keys
VR_SETTINGS = ("watch_hand", "open_by_looking", "size", "linger", "ring", "ring_strength",
               "lyrics", "haptics")


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

    def font(self, px: int, bold: bool = False) -> QFont:
        f = QFont(self.family) if self.family else QFont()
        f.setPixelSize(px)
        f.setBold(bold)
        return f

    def icon(self, key: str, px: int, color: str) -> QPixmap | None:
        if key in icons.DRAWN:
            return icons.drawn(key, px, color, square=not self.modern)
        return self.ctx.theme.icon(key, px, color)


class _IconButton(QPushButton):
    """a control: an icon from the set (modern) or tide's glyph
    (brutalist), with an "on" state drawn in the accent."""

    def __init__(self, icon_key: str, glyph_key: str | None, px: int, round_: bool = False) -> None:
        super().__init__()
        self.icon_key, self.glyph_key = icon_key, glyph_key
        self.px = px
        self.round = round_
        self.on = False
        self.setObjectName("round" if round_ else "control")
        self.setFixedSize(px, px)
        self.look: _Look | None = None

    def set_keys(self, icon_key: str, glyph_key: str | None) -> None:
        self.icon_key, self.glyph_key = icon_key, glyph_key
        self.paint_icon()

    def set_on(self, on: bool) -> None:
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
        pix = None if use_glyph else look.icon(self.icon_key, int(self.px * 0.46), color)
        if pix is not None:
            self.setIcon(QIcon(pix))
            self.setIconSize(QSize(pix.width(), pix.height()))
            self.setText("")
        else:
            self.setIcon(QIcon())
            self.setText(look.ctx.theme.glyph(self.glyph_key) if self.glyph_key else self.icon_key)
            self.setFont(look.font(int(self.px * 0.36), True))


class _NowPlaying(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self._dragging = False
        self._art_url = ""

        self.cover = QLabel()
        self.cover.setObjectName("cover")
        self.cover.setFixedSize(COVER_PX, COVER_PX)
        self.cover.setAlignment(Qt.AlignCenter)
        self.title = QLabel()
        self.meta = QLabel()
        self.meta.setObjectName("dim")
        self.next = QLabel()
        self.next.setObjectName("dim")
        self.lyric_prev = QLabel()
        self.lyric_prev.setObjectName("dim")
        self.lyric_now = QLabel()
        self.lyric_now.setObjectName("lyric")
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
        self.vol_icon.setFixedSize(36, 36)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedSize(150, 44)
        self.volume.valueChanged.connect(lambda v: ctx.playback.set_volume(v))
        seek_row = QHBoxLayout()
        seek_row.setSpacing(14)
        seek_row.addWidget(self.elapsed)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.total)
        seek_row.addSpacing(24)
        seek_row.addWidget(self.vol_icon)
        seek_row.addWidget(self.volume)

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
        lay.setSpacing(14)
        lay.addLayout(top)
        lay.addLayout(seek_row)
        lay.addLayout(controls)

    def restyle(self, look: _Look) -> None:
        self.title.setFont(look.font(38, True))
        self.meta.setFont(look.font(24))
        self.next.setFont(look.font(19))
        self.lyric_prev.setFont(look.font(21))
        self.lyric_now.setFont(look.font(27, True))
        self.lyric_next.setFont(look.font(21))
        for lab in (self.elapsed, self.total):
            lab.setFont(look.font(21))
        self.vol_icon.setPixmap(look.icon("volume", 36, look.dim))
        for b in self.buttons:
            b.look = look
            b.paint_icon()

    def _set(self, label: QLabel, text: str) -> None:
        fm = QFontMetrics(label.font())
        label.setText(fm.elidedText(text, Qt.ElideRight, TEXT_W))

    def refresh(self, ring_on: bool, lyrics_on: bool) -> None:
        t, pb, q = self.ctx.theme, self.ctx.playback, self.ctx.queue
        track = pb.current()
        if track is None:
            self._set(self.title, t.case("nothing playing"))
            self._set(self.meta, "")
        else:
            self._set(self.title, t.case(track.title))
            self._set(self.meta, t.case(" · ".join(x for x in (track.artists, track.album) if x)))
            if track.art_url != self._art_url:
                self._art_url = track.art_url
                self.cover.clear()
                if track.art_url:
                    self.ctx.track.art(track.art_url, self._got_art)
        up = q.up_next()
        self._set(self.next, t.case(f"next: {up.title} · {up.artists}") if up else "")
        playing = pb.state() in ("playing", "loading")
        self.play.set_keys("pause" if playing else "play", "pause" if playing else "play")
        self.shuffle.set_on(q.shuffle())
        mode = q.repeat()
        self.repeat.set_keys("repeat_one" if mode == "one" else "repeat",
                             "repeat_one" if mode == "one" else "repeat")
        self.repeat.set_on(mode != "off")
        liked = pb.liked()
        self.like.set_keys("like_on" if liked else "like_off", "like_on" if liked else "like_off")
        self.like.set_on(liked)
        self.ring.set_on(ring_on)
        if not self.volume.isSliderDown():
            self.volume.blockSignals(True)
            self.volume.setValue(pb.volume())
            self.volume.blockSignals(False)
        self.progress(pb.position(), pb.duration(), lyrics_on)

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
            path.addRoundedRect(QRectF(0, 0, COVER_PX, COVER_PX), 16, 16)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            pix = rounded
        self.cover.setPixmap(pix)

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
        self._set(self.lyric_prev, t.case(prev or ""))
        self._set(self.lyric_now, t.case(now or ""))
        self._set(self.lyric_next, t.case(nxt or ""))
        return before != (self.lyric_prev.text(), self.lyric_now.text(), self.lyric_next.text())

    def _seek_start(self) -> None:
        self._dragging = True

    def _seek_end(self) -> None:
        self._dragging = False
        dur = self.ctx.playback.duration()
        if dur > 0:
            self.ctx.playback.seek(self.seek.value() / 1000 * dur)


class _Settings(QWidget):
    """the plugin's own settings as big segmented buttons, built from
    plugin.toml so it always matches the desktop dialog."""

    changed = Signal(str, object)     # key, value

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.rows: dict[str, tuple[QLabel, QButtonGroup, list]] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        for r, key in enumerate(VR_SETTINGS):
            d = ctx.settings.desc(key)
            label = QLabel(ctx.theme.case(d.label))
            label.setObjectName("dim")
            group = QButtonGroup(self)
            group.setExclusive(True)
            values = ([(True, "on"), (False, "off")] if d.kind == "bool"
                      else [(v, lab) for v, lab in d.choices])
            row = QHBoxLayout()
            row.setSpacing(8)
            buttons = []
            for value, text in values:
                b = QPushButton(ctx.theme.case(self._short(text)))
                b.setObjectName("segment")
                b.setCheckable(True)
                b.setFixedHeight(48)
                b.clicked.connect(lambda _c=False, k=key, v=value: self.changed.emit(k, v))
                group.addButton(b)
                row.addWidget(b)
                buttons.append((value, b))
            row.addStretch(1)
            grid.addWidget(label, r, 0, Qt.AlignVCenter)
            grid.addLayout(row, r, 1)
            self.rows[key] = (label, group, buttons)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(len(VR_SETTINGS), 1)

    @staticmethod
    def _short(text: str) -> str:
        return text.replace(" of looking away", "")

    def restyle(self, look: _Look) -> None:
        for label, _g, buttons in self.rows.values():
            label.setFont(look.font(21))
            for _v, b in buttons:
                b.setFont(look.font(19, True))

    def refresh(self) -> None:
        for key, (_l, _g, buttons) in self.rows.items():
            current = self.ctx.settings[key]
            for value, b in buttons:
                b.setChecked(value == current)


class VrPanel(QWidget):
    """the whole panel: pages, the tab bar, the x."""

    close_requested = Signal()
    changed = Signal()                 # something on it looks different now
    setting_changed = Signal(str, object)

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.ring_on = False
        self.lyrics_on = True
        self.setObjectName("vrpanel")
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(*PANEL_PX)

        self.now = _NowPlaying(ctx)
        self.settings_page = _Settings(ctx)
        self.settings_page.changed.connect(self.setting_changed)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.now)
        self.stack.addWidget(self.settings_page)

        self.tabs: dict[str, QPushButton] = {}
        self._tab_keys: dict[str, str] = {}
        bar = QHBoxLayout()
        bar.setSpacing(14)
        for key, icon_key, label, built in TABS:
            b = QPushButton(ctx.theme.case(label))
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setFixedHeight(72)
            b.setEnabled(built)
            b.clicked.connect(lambda _c=False, k=key: self.show_tab(k))
            self.tabs[key] = b
            self._tab_keys[key] = icon_key
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

    def show_tab(self, key: str) -> None:
        page = {"now": self.now, "settings": self.settings_page}.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for k, b in self.tabs.items():
            b.setChecked(k == key)
        if key == "settings":
            self.settings_page.refresh()
        self.changed.emit()

    # ---- look ----

    def restyle(self) -> None:
        look = _Look(self.ctx)
        self.look = look
        r = 26 if look.modern else 0
        br = 16 if look.modern else 0
        self.setStyleSheet(f"""
            QWidget#vrpanel {{ background: {look.bg}; border: 3px solid {look.border};
                               border-radius: {r}px; }}
            QLabel {{ color: {look.fg}; background: transparent; }}
            QLabel#dim {{ color: {look.dim}; }}
            QLabel#lyric {{ color: {look.fg}; }}
            QLabel#cover {{ background: {look.hover}; border-radius: {br}px; }}
            QPushButton {{ color: {look.fg}; background: transparent; border: 3px solid {look.border};
                           border-radius: {br}px; }}
            QPushButton:hover {{ background: {look.hover}; }}
            QPushButton:pressed {{ background: {look.accent}; color: {look.bg};
                                   border-color: {look.accent}; }}
            QPushButton[on="true"] {{ border-color: {look.accent}; }}
            QPushButton#round {{ border-radius: {PLAY_PX // 2 if look.modern else 0}px;
                                 background: {look.fg if look.modern else 'transparent'}; }}
            QPushButton#close {{ min-width: 60px; max-width: 60px; min-height: 60px;
                                 max-height: 60px; }}
            QPushButton#tab {{ border-color: {look.dim}; color: {look.dim}; }}
            QPushButton#tab:checked {{ border-color: {look.accent}; color: {look.fg};
                                       background: {look.hover}; }}
            QPushButton#tab:disabled {{ border-color: {look.hover}; color: {look.hover}; }}
            QPushButton#segment {{ padding: 0 18px; border-color: {look.dim}; color: {look.dim}; }}
            QPushButton#segment:checked {{ border-color: {look.accent}; color: {look.fg};
                                           background: {look.hover}; }}
            QSlider::groove:horizontal {{ height: 10px; background: {look.hover};
                                          border-radius: {5 if look.modern else 0}px; }}
            QSlider::sub-page:horizontal {{ background: {look.accent};
                                            border-radius: {5 if look.modern else 0}px; }}
            QSlider::handle:horizontal {{ width: 28px; margin: -13px 0; background: {look.fg};
                                          border-radius: {14 if look.modern else 0}px; }}
        """)
        self.close_btn.setFont(look.font(26, True))
        self.close_btn.adjustSize()
        self.close_btn.move(self.width() - 34 - 60, 30)
        self.close_btn.raise_()
        # the play button on modern: a filled disc, the icon in the background color
        self.now.restyle(look)
        self.now.play.look = _PlayLook(look) if look.modern else look
        self.now.play.paint_icon()
        self.settings_page.restyle(look)
        for key, b in self.tabs.items():
            b.setFont(look.font(20, True))
            color = look.dim if not b.isEnabled() else look.fg
            pix = look.icon(self._tab_keys[key], 30, color)
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
        img = QImage(*PANEL_PX, QImage.Format_RGBA8888)
        img.fill(Qt.transparent)
        self.render(img)
        return img


class _PlayLook(_Look):
    """modern's play button: icon in the background color on a fg disc."""

    def __init__(self, base: _Look) -> None:
        self.__dict__.update(base.__dict__)
        self.fg = base.bg
        self.accent = base.bg


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
