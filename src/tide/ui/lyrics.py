"""Lyrics panel — plain text + timed (synced) + karaoke modes.

Three render paths:

  - **untimed**: render the full block in a scrollable label, like before.
  - **timed (default)**: render each line as its own widget so we can
    highlight the active one (bolder + larger) and scroll it into view as
    playback advances. The active line is sung word by word: a SweepLine
    takes its label's place in the list while it's active.
  - **karaoke**: toggleable from a checkbox at the top of this view.
    Big-center current line whose words light up as they're sung, the
    light sweeping through each word (ui/karaoke_line); prev/next lines
    small + dim above and below.

Word timing (2.3) comes from lyric_timing's shared service: a
word-synced source (LRClib lyricsfile, NetEase YRC) matched onto the
video's lines, optionally the local aligner, else line mode — words
spread across each line's window by length. The karaoke checkbox names
which one is in force. ``offset_ms`` (the "lyrics offset" setting) moves
every surface's lyrics against the playhead.

Modern (3.2) reads differently from brutalist, which keeps its 1.x look:
the sung line is the brightest thing on the page and what's coming sits a
step under it (line_tones), the sung line grows into place with the
scroll instead of snapping, lines fade out at the viewport's edges, the
scrollbar only shows while you scroll, and the empty states are a quiet
centred line with a retry.
"""
from __future__ import annotations

import weakref
from bisect import bisect_right

from PySide6.QtCore import (
    QEvent, QObject, QPoint, QRectF, QThread, Qt, QVariantAnimation, Signal, QTimer,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QGraphicsEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import api, contrast, lyric_timing, lyric_words, lyrics_picks, qthreads, theming
from . import legibility, motion as motion_module, scale as _scale
from .karaoke_line import SweepLine
from .headings import Heading, line_heading as _line_heading
from .widgets import BracketButton


# Lyric type is sized against the app's body text (the theme's size, or
# the user's override, times the ui scale), never a fixed point size: at
# "huge" the old fixed 10pt lines came out smaller than the checkbox above
# them. Multipliers per step: (timed line, active line, karaoke side
# line, karaoke center line).
SIZES: dict[str, tuple[float, float, float, float]] = {
    "small": (1.0, 1.3, 1.2, 2.8),
    "medium": (1.3, 1.6, 1.3, 3.0),
    "large": (1.6, 2.0, 1.45, 3.2),
    "huge": (2.1, 2.6, 1.7, 3.6),
}
SIZE_BLURBS: dict[str, str] = {
    "small": "small · the size of the text around it",
    "medium": "medium",
    "large": "large",
    "huge": "huge · for across the room",
}
DEFAULT_SIZE = "large"

_size: str = DEFAULT_SIZE
_views: "weakref.WeakSet[LyricsView]" = weakref.WeakSet()


def set_size(name: str) -> None:
    """Pick a lyric size and restyle every open lyrics view (the panel,
    the mini's, fullscreen's). Unknown names fall back to the default."""
    global _size
    name = str(name or "").strip().lower()
    _size = name if name in SIZES else DEFAULT_SIZE
    for view in list(_views):
        try:
            view.refresh_type()
        except RuntimeError:
            pass    # C++ side already gone


def size() -> str:
    return _size


_offset_s: float = 0.0
OFFSET_RANGE_MS = 2000


def set_offset_ms(ms) -> None:
    """Move every surface's lyrics against the playhead: positive shows
    them later (for a sheet that runs ahead of the singer)."""
    global _offset_s
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        ms = 0
    ms = max(-OFFSET_RANGE_MS, min(OFFSET_RANGE_MS, ms))
    _offset_s = -ms / 1000.0
    _apply_offsets()


def _apply_offsets() -> None:
    lyric_timing.set_offset_s(_offset_s + _song_offset_s)
    for view in list(_views):
        try:
            view.update_position(view._raw_position)
        except RuntimeError:
            pass


# The playing song's own timing nudge (lyrics_picks), on top of the
# setting. Everything that reads lyric_timing.offset_s gets the sum.
_song_key: str | None = None
_song_offset_s: float = 0.0
TIMING_STEP_MS = 250


def song_changed(track) -> None:
    """The playing song changed: load its own nudge. The main window
    calls this on every track change, so discord's lyric line and the
    plugins get it too, not only an open lyrics view."""
    global _song_key, _song_offset_s
    key = lyric_timing.track_key(track) if track is not None else None
    if key == _song_key:
        return
    _song_key = key
    _song_offset_s = -lyrics_picks.offset_ms(key) / 1000.0
    _apply_offsets()


def set_song_offset_ms(key: str | None, ms: int) -> None:
    """Nudge one song's lyrics (positive = later) and remember it."""
    global _song_offset_s
    if not key:
        return
    rng = lyrics_picks.OFFSET_RANGE_MS
    ms = max(-rng, min(rng, int(ms)))
    lyrics_picks.set_offset_ms(key, ms)
    if key == _song_key:
        _song_offset_s = -ms / 1000.0
        _apply_offsets()


# Romanized lines under the lyrics, where NetEase has them (Japanese,
# mostly). One switch for every view, in the right-click menu; the main
# window keeps it in settings (set_romanized_saver).
_roman_on: bool = False
_roman_saver = None


def set_romanized(on: bool, *, save: bool = True) -> None:
    global _roman_on
    _roman_on = bool(on)
    if save and _roman_saver is not None:
        try:
            _roman_saver(_roman_on)
        except Exception:
            pass
    for view in list(_views):
        try:
            view._apply_roman()
        except RuntimeError:
            pass


def romanized() -> bool:
    return _roman_on


def set_romanized_saver(fn) -> None:
    global _roman_saver
    _roman_saver = fn


def wants_roman(texts) -> bool:
    """Lyrics in another script (kana, hangul, ...): worth asking NetEase
    for romanized lines. Mostly Latin text never asks."""
    letters = [ch for t in texts for ch in (t or "") if ch.isalpha()]
    if len(letters) < 12:
        return False
    other = sum(1 for ch in letters if ord(ch) > 0x24F)
    return other / len(letters) >= 0.3


def prefetch(api_obj, track) -> None:
    """Fetch ``track``'s lyrics into the shared answer ahead of time, so
    the lyrics are there the moment the song starts. The main window calls
    this alongside the stream prefetch, as a song nears its end."""
    if api_obj is None or track is None or not getattr(track, "video_id", None):
        return
    source = lyrics_picks.source(lyric_timing.track_key(track))

    def run() -> None:
        from ..lyrics_provider import fetch_from
        try:
            fetch_from(api_obj, track, source)
        except Exception:
            pass                # the view asks again when the song starts
    import threading
    threading.Thread(target=run, name="tide-lyrics-prefetch", daemon=True).start()


def _fmt_nudge(ms: int) -> str:
    if not ms:
        return "0 s"
    num = f"{abs(ms) / 1000.0:.2f}".rstrip("0").rstrip(".")
    return f"{'+' if ms > 0 else '−'}{num} s"


_list_sweep_on: bool = True


def set_list_sweep(on: bool) -> None:
    """Whether the lyrics list's active line sweeps word by word (off =
    the whole line lights at once). Karaoke is not affected."""
    global _list_sweep_on
    _list_sweep_on = bool(on)
    for view in list(_views):
        try:
            view.sweep_check.blockSignals(True)
            view.sweep_check.setChecked(_list_sweep_on)
            view.sweep_check.blockSignals(False)
            view._unplace_sweep()
            view._restyle_lines()
            view._place_sweep()
            view._list_sweep.report_position(view._last_position)
        except RuntimeError:
            pass


def list_sweep() -> bool:
    return _list_sweep_on


# Karaoke mode and the word sweep show only while a plugin offers word
# view (precise lyric timing does, ctx.lyrics.enable_word_view). Without
# it the lyrics are plain synced lines: no toggles, no karaoke, no sweep.
_word_view = 0


def word_view() -> bool:
    return _word_view > 0


def set_word_view(on: bool) -> None:
    global _word_view
    _word_view = max(0, _word_view + (1 if on else -1))
    for view in list(_views):
        try:
            view._sync_word_view()
        except RuntimeError:
            pass


def offset_ms() -> int:
    return int(round(-_offset_s * 1000))


def refresh_timing() -> None:
    """Re-ask the word-timing service for every open view's sheet (its
    settings changed). Cheap when nothing did: the answer is cached."""
    for view in list(_views):
        try:
            view.refresh_timing()
        except RuntimeError:
            pass


TIMING_TAGS: dict[str, str] = {
    "source": "synced words",
    "aligned": "aligned words",
    "line": "estimated words",
}


def timing_of(sheet) -> str:
    """The sheet's best word timing: "source" > "aligned" > "line"."""
    kinds = {l.timing for l in sheet if l.words}
    for k in ("source", "aligned"):
        if k in kinds:
            return k
    return "line"


def size_choices() -> tuple[tuple[str, str], ...]:
    return tuple((k, SIZE_BLURBS[k]) for k in SIZES)


# The lyrics tab's layout. A theme picks it in its [slots] table
# (lyrics = "list" | "beside" | "center"; say nothing and it's the list),
# and the "lyrics layout" setting can override the theme.
LAYOUTS: dict[str, str] = {
    "list": "list · the lines down the left",
    "beside": "beside the cover · the art and title on the left",
    "center": "center · the lines down the middle",
}
LAYOUT_THEME = "theme"
# beside needs room for the cover and the lines; a narrower tab shows the list
BESIDE_MIN_W = 760

_layout_pref: str = LAYOUT_THEME


def set_layout_pref(name: str) -> None:
    """The "lyrics layout" setting: a layout, or "theme" to follow it."""
    global _layout_pref
    name = str(name or "").strip().lower()
    _layout_pref = name if name in LAYOUTS else LAYOUT_THEME
    for view in list(_views):
        try:
            view.refresh_layout()
        except RuntimeError:
            pass


def layout_pref() -> str:
    return _layout_pref


def layout_choices() -> tuple[tuple[str, str], ...]:
    return ((LAYOUT_THEME, "the theme's · whatever the theme picks"),) + tuple(LAYOUTS.items())


def layout_for(theme) -> str:
    """The layout in force: the setting's, else the theme's slot, else
    the list."""
    if _layout_pref in LAYOUTS:
        return _layout_pref
    try:
        slot = str(theme.t("slots", "lyrics", "") or "").strip().lower() if theme else ""
    except Exception:
        slot = ""
    return slot if slot in LAYOUTS else "list"


def _body_pt() -> float:
    mgr = theming.manager()
    theme = mgr.current_effective()
    base = 0.0
    try:
        base = float(mgr.user_font_size() or 0)
    except Exception:
        base = 0.0
    if base <= 0:
        base = float(theme.t("typography", "size_pt", 10) if theme else 10)
    return base * _scale.factor()


def type_pt(role: str, font_scale: float = 1.0) -> int:
    """Point size for one lyric role ("line", "active", "side", "center")
    at the current size step, body text and ui scale."""
    idx = ("line", "active", "side", "center").index(role)
    return max(1, round(_body_pt() * SIZES[_size][idx] * font_scale))


# Modern line tones, as APCA lightness contrast against the theme's bg.
# An accent quieter than this lifts toward fg so the sung line still leads.
ACTIVE_MIN_LC = 60.0
# Lines still to come get this share of the sung line's contrast,
UPCOMING_SHARE = 0.78
# and lines already sung at most this share of theirs: a theme whose dim
# is strong (paper, golden hour) eases it toward bg for the list, but no
# further than PAST_MIN_LC, the least any text should have.
PAST_SHARE = 0.62
PAST_MIN_LC = 30.0


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, float(t)))
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def line_tones(theme) -> tuple[QColor, QColor, QColor]:
    """(sung, coming, sung-past) inks for the modern lyrics list. Derived
    from the theme's fg / dim / accent / bg, so every theme gets them:
    the sung line is the accent (lifted toward fg when the accent reads
    quieter than ACTIVE_MIN_LC), what's coming is fg eased toward dim
    until it sits under the sung line, and what's been sung is dim, eased
    toward bg when it would crowd what's coming."""
    def tok(name: str, default: str) -> QColor:
        c = QColor(theme.token(name, default) if theme else default)
        return c if c.isValid() else QColor(default)

    fg, dim = tok("fg", "#e6e6e6"), tok("dim", "#6f6f6f")
    accent, bg = tok("accent", "#d4b95e"), tok("bg", "#000000")
    y_bg = contrast.apca_y(bg)

    def lc(c: QColor) -> float:
        return abs(contrast.apca_lc(contrast.apca_y(c), y_bg))

    def solve(a: QColor, b: QColor, ok) -> QColor:
        # smallest blend of a toward b that passes ok (ok rises with t)
        lo, hi = 0.0, 1.0
        for _ in range(14):
            mid = (lo + hi) / 2.0
            if ok(_mix(a, b, mid)):
                hi = mid
            else:
                lo = mid
        return _mix(a, b, hi)

    sung = QColor(accent)
    want = min(lc(fg), ACTIVE_MIN_LC)
    if lc(sung) < want:
        sung = solve(accent, fg, lambda c: lc(c) >= want)
    if lc(fg) <= lc(dim):
        return sung, QColor(fg), QColor(dim)     # an odd theme: leave it be
    target = lc(sung) * UPCOMING_SHARE
    coming = QColor(fg) if lc(fg) <= target else solve(fg, dim, lambda c: lc(c) <= target)
    past = QColor(dim)
    cap = max(lc(coming) * PAST_SHARE, min(lc(dim), PAST_MIN_LC))
    if lc(dim) > cap:
        past = solve(dim, bg, lambda c: lc(c) <= cap)
    return sung, coming, past


def _pt_css(pt: float) -> str:
    return f"{round(float(pt), 2):g}pt"


# A list line's look: (point size, colour, vertical padding px, weight).
def _line_css(look) -> str:
    pt, color, pad, weight = look
    return (f"color: {color.name()}; background: transparent; "
            f"font-weight: {int(weight)}; font-size: {_pt_css(pt)}; "
            f"padding: {round(pad)}px {_scale.px(LINE_HPAD)}px;")


def _lerp_look(a, b, p: float):
    p = max(0.0, min(1.0, float(p)))
    weight = a[3] + (b[3] - a[3]) * p
    return (a[0] + (b[0] - a[0]) * p,
            _mix(a[1], b[1], p),
            a[2] + (b[2] - a[2]) * p,
            int(round(weight / 100.0)) * 100)


class _EdgeFade(QGraphicsEffect):
    """Fades one widget out toward the top and bottom of the lyrics
    viewport. Only lines near an edge wear one (LyricsView._refresh_fades),
    so the sung line and its sweep never paint through a pixmap."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # (widget top in the viewport, viewport height, fade px,
        #  top strength, bottom strength)
        self._band = (0.0, 0.0, 0.0, 0.0, 0.0)

    def set_band(self, y: float, vh: float, fade: float,
                 top_k: float, bottom_k: float) -> None:
        band = (float(y), float(vh), float(fade), float(top_k), float(bottom_k))
        if band != self._band:
            self._band = band
            self.update()

    def draw(self, painter: QPainter) -> None:
        offset = QPoint()
        pix = self.sourcePixmap(Qt.LogicalCoordinates, offset, QGraphicsEffect.NoPad)
        if pix.isNull():
            return
        y, vh, fade, top_k, bottom_k = self._band
        if vh > 0.0 and fade > 0.0:
            share = min(0.5, fade / vh)
            top = -y - offset.y()
            grad = QLinearGradient(0.0, top, 0.0, top + vh)
            grad.setColorAt(0.0, QColor(0, 0, 0, round(255 * (1.0 - top_k))))
            grad.setColorAt(share, QColor(0, 0, 0, 255))
            grad.setColorAt(1.0 - share, QColor(0, 0, 0, 255))
            grad.setColorAt(1.0, QColor(0, 0, 0, round(255 * (1.0 - bottom_k))))
            dpr = pix.devicePixelRatio() or 1.0
            p = QPainter(pix)
            p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            p.fillRect(QRectF(0.0, 0.0, pix.width() / dpr, pix.height() / dpr), grad)
            p.end()
        painter.drawPixmap(offset, pix)


# The empty states: brutalist keeps its 1.x lines, modern says it plainly
# (centred, with a retry where one could help).
STATUS_PLAIN: dict[str, str] = {
    "no_track": "── no track ──",
    "loading": "── loading ──",
    "missing": "── no lyrics for this track ──",
    "failed": "── lyrics unavailable ──",
}
STATUS_MODERN: dict[str, str] = {
    "no_track": "nothing playing",
    "loading": "loading lyrics",
    "missing": "no lyrics for this song",
    "failed": "couldn't get the lyrics",
}
STATUS_RETRY = frozenset({"missing", "failed"})
# a cached sheet lands well inside this, so "loading" doesn't flash
LOADING_DELAY_MS = 300
# the quiet scrollbar stays this long after the last scroll
BAR_LINGER_MS = 1200

# Breaks (the intro, an instrumental, a long blank line) this long get
# the dots where the next line will land.
GAP_MIN_S = 4.0
# A line stays lit this long past the end of its singing (when the source
# says where that is), and only fades back if the next line is at least
# FADE_MIN_S away: a quick handoff just moves on.
TAIL_GRACE_S = 0.35
FADE_MIN_S = 2.5
# After a scroll of your own the list stops following the song, then
# picks it up again after this long without one.
FOLLOW_IDLE_MS = 8000
# Moving between lines crosses a gap; the hover holds this long so the
# plate slides instead of blinking.
HOVER_LINGER_MS = 90
# Modern lines' side padding: room for the hover plate around the text.
LINE_HPAD = 10
# how long the word after a key (the timing nudge, a copy) stays up
TOAST_MS = 1400

# Clicking a line plays from it. The main window hands over its player's
# seek; every lyrics view (tab, mini, fullscreen, visualizer tile) uses it.
_seek_fn = None


def set_seeker(fn) -> None:
    """``fn(seconds)`` seeks the player, or None for no click to seek."""
    global _seek_fn
    _seek_fn = fn


def _is_blank(text: str) -> bool:
    """No words to sing: empty, or only ♪ and punctuation."""
    return not any(ch.isalnum() for ch in (text or ""))


_NOTE_CHARS = frozenset("♩♪♫♬🎵🎶🎼")


def _is_note(text: str) -> bool:
    """A source's instrumental mark (a line of just ♪): drawn as the
    break's three dots instead."""
    t = (text or "").strip()
    return bool(t) and _is_blank(t) and any(ch in _NOTE_CHARS for ch in t)


def _paint_dots(p: QPainter, *, left: float, cy: float, d: float,
                modern: bool, colors) -> None:
    """The three dots of a break: round in modern, square in brutalist."""
    gap = d * 0.8
    p.setRenderHint(QPainter.Antialiasing, modern)
    p.setPen(Qt.NoPen)
    for k, c in enumerate(colors):
        p.setBrush(c)
        box = QRectF(left + k * (d + gap), cy - d / 2.0, d, d)
        if modern:
            p.drawEllipse(box)
        else:
            p.drawRect(box)


def _dot_size(font_height: float) -> float:
    return max(5.0, font_height * 0.42)


def _dots_width(d: float) -> float:
    return 3.0 * d + 2.0 * d * 0.8


def _gap_key(gap) -> int | None:
    # a break is known by the line it leads to
    return gap[0] if gap else None


def _clock(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


def real_line_ends(sheet, result) -> list:
    """When each line's singing stops, only where something knows it:
    a word-timed line's last word, or the source's own line end (YT and
    lrclib's lyricsfile say). None where it would only be a guess, so a
    line never fades back while it's still being sung."""
    src = list(getattr(result, "line_ends", []) or [])
    known = len(src) == len(sheet)
    out: list = []
    for i, line in enumerate(sheet):
        end = None
        if line.words and line.timing in ("source", "aligned"):
            end = float(line.words[-1].end)
        elif known and src[i] is not None:
            end = float(line.end)        # the source's end, capped at the next line
        out.append(end)
    return out


class _HoverPlate(QWidget):
    """The hovered line's backing: a faint plate in modern and the time a
    click jumps to, in both. Sits under the lines and ignores the mouse."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._tint = QColor(0, 0, 0, 0)
        self._ink = QColor("#6f6f6f")
        self._radius = 0.0
        self._inset = 0
        self._time = ""

    def set_look(self, *, tint: QColor, ink: QColor, radius: float, inset: int) -> None:
        self._tint, self._ink = QColor(tint), QColor(ink)
        self._radius, self._inset = float(radius), int(inset)
        self.update()

    def set_time(self, text: str) -> None:
        if text != self._time:
            self._time = text
            self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._tint.alpha():
            r = min(self._radius, self.height() / 2.0)
            p.setPen(Qt.NoPen)
            p.setBrush(self._tint)
            p.drawRoundedRect(QRectF(self.rect()), r, r)
        if self._time:
            p.setPen(legibility.ink(self, self._ink))
            p.drawText(self.rect().adjusted(0, 0, -self._inset, 0),
                       int(Qt.AlignRight | Qt.AlignVCenter), self._time)
        p.end()


class _BackPill(QPushButton):
    """"back to the song", floating over the list while it isn't
    following. Opaque on purpose: lyrics scroll under it. Modern is a
    rounded panel with a hairline; brutalist is its bracketed text on bg."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._theme = None
        self._label = "back to the song"

    def set_label(self, text: str) -> None:
        self._label = str(text)
        self.updateGeometry()
        self.update()

    def set_theme(self, theme) -> None:
        self._theme = theme
        self.updateGeometry()
        self.update()

    def _modern(self) -> bool:
        return getattr(self._theme, "aesthetic", "") == "modern"

    def _text(self) -> str:
        label = theming.styled_case(self._label, self._theme)
        return label if self._modern() else f"[{label}]"

    def sizeHint(self):
        from PySide6.QtCore import QSize
        fm = self.fontMetrics()
        hpad, vpad = (_scale.px(16), _scale.px(7)) if self._modern() else (_scale.px(6), _scale.px(3))
        return QSize(fm.horizontalAdvance(self._text()) + 2 * hpad, fm.height() + 2 * vpad)

    def paintEvent(self, _ev) -> None:
        from .. import material
        theme = self._theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hot = self.underMouse() or self.isDown()
        fg = QColor(theme.token("fg", "#e6e6e6") if theme else "#e6e6e6")
        accent = QColor(theme.token("accent", "#d4b95e") if theme else "#d4b95e")
        if self._modern():
            fill, line = material.panel_colors(theme)
            p.setPen(line)
            p.setBrush(fill)
            r = rect.height() / 2.0
            p.drawRoundedRect(rect, r, r)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"))
            p.drawRect(rect)
        p.setPen(accent if hot else fg)
        p.drawText(self.rect(), int(Qt.AlignCenter), self._text())
        p.end()

    def enterEvent(self, ev) -> None:
        super().enterEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:
        super().leaveEvent(ev)
        self.update()


class _GapMarker(QWidget):
    """Three dots where the next line will land, lighting up as it gets
    close: the intro, an instrumental break, a long blank line. Modern
    fills round dots smoothly; brutalist lights square cells one at a time.
    Keeps its own clock between position reports, like the sweep line."""

    FRAME_MS = 33

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        from .karaoke_line import PlayClock
        self.clock = PlayClock()
        self._span = (0.0, 1.0)
        self._lit = QColor("#d4b95e")
        self._dim = QColor("#6f6f6f")
        self._modern = True
        self._pt = 16.0
        self._left = 0
        self._presence = 1.0
        self._alpha = 1.0
        self._centered = False
        # 1 = every dot back to unlit: how the wait ends on a ♪ line, so
        # the line's own (unlit) dots take over without a blink
        self._drain = 0.0
        # standing in for lines that stepped aside: exactly their height
        self._full_h: int | None = None
        self._state = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._apply_height()

    def set_look(self, *, lit: QColor, dim: QColor, modern: bool, pt: float, left: int) -> None:
        self._lit, self._dim, self._modern = QColor(lit), QColor(dim), bool(modern)
        self._pt, self._left = float(pt), int(left)
        f = QFont(self.font())
        f.setPointSizeF(max(1.0, self._pt))
        self.setFont(f)
        self._apply_height()
        self._state = None
        self.update()

    def set_centered(self, on: bool) -> None:
        self._centered = bool(on)
        self.update()

    def set_span(self, start: float, end: float) -> None:
        self._span = (float(start), max(float(start) + 0.01, float(end)))
        self._drain = 0.0
        self._state = None
        self.update()

    def set_drain(self, t: float) -> None:
        self._drain = max(0.0, min(1.0, float(t)))
        self.update()

    def span(self) -> tuple[float, float]:
        return self._span

    def presence(self) -> float:
        return self._presence

    def set_presence(self, p: float) -> None:
        """0 = not there (no height, nothing drawn), 1 = a full line: the
        dots grow in and out with the line motion instead of shoving the
        list."""
        self._presence = max(0.0, min(1.0, float(p)))
        self._alpha = self._presence
        self._apply_height()
        self.update()

    def set_alpha(self, a: float) -> None:
        """Fade the dots without changing the height (standing in for a
        blank line that already held the space)."""
        self._alpha = max(0.0, min(1.0, float(a)))
        self.update()

    def alpha(self) -> float:
        return self._alpha

    def set_full_height(self, h: int | None) -> None:
        self._full_h = None if h is None else max(1, int(h))
        self._apply_height()

    def _apply_height(self) -> None:
        full = self._full_h if self._full_h is not None else QFontMetrics(self.font()).height() + 8
        self.setFixedHeight(max(0, round(full * self._presence)))

    def report_position(self, secs: float) -> None:
        self.clock.report(float(secs))
        self._sync_timer()
        self._tick()

    def progress(self) -> float:
        start, end = self._span
        return max(0.0, min(1.0, (self.clock.now() - start) / (end - start)))

    def _sync_timer(self) -> None:
        want = self.isVisible() and self.clock.running()
        if want and not self._timer.isActive():
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        p = self.progress()
        state = int(p * 3) if not self._modern else round(p * 90)
        if state != self._state:
            self._state = state
            self.update()
        if not self.clock.running():
            self._sync_timer()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._sync_timer()

    def hideEvent(self, ev) -> None:
        super().hideEvent(ev)
        self._timer.stop()

    def paintEvent(self, _ev) -> None:
        if self._presence <= 0.0 or self._alpha <= 0.0:
            return
        lit, dim = legibility.ink(self, self._lit), legibility.ink(self, self._dim)
        prog = self.progress()
        colors = []
        for k in range(3):
            share = max(0.0, min(1.0, prog * 3.0 - k))
            if not self._modern:
                share = 1.0 if share >= 1.0 else 0.0
            c = _mix(dim, lit, share * (1.0 - self._drain))
            c.setAlphaF(self._alpha)
            colors.append(c)
        d = _dot_size(QFontMetrics(self.font()).height())
        left = (self.width() - _dots_width(d)) / 2.0 if self._centered else self._left
        p = QPainter(self)
        _paint_dots(p, left=left, cy=self.height() / 2.0, d=d,
                    modern=self._modern, colors=colors)
        p.end()


# Toggles plugins put in the lyrics view (ctx.ui.add_lyrics_toggle; the
# instrumental finder's [mute lyrics]). One shared status line under them.
class LyricsToggle:
    """one plugin toggle, shown in every LyricsView that shows its chrome.
    ``fn(on, track)`` runs on a click with the view's current song."""

    def __init__(self, label: str, fn) -> None:
        self.label, self.fn = label, fn
        self.checked = False
        self.status = ""

    def set_checked(self, on: bool) -> None:
        self.checked = bool(on)
        toggles_signal.changed.emit()

    def set_status(self, text: str) -> None:
        self.status = str(text or "")
        toggles_signal.changed.emit()


class _Toggles(QObject):
    changed = Signal()


PLUGIN_TOGGLES: list[LyricsToggle] = []
toggles_signal = _Toggles()


def add_toggle(entry: LyricsToggle) -> None:
    PLUGIN_TOGGLES.append(entry)
    toggles_signal.changed.emit()


def remove_toggle(entry: LyricsToggle) -> None:
    if entry in PLUGIN_TOGGLES:
        PLUGIN_TOGGLES.remove(entry)
        toggles_signal.changed.emit()


class _LyricsWorker(QObject):
    done = Signal(str, object)        # video_id, LyricsResult | None
    failed = Signal(str, str)

    def __init__(self, api_obj: api.Api, track: api.Track, source: str | None = None,
                 force: bool = False) -> None:
        super().__init__()
        self.api = api_obj
        self.track = track
        self.source = source            # the song's pick, or None
        self.force = force              # try again: past the shared answer

    def run(self) -> None:
        from ..lyrics_provider import fetch_from
        try:
            self.done.emit(self.track.video_id,
                           fetch_from(self.api, self.track, self.source, force=self.force))
        except Exception as exc:
            self.failed.emit(self.track.video_id, str(exc))


class _RomanWorker(QObject):
    done = Signal(str, object)       # track key, [(t, original, roman)] | None

    def __init__(self, key: str, track) -> None:
        super().__init__()
        self.key, self.track = key, track

    def run(self) -> None:
        from ..lyrics_netease import fetch_roman
        t = self.track
        try:
            pairs = fetch_roman(title=getattr(t, "title", "") or "",
                                artist=getattr(t, "artists", "") or "",
                                duration_seconds=float(getattr(t, "duration_seconds", 0) or 0))
        except Exception:
            pairs = None
        self.done.emit(self.key, pairs)


class _LineLabel(legibility.InkLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setAlignment(Qt.AlignLeft)
        # Lyric text is remote (LRClib / YT Music). QLabel's default AutoText
        # would render a line like "<b><font size=40>…" as HTML; force
        # PlainText so lines always display literally. (The karaoke path
        # builds its own escaped rich text separately.)
        self.setTextFormat(Qt.PlainText)
        self._dots: bool | None = None       # None = words; else modern?
        self._roman: str | None = None       # romanized line under the words

    def set_centered(self, on: bool) -> None:
        # top: a romanized line takes the band under the words
        self.setAlignment((Qt.AlignHCenter if on else Qt.AlignLeft) | Qt.AlignTop)

    def set_roman(self, text: str | None) -> None:
        text = text or None
        if text != self._roman:
            self._roman = text
            self.updateGeometry()
            self.update()

    def _roman_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(1.0, f.pointSizeF() * 0.72))
        f.setBold(False)
        return f

    def _roman_h(self) -> int:
        return QFontMetrics(self._roman_font()).height() + 2 if self._roman else 0

    def heightForWidth(self, w: int) -> int:
        return super().heightForWidth(w) + self._roman_h()

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(sh.height() + self._roman_h())
        return sh

    def minimumSizeHint(self):
        sh = super().minimumSizeHint()
        sh.setHeight(sh.height() + self._roman_h())
        return sh

    def set_dots(self, modern: bool) -> None:
        """Draw this line (a ♪) as the break's three dots, in its own
        colour: what's coming, or what's been sung."""
        self._dots = bool(modern)
        if self.text() != " ":
            self.setText(" ")
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)            # the ink check, and the words
        if self._roman:
            self._paint_roman()
        if self._dots is None:
            return
        indent = self.indent()
        if indent < 0:
            indent = self.fontMetrics().horizontalAdvance("x") // 2 if self.frameWidth() > 0 else 0
        r = self.contentsRect()
        d = _dot_size(self.fontMetrics().height())
        left = r.left() + indent
        if self.alignment() & Qt.AlignHCenter:
            left = r.left() + (r.width() - _dots_width(d)) / 2.0
        color = self.palette().color(self.foregroundRole())
        p = QPainter(self)
        _paint_dots(p, left=left, cy=r.center().y() + 0.5, d=d,
                    modern=self._dots, colors=(color, color, color))
        p.end()

    def _paint_roman(self) -> None:
        r = self.contentsRect()
        font = self._roman_font()
        fm = QFontMetrics(font)
        h = self._roman_h()
        indent = self.indent()
        if indent < 0:
            indent = self.fontMetrics().horizontalAdvance("x") // 2 if self.frameWidth() > 0 else 0
        band = r.adjusted(indent, r.height() - h, 0, 0)
        color = QColor(self.palette().color(self.foregroundRole()))
        color.setAlphaF(color.alphaF() * 0.75)
        center = bool(self.alignment() & Qt.AlignHCenter)
        p = QPainter(self)
        p.setFont(font)
        p.setPen(color)
        p.drawText(band, int((Qt.AlignHCenter if center else Qt.AlignLeft) | Qt.AlignVCenter),
                   fm.elidedText(self._roman, Qt.ElideRight, band.width()))
        p.end()


def _set_line_sheet(lbl: QLabel, sheet: str) -> None:
    if getattr(lbl, "_line_sheet", None) != sheet:
        lbl._line_sheet = sheet
        lbl.setStyleSheet(sheet)


class _KaraokeWidget(QWidget):
    """Big-center karaoke rendering. Stack of three lines (prev, current,
    next): the current one big and centred, its words lit as they're
    sung by the SweepLine (ui/karaoke_line), the neighbours small + dim.

    Fed a word sheet (lyric_words.WordLine list): word timing from a
    source, the aligner, or — line mode — spread across the line's window
    by length. The sheet's own line starts pick the current line, so a
    re-timed sheet re-times the karaoke too."""

    def __init__(self, parent: QWidget | None = None, *,
                 font_scale: float = 1.0) -> None:
        super().__init__(parent)
        self._sheet: list = []
        self._starts: list[float] = []
        # Effective, not base: built mid-song (mini panel, fullscreen)
        # the accent must be the adaptive one already in force.
        self._theme = theming.manager().current_effective()
        self._font_scale = float(font_scale)
        self._active_idx: int = -1
        self._current_secs: float = 0.0

        # InkLabel: rich, selectable text paints past the style proxy.
        self.prev_label = legibility.InkLabel(" ")
        self.next_label = legibility.InkLabel(" ")
        self.current_line = SweepLine()

        for lbl in (self.prev_label, self.next_label):
            lbl.setWordWrap(True)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            # remote text: never rendered as markup
            lbl.setTextFormat(Qt.PlainText)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(18)
        col.addStretch(1)
        col.addWidget(self.prev_label)
        col.addWidget(self.current_line)
        col.addWidget(self.next_label)
        col.addStretch(1)

        self._apply_styles()

    def set_lines(self, lines: list[tuple[float, str]]) -> None:
        self.set_sheet(lyric_words.line_sheet(list(lines)))

    def set_sheet(self, sheet: list) -> None:
        self._sheet = list(sheet)
        self._starts = [l.start for l in self._sheet]
        self._active_idx = -2
        self.set_position(self._current_secs)

    def sheet(self) -> list:
        return list(self._sheet)

    def set_position(self, secs: float) -> None:
        self._current_secs = float(secs)
        idx = bisect_right(self._starts, self._current_secs) - 1
        if idx != self._active_idx:
            self._active_idx = idx
            self._render()
        self.current_line.report_position(self._current_secs)

    def set_active_index(self, idx: int, current_secs: float) -> None:
        """Kept for callers that pick the line themselves; the sheet's
        own timing wins."""
        self.set_position(current_secs)

    def set_theme(self, theme) -> None:
        self._theme = theme
        self._apply_styles()
        self._render()

    # ---------- internals ----------

    def _apply_styles(self) -> None:
        theme = self._theme
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        side_pt = type_pt("side", self._font_scale)
        cur_pt = type_pt("center", self._font_scale)
        self.prev_label.setStyleSheet(
            f"color: {dim}; background: transparent; font-size: {side_pt}pt; padding: 0;"
        )
        self.next_label.setStyleSheet(
            f"color: {dim}; background: transparent; font-size: {side_pt}pt; padding: 0;"
        )
        modern = getattr(theme, "aesthetic", "") == "modern"
        self.current_line.set_look(fg=QColor(fg), accent=QColor(accent),
                                   modern=modern, pt=cur_pt)
        self.current_line.setContentsMargins(0, 12, 0, 12)

    def _render(self) -> None:
        idx = self._active_idx
        n = len(self._sheet)
        prev_text = self._sheet[idx - 1].text if 0 < idx < n else ""
        next_text = self._sheet[idx + 1].text if 0 <= idx < n - 1 else ""
        if idx < 0 and n:
            # the intro: what's coming up sits underneath
            next_text = self._sheet[0].text
        self.prev_label.setText(prev_text or " ")
        self.next_label.setText(next_text or " ")
        self.current_line.set_line(self._sheet[idx] if 0 <= idx < n else None)


class LyricsView(QWidget):
    # The tab's [sweep words] checkbox: MainWindow persists it as
    # Settings.lyrics_list_sweep and applies it (set_list_sweep).
    list_sweep_toggled = Signal(bool)

    def __init__(self, api_obj: api.Api, parent: QWidget | None = None, *,
                 font_scale: float = 1.0, tab: bool = False) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._theme = theming.manager().current_effective()
        self._tones = line_tones(self._theme)
        # The main window's lyrics tab (not an embed): modern drops its
        # heading, the strip already says what's playing.
        self._tab = bool(tab)
        # Type multiplier for embeds that are read from farther away (the
        # fullscreen mode). 1.0 = the classic panel sizes, unchanged.
        self._font_scale = float(font_scale)
        theming.manager().theme_changed.connect(self._on_theme)
        _views.add(self)

        self._current_video_id: str | None = None
        self._current_track = None
        self._current_key: str | None = None
        self._result = None
        # Position as reported, before the lyrics offset.
        self._raw_position: float = 0.0
        lyric_timing.service().updated.connect(self._on_sheet)
        self._thread: QThread | None = None
        self._worker: _LyricsWorker | None = None

        # Timed state
        self._timed_lines: list[tuple[float, str]] = []
        self._timed_starts: list[float] = []
        self._line_widgets: list[_LineLabel] = []
        self._active_line_index: int = -1
        self._lines_behind: bool = False
        self._karaoke_mode: bool = False
        # Line-advance motion (glide scroll + accent fade-in). One of
        # each at most; stopped whenever the line list is rebuilt.
        self._scroll_anim: QVariantAnimation | None = None
        self._active_anim: QVariantAnimation | None = None
        # Modern's line advance: one animation grows the new line, settles
        # the old one and glides the scroll. {"anim", "end"} while it runs.
        self._line_motion: dict | None = None
        # Last known playback position, used so karaoke can re-render
        # smoothly when the user toggles in mid-line.
        self._last_position: float = 0.0
        # Which empty state is up ("no_track", "loading", "missing",
        # "failed"), None while lyrics show.
        self._status: str | None = None
        self._loading_for: str | None = None
        self._force_fetch = False
        # Widgets wearing an _EdgeFade right now.
        self._faded: list[QWidget] = []
        self._fades_queued = False
        # The line drawn as sung: the active line, or -1 once its singing
        # is over (a break) or on a blank line. And the break the dots are
        # up for, as (next sung line, from, to), or None.
        self._lit_index: int = -1
        self._gap: tuple | None = None
        # Per line: when its singing stops (None = unknown), blank or not,
        # and the next line with words (index i + 1; [0] is for the intro).
        self._ends: list = []
        self._blank: list[bool] = []
        self._note: list[bool] = []
        # NetEase's romanized line for each line (None where there's none),
        # and the song they're for.
        self._roman: list = []
        self._roman_key: str | None = None
        self._next_sung: list = []
        # The tab's layout in force (layout_for), and whether the lines
        # are centred (the center layout).
        self._mode = "list"
        self._centered = False
        # False after a scroll of your own: the list stays where you put it.
        self._following = True
        self._press: tuple | None = None
        self._hover_obj: QWidget | None = None

        self.heading = Heading("lyrics")
        self.heading.setProperty("class", "dim")
        # Heading embeds the remote track title (see show_for); plain-text it.
        self.heading.setTextFormat(Qt.PlainText)

        # Toggles bar — karaoke mode, the sweep, then whatever plugins add
        # (the instrumental finder's [mute lyrics]). Lives in this view per
        # the spec, NOT on the main playback control bar.
        self.karaoke_check = QCheckBox(theming.styled_case("karaoke mode"))
        self.karaoke_check.toggled.connect(self._on_karaoke_toggled)
        # Word-by-word sweep in the live list (karaoke always sweeps).
        # Part of the tab's chrome: it follows karaoke_check's visibility,
        # so embeds that hide the chrome (mini, fullscreen, the visualizer
        # tile) hide this too.
        self.sweep_check = QCheckBox(theming.styled_case("sweep words"))
        self.sweep_check.setChecked(_list_sweep_on)
        self.sweep_check.setToolTip(theming.styled_case(
            "light the line being sung word by word. off lights the whole line."))
        self.sweep_check.toggled.connect(self.list_sweep_toggled.emit)
        # plugins' toggles (rebuilt as plugins come and go) and their status
        self.plugin_row = QWidget()
        self._plugin_layout = QHBoxLayout(self.plugin_row)
        self._plugin_layout.setContentsMargins(0, 0, 0, 0)
        self._plugin_layout.setSpacing(12)
        self._plugin_btns: list[tuple[LyricsToggle, QPushButton]] = []
        self.plugin_status = QLabel("")
        self.plugin_status.setProperty("class", "dim")
        self.plugin_status.setWordWrap(True)
        self.plugin_status.setTextFormat(Qt.PlainText)
        toggles_signal.changed.connect(self._sync_plugin_toggles)

        toggles_row = QHBoxLayout()
        toggles_row.setContentsMargins(0, 0, 0, 0)
        toggles_row.setSpacing(12)
        # the word toggles, as one row a plugin shows (set_word_view)
        self.word_toggles = QWidget()
        word_row = QHBoxLayout(self.word_toggles)
        word_row.setContentsMargins(0, 0, 0, 0)
        word_row.setSpacing(12)
        word_row.addWidget(self.karaoke_check)
        word_row.addWidget(self.sweep_check)
        self.word_toggles.setVisible(word_view())
        toggles_row.addWidget(self.word_toggles)
        toggles_row.addWidget(self.plugin_row)
        toggles_row.addStretch(1)

        # Container we swap between the plain QLabel and the timed line list.
        self._plain_label = legibility.InkLabel("── no track ──")
        self._plain_label.setWordWrap(True)
        self._plain_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._plain_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._plain_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._plain_label.setContentsMargins(0, 6, 0, 6)
        # Untimed lyrics are a remote text block — plain-text it so markup in
        # the lyrics can't render as HTML.
        self._plain_label.setTextFormat(Qt.PlainText)
        self._plain_label.setContextMenuPolicy(Qt.NoContextMenu)

        self._timed_host = QWidget()
        self._timed_layout = QVBoxLayout(self._timed_host)
        self._timed_layout.setContentsMargins(0, 6, 0, 6)
        self._timed_layout.setSpacing(4)
        self._timed_layout.addStretch(1)
        # The list's active line, sung word by word: this sweep line takes
        # the active label's place in the list (the label hides) and moves
        # down as the song does. Only for lines with words to sweep.
        self._sheet: list = []
        self._list_sweep = SweepLine(self._timed_host)
        self._list_sweep.set_left_aligned(True)
        self._list_sweep.setContentsMargins(0, 4, 0, 4)
        self._list_sweep.setCursor(Qt.PointingHandCursor)
        self._list_sweep.setContextMenuPolicy(Qt.NoContextMenu)
        self._list_sweep.installEventFilter(self)
        self._list_sweep.hide()
        self._sweep_at = -1
        # The break's dots, in the list just above the next sung line (a
        # blank line right there steps aside for them).
        self._marker = _GapMarker(self._timed_host)
        self._marker.setContextMenuPolicy(Qt.NoContextMenu)
        self._marker.installEventFilter(self)
        self._marker.hide()
        self._marker_at = -1
        self._marker_hid: list[int] = []
        self._marker_kind = ""
        # Under the hovered line. Not in the layout: placed by hand.
        self._plate = _HoverPlate(self._timed_host)
        self._plate.hide()
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_LINGER_MS)
        self._hover_timer.timeout.connect(self._hover_gone)

        self._karaoke_widget = _KaraokeWidget(font_scale=self._font_scale)
        # Named so the adaptive-background QSS transparentizes this container
        # (a QWidget subclass, so it isn't caught by the `.QWidget` rule) and
        # the gradient shows behind the karaoke lines. See theming._CONTENT_BACKDROP_QSS.
        self._karaoke_widget.setObjectName("lyricsKaraoke")
        for lbl in (self._karaoke_widget.prev_label, self._karaoke_widget.next_label):
            lbl.setContextMenuPolicy(Qt.NoContextMenu)

        # Modern's empty states: one dim line in the middle, and a retry
        # when the lyrics didn't come. A plain QWidget so the backdrop
        # QSS (the `.QWidget` rule) keeps it see-through.
        self._empty_host = QWidget()
        empty_col = QVBoxLayout(self._empty_host)
        empty_col.setContentsMargins(0, 0, 0, 0)
        empty_col.setSpacing(_scale.px(12))
        self._empty_msg = legibility.InkLabel("")
        self._empty_msg.setProperty("class", "dim")
        self._empty_msg.setAlignment(Qt.AlignCenter)
        self._empty_msg.setWordWrap(True)
        self._empty_msg.setTextFormat(Qt.PlainText)
        self._empty_btn = BracketButton("try again")
        self._empty_btn.setRole("pill")
        self._empty_btn.clicked.connect(self._retry)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._empty_btn)
        btn_row.addStretch(1)
        empty_col.addStretch(1)
        empty_col.addWidget(self._empty_msg)
        empty_col.addLayout(btn_row)
        empty_col.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setWidget(self._plain_label)
        bar = self._scroll.verticalScrollBar()
        bar.valueChanged.connect(self._queue_fades)
        bar.rangeChanged.connect(self._queue_fades)
        # Quiet scrollbar (modern): see-through until you scroll or reach
        # for it. It keeps its width, so nothing reflows when it shows.
        self._bar_fx: QGraphicsOpacityEffect | None = None
        self._bar_timer = QTimer(self)
        self._bar_timer.setSingleShot(True)
        self._bar_timer.setInterval(BAR_LINGER_MS)
        self._bar_timer.timeout.connect(self._bar_idle)
        self._scroll.viewport().installEventFilter(self)
        bar.installEventFilter(self)
        bar.sliderPressed.connect(self._poke_bar)
        # Wheel, keys, a drag or a click on the bar; never our own glide.
        bar.actionTriggered.connect(self._on_user_scroll)
        self._follow_timer = QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(FOLLOW_IDLE_MS)
        self._follow_timer.timeout.connect(self._resume_follow)
        # Floats over the list while it isn't following the song.
        self._back_btn = _BackPill(self)
        self._back_btn.clicked.connect(self._resume_follow)
        self._back_btn.hide()
        # A word after a key does something you can't see (the timing
        # nudge, a copy): same face as the pill, ignores the mouse.
        self._toast = _BackPill(self)
        self._toast.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(TOAST_MS)
        self._toast_timer.timeout.connect(self._toast.hide)
        # The surface around an embed adds its own items under ours.
        self._menu_extra = None

        # The beside layout's column (the tab only): the cover, the title
        # and the artist, left of the lines.
        self._side: QWidget | None = None
        self._art_url: str | None = None
        if self._tab:
            self._build_side()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        layout.setSpacing(_scale.px(8))
        layout.addWidget(self.heading)
        layout.addLayout(toggles_row)
        layout.addWidget(self.plugin_status)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(_scale.px(28))
        if self._side is not None:
            body.addWidget(self._side)
        body.addWidget(self._scroll, stretch=1)
        layout.addLayout(body, stretch=1)
        self._sync_plugin_toggles()
        self.refresh_type()
        self._sync_heading()
        self._sync_quiet_bar()
        self._sync_back_btn()
        self.refresh_layout()
        self._show_status("no_track")

    def _build_side(self) -> None:
        from .widgets import AlbumArt
        side = QWidget()
        col = QVBoxLayout(side)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(_scale.px(10))
        self._side_art = AlbumArt(280)
        self._side_art.set_framed(False)
        self._side_title = legibility.InkLabel("")
        self._side_title.setProperty("class", "title")
        self._side_artist = legibility.InkLabel("")
        self._side_artist.setProperty("class", "dim")
        for lbl in (self._side_title, self._side_artist):
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.PlainText)     # remote text
        col.addStretch(1)
        col.addWidget(self._side_art, alignment=Qt.AlignLeft)
        col.addWidget(self._side_title)
        col.addWidget(self._side_artist)
        col.addStretch(1)
        # never the reason the view is tall: the lines set the height
        side.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Ignored)
        side.hide()
        self._side = side

    def _side_track(self, track) -> None:
        """Fill the beside column for ``track`` (None: empty it)."""
        if self._side is None:
            return
        case = theming.styled_case
        if track is None:
            self._art_url = None
            self._side_title.setText(case("nothing playing"))
            self._side_artist.setText("")
            self._side_art.setImage(None)
            return
        self._side_title.setText(case(track.title or ""))
        self._side_artist.setText(case(getattr(track, "artists", "") or ""))
        url = getattr(track, "thumbnail", "") or ""
        self._art_url = url or None
        if not url:
            self._side_art.setImage(None)
            return
        from . import art_cache
        img = art_cache.cache().request(url, lambda image, url=url: self._side_art_ready(url, image))
        if img is not None:
            self._side_art_ready(url, img)

    def _side_art_ready(self, url: str, image) -> None:
        try:
            if url == self._art_url:
                self._side_art.setImage(image)
        except RuntimeError:
            pass        # the view is gone

    def refresh_layout(self) -> None:
        """Apply the layout in force: the setting's, else the theme's
        [slots] lyrics (the tab only; embeds keep the list)."""
        mode = layout_for(self._theme) if self._tab else "list"
        self._mode = mode
        center = mode == "center"
        if center != self._centered:
            self._centered = center
            for lbl in self._line_widgets:
                lbl.set_centered(center)
            self._list_sweep.set_left_aligned(not center)
            self._list_sweep.set_snug(center)
            self._marker.set_centered(center)
            self._unplace_sweep()
            self._place_sweep()
            self._list_sweep.report_position(self._last_position)
        self._sync_side()
        self._queue_fades()

    def _sync_side(self) -> None:
        side = self._side
        if side is None:
            return
        want = self._mode == "beside" and self.width() >= _scale.px(BESIDE_MIN_W)
        if want:
            w = max(_scale.px(200), min(_scale.px(340), int(self.width() * 0.32)))
            side.setFixedWidth(w)
            # the cover fits the column and leaves the window's height room
            # for the title and artist under it
            art = min(w, int(self.height() * 0.55))
            self._side_art.set_base_size(max(96, int(art / max(0.5, _scale.factor()))))
        if want != side.isVisible():
            side.setVisible(want)

    def _sync_plugin_toggles(self) -> None:
        """match the plugin row to PLUGIN_TOGGLES: buttons, checks, status."""
        try:
            have = [e for e, _b in self._plugin_btns]
        except RuntimeError:
            return              # the view is going
        if have != PLUGIN_TOGGLES:
            for _e, btn in self._plugin_btns:
                self._plugin_layout.removeWidget(btn)
                btn.deleteLater()
            self._plugin_btns = []
            for entry in PLUGIN_TOGGLES:
                btn = QPushButton(theming.styled_case(f"[{entry.label}]"))
                btn.setCheckable(True)
                btn.clicked.connect(lambda on, e=entry: self._on_plugin_toggle(e, on))
                self._plugin_layout.addWidget(btn)
                self._plugin_btns.append((entry, btn))
        for entry, btn in self._plugin_btns:
            btn.blockSignals(True)
            btn.setChecked(entry.checked)
            btn.blockSignals(False)
            btn.setEnabled(self._current_track is not None if hasattr(self, "_current_track")
                           else False)
        self.plugin_status.setText(theming.styled_case(
            next((e.status for e in PLUGIN_TOGGLES if e.status), "")))

    def _on_plugin_toggle(self, entry: LyricsToggle, on: bool) -> None:
        entry.checked = bool(on)
        from ..plugin_api import track_info
        try:
            entry.fn(bool(on), track_info(self._current_track))
        finally:
            toggles_signal.changed.emit()

    # ---------- public ----------

    def show_for(self, track) -> None:
        if track is None:
            self._current_video_id = None
            self._current_track = None
            self._current_key = None
            self._result = None
            self.heading.set_label("lyrics")
            self._side_track(None)
            self._show_status("no_track")
            self._clear_timed()
            self._sync_plugin_toggles()
            return
        if self._current_video_id == track.video_id:
            return
        self._current_video_id = track.video_id
        self._current_track = track
        self._current_key = lyric_timing.track_key(track)
        self._result = None
        self.heading.set_label(f"lyrics · {(track.title or '').lower()}")
        self._side_track(track)
        self._show_status("loading")
        self._clear_timed()
        self._set_following(True)         # a new song starts at its top
        song_changed(track)               # its own timing nudge
        # plugin toggles work once there's a song
        self._sync_plugin_toggles()

        thread = QThread()
        force, self._force_fetch = self._force_fetch, False
        worker = _LyricsWorker(self.api, track, lyrics_picks.source(self._current_key), force)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _shift(self) -> float:
        """Lyric time minus song time: the setting, plus this song's own
        nudge when it's the song this view shows."""
        mine = self._current_key is not None and self._current_key == _song_key
        return _offset_s + (_song_offset_s if mine else 0.0)

    def update_position(self, seconds: float) -> None:
        self._raw_position = float(seconds)
        seconds = float(seconds) + self._shift()
        self._last_position = seconds
        if not self._timed_lines:
            return
        # Latest line whose timestamp <= seconds.
        idx = bisect_right(self._timed_starts, seconds) - 1
        # Karaoke keeps its own clock between reports; every one re-syncs it.
        if self._karaoke_mode:
            self._karaoke_widget.set_position(seconds)
        elif self._sweep_at >= 0:
            self._list_sweep.report_position(seconds)
        lit, gap = self._visual(idx, seconds)
        if gap is not None and self._gap is not None and gap[0] == self._gap[0]:
            gap = self._gap         # the same break: its dots keep their start
        if self._marker_at >= 0:
            self._marker.report_position(seconds)
        if (idx == self._active_line_index and lit == self._lit_index
                and _gap_key(gap) == _gap_key(self._gap)):
            return
        prev_idx, prev_lit, prev_gap = self._active_line_index, self._lit_index, self._gap
        self._active_line_index, self._lit_index, self._gap = idx, lit, gap
        if self._karaoke_mode:
            return
        if (self._modern() and self.isVisible()
                and motion_module.intensity() != motion_module.Intensity.OFF):
            self._advance_modern(prev_idx, prev_lit, prev_gap, seconds)
            return
        self._restyle_lines()
        if _gap_key(prev_gap) != _gap_key(self._gap):
            self._place_marker(self._gap, 1.0)
        self._place_sweep()
        if self._sweep_at >= 0:
            self._list_sweep.report_position(seconds)
        if not self.isVisible():
            # Nobody is looking: skip the colour fade (a stylesheet per
            # frame) and the scroll glide; showEvent parks the list.
            self._lines_behind = True
            return
        if lit != prev_lit:
            self._animate_active_line()
        if self._following:
            self._glide_to_active()

    def _visual(self, idx: int, secs: float) -> tuple[int, tuple | None]:
        """(the line drawn as sung, the break the dots are up for) at
        ``secs``. A line is sung until its singing ends (when that's
        known) and the next line is a while off; a break long enough
        gets dots, from where it starts to the next line with words."""
        starts, n = self._timed_starts, len(self._timed_starts)
        if not n or len(self._next_sung) != n + 1:
            return idx, None
        if idx >= 0 and not self._blank[idx]:
            end = self._ends[idx] if idx < len(self._ends) else None
            if end is None or secs < end + TAIL_GRACE_S:
                return idx, None
            j = self._next_sung[idx + 1]
            if j is None:
                return -1, None                  # the last line, sung
            if starts[j] - end < FADE_MIN_S:
                return idx, None                 # the next one is right there
            begin = end + TAIL_GRACE_S
            return -1, ((j, begin, starts[j]) if starts[j] - begin >= GAP_MIN_S else None)
        # the intro, or a blank line
        j = self._next_sung[idx + 1]
        if j is None:
            return -1, None
        if idx < 0:
            begin = 0.0
        else:
            k = idx
            while k > 0 and self._blank[k - 1]:
                k -= 1
            begin = starts[k]
        return -1, ((j, begin, starts[j]) if starts[j] - begin >= GAP_MIN_S else None)

    def _resync(self) -> None:
        """Forget the drawn state and redraw it from the playhead (a new
        sheet, back from karaoke)."""
        self._unplace_marker()
        self._gap = None
        self._active_line_index = -2
        self._lit_index = -2
        self.update_position(self._raw_position)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.sweep_check.setVisible(not self.karaoke_check.isHidden())
        if not self._following:
            # back on the view: it follows the song again
            self._set_following(True)
            self._lines_behind = True
        self._place_back_btn()
        if self._lines_behind:
            self._lines_behind = False
            if not self._karaoke_mode and self._line_widgets:
                self._glide_to_active()

    # ---------- async result handling ----------

    def _on_done(self, video_id: str, result) -> None:
        if video_id != self._current_video_id:
            return
        from ..lyrics_provider import LyricsResult
        if not isinstance(result, LyricsResult):
            picked = lyrics_picks.source(self._current_key)
            self._show_status("missing", f"{picked} has no lyrics for this song" if picked else None)
            return
        self._result = result
        # Word timing: the best sheet known now; the service upgrades it
        # in the background (_on_sheet).
        sheet = lyric_timing.service().sheet_for(self._current_track, result)
        if sheet:
            self._show_sheet(sheet)
        elif result.plain_text:
            self._show_plain(result.plain_text)
        else:
            self._show_status("missing")

    def refresh_timing(self) -> None:
        if self._result is None or self._current_track is None:
            return
        sheet = lyric_timing.service().sheet_for(self._current_track, self._result)
        if sheet:
            self._show_sheet(sheet)
        self._karaoke_widget.current_line.update()

    def _on_sheet(self, key: str, sheet) -> None:
        if key != self._current_key or self._current_key is None or not sheet:
            return
        self._show_sheet(sheet)

    def _on_failed(self, video_id: str, _msg: str) -> None:
        if video_id != self._current_video_id:
            return
        self._show_status("failed")

    def _retry(self) -> None:
        """the empty state's [try again]: fetch the song's lyrics again."""
        track = self._current_track
        if track is None:
            return
        self._current_video_id = None
        self._force_fetch = True
        self.show_for(track)

    # ---------- render modes ----------

    def _show_sheet(self, sheet) -> None:
        """Show a word sheet. A sheet with the same lines as the one up
        (a timing upgrade) only re-times: the line widgets stay."""
        lines = [(l.start, l.text) for l in sheet]
        self._sheet = list(sheet)
        same = (len(lines) == len(self._timed_lines) and self._line_widgets
                and all(a[1] == b[1] for a, b in zip(lines, self._timed_lines)))
        if same:
            self._timed_lines = lines
            self._timed_starts = [t for t, _l in lines]
            self._karaoke_widget.set_sheet(sheet)
            self._unplace_sweep()       # re-placed below with the new words
        else:
            self._show_timed(lines)
            self._karaoke_widget.set_sheet(sheet)
        self._ends = real_line_ends(sheet, self._result)
        tag = TIMING_TAGS[timing_of(sheet)]
        self.karaoke_check.setText(theming.styled_case(f"karaoke mode · {tag}"))
        self._resync()
        self._load_roman(fresh=not same)

    # ---------- romanized lines ----------

    def _load_roman(self, fresh: bool) -> None:
        """Ask NetEase for the song's romanized lines, once per song and
        only for lyrics in another script. ``fresh``: new line widgets."""
        key = self._current_key
        if not self._timed_lines or key is None:
            return
        if key == self._roman_key and not fresh:
            return              # a re-time: same lines, same romanized ones
        # new lines (a new song, or another source's): match them afresh;
        # NetEase's answer is on disk after the first ask
        self._roman_key = key
        self._roman = []
        try:
            allowed = lyric_timing.service().netease_allowed()
        except Exception:
            allowed = False
        if not allowed or not wants_roman(t for _s, t in self._timed_lines):
            return
        thread = QThread()
        worker = _RomanWorker(key, self._current_track)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_roman)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    def _on_roman(self, key: str, pairs) -> None:
        """Line NetEase's originals up with the lines shown (by text, in
        order; lyric_words.match_offset) and take each one's romanized
        twin by its timestamp."""
        if key != self._current_key or not pairs or not self._timed_lines:
            return
        base = lyric_words.line_sheet(list(self._timed_lines))
        theirs = lyric_words.line_sheet([(t, orig) for t, orig, _r in pairs])
        matched = lyric_words.match_offset(base, theirs)
        if matched is None:
            return              # another version of the song, likely
        _off, pairing = matched
        self._roman = [pairs[pairing[i]][2] if i in pairing else None
                       for i in range(len(self._timed_lines))]
        self._apply_roman()

    def _apply_roman(self) -> None:
        on = _roman_on
        for i, lbl in enumerate(self._line_widgets):
            text = self._roman[i] if on and i < len(self._roman) else None
            lbl.set_roman(None if (i < len(self._blank) and self._blank[i]) else text)
        self._unplace_sweep()
        self._place_sweep()
        self._list_sweep.report_position(self._last_position)
        self._queue_fades()

    def has_roman(self) -> bool:
        return any(self._roman)

    def _drop_sheet(self) -> None:
        self._sheet = []
        self._unplace_sweep()
        self._unplace_marker()
        self._timed_lines = []
        self._timed_starts = []
        self._ends, self._blank, self._note, self._next_sung = [], [], [], []
        self.karaoke_check.setText(theming.styled_case("karaoke mode"))
        self._active_line_index = -1
        self._lit_index = -1
        self._gap = None
        self._set_hover(None)

    def _set_scroll_widget(self, widget: QWidget) -> None:
        self._scroll.takeWidget()
        self._scroll.setWidget(widget)
        self._queue_fades()

    def _show_plain(self, text: str) -> None:
        self._drop_sheet()
        self._status = None
        self._plain_label.setText(text)
        self._set_scroll_widget(self._plain_label)

    def _show_status(self, kind: str, text: str | None = None) -> None:
        """An empty state: brutalist's 1.x line in the plain label, or
        modern's centred line (with [try again] where it could help).
        ``text`` says it differently (a picked source that had nothing)."""
        if not self._modern():
            self._show_plain(f"── {text} ──" if text else STATUS_PLAIN[kind])
            self._status = kind
            return
        self._drop_sheet()
        self._status = kind
        self._empty_btn.setVisible(kind in STATUS_RETRY and self._current_track is not None)
        if kind == "loading":
            # Most sheets are cached and land before anyone could read
            # "loading"; say it only once the wait is real.
            self._empty_msg.setText("")
            self._loading_for = self._current_video_id
            QTimer.singleShot(LOADING_DELAY_MS, self._say_loading)
        else:
            self._empty_msg.setText(theming.styled_case(text or STATUS_MODERN[kind]))
        self._set_scroll_widget(self._empty_host)

    def _say_loading(self) -> None:
        try:
            if self._status == "loading" and self._loading_for == self._current_video_id:
                self._empty_msg.setText(theming.styled_case(STATUS_MODERN["loading"]))
        except RuntimeError:
            pass        # the view is gone

    def _show_timed(self, lines: list[tuple[float, str]]) -> None:
        self._status = None
        self._timed_lines = lines
        self._timed_starts = [t for t, _l in lines]
        self._active_line_index = -1
        self._lit_index = -1
        self._gap = None
        self._clear_timed()
        self._blank = [_is_blank(text) for _t, text in lines]
        self._note = [_is_note(text) for _t, text in lines]
        self._ends = [None] * len(lines)       # _show_sheet knows better
        nxt, upcoming = [None] * (len(lines) + 1), None
        for i in range(len(lines) - 1, -2, -1):
            nxt[i + 1] = upcoming
            if i >= 0 and not self._blank[i]:
                upcoming = i
        self._next_sung = nxt
        for i, (_t, text) in enumerate(lines):
            lbl = _LineLabel(text or " ", self._timed_host)
            lbl.set_centered(self._centered)
            if self._note[i]:
                lbl.set_dots(self._modern())
            lbl._index = i
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.setContextMenuPolicy(Qt.NoContextMenu)     # the view's menu
            lbl.installEventFilter(self)
            self._line_widgets.append(lbl)
            self._timed_layout.insertWidget(self._timed_layout.count() - 1, lbl)
        self._restyle_lines()
        # Keep both renderings ready; show the one matching the toggle.
        self._karaoke_widget.set_lines(lines)
        if self._karaoke_mode:
            self._set_scroll_widget(self._karaoke_widget)
        else:
            self._set_scroll_widget(self._timed_host)

    def _clear_timed(self) -> None:
        self._stop_line_motion()
        self._unplace_sweep()
        self._unplace_marker()
        self._set_hover(None)
        for w in self._line_widgets:
            w.deleteLater()
        self._line_widgets = []
        # the labels are going; only the kept widgets stay on the list
        self._faded = [w for w in self._faded
                       if w is self._list_sweep or w is self._plain_label]

    def _unplace_sweep(self) -> None:
        at = self._sweep_at
        self._sweep_at = -1
        self._timed_layout.removeWidget(self._list_sweep)
        self._list_sweep.hide()
        self._list_sweep.set_line(None)
        if 0 <= at < len(self._line_widgets):
            try:
                self._line_widgets[at].show()
            except RuntimeError:
                pass

    def _place_sweep(self) -> None:
        """Put the sweep line where the sung label is (and the label back
        where the sweep was)."""
        idx = self._lit_index
        line = self._sheet[idx] if 0 <= idx < len(self._sheet) else None
        shows_roman = _roman_on and 0 <= idx < len(self._roman) and bool(self._roman[idx])
        ok = (_list_sweep_on and word_view() and line is not None and bool(line.words)
              and not shows_roman
              and idx < len(self._line_widgets)
              and line.text == self._timed_lines[idx][1] and bool(line.text.strip()))
        if self._sweep_at == idx and ok:
            self._list_sweep.set_line(line)
            return
        self._unplace_sweep()
        if not ok:
            return
        self._apply_sweep_look()
        # start the text where the labels start theirs: QLabel indents by
        # half an "x" when it has a frame (and the list labels do)
        lbl = self._line_widgets[idx]
        indent = lbl.indent()
        if indent < 0:
            indent = lbl.fontMetrics().horizontalAdvance("x") // 2 if lbl.frameWidth() > 0 else 0
        left = lbl.contentsRect().left() + indent
        self._list_sweep.setContentsMargins(left, 4, left if self._centered else 0, 4)
        self._list_sweep.set_line(line)
        self._timed_layout.insertWidget(self._timed_layout.indexOf(lbl), self._list_sweep)
        self._line_widgets[idx].hide()
        self._list_sweep.show()
        self._sweep_at = idx

    def _text_left(self) -> int:
        """Where the list's text starts, in a line's own coordinates."""
        if not self._line_widgets:
            return _scale.px(LINE_HPAD) if self._modern() else 0
        lbl = self._line_widgets[0]
        indent = lbl.indent()
        if indent < 0:
            indent = lbl.fontMetrics().horizontalAdvance("x") // 2 if lbl.frameWidth() > 0 else 0
        return lbl.contentsRect().left() + indent

    def _apply_marker_look(self) -> None:
        theme = self._theme
        if self._modern():
            lit, dim = self._tones[0], self._tones[2]
        else:
            lit = QColor(theme.token("accent", "#d4b95e") if theme else "#d4b95e")
            dim = QColor(theme.token("dim", "#6f6f6f") if theme else "#6f6f6f")
        self._marker.set_look(lit=lit, dim=dim, modern=self._modern(),
                              pt=type_pt("line", self._font_scale), left=self._text_left())

    def _place_marker(self, gap, presence: float) -> str:
        """Put the dots in for ``gap`` (none for None), just above the line
        it leads to. Blank lines right above step aside and the dots take
        exactly their room. Returns what they stand in for: "note" (a ♪
        line, already drawn as dots), "blank", or "" (new room)."""
        self._unplace_marker()
        if gap is None or self._karaoke_mode:
            return ""
        j, begin, end = gap
        if not (0 <= j < len(self._line_widgets)):
            return ""
        self._apply_marker_look()
        self._marker.set_span(begin, end)
        run, k = [], j - 1
        while k >= 0 and self._blank[k] and k != self._sweep_at:
            run.append(k)
            k -= 1
        kind = ""
        if run:
            spacing = self._timed_layout.spacing()
            room = sum(self._line_widgets[i].height() for i in run) + spacing * (len(run) - 1)
            self._marker.set_full_height(room)
            kind = "note" if any(self._note[i] for i in run) else "blank"
            for i in run:
                self._line_widgets[i].hide()
            self._marker_hid = run
            presence = 1.0
        else:
            self._marker.set_full_height(None)
        self._marker_kind = kind
        self._marker.set_presence(presence)
        self._marker.report_position(self._last_position)
        self._timed_layout.insertWidget(
            self._timed_layout.indexOf(self._line_widgets[j]), self._marker)
        self._marker.show()
        self._marker_at = j
        return kind

    def _unplace_marker(self) -> None:
        if self._marker_at < 0:
            return
        self._marker_at = -1
        self._marker_kind = ""
        self._timed_layout.removeWidget(self._marker)
        self._marker.hide()
        hid, self._marker_hid = self._marker_hid, []
        for i in hid:
            if 0 <= i < len(self._line_widgets):
                try:
                    self._line_widgets[i].show()
                except RuntimeError:
                    pass

    def _apply_sweep_look(self) -> None:
        theme = self._theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        accent = QColor(theme.token("accent", "#d4b95e") if theme else "#d4b95e")
        if self._modern():
            accent = self._tones[0]
        self._list_sweep.set_look(
            fg=QColor(fg), accent=accent,
            modern=getattr(theme, "aesthetic", "") == "modern",
            pt=type_pt("active", self._font_scale))

    def _stop_line_motion(self) -> None:
        self._finish_line_motion()
        for anim in (self._scroll_anim, self._active_anim):
            if anim is not None:
                anim.stop()
        self._scroll_anim = None
        self._active_anim = None

    # ---------- modern line looks ----------

    def _modern(self) -> bool:
        return getattr(self._theme, "aesthetic", "") == "modern"

    def _base_weight(self) -> int:
        try:
            w = int(self._theme.t("typography", "weight", 400)) if self._theme else 400
        except (TypeError, ValueError):
            w = 400
        return max(100, min(900, int(round(w / 100.0)) * 100))

    def _steady_look(self, i: int):
        """Where line ``i`` rests for the playhead now."""
        sung, coming, past = self._tones
        if i == self._lit_index:
            return (float(type_pt("active", self._font_scale)), sung, 4.0, 700)
        rest = float(type_pt("line", self._font_scale))
        done = i <= self._active_line_index
        return (rest, past if done else coming, 2.0, self._base_weight())

    def _set_look(self, lbl: QLabel, look) -> None:
        lbl._look = look
        _set_line_sheet(lbl, _line_css(look))

    def _finish_line_motion(self) -> None:
        motion = self._line_motion
        self._line_motion = None
        if motion is None:
            return
        motion["anim"].stop()
        motion["end"]()

    def _glide_target(self, *, skip_marker: bool = False) -> QWidget | None:
        """What the glide parks: the break's dots, else the sung line,
        else the line the playhead is on (a blank one, or one already
        sung)."""
        if self._marker_at >= 0 and not skip_marker:
            return self._marker
        i = self._lit_index if self._lit_index >= 0 else self._active_line_index
        if not (0 <= i < len(self._line_widgets)):
            return None
        return self._list_sweep if i == self._sweep_at else self._line_widgets[i]

    def _advance_modern(self, prev_idx: int, prev_lit: int, prev_gap,
                        seconds: float) -> None:
        """Modern's change of state, as one motion: the new sung line
        grows into place while the old one settles, the break's dots grow
        in or out, and the scroll glides along with them, re-aimed every
        frame at where its target is now, so it lands exactly where the
        grown line ends up."""
        self._finish_line_motion()
        for anim in (self._scroll_anim, self._active_anim):
            if anim is not None:
                anim.stop()
        self._scroll_anim = self._active_anim = None
        n = len(self._line_widgets)
        lit = self._lit_index
        movers: dict[int, tuple] = {}
        for i in (prev_lit, lit, prev_idx, self._active_line_index):
            if 0 <= i < n and i not in movers:
                was = getattr(self._line_widgets[i], "_look", None) or self._steady_look(i)
                will = self._steady_look(i)
                if was != will:
                    movers[i] = (was, will)
        for i, lbl in enumerate(self._line_widgets):
            if i not in movers:
                self._set_look(lbl, self._steady_look(i))
        # the dots: grow in for a new break, shrink away once it's over
        dots = None             # (setter, from, to)
        leaving = False
        if _gap_key(prev_gap) != _gap_key(self._gap):
            m = self._marker
            if self._marker_at >= 0 and self._gap is None:
                leaving = True
                if self._marker_kind == "note":
                    dots = (m.set_drain, 0.0, 1.0)      # back to the ♪ line's dots
                elif self._marker_kind == "blank":
                    dots = (m.set_alpha, m.alpha(), 0.0)
                else:
                    dots = (m.set_presence, m.presence(), 0.0)
            else:
                kind = self._place_marker(self._gap, 0.0)
                if self._marker_at >= 0:
                    if kind == "blank":
                        m.set_alpha(0.0)
                        dots = (m.set_alpha, 0.0, 1.0)
                    elif kind == "":
                        dots = (m.set_presence, 0.0, 1.0)
        self._place_sweep()
        sweep = None
        if lit in movers and lit == self._sweep_at:
            # the sweep line stands in for the label: grow it instead
            was, will = movers.pop(lit)
            self._set_look(self._line_widgets[lit], will)
            sweep = (was[0], will[0])
            self._list_sweep.set_pt(sweep[0])
        if self._sweep_at >= 0:
            self._list_sweep.report_position(seconds)
        target = self._glide_target(skip_marker=leaving)
        bar = self._scroll.verticalScrollBar()
        start_v = float(bar.value())
        host_layout = self._timed_host.layout()

        def frame(p: float) -> None:
            try:
                for i, (a, b) in movers.items():
                    self._set_look(self._line_widgets[i], _lerp_look(a, b, p))
                if sweep is not None:
                    self._list_sweep.set_pt(sweep[0] + (sweep[1] - sweep[0]) * p)
                if dots is not None:
                    dots[0](dots[1] + (dots[2] - dots[1]) * p)
                if target is not None and self._following:
                    if host_layout is not None:
                        host_layout.activate()
                    goal = target.pos().y() - int(self._scroll.viewport().height() * 0.38)
                    goal = max(0, min(int(goal), bar.maximum()))
                    bar.setValue(int(round(start_v + (goal - start_v) * p)))
                self._refresh_fades()
                self._sync_plate()
            except RuntimeError:
                pass        # the list was rebuilt mid-motion; the stop is coming

        def end() -> None:
            frame(1.0)
            if leaving:
                self._unplace_marker()

        frame(0.0)
        state: dict = {"end": end}

        def done() -> None:
            if self._line_motion is state:
                self._line_motion = None
                if leaving:
                    self._unplace_marker()

        state["anim"] = motion_module.value_lerp(
            0.0, 1.0,
            on_update=frame,
            dur=motion_module.dur("med"),
            easing=motion_module.ease("out_strong"),
            on_done=done,
            owner=self,
            kind="value/lyric_line",
        )
        if state["anim"] is not None:
            self._line_motion = state

    # ---------- edge fades + quiet scrollbar (modern) ----------

    def _queue_fades(self, *_args) -> None:
        # Coalesced: scroll, range and layout changes come in bursts.
        if self._fades_queued:
            return
        self._fades_queued = True
        QTimer.singleShot(0, self._run_queued_fades)

    def _run_queued_fades(self) -> None:
        self._fades_queued = False
        try:
            self._refresh_fades()
            self._sync_plate()
        except RuntimeError:
            pass

    def _refresh_fades(self) -> None:
        """Put an _EdgeFade on every line that reaches into the top or
        bottom band of the viewport, take it off the rest. A band only
        fades once there's more list past that edge, so the first line
        of a song isn't greyed out at the top."""
        want: list[QWidget] = []
        shown = self._scroll.widget()
        if self._modern():
            if shown is self._timed_host:
                want = list(self._line_widgets) + [self._list_sweep, self._marker, self._plate]
            elif shown is self._plain_label:
                want = [self._plain_label]
        viewport = self._scroll.viewport()
        vh = float(viewport.height())
        fade = max(float(_scale.px(16)), min(float(_scale.px(80)), vh * 0.14))
        bar = self._scroll.verticalScrollBar()
        value, top_end = float(bar.value()), float(bar.maximum())
        top_k = min(1.0, value / fade) if fade > 0 else 0.0
        bottom_k = min(1.0, (top_end - value) / fade) if fade > 0 else 0.0
        host_y = float(shown.y()) if shown is not None else 0.0
        faded: list[QWidget] = []
        for w in want:
            if w.isHidden():
                continue
            y = host_y + (w.y() if w is not shown else 0.0)
            bottom = y + w.height()
            near = ((top_k > 0.0 and y < fade and bottom > 0.0)
                    or (bottom_k > 0.0 and bottom > vh - fade and y < vh))
            if not near:
                continue
            eff = w.graphicsEffect()
            if not isinstance(eff, _EdgeFade):
                eff = _EdgeFade(w)
                w.setGraphicsEffect(eff)
            eff.set_band(y, vh, fade, top_k, bottom_k)
            faded.append(w)
        keep = set(map(id, faded))
        for w in self._faded:
            if id(w) in keep:
                continue
            try:
                if isinstance(w.graphicsEffect(), _EdgeFade):
                    w.setGraphicsEffect(None)
            except RuntimeError:
                pass        # a line already deleted
        self._faded = faded

    def _sync_quiet_bar(self) -> None:
        bar = self._scroll.verticalScrollBar()
        if self._modern():
            if self._bar_fx is None:
                self._bar_fx = QGraphicsOpacityEffect(bar)
                self._bar_fx.setOpacity(0.0)
                bar.setGraphicsEffect(self._bar_fx)
        elif self._bar_fx is not None:
            self._bar_timer.stop()
            self._bar_fx = None
            bar.setGraphicsEffect(None)

    def _fade_bar(self, to: float) -> None:
        eff = self._bar_fx
        if eff is None:
            return

        def set_opacity(v: float, eff=eff) -> None:
            try:
                eff.setOpacity(v)
            except RuntimeError:
                pass        # back to brutalist mid-fade
        motion_module.value_lerp(
            eff.opacity(), to, on_update=set_opacity,
            dur=motion_module.dur("short"), owner=self, kind="value/lyric_bar")

    def _poke_bar(self) -> None:
        if self._bar_fx is None:
            return
        if self._bar_fx.opacity() < 1.0:
            self._fade_bar(1.0)
        self._bar_timer.start()

    def _bar_idle(self) -> None:
        bar = self._scroll.verticalScrollBar()
        if bar.underMouse() or bar.isSliderDown():
            self._bar_timer.start()
            return
        self._fade_bar(0.0)

    def eventFilter(self, obj, event) -> bool:
        if getattr(self, "_back_btn", None) is None:
            return super().eventFilter(obj, event)     # still being built
        try:
            kind = event.type()
            if obj is self._scroll.viewport():
                if kind == QEvent.Resize:
                    self._queue_fades()
                    self._place_back_btn()
            elif obj is self._scroll.verticalScrollBar():
                if kind == QEvent.Enter:
                    self._poke_bar()
            elif self._line_index(obj) is not None:
                self._line_event(obj, event)
        except RuntimeError:
            pass
        return super().eventFilter(obj, event)

    # ---------- click to seek + hover ----------

    def _line_index(self, obj) -> int | None:
        """The line a list widget stands for: a label, the sweep line (the
        sung one) or the dots (the line they lead to). None otherwise."""
        if obj is self._list_sweep:
            return self._sweep_at if self._sweep_at >= 0 else None
        if obj is self._marker:
            return self._gap[0] if self._gap is not None else None
        i = getattr(obj, "_index", None)
        if i is None or not (0 <= i < len(self._line_widgets)) or self._line_widgets[i] is not obj:
            return None
        return i

    def _line_event(self, obj, event) -> None:
        kind = event.type()
        if kind == QEvent.Enter:
            self._hover_timer.stop()
            self._set_hover(obj)
        elif kind == QEvent.Leave:
            self._hover_timer.start()
        elif kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._press = (obj, event.position())
        elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            press, self._press = self._press, None
            if press is None or press[0] is not obj:
                return
            from PySide6.QtWidgets import QApplication
            moved = (event.position() - press[1]).manhattanLength()
            if moved >= QApplication.startDragDistance():
                return          # a drag: selecting text, not a click
            if isinstance(obj, QLabel) and obj.hasSelectedText():
                return
            self._seek_line(self._line_index(obj))

    def _seek_line(self, i: int | None) -> None:
        """Play from line ``i``: its start, back through the lyrics offset
        to the song's own time. The list follows the song again."""
        if i is None or not (0 <= i < len(self._timed_starts)) or _seek_fn is None:
            return
        self._set_following(True)
        target = self._timed_starts[i] - self._shift()
        try:
            # a hair past the start, so the playhead lands on the line
            _seek_fn(max(0.0, target + 0.02))
        except Exception:
            pass

    def _set_hover(self, obj) -> None:
        self._hover_obj = obj
        self._sync_plate()

    def _hover_gone(self) -> None:
        self._set_hover(None)

    def _sync_plate(self) -> None:
        """Lay the plate under the hovered line, with the time a click
        would jump to when it fits beside the words."""
        plate, obj = self._plate, self._hover_obj
        i = self._line_index(obj) if obj is not None else None
        if (i is None or obj.isHidden() or self._karaoke_mode
                or self._scroll.widget() is not self._timed_host):
            plate.hide()
            return
        host = self._timed_host
        pad = _scale.px(2)
        plate.setGeometry(0, obj.y() - pad, host.width(), obj.height() + 2 * pad)
        theme = self._theme
        modern = self._modern()
        tint = QColor(theme.token("surface_2", "#17ffffff") if (theme and modern) else "#00000000")
        radius = 0.0
        if modern and theme is not None:
            try:
                radius = float(theme.t("layout", "radius_px", 6) or 0)
            except (TypeError, ValueError):
                radius = 6.0
        ink = QColor(theme.token("dim", "#6f6f6f") if theme else "#6f6f6f")
        inset = _scale.px(LINE_HPAD if modern else 8)
        plate.set_look(tint=tint, ink=ink, radius=radius, inset=inset)
        when = _clock(self._timed_starts[i] - self._shift())
        if obj is self._marker:
            words_w = 0
        elif obj is self._list_sweep:
            words_w = QFontMetrics(obj.font()).horizontalAdvance(obj.line().text if obj.line() else "")
        else:
            words_w = obj.fontMetrics().horizontalAdvance(obj.text())
        if self._centered:
            room = (host.width() - words_w) / 2.0 - inset
        else:
            room = host.width() - self._text_left() - words_w - inset
        fits = room >= plate.fontMetrics().horizontalAdvance(when) + _scale.px(24)
        plate.set_time(when if fits else "")
        plate.show()
        plate.lower()

    # ---------- following the song ----------

    def _on_user_scroll(self, _action) -> None:
        """A scroll of your own: the list stays where you put it until
        you click back, pick a line, or leave it alone for a while."""
        self._poke_bar()
        if self._karaoke_mode or self._scroll.widget() is not self._timed_host:
            return
        if self._following:
            self._set_following(False)
            if self._scroll_anim is not None:
                self._scroll_anim.stop()
                self._scroll_anim = None
        self._follow_timer.start()

    def _set_following(self, on: bool) -> None:
        self._following = bool(on)
        if self._following:
            self._follow_timer.stop()
        self._back_btn.setVisible(not self._following)
        if not self._following:
            self._place_back_btn()

    def _resume_follow(self) -> None:
        was = self._following
        self._set_following(True)
        if (not was and self.isVisible() and self._line_widgets
                and not self._karaoke_mode):
            self._glide_to_active()

    def _sync_back_btn(self) -> None:
        self._back_btn.set_theme(self._theme)
        self._place_back_btn()

    def _place_back_btn(self) -> None:
        btn = self._back_btn
        if btn.isHidden():
            return
        btn.resize(btn.sizeHint())
        g = self._scroll.geometry()
        btn.move(g.x() + (g.width() - btn.width()) // 2,
                 g.y() + g.height() - btn.height() - _scale.px(12))
        btn.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_side()
        self._place_back_btn()
        self._place_toast()

    # ---------- the right-click menu, and what it and the keys do ----------

    def set_menu_extra(self, fn) -> None:
        """``fn(menu)`` adds the surrounding surface's own items under the
        lyric ones (the mini, fullscreen, the visualizer tile)."""
        self._menu_extra = fn

    def contextMenuEvent(self, event) -> None:
        menu = self._build_menu(event.globalPos())
        if menu is None:
            event.ignore()
            return
        event.accept()
        self._menu = menu
        menu.popup(event.globalPos())       # non-blocking, like the others

    def _line_at(self, global_pos) -> int | None:
        from PySide6.QtWidgets import QApplication
        w = QApplication.widgetAt(global_pos)
        while w is not None and w is not self:
            i = self._line_index(w)
            if i is not None:
                return i
            w = w.parentWidget()
        return None

    def _build_menu(self, global_pos):
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        case = theming.styled_case
        lines = bool(self._timed_lines) and self._scroll.widget() is self._timed_host
        i = self._line_at(global_pos) if lines else None

        def add(label: str, fn, enabled: bool = True) -> None:
            act = QAction(case(label), menu)
            act.setEnabled(enabled)
            act.triggered.connect(lambda _c=False: fn())
            menu.addAction(act)

        if i is not None:
            add("play from here", lambda: self._seek_line(i), _seek_fn is not None)
            if not self._blank[i]:
                add("copy line", lambda: self._copy(self._timed_lines[i][1]))
                add("make a card", lambda: QTimer.singleShot(0, self, lambda: self.make_card(i)))
        if self._lyrics_text():
            add("copy all lyrics", self.copy_all)
        if lines and self.has_roman():
            act = QAction(case("show romanized"), menu)
            act.setCheckable(True)
            act.setChecked(_roman_on)
            act.triggered.connect(lambda on: set_romanized(bool(on)))
            menu.addAction(act)
        if self._current_track is not None:
            if menu.actions():
                menu.addSeparator()
            if self._timed_lines:
                menu.addAction(self._timing_row(menu))
            self._source_menu(menu)
        if self._menu_extra is not None:
            if menu.actions():
                menu.addSeparator()
            try:
                self._menu_extra(menu)
            except Exception:
                pass
        if not menu.actions():
            menu.deleteLater()
            return None
        return menu

    def _timing_row(self, menu):
        """timing  −  +0.25 s  +  reset, for this song. Its buttons don't
        close the menu, so a few taps land without reopening it."""
        from PySide6.QtWidgets import QWidgetAction
        row = QWidget(menu)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(_scale.px(12), _scale.px(4), _scale.px(8), _scale.px(4))
        lay.setSpacing(_scale.px(6))
        modern = self._modern()
        case = theming.styled_case
        title = QLabel(case("timing"))
        value = QLabel()
        value.setMinimumWidth(QFontMetrics(value.font()).horizontalAdvance("−10.25 s"))
        value.setAlignment(Qt.AlignCenter)

        def button(text: str, tip: str, fn) -> QPushButton:
            btn = QPushButton(text if modern else f"[{text}]")
            btn.setFlat(True)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setToolTip(case(tip))
            btn.clicked.connect(lambda _c=False: (fn(), refresh()))
            return btn

        def refresh() -> None:
            value.setText(_fmt_nudge(self.timing_ms()))

        lay.addWidget(title)
        lay.addStretch(1)
        lay.addWidget(button("−", "show this song's lyrics earlier", lambda: self.nudge_timing(-1)))
        lay.addWidget(value)
        lay.addWidget(button("+", "show this song's lyrics later", lambda: self.nudge_timing(1)))
        lay.addWidget(button(case("reset"), "back to the lyrics offset setting", self.reset_timing))
        refresh()
        act = QWidgetAction(menu)
        act.setDefaultWidget(row)
        return act

    def _sources(self) -> list[str]:
        track = self._current_track
        out = []
        if (getattr(track, "source", "") or "ytmusic") == "ytmusic" \
                and callable(getattr(self.api, "_yt_timed_lyrics", None)):
            out.append("youtube music")
        out.append("lrclib")
        try:
            if lyric_timing.service().netease_allowed():
                out.append("netease")
        except Exception:
            pass
        return out

    def _source_menu(self, menu) -> None:
        """lyrics from <where they came from> ▸ automatic / each source,
        remembered for this song."""
        from PySide6.QtGui import QAction, QActionGroup
        case = theming.styled_case
        now = getattr(self._result, "source", "") if self._result is not None else ""
        picked = lyrics_picks.source(self._current_key)
        sub = menu.addMenu(case(f"lyrics from {now}" if now else "lyrics from"))
        group = QActionGroup(sub)
        for label, src in [("automatic", None)] + [(x, x) for x in self._sources()]:
            act = QAction(case(label), sub)
            act.setCheckable(True)
            act.setChecked(picked == src)
            act.triggered.connect(lambda _c=False, src=src: self.pick_source(src))
            group.addAction(act)
            sub.addAction(act)

    def make_card(self, i: int) -> None:
        """Open the lyric card for line ``i`` and the lines after it."""
        if not (0 <= i < len(self._timed_lines)) or self._blank[i]:
            return
        from .lyric_card import CardDialog
        sung = [k for k in range(len(self._timed_lines)) if not self._blank[k]]
        track = self._current_track
        url = getattr(track, "thumbnail", "") or ""
        cover = None
        dialog = CardDialog([self._timed_lines[k][1] for k in sung], sung.index(i),
                            title=getattr(track, "title", "") or "",
                            artist=getattr(track, "artists", "") or "",
                            cover=None, parent=self.window())
        if url:
            from . import art_cache

            def ready(image, dialog=dialog) -> None:
                try:
                    dialog.set_cover(image)
                except RuntimeError:
                    pass        # closed before the cover came
            cover = art_cache.cache().request(url, ready)
            if cover is not None:
                dialog.set_cover(cover)
        dialog.show()

    def pick_source(self, src: str | None) -> None:
        """Take this song's lyrics from ``src`` from now on (None: tide's
        usual order), and fetch them again."""
        key = self._current_key
        if not key or src == lyrics_picks.source(key):
            return
        lyrics_picks.set_source(key, src)
        self._retry()

    def timing_ms(self) -> int:
        return lyrics_picks.offset_ms(self._current_key)

    def nudge_timing(self, steps: int) -> None:
        """Move this song's lyrics ``steps`` × 0.25 s (positive: later)."""
        key = self._current_key
        if not key or not self._timed_lines:
            return
        set_song_offset_ms(key, self.timing_ms() + int(steps) * TIMING_STEP_MS)
        self._say(f"timing {_fmt_nudge(self.timing_ms())}")

    def reset_timing(self) -> None:
        key = self._current_key
        if not key:
            return
        set_song_offset_ms(key, 0)
        self._say("timing 0 s")

    def toggle_karaoke(self) -> None:
        if word_view() and self._timed_lines:
            self.karaoke_check.setChecked(not self.karaoke_check.isChecked())

    def _lyrics_text(self) -> str:
        if self._timed_lines:
            return "\n".join(text for _t, text in self._timed_lines)
        if self._status is None and self._scroll.widget() is self._plain_label:
            return self._plain_label.text()
        return ""

    def copy_all(self) -> None:
        text = self._lyrics_text()
        if text:
            self._copy(text)
            self._say("copied the lyrics")

    def copy_line(self) -> None:
        """Copy what's selected, else the hovered line, else the one being
        sung (or just sung)."""
        for lbl in list(self._line_widgets) + [self._plain_label]:
            if lbl.hasSelectedText():
                self._copy(lbl.selectedText())
                self._say("copied")
                return
        i = self._line_index(self._hover_obj) if self._hover_obj is not None else None
        if i is None:
            i = self._lit_index if self._lit_index >= 0 else self._active_line_index
        if 0 <= i < len(self._timed_lines) and not self._blank[i]:
            self._copy(self._timed_lines[i][1])
            self._say("copied the line")

    def _copy(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(str(text))

    def _say(self, text: str) -> None:
        toast = self._toast
        toast.set_theme(self._theme)
        toast.set_label(text)
        toast.show()
        self._place_toast()
        self._toast_timer.start()

    def _place_toast(self) -> None:
        toast = self._toast
        if toast.isHidden():
            return
        toast.resize(toast.sizeHint())
        g = self._scroll.geometry()
        toast.move(g.x() + (g.width() - toast.width()) // 2, g.y() + _scale.px(12))
        toast.raise_()

    def _animate_active_line(self) -> None:
        """Fade the newly-active line from body color into the accent.
        The restyle above already snapped the bold + size (so layout
        settles in one step); the color glide is what makes the handoff
        read as motion instead of a blink."""
        if self._active_anim is not None:
            self._active_anim.stop()
            self._active_anim = None
        idx = self._lit_index
        if not (0 <= idx < len(self._line_widgets)):
            return
        if idx == self._sweep_at:
            return   # the sweep line is up; the label is hidden
        if motion_module.intensity() == motion_module.Intensity.OFF:
            return   # _restyle_lines already painted the accent
        theme = self._theme
        fg = QColor(theme.token("fg", "#e6e6e6") if theme else "#e6e6e6")
        accent = QColor(
            theme.token("accent", "#d4b95e") if theme else "#d4b95e")
        lbl = self._line_widgets[idx]
        pt = type_pt("active", self._font_scale)

        def _apply(c: QColor, lbl=lbl, pt=pt) -> None:
            try:
                _set_line_sheet(lbl,
                    f"color: {c.name()}; background: transparent; "
                    f"font-weight: 700; font-size: {pt}pt; padding: 4px 0;"
                )
            except RuntimeError:
                pass   # line list rebuilt mid-fade; the stop is coming

        self._active_anim = motion_module.color_lerp(
            fg, accent,
            on_update=_apply,
            owner=self,
            kind="color/lyric_active",
        )

    def _glide_to_active(self) -> None:
        """Teleprompter scroll: park the active line around a third from
        the top of the viewport. ensureWidgetVisible only moved when a
        line left the view, so the list sat still and then jumped a
        page; gliding a little on every line keeps the eye anchored."""
        target = self._glide_target()
        if target is None:
            return
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
            self._scroll_anim = None
        # Sizes just changed in _restyle_lines; settle geometry before
        # reading positions or the goal is one restyle stale.
        host_layout = self._timed_host.layout()
        if host_layout is not None:
            host_layout.activate()
        if motion_module.intensity() == motion_module.Intensity.OFF:
            self._scroll.ensureWidgetVisible(
                target, 0, max(80, self._scroll.viewport().height() // 3),
            )
            return
        bar = self._scroll.verticalScrollBar()
        goal = target.pos().y() - int(self._scroll.viewport().height() * 0.38)
        goal = max(0, min(int(goal), bar.maximum()))
        if goal == bar.value():
            return
        self._scroll_anim = motion_module.value_lerp(
            float(bar.value()), float(goal),
            on_update=lambda v, bar=bar: bar.setValue(int(v)),
            dur=motion_module.dur("med"),
            easing=motion_module.ease("out_strong"),
            owner=self,
            kind="value/lyric_scroll",
        )

    def _restyle_lines(self) -> None:
        if self._modern():
            self._finish_line_motion()
            for i, lbl in enumerate(self._line_widgets):
                self._set_look(lbl, self._steady_look(i))
            self._queue_fades()
            return
        theme = self._theme
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#6f6f6f") if theme else "#6f6f6f"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        active_pt = type_pt("active", self._font_scale)
        rest_pt = type_pt("line", self._font_scale)
        # Only the lines whose sheet actually changes are touched: each
        # setStyleSheet is a repolish, and a song has dozens of lines of
        # which two change state per step.
        for i, lbl in enumerate(self._line_widgets):
            if i == self._lit_index:
                _set_line_sheet(lbl,
                    f"color: {accent}; background: transparent; "
                    f"font-weight: 700; font-size: {active_pt}pt; padding: 4px 0;"
                )
            elif i <= self._active_line_index:
                _set_line_sheet(lbl,
                    f"color: {dim}; background: transparent; "
                    f"font-size: {rest_pt}pt; padding: 2px 0;"
                )
            else:
                _set_line_sheet(lbl,
                    f"color: {fg}; background: transparent; "
                    f"font-size: {rest_pt}pt; padding: 2px 0;"
                )

    # ---------- karaoke + swap ----------

    def _sync_word_view(self) -> None:
        on = word_view()
        self.word_toggles.setVisible(on)
        if not on and self.karaoke_check.isChecked():
            self.karaoke_check.setChecked(False)      # back to the list
        self._unplace_sweep()
        self._restyle_lines()
        self._place_sweep()
        self._list_sweep.report_position(self._last_position)

    def _on_karaoke_toggled(self, on: bool) -> None:
        self._karaoke_mode = bool(on)
        self.sweep_check.setEnabled(not self._karaoke_mode)   # karaoke always sweeps
        # No timed data yet → toggle is harmless visual-state-only.
        if not self._timed_lines:
            return
        if self._karaoke_mode:
            # Re-sync the karaoke widget to the current playback position
            # so the user doesn't see the "active line" pop back to -1
            # for a beat after toggling on.
            self._karaoke_widget.set_position(self._last_position)
            self._unplace_marker()
            self._set_hover(None)
            self._set_following(True)
            self._set_scroll_widget(self._karaoke_widget)
        else:
            self._set_scroll_widget(self._timed_host)
            self._resync()          # the list sat still while karaoke ran

    # ---------- theme ----------

    def _on_theme(self, theme) -> None:
        was_modern = self._modern()
        self._theme = theme
        self.refresh_type()
        self._sync_heading()
        self._sync_quiet_bar()
        self._sync_back_btn()
        self.refresh_layout()
        if self._status is not None and was_modern != self._modern():
            self._show_status(self._status)     # the other aesthetic's empty state
        self._queue_fades()

    def _sync_heading(self) -> None:
        if self._tab:
            self.heading.setVisible(not self._modern())

    def refresh_type(self) -> None:
        """Re-derive every lyric size: theme, scale or the size step
        changed."""
        self._tones = line_tones(self._theme)
        self._empty_msg.setStyleSheet(
            f"font-size: {type_pt('line', self._font_scale)}pt;")
        if self._side is not None:
            # the cover's words, sized off the lyrics beside them
            line = type_pt("line", self._font_scale)
            self._side_title.setStyleSheet(
                f"font-size: {_pt_css(line * 1.2)}; font-weight: 700;")
            self._side_artist.setStyleSheet(f"font-size: {_pt_css(line * 0.9)};")
        self._plain_label.setStyleSheet(
            f"font-size: {type_pt('line', self._font_scale)}pt;")
        for i, lbl in enumerate(self._line_widgets):
            if i < len(self._note) and self._note[i]:
                lbl.set_dots(self._modern())
        if self._line_widgets:
            self._restyle_lines()
        self._apply_sweep_look()
        self._apply_marker_look()
        self._karaoke_widget.set_theme(self._theme)
        self._queue_fades()         # the plate follows the new sizes
