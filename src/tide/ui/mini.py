"""The mini player — a dedicated frameless now-playing window.

Opened by clicking the album art in the main window's now-playing strip
(or Ctrl+M); the main window hides while this is up. Everything here rides
the app's existing machinery: the adaptive gradient is a `CentralBg`, the
palette keeps flowing because `AdaptiveDriver.set_mini_active` counts us as
a consumer, the bass envelope arrives through `AmbientController`
multi-target, lyrics come from a private `LyricTracker` (one line, the
ticker) plus an embedded `LyricsView` (the full panel), and every control
is a theme-styled `BracketButton`.

Distinctives, deliberately not found in other mini players:
  * the backdrop *breathes* with the actual bass envelope,
  * the window border doubles as the progress bar (`_RingOverlay`) and is
    seekable,
  * a live synced-lyric ticker line under the artist,
  * the art tile can flip into a live visualizer canvas (the main
    visualizer's renderers over the breathing backdrop),
  * "zen" — controls fade away when the mouse has left it alone.

Wayland rules (KDE): never self-position — moving is only ever
`startSystemMove()`; no always-on-top; fixed layout-driven size.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, Qt, QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QAction, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QMenu,
    QStackedWidget, QVBoxLayout, QWidget,
)

# Qt's QWIDGETSIZE_MAX (the "no maximum" sentinel setMaximumHeight expects).
# PySide6 doesn't export it (PYSIDE-1135), so mirror the C++ value.
QWIDGETSIZE_MAX = (1 << 24) - 1

from .. import audio_capture, settings as settings_module, theming
from ..lyric_tracker import LyricTracker
from ..player import PlayState
from . import art_cache, motion as motion_module, scale as _scale
from .central_bg import CentralBg
from .variants import ThinProgress
from .visualizer import _Canvas
from .widgets import AlbumArt, BracketButton, _color


_BACKDROP_CHOICES = [
    ("follow main", "follow"),
    ("living fields", "field"),
    ("diagonal band", "band"),
    ("bass arch", "vbeam"),
    ("off · flat", "off"),
]
_PROGRESS_CHOICES = [
    ("border ring", "ring"),
    ("thin bar", "thin"),
]

_CORNER_RADIUS = 12          # the mini is always "rounded" — that's the look
_ZEN_IDLE_MS = 2600
_LYRICS_PANEL_H = 210
_TICKER_MAX_LINES = 3        # long synced lines wrap this far, then elide
_PULSE_PAD_MAX = 8           # px each side the window swells on full bass
_SCREEN_FIT_SLACK = 24       # breathing room when checking screen fit

# Scoped transparency so the gradient shows through, mirroring what
# theming._CONTENT_BACKDROP_QSS does for #appSurface — themes paint
# `QWidget { background: @bg }` globally and would otherwise fill every
# container with an opaque slab.
_MINI_QSS = """
QWidget#miniSurface,
QWidget#miniSurface .QWidget,
QWidget#miniSurface QStackedWidget,
QWidget#miniSurface LyricsView,
QWidget#miniSurface QScrollArea,
QWidget#miniSurface QScrollArea > QWidget,
QWidget#miniSurface QScrollArea > QWidget > QWidget,
QWidget#miniSurface #lyricsKaraoke {
    background: transparent;
}
"""


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}"


class _RingOverlay(QWidget):
    """Progress drawn as the window's border: a rounded-rect arc that starts
    at top-center and fills clockwise. Display-only — seeking is handled by
    MiniPlayer's border-band hit test so this can stay mouse-transparent."""

    PEN_W = 3.0

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._frac = 0.0
        self._active = False
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def set_progress(self, position: float, duration: float) -> None:
        frac = (position / duration) if duration > 0 else 0.0
        frac = max(0.0, min(1.0, frac))
        if abs(frac - self._frac) < 0.0005 and self._active == (duration > 0):
            return
        self._frac = frac
        self._active = duration > 0
        self.update()

    def border_path(self) -> QPainterPath:
        """Clockwise rounded-rect path starting at top-center — shared by the
        painter and the seek hit-test so they can never disagree."""
        inset = self.PEN_W / 2.0 + 1.0
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        r = max(1.0, float(_CORNER_RADIUS) - inset)
        left, top = rect.left(), rect.top()
        right, bottom = rect.right(), rect.bottom()
        cx = rect.center().x()
        path = QPainterPath(QPointF(cx, top))
        path.lineTo(right - r, top)
        path.arcTo(right - 2 * r, top, 2 * r, 2 * r, 90, -90)
        path.lineTo(right, bottom - r)
        path.arcTo(right - 2 * r, bottom - 2 * r, 2 * r, 2 * r, 0, -90)
        path.lineTo(left + r, bottom)
        path.arcTo(left, bottom - 2 * r, 2 * r, 2 * r, 270, -90)
        path.lineTo(left, top + r)
        path.arcTo(left, top, 2 * r, 2 * r, 180, -90)
        path.lineTo(cx, top)
        return path

    def frac_at(self, pos) -> float:
        """Fraction of the track for a point near the border (nearest-point
        sampling along the path)."""
        path = self.border_path()
        best_frac, best_d2 = 0.0, float("inf")
        samples = 220
        px, py = float(pos.x()), float(pos.y())
        for i in range(samples + 1):
            t = i / samples
            pt = path.pointAtPercent(t)
            d2 = (pt.x() - px) ** 2 + (pt.y() - py) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_frac = t
        return best_frac

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = self.border_path()
        dim = _color(self._theme, "dim", "#666")
        dim.setAlpha(70)
        # The full track, faint.
        p.setPen(QPen(dim, 1.0))
        p.drawPath(path)
        if not self._active or self._frac <= 0.0:
            return
        accent = _color(self._theme, "accent", "#d4b95e")
        pen = QPen(accent, self.PEN_W)
        pen.setCapStyle(Qt.FlatCap)
        if self._frac < 1.0:
            # SVG stroke-dashoffset trick: one dash = the filled arc, one gap
            # = the rest. Pattern units are pen-width multiples; the path
            # starts at top-center so offset stays 0.
            total = path.length() / self.PEN_W
            fill = max(0.001, total * self._frac)
            pen.setDashPattern([fill, max(0.001, total - fill)])
        p.setPen(pen)
        p.drawPath(path)


class MiniPlayer(QWidget):
    """See module docstring. Constructed once, lazily, by MainWindow."""

    def __init__(self, window) -> None:
        super().__init__(None)
        self._window = window
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("tide · mini")
        self.setStyleSheet(_MINI_QSS)
        self.setMouseTracking(True)

        self._raw_title = ""
        self._raw_artist = ""
        self._raw_ticker = ""
        self._art_url: str | None = None
        self._lyrics_open = False
        self._lyrics_squeezed = False       # lyrics needed the controls' room
        self._last_lyrics_pos = -10.0
        self._zen_asleep = False
        self._menu: QMenu | None = None
        self.lyrics_panel = None            # lazy LyricsView
        # Bass-resize (optional): the content margins breathe with the
        # envelope, so the window — and the progress ring with it —
        # physically swells on bass.
        self._pulse_resize_on = False
        self._pulse_pad = 0
        self._gutter = 0
        self._pulse_shown = 0.0
        self._pulse_target = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        # ---------- content ----------
        content = QWidget()
        content.setObjectName("miniSurface")
        content.setMouseTracking(True)
        col = QVBoxLayout(content)
        self._base_margins = _scale.margins(16, 16, 16, 12)
        col.setContentsMargins(*self._base_margins)
        col.setSpacing(_scale.px(8))

        self.art = AlbumArt(260)
        self.art.set_framed(False)      # big art over the gradient — no box
        self.art.setCursor(Qt.PointingHandCursor)
        self.art.setToolTip("back to full")
        self.art.clicked.connect(self._request_exit)
        # Art ⇄ visualizer flip: page 1 hosts a visualizer canvas that can
        # stand in for the art tile. _Canvas paints transparently and picks
        # its renderer from the theme, so the breathing backdrop stays
        # visible behind the marks.
        self.vis_canvas = _Canvas()
        self.art_stack = QStackedWidget()
        self.art_stack.addWidget(self.art)
        self.art_stack.addWidget(self.vis_canvas)
        self.art_stack.setFixedSize(self.art.size())
        art_row = QHBoxLayout()
        art_row.addStretch(1)
        art_row.addWidget(self.art_stack)
        art_row.addStretch(1)
        col.addLayout(art_row)

        # Everything below the art fades in zen; keep it in one plain-QWidget
        # group (caught by the .QWidget transparency rule) with a permanent
        # opacity effect so hiding never collapses the layout.
        self._fade_group = QWidget()
        self._fade_group.setMouseTracking(True)
        fg = QVBoxLayout(self._fade_group)
        fg.setContentsMargins(0, 0, 0, 0)
        fg.setSpacing(_scale.px(6))

        self.title_lbl = QLabel("nothing playing")
        self.title_lbl.setTextFormat(Qt.PlainText)
        self.title_lbl.setAlignment(Qt.AlignHCenter)
        self.artist_lbl = QLabel("")
        self.artist_lbl.setTextFormat(Qt.PlainText)
        self.artist_lbl.setAlignment(Qt.AlignHCenter)
        self.artist_lbl.setProperty("class", "dim")
        self.ticker_lbl = QLabel("")
        self.ticker_lbl.setTextFormat(Qt.PlainText)
        # Long lyric lines wrap instead of eliding; the window grows DOWN to
        # make room (width can't budge: Wayland anchors a widening window at
        # its left edge). Height is pinned per LINE — never per scramble
        # frame — so this can't reintroduce the window-spazz.
        self.ticker_lbl.setWordWrap(True)
        self.ticker_lbl.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.ticker_lbl.setProperty("class", "dim")
        self._ticker_h_anim: QVariantAnimation | None = None
        fg.addWidget(self.title_lbl)
        fg.addWidget(self.artist_lbl)
        fg.addWidget(self.ticker_lbl)
        # Pin the text labels to the art's width. Their text is elided to
        # that width anyway, but without the pin every scramble frame
        # re-measures (non-mono fonts give every random-glyph frame a
        # different pixel width) and the SetFixedSize layout re-negotiates
        # the WINDOW per frame — visible as the window spazzing on each new
        # ticker line.
        self._pin_label_widths()

        self.progress = ThinProgress()
        self.progress.seek_requested.connect(self._on_seek_requested)
        fg.addWidget(self.progress)

        transport = QHBoxLayout()
        transport.setSpacing(_scale.px(6))
        self.prev_btn = BracketButton("prev", "◂◂")
        self.play_btn = BracketButton("play", "▶")
        self.next_btn = BracketButton("next", "▸▸")
        self.like_btn = BracketButton("♡", "♡")
        for btn in (self.prev_btn, self.play_btn, self.next_btn, self.like_btn):
            btn.setFocusPolicy(Qt.NoFocus)
        transport.addStretch(1)
        transport.addWidget(self.prev_btn)
        transport.addWidget(self.play_btn)
        transport.addWidget(self.next_btn)
        transport.addWidget(self.like_btn)
        transport.addStretch(1)
        fg.addLayout(transport)

        bottom = QHBoxLayout()
        self.lyrics_btn = BracketButton("lyrics")
        self.lyrics_btn.setFocusPolicy(Qt.NoFocus)
        self.time_lbl = QLabel("0:00")
        self.time_lbl.setTextFormat(Qt.PlainText)
        self.time_lbl.setProperty("class", "dim")
        # Glyph-only ([📌] like the ♡ button): a text label here out-measures
        # the art and widens the whole window in bracket-style themes.
        self.pin_btn = BracketButton("📌", "📌")
        self.pin_btn.setFocusPolicy(Qt.NoFocus)
        self.pin_btn.setToolTip("keep on top of other windows")
        self.exit_btn = BracketButton("expand", "⤢")
        self.exit_btn.setFocusPolicy(Qt.NoFocus)
        bottom.addWidget(self.lyrics_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.time_lbl)
        bottom.addStretch(1)
        bottom.addWidget(self.pin_btn)
        bottom.addWidget(self.exit_btn)
        fg.addLayout(bottom)

        col.addWidget(self._fade_group)
        self._content_col = col

        self._zen_eff = QGraphicsOpacityEffect(self._fade_group)
        self._zen_eff.setOpacity(1.0)
        self._fade_group.setGraphicsEffect(self._zen_eff)
        self._zen_anim: QPropertyAnimation | None = None
        self._zen_h_anim: QVariantAnimation | None = None
        self._zen_timer = QTimer(self)
        self._zen_timer.setSingleShot(True)
        self._zen_timer.setInterval(_ZEN_IDLE_MS)
        self._zen_timer.timeout.connect(self._on_zen_timeout)

        self.central_bg = CentralBg(content)
        self.central_bg.set_radius(_CORNER_RADIUS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.setSizeConstraint(QLayout.SetFixedSize)
        outer.addWidget(self.central_bg)

        self.ring = _RingOverlay(self)
        self.ring.raise_()

        # ---------- wiring ----------
        self.prev_btn.clicked.connect(window._on_prev_clicked)
        self.play_btn.clicked.connect(window._on_play_clicked)
        self.next_btn.clicked.connect(window._on_next_clicked)
        self.like_btn.clicked.connect(window._on_like_clicked)
        # Not connected straight to _toggle_lyrics: clicked(checked) would
        # land its bool in the ``animate`` parameter.
        self.lyrics_btn.clicked.connect(self._on_lyrics_btn)
        self.pin_btn.clicked.connect(self._on_pin_btn)
        self.exit_btn.clicked.connect(self._request_exit)

        window.queue.current_changed.connect(self._on_track_changed)
        window.player.state_changed.connect(self._on_state)
        window.player.position_changed.connect(self._on_position)
        window.player.duration_changed.connect(self._on_duration)
        theming.manager().theme_changed.connect(self._on_theme)

        # Visualizer-flip capture: our own consumer name on the shared feed,
        # held only while the vis page is on screen and the player is
        # actually playing (mirrors AmbientController's acquire/release).
        self._feed = audio_capture.feed()
        self._vis_capturing = False
        self._feed.bands_updated.connect(self._on_vis_bands)
        self._feed.waveform_updated.connect(self._on_vis_waveform)

        # Private synced-lyric feed for the ticker. NOT window._lyric_tracker
        # — that one's enable state belongs to the Discord settings.
        self._tracker = LyricTracker(window.api, window.player, window.queue,
                                     parent=self)
        self._tracker.start_wire()
        self._tracker.lyric_changed.connect(self._on_ticker_line)

        QShortcut(QKeySequence(Qt.Key_Escape), self, self._request_exit)
        QShortcut(QKeySequence("Ctrl+M"), self, self._request_exit)
        QShortcut(QKeySequence("Space"), self, window._on_play_clicked)
        QShortcut(QKeySequence("Ctrl+Right"), self, window._on_next_clicked)
        QShortcut(QKeySequence("Ctrl+Left"), self, window._on_prev_clicked)
        QShortcut(QKeySequence("Ctrl+H"), self, window._on_like_clicked)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self._install_wake_filters()

    # ---------- settings plumbing ----------

    def _settings(self):
        s = getattr(self._window, "_settings", None)
        if s is None:
            # Bare test construction — defaults, not a disk read per call.
            s = getattr(self, "_fallback_settings", None)
            if s is None:
                s = settings_module.Settings()
                self._fallback_settings = s
        return s

    def _set_setting(self, field: str, value) -> None:
        s = getattr(self._window, "_settings", None)
        if s is not None:
            setattr(s, field, value)
            try:
                settings_module.save(s)
            except Exception:
                pass
        else:
            setattr(self._settings(), field, value)
        self.apply_settings()

    def resolved_backdrop_style(self) -> str:
        s = self._settings()
        style = s.mini_backdrop_style or "follow"
        if style == "follow":
            style = s.adaptive_background_style or "field"
        return style

    def apply_settings(self) -> None:
        """Push the current mini_* settings into the widgets. Called on every
        show, after the settings dialog saves, and from the context menu."""
        s = self._settings()
        style = self.resolved_backdrop_style()
        if style == "off":
            self.central_bg.set_enabled(False)
        else:
            self.central_bg.set_style(style)
            self.central_bg.set_enabled(True)
        self.central_bg.set_motion(s.motion or "lite")

        ring_mode = (s.mini_progress_style or "ring") != "thin"
        self.ring.setVisible(ring_mode)
        self.progress.setVisible(not ring_mode)

        self.ticker_lbl.setVisible(bool(s.mini_ticker))
        self._sync_tracker_enabled()

        self.art_stack.setCurrentWidget(
            self.vis_canvas if s.mini_show_visualizer else self.art
        )
        self._reconcile_vis()

        if not s.mini_zen:
            self._zen_wake()
            self._zen_timer.stop()
        elif self.isVisible():
            self._zen_timer.start()

        self._apply_pin()

        # Bass resize: opt-in, and meaningful with or without the gradient
        # (a flat card breathing is still a card breathing).
        self._pulse_resize_on = bool(s.mini_pulse) and bool(s.mini_pulse_resize)
        if not self._pulse_resize_on:
            self._reset_pulse_pad()
        else:
            # Reserve the gutter at rest so the swell has room on all sides.
            self._sync_gutter()

        # The pulse consumer is only held while we're actually on screen.
        ambient = getattr(self._window, "_ambient", None)
        if ambient is not None:
            ambient.set_mini_active(
                bool(s.mini_pulse)
                and (style != "off" or self._pulse_resize_on)
                and self.isVisible()
            )

    def _sync_tracker_enabled(self) -> None:
        s = self._settings()
        self._tracker.set_enabled(
            self.isVisible() and bool(s.mini_ticker) and not self._lyrics_open
        )

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.apply_settings()
        if self._settings().mini_lyrics_open and not self._lyrics_open:
            self._toggle_lyrics(animate=False)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._tracker.set_enabled(False)
        self._zen_timer.stop()
        self._zen_wake(snap=True)
        self._reset_pulse_pad()
        self._reconcile_vis()
        ambient = getattr(self._window, "_ambient", None)
        if ambient is not None:
            ambient.set_mini_active(False)

    def closeEvent(self, event) -> None:
        # A compositor close ("close window" from the taskbar) means "give me
        # tide back", not "quit" — never leave the app running headless.
        if not getattr(self._window, "_wants_quit", False) and self._window.isHidden():
            event.ignore()
            self._request_exit()
            return
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_ring()

    # ---------- data slots (queue / player / theme) ----------

    def sync_now(self, track, duration, position, state, liked) -> None:
        """Full refresh, called right before every show so a mini opened
        mid-song is correct on frame one."""
        self._apply_track(track, animate=False)
        self.progress.setDuration(duration or 0.0)
        self.progress.setPosition(position or 0.0)
        self.ring.set_progress(position or 0.0, duration or 0.0)
        self._update_time(position or 0.0, duration or 0.0)
        self._on_state(state)
        self.set_liked(liked)
        try:
            self.set_nav_enabled(self._window.prev_btn.isEnabled(),
                                 self._window.next_btn.isEnabled())
            self.set_like_enabled(self._window.like_btn.isEnabled())
        except RuntimeError:
            pass
        if self._lyrics_open and self.lyrics_panel is not None:
            self.lyrics_panel.show_for(track)
            self.lyrics_panel.update_position(position or 0.0)

    def _on_track_changed(self, track) -> None:
        if not self.isVisible():
            return
        self._apply_track(track, animate=True)
        if self._lyrics_open and self.lyrics_panel is not None:
            self.lyrics_panel.show_for(track)

    def _apply_track(self, track, animate: bool = True) -> None:
        if track is None:
            self._raw_title = ""
            self._raw_artist = ""
            self._art_url = None
            self.title_lbl.setText(theming.styled_case("nothing playing"))
            self.artist_lbl.setText("")
            self.art.setImage(None)
            self.progress.reset()
            self.ring.set_progress(0.0, 0.0)
            self._update_time(0.0, 0.0)
            return
        self._raw_title = track.title or ""
        self._raw_artist = track.artists or ""
        self._set_label(self.title_lbl, self._raw_title, "scramble/title",
                        animate)
        self._set_label(self.artist_lbl, self._raw_artist, "scramble/artist",
                        animate)
        url = track.thumbnail or ""
        self._art_url = url or None
        if not url:
            self.art.setImage(None)
        else:
            img = art_cache.cache().request(
                url, lambda image, url=url: self._on_art_ready(url, image)
            )
            if img is not None:
                self._on_art_ready(url, img)

    def _on_art_ready(self, url: str, image) -> None:
        if url != self._art_url:
            return
        self.art.setImage(image)

    def _set_label(self, label: QLabel, text: str, kind: str,
                   animate: bool) -> None:
        shown = self._elide(label, theming.styled_case(text))
        if animate:
            motion_module.scramble_text(label.setText, shown, owner=self,
                                        kind=kind)
        else:
            label.setText(shown)

    def _elide(self, label: QLabel, text: str) -> str:
        fm = QFontMetrics(label.font())
        return fm.elidedText(text, Qt.ElideRight, self.art.width())

    def _pin_label_widths(self) -> None:
        w = self.art.width()
        for lbl in (self.title_lbl, self.artist_lbl, self.ticker_lbl):
            lbl.setFixedWidth(w)
        if self.ticker_lbl.maximumHeight() == QWIDGETSIZE_MAX:
            # First run: reserve exactly one line.
            self.ticker_lbl.setFixedHeight(self._ticker_line_height())

    # ---------- ticker sizing ----------

    def _ticker_line_height(self) -> int:
        return QFontMetrics(self.ticker_lbl.font()).lineSpacing() + 2

    def _ticker_fit(self, text: str) -> tuple[str, int]:
        """Wrap-fit ``text`` to the ticker's pinned width. Returns the
        display text (word-elided once it would pass _TICKER_MAX_LINES)
        and the pixel height it needs."""
        fm = QFontMetrics(self.ticker_lbl.font())
        width = max(1, self.art.width())
        flags = int(Qt.TextWordWrap | Qt.AlignHCenter)
        one = self._ticker_line_height()
        cap = one * _TICKER_MAX_LINES
        base = text
        shown = text
        while True:
            rect = fm.boundingRect(0, 0, width, 10_000, flags, shown)
            if rect.height() <= cap or " " not in base:
                break
            base = base[: base.rfind(" ")].rstrip()
            shown = base + " …"
        return shown, max(one, min(rect.height() + 2, cap))

    def _set_ticker_height(self, h: int, animate: bool) -> None:
        if self._ticker_h_anim is not None:
            self._ticker_h_anim.stop()
            self._ticker_h_anim = None
        if self.ticker_lbl.height() == h and self.ticker_lbl.maximumHeight() == h:
            return
        if (not animate
                or motion_module.intensity() == motion_module.Intensity.OFF):
            self.ticker_lbl.setFixedHeight(h)
            return
        anim = QVariantAnimation(self)
        anim.setDuration(motion_module.DUR_SHORT)
        anim.setStartValue(int(self.ticker_lbl.height()))
        anim.setEndValue(int(h))
        anim.valueChanged.connect(self._on_ticker_h_value)
        self._ticker_h_anim = anim
        anim.start()

    def _on_ticker_h_value(self, value) -> None:
        self.ticker_lbl.setFixedHeight(int(value))

    def _apply_ticker_text(self, raw: str, animate: bool) -> None:
        if raw:
            shown, h = self._ticker_fit(theming.styled_case(raw))
        else:
            shown, h = "", self._ticker_line_height()
        self._set_ticker_height(h, animate)
        if animate:
            motion_module.scramble_text(self.ticker_lbl.setText, shown,
                                        owner=self, kind="scramble/ticker")
        else:
            self.ticker_lbl.setText(shown)

    def _on_state(self, state) -> None:
        if state == PlayState.PLAYING:
            self.play_btn.setLabel("pause")
            self.play_btn.setGlyph("⏸")
        elif state == PlayState.LOADING:
            self.play_btn.setLabel("…")
            self.play_btn.setGlyph("…")
        else:
            self.play_btn.setLabel("play")
            self.play_btn.setGlyph("▶")
        self._reconcile_vis()

    def _on_position(self, secs: float) -> None:
        if not self.isVisible():
            return
        duration = self._window.player.duration
        self.progress.setPosition(secs)
        self.ring.set_progress(secs, duration)
        self._update_time(secs, duration)
        if self._lyrics_open and self.lyrics_panel is not None:
            if abs(secs - self._last_lyrics_pos) >= 0.25:
                self._last_lyrics_pos = secs
                try:
                    self.lyrics_panel.update_position(secs)
                except Exception:
                    pass

    def _on_duration(self, secs: float) -> None:
        if not self.isVisible():
            return
        self.progress.setDuration(secs)
        self.ring.set_progress(self._window._last_position, secs)

    def _update_time(self, pos: float, dur: float) -> None:
        # Elapsed only: the ring/thin bar already shows the proportion, and
        # the full "m:ss / m:ss" out-measures the art — it would widen the
        # whole window (and differently per theme font). Full time on hover.
        self.time_lbl.setText(_mmss(pos))
        self.time_lbl.setToolTip(f"{_mmss(pos)} / {_mmss(dur)}")

    def _on_theme(self, _theme) -> None:
        # Art rescales on ui_scale changes (its own theme handler runs
        # first, so its width is current here) — keep the pins in step.
        self._pin_label_widths()
        self.art_stack.setFixedSize(self.art.size())
        # Text case is a per-theme property — re-render what we're showing.
        if self._raw_title:
            self._set_label(self.title_lbl, self._raw_title,
                            "scramble/title", False)
        if self._raw_artist:
            self._set_label(self.artist_lbl, self._raw_artist,
                            "scramble/artist", False)
        # Font/scale may have changed — re-fit the ticker (height pin too).
        self._apply_ticker_text(self._raw_ticker, animate=False)

    def _on_ticker_line(self, value) -> None:
        text = value if isinstance(value, str) else ""
        self._raw_ticker = text
        if not self.isVisible():
            self._apply_ticker_text("", animate=False)
            return
        # No scramble/height anim while zen has the line at opacity 0 —
        # pointless churn on an invisible label.
        self._apply_ticker_text(text, animate=bool(text)
                                and not self._zen_asleep)

    # ---------- art ⇄ visualizer flip ----------

    def _is_playing(self) -> bool:
        try:
            return self._window.player.state == PlayState.PLAYING
        except Exception:
            return False

    def _reconcile_vis(self) -> None:
        """Acquire/release the shared capture (consumer ``"mini"``). Held
        only while the vis page is on screen with music actually playing, so
        an idle, hidden, or art-mode mini never keeps a parec process alive
        on its own."""
        want = (
            bool(self._settings().mini_show_visualizer)
            and self.isVisible()
            and self._is_playing()
        )
        if want and not self._vis_capturing:
            # Honor the saved monitor-source override (shared with the
            # visualizer's audio-source picker), same as the ambient pulse.
            try:
                source = settings_module.load().audio_device or None
            except Exception:
                source = None
            self._vis_capturing = bool(
                self._feed.add_consumer("mini", source=source)
            )
        elif not want and self._vis_capturing:
            self._feed.remove_consumer("mini")
            self._vis_capturing = False

    def _on_vis_bands(self, bands) -> None:
        if self._vis_capturing and self.vis_canvas.isVisible():
            self.vis_canvas.update_bands(bands)

    def _on_vis_waveform(self, wave) -> None:
        if self._vis_capturing and self.vis_canvas.isVisible():
            self.vis_canvas.update_waveform(wave)

    # ---------- like / nav state pushed by MainWindow ----------

    def set_liked(self, liked: bool) -> None:
        glyph = "♥" if liked else "♡"
        self.like_btn.setLabel(glyph)
        self.like_btn.setGlyph(glyph)

    def set_like_enabled(self, enabled: bool) -> None:
        self.like_btn.setEnabled(bool(enabled))

    def set_nav_enabled(self, prev_ok: bool, next_ok: bool) -> None:
        self.prev_btn.setEnabled(bool(prev_ok))
        self.next_btn.setEnabled(bool(next_ok))

    # ---------- lyrics panel ----------

    def _on_lyrics_btn(self) -> None:
        self._toggle_lyrics(True)

    def _toggle_lyrics(self, animate: bool = True) -> None:
        want = not self._lyrics_open
        self._lyrics_open = want
        if self.lyrics_panel is None and want:
            from .lyrics import LyricsView
            panel = LyricsView(self._window.api)
            for chrome in (panel.heading, panel.karaoke_check, panel.mute_btn,
                           panel.swap_status):
                chrome.hide()
            panel.setFixedHeight(0)
            self._content_col.addWidget(panel)
            self.lyrics_panel = panel
            self._install_wake_filters()
        panel = self.lyrics_panel
        if panel is None:
            return
        target = _scale.px(_LYRICS_PANEL_H) if want else 0
        squeeze = False
        if want:
            panel.show()
            panel.show_for(getattr(self._window, "_current", None))
            panel.update_position(getattr(self._window, "_last_position", 0.0))
            # If the expanded window can't fit the screen, fold the controls
            # away immediately (the same collapse zen uses) so the lyrics get
            # the room. Hovering still brings the controls back; idle
            # re-collapses while the squeeze is needed, zen preference or not.
            scr = self.screen()
            if scr is not None:
                projected = self.height() + target + _scale.px(8)
                squeeze = projected > (scr.availableGeometry().height()
                                       - _SCREEN_FIT_SLACK)
        self._lyrics_squeezed = want and squeeze
        start = panel.height()
        if not animate or motion_module.intensity() == motion_module.Intensity.OFF:
            panel.setFixedHeight(target)
            if not want:
                panel.hide()
        else:
            anim = QVariantAnimation(self)
            anim.setDuration(motion_module.DUR_SHORT)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.valueChanged.connect(self._on_lyrics_anim_value)
            if not want:
                anim.finished.connect(self._on_lyrics_anim_closed)
            self._lyrics_anim = anim
            anim.start()
        if want and squeeze:
            self._zen_sleep(force=True)
        else:
            self._zen_wake()
        self._sync_tracker_enabled()
        if bool(self._settings().mini_lyrics_open) != want:
            self._set_setting("mini_lyrics_open", want)

    def _on_lyrics_anim_value(self, value) -> None:
        if self.lyrics_panel is not None:
            self.lyrics_panel.setFixedHeight(int(value))

    def _on_lyrics_anim_closed(self) -> None:
        if self.lyrics_panel is not None and not self._lyrics_open:
            self.lyrics_panel.hide()

    # ---------- zen ----------

    def _install_wake_filters(self) -> None:
        for w in [self] + self.findChildren(QWidget):
            # Remove first so re-runs (after the lyrics panel is built) never
            # double-install and fire the filter twice per event.
            w.removeEventFilter(self)
            w.installEventFilter(self)
            w.setMouseTracking(True)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.MouseMove, QEvent.Enter,
                            QEvent.MouseButtonPress, QEvent.Wheel,
                            QEvent.HoverMove):
            self._zen_wake()
            if self.isVisible() and (self._settings().mini_zen
                                     or self._lyrics_squeezed):
                self._zen_timer.start()
        return super().eventFilter(obj, event)

    def _animate_zen(self, to: float) -> None:
        if self._zen_anim is not None:
            self._zen_anim.stop()
            self._zen_anim = None
        if motion_module.intensity() == motion_module.Intensity.OFF:
            self._zen_eff.setOpacity(to)
            return
        anim = QPropertyAnimation(self._zen_eff, b"opacity", self)
        anim.setDuration(motion_module.DUR_MED)
        anim.setStartValue(self._zen_eff.opacity())
        anim.setEndValue(to)
        self._zen_anim = anim
        anim.start()

    # -- zen height: the window shrinks until only the art card is left.
    # The fade group's height animates to 0 and the SetFixedSize layout
    # pulls the window up with it each frame; the art is at the top so it
    # never moves on screen (Wayland keeps the top-left corner anchored).

    def _stop_zen_h_anim(self) -> None:
        if self._zen_h_anim is not None:
            self._zen_h_anim.stop()
            self._zen_h_anim = None

    def _group_constrained(self) -> bool:
        return self._fade_group.maximumHeight() != QWIDGETSIZE_MAX

    def _clear_group_height(self) -> None:
        # Back to natural layout sizing — a stale fixed height would freeze
        # the group across theme/scale changes.
        self._fade_group.setMinimumHeight(0)
        self._fade_group.setMaximumHeight(QWIDGETSIZE_MAX)

    def _animate_group_height(self, end: int, on_done=None) -> None:
        self._stop_zen_h_anim()
        anim = QVariantAnimation(self)
        anim.setDuration(motion_module.DUR_MED)
        # Deliberately linear — the collapse should read as one steady
        # mechanical motion, in step with the opacity fade.
        anim.setEasingCurve(QEasingCurve.Linear)
        anim.setStartValue(int(self._fade_group.height()))
        anim.setEndValue(int(end))
        anim.valueChanged.connect(self._on_zen_h_value)
        if on_done is not None:
            anim.finished.connect(on_done)
        self._zen_h_anim = anim
        anim.start()

    def _on_zen_h_value(self, value) -> None:
        self._fade_group.setFixedHeight(int(value))

    def _on_zen_wake_h_done(self) -> None:
        self._zen_h_anim = None
        self._clear_group_height()

    def _on_zen_timeout(self) -> None:
        # Squeezed lyrics re-collapse even with the zen preference off — the
        # controls simply don't fit the screen while the panel is up.
        self._zen_sleep(force=self._lyrics_squeezed)

    def _zen_sleep(self, force: bool = False) -> None:
        if not force and not self._settings().mini_zen:
            return
        if self._menu is not None and self._menu.isVisible():
            return
        if self._zen_asleep:
            return
        self._zen_asleep = True
        self._animate_zen(0.0)
        if motion_module.intensity() == motion_module.Intensity.OFF:
            self._stop_zen_h_anim()
            self._fade_group.setFixedHeight(0)
        else:
            self._animate_group_height(0)

    def _zen_wake(self, snap: bool = False) -> None:
        dormant = (self._zen_asleep
                   or self._zen_h_anim is not None
                   or self._group_constrained()
                   or self._zen_eff.opacity() < 0.999)
        if not dormant:
            return
        self._zen_asleep = False
        if snap or motion_module.intensity() == motion_module.Intensity.OFF:
            if self._zen_anim is not None:
                self._zen_anim.stop()
                self._zen_anim = None
            self._zen_eff.setOpacity(1.0)
            self._stop_zen_h_anim()
            self._clear_group_height()
        else:
            self._animate_zen(1.0)
            if self._group_constrained() or self._zen_h_anim is not None:
                self._animate_group_height(
                    self._fade_group.sizeHint().height(),
                    on_done=self._on_zen_wake_h_done,
                )

    # ---------- interaction ----------

    def _request_exit(self) -> None:
        # Deferred: reached from mouse handlers / shortcuts; hiding windows
        # inside the emission is the PySide6+py3.14 crash pattern.
        QTimer.singleShot(0, self._window.exit_mini_mode)

    def _on_seek_requested(self, seconds: float) -> None:
        self._window.player.seek(seconds)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            pos = ev.position().toPoint()
            band = int(_RingOverlay.PEN_W) + 9
            # Border band around the CARD (ring geometry), not the window —
            # with bass-resize on, the window carries a transparent gutter.
            ring_rect = self.ring.geometry()
            inner = ring_rect.adjusted(band, band, -band, -band)
            duration = self._window.player.duration
            if (self.ring.isVisible() and duration > 0
                    and not inner.contains(pos)):
                frac = self.ring.frac_at(pos - ring_rect.topLeft())
                self._window.player.seek(frac * duration)
                ev.accept()
                return
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                ev.accept()
                return
        super().mousePressEvent(ev)

    def wheelEvent(self, ev) -> None:
        delta = ev.angleDelta().y()
        if delta == 0:
            return
        step = 5 if delta > 0 else -5
        try:
            vol = self._window.volume.volume()
            self._window.volume.setVolume(max(0, min(100, vol + step)))
        except (RuntimeError, AttributeError):
            return
        # Feedback: a quick glow kick when the backdrop isn't already
        # breathing on its own.
        ambient = getattr(self._window, "_ambient", None)
        holding = ambient is not None and getattr(ambient, "_holding", False)
        if not holding:
            self.set_pulse(0.55)
            QTimer.singleShot(200, self._pulse_settle)
        ev.accept()

    def _pulse_settle(self) -> None:
        try:
            self.set_pulse(0.0)
        except RuntimeError:
            pass

    # ---------- bass pulse fan-out (AmbientController target) ----------

    def set_pulse(self, level: float) -> None:
        """AmbientController target: the envelope drives the backdrop glow
        always, and — when "bass resize" is on — the window geometry too."""
        self.central_bg.set_pulse(level)
        if not self._pulse_resize_on:
            return
        self._pulse_target = max(0.0, min(1.0, float(level)))
        if not self._pulse_timer.isActive():
            self._pulse_timer.start()

    def _pulse_tick(self) -> None:
        self._pulse_shown += (self._pulse_target - self._pulse_shown) * 0.35
        if abs(self._pulse_shown - self._pulse_target) < 0.01:
            self._pulse_shown = self._pulse_target
        self._apply_pulse_pad(self._pulse_shown)
        if self._pulse_shown == 0.0 and self._pulse_target == 0.0:
            self._pulse_timer.stop()

    def _apply_pulse_pad(self, level: float) -> None:
        # Quantized to whole pixels so a swell costs a handful of relayouts,
        # not one per timer tick.
        pad = int(round(max(0.0, min(1.0, level)) * _PULSE_PAD_MAX))
        if pad == self._pulse_pad:
            return
        self._pulse_pad = pad
        self._sync_gutter()

    def _sync_gutter(self) -> None:
        """Center-origin swell. The window pre-reserves the full pad as a
        transparent outer gutter; on bass the card's own margins grow by
        ``pad`` while the gutter shrinks by the same amount, so the total
        size hint — and with it the window geometry — never changes. The
        card visibly grows out of the middle. (Wayland pins the top-left
        corner on client resizes and forbids self-moves, so a window that
        actually grew could only ever expand right/down.)
        """
        pad = self._pulse_pad if self._pulse_resize_on else 0
        g = (_PULSE_PAD_MAX - pad) if self._pulse_resize_on else 0
        self._gutter = g
        l, t, r, b = self._base_margins
        self._content_col.setContentsMargins(l + pad, t + pad, r + pad, b + pad)
        self.layout().setContentsMargins(g, g, g, g)
        self._place_ring()

    def _place_ring(self) -> None:
        # The ring hugs the CARD edge, which is inset by the gutter — not
        # the (possibly larger, transparent-bordered) window edge.
        g = self._gutter
        self.ring.setGeometry(self.rect().adjusted(g, g, -g, -g))
        self.ring.raise_()

    def _reset_pulse_pad(self) -> None:
        self._pulse_timer.stop()
        self._pulse_target = 0.0
        self._pulse_shown = 0.0
        self._pulse_pad = 0
        self._sync_gutter()

    # ---------- pin on top ----------

    def _on_pin_btn(self) -> None:
        self._set_setting("mini_pin", not bool(self._settings().mini_pin))

    def _apply_pin(self) -> None:
        """Keep-above. On Wayland no client-side hint exists (xdg-shell has
        no keep-above), so we ask KWin over D-Bus; on X11 the Qt hint works
        directly. Elsewhere it degrades to nothing."""
        on = bool(self._settings().mini_pin)
        glyph = "📍" if on else "📌"
        self.pin_btn.setLabel(glyph)
        self.pin_btn.setGlyph(glyph)
        self.pin_btn.setToolTip("pinned on top — click to release" if on
                                else "keep on top of other windows")
        from PySide6.QtGui import QGuiApplication
        platform = QGuiApplication.platformName()
        if platform.startswith("wayland"):
            if self.isVisible():
                self._kwin_pin(on)
        elif platform == "xcb":
            if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != on:
                was_visible = self.isVisible()
                self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
                if was_visible:
                    self.show()

    def _kwin_pin(self, on: bool) -> None:
        """Set keepAbove on our window through KWin's scripting D-Bus —
        the only route to keep-above on KDE Wayland. Best-effort: silently
        degrades on other compositors (a toast explains, once, on enable)."""
        import os
        import tempfile
        path = None
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusInterface
            flag = "true" if on else "false"
            # Match our exact caption; the "·" goes in escaped so file
            # encoding can never bite.
            js = (
                "var list = workspace.windowList ? workspace.windowList()"
                " : workspace.clientList();"
                "for (var i = 0; i < list.length; ++i) {"
                " var w = list[i];"
                " if (w.caption && w.caption.indexOf('tide \\u00b7 mini') !== -1)"
                f" {{ w.keepAbove = {flag}; }}"
                "}"
            )
            path = os.path.join(tempfile.gettempdir(),
                                f"tide-mini-pin-{os.getpid()}.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(js)
            bus = QDBusConnection.sessionBus()
            scripting = QDBusInterface("org.kde.KWin", "/Scripting",
                                       "org.kde.kwin.Scripting", bus)
            if not scripting.isValid():
                raise RuntimeError("kwin scripting not on the bus")
            scripting.call("unloadScript", "tide-mini-pin")
            reply = scripting.call("loadScript", path, "tide-mini-pin")
            args = reply.arguments() if reply is not None else []
            script_id = args[0] if args else -1
            if not isinstance(script_id, int) or script_id < 0:
                raise RuntimeError("loadScript refused")
            ran = False
            # Script object path moved across KWin releases; try both.
            for obj_path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
                script = QDBusInterface("org.kde.KWin", obj_path,
                                        "org.kde.kwin.Script", bus)
                if script.isValid():
                    script.call("run")
                    ran = True
                    break
            scripting.call("unloadScript", "tide-mini-pin")
            if not ran:
                raise RuntimeError("no script object")
        except Exception:
            if on:
                from .toast import show_toast
                show_toast(self, "pin needs kwin — use a window rule instead")
        finally:
            if path is not None:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ---------- context menu ----------

    def _on_context_menu(self, pos) -> None:
        s = self._settings()
        menu = QMenu(self)

        backdrop = menu.addMenu(theming.styled_case("backdrop"))
        current_style = s.mini_backdrop_style or "follow"
        for label, slug in _BACKDROP_CHOICES:
            act = QAction(theming.styled_case(label), backdrop)
            act.setCheckable(True)
            act.setChecked(slug == current_style)
            act.triggered.connect(
                lambda _checked=False, slug=slug:
                self._set_setting("mini_backdrop_style", slug)
            )
            backdrop.addAction(act)

        progress = menu.addMenu(theming.styled_case("progress"))
        current_prog = s.mini_progress_style or "ring"
        for label, slug in _PROGRESS_CHOICES:
            act = QAction(theming.styled_case(label), progress)
            act.setCheckable(True)
            act.setChecked(slug == current_prog)
            act.triggered.connect(
                lambda _checked=False, slug=slug:
                self._set_setting("mini_progress_style", slug)
            )
            progress.addAction(act)

        menu.addSeparator()
        for label, field in (("pin on top", "mini_pin"),
                             ("lyric ticker", "mini_ticker"),
                             ("auto-hide controls", "mini_zen"),
                             ("bass pulse", "mini_pulse"),
                             ("bass resize", "mini_pulse_resize"),
                             ("visualizer tile", "mini_show_visualizer")):
            act = QAction(theming.styled_case(label), menu)
            act.setCheckable(True)
            act.setChecked(bool(getattr(s, field)))
            act.triggered.connect(
                lambda checked=False, field=field:
                self._set_setting(field, bool(checked))
            )
            menu.addAction(act)

        menu.addSeparator()
        exit_act = QAction(theming.styled_case("exit mini player"), menu)
        exit_act.triggered.connect(self._request_exit)
        menu.addAction(exit_act)

        self._menu = menu
        # popup() is non-blocking — no modal-from-handler hazard.
        menu.popup(self.mapToGlobal(pos))
