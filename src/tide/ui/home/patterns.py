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

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
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
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        art_cache.cache().image_loaded.connect(self._on_art)
        self._thumb_px = _scale.px(self.THUMB)
        self.setFixedSize(_scale.px(self.WIDTH), _scale.px(self.HEIGHT))
        self.setCursor(Qt.PointingHandCursor)
        if self._thumb_url:
            art_cache.cache().request(self._thumb_url, None)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        from .. import scale as _scale
        self._thumb_px = _scale.px(self.THUMB)
        self.setFixedSize(_scale.px(self.WIDTH), _scale.px(self.HEIGHT))
        self.update()

    def _on_art(self, url: str, _img) -> None:
        if url == self._thumb_url:
            self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self._payload)

    def enterEvent(self, ev) -> None:
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:
        self.update()
        super().leaveEvent(ev)

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
        if img is None:
            p.fillRect(thumb_rect, bg_alt)
            p.setPen(dim)
            p.drawRect(thumb_rect.adjusted(0, 0, -1, -1))
        else:
            p.drawPixmap(thumb_rect, QPixmap.fromImage(img).scaled(
                self._thumb_px, self._thumb_px,
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        if self.underMouse():
            p.setPen(accent)
            p.setBrush(Qt.NoBrush)
            p.drawRect(thumb_rect.adjusted(-1, -1, 0, 0))

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
    Scrolling *is* the paging — no chevron buttons to babysit."""

    item_activated = Signal(object)
    ROWS = 4

    def __init__(self, items: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(_scale.px(14))
        grid.setVerticalSpacing(_scale.px(4))
        for i, it in enumerate(items):
            row = CompactTrackRow(it.title, it.subtitle, it.thumbnail, it)
            row.clicked.connect(self.item_activated.emit)
            grid.addWidget(row, i % self.ROWS, i // self.ROWS)
        grid.setColumnStretch(grid.columnCount(), 1)
        self.setWidget(inner)
        self.setFixedHeight(
            self.ROWS * _scale.px(CompactTrackRow.HEIGHT)
            + (self.ROWS - 1) * _scale.px(4) + _scale.px(14))


class DenseGrid(QWidget):
    """Listen-again: small square tiles, up to two rows."""

    item_activated = Signal(object)
    COLS = 7
    MAX_ITEMS = 14
    THUMB = 84

    def __init__(self, items: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for i, it in enumerate(items[: self.MAX_ITEMS]):
            c = Card(it.title, it.subtitle, it.thumbnail, it,
                     circular=(it.kind == "artist"), thumb_px=self.THUMB)
            c.clicked.connect(self.item_activated.emit)
            grid.addWidget(c, i // self.COLS, i % self.COLS)
        grid.setColumnStretch(self.COLS, 1)


class Mosaic(QWidget):
    """One featured 2×2 tile + up to eight 1×1s. ``seed`` picks the
    featured item, so the arrangement reshuffles with the daily seed
    instead of always crowning items[0]."""

    item_activated = Signal(object)
    SMALL = 108
    FEATURED = 240

    def __init__(self, items: list, seed: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if not items:
            return
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        featured_idx = seed % len(items)
        featured = items[featured_idx]
        rest = [it for i, it in enumerate(items) if i != featured_idx][:8]
        big = Card(featured.title, featured.subtitle, featured.thumbnail,
                   featured, circular=(featured.kind == "artist"),
                   thumb_px=self.FEATURED)
        big.clicked.connect(self.item_activated.emit)
        grid.addWidget(big, 0, 0, 2, 2)
        for i, it in enumerate(rest):
            c = Card(it.title, it.subtitle, it.thumbnail, it,
                     circular=(it.kind == "artist"), thumb_px=self.SMALL)
            c.clicked.connect(self.item_activated.emit)
            grid.addWidget(c, i // 4, 2 + (i % 4))
        grid.setColumnStretch(6, 1)


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


class RankedList(QWidget):
    """Chart rows in two balanced columns: 1–5 left, 6–10 right."""

    item_activated = Signal(object)
    PER_COLUMN = 5

    def __init__(self, entries: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(2)
        shown = entries[: self.PER_COLUMN * 2]
        for i, e in enumerate(shown):
            it = e.item
            row = CompactTrackRow(it.title, it.subtitle, it.thumbnail, it,
                                  rank=e.rank, trend=e.trend)
            row.clicked.connect(self.item_activated.emit)
            grid.addWidget(row, i % self.PER_COLUMN, i // self.PER_COLUMN)
        grid.setColumnStretch(2, 1)


class ChipRow(QWidget):
    """Moods & genres pills, chunked into rows. Emits the MoodCategory."""

    chip_activated = Signal(object)
    PER_ROW = 6

    def __init__(self, categories: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        for start in range(0, len(categories), self.PER_ROW):
            row = QHBoxLayout()
            row.setSpacing(2)
            for cat in categories[start:start + self.PER_ROW]:
                btn = BracketButton(cat.title)
                btn.clicked.connect(lambda _=False, c=cat:
                                    self.chip_activated.emit(c))
                row.addWidget(btn)
            row.addStretch(1)
            col.addLayout(row)


class Hero(QWidget):
    """Greeting + keep-listening. All local data — renders instantly and
    for every source, which is why it leads the page."""

    resume_clicked = Signal()
    track_clicked = Signal(object)      # Track
    radio_clicked = Signal(object)      # Track
    likes_clicked = Signal()

    ART = 72

    def __init__(self, greeting: str, stats_line: str, last_track,
                 *, can_resume: bool, show_likes: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale

        title = QLabel(theming.styled_case(greeting))
        f = title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 6)
        title.setFont(f)
        title.setTextFormat(Qt.PlainText)

        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.addWidget(title, stretch=1)
        if stats_line:
            stats = QLabel(theming.styled_case(stats_line))
            stats.setProperty("class", "dim")
            stats.setTextFormat(Qt.PlainText)
            head_row.addWidget(stats, alignment=Qt.AlignBottom)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addLayout(head_row)

        if last_track is not None:
            # Only reserve the art slot when there's art to put in it — a
            # thumbnail-less history entry otherwise renders as a mute
            # 72px indent that reads like a layout bug.
            self._art = None
            self._art_url = last_track.thumbnail or ""
            if self._art_url:
                self._art_px = _scale.px(self.ART)
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
            track_lbl = QLabel(theming.styled_case(
                f"{last_track.artists} — {last_track.title}"
                if last_track.artists else last_track.title))
            track_lbl.setTextFormat(Qt.PlainText)

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

    def _set_art(self, img) -> None:
        if self._art is None:
            return
        self._art.setPixmap(QPixmap.fromImage(img).scaled(
            self._art_px, self._art_px,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def _on_art(self, url: str, img) -> None:
        if url and url == getattr(self, "_art_url", "") and img is not None:
            self._set_art(img)
