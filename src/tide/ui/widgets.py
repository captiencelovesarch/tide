"""Theme-aware custom widgets.

Everything subscribes to ThemeManager.theme_changed so a runtime theme swap
re-paints without a restart. Pure-QSS widgets get their styling from the
stylesheet directly; the custom-painted ones (MonoProgress, AlbumArt) read
tokens and repaint here.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget

from .. import theming


def _color(theme, name: str, default: str) -> QColor:
    return QColor(theme.token(name, default)) if theme else QColor(default)


class BracketButton(QPushButton):
    """Text button rendered like `[play]`. Hover inverts bg/fg.

    Honors the theme's control_style:
      - "bracket"  -> "[label]"
      - "glyph"    -> uses `glyph` (e.g. "▶") if supplied, else label
      - "icon"     -> falls back to label (icons land later)
    """

    def __init__(self, label: str, glyph: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._glyph = glyph
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._apply_theme(theming.manager().current())
        theming.manager().theme_changed.connect(self._apply_theme)

    def setLabel(self, label: str) -> None:
        self._label = label
        self._update_text()

    def setGlyph(self, glyph: str | None) -> None:
        self._glyph = glyph
        self._update_text()

    def _apply_theme(self, theme) -> None:
        self._theme = theme
        self._update_text()
        # All styling lives in QSS for BracketButton, set as object name so
        # the stylesheet can target it precisely.
        self.setObjectName("BracketButton")
        bg = theme.token("bg", "#000") if theme else "#000"
        fg = theme.token("fg", "#fff") if theme else "#fff"
        hover_bg = theme.token("sel_bg", fg) if theme else fg
        hover_fg = theme.token("sel_fg", bg) if theme else bg
        dim = theme.token("dim", "#666") if theme else "#666"
        self.setStyleSheet(
            f"QPushButton#BracketButton {{"
            f"  background: transparent;"
            f"  color: {fg};"
            f"  border: none;"
            f"  padding: 4px 8px;"
            f"}}"
            f"QPushButton#BracketButton:hover {{"
            f"  background: {hover_bg};"
            f"  color: {hover_fg};"
            f"}}"
            f"QPushButton#BracketButton:disabled {{ color: {dim}; }}"
        )
        self.update()

    def _update_text(self) -> None:
        style = "bracket"
        if getattr(self, "_theme", None) is not None:
            style = str(self._theme.t("layout", "control_style", "bracket"))
        if style == "glyph" and self._glyph:
            self.setText(self._glyph)
        elif style == "icon":
            self.setText(self._label)  # icons come later
        else:
            self.setText(f"[{self._label}]")


class MonoProgress(QWidget):
    """Text-rendered progress bar: `[▮▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯]`.

    Click anywhere on the bar to seek. Emits `seek_requested(seconds)`.
    Non-mono themes can override by setting layout.control_style = "glyph"
    or "icon" — the widget still paints, just with a continuous bar instead
    of cells.
    """

    seek_requested = Signal(float)

    CELLS = 28          # number of cells in the bar; tuned for typical widths
    FILLED_CHAR = "▮"
    EMPTY_CHAR = "▯"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self._theme = theming.manager().current()
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setDuration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)
        self._enabled = self._duration > 0
        self.update()

    def setPosition(self, seconds: float) -> None:
        self._position = max(0.0, min(seconds, self._duration or seconds))
        self.update()

    def reset(self) -> None:
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self.update()

    def mousePressEvent(self, event) -> None:
        if not self._enabled or self._duration <= 0:
            return
        x = event.position().x()
        # Inner content rect leaves a single character of padding at each end
        # for the [ and ] brackets so seeks land where the user sees fill.
        fm = QFontMetrics(self.font())
        bracket_w = fm.horizontalAdvance("[")
        inner = QRect(int(bracket_w), 0, int(self.width() - 2 * bracket_w), self.height())
        if inner.width() <= 0:
            return
        frac = max(0.0, min(1.0, (x - inner.x()) / inner.width()))
        self.seek_requested.emit(frac * self._duration)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        theme = self._theme
        fg = _color(theme, "fg", "#e6e6e6")
        dim = _color(theme, "dim", "#6f6f6f")
        accent = _color(theme, "accent", "#d4b95e")

        p.setFont(self.font())
        rect = self.rect()

        if self._duration <= 0:
            p.setPen(dim)
            text = "[" + (self.EMPTY_CHAR * self.CELLS) + "]"
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)
            return

        frac = self._position / self._duration if self._duration else 0
        filled = max(0, min(self.CELLS, round(frac * self.CELLS)))
        empty = self.CELLS - filled

        # Draw the brackets in dim, the filled cells in accent, empties in fg.
        p.setPen(dim)
        bracket_open = "["
        bracket_close = "]"
        fm = QFontMetrics(self.font())
        x = 0
        # opening bracket
        p.drawText(QRect(x, 0, fm.horizontalAdvance(bracket_open), rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, bracket_open)
        x += fm.horizontalAdvance(bracket_open)
        # filled cells
        if filled:
            p.setPen(accent)
            chunk = self.FILLED_CHAR * filled
            w = fm.horizontalAdvance(chunk)
            p.drawText(QRect(x, 0, w, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, chunk)
            x += w
        # empty cells
        if empty:
            p.setPen(fg)
            chunk = self.EMPTY_CHAR * empty
            w = fm.horizontalAdvance(chunk)
            p.drawText(QRect(x, 0, w, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, chunk)
            x += w
        # closing bracket
        p.setPen(dim)
        p.drawText(QRect(x, 0, fm.horizontalAdvance(bracket_close), rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, bracket_close)


class MonoVolume(QWidget):
    """Compact volume bar: `[♪ ▮▮▮▮▮▯▯▯▯▯]`.

    Scroll wheel steps ±5. Click + drag sets the level.
    """

    volume_changed = Signal(int)   # 0..100

    CELLS = 10
    FILLED_CHAR = "▮"
    EMPTY_CHAR = "▯"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._volume = 80
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self.setFixedHeight(22)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("scroll to adjust volume")

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setVolume(self, value: int, *, emit: bool = True) -> None:
        v = max(0, min(100, int(value)))
        if v == self._volume:
            return
        self._volume = v
        self.update()
        if emit:
            self.volume_changed.emit(v)

    def volume(self) -> int:
        return self._volume

    # ------- input -------

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        step = 5 if delta > 0 else -5
        self.setVolume(self._volume + step)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        self._set_from_x(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self._set_from_x(event.position().x())

    def _set_from_x(self, x: float) -> None:
        fm = QFontMetrics(self.font())
        prefix = "[♪ "
        suffix = "]"
        prefix_w = fm.horizontalAdvance(prefix)
        suffix_w = fm.horizontalAdvance(suffix)
        usable = max(1, self.width() - prefix_w - suffix_w)
        rel = max(0.0, min(1.0, (x - prefix_w) / usable))
        self.setVolume(round(rel * 100))

    # ------- paint -------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        theme = self._theme
        fg = _color(theme, "fg", "#e6e6e6")
        dim = _color(theme, "dim", "#6f6f6f")
        accent = _color(theme, "accent", "#d4b95e")
        p.setFont(self.font())
        rect = self.rect()
        fm = QFontMetrics(self.font())

        filled = round((self._volume / 100.0) * self.CELLS)
        empty = self.CELLS - filled

        x = 0
        # prefix "[♪ "
        p.setPen(dim)
        prefix = "[♪ "
        p.drawText(QRect(x, 0, fm.horizontalAdvance(prefix), rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, prefix)
        x += fm.horizontalAdvance(prefix)
        # filled cells
        if filled:
            p.setPen(accent)
            chunk = self.FILLED_CHAR * filled
            w = fm.horizontalAdvance(chunk)
            p.drawText(QRect(x, 0, w, rect.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, chunk)
            x += w
        # empty cells
        if empty:
            p.setPen(fg)
            chunk = self.EMPTY_CHAR * empty
            w = fm.horizontalAdvance(chunk)
            p.drawText(QRect(x, 0, w, rect.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, chunk)
            x += w
        # suffix "]"
        p.setPen(dim)
        p.drawText(QRect(x, 0, fm.horizontalAdvance("]"), rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, "]")


class AlbumArt(QLabel):
    """Sharp-scaled album art tile. Empty state shows a `[ no art ]` glyph."""

    def __init__(self, size: int = 96, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self._pixmap_raw: QPixmap | None = None
        self._theme = theming.manager().current()
        self._apply_theme(self._theme)
        theming.manager().theme_changed.connect(self._apply_theme)
        self._render_empty()

    def _apply_theme(self, theme) -> None:
        self._theme = theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        radius = int(theme.t("layout", "radius_px", 0)) if theme else 0
        self.setStyleSheet(
            f"QLabel {{ background: {bg}; border: 1px solid {fg}; "
            f"border-radius: {radius}px; color: {fg}; }}"
        )
        if self._pixmap_raw is None:
            self._render_empty()
        else:
            self._render(self._pixmap_raw)

    def setImage(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            self._pixmap_raw = None
            self._render_empty()
            return
        self._pixmap_raw = QPixmap.fromImage(image)
        self._render(self._pixmap_raw)

    def _render(self, pix: QPixmap) -> None:
        scaled = pix.scaled(
            self._size, self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.FastTransformation,
        )
        self.setPixmap(scaled)

    def _render_empty(self) -> None:
        self.setPixmap(QPixmap())
        self.setText("[no art]")


class NowPlayingLabel(QWidget):
    """artist — title  (album)   |   small dim status row underneath."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._artist = ""
        self._title = ""
        self._album = ""
        self._status = ""
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(40)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def setTrack(self, artist: str, title: str, album: str = "") -> None:
        self._artist = artist
        self._title = title
        self._album = album
        self.update()

    def setStatus(self, text: str) -> None:
        self._status = text
        self.update()

    def clear(self) -> None:
        self._artist = self._title = self._album = self._status = ""
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        fg = _color(self._theme, "fg", "#e6e6e6")
        dim = _color(self._theme, "dim", "#6f6f6f")
        rect = self.rect().adjusted(0, 4, -8, -4)
        fm = QFontMetrics(self.font())

        if not self._title and not self._status:
            p.setPen(dim)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, "nothing playing")
            return

        line1_rect = QRect(rect.x(), rect.y(), rect.width(), fm.height())
        line2_rect = QRect(rect.x(), rect.y() + fm.height() + 2, rect.width(), fm.height())

        # line 1: artist — title
        p.setPen(fg)
        line1 = (
            f"{self._artist} — {self._title}" if (self._artist and self._title)
            else (self._title or self._artist)
        )
        line1 = fm.elidedText(line1.lower(), Qt.ElideRight, line1_rect.width())
        p.drawText(line1_rect, Qt.AlignVCenter | Qt.AlignLeft, line1)

        # line 2: album · status (dim)
        line2_parts: list[str] = []
        if self._album:
            line2_parts.append(self._album.lower())
        if self._status:
            line2_parts.append(self._status.lower())
        line2 = "  ·  ".join(line2_parts)
        if line2:
            p.setPen(dim)
            line2 = fm.elidedText(line2, Qt.ElideRight, line2_rect.width())
            p.drawText(line2_rect, Qt.AlignVCenter | Qt.AlignLeft, line2)
