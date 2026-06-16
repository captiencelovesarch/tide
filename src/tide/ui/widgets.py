"""Theme-aware custom widgets.

Everything subscribes to ThemeManager.theme_changed so a runtime theme swap
re-paints without a restart. Pure-QSS widgets get their styling from the
stylesheet directly; the custom-painted ones (MonoProgress, AlbumArt) read
tokens and repaint here.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
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
        # Optional decorative icon glyph rendered before the label (e.g. a
        # nav-rail icon set). Distinct from ``_glyph`` which REPLACES the
        # label when the theme's control_style is "glyph".
        self._icon: str | None = None
        # Optional SVG body for an image icon. When set, takes precedence
        # over ``_icon`` and renders via QPushButton's native QIcon slot
        # using the active theme's fg color (substituted for the SVG's
        # ``currentColor`` token).
        self._svg_text: str | None = None
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

    def setIcon(self, icon) -> None:  # type: ignore[override]
        """Polymorphic setter. Strings (or None) set the unicode glyph
        prefix; a QIcon takes the native Qt path. Most callers should use
        ``setIconGlyph`` / ``setSvgIcon`` directly — this exists so
        existing code calling ``btn.setIcon("X")`` keeps compiling."""
        if isinstance(icon, str) or icon is None:
            self.setIconGlyph(icon)
        else:
            super().setIcon(icon)

    def setIconGlyph(self, glyph: str | None) -> None:
        """Set a small unicode glyph rendered before the label. Pass
        ``None`` to remove. Clears any active SVG icon — the two modes
        don't coexist (one or the other, not both)."""
        self._icon = glyph
        if glyph is not None:
            self._svg_text = None
            super().setIcon(QIcon())
        self._update_text()

    def setSvgIcon(self, svg_text: str | None) -> None:
        """Set an SVG-rendered image icon (recolored to match the active
        theme's fg). Pass ``None`` to remove. Clears any glyph prefix —
        the modes are mutually exclusive."""
        self._svg_text = svg_text
        if svg_text is not None:
            self._icon = None
            self._refresh_svg_icon()
        else:
            super().setIcon(QIcon())
        self._update_text()

    def _refresh_svg_icon(self) -> None:
        if not self._svg_text:
            return
        # Recolor: replace SVG's ``currentColor`` token with the active
        # theme's fg so the icon sits visually with the label text.
        fg = "#e6e6e6"
        if getattr(self, "_theme", None) is not None:
            fg = self._theme.token("fg", "#e6e6e6")
        svg = self._svg_text.replace("currentColor", fg)
        try:
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        except Exception:
            return
        # Pull scale.px so the icon grows with ui_scale alongside text.
        try:
            from . import scale as _scale
            target = _scale.px(16)
        except Exception:
            target = 16
        pix = QPixmap(target, target)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        super().setIcon(QIcon(pix))
        self.setIconSize(QSize(target, target))

    def _apply_theme(self, theme) -> None:
        self._theme = theme
        # Re-render SVG icon (if any) against the new fg color so it tracks
        # theme + adaptive accent changes seamlessly.
        if self._svg_text:
            self._refresh_svg_icon()
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
            base = self._glyph
        elif style == "icon":
            base = self._label  # full icon-font mode lands when SVG icons ship
        else:
            base = f"[{self._label}]"
        # Decorative icon prefix (nav icon set). Prepended to whatever the
        # style chose so it works in bracket / glyph / icon modes alike.
        if self._icon:
            self.setText(f"{self._icon} {base}")
        else:
            self.setText(base)


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
        from . import scale as _scale
        self.setFixedHeight(_scale.px(22))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        # Theme re-emit fires after a scale change too; re-apply scaled
        # height so this progress bar tracks the new ui_scale live.
        from . import scale as _scale
        self.setFixedHeight(_scale.px(22))
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
        from . import scale as _scale
        self.setFixedHeight(_scale.px(22))
        self.setMinimumWidth(_scale.px(150))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("scroll to adjust volume")

    def _on_theme(self, theme) -> None:
        self._theme = theme
        from . import scale as _scale
        self.setFixedHeight(_scale.px(22))
        self.setMinimumWidth(_scale.px(150))
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
        self._base_size = size
        from . import scale as _scale
        self._size = _scale.px(size)
        self.setFixedSize(self._size, self._size)
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
        # Re-derive scaled size from the base so a ui_scale change picked
        # up via theme_changed resizes the tile.
        from . import scale as _scale
        new_size = _scale.px(self._base_size)
        if new_size != self._size:
            self._size = new_size
            self.setFixedSize(self._size, self._size)
        self.setStyleSheet(
            f"QLabel {{ background: {bg}; border: 1px solid {fg}; "
            f"border-radius: {radius}px; color: {fg}; }}"
        )
        if self._pixmap_raw is None:
            self._render_empty()
        else:
            self._render(self._pixmap_raw)

    def setImage(self, image: QImage | None) -> None:
        from . import motion as motion_module

        if image is None or image.isNull():
            self._pixmap_raw = None
            self._render_empty()
            return
        new_raw = QPixmap.fromImage(image)
        new_scaled = new_raw.scaled(
            self._size, self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.FastTransformation,
        )
        # Capture what's currently on screen so the crossfade has a "from".
        # Reading from QLabel.pixmap() lets us cross from whatever the user
        # last saw, including a mid-crossfade intermediate frame if the
        # tracks change rapidly — the helper's prior-cancellation makes this
        # safe.
        old_display = self.pixmap()
        self._pixmap_raw = new_raw
        # The helper snaps when old_display is null/empty (first-ever load
        # or coming from the "[no art]" state), and respects motion=OFF
        # globally. No special-casing here.
        motion_module.crossfade_pixmap(
            setter=self.setPixmap,
            old_pixmap=old_display,
            new_pixmap=new_scaled,
            owner=self,
        )

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
        from . import scale as _scale
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(_scale.px(40))

    def _on_theme(self, theme) -> None:
        self._theme = theme
        from . import scale as _scale
        self.setMinimumHeight(_scale.px(40))
        self.update()

    def setTrack(self, artist: str, title: str, album: str = "") -> None:
        self._artist = artist
        self._title = title
        self._album = album
        self.update()

    def setTrackAnimated(self, artist: str, title: str, album: str = "") -> None:
        """Animated variant of ``setTrack``. Decodes the title (and, at
        FULL intensity, the artist + album rows) via a left-to-right scramble.

        Intensity rules:
          * OFF — falls through to ``setTrack`` (no animation).
          * LITE — title decodes only; artist + album snap to new values.
          * FULL — all three decode concurrently with staggered durations so
            the title resolves first, then artist, then album (cascade feel
            without QTimer-based offsets — each scramble is independent).

        Skip-condition: when the new tuple matches the current display,
        falls through to ``setTrack`` (replay-same-track edge — animating
        text that's already on screen would look broken).
        """
        from . import motion as motion_module

        if (
            motion_module.intensity() == motion_module.Intensity.OFF
            or (artist == self._artist and title == self._title and album == self._album)
        ):
            self.setTrack(artist, title, album)
            return

        # Commit the target values up front. The scramble immediately
        # overwrites ``_title`` (and ``_artist`` / ``_album`` in FULL) with
        # its frame-0 paint, so the new real values never flash before the
        # decode begins — but for LITE mode the artist/album rows do need
        # to be set so the paint reads them correctly.
        self._artist = artist
        self._title = title
        self._album = album

        # Title always decodes (LITE + FULL).
        motion_module.scramble_text(
            lambda s: self._scramble_frame("title", s),
            title,
            dur=motion_module.DUR_MED,
            owner=self,
            kind="scramble/title",
        )

        if motion_module.intensity() == motion_module.Intensity.FULL:
            # Longer durations produce a slower decode → arrives later →
            # cascade feel. No QTimer offsets needed; the scramble's own
            # linear-stagger schedules each char's reveal across its dur.
            motion_module.scramble_text(
                lambda s: self._scramble_frame("artist", s),
                artist,
                dur=motion_module.DUR_MED + 150,
                owner=self,
                kind="scramble/artist",
            )
            motion_module.scramble_text(
                lambda s: self._scramble_frame("album", s),
                album,
                dur=motion_module.DUR_MED + 300,
                owner=self,
                kind="scramble/album",
            )

    def _scramble_frame(self, field: str, frame: str) -> None:
        """Single per-frame setter for the scramble cascade. Writes into the
        right state slot and triggers a repaint. Inlined as one method so
        the closure captured by ``scramble_text`` is just ``(field, frame)``
        and we avoid creating a lambda per field-call."""
        if field == "title":
            self._title = frame
        elif field == "artist":
            self._artist = frame
        elif field == "album":
            self._album = frame
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
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, theming.styled_case("nothing playing", self._theme))
            return

        line1_rect = QRect(rect.x(), rect.y(), rect.width(), fm.height())
        line2_rect = QRect(rect.x(), rect.y() + fm.height() + 2, rect.width(), fm.height())

        # line 1: artist — title
        p.setPen(fg)
        line1 = (
            f"{self._artist} — {self._title}" if (self._artist and self._title)
            else (self._title or self._artist)
        )
        line1 = fm.elidedText(theming.styled_case(line1, self._theme), Qt.ElideRight, line1_rect.width())
        p.drawText(line1_rect, Qt.AlignVCenter | Qt.AlignLeft, line1)

        # line 2: album · status (dim)
        line2_parts: list[str] = []
        if self._album:
            line2_parts.append(theming.styled_case(self._album, self._theme))
        if self._status:
            line2_parts.append(theming.styled_case(self._status, self._theme))
        line2 = "  ·  ".join(line2_parts)
        if line2:
            p.setPen(dim)
            line2 = fm.elidedText(line2, Qt.ElideRight, line2_rect.width())
            p.drawText(line2_rect, Qt.AlignVCenter | Qt.AlignLeft, line2)
