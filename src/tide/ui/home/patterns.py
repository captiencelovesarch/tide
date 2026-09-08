"""Home pattern renderers (v1.5).

Each pattern is a widget that takes shelf items (or chart entries / mood
chips) and lays them out in one of the home engine's shapes:

    tap_grid    — YT's quick-picks shape: compact rows in 4-row columns,
                  scrolled horizontally
    dense_grid  — small square tiles, two rows ("listen again")
    mosaic      — one featured 2×2 tile + 1×1s beside it, featured pick
                  seeded by day so the page reshuffles daily
    circle_row  — the existing ShelfRow with circular artist cards
    ranked_list — charts: big dim numeral + trend glyph per row
    chip_row    — moods & genres pills
    hero        — greeting + keep-listening resume card

Every item-bearing pattern emits ``item_activated(payload)`` and lets the
HomeView do the dispatching — patterns are layout, not navigation.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import theming
from .. import art_cache
from ..card import Card, ShelfRow
from ..flow import FlowLayout, ResponsiveGrid
from ..widgets import BracketButton


def _qcolor(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))


# ---------- compact row (tap grid + ranked list cells) ----------


class CompactTrackRow(QWidget):
    """Small horizontal cell: [thumb] title / subtitle. The quick-picks
    unit. Optional rank+trend prefix turns it into a chart row."""

    clicked = Signal(object)

    THUMB = 40
    WIDTH = 252
    HEIGHT = 48

    def __init__(self, title: str, subtitle: str, thumbnail_url: str, payload,
                 *, rank: int = 0, trend: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self._title = title
        self._subtitle = subtitle
        self._thumb_url = thumbnail_url or ""
        self._payload = payload
        self._rank = rank
        self._trend = trend
        self._theme = theming.manager().current_effective()
        theming.manager().theme_changed.connect(self._on_theme)
        art_cache.cache().image_loaded.connect(self._on_art)
        self._thumb_px = _scale.px(self.THUMB)
        # A ResponsiveGrid column width, once assigned; wins over WIDTH.
        self._tile_w: int | None = None
        self._hover_t = 0.0                    # see Card._set_hover
        self.setFixedSize(_scale.px(self.WIDTH), _scale.px(self.HEIGHT))
        self.setCursor(Qt.PointingHandCursor)
        if self._thumb_url:
            art_cache.cache().request(self._thumb_url, None)

    # ---- ResponsiveGrid tile protocol ----

    def set_tile_width(self, width: int) -> None:
        from .. import scale as _scale
        self._tile_w = int(width)
        self.setFixedSize(self._tile_w, _scale.px(self.HEIGHT))
        self.update()

    def tile_height(self, width: int) -> int:
        from .. import scale as _scale
        return _scale.px(self.HEIGHT)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        from .. import scale as _scale
        self._thumb_px = _scale.px(self.THUMB)
        width = self._tile_w or _scale.px(self.WIDTH)
        self.setFixedSize(width, _scale.px(self.HEIGHT))
        self.update()

    def _on_art(self, url: str, _img) -> None:
        if url == self._thumb_url:
            self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self._payload)

    def enterEvent(self, ev) -> None:
        self._set_hover(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:
        self._set_hover(False)
        super().leaveEvent(ev)

    def _set_hover(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        if getattr(self._theme, "aesthetic", "") != "modern":
            self._hover_t = target
            self.update()
            return
        from .. import motion as motion_module
        motion_module.value_lerp(
            self._hover_t, target, on_update=self._hover_frame,
            dur=motion_module.dur("micro"),
            easing=motion_module.ease("spring" if on else "out"),
            owner=self, kind="hover")

    def _hover_frame(self, v: float) -> None:
        self._hover_t = max(0.0, min(1.3, float(v)))
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        theme = self._theme
        fg = _qcolor(theme, "fg", "#e6e6e6")
        dim = _qcolor(theme, "dim", "#6f6f6f")
        accent = _qcolor(theme, "accent", "#d4b95e")
        bg_alt = _qcolor(theme, "bg_alt", "#141414")
        fm = QFontMetrics(self.font())
        modern = getattr(theme, "aesthetic", "") == "modern"
        radius = theming.effective_radius_px(theme)
        hovered = self.underMouse()
        t = self._hover_t if modern else 0.0

        # modern hover: a surface fades in under the whole row (brutalist
        # keeps its accent halo around the thumbnail, drawn after the art).
        if modern and t > 0.0:
            fill = _qcolor(theme, "surface_2", "#17ffffff")
            fill.setAlphaF(fill.alphaF() * min(1.0, t))
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(self.rect()), radius, radius)

        x = 0
        # Rank + trend prefix (charts).
        if self._rank:
            rank_w = fm.horizontalAdvance("00") + 6
            p.setPen(dim)
            p.drawText(QRect(0, 0, rank_w, self.height()),
                       Qt.AlignVCenter | Qt.AlignRight, str(self._rank))
            glyph, color = {
                "up": ("↑", accent),
                "down": ("↓", dim),
                "new": ("•", accent),
            }.get(self._trend, ("—", dim))
            p.setPen(color)
            p.drawText(QRect(rank_w + 2, 0, 12, self.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, glyph)
            x = rank_w + 16

        thumb_y = (self.height() - self._thumb_px) // 2
        thumb_rect = QRect(x, thumb_y, self._thumb_px, self._thumb_px)
        img = art_cache.cache().get(self._thumb_url) if self._thumb_url else None
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(thumb_rect), radius, radius)
        if img is None:
            if modern:
                p.setPen(Qt.NoPen)
                p.setBrush(_qcolor(theme, "surface_1", "#0dffffff"))
                p.drawPath(clip)
                p.setPen(_qcolor(theme, "outline", "#1affffff"))
                p.setBrush(Qt.NoBrush)
                p.drawPath(clip)
            else:
                p.fillRect(thumb_rect, bg_alt)
                p.setPen(dim)
                p.drawRect(thumb_rect.adjusted(0, 0, -1, -1))
        else:
            pix = QPixmap.fromImage(img).scaled(
                self._thumb_px, self._thumb_px,
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            if radius > 0:
                p.setClipPath(clip)
                p.drawPixmap(thumb_rect, pix)
                p.setClipping(False)
            else:
                p.drawPixmap(thumb_rect, pix)
        if hovered and not modern:
            p.setPen(accent)
            p.setBrush(Qt.NoBrush)
            p.drawRect(thumb_rect.adjusted(-1, -1, 0, 0))
        if modern and t > 0.0:
            # A play badge centred on the thumb: these rows always play.
            d = self._thumb_px * 0.55
            p.save()
            p.setOpacity(min(1.0, t))
            p.setPen(Qt.NoPen)
            p.setBrush(fg)
            cx, cy = thumb_rect.center().x() + 0.5, thumb_rect.center().y() + 0.5
            p.drawEllipse(QRectF(cx - d / 2, cy - d / 2, d, d))
            tri = QPainterPath()
            s = d * 0.36
            tri.moveTo(cx - s * 0.42, cy - s * 0.5)
            tri.lineTo(cx + s * 0.58, cy)
            tri.lineTo(cx - s * 0.42, cy + s * 0.5)
            tri.closeSubpath()
            p.setBrush(_qcolor(theme, "bg", "#000000"))
            p.drawPath(tri)
            p.restore()

        text_x = thumb_rect.right() + 8
        text_w = self.width() - text_x - 2
        title = fm.elidedText(theming.styled_case(self._title, theme),
                              Qt.ElideRight, text_w)
        sub = fm.elidedText(theming.styled_case(self._subtitle, theme),
                            Qt.ElideRight, text_w)
        line_h = fm.height()
        top = (self.height() - 2 * line_h) // 2
        p.setPen(fg)
        p.drawText(QRect(text_x, top, text_w, line_h),
                   Qt.AlignVCenter | Qt.AlignLeft, title)
        p.setPen(dim)
        p.drawText(QRect(text_x, top + line_h, text_w, line_h),
                   Qt.AlignVCenter | Qt.AlignLeft, sub)


# ---------- patterns ----------


class TapGrid(QScrollArea):
    """Quick-picks: 4-row columns of compact rows, scrolled horizontally.
    Scrolling *is* the paging — no chevron buttons to babysit. The
    columns are sized so a whole number of them fills the width; the rest
    run past the right edge and scroll into view a page at a time."""

    item_activated = Signal(object)
    ROWS = 4
    MIN_COL = CompactTrackRow.WIDTH
    MAX_COL = 380

    def __init__(self, items: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.grid = ResponsiveGrid(
            min_tile=_scale.px(self.MIN_COL), max_tile=_scale.px(self.MAX_COL),
            gap=_scale.px(14), row_gap=_scale.px(4), rows=self.ROWS)
        for it in items:
            row = CompactTrackRow(it.title, it.subtitle, it.thumbnail, it)
            row.clicked.connect(self.item_activated.emit)
            self.grid.add(row)
        self.setWidget(self.grid)
        self.setFixedHeight(
            self.ROWS * _scale.px(CompactTrackRow.HEIGHT)
            + (self.ROWS - 1) * _scale.px(4) + _scale.px(14))

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.grid.set_avail_width(self.viewport().width())


class DenseGrid(ResponsiveGrid):
    """Listen-again: small square tiles filling the width, wrapping as
    needed. Fourteen at most, so a wide window gets one long row and a
    narrow one gets a few short ones."""

    item_activated = Signal(object)
    MAX_ITEMS = 14
    THUMB = 84
    MAX_THUMB = 144

    def __init__(self, items: list, parent: QWidget | None = None) -> None:
        from .. import scale as _scale
        margin = 2 * _scale.px(Card.MARGIN)
        super().__init__(min_tile=_scale.px(self.THUMB) + margin,
                         max_tile=_scale.px(self.MAX_THUMB) + margin,
                         gap=_scale.px(4), parent=parent)
        for it in items[: self.MAX_ITEMS]:
            c = Card(it.title, it.subtitle, it.thumbnail, it,
                     circular=(it.kind == "artist"), thumb_px=self.THUMB)
            c.clicked.connect(self.item_activated.emit)
            self.add(c)


class Mosaic(ResponsiveGrid):
    """One featured 2×2 tile + up to eight 1×1s, on a grid whose small
    tiles fill the width. ``seed`` picks the featured item, so the
    arrangement reshuffles with the daily seed instead of always crowning
    items[0]."""

    item_activated = Signal(object)
    SMALL = 108
    MAX_SMALL = 160

    def __init__(self, items: list, seed: int = 0,
                 parent: QWidget | None = None) -> None:
        from .. import scale as _scale
        import math
        margin = 2 * _scale.px(Card.MARGIN)
        featured_idx = seed % len(items) if items else 0
        rest = [it for i, it in enumerate(items) if i != featured_idx][:8]
        # The shape is the point: two rows, the feature on the left, the
        # rest split evenly beside it. So at most 2 + half-the-rest columns,
        # and no row balancing (it would count the feature as two cells).
        super().__init__(min_tile=_scale.px(self.SMALL) + margin,
                         max_tile=_scale.px(self.MAX_SMALL) + margin,
                         gap=_scale.px(4), balance=False,
                         max_columns=2 + max(1, math.ceil(len(rest) / 2)),
                         parent=parent)
        if not items:
            return
        featured = items[featured_idx]
        big = Card(featured.title, featured.subtitle, featured.thumbnail,
                   featured, circular=(featured.kind == "artist"),
                   thumb_px=self.SMALL)
        big.clicked.connect(self.item_activated.emit)
        self.add(big, span=2)
        for it in rest:
            c = Card(it.title, it.subtitle, it.thumbnail, it,
                     circular=(it.kind == "artist"), thumb_px=self.SMALL)
            c.clicked.connect(self.item_activated.emit)
            self.add(c)


def circle_row(items: list, on_activate) -> ShelfRow:
    """Artists as circular cards in the classic horizontal strip."""
    row = ShelfRow()
    for it in items:
        c = Card(it.title, it.subtitle, it.thumbnail, it, circular=True)
        c.clicked.connect(on_activate)
        row.add_card(c)
    row.end_with_stretch()
    return row


def shelf_row(items: list, on_activate) -> ShelfRow:
    """The v1.4 look — the fallback pattern and the whole page in
    "plain shelves" mode."""
    row = ShelfRow()
    for it in items:
        c = Card(it.title, it.subtitle, it.thumbnail, it,
                 circular=(it.kind == "artist"))
        c.clicked.connect(on_activate)
        row.add_card(c)
    row.end_with_stretch()
    return row


class RankedList(ResponsiveGrid):
    """Chart rows in two balanced columns: 1–5 left, 6–10 right. The
    columns share the width up to a comfortable line length."""

    item_activated = Signal(object)
    PER_COLUMN = 5
    MAX_COL = 480

    def __init__(self, entries: list, parent: QWidget | None = None) -> None:
        from .. import scale as _scale
        super().__init__(min_tile=_scale.px(CompactTrackRow.WIDTH),
                         max_tile=_scale.px(self.MAX_COL),
                         gap=_scale.px(24), row_gap=_scale.px(2),
                         rows=self.PER_COLUMN, parent=parent)
        for e in entries[: self.PER_COLUMN * 2]:
            it = e.item
            row = CompactTrackRow(it.title, it.subtitle, it.thumbnail, it,
                                  rank=e.rank, trend=e.trend)
            row.clicked.connect(self.item_activated.emit)
            self.add(row)


class ChipRow(QWidget):
    """Moods & genres pills, wrapping to the width. Emits the MoodCategory."""

    chip_activated = Signal(object)

    def __init__(self, categories: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        flow = FlowLayout(self, h_gap=_scale.px(4), v_gap=_scale.px(4))
        self.chips: list[BracketButton] = []
        for cat in categories:
            btn = BracketButton(cat.title)
            btn.clicked.connect(lambda _=False, c=cat:
                                self.chip_activated.emit(c))
            flow.addWidget(btn)
            self.chips.append(btn)


class Hero(QWidget):
    """Greeting + keep-listening. All local data — renders instantly and
    for every source, which is why it leads the page.

    modern: the page's one big moment — the greeting at display size, the
    art at 128px, the block on its own surface. brutalist: the bold
    greeting and 72px art it has always had, no surface."""

    resume_clicked = Signal()
    track_clicked = Signal(object)      # Track
    radio_clicked = Signal(object)      # Track
    likes_clicked = Signal()

    ART = 72
    ART_MODERN = 128

    def __init__(self, greeting: str, stats_line: str, last_track,
                 *, can_resume: bool, show_likes: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self._theme = theming.manager().current_effective()
        modern = getattr(self._theme, "aesthetic", "") == "modern"
        self._modern = modern
        self._greeting = greeting
        self._art_img = None
        self._texts: list[tuple[QLabel, str]] = []   # (label, raw) to re-case

        title = QLabel(theming.styled_case(greeting))
        self._title = title
        self._texts.append((title, greeting))
        self._face_title(modern)
        title.setTextFormat(Qt.PlainText)

        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.addWidget(title, stretch=1)
        if stats_line:
            stats = QLabel(theming.styled_case(stats_line))
            stats.setProperty("class", "dim")
            stats.setTextFormat(Qt.PlainText)
            self._texts.append((stats, stats_line))
            head_row.addWidget(stats, alignment=Qt.AlignBottom)

        col = QVBoxLayout(self)
        self._col = col
        self._face_layout(modern)
        col.addLayout(head_row)

        if last_track is not None:
            # Only reserve the art slot when there's art to put in it — a
            # thumbnail-less history entry otherwise renders as a mute
            # 72px indent that reads like a layout bug.
            self._art = None
            self._art_url = last_track.thumbnail or ""
            if self._art_url:
                self._art_px = _scale.px(self.ART_MODERN if modern else self.ART)
                self._art = QLabel()
                self._art.setFixedSize(self._art_px, self._art_px)
                self._art.setCursor(Qt.PointingHandCursor)
                self._art.mousePressEvent = (       # type: ignore[assignment]
                    lambda ev: self.track_clicked.emit(last_track))
                art_cache.cache().image_loaded.connect(self._on_art)
                img = art_cache.cache().request(self._art_url, None)
                if img is not None:
                    self._set_art(img)

            keep = QLabel(theming.styled_case("keep listening"))
            keep.setProperty("class", "dim")
            keep.setTextFormat(Qt.PlainText)
            self._texts.append((keep, "keep listening"))
            track_text = (f"{last_track.artists} — {last_track.title}"
                          if last_track.artists else last_track.title)
            track_lbl = QLabel(theming.styled_case(track_text))
            track_lbl.setTextFormat(Qt.PlainText)
            self._texts.append((track_lbl, track_text))

            btns = QHBoxLayout()
            btns.setSpacing(2)
            if can_resume:
                b = BracketButton("resume")
                b.clicked.connect(self.resume_clicked.emit)
                btns.addWidget(b)
            else:
                b = BracketButton("play")
                b.clicked.connect(lambda: self.track_clicked.emit(last_track))
                btns.addWidget(b)
            r = BracketButton("radio")
            r.clicked.connect(lambda: self.radio_clicked.emit(last_track))
            btns.addWidget(r)
            if show_likes:
                lk = BracketButton("shuffle likes")
                lk.clicked.connect(self.likes_clicked.emit)
                btns.addWidget(lk)
            btns.addStretch(1)

            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(2)
            text_col.addWidget(keep)
            text_col.addWidget(track_lbl)
            text_col.addLayout(btns)
            text_col.addStretch(1)

            resume_row = QHBoxLayout()
            resume_row.setSpacing(12)
            if self._art is not None:
                resume_row.addWidget(self._art, alignment=Qt.AlignTop)
            resume_row.addLayout(text_col, stretch=1)
            col.addLayout(resume_row)

        theming.manager().theme_changed.connect(self._on_theme)

    # ---- faces ----

    def _face_title(self, modern: bool) -> None:
        title = self._title
        # modern: size and weight come from the app sheet (QLabel.display).
        # brutalist: the bold, six points up, as its own widget sheet — a
        # setFont() loses to the app sheet's font rule on the next
        # repolish (a theme flip), a widget sheet does not.
        title.setProperty("class", "display" if modern else None)
        if modern:
            title.setStyleSheet("")
        else:
            title.setStyleSheet(
                f"font-weight: bold; font-size: {self.font().pointSize() + 6}pt;")
        st = title.style()
        st.unpolish(title)
        st.polish(title)

    def _face_layout(self, modern: bool) -> None:
        from .. import scale as _scale
        if modern:
            pad = _scale.px(16)
            self._col.setContentsMargins(pad, pad, pad, pad)
            self._col.setSpacing(_scale.px(12))
        else:
            self._col.setContentsMargins(0, 0, 0, 0)
            self._col.setSpacing(8)

    def _on_theme(self, theme) -> None:
        """Follow the live theme: text case, the personality's face, the
        art's size and corners, the surface colours."""
        from .. import scale as _scale
        self._theme = theme
        modern = getattr(theme, "aesthetic", "") == "modern"
        if modern != self._modern:
            self._modern = modern
            self._face_title(modern)
            self._face_layout(modern)
        for label, raw in self._texts:
            label.setText(theming.styled_case(raw, theme))
        art = getattr(self, "_art", None)
        if art is not None:
            self._art_px = _scale.px(self.ART_MODERN if modern else self.ART)
            art.setFixedSize(self._art_px, self._art_px)
            if self._art_img is not None:
                self._set_art(self._art_img)
        self.update()

    def _set_art(self, img) -> None:
        if self._art is None:
            return
        self._art_img = img
        pix = QPixmap.fromImage(img).scaled(
            self._art_px, self._art_px,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        radius = theming.effective_radius_px(self._theme)
        if radius > 0:
            rounded = QPixmap(pix.size())
            rounded.fill(Qt.transparent)
            painter = QPainter(rounded)
            painter.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(QRectF(rounded.rect()), radius, radius)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, pix)
            painter.end()
            pix = rounded
        self._art.setPixmap(pix)

    def paintEvent(self, ev) -> None:
        if self._modern:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            radius = theming.effective_radius_px(self._theme) * 2
            p.setPen(_qcolor(self._theme, "outline", "#1affffff"))
            p.setBrush(_qcolor(self._theme, "surface_1", "#0dffffff"))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                              radius, radius)
        super().paintEvent(ev)

    def _on_art(self, url: str, img) -> None:
        if url and url == getattr(self, "_art_url", "") and img is not None:
            self._set_art(img)
