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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import theming
from .. import art_cache, legibility
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
            rank_rect = QRect(0, 0, rank_w, self.height())
            rank = str(self._rank)
            p.setPen(legibility.text_ink(
                p, dim, rank_rect, Qt.AlignVCenter | Qt.AlignRight, rank))
            p.drawText(rank_rect, Qt.AlignVCenter | Qt.AlignRight, rank)
            glyph, color = {
                "up": ("↑", accent),
                "down": ("↓", dim),
                "new": ("•", accent),
            }.get(self._trend, ("—", dim))
            trend_rect = QRect(rank_w + 2, 0, 12, self.height())
            p.setPen(legibility.text_ink(
                p, color, trend_rect, Qt.AlignVCenter | Qt.AlignLeft, glyph))
            p.drawText(trend_rect, Qt.AlignVCenter | Qt.AlignLeft, glyph)
            x = rank_w + 16

        thumb_y = (self.height() - self._thumb_px) // 2
        thumb_rect = QRect(x, thumb_y, self._thumb_px, self._thumb_px)
        pix = art_cache.cache().scaled(
            self._thumb_url, self._thumb_px, self._thumb_px,
            self.devicePixelRatioF()) if self._thumb_url else None
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(thumb_rect), radius, radius)
        if pix is None:
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
        flags = Qt.AlignVCenter | Qt.AlignLeft
        title_rect = QRect(text_x, top, text_w, line_h)
        sub_rect = QRect(text_x, top + line_h, text_w, line_h)
        p.setPen(legibility.text_ink(p, fg, title_rect, flags, title))
        p.drawText(title_rect, flags, title)
        p.setPen(legibility.text_ink(p, dim, sub_rect, flags, sub))
        p.drawText(sub_rect, flags, sub)


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
    """One featured 2×2 tile + up to sixteen 1×1s, on a grid whose small
    tiles fill the width. Always two rows: a wide page shows more of the
    small tiles, a narrow one fewer (the rest are hidden, not wrapped).
    ``seed`` picks the featured item, so the arrangement reshuffles with
    the daily seed instead of always crowning items[0]."""

    item_activated = Signal(object)
    SMALL = 108
    MAX_SMALL = 160
    MAX_REST = 16

    def __init__(self, items: list, seed: int = 0,
                 parent: QWidget | None = None) -> None:
        from .. import scale as _scale
        import math
        margin = 2 * _scale.px(Card.MARGIN)
        featured_idx = seed % len(items) if items else 0
        rest = [it for i, it in enumerate(items) if i != featured_idx][:self.MAX_REST]
        # The shape is the point: two rows, the feature on the left, the
        # rest split evenly beside it. So at most 2 + half-the-rest columns,
        # and no row balancing (it would count the feature as two cells).
        super().__init__(min_tile=_scale.px(self.SMALL) + margin,
                         max_tile=_scale.px(self.MAX_SMALL) + margin,
                         gap=_scale.px(4), balance=False,
                         max_columns=2 + max(1, math.ceil(len(rest) / 2)),
                         max_rows=2, parent=parent)
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


class ChartsBand(QWidget):
    """top songs and the charts artists as one band: side by side once
    both fit at two columns each, stacked under each other below that.
    On their own, either one capped its rows at MAX_COL and left the
    right third of a wide page empty.

    The two lists come from different workers; each lands in its own
    half (``slot`` = "top_songs" | "charts_artists"), always top songs on
    the left, whichever answers first."""

    ORDER = ("top_songs", "charts_artists")
    SPACING = 32

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QBoxLayout
        self._box = QBoxLayout(QBoxLayout.TopToBottom, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(12)
        self._halves: dict[str, QWidget] = {}
        self._wide = False

    def set_list(self, slot: str, title: str, ranked: QWidget) -> None:
        from .. import scale as _scale
        from ..headings import Heading
        old = self._halves.pop(slot, None)
        if old is not None:
            self._box.removeWidget(old)
            old.deleteLater()
        half = QWidget()
        col = QVBoxLayout(half)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(_scale.px(10))
        heading = Heading(title)
        heading.setProperty("class", "dim")
        col.addWidget(heading)
        col.addWidget(ranked)
        col.addStretch(1)
        half.heading = heading          # type: ignore[attr-defined]
        half.ranked = ranked            # type: ignore[attr-defined]
        rank = self.ORDER.index(slot) if slot in self.ORDER else len(self.ORDER)
        index = sum(1 for s in self._halves
                    if (self.ORDER.index(s) if s in self.ORDER else 99) < rank)
        self._box.insertWidget(index, half, 1)
        self._halves[slot] = half
        self._reflow(force=True)

    def halves(self) -> list[QWidget]:
        return [self._box.itemAt(i).widget() for i in range(self._box.count())]

    def side_by_side(self) -> bool:
        return self._wide

    def _min_wide(self) -> int:
        """Both lists at two columns of the narrowest row, plus the gutter."""
        from .. import scale as _scale
        one = 2 * _scale.px(CompactTrackRow.WIDTH) + _scale.px(24)
        return 2 * one + _scale.px(self.SPACING)

    def _reflow(self, force: bool = False) -> None:
        from PySide6.QtWidgets import QBoxLayout
        from .. import scale as _scale
        wide = len(self._halves) > 1 and self.width() >= self._min_wide()
        if wide == self._wide and not force:
            return
        self._wide = wide
        self._box.setDirection(QBoxLayout.LeftToRight if wide
                               else QBoxLayout.TopToBottom)
        self._box.setSpacing(_scale.px(self.SPACING) if wide else 12)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._reflow()


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


def _fmt_minutes(m: float) -> str:
    m = int(round(m))
    if m >= 60:
        return f"{m // 60}h {m % 60:02d}m"
    return f"{m}m"


class _WeekChart(QWidget):
    """Seven bars of listening time, oldest day on the left, today on the
    right in the accent, each day's initial under its bar. modern rounds
    the bars; brutalist keeps them square."""

    BAR = 14
    GAP = 9
    HEIGHT = 58

    def __init__(self, minutes: list[float], days: list[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._minutes = list(minutes)
        self._days = list(days)
        self._theme = theming.manager().current_effective()
        self._fit()
        self.setToolTip("\n".join(
            f"{d} · {_fmt_minutes(m)}" for d, m in zip(self._day_names(),
                                                     self._minutes)))
        theming.manager().theme_changed.connect(self._on_theme)

    def _day_names(self) -> list[str]:
        import datetime as _dt
        today = _dt.date.today()
        return [(today - _dt.timedelta(days=6 - i)).strftime("%a").lower()
                for i in range(7)]

    def _fit(self) -> None:
        from .. import scale as _scale
        w = 7 * _scale.px(self.BAR) + 6 * _scale.px(self.GAP)
        self.setFixedSize(w, _scale.px(self.HEIGHT))

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self._fit()
        self.update()

    def paintEvent(self, _ev) -> None:
        from .. import scale as _scale
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        theme = self._theme
        modern = getattr(theme, "aesthetic", "") == "modern"
        accent = _qcolor(theme, "accent", "#d4b95e")
        dim = _qcolor(theme, "dim", "#6f6f6f")
        fg = _qcolor(theme, "fg", "#e6e6e6")
        fm = QFontMetrics(self.font())
        label_h = fm.height()
        bar, gap = _scale.px(self.BAR), _scale.px(self.GAP)
        chart_h = self.height() - label_h - _scale.px(4)
        peak = max(max(self._minutes, default=0.0), 1.0)
        radius = min(bar / 2.0, 3.0) if modern else 0.0
        for i, m in enumerate(self._minutes):
            x = i * (bar + gap)
            h = max(_scale.px(2), round(chart_h * m / peak)) if m > 0 \
                else _scale.px(2)
            bar_rect = QRectF(x, chart_h - h, bar, h)
            col = legibility.ink(self, QColor(accent if i == 6 else dim),
                                 bar_rect.toAlignedRect())
            if m <= 0:
                col.setAlpha(90)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(bar_rect, radius, radius)
            day_rect = QRect(x - gap // 2, chart_h + _scale.px(4),
                             bar + gap, label_h)
            day = theming.styled_case(self._days[i], theme)
            p.setPen(legibility.text_ink(p, fg if i == 6 else dim, day_rect,
                                         Qt.AlignHCenter | Qt.AlignTop, day))
            p.drawText(day_rect, Qt.AlignHCenter | Qt.AlignTop, day)
        p.end()


class _RepeatRow(QWidget):
    """One on-repeat song: small art, the title, artist and play count
    under it. Click to play."""

    clicked = Signal(object)            # Track
    ART = 34
    TEXT_W = 230

    def __init__(self, track, plays: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self._track = track
        self._theme = theming.manager().current_effective()
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{track.artists} — {track.title}")
        self._art = QLabel()
        px = _scale.px(self.ART)
        self._art.setFixedSize(px, px)
        self._title = QLabel()
        self._title.setTextFormat(Qt.PlainText)
        self._sub = QLabel()
        self._sub.setTextFormat(Qt.PlainText)
        self._sub.setProperty("class", "dim")
        self._raw_title = track.title or ""
        self._raw_sub = f"{track.artists} · {plays} plays" if track.artists \
            else f"{plays} plays"
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        text.addWidget(self._title)
        text.addWidget(self._sub)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_scale.px(8))
        row.addWidget(self._art, alignment=Qt.AlignVCenter)
        row.addLayout(text)
        row.addStretch(1)
        # The cover is re-read from art_cache on a theme change rather than
        # held here: a full-size decode per tile adds up across a shelf.
        self._art_src = track.thumbnail or ""
        self._set_texts()
        url = self._art_src
        if url:
            img = art_cache.cache().request(url, self._on_art)
            if img is not None:
                self._on_art(img)
        theming.manager().theme_changed.connect(self._on_theme)

    def _set_texts(self) -> None:
        from .. import scale as _scale
        width = _scale.px(self.TEXT_W)
        for label, raw in ((self._title, self._raw_title),
                           (self._sub, self._raw_sub)):
            fm = QFontMetrics(label.font())
            label.setText(fm.elidedText(theming.styled_case(raw, self._theme),
                                        Qt.ElideRight, width))

    def _on_art(self, img) -> None:
        if img is None or img.isNull():
            return
        try:
            size = self._art.width()
            pix = QPixmap.fromImage(img).scaled(
                size, size, Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation)
            radius = min(theming.effective_radius_px(self._theme), size // 4)
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
        except RuntimeError:
            pass    # the home page rebuilt before the art arrived

    def _on_theme(self, theme) -> None:
        from .. import scale as _scale
        self._theme = theme
        px = _scale.px(self.ART)
        self._art.setFixedSize(px, px)
        self._set_texts()
        img = art_cache.cache().get(self._art_src) if self._art_src else None
        if img is not None:
            self._on_art(img)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self.clicked.emit(self._track)
            ev.accept()
            return
        super().mousePressEvent(ev)


class RecentStrip(QWidget):
    """The hero's middle: covers of what you played lately, two rows of
    small squares, as many as the width holds (view.recently_played
    picks them, one per cover). Click a cover to play that song; hover
    for its name. It takes whatever room the keep-listening card and the
    week leave it, so a wide hero has no empty middle."""

    track_clicked = Signal(object)      # Track

    TILE = 52
    TILE_MODERN = 60
    ROWS = 2
    GAP = 6
    MAX_GAP = 18

    def __init__(self, tracks: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tracks = [t for t in tracks if getattr(t, "thumbnail", "")]
        self._theme = theming.manager().current_effective()
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(0)
        self._fit()
        art_cache.cache().image_loaded.connect(self._on_art)
        for t in self._tracks:
            art_cache.cache().request(t.thumbnail, None)
        theming.manager().theme_changed.connect(self._on_theme)

    def tracks(self) -> list:
        return list(self._tracks)

    def _tile(self) -> int:
        from .. import scale as _scale
        modern = getattr(self._theme, "aesthetic", "") == "modern"
        return _scale.px(self.TILE_MODERN if modern else self.TILE)

    def _fit(self) -> None:
        from .. import scale as _scale
        self.setFixedHeight(self.ROWS * self._tile()
                            + (self.ROWS - 1) * _scale.px(self.GAP))

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        from .. import scale as _scale
        tile, gap = self._tile(), _scale.px(self.GAP)
        return QSize(6 * tile + 5 * gap, self.height())

    def cells(self) -> list[QRect]:
        """One rect per cover shown: rows filled left to right, newest
        first, columns spread to the full width once there are covers
        enough to fill it."""
        from .. import scale as _scale
        import math
        tile, gap = self._tile(), _scale.px(self.GAP)
        n = len(self._tracks)
        fit = max(0, (self.width() + gap) // (tile + gap))
        cols = min(fit, math.ceil(n / self.ROWS)) if n else 0
        if cols <= 0:
            return []
        step = tile + gap
        if cols == fit and cols > 1:
            spread = (self.width() - cols * tile) / (cols - 1)
            step = tile + min(spread, _scale.px(self.MAX_GAP))
        out = []
        for i in range(min(n, cols * self.ROWS)):
            r, c = divmod(i, cols)
            out.append(QRect(round(c * step), r * (tile + gap), tile, tile))
        return out

    def _index_at(self, pos) -> int:
        for i, rect in enumerate(self.cells()):
            if rect.contains(pos):
                return i
        return -1

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self._fit()
        self.update()

    def _on_art(self, url: str, _img) -> None:
        if any(t.thumbnail == url for t in self._tracks):
            self.update()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        i = self._index_at(ev.position().toPoint())
        if i != self._hover:
            self._hover = i
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            i = self._index_at(ev.position().toPoint())
            if i >= 0:
                self.track_clicked.emit(self._tracks[i])
                ev.accept()
                return
        super().mousePressEvent(ev)

    def event(self, ev) -> bool:
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QToolTip
        if ev.type() == QEvent.ToolTip:
            i = self._index_at(ev.pos())
            if i >= 0:
                t = self._tracks[i]
                text = f"{t.artists} — {t.title}" if t.artists else t.title
                QToolTip.showText(ev.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        theme = self._theme
        modern = getattr(theme, "aesthetic", "") == "modern"
        radius = theming.effective_radius_px(theme)
        accent = _qcolor(theme, "accent", "#d4b95e")
        dpr = self.devicePixelRatioF()
        for i, rect in enumerate(self.cells()):
            t = self._tracks[i]
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), radius, radius)
            pix = art_cache.cache().scaled(t.thumbnail, rect.width(),
                                           rect.height(), dpr)
            if pix is None:
                p.setPen(Qt.NoPen)
                p.setBrush(_qcolor(theme, "surface_1", "#0dffffff") if modern
                           else _qcolor(theme, "bg_alt", "#141414"))
                p.drawPath(path)
            else:
                # Centre-crop: video thumbnails are 16:9, covers square.
                side = min(pix.width(), pix.height())
                src = QRectF((pix.width() - side) / 2, (pix.height() - side) / 2,
                             side, side)
                p.save()
                if radius > 0:
                    p.setClipPath(path)
                p.drawPixmap(QRectF(rect), pix, src)
                p.restore()
            if i == self._hover:
                p.setBrush(Qt.NoBrush)
                if modern:
                    pen = QColor(_qcolor(theme, "fg", "#e6e6e6"))
                    pen.setAlphaF(0.85)
                    p.setPen(pen)
                    p.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                                      radius, radius)
                else:
                    p.setPen(accent)
                    p.drawRect(rect.adjusted(-1, -1, 0, 0))
        p.end()


class WeekPanel(QWidget):
    """The hero's right side: the week at a glance (a bar per day, the
    streak, artists new to you) and the songs on repeat. Local history
    only (see view.week_summary)."""

    track_clicked = Signal(object)      # Track

    def __init__(self, week: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self._texts: list[tuple[QLabel, str]] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_scale.px(32))

        week_col = QVBoxLayout()
        week_col.setContentsMargins(0, 0, 0, 0)
        week_col.setSpacing(_scale.px(6))
        week_col.addWidget(self._dim("this week"))
        self.chart = _WeekChart(week.get("minutes", [0.0] * 7),
                                week.get("days", [""] * 7))
        week_col.addWidget(self.chart)
        streak = int(week.get("streak", 0) or 0)
        if streak >= 2:
            week_col.addWidget(self._dim(f"{streak} days in a row"))
        new = list(week.get("new") or [])
        if new:
            shown = ", ".join(new[:2]) + (f" +{len(new) - 2}" if len(new) > 2 else "")
            lbl = self._dim(f"new to you: {shown}")
            lbl.setToolTip(", ".join(new))
            week_col.addWidget(lbl)
        week_col.addStretch(1)
        row.addLayout(week_col)

        repeat = list(week.get("repeat") or [])
        if repeat:
            rep_col = QVBoxLayout()
            rep_col.setContentsMargins(0, 0, 0, 0)
            rep_col.setSpacing(_scale.px(6))
            rep_col.addWidget(self._dim("on repeat"))
            self.repeat_rows: list[_RepeatRow] = []
            for track, plays in repeat:
                r = _RepeatRow(track, plays)
                r.clicked.connect(self.track_clicked.emit)
                rep_col.addWidget(r)
                self.repeat_rows.append(r)
            rep_col.addStretch(1)
            row.addLayout(rep_col)
        theming.manager().theme_changed.connect(self._on_theme)

    def _dim(self, raw: str) -> QLabel:
        lbl = QLabel(theming.styled_case(raw))
        lbl.setProperty("class", "dim")
        lbl.setTextFormat(Qt.PlainText)
        self._texts.append((lbl, raw))
        return lbl

    def _on_theme(self, theme) -> None:
        for label, raw in self._texts:
            label.setText(theming.styled_case(raw, theme))


class Hero(QWidget):
    """Greeting + keep-listening. All local data — renders instantly and
    for every source, which is why it leads the page.

    modern: the page's one big moment — the greeting at display size, the
    art at 128px, the block on its own surface. brutalist: the bold
    greeting and 72px art it has always had, no surface.

    The body is three zones: keep listening, the recently-played covers
    (which take the spare width), and the week. Too narrow for all three
    on one line, the week drops under the other two."""

    resume_clicked = Signal()
    track_clicked = Signal(object)      # Track
    radio_clicked = Signal(object)      # Track
    likes_clicked = Signal()

    ART = 72
    ART_MODERN = 128

    TEXT_MAX = 340          # keep-listening column, so the covers get the rest

    def __init__(self, greeting: str, stats_line: str, last_track,
                 *, can_resume: bool, show_likes: bool,
                 week: dict | None = None, recent: list | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .. import scale as _scale
        self._theme = theming.manager().current_effective()
        modern = getattr(self._theme, "aesthetic", "") == "modern"
        self._modern = modern
        self._greeting = greeting
        self._art_url = ""
        self._texts: list[tuple[QLabel, str]] = []   # (label, raw) to re-case
        self.week_panel = None
        self.recent_strip = None
        self._body = None
        self._wide: bool | None = None

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
            track_lbl.setWordWrap(True)
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

            self._text_box = QWidget()
            text_col = QVBoxLayout(self._text_box)
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(2)
            text_col.addWidget(keep)
            text_col.addWidget(track_lbl)
            text_col.addLayout(btns)
            text_col.addStretch(1)

            # keep listening | recently played (the spare width) | the week
            if recent:
                self.recent_strip = RecentStrip(recent)
                self.recent_strip.track_clicked.connect(self.track_clicked.emit)
                self._recent_box = QWidget()
                rcol = QVBoxLayout(self._recent_box)
                rcol.setContentsMargins(0, 0, 0, 0)
                rcol.setSpacing(_scale.px(6))
                head = QLabel(theming.styled_case("recently played"))
                head.setProperty("class", "dim")
                head.setTextFormat(Qt.PlainText)
                self._texts.append((head, "recently played"))
                rcol.addWidget(head)
                rcol.addWidget(self.recent_strip)
                rcol.addStretch(1)
            if week:
                self.week_panel = WeekPanel(week)
                self.week_panel.track_clicked.connect(self.track_clicked.emit)
            self._body = QGridLayout()
            self._body.setContentsMargins(0, 0, 0, 0)
            self._body.setHorizontalSpacing(_scale.px(24))
            self._body.setVerticalSpacing(_scale.px(14))
            self._fit_text_box()
            self._place(wide=True)
            col.addLayout(self._body)

        theming.manager().theme_changed.connect(self._on_theme)

    # ---- body layout ----

    def _fit_text_box(self) -> None:
        from .. import scale as _scale
        box = getattr(self, "_text_box", None)
        if box is not None and self.recent_strip is not None:
            box.setMaximumWidth(_scale.px(self.TEXT_MAX))

    def _min_wide(self) -> int:
        """Width the body needs for all three zones on one line: the
        keep-listening card, three covers, the week."""
        from .. import scale as _scale
        need = 0
        if self._art is not None:
            need += self._art_px + 12
        need += _scale.px(220)
        if self.recent_strip is not None:
            need += 3 * self.recent_strip._tile() + _scale.px(24)
        if self.week_panel is not None:
            need += self.week_panel.sizeHint().width() + _scale.px(24)
        m = self._col.contentsMargins()
        return need + m.left() + m.right()

    def is_wide(self) -> bool:
        return bool(self._wide)

    def _place(self, *, wide: bool) -> None:
        """(Re)seat the zones in the body grid. Wide: one line. Narrow:
        art + keep listening + covers on top, the week under them."""
        body = self._body
        if body is None or wide == self._wide:
            return
        self._wide = wide
        for w in (self._art, getattr(self, "_text_box", None),
                  getattr(self, "_recent_box", None), self.week_panel):
            if w is not None:
                body.removeWidget(w)
        for c in range(4):
            body.setColumnStretch(c, 0)
        art = self._art
        if art is not None:
            body.addWidget(art, 0, 0, Qt.AlignTop)
        body.addWidget(self._text_box, 0, 1, Qt.AlignTop)
        if self.recent_strip is not None:
            body.addWidget(self._recent_box, 0, 2)
            body.setColumnStretch(2, 1)
        else:
            body.setColumnStretch(1, 1)
        if self.week_panel is not None:
            if wide:
                body.addWidget(self.week_panel, 0, 3, Qt.AlignTop | Qt.AlignRight)
            else:
                body.addWidget(self.week_panel, 1, 0, 1, 4,
                               Qt.AlignTop | Qt.AlignLeft)

    def minimumSizeHint(self):  # noqa: N802
        # The one-line body's minimum would hold the page open at its
        # width, and the hero would never get narrow enough to reflow.
        from PySide6.QtCore import QSize
        from .. import scale as _scale
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), _scale.px(320)), hint.height())

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if self._body is not None:
            self._place(wide=self.width() >= self._min_wide())

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
            img = art_cache.cache().get(self._art_url) if self._art_url else None
            if img is not None:
                self._set_art(img)
        if self._body is not None:
            self._fit_text_box()
            self._place(wide=self.width() >= self._min_wide())
        self.update()

    def _set_art(self, img) -> None:
        if self._art is None:
            return
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
