"""Fullscreen mode — the lean-back now-playing surface.

Put on a song, hit F11 (or the [⤢] on the strip), send the audio to the
speakers and go do something else. The screen becomes big album art with
the title and artist under it, and beside it a side pane holding either
the synced lyrics or the queue (the same Queue model the main window
drives), all over the same adaptive backdrop the rest of the app
breathes with. Chrome (transport, seekable progress, the pane tabs) only
exists while the mouse is awake — leave it alone for a beat and it fades
out, cursor included, until only the music is left.

Same skeleton as the mini player (ui/mini.py): a separate top-level the
main window hides behind, riding the app's existing machinery. The
gradient is a `CentralBg`, the palette keeps flowing because
`AdaptiveDriver.set_mini_active` counts us as a consumer, the bass
envelope arrives through `AmbientController` multi-target, and the words
are an embedded `LyricsView` with its type scaled up to read from across
the room.

While the window is up and music is actually playing, tide asks the
session's screensaver to hold off (org.freedesktop.ScreenSaver over
D-Bus) — a lean-back display that blanks two minutes into cleaning the
kitchen isn't one. Best-effort: released on pause, hide and quit, and a
compositor without the interface just means the usual screen timeout.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QEvent, QSize, Qt, QTimer, QVariantAnimation,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QListView, QMenu,
    QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .. import glyphs, theming
from ..player import PlayState
from ..queue import Role as QueueRole
from . import art_cache, motion as motion_module, scale as _scale
from .central_bg import CentralBg
from .lyrics import LyricsView
from .mini import _BACKDROP_CHOICES
from .variants import ThinProgress
from .widgets import AlbumArt, BracketButton

_ZEN_IDLE_MS = 3000
# Read-from-the-couch multiplier for the embedded lyrics panel
# (13pt active line → ~23pt, karaoke center line → ~50pt).
_LYRICS_FONT_SCALE = 1.8

# Scoped transparency so the gradient shows through — same story as the
# mini's _MINI_QSS: themes paint `QWidget { background: @bg }` globally
# and would otherwise fill every container with an opaque slab.
_FS_QSS = """
QWidget#fsSurface,
QWidget#fsSurface .QWidget,
QWidget#fsSurface LyricsView,
QWidget#fsSurface QScrollArea,
QWidget#fsSurface QScrollArea > QWidget,
QWidget#fsSurface QScrollArea > QWidget > QWidget,
QWidget#fsSurface QListView,
QWidget#fsSurface QListView::item,
QWidget#fsSurface #lyricsKaraoke {
    background: transparent;
}
"""


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}"


class _IdleInhibitor:
    """Best-effort screensaver hold via org.freedesktop.ScreenSaver.

    KDE and GNOME both serve the interface on the session bus; anywhere
    it's missing (or QtDBus is), every call degrades to a silent no-op.
    Cookie-based like the spec wants: one Inhibit per hold, UnInhibit
    with the same cookie to release.
    """

    def __init__(self) -> None:
        self._cookie: int | None = None

    @property
    def active(self) -> bool:
        return self._cookie is not None

    def set_active(self, want: bool) -> None:
        want = bool(want)
        if want == self.active:
            return
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusInterface
            iface = QDBusInterface(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver",
                QDBusConnection.sessionBus(),
            )
            if not iface.isValid():
                self._cookie = None
                return
            if want:
                reply = iface.call("Inhibit", "tide", "music playing fullscreen")
                args = reply.arguments() if reply is not None else []
                cookie = args[0] if args else None
                self._cookie = int(cookie) if isinstance(cookie, int) else None
            else:
                cookie, self._cookie = self._cookie, None
                if cookie is not None:
                    iface.call("UnInhibit", cookie)
        except Exception:
            # Losing the inhibit is cosmetic; never let D-Bus trouble
            # reach playback. Drop the cookie so we don't wedge "active".
            self._cookie = None


class _FsQueueDelegate(QStyledItemDelegate):
    """Text-only queue rows over the gradient. The main app's
    TrackRowDelegate paints thumbnails and an opaque row fill, which is
    the wrong look for this surface — here the rows read like the lyric
    lines beside them: past rows dim, the current one accent + bold."""

    FONT_SCALE = 1.25

    def _font(self, option) -> QFont:
        f = QFont(option.font)
        f.setPointSize(max(1, round(f.pointSize() * self.FONT_SCALE)))
        return f

    def sizeHint(self, option, index) -> QSize:
        fm = QFontMetrics(self._font(option))
        return QSize(max(120, option.rect.width()),
                     fm.height() + _scale.px(10))

    def paint(self, painter, option, index) -> None:
        theme = theming.manager().current_effective()
        fg = QColor(theme.token("fg", "#e6e6e6") if theme else "#e6e6e6")
        dim = QColor(theme.token("dim", "#6f6f6f") if theme else "#6f6f6f")
        accent = QColor(
            theme.token("accent", "#d4b95e") if theme else "#d4b95e")
        is_current = bool(index.data(int(QueueRole.IsCurrent)))
        try:
            past = index.row() < int(index.model().current_index)
        except Exception:
            past = False
        text = str(index.data(Qt.DisplayRole) or "")
        # The model bakes a "* " current marker into the display line for
        # the plain main view; the accent + bold already say it here.
        if text[:2] in ("* ", "  "):
            text = text[2:]
        f = self._font(option)
        f.setBold(is_current)
        painter.save()
        painter.setFont(f)
        painter.setPen(accent if is_current else (dim if past else fg))
        rect = option.rect.adjusted(_scale.px(6), 0, -_scale.px(6), 0)
        elided = QFontMetrics(f).elidedText(text, Qt.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()


class FullscreenPlayer(QWidget):
    """See module docstring. Constructed once, lazily, by MainWindow."""

    def __init__(self, window) -> None:
        super().__init__(None)
        self._window = window
        self.setWindowTitle("tide · fullscreen")
        self.setStyleSheet(_FS_QSS)
        self.setMouseTracking(True)

        self._raw_title = ""
        self._raw_artist = ""
        self._art_url: str | None = None
        # Which side pane is up: "lyrics" | "queue" | "off". showEvent
        # applies the remembered setting.
        self._pane = "off"
        # Open/close geometry animation (host width + art size together).
        self._pane_anim: QVariantAnimation | None = None
        # Per-screen metrics, filled by prepare_for_screen.
        self._pane_w = 700
        self._art_base = 320
        self._art_base_solo = 346
        self._last_lyrics_pos = -10.0
        self._zen_asleep = False
        self._menu: QMenu | None = None
        self._inhibitor = _IdleInhibitor()

        # ---------- content ----------
        content = QWidget()
        content.setObjectName("fsSurface")
        content.setMouseTracking(True)
        col = QVBoxLayout(content)
        col.setContentsMargins(*_scale.margins(36, 20, 36, 20))
        col.setSpacing(_scale.px(10))

        # Top chrome (fades in zen): the side-pane tabs + exit, kept to
        # the right where the sketchy little icons live.
        self.lyrics_btn = BracketButton("lyrics", "♫")
        self.lyrics_btn.setToolTip("lyrics pane (l)")
        self.queue_btn = BracketButton("queue", "≡")
        self.queue_btn.setToolTip("queue pane (q)")
        self.exit_btn = BracketButton("exit", "✕")
        self.exit_btn.setToolTip("back to the full window (esc)")
        for btn in (self.lyrics_btn, self.queue_btn, self.exit_btn):
            btn.setFocusPolicy(Qt.NoFocus)
        self._top_bar = QWidget()
        self._top_bar.setMouseTracking(True)
        top = QHBoxLayout(self._top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(_scale.px(8))
        top.addStretch(1)
        top.addWidget(self.lyrics_btn)
        top.addWidget(self.queue_btn)
        top.addSpacing(_scale.px(16))
        top.addWidget(self.exit_btn)
        col.addWidget(self._top_bar)

        # Center: art + title/artist on the left, lyrics on the right.
        # The lyrics HOST stays in the layout even when the pane is
        # hidden so the art block never jumps on toggle — an empty right
        # half over the backdrop reads as intentional in fullscreen.
        # Nominal size; prepare_for_screen re-derives it per monitor.
        self.art = AlbumArt(320)
        self.art.set_framed(False)
        self.art.setCursor(Qt.PointingHandCursor)
        self.art.setToolTip("back to the full window")
        self.art.clicked.connect(self._request_exit)

        self.title_lbl = QLabel("nothing playing")
        self.title_lbl.setTextFormat(Qt.PlainText)
        self.title_lbl.setAlignment(Qt.AlignHCenter)
        self.artist_lbl = QLabel("")
        self.artist_lbl.setTextFormat(Qt.PlainText)
        self.artist_lbl.setAlignment(Qt.AlignHCenter)

        left_host = QWidget()
        left_host.setMouseTracking(True)
        left = QVBoxLayout(left_host)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(_scale.px(8))
        left.addStretch(1)
        left.addWidget(self.art, alignment=Qt.AlignHCenter)
        left.addSpacing(_scale.px(10))
        left.addWidget(self.title_lbl)
        left.addWidget(self.artist_lbl)
        left.addStretch(1)

        self.lyrics_panel = LyricsView(window.api,
                                       font_scale=_LYRICS_FONT_SCALE)
        # Chrome that belongs to the in-app panel, not this surface.
        for chrome in (self.lyrics_panel.heading,
                       self.lyrics_panel.karaoke_check,
                       self.lyrics_panel.mute_btn,
                       self.lyrics_panel.swap_status):
            chrome.hide()
        self.lyrics_panel.setSizePolicy(QSizePolicy.Expanding,
                                        QSizePolicy.Expanding)
        # Start hidden to match _pane="off"; showEvent applies the
        # remembered pane.
        self.lyrics_panel.hide()

        # The queue pane: the same Queue model the main window drives,
        # rendered as plain text rows over the gradient. Double-click a
        # row to jump to it, like the main queue view.
        self.queue_view = QListView()
        self.queue_view.setModel(window.queue)
        self.queue_view.setItemDelegate(_FsQueueDelegate(self))
        self.queue_view.setFrameShape(QListView.NoFrame)
        self.queue_view.setSelectionMode(QListView.NoSelection)
        self.queue_view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.queue_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue_view.setFocusPolicy(Qt.NoFocus)
        self.queue_view.doubleClicked.connect(window._on_queue_double)
        self.queue_view.hide()

        # Both panes sit centered between stretches, pinned by
        # prepare_for_screen to the art column's band, so whichever is
        # up reads as parallel with the art instead of hugging the top.
        # The host itself is fixed-width and starts collapsed; the pane
        # open/close animates that one width.
        lyrics_host = QWidget()
        lyrics_host.setMouseTracking(True)
        lyrics_host.setFixedWidth(0)
        lyrics_host.hide()
        lh = QVBoxLayout(lyrics_host)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.addStretch(1)
        lh.addWidget(self.lyrics_panel)
        lh.addWidget(self.queue_view)
        lh.addStretch(1)
        self._lyrics_host = lyrics_host

        # No trailing stretch: the pane hugs the right margin, and with
        # the host collapsed the two equal stretches center the art
        # block on the screen — the open/close slide falls out of the
        # host width alone, no stretch juggling.
        center = QHBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        center.addStretch(1)
        center.addWidget(left_host)
        center.addStretch(1)
        center.addWidget(lyrics_host)
        col.addLayout(center, stretch=1)

        # Bottom chrome (fades in zen): transport + seekable progress.
        _g = glyphs.glyph
        self.shuffle_btn = BracketButton(_g("shuffle"), _g("shuffle"))
        self.prev_btn = BracketButton("prev", _g("prev"))
        self.play_btn = BracketButton("play", _g("play"))
        self.next_btn = BracketButton("next", _g("next"))
        self.repeat_btn = BracketButton(_g("repeat"), _g("repeat"))
        self.like_btn = BracketButton(_g("like_off"), _g("like_off"))
        self.shuffle_btn.setToolTip("shuffle")
        self.repeat_btn.setToolTip("repeat: off / all / one")
        for btn in (self.shuffle_btn, self.prev_btn, self.play_btn,
                    self.next_btn, self.repeat_btn, self.like_btn):
            btn.setFocusPolicy(Qt.NoFocus)
        self.progress = ThinProgress()
        self.progress.seek_requested.connect(self._on_seek_requested)
        self.time_lbl = QLabel("0:00 / 0:00")
        self.time_lbl.setTextFormat(Qt.PlainText)
        self.time_lbl.setProperty("class", "dim")

        self._bottom_bar = QWidget()
        self._bottom_bar.setMouseTracking(True)
        bottom = QHBoxLayout(self._bottom_bar)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(_scale.px(6))
        bottom.addWidget(self.shuffle_btn)
        bottom.addWidget(self.prev_btn)
        bottom.addWidget(self.play_btn)
        bottom.addWidget(self.next_btn)
        bottom.addWidget(self.repeat_btn)
        bottom.addWidget(self.like_btn)
        bottom.addSpacing(_scale.px(18))
        bottom.addWidget(self.progress, stretch=1)
        bottom.addSpacing(_scale.px(10))
        bottom.addWidget(self.time_lbl)
        col.addWidget(self._bottom_bar)

        self.central_bg = CentralBg(content)
        # Edge-to-edge — a fullscreen surface has no corners to round.
        self.central_bg.set_radius(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.central_bg)

        # Chrome fade: one animation drives both bars' opacity effects.
        self._top_eff = QGraphicsOpacityEffect(self._top_bar)
        self._top_eff.setOpacity(1.0)
        self._top_bar.setGraphicsEffect(self._top_eff)
        self._bottom_eff = QGraphicsOpacityEffect(self._bottom_bar)
        self._bottom_eff.setOpacity(1.0)
        self._bottom_bar.setGraphicsEffect(self._bottom_eff)
        self._zen_anim: QVariantAnimation | None = None
        self._zen_timer = QTimer(self)
        self._zen_timer.setSingleShot(True)
        self._zen_timer.setInterval(_ZEN_IDLE_MS)
        self._zen_timer.timeout.connect(self._zen_sleep)

        self._apply_text_styles()
        self._pin_label_widths()

        # ---------- wiring ----------
        self.shuffle_btn.clicked.connect(window._on_shuffle_clicked)
        self.prev_btn.clicked.connect(window._on_prev_clicked)
        self.play_btn.clicked.connect(window._on_play_clicked)
        self.next_btn.clicked.connect(window._on_next_clicked)
        self.repeat_btn.clicked.connect(window._on_repeat_clicked)
        self.like_btn.clicked.connect(window._on_like_clicked)
        self.set_modes(window.queue.shuffle_enabled, window.queue.repeat_mode)
        # Not connected straight to the toggles: clicked(checked) would
        # land its bool in the keyword parameters.
        self.lyrics_btn.clicked.connect(self._on_lyrics_btn)
        self.queue_btn.clicked.connect(self._on_queue_btn)
        self.exit_btn.clicked.connect(self._request_exit)

        window.queue.current_changed.connect(self._on_track_changed)
        window.player.state_changed.connect(self._on_state)
        window.player.position_changed.connect(self._on_position)
        window.player.duration_changed.connect(self._on_duration)
        theming.manager().theme_changed.connect(self._on_theme)

        # Escape is a fixed exit affordance on every companion window —
        # deliberately NOT rebindable. L/Q/K are this surface's own
        # pane keys, not ACTIONS ids, so they stay literal too.
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._request_exit)
        # The transport keys mirror rebindable ACTIONS ids. QShortcut
        # context is per-window, so the main window's shortcuts can't
        # fire here — these are built from the SAME effective keymap
        # (and re-keyed by MainWindow.rebind_shortcuts' companion walk),
        # so a rebind follows the user into fullscreen instead of the
        # shipped defaults living on in it. Volume resolves at fire time
        # (attribute access) so a strip rebuild can't strand it.
        self._keymap_handlers = {
            "fullscreen": self._request_exit,
            "mini_mode": self._request_mini,
            "play_pause": window._on_play_clicked,
            "next_track": window._on_next_clicked,
            "prev_track": window._on_prev_clicked,
            "like": window._on_like_clicked,
            "volume_up": lambda: window.volume.setVolume(
                window.volume.volume() + 5),
            "volume_down": lambda: window.volume.setVolume(
                window.volume.volume() - 5),
        }
        self._keymap_shortcuts: dict[str, QShortcut] = {
            action_id: QShortcut(QKeySequence(), self, handler)
            for action_id, handler in self._keymap_handlers.items()
        }
        self.rebind_shortcuts()
        QShortcut(QKeySequence("L"), self, self._on_lyrics_btn)
        QShortcut(QKeySequence("Q"), self, self._on_queue_btn)
        QShortcut(QKeySequence("K"), self, self._toggle_karaoke)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # Default metrics until MainWindow hands over the real target
        # monitor right before showing.
        self.prepare_for_screen(QGuiApplication.primaryScreen())

        self._install_wake_filters()

    def rebind_shortcuts(self) -> None:
        """Re-key the transport shortcuts from the effective keymap.
        Runs at construction and from MainWindow.rebind_shortcuts (the
        keymap editor's accept path) via the _companions walk. An
        unbound action ("" sequence) leaves an inert QShortcut."""
        from .window import effective_keymap
        km = effective_keymap(getattr(self._window, "_settings", None))
        for action_id, sc in self._keymap_shortcuts.items():
            sc.setKey(QKeySequence(km.get(action_id, "")))

    def prepare_for_screen(self, screen) -> None:
        """Size the fixed pieces for the monitor we're about to fill.
        MainWindow calls this with the screen the active window sits on,
        so a fullscreen opened on the second monitor gets that monitor's
        numbers instead of the primary's. AlbumArt multiplies its base
        by the ui scale, so divide it back out to land on the wanted
        pixels."""
        if self._pane_anim is not None:
            self._pane_anim.stop()
            self._pane_anim = None
        geo = screen.geometry() if screen is not None else None
        sw = geo.width() if geo is not None else 1920
        sh = geo.height() if geo is not None else 1080
        art_px = min(int(sh * 0.50), int(sw * 0.38))
        self._art_base = max(200, int(art_px / _scale.factor()))
        # With no pane up the art takes the middle of the screen and a
        # little more room.
        self._art_base_solo = max(self._art_base + 1,
                                  int(self._art_base * 1.08))
        self._pane_w = int(sw * 0.40)
        solo = self._pane not in ("lyrics", "queue")
        self.art.set_base_size(self._art_base_solo if solo
                               else self._art_base)
        self._lyrics_host.setFixedWidth(0 if solo else self._pane_w)
        self._lyrics_host.setVisible(not solo)
        # Pin the side panes to the art column's visual band (art plus
        # its caption) so lyrics/queue sit parallel with the art instead
        # of running from the top edge. Fixed, not max: between the
        # host's centering stretches a max-height pane collapses to its
        # sizeHint (a squeezed strip). Derived from the same numbers as
        # the art tile — its width() is stale until the next layout pass.
        band = _scale.px(self._art_base) + _scale.px(120)
        self.lyrics_panel.setFixedHeight(band)
        self.queue_view.setFixedHeight(band)
        self._pin_label_widths()
        # Re-elide against the new art width.
        if self._raw_title:
            self._set_label(self.title_lbl, self._raw_title,
                            "scramble/fs-title", False)
        if self._raw_artist:
            self._set_label(self.artist_lbl, self._raw_artist,
                            "scramble/fs-artist", False)

    # ---------- settings plumbing ----------

    def _settings(self):
        s = getattr(self._window, "_settings", None)
        if s is None:
            # Bare test construction — defaults, not a disk read per call.
            s = getattr(self, "_fallback_settings", None)
            if s is None:
                from .. import settings as settings_module
                s = settings_module.Settings()
                self._fallback_settings = s
        return s

    def _set_setting(self, field: str, value) -> None:
        s = getattr(self._window, "_settings", None)
        if s is not None:
            setattr(s, field, value)
            try:
                from .. import settings as settings_module
                settings_module.save(s)
            except Exception:
                pass
        else:
            setattr(self._settings(), field, value)
        self.apply_settings()

    def resolved_backdrop_style(self) -> str:
        s = self._settings()
        style = s.fullscreen_backdrop_style or "follow"
        if style == "follow":
            # Follow means the main surface's whole look, including
            # whether the backdrop is on at all. Forcing a gradient here
            # (the mini's rule) painted the theme's bg_alt hue whenever
            # no album palette was flowing — on nord/abyss/storm that is
            # a plainly wrong blue the user's main window never shows.
            if not bool(s.adaptive_background):
                return "off"
            style = s.adaptive_background_style or "field"
        return style

    def apply_settings(self) -> None:
        """Push fullscreen_* settings into the widgets. Called on every
        show and after every context-menu change."""
        s = self._settings()
        style = self.resolved_backdrop_style()
        if style == "off":
            self.central_bg.set_enabled(False)
        else:
            self.central_bg.set_style(style)
            self.central_bg.set_enabled(True)
        self.central_bg.set_motion(s.motion or "lite")

        # The pulse consumer is only held while we're actually on screen.
        # Modes are exclusive, so sharing the mini's gate is safe.
        ambient = getattr(self._window, "_ambient", None)
        if ambient is not None:
            ambient.set_mini_active(
                bool(s.fullscreen_pulse)
                and style != "off"
                and self.isVisible()
            )

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.apply_settings()
        # Snap, don't animate — the remembered pane should simply be
        # there when the mode opens.
        self._apply_pane(self._settings().fullscreen_pane or "lyrics",
                         persist=False, animate=False)
        self._zen_wake(snap=True)
        self._zen_timer.start()
        self._reconcile_inhibit()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._zen_timer.stop()
        self._zen_wake(snap=True)
        ambient = getattr(self._window, "_ambient", None)
        if ambient is not None:
            ambient.set_mini_active(False)
        self._reconcile_inhibit()

    def closeEvent(self, event) -> None:
        # A compositor close means "give me tide back", not "quit" —
        # never leave the app running headless. Same rule as the mini.
        self._inhibitor.set_active(False)
        if not getattr(self._window, "_wants_quit", False) and self._window.isHidden():
            event.ignore()
            self._request_exit()
            return
        super().closeEvent(event)

    # ---------- data slots (queue / player / theme) ----------

    def sync_now(self, track, duration, position, state, liked) -> None:
        """Full refresh, called right before every show so a fullscreen
        opened mid-song is correct on frame one."""
        self._apply_track(track, animate=False)
        self.progress.setDuration(duration or 0.0)
        self.progress.setPosition(position or 0.0)
        self._update_time(position or 0.0, duration or 0.0)
        self._on_state(state)
        self.set_liked(liked)
        try:
            self.set_nav_enabled(self._window.prev_btn.isEnabled(),
                                 self._window.next_btn.isEnabled())
            self.set_like_enabled(self._window.like_btn.isEnabled())
        except RuntimeError:
            pass
        if self._pane == "lyrics":
            self.lyrics_panel.show_for(track)
            self.lyrics_panel.update_position(position or 0.0)
        elif self._pane == "queue":
            self._scroll_queue_to_current()

    def _on_track_changed(self, track) -> None:
        if not self.isVisible():
            return
        self._apply_track(track, animate=True)
        if self._pane == "lyrics":
            self.lyrics_panel.show_for(track)
        elif self._pane == "queue":
            # Rows repaint via the model's own dataChanged; just keep
            # the current one in the middle of the band.
            self._scroll_queue_to_current()

    def _apply_track(self, track, animate: bool = True) -> None:
        if track is None:
            self._raw_title = ""
            self._raw_artist = ""
            self._art_url = None
            self.title_lbl.setText(theming.styled_case("nothing playing"))
            self.artist_lbl.setText("")
            self.art.setImage(None)
            self.central_bg.set_art(None)
            self.progress.reset()
            self._update_time(0.0, 0.0)
            return
        self._raw_title = track.title or ""
        self._raw_artist = track.artists or ""
        self._set_label(self.title_lbl, self._raw_title, "scramble/fs-title",
                        animate)
        self._set_label(self.artist_lbl, self._raw_artist,
                        "scramble/fs-artist", animate)
        url = track.thumbnail or ""
        self._art_url = url or None
        if not url:
            self.art.setImage(None)
            self.central_bg.set_art(None)
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
        # The liquid backdrop melts the cover itself; harmless for the rest.
        self.central_bg.set_art(image)

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
        # Pinned to the art so a long title elides instead of shoving the
        # lyrics pane around (see the mini's window-spazz war story).
        w = self.art.width()
        for lbl in (self.title_lbl, self.artist_lbl):
            lbl.setFixedWidth(w)

    def _on_state(self, state) -> None:
        if state == PlayState.PLAYING:
            self.play_btn.setLabel("pause")
            # ▮▮ not ⏸ — same-font baseline alignment; see glyphs.py.
            self.play_btn.setGlyph(glyphs.glyph("pause"))
        elif state == PlayState.LOADING:
            self.play_btn.setLabel(glyphs.glyph("loading"))
            self.play_btn.setGlyph(glyphs.glyph("loading"))
        else:
            self.play_btn.setLabel("play")
            self.play_btn.setGlyph(glyphs.glyph("play"))
        self._reconcile_inhibit()

    def _on_position(self, secs: float) -> None:
        if not self.isVisible():
            return
        duration = self._window.player.duration
        self.progress.setPosition(secs)
        self._update_time(secs, duration)
        if self._pane != "lyrics":
            return
        # Karaoke interpolates the active word per tick, so it gets every
        # update; the line list only needs ~4 Hz.
        if self.lyrics_panel._karaoke_mode:
            self._last_lyrics_pos = secs
            self.lyrics_panel.update_position(secs)
        elif abs(secs - self._last_lyrics_pos) >= 0.25:
            self._last_lyrics_pos = secs
            self.lyrics_panel.update_position(secs)

    def _on_duration(self, secs: float) -> None:
        if not self.isVisible():
            return
        self.progress.setDuration(secs)
        self._update_time(self._window._last_position, secs)

    def _update_time(self, pos: float, dur: float) -> None:
        self.time_lbl.setText(f"{_mmss(pos)} / {_mmss(dur)}")

    def _on_theme(self, _theme) -> None:
        # Art rescales on ui_scale changes (its own theme handler runs
        # first, so its width is current here) — keep the pins in step.
        self._pin_label_widths()
        self._apply_text_styles()
        if self._raw_title:
            self._set_label(self.title_lbl, self._raw_title,
                            "scramble/fs-title", False)
        if self._raw_artist:
            self._set_label(self.artist_lbl, self._raw_artist,
                            "scramble/fs-artist", False)

    def _apply_text_styles(self) -> None:
        # Big glanceable type, themed by token. Per-widget styles (not
        # `class` properties) because the sizes here are this surface's
        # own, like the karaoke widget does it.
        theme = theming.manager().current_effective()
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        self.title_lbl.setStyleSheet(
            f"color: {fg}; background: transparent; "
            f"font-size: {_scale.round_pt(19)}pt; font-weight: 700;"
        )
        self.artist_lbl.setStyleSheet(
            f"color: {dim}; background: transparent; "
            f"font-size: {_scale.round_pt(13)}pt;"
        )

    # ---------- like / nav state pushed by MainWindow ----------

    def set_liked(self, liked: bool) -> None:
        glyph = glyphs.glyph("like_on" if liked else "like_off")
        self.like_btn.setLabel(glyph)
        self.like_btn.setGlyph(glyph)

    def set_like_enabled(self, enabled: bool) -> None:
        self.like_btn.setEnabled(bool(enabled))

    def set_nav_enabled(self, prev_ok: bool, next_ok: bool) -> None:
        self.prev_btn.setEnabled(bool(prev_ok))
        self.next_btn.setEnabled(bool(next_ok))

    def set_modes(self, shuffle_on: bool, repeat_mode) -> None:
        from ..queue import RepeatMode
        mode = RepeatMode.parse(repeat_mode)
        self.shuffle_btn.setActiveState(bool(shuffle_on))
        self.repeat_btn.setActiveState(mode is not RepeatMode.OFF)
        glyph = glyphs.glyph(
            "repeat_one" if mode is RepeatMode.ONE else "repeat")
        self.repeat_btn.setLabel(glyph)
        self.repeat_btn.setGlyph(glyph)

    # ---------- ambient pulse fan-in ----------

    def set_pulse(self, level: float) -> None:
        """AmbientController target hook — the backdrop swells with the
        actual bass envelope, same as the mini and the main surface."""
        self.central_bg.set_pulse(level)

    # ---------- side panes (lyrics / queue) ----------

    def _on_lyrics_btn(self) -> None:
        self._apply_pane("off" if self._pane == "lyrics" else "lyrics")

    def _on_queue_btn(self) -> None:
        self._apply_pane("off" if self._pane == "queue" else "queue")

    def _apply_pane(self, pane: str, persist: bool = True,
                    animate: bool = True) -> None:
        pane = pane if pane in ("lyrics", "queue", "off") else "lyrics"
        was_open = self._pane in ("lyrics", "queue")
        self._pane = pane
        # Hide first, show second. Both panes are pinned to the band
        # height; an instant with both visible stacks them and spikes
        # the layout minimum past the screen, which grew the window and
        # shoved the whole surface down (the reported shift).
        if pane != "lyrics":
            self.lyrics_panel.hide()
        if pane != "queue":
            self.queue_view.hide()
        if pane == "lyrics":
            self.lyrics_panel.show()
        elif pane == "queue":
            self.queue_view.show()
        self.lyrics_btn.setActiveState(pane == "lyrics")
        self.queue_btn.setActiveState(pane == "queue")
        open_ = pane in ("lyrics", "queue")
        if open_ != was_open or self._lyrics_host.isVisible() != open_:
            self._animate_pane(open_, animate=animate)
        if pane == "lyrics":
            self.lyrics_panel.show_for(getattr(self._window, "_current", None))
            self.lyrics_panel.update_position(
                getattr(self._window, "_last_position", 0.0))
        elif pane == "queue":
            self._scroll_queue_to_current()
        if persist and (self._settings().fullscreen_pane or "lyrics") != pane:
            self._set_setting("fullscreen_pane", pane)

    def _animate_pane(self, open_: bool, animate: bool = True) -> None:
        """Slide the pane host open/closed and let the art follow: pane
        gone, the art glides to the true center and grows a touch; pane
        back, it returns. One animated width drives the whole move — the
        center row has no trailing stretch, so the collapsing host IS
        the centering."""
        if self._pane_anim is not None:
            self._pane_anim.stop()
            self._pane_anim = None
        start_w = self._lyrics_host.width() if self._lyrics_host.isVisible() else 0
        end_w = self._pane_w if open_ else 0
        start_a = self.art._base_size
        end_a = self._art_base if open_ else self._art_base_solo
        if open_:
            self._lyrics_host.show()
        if (not animate
                or motion_module.intensity() == motion_module.Intensity.OFF):
            self._lyrics_host.setFixedWidth(end_w)
            self.art.set_base_size(end_a)
            if not open_:
                self._lyrics_host.hide()
            self._finish_pane_move()
            return

        def _tick(t) -> None:
            t = float(t)
            self._lyrics_host.setFixedWidth(
                round(start_w + (end_w - start_w) * t))
            self.art.set_base_size(round(start_a + (end_a - start_a) * t))
            self._pin_label_widths()

        def _done() -> None:
            self._pane_anim = None
            if not open_:
                self._lyrics_host.hide()
            self._finish_pane_move()

        anim = QVariantAnimation(self)
        anim.setDuration(motion_module.DUR_MED)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.valueChanged.connect(_tick)
        anim.finished.connect(_done)
        self._pane_anim = anim
        anim.start()

    def _finish_pane_move(self) -> None:
        # Re-pin and re-elide against the settled art width.
        self._pin_label_widths()
        if self._raw_title:
            self._set_label(self.title_lbl, self._raw_title,
                            "scramble/fs-title", False)
        if self._raw_artist:
            self._set_label(self.artist_lbl, self._raw_artist,
                            "scramble/fs-artist", False)

    def _scroll_queue_to_current(self) -> None:
        q = self._window.queue
        row = q.current_index
        if 0 <= row < q.rowCount():
            self.queue_view.scrollTo(q.index(row), QListView.PositionAtCenter)

    def _toggle_karaoke(self) -> None:
        # Route through the panel's own (hidden) checkbox so the mode
        # state lives in exactly one place. Turning it on wants the
        # lyrics on screen, so the pane follows.
        check = self.lyrics_panel.karaoke_check
        turning_on = not check.isChecked()
        if turning_on and self._pane != "lyrics":
            self._apply_pane("lyrics")
        check.setChecked(turning_on)

    # ---------- zen (chrome + cursor fade away) ----------

    def _install_wake_filters(self) -> None:
        for w in [self] + self.findChildren(QWidget):
            w.removeEventFilter(self)
            w.installEventFilter(self)
            w.setMouseTracking(True)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.MouseMove, QEvent.Enter,
                            QEvent.MouseButtonPress, QEvent.Wheel,
                            QEvent.HoverMove):
            self._zen_wake()
            if self.isVisible():
                self._zen_timer.start()
        return super().eventFilter(obj, event)

    def _animate_chrome(self, to: float) -> None:
        if self._zen_anim is not None:
            self._zen_anim.stop()
            self._zen_anim = None
        if motion_module.intensity() == motion_module.Intensity.OFF:
            self._top_eff.setOpacity(to)
            self._bottom_eff.setOpacity(to)
            return
        anim = QVariantAnimation(self)
        anim.setDuration(motion_module.DUR_MED)
        anim.setStartValue(float(self._top_eff.opacity()))
        anim.setEndValue(float(to))
        anim.valueChanged.connect(self._on_chrome_opacity)
        self._zen_anim = anim
        anim.start()

    def _on_chrome_opacity(self, value) -> None:
        self._top_eff.setOpacity(float(value))
        self._bottom_eff.setOpacity(float(value))

    def _zen_sleep(self) -> None:
        if self._menu is not None and self._menu.isVisible():
            return
        if self._zen_asleep:
            return
        self._zen_asleep = True
        self._animate_chrome(0.0)
        # The cursor goes with the chrome — a parked arrow over the art
        # ruins the whole "ambient display" read.
        self.setCursor(Qt.BlankCursor)
        self.art.setCursor(Qt.BlankCursor)

    def _zen_wake(self, snap: bool = False) -> None:
        if not self._zen_asleep and self._zen_anim is None:
            return
        self._zen_asleep = False
        self.unsetCursor()
        self.art.setCursor(Qt.PointingHandCursor)
        if snap:
            if self._zen_anim is not None:
                self._zen_anim.stop()
                self._zen_anim = None
            self._top_eff.setOpacity(1.0)
            self._bottom_eff.setOpacity(1.0)
        else:
            self._animate_chrome(1.0)

    # ---------- interaction ----------

    def _request_exit(self) -> None:
        # Deferred: reached from mouse handlers / shortcuts; hiding
        # windows inside the emission is the PySide6+py3.14 crash pattern.
        QTimer.singleShot(0, self._window.exit_fullscreen_mode)

    def _request_mini(self) -> None:
        QTimer.singleShot(0, lambda: self._window.set_mini_mode(True))

    def _on_seek_requested(self, seconds: float) -> None:
        self._window.player.seek(seconds)

    def _reconcile_inhibit(self) -> None:
        try:
            playing = self._window.player.state == PlayState.PLAYING
        except Exception:
            playing = False
        self._inhibitor.set_active(self.isVisible() and playing)

    # ---------- context menu ----------

    def _on_context_menu(self, pos) -> None:
        s = self._settings()
        menu = QMenu(self)

        backdrop = menu.addMenu(theming.styled_case("backdrop"))
        current_style = s.fullscreen_backdrop_style or "follow"
        for label, slug in _BACKDROP_CHOICES:
            act = QAction(theming.styled_case(label), backdrop)
            act.setCheckable(True)
            act.setChecked(slug == current_style)
            act.triggered.connect(
                lambda _checked=False, slug=slug:
                self._set_setting("fullscreen_backdrop_style", slug)
            )
            backdrop.addAction(act)

        menu.addSeparator()
        lyr = QAction(theming.styled_case("lyrics pane"), menu)
        lyr.setCheckable(True)
        lyr.setChecked(self._pane == "lyrics")
        lyr.triggered.connect(lambda _checked=False: self._on_lyrics_btn())
        menu.addAction(lyr)
        que = QAction(theming.styled_case("queue pane"), menu)
        que.setCheckable(True)
        que.setChecked(self._pane == "queue")
        que.triggered.connect(lambda _checked=False: self._on_queue_btn())
        menu.addAction(que)
        kar = QAction(theming.styled_case("karaoke mode"), menu)
        kar.setCheckable(True)
        kar.setChecked(self.lyrics_panel.karaoke_check.isChecked())
        kar.triggered.connect(lambda _checked=False: self._toggle_karaoke())
        menu.addAction(kar)
        pulse = QAction(theming.styled_case("bass pulse"), menu)
        pulse.setCheckable(True)
        pulse.setChecked(bool(s.fullscreen_pulse))
        pulse.triggered.connect(
            lambda checked=False:
            self._set_setting("fullscreen_pulse", bool(checked))
        )
        menu.addAction(pulse)

        menu.addSeparator()
        exit_act = QAction(theming.styled_case("exit fullscreen"), menu)
        exit_act.triggered.connect(self._request_exit)
        menu.addAction(exit_act)

        self._menu = menu
        # popup() is non-blocking — no modal-from-handler hazard.
        menu.popup(self.mapToGlobal(pos))
