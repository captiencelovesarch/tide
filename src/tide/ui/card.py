"""Card widget — used on the Explore page + album/artist search tabs.

Square thumbnail, title, subtitle. Click → ``clicked`` signal with the
payload object (Track / AlbumEntry / ArtistEntry / PlaylistEntry). For
artist cards the thumbnail mask is circular; everything else stays square.

A ``ShelfRow`` helper widget arranges cards in a horizontally scrolling
strip — used by the Explore view for each shelf.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from . import art_cache


def _qcolor(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))


class Card(QWidget):
    clicked = Signal(object)        # the payload supplied at construction

    # Base sizes at UI scale = 1.0. Class-level so Shelf (and any other
    # consumer that doesn't hold a Card instance) can still read them. Each
    # Card instance shadows these with scaled values via
    # _refresh_scaled_sizes — so `self.THUMB` is scaled, `Card.THUMB` is base.
    THUMB = 144
    TEXT_HEIGHT = 44
    MARGIN = 6

    def __init__(
        self,
        title: str,
        subtitle: str,
        thumbnail_url: str,
        payload,
        *,
        circular: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        self._thumb_url = thumbnail_url
        self._payload = payload
        self._circular = circular
        self._theme = theming.manager().current()
        self._refresh_scaled_sizes()
        theming.manager().theme_changed.connect(self._on_theme)
        art_cache.cache().image_loaded.connect(self._on_art_loaded)

        self.setFixedSize(self.THUMB + 2 * self.MARGIN,
                          self.THUMB + self.TEXT_HEIGHT + 2 * self.MARGIN)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        # Trigger fetch right away so the visible shelves warm fast.
        art_cache.cache().request(thumbnail_url or "", None)

    def _refresh_scaled_sizes(self) -> None:
        from . import scale as _scale
        # Shadow the class-level base with scaled per-instance values.
        cls = type(self)
        self.THUMB = _scale.px(cls.THUMB)
        self.TEXT_HEIGHT = _scale.px(cls.TEXT_HEIGHT)
        self.MARGIN = _scale.px(cls.MARGIN)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        new_thumb = self.THUMB
        self._refresh_scaled_sizes()
        if new_thumb != self.THUMB:
            # ui_scale change — resize the tile.
            self.setFixedSize(self.THUMB + 2 * self.MARGIN,
                              self.THUMB + self.TEXT_HEIGHT + 2 * self.MARGIN)
        self.update()

    def _on_art_loaded(self, url: str, _img) -> None:
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
        bg_alt = _qcolor(theme, "bg_alt", "#141414")
        fg = _qcolor(theme, "fg", "#e6e6e6")
        dim = _qcolor(theme, "dim", "#6f6f6f")
        accent = _qcolor(theme, "accent", "#d4b95e")

        thumb_rect = QRect(self.MARGIN, self.MARGIN, self.THUMB, self.THUMB)

        # Optional hover halo.
        if self.underMouse():
            p.setPen(accent)
            p.setBrush(Qt.NoBrush)
            p.drawRect(thumb_rect.adjusted(-1, -1, 0, 0))

        img = art_cache.cache().get(self._thumb_url or "")
        if img is None:
            p.fillRect(thumb_rect, bg_alt)
            p.setPen(dim)
            p.drawRect(thumb_rect.adjusted(0, 0, -1, -1))
        else:
            pix = QPixmap.fromImage(img).scaled(
                self.THUMB, self.THUMB,
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
            )
            if self._circular:
                clip = QPainterPath()
                clip.addEllipse(thumb_rect)
                p.setClipPath(clip)
                p.drawPixmap(thumb_rect, pix)
                p.setClipping(False)
            else:
                p.drawPixmap(thumb_rect, pix)

        # Title (one line, elided).
        fm = QFontMetrics(self.font())
        title_y = thumb_rect.bottom() + 6
        title_rect = QRect(self.MARGIN, title_y, self.THUMB, fm.height())
        title = theming.styled_case(self._title, theme)
        title = fm.elidedText(title, Qt.ElideRight, self.THUMB)
        p.setPen(fg)
        p.drawText(title_rect, Qt.AlignVCenter | Qt.AlignLeft, title)

        # Subtitle (one line, elided, dim).
        sub_rect = QRect(self.MARGIN, title_y + fm.height(), self.THUMB, fm.height())
        sub = theming.styled_case(self._subtitle, theme)
        sub = fm.elidedText(sub, Qt.ElideRight, self.THUMB)
        p.setPen(dim)
        p.drawText(sub_rect, Qt.AlignVCenter | Qt.AlignLeft, sub)


class ShelfRow(QScrollArea):
    """Horizontal scrolling row of cards. Used by the Explore page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self._apply_scaled_height()
        # Theme re-emit also fires on ui_scale change, so this is how we
        # pick up live scale updates without a dedicated channel.
        theming.manager().theme_changed.connect(lambda _t: self._apply_scaled_height())

        self._inner = QWidget()
        self._layout = QHBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self.setWidget(self._inner)

    def _apply_scaled_height(self) -> None:
        from . import scale as _scale
        # Match a scaled Card's outer footprint plus a fixed gutter for text
        # overflow + scrollbar reserve.
        self.setFixedHeight(
            _scale.px(Card.THUMB + Card.TEXT_HEIGHT + 2 * Card.MARGIN) + _scale.px(14)
        )

    def add_card(self, card: Card) -> None:
        self._layout.addWidget(card)

    def clear(self) -> None:
        while self._layout.count():
            it = self._layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    def end_with_stretch(self) -> None:
        self._layout.addStretch(1)


class CardGrid(QWidget):
    """Wrapping grid of cards. Used by search album/artist tabs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)        # placeholder; we use FlowLayout-ish via wrapping
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        from PySide6.QtWidgets import QVBoxLayout, QGridLayout
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(14)
        self._layout.addLayout(self._grid)
        self._cols = 5

    def set_columns(self, n: int) -> None:
        self._cols = max(1, n)

    def add_card(self, card: Card) -> None:
        existing = self._grid.count()
        row, col = divmod(existing, self._cols)
        self._grid.addWidget(card, row, col)

    def clear(self) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
