"""Card widget — used on the home page + library + album/artist search tabs.

Square thumbnail, title, subtitle. Click → ``clicked`` signal with the
payload object (Track / AlbumEntry / ArtistEntry / PlaylistEntry). For
artist cards the thumbnail mask is circular; everything else follows the
corner style (square in brutalist, rounded in modern).

A card is also a ``ResponsiveGrid`` tile: ``set_tile_width`` resizes the
thumbnail to fill a column, ``tile_height`` says how tall that makes it.

``ShelfRow`` arranges cards in a horizontally scrolling strip; ``CardGrid``
wraps them to the width it is given.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
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
from . import art_cache, legibility
from .flow import ResponsiveGrid


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
        thumb_px: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        self._thumb_url = thumbnail_url
        self._payload = payload
        self._circular = circular
        # v1.5 home patterns size cards per pattern (dense grids run small,
        # mosaic features run big). None = the classic 144px shelf card.
        self._thumb_base = thumb_px
        # A ResponsiveGrid column width, once one has been assigned; wins
        # over the base thumb size and survives theme / scale refreshes.
        self._tile_w: int | None = None
        # modern hover, 0..1 (a touch past 1 mid-spring): the surface fades
        # in, the art lifts 3%, a play badge appears on playable items.
        # Driven by motion.value_lerp so OFF snaps and the springy profile
        # overshoots; brutalist keeps its instant accent halo.
        self._hover_t = 0.0
        self._theme = theming.manager().current_effective()
        self._refresh_scaled_sizes()
        theming.manager().theme_changed.connect(self._on_theme)
        art_cache.cache().image_loaded.connect(self._on_art_loaded)

        self.setFixedSize(self.THUMB + 2 * self.MARGIN,
                          self.THUMB + self.TEXT_HEIGHT + 2 * self.MARGIN)
        # A card is a fixed 144px column: its title elides more often than
        # not, so carry the full text in the tooltip.
        self.setToolTip("\n".join(t for t in (title, subtitle) if t))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        # Trigger fetch right away so the visible shelves warm fast.
        art_cache.cache().request(thumbnail_url or "", None)

    def _refresh_scaled_sizes(self) -> None:
        from . import scale as _scale
        # Shadow the class-level base with scaled per-instance values.
        cls = type(self)
        base = self._thumb_base if getattr(self, "_thumb_base", None) else cls.THUMB
        self.TEXT_HEIGHT = _scale.px(cls.TEXT_HEIGHT)
        self.MARGIN = _scale.px(cls.MARGIN)
        tile_w = getattr(self, "_tile_w", None)
        if tile_w:
            self.THUMB = max(16, int(tile_w) - 2 * self.MARGIN)
        else:
            self.THUMB = _scale.px(base)

    # ---- ResponsiveGrid tile protocol ----

    def set_tile_width(self, width: int) -> None:
        self._tile_w = int(width)
        self._refresh_scaled_sizes()
        self.setFixedSize(self.THUMB + 2 * self.MARGIN,
                          self.THUMB + self.TEXT_HEIGHT + 2 * self.MARGIN)
        self.update()

    def tile_height(self, width: int) -> int:
        # thumb = width - 2·margin; height = thumb + text + 2·margin
        return int(width) + self.TEXT_HEIGHT

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
        self._set_hover(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:
        self._set_hover(False)
        super().leaveEvent(ev)

    # ---- hover ----

    def _playable(self) -> bool:
        """Clicking plays (a song / video) rather than opening a page."""
        p = self._payload
        kind = getattr(p, "kind", "")
        if kind:
            return kind in ("song", "video")
        return type(p).__name__ == "Track"

    def _set_hover(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        if getattr(self._theme, "aesthetic", "") != "modern":
            self._hover_t = target
            self.update()
            return
        from . import motion as motion_module
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
        bg_alt = _qcolor(theme, "bg_alt", "#141414")
        fg = _qcolor(theme, "fg", "#e6e6e6")
        dim = _qcolor(theme, "dim", "#6f6f6f")
        accent = _qcolor(theme, "accent", "#d4b95e")
        modern = getattr(theme, "aesthetic", "") == "modern"
        radius = 0 if self._circular else theming.effective_radius_px(theme)

        thumb_rect = QRect(self.MARGIN, self.MARGIN, self.THUMB, self.THUMB)
        hovered = self.underMouse()
        t = self._hover_t if modern else 0.0

        # Hover. modern: a surface fades in behind the whole card and the
        # art lifts. brutalist: the one-pixel accent halo it has always
        # drawn around the thumbnail, on and off.
        if modern and t > 0.0:
            fill = _qcolor(theme, "surface_2", "#17ffffff")
            fill.setAlphaF(fill.alphaF() * min(1.0, t))
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(self.rect()), radius + 2, radius + 2)
        elif hovered and not modern:
            p.setPen(accent)
            p.setBrush(Qt.NoBrush)
            p.drawRect(thumb_rect.adjusted(-1, -1, 0, 0))

        art_rect = QRectF(thumb_rect)
        if t > 0.0:
            grow = art_rect.width() * 0.03 * t
            art_rect = art_rect.adjusted(-grow / 2, -grow / 2, grow / 2, grow / 2)
        clip = QPainterPath()
        if self._circular:
            clip.addEllipse(art_rect)
        else:
            clip.addRoundedRect(art_rect, radius, radius)

        # Scaled once at rest size: the hover lift redraws it a few percent
        # larger every frame, which smooth pixmap drawing covers.
        pix = art_cache.cache().scaled(
            self._thumb_url or "", self.THUMB, self.THUMB, self.devicePixelRatioF())
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
            if self._circular or radius > 0 or t > 0.0:
                p.setClipPath(clip)
                p.setRenderHint(QPainter.SmoothPixmapTransform, t > 0.0)
                p.drawPixmap(art_rect.toRect(), pix)
                p.setClipping(False)
            else:
                p.drawPixmap(thumb_rect, pix)

        if modern and t > 0.0 and self._playable():
            _draw_play_badge(p, art_rect, fg, _qcolor(theme, "bg", "#000000"),
                             min(1.0, t), self.MARGIN)

        # Title (one line, elided).
        fm = QFontMetrics(self.font())
        title_y = thumb_rect.bottom() + 6
        title_rect = QRect(self.MARGIN, title_y, self.THUMB, fm.height())
        title = theming.styled_case(self._title, theme)
        title = fm.elidedText(title, Qt.ElideRight, self.THUMB)
        flags = Qt.AlignVCenter | Qt.AlignLeft
        p.setPen(legibility.text_ink(p, fg, title_rect, flags, title))
        p.drawText(title_rect, flags, title)

        # Subtitle (one line, elided, dim).
        sub_rect = QRect(self.MARGIN, title_y + fm.height(), self.THUMB, fm.height())
        sub = theming.styled_case(self._subtitle, theme)
        sub = fm.elidedText(sub, Qt.ElideRight, self.THUMB)
        p.setPen(legibility.text_ink(p, dim, sub_rect, flags, sub))
        p.drawText(sub_rect, flags, sub)


def _draw_play_badge(p: QPainter, art: QRectF, fill: QColor, ink: QColor,
                     opacity: float, margin: int) -> None:
    """A filled play circle at the art's bottom-right corner: the modern
    hover's "this plays" cue. Sized from the art so dense tiles get a
    small one and a feature card a large one."""
    from . import scale as _scale
    d = max(_scale.px(22), min(_scale.px(40), art.width() * 0.26))
    inset = max(float(margin), d * 0.3)
    cx = art.right() - inset - d / 2
    cy = art.bottom() - inset - d / 2
    p.save()
    p.setOpacity(opacity)
    p.setPen(Qt.NoPen)
    p.setBrush(fill)
    p.drawEllipse(QRectF(cx - d / 2, cy - d / 2, d, d))
    tri = QPainterPath()
    s = d * 0.36
    tri.moveTo(cx - s * 0.42, cy - s * 0.5)
    tri.lineTo(cx + s * 0.58, cy)
    tri.lineTo(cx - s * 0.42, cy + s * 0.5)
    tri.closeSubpath()
    p.setBrush(ink)
    p.drawPath(tri)
    p.restore()


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
        # pick up live scale updates without a dedicated channel. Must be a
        # bound method: a lambda connected to the app-lifetime theming
        # manager never disconnects, so every discarded ShelfRow would keep
        # being invoked on dead C++ objects at each theme change.
        theming.manager().theme_changed.connect(self._on_theme_rescale)

        self._inner = QWidget()
        self._layout = QHBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self.setWidget(self._inner)

    def _on_theme_rescale(self, _theme) -> None:
        self._apply_scaled_height()

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


class CardGrid(ResponsiveGrid):
    """Cards wrapped to the width they are given: as many columns as the
    classic 144px card allows, grown up to 200px to fill each row. Used by
    the library's album / artist / following tabs and the mood pages."""

    MIN_THUMB = Card.THUMB
    MAX_THUMB = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        from . import scale as _scale
        margin = 2 * _scale.px(Card.MARGIN)
        super().__init__(min_tile=_scale.px(self.MIN_THUMB) + margin,
                         max_tile=_scale.px(self.MAX_THUMB) + margin,
                         gap=_scale.px(8), row_gap=_scale.px(14), parent=parent)

    def add_card(self, card: Card) -> None:
        self.add(card)
