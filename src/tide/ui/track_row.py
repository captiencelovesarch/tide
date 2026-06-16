"""Track-row item delegate.

Renders a single track in a list with optional thumbnail. The "show
thumbnails" decision is made per-theme (via `[layout].show_thumbnails`)
and globally overridable via Settings (when implemented).

A track is exposed to the delegate via ``Qt.UserRole`` returning an
``api.Track`` instance. ``Qt.UserRole + 100`` (TrackRowDelegate.IsCurrentRole)
optionally returns True to mark the row as "currently playing" — used by
the queue model.

The delegate listens to the shared art_cache so as soon as a thumbnail
arrives it invalidates the right row.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .. import api, theming
from . import art_cache


IsCurrentRole = Qt.UserRole + 100


_thumbnail_setting_override: str = "theme"   # "theme" | "on" | "off"


def set_thumbnail_override(value: str) -> None:
    global _thumbnail_setting_override
    if value in ("theme", "on", "off"):
        _thumbnail_setting_override = value


def show_thumbnails_for(theme) -> bool:
    if _thumbnail_setting_override == "on":
        return True
    if _thumbnail_setting_override == "off":
        return False
    if theme is None:
        return True
    val = theme.t("layout", "show_thumbnails", None)
    if val is None:
        # Fall back to "true if not a mono font theme".
        return not bool(theme.t("typography", "mono", False))
    return bool(val)


class TrackRowDelegate(QStyledItemDelegate):
    # Base sizes at UI scale = 1.0. Per-instance scaled values (THUMB_SIZE
    # etc.) are populated in __init__ and refreshed in _on_theme_changed,
    # which is also fired right after a ui_scale change.
    BASE_THUMB_SIZE = 40
    BASE_THUMB_MARGIN = 10
    BASE_ROW_HEIGHT = 56
    BASE_ROW_HEIGHT_TEXT_ONLY = 28

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._theme = theming.manager().current()
        self._refresh_scaled_sizes()
        theming.manager().theme_changed.connect(self._on_theme_changed)
        # Listen for newly-loaded art. Coalesce a burst of loads into ONE
        # viewport repaint via a single-shot timer — otherwise loading 50
        # thumbnails while scrolling a list triggers 50 full paints.
        art_cache.cache().image_loaded.connect(self._on_art_loaded)
        self._views = set()
        self._art_repaint_timer = QTimer()
        self._art_repaint_timer.setSingleShot(True)
        self._art_repaint_timer.setInterval(80)
        self._art_repaint_timer.timeout.connect(self._flush_art_repaint)
        # URLs we've already kicked off fetches for, so paint() doesn't
        # re-request on every frame.
        self._requested_urls: set[str] = set()

    def attach(self, view) -> None:
        self._views.add(view)

    def _on_theme_changed(self, theme) -> None:
        self._theme = theme
        self._refresh_scaled_sizes()
        for v in list(self._views):
            try:
                v.viewport().update()
                v.scheduleDelayedItemsLayout()
            except Exception:
                pass

    def _refresh_scaled_sizes(self) -> None:
        from . import scale as _scale
        self.THUMB_SIZE = _scale.px(self.BASE_THUMB_SIZE)
        self.THUMB_MARGIN = _scale.px(self.BASE_THUMB_MARGIN)
        self.ROW_HEIGHT = _scale.px(self.BASE_ROW_HEIGHT)
        self.ROW_HEIGHT_TEXT_ONLY = _scale.px(self.BASE_ROW_HEIGHT_TEXT_ONLY)

    def _on_art_loaded(self, _url: str, _img: QImage) -> None:
        # Coalesce — let the timer fire once for a burst.
        if not self._art_repaint_timer.isActive():
            self._art_repaint_timer.start()

    def _flush_art_repaint(self) -> None:
        for v in list(self._views):
            try:
                v.viewport().update()
            except Exception:
                pass

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        show = show_thumbnails_for(self._theme)
        fm = QFontMetrics(option.font)
        natural = fm.height() + 12
        if show:
            return QSize(option.rect.width() if option.rect.width() > 0 else 200,
                         max(self.ROW_HEIGHT, natural))
        return QSize(option.rect.width() if option.rect.width() > 0 else 200,
                     max(self.ROW_HEIGHT_TEXT_ONLY, natural))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        tr: api.Track | None = index.data(Qt.UserRole)
        if not isinstance(tr, api.Track):
            super().paint(painter, option, index)
            return

        theme = self._theme
        bg = _qcolor(theme, "bg", "#0b0b0b")
        bg_alt = _qcolor(theme, "bg_alt", "#141414")
        fg = _qcolor(theme, "fg", "#e6e6e6")
        dim = _qcolor(theme, "dim", "#6f6f6f")
        sel_bg = _qcolor(theme, "sel_bg", "#e6e6e6")
        sel_fg = _qcolor(theme, "sel_fg", "#0b0b0b")
        accent = _qcolor(theme, "accent", "#d4b95e")

        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        is_current = bool(index.data(IsCurrentRole))

        # background
        painter.save()
        if selected:
            painter.fillRect(option.rect, sel_bg)
            text_color = sel_fg
            dim_color = sel_fg
        elif hovered:
            painter.fillRect(option.rect, bg_alt)
            text_color = fg
            dim_color = dim
        else:
            text_color = fg
            dim_color = dim

        show = show_thumbnails_for(theme)
        marker = str(theme.t("layout", "list_marker", "> ")) if theme else "> "

        x = option.rect.left() + self.THUMB_MARGIN
        y = option.rect.top()
        h = option.rect.height()

        if show:
            thumb_rect = QRect(x, y + (h - self.THUMB_SIZE) // 2, self.THUMB_SIZE, self.THUMB_SIZE)
            url = tr.thumbnail or ""
            cached = art_cache.cache().get(url) if url else None
            if cached is None and url and url not in self._requested_urls:
                # Kick off the fetch once; subsequent paints will hit cache.
                self._requested_urls.add(url)
                art_cache.cache().request(url, None)
            if cached is not None:
                pix = QPixmap.fromImage(cached).scaled(
                    self.THUMB_SIZE, self.THUMB_SIZE,
                    Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                )
                painter.drawPixmap(thumb_rect, pix)
            else:
                # Placeholder: theme-colored solid + 1px border.
                painter.fillRect(thumb_rect, bg_alt)
                painter.setPen(dim_color)
                painter.drawRect(thumb_rect.adjusted(0, 0, -1, -1))
            x = thumb_rect.right() + self.THUMB_MARGIN
        else:
            painter.setPen(text_color if is_current else dim_color)
            cursor = marker if is_current else "  "
            cursor_w = QFontMetrics(option.font).horizontalAdvance(cursor)
            painter.drawText(
                QRect(x, y, cursor_w, h),
                Qt.AlignVCenter | Qt.AlignLeft, cursor,
            )
            x += cursor_w

        # text: "artist — title"
        title = theming.styled_case(tr.title or "", theme)
        artist = theming.styled_case(tr.artists or "", theme)
        primary = f"{artist} — {title}" if (artist and title) else (title or artist)
        if show:
            # title on top line, artist on second, like a music app
            fm = QFontMetrics(option.font)
            line_h = fm.height()
            painter.setPen(text_color if not (is_current and not selected) else accent)
            painter.drawText(
                QRect(x, y + (h - 2 * line_h - 2) // 2, option.rect.right() - x - 60, line_h),
                Qt.AlignVCenter | Qt.AlignLeft,
                fm.elidedText(title, Qt.ElideRight, option.rect.right() - x - 60),
            )
            painter.setPen(dim_color)
            painter.drawText(
                QRect(x, y + (h - 2 * line_h - 2) // 2 + line_h + 2,
                      option.rect.right() - x - 60, line_h),
                Qt.AlignVCenter | Qt.AlignLeft,
                fm.elidedText(artist, Qt.ElideRight, option.rect.right() - x - 60),
            )
        else:
            painter.setPen(accent if is_current and not selected else text_color)
            fm = QFontMetrics(option.font)
            painter.drawText(
                QRect(x, y, option.rect.right() - x - 60, h),
                Qt.AlignVCenter | Qt.AlignLeft,
                fm.elidedText(primary, Qt.ElideRight, option.rect.right() - x - 60),
            )

        # duration, right-aligned
        if tr.duration:
            painter.setPen(dim_color)
            painter.drawText(
                QRect(option.rect.right() - 56, y, 50, h),
                Qt.AlignVCenter | Qt.AlignRight, tr.duration,
            )

        painter.restore()


def _qcolor(theme, key: str, default: str) -> QColor:
    if theme is None:
        return QColor(default)
    return QColor(theme.token(key, default))
