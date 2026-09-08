"""Theme-aware custom widgets.

Everything subscribes to ThemeManager.theme_changed so a runtime theme swap
re-paints without a restart. Pure-QSS widgets get their styling from the
stylesheet directly; the custom-painted ones (MonoProgress, AlbumArt) read
tokens and repaint here.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget

from .. import theming
from . import marquee
from .marquee import Marquee


def _color(theme, name: str, default: str) -> QColor:
    return QColor(theme.token(name, default)) if theme else QColor(default)


def paint_popover_panel(widget: QWidget, theme) -> None:
    """Paint an opaque, rounded panel with a hairline for a translucent
    top-level popover. A stylesheet ``background`` on a QFrame under
    ``WA_TranslucentBackground`` never reaches the window buffer (the
    speed and fx popovers floated as bare controls over the backdrop),
    so the spring faces paint their own."""
    from .. import material
    fill, line = material.panel_colors(theme)
    radius = float(theming.effective_radius_px(theme))
    p = QPainter(widget)
    p.setRenderHint(QPainter.Antialiasing, True)
    rect = QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setPen(QPen(line, 1.0))
    p.setBrush(fill)
    p.drawRoundedRect(rect, radius, radius)
    p.end()


# Rendered SVG pixmaps by (svg text, ink, px). Every modern BracketButton
# re-resolves its icon on each theme_changed, and the adaptive driver emits
# that per track — without this each emission would re-rasterize ~100 SVGs
# twice (normal + disabled). Bounded: a theme flip with a new fg adds one
# entry per icon, so a few hundred covers a long session.
_SVG_PIX_CACHE: dict[tuple[str, str, int], QPixmap] = {}
_SVG_PIX_CACHE_MAX = 512


class BracketButton(QPushButton):
    """Text button rendered like `[play]` — in brutalist. In modern the
    same widget wears a bare label on a translucent surface, or an SVG
    icon when the action has one, styled by the base sheet instead of
    inline (see ``_base.qss``, "BracketButton, the modern face").

    brutalist honors the theme's control_style:
      - "bracket"  -> "[label]"
      - "glyph"    -> uses `glyph` (e.g. "▶") if supplied, else label
      - "icon"     -> falls back to label

    modern ignores control_style. Its face is decided per button:
      - an explicit icon key (``setIconKey``) or a glyph the registry
        knows (``glyphs.key_for``) that has an SVG → that icon
      - a glyph the user retyped in the glyph editor → the glyph text,
        never an icon (the override is the user's face for that button)
      - otherwise the bare label

    ``role`` shapes it: "pill" (text, the default), "icon" (icon only),
    "transport" (the strip's play row), "row" (the nav rail). Auto when
    unset: icon-only for a key-derived icon, pill otherwise. ``primary``
    marks the play button; ``current`` the rail's active view.
    """

    # Icon pixel sizes before ui-scale, by role / size hint. Every
    # transport size sits under PRIMARY_ICON_PX so play stays the biggest
    # thing in the row whichever variant a layout picked.
    _ICON_PX = {"icon": 18, "pill": 16, "row": 18, "transport": 20,
                "large": 24, "compact": 16}
    # Subclasses (LargeButton / CompactButton / IconButton) pin these.
    ICON_SIZE_HINT: str = ""
    DEFAULT_ROLE: str = ""

    def __init__(self, label: str, glyph: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._glyph = glyph
        # Latched "mode is on" look (shuffle / repeat): accent text while
        # set. Purely visual — callers own the actual mode state.
        self._active_state = False
        # Optional decorative icon glyph rendered before the label (e.g. a
        # nav-rail icon set). Distinct from ``_glyph`` which REPLACES the
        # label when the theme's control_style is "glyph".
        self._icon: str | None = None
        # Optional SVG body for an image icon. When set, takes precedence
        # over ``_icon`` and renders via QPushButton's native QIcon slot
        # using the active theme's fg color (substituted for the SVG's
        # ``currentColor`` token).
        self._svg_text: str | None = None
        # modern-only: an icon looked up by key in icons/svg (brutalist
        # ignores it and keeps its text face).
        self._icon_key: str | None = None
        self._role: str = ""
        self._role_applied: str = ""
        self._primary = False
        self._current = False
        self._muted = False
        self._auto_tooltip = False
        # True while the modern face has a key-derived SVG in the native
        # icon slot, so a flip back to brutalist knows to clear it.
        self._modern_icon = False
        # True while the modern row role has widened the size policy.
        self._row_expanded = False
        # modern: show the glyph as text even when an icon exists (the
        # "bracket" transport variant keeps its typed faces).
        self._prefer_text = False
        # Collapsed rail: icon only, label in the tooltip.
        self._label_hidden = False
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._apply_theme(theming.manager().current_effective())
        theming.manager().theme_changed.connect(self._apply_theme)

    def setLabel(self, label: str) -> None:
        self._label = label
        self._update_text()

    def setGlyph(self, glyph: str | None) -> None:
        self._glyph = glyph
        self._update_text()

    def setActiveState(self, on: bool) -> None:
        """Latch/unlatch the accent "mode on" look. QSS reads it via the
        ``activeState`` dynamic property; repolish makes the change land
        immediately instead of on the next style event."""
        on = bool(on)
        if on == self._active_state:
            return
        self._active_state = on
        self.setProperty("activeState", on)
        self._repolish()
        if self._modern():
            self._update_text()     # icon ink follows the state
        self.update()

    def activeState(self) -> bool:
        return self._active_state

    # ----- modern-only shape controls (no-ops for brutalist's face) -----

    def setIconKey(self, key: str | None) -> None:
        """Name an SVG in icons/svg for the modern face. brutalist keeps
        its text. Pass None to fall back to the glyph / label."""
        self._icon_key = key or None
        self._update_text()

    def iconKey(self) -> str | None:
        return self._icon_key

    def setRole(self, role: str) -> None:
        """Force a shape: "pill" / "icon" / "transport" / "row". Empty
        string returns to automatic."""
        self._role = str(role or "")
        self._update_text()

    def role(self) -> str:
        return self._role_applied

    def setPrimary(self, on: bool) -> None:
        """The play button's filled circle (modern)."""
        on = bool(on)
        if on == self._primary:
            return
        self._primary = on
        self.setProperty("primary", on)
        self._repolish()
        self._update_text()

    def setCurrent(self, on: bool) -> None:
        """The rail's active view (modern: surface + accent)."""
        on = bool(on)
        if on == self._current:
            return
        self._current = on
        self.setProperty("current", on)
        self._repolish()
        self._update_text()

    def isCurrent(self) -> bool:
        return self._current

    def setPreferText(self, on: bool) -> None:
        """modern: keep the glyph as text instead of swapping in its icon."""
        on = bool(on)
        if on == self._prefer_text:
            return
        self._prefer_text = on
        self._update_text()

    def setLabelHidden(self, on: bool) -> None:
        """Icon-only face with the label as tooltip (the collapsed rail).
        modern: the icon role. brutalist: the glyph prefix alone, or
        nothing but the SVG."""
        on = bool(on)
        if on == self._label_hidden:
            return
        self._label_hidden = on
        self._update_text()

    def setMuted(self, on: bool) -> None:
        """Dim face for a control whose feature is off but clickable (the
        fx rack bypassed). Distinct from disabled: still takes input."""
        on = bool(on)
        if on == self._muted:
            return
        self._muted = on
        self.setProperty("muted", on)
        self._repolish()
        self._update_text()

    def _repolish(self) -> None:
        st = self.style()
        st.unpolish(self)
        st.polish(self)

    # ----- icons -----

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
        else:
            super().setIcon(QIcon())
        self._update_text()

    def _modern(self) -> bool:
        return getattr(self._theme, "aesthetic", "") == "modern"

    def _tok(self, name: str, default: str) -> str:
        theme = getattr(self, "_theme", None)
        return theme.token(name, default) if theme is not None else default

    # The primary (play) circle: icon + padding, one size for every
    # variant so the circle is a circle. ``theming._substitute`` derives
    # @radius_round from the same numbers — keep the two in step.
    PRIMARY_ICON_PX = 24
    PRIMARY_PAD_PX = 10

    @classmethod
    def primary_diameter(cls) -> int:
        from . import scale as _scale
        return _scale.px(cls.PRIMARY_ICON_PX) + 2 * _scale.px(cls.PRIMARY_PAD_PX)

    def _icon_px(self, role: str) -> int:
        from . import scale as _scale
        if self._primary:
            return _scale.px(self.PRIMARY_ICON_PX)
        hint = type(self).ICON_SIZE_HINT
        base = self._ICON_PX.get(hint) or self._ICON_PX.get(role) or 18
        return _scale.px(base)

    def _fit_primary(self) -> None:
        """Pin the primary button square so QSS's round radius fits; let
        it go again when the button stops being primary or leaves modern.
        Qt does not clamp a border-radius wider than the box, it just
        stops drawing it round, so the box has to match the radius."""
        if self._primary and self._modern():
            d = self.primary_diameter()
            if self.minimumSize() != QSize(d, d):
                self.setFixedSize(d, d)
        elif self.minimumSize() == self.maximumSize() and self.minimumWidth() > 0:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)

    def _render_svg(self, svg_text: str, ink: str, px: int) -> QPixmap | None:
        key = (svg_text, ink, px)
        hit = _SVG_PIX_CACHE.get(key)
        if hit is not None:
            return hit
        try:
            renderer = QSvgRenderer(QByteArray(
                svg_text.replace("currentColor", ink).encode("utf-8")))
        except Exception:
            return None
        pix = QPixmap(px, px)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        if len(_SVG_PIX_CACHE) >= _SVG_PIX_CACHE_MAX:
            _SVG_PIX_CACHE.clear()
        _SVG_PIX_CACHE[key] = pix
        return pix

    def _refresh_svg_icon(self, svg_text: str | None = None, *,
                          ink: str | None = None, px: int | None = None) -> None:
        """Render ``svg_text`` (default: the explicit nav SVG) into the
        native icon slot. brutalist: fg ink at 16px, as it always was.
        modern passes ink / size for the role and gets a dim disabled
        state too, since ``color: @dim`` can't reach a pixmap."""
        svg_text = svg_text if svg_text is not None else self._svg_text
        if not svg_text:
            return
        if ink is None:
            ink = self._tok("fg", "#e6e6e6")
        if px is None:
            try:
                from . import scale as _scale
                px = _scale.px(16)
            except Exception:
                px = 16
        pix = self._render_svg(svg_text, ink, px)
        if pix is None:
            return
        icon = QIcon(pix)
        if self._modern():
            dim = self._render_svg(svg_text, self._tok("dim", "#666"), px)
            if dim is not None:
                icon.addPixmap(dim, QIcon.Disabled)
        super().setIcon(icon)
        self.setIconSize(QSize(px, px))

    def _apply_theme(self, theme) -> None:
        self._theme = theme
        # All styling lives in QSS for BracketButton, set as object name so
        # the stylesheet can target it precisely.
        self.setObjectName("BracketButton")
        if self._modern():
            # The base sheet owns the face (role / primary / current rules).
            self.setStyleSheet("")
            self._update_text()
            self.update()
            return
        self._fit_primary()
        if self._row_expanded:
            self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            self._row_expanded = False
        if self._modern_icon:
            # Back from modern: drop the icon it put here (an explicit nav
            # SVG survives — re-rendered just below) and its tooltip.
            super().setIcon(QIcon())
            self._modern_icon = False
            if self._auto_tooltip:
                self.setToolTip("")
                self._auto_tooltip = False
        # Re-render SVG icon (if any) against the new fg color so it tracks
        # theme + adaptive accent changes seamlessly.
        if self._svg_text:
            self._refresh_svg_icon()
        self._update_text()
        bg = theme.token("bg", "#000") if theme else "#000"
        fg = theme.token("fg", "#fff") if theme else "#fff"
        hover_bg = theme.token("sel_bg", fg) if theme else fg
        hover_fg = theme.token("sel_fg", bg) if theme else bg
        dim = theme.token("dim", "#666") if theme else "#666"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        self.setStyleSheet(
            f"QPushButton#BracketButton {{"
            f"  background: transparent;"
            f"  color: {fg};"
            f"  border: none;"
            f"  padding: 4px 8px;"
            f"}}"
            f'QPushButton#BracketButton[activeState="true"] {{ color: {accent}; }}'
            f'QPushButton#BracketButton[current="true"] {{ color: {accent}; }}'
            f"QPushButton#BracketButton:hover {{"
            f"  background: {hover_bg};"
            f"  color: {hover_fg};"
            f"}}"
            f"QPushButton#BracketButton:disabled {{ color: {dim}; }}"
        )
        self.update()

    def _update_text(self) -> None:
        if self._modern():
            self._update_modern_face()
            return
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
        if self._label_hidden and (self._icon or self._svg_text):
            # Collapsed rail: the glyph prefix alone, or just the SVG.
            self.setText(self._icon or "")
            if self._label and not self._auto_tooltip and not self.toolTip():
                self.setToolTip(self._label)
                self._auto_tooltip = True
        elif self._icon:
            self.setText(f"{self._icon} {base}")
            if self._auto_tooltip:
                self.setToolTip("")
                self._auto_tooltip = False
        else:
            self.setText(base)
            if self._auto_tooltip:
                self.setToolTip("")
                self._auto_tooltip = False

    def _resolve_svg(self) -> tuple[str | None, bool]:
        """(svg text, key_derived). An explicit nav SVG wins; then the
        icon key; then a glyph the registry recognises. A glyph the user
        retyped resolves to no key (glyphs.key_for), so it stays text."""
        if self._svg_text:
            return self._svg_text, False
        if self._prefer_text:
            return None, False
        from .. import glyphs
        from . import nav_icons
        key = self._icon_key or glyphs.key_for(self._glyph or "")
        if key and not glyphs.is_overridden(key):
            svg = nav_icons.svg_text_for(key)
            if svg:
                return svg, True
        return None, False

    def _update_modern_face(self) -> None:
        from .. import glyphs
        svg, key_derived = self._resolve_svg()

        # Text candidate: a glyph without an icon shows as itself (bare); a
        # label shows bare. When the icon carries a glyph face that also
        # leads the label ("zzz 12m" behind a moon), drop that face.
        base = self._glyph if (self._glyph and svg is None) else self._label
        face_stripped = False
        if svg is not None and self._icon_key in glyphs.DEFAULT_PACK:
            face = glyphs.glyph(self._icon_key)
            if face and base.startswith(face):
                base = base[len(face):].strip()
                face_stripped = True

        role = self._role or type(self).DEFAULT_ROLE
        if not role:
            if svg is not None and key_derived:
                # Icon-only, unless a state suffix survived the face strip
                # (an armed sleep timer: moon + "12m").
                role = "pill" if (face_stripped and base) else "icon"
            else:
                role = "pill"
        if self._label_hidden and (svg is not None or self._icon):
            role = "icon"
        text_hidden = (svg is not None and role in ("icon", "transport")) or (
            self._label_hidden and self._icon is not None and svg is None)

        if svg is not None:
            if self._primary:
                ink = self._tok("bg", "#000")
            elif self._active_state or self._current:
                ink = self._tok("accent", "#d4b95e")
            elif self._muted:
                ink = self._tok("dim", "#666")
            else:
                ink = self._tok("fg", "#e6e6e6")
            self._refresh_svg_icon(svg, ink=ink, px=self._icon_px(role))
            self._modern_icon = not self._svg_text
        else:
            super().setIcon(QIcon())
            self._modern_icon = False

        if text_hidden and self._label_hidden and self._icon and svg is None:
            text = self._icon                      # the glyph alone
        elif text_hidden:
            text = ""
        else:
            text = f"{self._icon} {base}" if (self._icon and svg is None) else base
        self.setText(text)
        self._fit_primary()

        # Icon-only buttons keep their name for the tooltip, unless the
        # caller set a richer one (the strip's shortcut tips).
        if text_hidden and self._label and self._label != self._glyph:
            if not self.toolTip() or self._auto_tooltip:
                self.setToolTip(self._label)
                self._auto_tooltip = True
        elif self._auto_tooltip:
            self.setToolTip("")
            self._auto_tooltip = False

        # Rail rows fill the rail so the hover / current surface spans it.
        if role == "row" and not self._row_expanded:
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._row_expanded = True
        elif role != "row" and self._row_expanded:
            self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            self._row_expanded = False

        if role != self._role_applied:
            self._role_applied = role
            self.setProperty("role", role)
            self._repolish()


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
        self._theme = theming.manager().current_effective()
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
        self._theme = theming.manager().current_effective()
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

    # Left-click released inside the tile. Declared on the base class so the
    # circle/polaroid variants inherit it — the now-playing strip uses it to
    # open the mini player, the mini player uses it to come back.
    clicked = Signal()

    def __init__(self, size: int = 96, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._base_size = size
        from . import scale as _scale
        self._size = _scale.px(size)
        self.setFixedSize(self._size, self._size)
        self.setAlignment(Qt.AlignCenter)
        self._pixmap_raw: QPixmap | None = None
        self._radius = 0
        self._pressed = False
        self._press_pos = None
        # The 1px fg border is part of the tile look in lists/strip; big
        # standalone art (the mini player) turns it off via set_framed.
        self._framed = True
        self._theme = theming.manager().current_effective()
        self._apply_theme(self._theme)
        theming.manager().theme_changed.connect(self._apply_theme)
        self._render_empty()

    def set_framed(self, framed: bool) -> None:
        framed = bool(framed)
        if framed != self._framed:
            self._framed = framed
            self._apply_theme(self._theme)

    def set_base_size(self, base: int) -> None:
        """Re-anchor the unscaled base size after construction. The
        fullscreen window re-derives its art size per monitor, so the
        tile can't be pinned to whatever screen it was first built for.
        Goes through _apply_theme so the ui-scale multiply and a
        re-render at the new size both happen."""
        base = max(1, int(base))
        if base == self._base_size:
            return
        self._base_size = base
        self._apply_theme(self._theme)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._pressed = True
            self._press_pos = ev.position().toPoint()
            # Accept so the press doesn't propagate to a parent that starts a
            # window drag (the mini player moves itself on body presses).
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        # A press that travels is a drag, not a click. Hand it to the window
        # system as a move so the art stays a grabbable surface.
        if self._pressed:
            from PySide6.QtWidgets import QApplication
            moved = (ev.position().toPoint() - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._pressed = False
                win = self.window()
                handle = win.windowHandle() if win is not None else None
                if handle is not None:
                    handle.startSystemMove()
                ev.accept()
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        was_pressed = self._pressed
        self._pressed = False
        # Release outside the tile = cancelled press, same as a QPushButton.
        if was_pressed and ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)

    def _apply_theme(self, theme) -> None:
        self._theme = theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        # The frame color is aesthetic-dependent. Brutalist themes want the
        # hard fg box — it's part of the tile look. On modern themes fg is
        # near-white, which read as a glowing outline around every cover;
        # they frame with their border token instead, like their other cards.
        if theme is not None and getattr(theme, "aesthetic", "modern") == "modern":
            frame_col = theme.token("border_col", theme.token("border_dim", fg))
        else:
            frame_col = fg
        # Use the *effective* radius (corner-style override or theme base) so
        # the tile matches every QSS-styled widget. QSS border-radius only
        # rounds the QLabel's border/background — it never clips the pixmap —
        # so we also mask the art itself to this radius in _shape().
        radius = theming.effective_radius_px(theme)
        self._radius = radius
        # Re-derive scaled size from the base so a ui_scale change picked
        # up via theme_changed resizes the tile.
        from . import scale as _scale
        new_size = _scale.px(self._base_size)
        if new_size != self._size:
            self._size = new_size
            self.setFixedSize(self._size, self._size)
        border = f"1px solid {frame_col}" if self._framed else "none"
        self.setStyleSheet(
            f"QLabel {{ background: {bg}; border: {border}; "
            f"border-radius: {radius}px; color: {fg}; }}"
        )
        if self._pixmap_raw is None:
            self._render_empty()
        else:
            self._render(self._pixmap_raw)

    def _shape(self, scaled: QPixmap) -> QPixmap:
        """Round the art's corners to the active radius so it sits flush
        inside the rounded border instead of poking square corners through
        it. No-op when corners are sharp (radius 0) — returns the pixmap
        untouched so the brutalist look is bit-for-bit unchanged. Subclasses
        that paint their own shape (circle, polaroid) leave ``_radius`` at 0,
        so their inherited code paths skip this too.
        """
        r = self._radius
        if r <= 0 or scaled.isNull():
            return scaled
        size = self._size
        out = QPixmap(size, size)
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), float(r), float(r))
        painter.setClipPath(path)
        # KeepAspectRatioByExpanding can return a pixmap larger than the
        # tile in one axis; centre it so the crop matches QLabel's own
        # AlignCenter behaviour before the round.
        painter.drawPixmap((size - scaled.width()) // 2,
                           (size - scaled.height()) // 2, scaled)
        painter.end()
        return out

    def setImage(self, image: QImage | None) -> None:
        from . import motion as motion_module

        if image is None or image.isNull():
            self._pixmap_raw = None
            self._render_empty()
            return
        new_raw = QPixmap.fromImage(image)
        new_scaled = self._shape(new_raw.scaled(
            self._size, self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.FastTransformation,
        ))
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
        scaled = self._shape(pix.scaled(
            self._size, self._size,
            Qt.KeepAspectRatioByExpanding,
            Qt.FastTransformation,
        ))
        self.setPixmap(scaled)

    def _render_empty(self) -> None:
        self.setPixmap(QPixmap())
        # brutalist keeps its bracketed placeholder; modern says it plain.
        modern = getattr(theming.manager().current_effective(), "aesthetic", "") == "modern"
        self.setText("no art" if modern else "[no art]")


class NowPlayingLabel(QWidget):
    """artist — title  (album)   |   small dim status row underneath.

    Clicking it opens the song page (v1.5) — the label is the one thing on
    the strip that *names* the track, so it's the natural handle for
    "tell me more about this". Cursor advertises it; window wires it.
    """

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._artist = ""
        self._title = ""
        self._album = ""
        self._status = ""
        # v1.5 community numbers ("84.2m plays · 1.1m likes · 2016").
        # Rides in the dim line beside album/status; empty = absent, so
        # sources without insights change nothing about the paint.
        self._insights = ""
        self._theme = theming.manager().current_effective()
        theming.manager().theme_changed.connect(self._on_theme)
        from . import scale as _scale
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(_scale.px(40))
        self.setCursor(Qt.PointingHandCursor)
        # A title wider than the strip scrolls instead of losing its tail.
        # The tooltip is the always-available answer underneath it: it
        # works at motion OFF, and it's the only one that works while the
        # marquee is parked at the head of a very long title.
        self._marquee = Marquee(self)
        self._marquee.tick.connect(self.update)
        # Track-change transitions (text_fx): one per painted line, and
        # the text each line last showed, which is what a reveal starts
        # from. The variants paint through _paint_line to get both.
        from . import text_fx as _text_fx
        self._reveal = {"primary": _text_fx.TextReveal(self, "primary"),
                        "secondary": _text_fx.TextReveal(self, "secondary")}
        self._painted = {"primary": "", "secondary": ""}

    def _paint_line(self, p: QPainter, rect, text: str, fm: QFontMetrics, field: str, *,
                    color: QColor, flags=Qt.AlignVCenter | Qt.AlignLeft,
                    scroll: bool = False) -> None:
        """Draw one line, through its transition when one is running."""
        self._painted[field] = text
        reveal = self._reveal.get(field)
        if reveal is not None and reveal.active():
            accent = _color(self._theme, "accent", "#d4b95e")
            reveal.paint(p, rect, text, fm, fg=color, accent=accent, flags=flags)
            return
        p.setPen(color)
        if scroll:
            marquee.draw_text(p, rect, text, fm, self._marquee, flags=flags)
        else:
            p.drawText(rect, flags, fm.elidedText(text, Qt.ElideRight, rect.width()))

    def _primary_text(self) -> str:
        """The 'artist — title' run — what the marquee scrolls and what
        the tooltip leads with."""
        if self._artist and self._title:
            return f"{self._artist} — {self._title}"
        return self._title or self._artist

    def _refresh_tooltip(self) -> None:
        parts = [self._primary_text()]
        for extra in (self._album, self._insights, self._status):
            if extra:
                parts.append(extra)
        self.setToolTip("\n".join(p for p in parts if p))

    def hideEvent(self, ev) -> None:
        # Nothing to scroll for while we're off screen.
        self._marquee.stop()
        super().hideEvent(ev)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        from . import scale as _scale
        self.setMinimumHeight(_scale.px(40))
        self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)

    def setTrack(self, artist: str, title: str, album: str = "") -> None:
        self._artist = artist
        self._title = title
        self._album = album
        self._refresh_tooltip()
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
        from . import text_fx as _text_fx

        if (
            motion_module.intensity() == motion_module.Intensity.OFF
            or (artist == self._artist and title == self._title and album == self._album)
        ):
            self.setTrack(artist, title, album)
            return

        style = _text_fx.effective_style()
        if style in ("sweep", "rise"):
            # Painted transitions: the line starts from what it last
            # showed. FULL runs both lines; LITE runs the primary only.
            old_primary = self._painted.get("primary", "")
            old_secondary = self._painted.get("secondary", "")
            self._artist, self._title, self._album = artist, title, album
            self._marquee.stop()
            self._refresh_tooltip()
            self._reveal["primary"].start(old_primary)
            if motion_module.intensity() == motion_module.Intensity.FULL:
                self._reveal["secondary"].start(old_secondary, dur=motion_module.dur("med") + 150)
            self.update()
            return
        if style == "off":
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
        # From the target values, not the scramble frames — the tooltip
        # must read the real title while the decode is still running.
        self._refresh_tooltip()

        # Title always decodes (LITE + FULL).
        motion_module.scramble_text(
            lambda s: self._scramble_frame("title", s),
            title,
            dur=motion_module.dur("med"),
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
                dur=motion_module.dur("med") + 150,
                owner=self,
                kind="scramble/artist",
            )
            motion_module.scramble_text(
                lambda s: self._scramble_frame("album", s),
                album,
                dur=motion_module.dur("med") + 300,
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
        self._refresh_tooltip()
        self.update()

    def setInsights(self, text: str) -> None:
        if text == self._insights:
            return
        self._insights = text
        self._refresh_tooltip()
        self.update()

    def clear(self) -> None:
        self._artist = self._title = self._album = self._status = ""
        self._insights = ""
        self._marquee.stop()
        self._refresh_tooltip()
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

        # line 1: artist — title. Scrolls when it overruns the strip;
        # elides (exactly as it always did) at motion OFF.
        line1 = theming.styled_case(self._primary_text(), self._theme)
        self._paint_line(p, line1_rect, line1, fm, "primary", color=fg, scroll=True)

        # line 2: album · insights · status (dim)
        line2_parts: list[str] = []
        if self._album:
            line2_parts.append(theming.styled_case(self._album, self._theme))
        if self._insights:
            line2_parts.append(theming.styled_case(self._insights, self._theme))
        if self._status:
            line2_parts.append(theming.styled_case(self._status, self._theme))
        line2 = "  ·  ".join(line2_parts)
        if line2:
            self._paint_line(p, line2_rect, line2, fm, "secondary", color=dim)
