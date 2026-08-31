"""The chooser — "choose your tide".

The full-screen moment where tide 2.0 sells two products at a glance:
one library, two completely different players. Two side-by-side panes,
each a LIVE preview built from that personality's real theme — the same
``theme.toml`` tokens and typography the app itself would wear — so what
the user picks is literally what they get. No screenshots, no hardcoded
palette, no "artist's impression".

Three hosts share the same pane widget:
  * this dialog (the update-into-2.0 route and the settings re-pick),
  * the first-launch wizard's aesthetic step (phase 4B reuses
    ``PersonalityPane`` directly — do not fork the design).

Contracts kept here:
  * **The chooser never writes settings.** It resolves to a preset id
    (``chosen``/``choice()``) or to nothing, and the caller persists.
    Tools emit, callers persist.
  * **Panes wear their own theme, not the app's.** Every color, font and
    corner radius comes from ``theming.discover_themes()`` → the preset's
    theme slug. Nothing here reads the live theme except the dialog's own
    backdrop (which *should* blend with whatever is on screen). A pane is
    therefore immune to an ambient theme change: its styling is set once
    from its own tokens and per-widget stylesheets outrank the app sheet.
  * **No live user layer reaches a pane.** The same rule applied to text:
    glyph overrides and the sticky text-case override are both
    per-personality user state, so the mock reads ``glyphs.DEFAULT_PACK``
    (``_demo_glyph``) and the "now playing" rule is built with the pane's
    own theme (``headings.line_heading(theme=…)``). Otherwise one side's
    customizations would dress the other side's pitch.
  * **Brutalist is still. Always.** The animated backdrop is gated on the
    *personality's own* motion intensity (brutalist's builtin is ``off``),
    not on the ambient one — each pane previews its archetype, and the
    zero-animation promise is part of the brutalist product. A system
    reduced-motion signal stills the modern pane too.
  * **Per-frame work never touches tokens or QSS.** The modern backdrop
    ticks a float and calls ``update()``; that's the whole frame path.
    ≤24fps by construction.
  * **No timer outlives its widget.** The backdrop timer is a child of
    the pane (dies with it) and is stopped on hide/close.
  * **Nothing blocking, nothing remote.** No audio, no network, no art
    fetch — the covers are generated pixmaps. Constructing this offscreen
    with no screen at all is a supported case.
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


# Left-to-right order of the panes. Also the keyboard order.
PANE_ORDER: tuple[str, ...] = ("brutalist", "modern")

# The animated backdrop's frame cap. 24fps is plenty for a slow gradient
# drift and keeps the chooser cheap on an integrated GPU.
BACKDROP_FPS = 24
BACKDROP_INTERVAL_MS = max(1, round(1000 / BACKDROP_FPS))

# How far the gradient's blobs travel per frame, in radians. A full orbit
# takes ~20s — movement you notice only if you look for it.
_PHASE_STEP = 0.013

# Size the dialog falls back to when there is no screen to measure
# (headless / offscreen edge cases).
FALLBACK_SIZE: tuple[int, int] = (1100, 720)


# The fake track every pane pretends to be playing. Deliberately not a
# real artist: this is a mock, and it should read as one on close
# inspection while still feeling like music from across the room.
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
    """The ``Theme`` a pane for ``preset_id`` should wear.

    Resolution order: the personality's builtin theme slug, then any
    discovered theme whose ``[meta] aesthetic`` matches, then ``None``
    (every reader falls back to a hex default, so a stripped install
    still draws something honest rather than crashing).
    """
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
    """Linear blend of two colors. Used for derived shades so the panes
    never introduce a color the theme didn't declare."""
    t = max(0.0, min(1.0, float(t)))
    return QColor(
        int(round(a.red() + (b.red() - a.red()) * t)),
        int(round(a.green() + (b.green() - a.green()) * t)),
        int(round(a.blue() + (b.blue() - a.blue()) * t)),
    )


# ---------------------------------------------------------------- glyphs


def _demo_glyph(key: str) -> str:
    """A glyph for the mock, straight off the shipped pack (transport
    faces, and the ``heading_dash`` the brutalist rule is drawn with).

    ``glyphs.DEFAULT_PACK`` rather than ``glyphs.glyph()`` on purpose:
    the override layer is per-personality (a STASH_FIELD), so the live
    resolver would dress BOTH panes in whatever the user last swapped on
    the side they happen to be wearing — a modern preview showing the
    brutalist ▶ they typed last week. The chooser sells the products as
    they ship. Reading the registry rather than retyping the characters
    keeps one vocabulary: if ▮▮-style precision work ever moves a glyph
    (see glyphs.py's docstring for why these were chosen the hard way),
    the preview moves with the app instead of quietly drifting.
    """
    return glyphs.DEFAULT_PACK[key]


# ---------------------------------------------------------------- art


def placeholder_art(size: int, theme, *, radius: int = 0) -> QPixmap:
    """A generated cover tile. The chooser fetches nothing — no network,
    no cache, no disk — so the art is drawn from the pane's own tokens:
    a soft accent wash on a modern tile, a flat framed square with a mono
    slash on a brutalist one. Art shows in BOTH personalities; only its
    frame changes."""
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
            # A mono record mark. Crossed hairlines read as "broken
            # image" — directly under the pane's "album art still shows"
            # bullet, the placeholder has to read as art that IS there,
            # just monochrome. Curves are fine here: it's content, not
            # chrome. AA on for the circles only.
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
    """The modern pane's transport hero: an accent disc with a cut-out
    triangle. Brutalist gets ``[▶]`` text instead — that's the point."""
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
    """A real SpringSlider that paints in the PANE's theme.

    SpringSlider reads its tokens from the live theme manager at paint
    time — deliberately, so the adaptive driver can retint it mid-track.
    The chooser needs it wearing modern's palette while the app is still
    dressed in whatever the user has on (possibly a light brutalist
    theme), and the widget is out of this phase's scope to change. So the
    manager's effective-theme accessor is shadowed for the duration of
    ONE synchronous paint and restored in a ``finally``. Nothing else can
    observe it: paint is GUI-thread, non-reentrant, and the swap spans a
    single ``super().paintEvent`` call.
    """

    def __init__(self, theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preview_theme = theme
        # The dialog owns the arrow keys (left/right compares the panes),
        # so the slider stays a mouse-only flourish.
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
    """The pane's commit control, styled from the PANE's tokens.

    Not ``BracketButton``: that one binds itself to the app-wide theme
    (its label shape comes from the live theme's ``control_style``), so
    both panes would render identically and would restyle out from under
    the preview on any ambient theme tick. Here the brutalist pane always
    gets a ``[bracketed]`` label with the inverted hover, and the modern
    pane always gets a soft accent pill — because that difference IS the
    pitch.

    The word inside comes from the HOST, because what the button does
    depends on the host: in the standalone dialog it commits, so it says
    "choose"; in the wizard it only selects, so a button that said
    "choose" and then did nothing visible would read as broken.
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
        """Set the word, keeping the pane's own bracket shape."""
        word = str(label or "choose")
        self.setText(f"[{word}]" if self._bracket else word)

    def _apply(self, theme) -> None:
        fg = _hex(theme, "fg", "#e6e6e6")
        bg = _hex(theme, "bg", "#0b0b0b")
        accent = _hex(theme, "accent", "#d4b95e")
        sel_bg = _hex(theme, "sel_bg", fg)
        sel_fg = _hex(theme, "sel_fg", bg)
        radius = _layout_int(theme, "radius_px", 0)
        # Same reason the labels carry their font in QSS: the theme base
        # sheet's universal rule would otherwise dress this button in the
        # APP's typeface.
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
    """One side of the chooser: a live, self-contained preview of one
    personality, built from that personality's real theme.

    Reusable on purpose — the first-launch wizard's aesthetic step hosts
    the same widget so there is exactly one implementation of the pitch.

    Hosts decide what a click means. The pane only reports:
      * ``clicked(preset_id)`` — the body or the [choose] button was hit.
        The standalone dialog treats that as a commit; the wizard treats
        it as a selection and commits on [next].
      * ``set_selected(bool)`` draws the highlight ring.

    The pane starts/stops its own backdrop timer on show/hide, so a host
    that forgets can't leak one; ``start_animation``/``stop_animation``
    are there for hosts that swap panes in a stack without hiding them.

    ``compact`` tightens everything that can be tightened (margins, the
    name size, the art tile, one fewer trait) for hosts with a fixed,
    smaller canvas — the onboarding wizard is 720×600, where the full
    pane's ~480px minimum height would not fit. Same widget, same tokens,
    same pitch: only the breathing room changes.

    ``choose_label`` / ``selected_label`` are the other half of "hosts
    decide what a click means": the button says what it will DO here.
    The dialog keeps the default "choose" because there the button
    commits; the wizard, where a click only moves the selection ring,
    passes its own wording and a second word for the selected state so
    the click visibly lands.
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
        # Bundled IBM Plex Mono / Sans — the panes name theme families
        # directly, so make sure they're in the font database first.
        try:
            theming.register_bundled_fonts()
        except Exception:
            pass

        self.setObjectName(f"personalityPane_{self._preset_id}")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # A floor, not the real minimum — the layout's own minimum (the
        # wrapped copy plus the mock) is larger and wins.
        self.setMinimumSize(scale.px(240), scale.px(280))
        self._style_children()
        self._build()

        # Child timer: dies with the pane, no matter what the host does.
        self._timer = QTimer(self)
        self._timer.setInterval(BACKDROP_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ facts

    @property
    def preset_id(self) -> str:
        return self._preset_id

    @property
    def theme(self):
        """The ``Theme`` this pane is wearing (may be ``None`` on an
        install with no themes on disk)."""
        return self._theme

    def animates(self) -> bool:
        """Whether this pane's backdrop moves.

        Gated on the PERSONALITY's own motion intensity, not the app's:
        each pane previews its archetype, and brutalist's builtin is
        ``off`` — zero animation is the product, not a preference. A
        system reduced-motion signal stills the modern pane too.
        """
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
        """Only hosts that asked for a selected wording get one — the
        standalone dialog's "selected" is keyboard focus, not a decision,
        and a button that renamed itself on every arrow key would be
        claiming a commit that hasn't happened."""
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
        """One per-widget stylesheet for the whole subtree, built from
        this pane's own tokens and typography.

        Fonts have to live here, not in ``setFont``: every theme's base
        QSS carries a universal ``* { font-family; font-size }`` rule,
        and a stylesheet font beats a programmatic one. Routing size and
        family through this sheet is what actually makes the brutalist
        pane mono and the modern pane sans while the app wears neither.

        Set once, on the widget: a widget-level sheet outranks the app
        sheet, so an ambient theme change can never bleed into a preview
        (and never costs this pane a restyle either).
        """
        fg = _hex(self._theme, "fg", "#e6e6e6")
        dim = _hex(self._theme, "dim", "#6f6f6f")
        accent = _hex(self._theme, "accent", "#d4b95e")
        # The pitch and the trait list are the chooser's own copy, not
        # mock chrome — lifted off pure @dim (which some themes set very
        # low) so they're readable, still derived from the theme's own
        # two text tokens.
        soft = _mix(QColor(dim), QColor(fg), 0.45).name()
        softer = _mix(QColor(dim), QColor(fg), 0.30).name()
        family, base = self._typography()
        fam_rule = f' font-family: "{family}";' if family else ""
        body = scale.round_pt(base)
        pitch = scale.round_pt(base + (1 if self._compact else 2))
        trait = scale.round_pt(base + (0 if self._compact else 1))
        name = scale.round_pt(base + (6 if self._compact else 10))
        glyph = scale.round_pt(base + 4)
        # The modern pane can afford a bigger track title; the brutalist
        # one wants the whole mock on one type size, like a terminal.
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
            # The mock's container is a bare QWidget, which every theme's
            # base QSS paints with the APP's bg. Transparent, or the
            # ambient theme punches a flat hole in the preview.
            f"QWidget#paneMock {{ background: transparent; }}"
        )

    def _label(self, text: str, *, kind: str = "",
               wrap: bool = False) -> QLabel:
        lab = QLabel(text)
        if kind:
            lab.setObjectName(kind)
        lab.setWordWrap(wrap)
        # Labels must stay click-transparent so the whole pane is one
        # target — QLabel ignores mouse events by default, keep it there.
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
            # Nothing is selected at build time; set_selected re-labels.
            label=self._choose_label,
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

        # The pane's own theme + the shipped dash, never the live ones:
        # theming's case override and the glyph pack's heading_dash are
        # both per-personality user state, so the default builder would
        # render this rule in whichever personality the user happens to
        # be wearing — the other pane's customizations, in the one screen
        # whose entire job is showing each product as it ships.
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

        # The signature control, for real — magnetic detents and all.
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
        """The entire per-frame path: advance a float, ask for a repaint.
        No tokens, no QSS, no layout — theme_changed is a bus, not a
        frame clock."""
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
        """0 for brutalist (its theme declares radius 0 — sharp is the
        point), a generous multiple of the theme's own radius for modern
        so the card reads soft at card scale."""
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

        # Whether this pane has a backdrop at all is the PERSONALITY's
        # call, not the theme's: brutalist declares adaptive_background
        # False and never gets a gradient, however round its theme is.
        # When motion is off (reduced-motion, or a host that stopped us)
        # the same gradient still paints — it just doesn't drift.
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
            # Brutalist: no antialiasing, no gradient, no rounding. The
            # flatness is the feature.
            p.fillRect(self.rect(), bg)
            p.setPen(QPen(border_col, width))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)

    def _paint_backdrop(self, p: QPainter, rect: QRectF, bg: QColor,
                        fg: QColor, accent: QColor) -> None:
        """Two slowly orbiting accent blobs over the theme's own bg.
        Every color is derived from the pane's tokens — nothing is
        invented, nothing is pushed anywhere."""
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
        # Accept, or Qt routes the whole press→release sequence to the
        # parent and the pane stops being one big target.
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
    """"choose your tide" — the full-screen two-pane moment.

    Resolves to a preset id or to nothing. It NEVER writes settings: the
    caller reads ``choice()`` (or connects ``chosen``) and applies. Esc /
    close-X / the window manager killing it all resolve to no choice, and
    the caller decides what a dismissal means.

    The commit is deferred one event-loop turn (``QTimer`` child, not a
    bare singleShot) so nothing tears the dialog down from inside a click
    handler — the modal-from-click crash class this codebase has been
    bitten by. Consumers must therefore let the loop turn once (or use
    ``exec()``, which does) before reading the outcome.
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

        # Parented, single-shot: it cannot outlive the dialog.
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
        # The dialog's own chrome wears the LIVE theme on purpose — it is
        # the frame around the two products, not one of them (family is
        # inherited from the app sheet for the same reason; only the
        # sizes need saying). Set once: the chooser lives for seconds and
        # nothing restyles under it.
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
        # Cap the block and centre it. Uncapped, a 1440p screen turns the
        # panes into two 580×1200 slivers with a void in each; a FIXED cap
        # leaves a 1500×900 island in 2560×1440 of black. So the cap
        # scales: ~72% of the screen, never below the fixed floor that
        # keeps small screens uncapped.
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

        # Content wins every pixel it's allowed (its maximums are the real
        # limits); the stretches exist only to centre what's left over.
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
        """The chosen preset id, or "" while undecided / after dismissal.
        Set synchronously at click time, before the deferred close."""
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
        # Off the click handler's stack before anything closes this
        # dialog or the caller opens the next window.
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
        # Keep the keys on the dialog: every child is NoFocus so left /
        # right / enter always land here.
        self.setFocus(Qt.OtherFocusReason)

    def hideEvent(self, ev) -> None:
        self._stop_animations()
        super().hideEvent(ev)

    def closeEvent(self, ev) -> None:
        self._stop_animations()
        super().closeEvent(ev)

    # ------------------------------------------------------------ paint

    def paintEvent(self, _ev) -> None:
        # Read at paint time, never cached: the shell should sit on the
        # user's current backdrop color, whatever it happens to be.
        theme = theming.manager().current_effective()
        bg = _col(theme, "bg", "#0b0b0b")
        p = QPainter(self)
        p.fillRect(self.rect(), bg)


def _available_geometry():
    """The primary screen's usable rect, or ``None`` when there's no
    screen to ask (headless runs, odd offscreen setups). Factored out so
    the no-screen path is testable."""
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
