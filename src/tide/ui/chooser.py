"""The "choose your tide" chooser: two panes, each a live preview built
from that personality's real theme tokens. ``PersonalityPane`` is shared
with the wizard's aesthetic step — do not fork it.

Design contract:
  * Never writes settings — resolves to a preset id
    (``chosen``/``choice()``) or to nothing; the caller persists.
  * Panes wear their OWN theme via per-widget sheets (which outrank the
    app sheet), so ambient theme changes can't bleed in. No live user
    layer either — glyph/text-case overrides are per-personality state.
  * Backdrops animate on the PERSONALITY's motion intensity, never the
    app's; system reduced-motion stills the modern pane too.
  * Per-frame work never touches tokens or QSS: tick a float, call
    ``update()``. ≤24fps. The timer is a child of the pane.
  * Nothing blocking, nothing remote — covers are generated pixmaps;
    constructing offscreen with no screen is a supported case.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import glyphs, presets, theming
from . import motion, scale
from .headings import line_heading
from .spring_slider import SpringSlider


# Left-to-right pane order; also the keyboard order.
PANE_ORDER: tuple[str, ...] = ("brutalist", "modern")

BACKDROP_FPS = 24
BACKDROP_INTERVAL_MS = max(1, round(1000 / BACKDROP_FPS))

# Blob travel per frame, radians. A full orbit takes ~20s.
_PHASE_STEP = 0.013

# For headless / offscreen, where there is no screen to measure.
FALLBACK_SIZE: tuple[int, int] = (1100, 720)


# The mock's fake track — deliberately not a real artist.
DEMO_TITLE = "long way from the shore"
DEMO_ARTIST = "the undertow"
DEMO_ELAPSED = "1:04"
DEMO_TOTAL = "3:42"
DEMO_FRACTION = 0.28


TRAITS: dict[str, tuple[str, ...]] = {
    "brutalist": (
        "monospace type, hard edges, no gradients",
        "nothing moves. ever.",
        "[bracket] buttons and block meters",
        "album art still shows — art is content, not chrome",
    ),
    "modern": (
        "color pulled live from the album art",
        "springy controls with magnetic detents",
        "backdrops that breathe with the track",
        "soft corners and room to breathe",
    ),
}


# ---------------------------------------------------------------- theme


def pane_theme(preset_id: str):
    """The builtin's theme slug, else any theme whose ``[meta] aesthetic``
    matches, else ``None`` (readers fall back to hex defaults)."""
    try:
        themes = theming.discover_themes()
    except Exception:
        return None
    slug = ""
    definition = presets.BUILTINS.get(preset_id)
    if definition is not None:
        slug = definition.theme
    found = themes.get(slug)
    if found is not None:
        return found
    for candidate in themes.values():
        if str(getattr(candidate, "aesthetic", "")) == preset_id:
            return candidate
    return None


def _hex(theme, key: str, default: str) -> str:
    return theme.token(key, default) if theme is not None else default


def _col(theme, key: str, default: str) -> QColor:
    return QColor(_hex(theme, key, default))


def _layout_int(theme, key: str, default: int) -> int:
    if theme is None:
        return default
    try:
        return int(theme.t("layout", key, default))
    except (TypeError, ValueError):
        return default


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend, so derived shades stay inside the theme's palette."""
    t = max(0.0, min(1.0, float(t)))
    return QColor(
        int(round(a.red() + (b.red() - a.red()) * t)),
        int(round(a.green() + (b.green() - a.green()) * t)),
        int(round(a.blue() + (b.blue() - a.blue()) * t)),
    )


# ---------------------------------------------------------------- glyphs


def _demo_glyph(key: str) -> str:
    """``DEFAULT_PACK``, not ``glyphs.glyph()``: overrides are
    per-personality (a STASH_FIELD), so the live resolver would dress
    BOTH panes in the user's swaps."""
    return glyphs.DEFAULT_PACK[key]


# ---------------------------------------------------------------- art


def placeholder_art(size: int, theme, *, radius: int = 0) -> QPixmap:
    """A generated cover tile from the pane's own tokens — the chooser
    fetches nothing."""
    size = max(8, int(size))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    bg = _col(theme, "bg_alt", "#141414")
    fg = _col(theme, "fg", "#e6e6e6")
    dim = _col(theme, "dim", "#6f6f6f")
    accent = _col(theme, "accent", "#d4b95e")
    p = QPainter(pm)
    try:
        rect = QRectF(0.5, 0.5, size - 1.0, size - 1.0)
        if radius > 0:
            p.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(rect, float(radius), float(radius))
            p.setClipPath(path)
            grad = QRadialGradient(
                QPointF(size * 0.28, size * 0.22), size * 1.05)
            grad.setColorAt(0.0, _mix(bg, accent, 0.55))
            grad.setColorAt(0.55, _mix(bg, accent, 0.22))
            grad.setColorAt(1.0, bg)
            p.fillPath(path, QBrush(grad))
            p.setClipping(False)
            p.setPen(QPen(_mix(bg, fg, 0.18), 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        else:
            p.fillRect(pm.rect(), bg)
            p.setPen(QPen(fg, 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)
            # A record mark, not crossed hairlines — those read as a
            # broken image. AA for the circles only.
            p.setRenderHint(QPainter.Antialiasing, True)
            c = QPointF(size / 2.0, size / 2.0)
            p.setPen(QPen(dim, 1.0))
            p.setBrush(QBrush(_mix(bg, fg, 0.08)))
            p.drawEllipse(c, size * 0.34, size * 0.34)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_mix(bg, dim, 0.55), 1.0))
            p.drawEllipse(c, size * 0.26, size * 0.26)
            p.drawEllipse(c, size * 0.19, size * 0.19)
            p.setPen(QPen(dim, 1.0))
            p.setBrush(QBrush(_mix(bg, fg, 0.30)))
            p.drawEllipse(c, size * 0.10, size * 0.10)
            p.setBrush(QBrush(bg))
            p.setPen(Qt.NoPen)
            p.drawEllipse(c, size * 0.02, size * 0.02)
            p.setRenderHint(QPainter.Antialiasing, False)
    finally:
        p.end()
    return pm


def play_disc(size: int, theme) -> QPixmap:
    """Modern's play control: an accent disc with a cut-out triangle."""
    size = max(8, int(size))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    accent = _col(theme, "accent", "#9b87f5")
    bg = _col(theme, "bg", "#0a0a0a")
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QRectF(0.0, 0.0, float(size), float(size)))
        tri = QPainterPath()
        left = size * 0.40
        right = size * 0.68
        top = size * 0.30
        bottom = size * 0.70
        tri.moveTo(left, top)
        tri.lineTo(right, size * 0.5)
        tri.lineTo(left, bottom)
        tri.closeSubpath()
        p.setBrush(bg)
        p.drawPath(tri)
    finally:
        p.end()
    return pm


# ---------------------------------------------------------------- widgets


class _PreviewSpring(SpringSlider):
    """A real SpringSlider painting in the PANE's theme. SpringSlider
    reads tokens from the live theme manager at paint time, so the
    manager's effective-theme accessor is shadowed for one synchronous
    paint and restored in a ``finally`` — safe because paint is
    GUI-thread and non-reentrant.
    """

    def __init__(self, theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preview_theme = theme
        # The dialog owns the arrow keys — the slider stays mouse-only.
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, ev) -> None:
        theme = self._preview_theme
        if theme is None:
            super().paintEvent(ev)
            return
        mgr = theming.manager()
        mgr.current_effective = lambda: theme
        try:
            super().paintEvent(ev)
        finally:
            try:
                del mgr.current_effective
            except AttributeError:
                pass


class _PaneButton(QPushButton):
    """The pane's commit control, styled from the PANE's tokens. Not
    ``BracketButton`` — that binds to the app-wide theme, so both panes
    would render identically and restyle under the preview. The label
    comes from the host: the dialog's button commits, the wizard's only
    selects.
    """

    def __init__(self, theme, *, bracket: bool, label: str = "choose",
                 font_family: str = "", font_pt: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bracket = bool(bracket)
        self._font_family = str(font_family or "")
        self._font_pt = int(font_pt or 0)
        self.setObjectName("paneChoose")
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        # Focus lives on the dialog so left/right/enter always reach it.
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.set_label(label)
        self._apply(theme)

    def set_label(self, label: str) -> None:
        word = str(label or "choose")
        self.setText(f"[{word}]" if self._bracket else word)

    def _apply(self, theme) -> None:
        fg = _hex(theme, "fg", "#e6e6e6")
        bg = _hex(theme, "bg", "#0b0b0b")
        accent = _hex(theme, "accent", "#d4b95e")
        sel_bg = _hex(theme, "sel_bg", fg)
        sel_fg = _hex(theme, "sel_fg", bg)
        radius = _layout_int(theme, "radius_px", 0)
        # Font must be QSS, or the base sheet's universal font rule wins.
        font = ""
        if self._font_family:
            font += f' font-family: "{self._font_family}";'
        if self._font_pt > 0:
            font += f" font-size: {self._font_pt}pt;"
        if self._bracket:
            self.setStyleSheet(
                f"QPushButton#paneChoose {{ background: transparent;"
                f" color: {fg}; border: none;{font}"
                f" padding: {scale.px(6)}px {scale.px(12)}px; }}"
                f"QPushButton#paneChoose:hover {{ background: {sel_bg};"
                f" color: {sel_fg}; }}"
            )
        else:
            lift = _mix(QColor(accent), QColor(fg), 0.25).name()
            self.setStyleSheet(
                f"QPushButton#paneChoose {{ background: {accent};"
                f" color: {bg}; border: none;{font}"
                f" border-radius: {scale.px(max(radius * 2, 8))}px;"
                f" padding: {scale.px(9)}px {scale.px(22)}px; }}"
                f"QPushButton#paneChoose:hover {{ background: {lift}; }}"
            )


class PersonalityPane(QWidget):
    """One side of the chooser: a self-contained preview of one
    personality, built from that personality's real theme.

    ``clicked(preset_id)`` fires on the body or the button; the host
    decides what a click means. ``set_selected`` draws the highlight
    ring. The backdrop timer starts/stops on show/hide;
    ``start_animation``/``stop_animation`` are for hosts that swap panes
    in a stack without hiding them. ``compact`` tightens margins/sizes
    for the 720×600 wizard; ``choose_label``/``selected_label`` re-word
    the button so a select-only click visibly lands.
    """

    clicked = Signal(str)

    def __init__(self, preset_id: str, parent: QWidget | None = None, *,
                 compact: bool = False, choose_label: str = "choose",
                 selected_label: str = "") -> None:
        super().__init__(parent)
        self._compact = bool(compact)
        self._choose_label = str(choose_label or "choose")
        self._selected_label = str(selected_label or "")
        self._preset_id = str(preset_id)
        self._def = presets.builtin(self._preset_id)   # KeyError on unknown
        self._theme = pane_theme(self._preset_id)
        self._selected = False
        self._phase = 0.0
        self._pressed = False
        # Panes name theme font families directly — make sure the
        # bundled fonts are in the database first.
        try:
            theming.register_bundled_fonts()
        except Exception:
            pass

        self.setObjectName(f"personalityPane_{self._preset_id}")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # A floor — the layout's own minimum is larger and wins.
        self.setMinimumSize(scale.px(240), scale.px(280))
        self._style_children()
        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(BACKDROP_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ facts

    @property
    def preset_id(self) -> str:
        return self._preset_id

    @property
    def theme(self):
        """This pane's ``Theme`` (``None`` with no themes on disk)."""
        return self._theme

    def animates(self) -> bool:
        """The personality's own motion intensity, clamped by
        reduced-motion (see module contract)."""
        if motion.Intensity.parse(self._def.motion) is motion.Intensity.OFF:
            return False
        return not motion.reduced_motion()

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self._sync_button_label()
        self.update()

    def _sync_button_label(self) -> None:
        # Only hosts that asked for a selected wording get one.
        if not self._selected_label:
            return
        button = getattr(self, "_button", None)
        if button is None:
            return
        button.set_label(self._selected_label if self._selected
                         else self._choose_label)

    # ------------------------------------------------------------ build

    def _typography(self) -> tuple[str, float]:
        family = ""
        base = 10.0
        if self._theme is not None:
            family = str(self._theme.t("typography", "family", "") or "")
            try:
                base = float(self._theme.t("typography", "size_pt", 10))
            except (TypeError, ValueError):
                base = 10.0
        return family, base

    def _style_children(self) -> None:
        """One per-widget stylesheet for the whole subtree, from this
        pane's own tokens. Fonts must live here, not in ``setFont`` —
        every base QSS carries a universal font rule, and a stylesheet
        font beats a programmatic one.
        """
        fg = _hex(self._theme, "fg", "#e6e6e6")
        dim = _hex(self._theme, "dim", "#6f6f6f")
        accent = _hex(self._theme, "accent", "#d4b95e")
        # Copy text lifted off pure @dim (some themes set it very low).
        soft = _mix(QColor(dim), QColor(fg), 0.45).name()
        softer = _mix(QColor(dim), QColor(fg), 0.30).name()
        family, base = self._typography()
        fam_rule = f' font-family: "{family}";' if family else ""
        body = scale.round_pt(base)
        pitch = scale.round_pt(base + (1 if self._compact else 2))
        trait = scale.round_pt(base + (0 if self._compact else 1))
        name = scale.round_pt(base + (6 if self._compact else 10))
        glyph = scale.round_pt(base + 4)
        track = scale.round_pt(
            base + (2 if self._preset_id == "modern" else 0))
        self.setStyleSheet(
            f"QLabel {{ color: {fg}; background: transparent;"
            f"{fam_rule} font-size: {body}pt; }}"
            f"QLabel#paneName {{ font-size: {name}pt; font-weight: bold; }}"
            f"QLabel#panePitch {{ color: {soft}; font-size: {pitch}pt; }}"
            f"QLabel#paneTrait {{ color: {softer}; font-size: {trait}pt; }}"
            f"QLabel#paneDim {{ color: {dim}; }}"
            f"QLabel#paneAccent {{ color: {accent}; }}"
            f"QLabel#paneTrack {{ font-weight: bold; font-size: {track}pt; }}"
            f"QLabel#paneGlyph {{ color: {dim}; font-size: {glyph}pt; }}"
            # Transparent, or base QSS gives the mock the APP's bg and
            # the ambient theme punches a hole in the preview.
            f"QWidget#paneMock {{ background: transparent; }}"
        )

    def _label(self, text: str, *, kind: str = "",
               wrap: bool = False) -> QLabel:
        lab = QLabel(text)
        if kind:
            lab.setObjectName(kind)
        lab.setWordWrap(wrap)
        # Click-transparent, so the whole pane stays one target.
        lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        return lab

    def _build(self) -> None:
        col = QVBoxLayout(self)
        if self._compact:
            col.setContentsMargins(*scale.margins(16, 13, 16, 13))
            col.setSpacing(scale.px(6))
        else:
            col.setContentsMargins(*scale.margins(26, 24, 26, 22))
            col.setSpacing(scale.px(10))

        col.addWidget(self._label(self._def.label, kind="paneName"))
        col.addWidget(self._label(self._def.blurb, kind="panePitch",
                                  wrap=True))
        col.addStretch(1)

        mock = (self._build_modern_mock() if self._preset_id == "modern"
                else self._build_brutalist_mock())
        col.addWidget(mock)
        col.addSpacing(scale.px(8 if self._compact else 14))

        marker = "· "
        if self._theme is not None:
            raw = str(self._theme.t("layout", "list_marker", "") or "")
            if raw.strip():
                marker = raw
        traits = TRAITS.get(self._preset_id, ())
        if self._compact:
            traits = traits[:3]
        for trait in traits:
            col.addWidget(self._label(f"{marker}{trait}", kind="paneTrait",
                                      wrap=True))

        col.addStretch(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        family, base = self._typography()
        self._button = _PaneButton(
            self._theme,
            bracket=self._preset_id != "modern",
            label=self._choose_label,   # set_selected re-labels later
            font_family=family,
            font_pt=scale.round_pt(base + 1),
        )
        self._button.clicked.connect(self._emit_clicked)
        if self._preset_id == "modern":
            row.addStretch(1)
            row.addWidget(self._button)
            row.addStretch(1)
        else:
            row.addWidget(self._button)
            row.addStretch(1)
        col.addLayout(row)

    def _mock_frame(self) -> QWidget:
        frame = QWidget(self)
        frame.setObjectName("paneMock")
        frame.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return frame

    def _build_brutalist_mock(self) -> QWidget:
        frame = self._mock_frame()
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(scale.px(6))

        # Pane theme + shipped dash — case/dash overrides are
        # per-personality user state (see _demo_glyph).
        box.addWidget(self._label(
            line_heading("now playing", 34, theme=self._theme,
                         dash=_demo_glyph("heading_dash")),
            kind="paneDim"))

        tile = scale.px(48 if self._compact else 64)
        art = QLabel()
        art.setPixmap(placeholder_art(tile, self._theme, radius=0))
        art.setFixedSize(tile, tile)
        art.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(scale.px(2))
        text.addWidget(self._label(DEMO_TITLE, kind="paneTrack"))
        text.addWidget(self._label(DEMO_ARTIST, kind="paneDim"))
        text.addStretch(1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(scale.px(10))
        top.addWidget(art)
        top.addLayout(text, 1)
        box.addLayout(top)

        cells = 24
        done = int(round(cells * DEMO_FRACTION))
        bar = "▮" * done + "▯" * (cells - done)
        box.addWidget(self._label(f"[{bar}]", kind="paneAccent"))
        box.addWidget(self._label(f"{DEMO_ELAPSED} / {DEMO_TOTAL}",
                                  kind="paneDim"))
        box.addWidget(self._label(
            f"[{_demo_glyph('prev')}]  [{_demo_glyph('play')}]"
            f"  [{_demo_glyph('next')}]"))
        return frame

    def _build_modern_mock(self) -> QWidget:
        frame = self._mock_frame()
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(scale.px(10))

        tile = scale.px(54 if self._compact else 72)
        radius = max(_layout_int(self._theme, "radius_px", 6), 4)
        art = QLabel()
        art.setPixmap(placeholder_art(tile, self._theme, radius=radius))
        art.setFixedSize(tile, tile)
        art.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(scale.px(3))
        text.addStretch(1)
        text.addWidget(self._label(DEMO_TITLE, kind="paneTrack"))
        text.addWidget(self._label(DEMO_ARTIST, kind="paneDim"))
        text.addStretch(1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(scale.px(12))
        top.addWidget(art)
        top.addLayout(text, 1)
        box.addLayout(top)

        total_s = 222.0
        self._slider = _PreviewSpring(self._theme, self)
        self._slider.set_range(0.0, total_s, 1.0)
        self._slider.set_detents([total_s * f for f in (0.0, 0.25, 0.5,
                                                        0.75, 1.0)])
        self._slider.set_formatter(
            lambda v: f"{int(v) // 60}:{int(v) % 60:02d}")
        self._slider.set_value(total_s * DEMO_FRACTION)
        box.addWidget(self._slider)

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(scale.px(16))
        transport.addStretch(1)
        transport.addWidget(self._label(_demo_glyph("prev"),
                                        kind="paneGlyph"))
        disc = QLabel()
        disc.setPixmap(play_disc(scale.px(34), self._theme))
        disc.setFixedSize(scale.px(34), scale.px(34))
        disc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        transport.addWidget(disc)
        transport.addWidget(self._label(_demo_glyph("next"),
                                        kind="paneGlyph"))
        transport.addStretch(1)
        box.addLayout(transport)
        return frame

    # ------------------------------------------------------------ motion

    def _tick(self) -> None:
        # The whole per-frame path: advance a float, request a repaint.
        self._phase += _PHASE_STEP
        if self._phase > math.tau:
            self._phase -= math.tau
        self.update()

    def start_animation(self) -> None:
        if not self.animates() or self._timer.isActive():
            return
        self._timer.start()

    def stop_animation(self) -> None:
        self._timer.stop()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self.start_animation()

    def hideEvent(self, ev) -> None:
        self.stop_animation()
        super().hideEvent(ev)

    def closeEvent(self, ev) -> None:
        self.stop_animation()
        super().closeEvent(ev)

    # ------------------------------------------------------------ paint

    def _card_radius(self) -> float:
        """0 for brutalist (its theme declares radius 0); 2× the theme
        radius for modern so the card reads soft at card scale."""
        return float(scale.px(_layout_int(self._theme, "radius_px", 0) * 2,
                              minimum=0))

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        bg = _col(self._theme, "bg", "#0b0b0b")
        fg = _col(self._theme, "fg", "#e6e6e6")
        accent = _col(self._theme, "accent", "#d4b95e")
        border_col = _col(self._theme, "border_col", fg.name())
        radius = self._card_radius()
        width = float(max(1, _layout_int(self._theme, "border_px", 1)))
        if self._selected:
            border_col = accent
            width = max(width, 2.0)
        rect = QRectF(self.rect()).adjusted(
            width / 2.0, width / 2.0, -width / 2.0, -width / 2.0)

        # Backdrop presence is the PERSONALITY's call (brutalist declares
        # adaptive_background False). With motion off the gradient still
        # paints — it just doesn't drift.
        if radius > 0.0 or self._def.adaptive_background:
            p.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            p.fillPath(path, QBrush(bg))
            if self._def.adaptive_background:
                p.save()
                p.setClipPath(path)
                self._paint_backdrop(p, rect, bg, fg, accent)
                p.restore()
            p.setPen(QPen(border_col, width))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        else:
            # Brutalist: no antialiasing, no gradient, no rounding.
            p.fillRect(self.rect(), bg)
            p.setPen(QPen(border_col, width))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)

    def _paint_backdrop(self, p: QPainter, rect: QRectF, bg: QColor,
                        fg: QColor, accent: QColor) -> None:
        """Two slowly orbiting accent blobs, colors from the pane's own
        tokens."""
        w = rect.width()
        h = rect.height()
        ph = self._phase
        blobs = (
            (accent, 0.30 + 0.16 * math.sin(ph),
             0.30 + 0.13 * math.cos(ph * 0.8), 0.85, 0.28),
            (_mix(accent, fg, 0.45), 0.74 + 0.14 * math.cos(ph * 0.63),
             0.72 + 0.12 * math.sin(ph * 1.17), 0.70, 0.16),
        )
        for color, fx, fy, frad, strength in blobs:
            center = QPointF(rect.left() + w * fx, rect.top() + h * fy)
            grad = QRadialGradient(center, max(w, h) * frad)
            near = _mix(bg, color, strength)
            far = QColor(near)
            far.setAlpha(0)
            grad.setColorAt(0.0, near)
            grad.setColorAt(1.0, far)
            p.fillRect(rect, QBrush(grad))

    # ------------------------------------------------------------ input

    def _emit_clicked(self) -> None:
        self.clicked.emit(self._preset_id)

    def mousePressEvent(self, ev) -> None:
        # Accept, or Qt routes the press→release sequence to the parent
        # and the pane stops being one big target.
        if ev.button() == Qt.LeftButton:
            self._pressed = True
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        pressed, self._pressed = getattr(self, "_pressed", False), False
        if (pressed and ev.button() == Qt.LeftButton
                and self.rect().contains(ev.position().toPoint())):
            ev.accept()
            self._emit_clicked()
            return
        super().mouseReleaseEvent(ev)


# ---------------------------------------------------------------- dialog


class ChooserDialog(QDialog):
    """The full-screen two-pane dialog. NEVER writes settings — the
    caller reads ``choice()`` (or connects ``chosen``) and applies.
    Esc / close-X resolve to no choice. The commit is deferred one loop
    turn via a child ``QTimer`` so nothing tears the dialog down inside
    a click handler — the modal-from-click crash class. Let the loop
    turn once (or use ``exec()``) before reading the outcome.
    """

    chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None, *,
                 initial: str = "brutalist") -> None:
        super().__init__(parent)
        self.setObjectName("chooserDialog")
        self.setWindowTitle("choose your tide")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setModal(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._choice = ""
        self._panes: dict[str, PersonalityPane] = {}
        self._focus_index = (PANE_ORDER.index(initial)
                             if initial in PANE_ORDER else 0)

        self._build()
        self._sync_selection()
        self._size_to_screen()

        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.timeout.connect(self._finish)

    # ------------------------------------------------------------ build

    def _build(self) -> None:
        theme = theming.manager().current_effective()
        fg = _hex(theme, "fg", "#e6e6e6")
        dim = _mix(QColor(_hex(theme, "dim", "#6f6f6f")),
                   QColor(fg), 0.35).name()
        base = 10.0
        if theme is not None:
            try:
                base = float(theme.t("typography", "size_pt", 10))
            except (TypeError, ValueError):
                base = 10.0
        # Dialog chrome wears the LIVE theme on purpose — it frames the
        # two products without being either.
        self.setStyleSheet(
            f"QLabel#chooserTitle {{ color: {fg}; background: transparent;"
            f" font-size: {scale.round_pt(base + 12)}pt; font-weight: bold; }}"
            f"QLabel#chooserSub {{ color: {dim}; background: transparent;"
            f" font-size: {scale.round_pt(base + 2)}pt; }}"
            f"QLabel#chooserFooter {{ color: {dim};"
            f" background: transparent; }}"
            f"QWidget#chooserContent {{ background: transparent; }}"
        )

        title = QLabel("choose your tide")
        title.setObjectName("chooserTitle")
        title.setAlignment(Qt.AlignCenter)

        sub = QLabel("same library, same songs — two completely "
                     "different players.")
        sub.setObjectName("chooserSub")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)

        panes_row = QHBoxLayout()
        panes_row.setContentsMargins(0, 0, 0, 0)
        panes_row.setSpacing(scale.px(20))
        for preset_id in PANE_ORDER:
            pane = PersonalityPane(preset_id)
            pane.clicked.connect(self._choose)
            self._panes[preset_id] = pane
            panes_row.addWidget(pane, 1)

        footer = QLabel(
            "you can change your mind any time in settings · appearance."
            "    ← → to compare · enter to choose · esc to decide later"
        )
        footer.setObjectName("chooserFooter")
        footer.setAlignment(Qt.AlignCenter)
        footer.setWordWrap(True)

        content = QWidget(self)
        content.setObjectName("chooserContent")
        # Cap the block and centre it. Uncapped, 1440p turns the panes
        # into slivers; a fixed cap leaves an island in a black screen —
        # so the cap scales (~72%), floored so small screens stay uncapped.
        geo = _available_geometry()
        cap_w = int(geo.width() * 0.72) if geo is not None else 0
        cap_h = int(geo.height() * 0.75) if geo is not None else 0
        content.setMaximumWidth(max(scale.px(1500), cap_w))
        content.setMaximumHeight(max(scale.px(900), cap_h))
        inner = QVBoxLayout(content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)
        inner.addWidget(title)
        inner.addSpacing(scale.px(6))
        inner.addWidget(sub)
        inner.addSpacing(scale.px(24))
        inner.addLayout(panes_row, 1)
        inner.addSpacing(scale.px(18))
        inner.addWidget(footer)

        # Stretches only centre the leftover; content's maximums limit.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(content, 1000)
        row.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*scale.margins(36, 32, 36, 26))
        outer.addStretch(1)
        outer.addLayout(row, 1000)
        outer.addStretch(1)

    def _size_to_screen(self) -> None:
        geo = _available_geometry()
        if geo is not None:
            self.setGeometry(geo)
        else:
            self.resize(*FALLBACK_SIZE)

    # ------------------------------------------------------------ facts

    def choice(self) -> str:
        """The chosen preset id, or "" while undecided / after
        dismissal. Set at click time, before the deferred close."""
        return self._choice

    def panes(self) -> dict[str, PersonalityPane]:
        return dict(self._panes)

    def pane(self, preset_id: str) -> PersonalityPane | None:
        return self._panes.get(preset_id)

    def focused_preset(self) -> str:
        return PANE_ORDER[self._focus_index]

    # ------------------------------------------------------------ input

    def _sync_selection(self) -> None:
        focused = self.focused_preset()
        for preset_id, pane in self._panes.items():
            pane.set_selected(preset_id == focused)

    def _move_focus(self, step: int) -> None:
        self._focus_index = (self._focus_index + step) % len(PANE_ORDER)
        self._sync_selection()

    def _choose(self, preset_id: str) -> None:
        if self._choice or preset_id not in self._panes:
            return          # already committed — ignore the second click
        self._choice = preset_id
        if preset_id in PANE_ORDER:
            self._focus_index = PANE_ORDER.index(preset_id)
            self._sync_selection()
        self._stop_animations()
        # Off the click handler's stack before anything closes this.
        self._commit_timer.start(0)

    def _finish(self) -> None:
        preset_id = self._choice
        if not preset_id:
            return
        self.accept()
        self.chosen.emit(preset_id)

    def keyPressEvent(self, ev) -> None:
        key = ev.key()
        if key in (Qt.Key_Left, Qt.Key_Backtab, Qt.Key_Up):
            self._move_focus(-1)
            ev.accept()
            return
        if key in (Qt.Key_Right, Qt.Key_Tab, Qt.Key_Down):
            self._move_focus(1)
            ev.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._choose(self.focused_preset())
            ev.accept()
            return
        super().keyPressEvent(ev)   # Esc → reject(), no choice, no signal

    # ------------------------------------------------------------ life

    def _stop_animations(self) -> None:
        for pane in self._panes.values():
            pane.stop_animation()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        # Every child is NoFocus, so arrows / enter always land here.
        self.setFocus(Qt.OtherFocusReason)

    def hideEvent(self, ev) -> None:
        self._stop_animations()
        super().hideEvent(ev)

    def closeEvent(self, ev) -> None:
        self._stop_animations()
        super().closeEvent(ev)

    # ------------------------------------------------------------ paint

    def paintEvent(self, _ev) -> None:
        # Read at paint time, not cached — track the live theme's bg.
        theme = theming.manager().current_effective()
        bg = _col(theme, "bg", "#0b0b0b")
        p = QPainter(self)
        p.fillRect(self.rect(), bg)


def _available_geometry():
    """The primary screen's usable rect, or ``None`` with no screen."""
    try:
        screen = QGuiApplication.primaryScreen()
    except Exception:
        return None
    if screen is None:
        return None
    try:
        geo = screen.availableGeometry()
    except Exception:
        return None
    if geo.width() <= 0 or geo.height() <= 0:
        return None
    return geo
