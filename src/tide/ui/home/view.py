"""Home engine (v1.5) — blocks and patterns instead of shelf × N.

Replaces the v1.2–v1.4 ExploreView (same signal surface, so the window
wiring barely changes). The page is assembled from three strata:

  hero        — greeting + keep-listening resume card. Local data only,
                renders instantly, works for every source.
  shelves     — the active source's ``get_home`` shelves, each run through
                a pattern picker (quick picks → tap grid, listen again →
                dense grid, artist shelves → circle row, mixed shelves →
                mosaic/shelf rotation seeded by day).
  extras      — editorial feeds where the source has them: top songs
                (ranked), charts artists (ranked), new releases (mosaic),
                new music videos, charts playlists, moods & genres chips.
                Each arrives from its own worker and slots into a fixed
                order regardless of arrival order.

Every stratum is isolated: a parser break or dead network in one block
renders as absence, never as a broken page. Blocks are built one per
event-loop turn so a 10-block page doesn't jank the paint thread.

The strata live in fixed slots (2.3): hero, then the source's shelves,
then the extras. The extras' workers read a disk cache and answer in
milliseconds while ``get_home`` is a network round trip, so blocks
appended in arrival order put the generic charts above quick picks.

Settings → appearance → "home layout" = "shelves" restores the v1.4 look
exactly (plain shelf rows, no hero, no extras).

A mood chip flips the view into mood mode — a playlist grid for that
category with a [back to home] — without leaving the widget.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import api, history as history_module, qthreads, session as session_module, theming
from ..card import Card, CardGrid
from ..headings import Heading, line_heading as _line_heading
from ..widgets import BracketButton
from . import patterns


def _country_from_env() -> str:
    """Locale-derived charts country ("en_US.UTF-8" → "US"); ZZ = global."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var) or ""
        if "_" in val:
            cc = val.split("_", 1)[1][:2].upper()
            if len(cc) == 2 and cc.isalpha():
                return cc
    return "ZZ"


# Each line has to read right with ", <first name>" after it.
_GREETINGS_ANYTIME = ("welcome back", "hey", "hi", "good to see you")
_GREETINGS = {
    "morning": ("good morning", "morning", "rise and shine", "early start",
                "coffee first"),
    "afternoon": ("good afternoon", "afternoon", "back at it",
                  "afternoon break"),
    "evening": ("good evening", "evening", "winding down", "long day"),
    "late": ("up late", "still up", "night owl", "late one",
             "the quiet hours"),
}


def _greeting(now: float | None = None) -> str:
    """A time-of-day line, picked by the date so [refresh] doesn't reroll
    it; a new slot or a new day gets a new one."""
    t = time.localtime(now)
    hour = t.tm_hour
    if 5 <= hour < 12:
        slot = "morning"
    elif 12 <= hour < 18:
        slot = "afternoon"
    elif 18 <= hour < 23:
        slot = "evening"
    else:
        slot = "late"
    pool = _GREETINGS[slot] + _GREETINGS_ANYTIME
    day = t.tm_year * 1000 + t.tm_yday
    return pool[(day * 7 + len(slot)) % len(pool)]


def _weekly_stats() -> str:
    """One dim line from the local history: listening time + top artist,
    last 7 days. Duration is the honest estimate we have (history logs
    plays, not listen-through), so the line says what it means: 'about'."""
    try:
        entries = history_module.read_recent(500)
    except Exception:
        return ""
    cutoff = time.time() - 7 * 86400
    total = 0
    counts: dict[str, int] = {}
    for e in entries:
        if e.played_at < cutoff:
            continue
        total += max(0, int(e.duration_seconds or 0))
        first = (e.artists or "").split(",")[0].strip()
        if first:
            counts[first] = counts.get(first, 0) + 1
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    mins = rem // 60
    when = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
    top = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
    if len(top) > 24:
        top = top[:24].rstrip() + "…"
    line = f"about {when} this week"
    if top:
        line += f" · mostly {top}"
    return line


# How far back the hero's week panel reads, and what it takes to make
# "on repeat" (plays of one song in the last 7 days).
_WEEK_HISTORY_LINES = 5000
_REPEAT_MIN_PLAYS = 3


def week_summary(entries, now: float | None = None) -> dict | None:
    """The hero's week panel, from the local history (newest first, as
    history.read_recent gives it). None when the last 7 days are empty.

      minutes  7 floats, oldest day first, today last (listening time,
               estimated from the durations of the plays that started)
      days     7 one-letter day names to label them
      streak   days in a row with a play, ending today (or yesterday,
               when today hasn't started yet)
      repeat   up to 3 (Track, plays) for the most-played songs this week
      new      first artists heard for the first time this week, when the
               history reaches back far enough to tell (two weeks)
    """
    import datetime as _dt
    from collections import Counter

    now = time.time() if now is None else float(now)
    today = _dt.date.fromtimestamp(now)
    secs = [0.0] * 7
    plays: Counter = Counter()
    latest: dict = {}
    dates: set = set()
    first_seen: dict[str, float] = {}
    oldest = now
    for e in entries:
        when = float(e.played_at or 0.0)
        if when <= 0:
            continue
        oldest = min(oldest, when)
        day = _dt.date.fromtimestamp(when)
        dates.add(day)
        artist = (e.artists or "").split(",")[0].strip()
        if artist:
            key = artist.lower()
            first_seen[key] = min(first_seen.get(key, when), when)
        back = (today - day).days
        if 0 <= back < 7:
            secs[6 - back] += max(0, int(e.duration_seconds or 0))
            if e.video_id:
                plays[e.video_id] += 1
                latest.setdefault(e.video_id, e)
    if not any(secs) and not plays:
        return None
    streak = 0
    day = today if today in dates else today - _dt.timedelta(days=1)
    while day in dates:
        streak += 1
        day -= _dt.timedelta(days=1)
    repeat = [(latest[vid].to_track(), n) for vid, n in plays.most_common(3)
              if n >= _REPEAT_MIN_PLAYS]
    new: list[str] = []
    if oldest < now - 14 * 86400:
        cutoff = now - 7 * 86400
        names = {}
        for e in entries:
            artist = (e.artists or "").split(",")[0].strip()
            if artist and first_seen.get(artist.lower(), 0) >= cutoff:
                names.setdefault(artist.lower(), artist)
        new = list(names.values())
    days = [(today - _dt.timedelta(days=6 - i)).strftime("%a")[0].lower()
            for i in range(7)]
    return {"minutes": [s / 60.0 for s in secs], "days": days,
            "streak": streak, "repeat": repeat, "new": new}


_RECENT_MAX = 24


def recently_played(entries, limit: int = _RECENT_MAX) -> list:
    """The hero's recently-played strip: Tracks from the history (newest
    first), one per cover, so an album played through shows once. The
    keep-listening track (entries[0]) and its cover are left out, since
    the hero already shows them."""
    out: list = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        thumb = e.thumbnail or ""
        if not e.video_id or not thumb or thumb in seen:
            continue
        seen.add(thumb)
        if i == 0:
            continue
        out.append(e.to_track())
        if len(out) >= limit:
            break
    return out


# ---------- workers ----------


class _ShelvesWorker(QObject):
    done = Signal(int, list)
    failed = Signal(int, str)

    def __init__(self, api_obj: api.Api, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.api.get_home(limit=10))
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _ExploreWorker(QObject):
    done = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(self, api_obj: api.Api, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.api.get_explore_data())
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _ChartsWorker(QObject):
    done = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(self, api_obj: api.Api, country: str, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.country = country
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.api.get_charts_data(self.country))
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


class _MoodWorker(QObject):
    done = Signal(int, str, list)
    failed = Signal(int, str)

    def __init__(self, api_obj: api.Api, title: str, params: str, gen: int) -> None:
        super().__init__()
        self.api = api_obj
        self.title = title
        self.params = params
        self.gen = gen

    def run(self) -> None:
        try:
            self.done.emit(self.gen, self.title,
                           self.api.get_mood_playlists_list(self.params))
        except Exception as exc:
            self.failed.emit(self.gen, str(exc))


# ---------- pattern picker ----------

# Extras render in this order no matter which worker answers first.
# top songs and the charts artists share one band (patterns.ChartsBand),
# side by side when the page is wide enough.
_EXTRA_WEIGHTS = {
    "charts": 0,
    "new_releases": 1,
    "new_videos": 2,
    "charts_playlists": 3,
    "moods": 4,
}


def pick_pattern(title: str, items: list, index: int, seed: int) -> str:
    """Shelf → pattern. Stable names first, then content shape, then a
    seeded rotation so mixed shelves vary by day instead of by chance."""
    t = (title or "").lower()
    if "quick picks" in t:
        return "tap_grid"
    if "listen again" in t:
        return "dense_grid"
    kinds = {getattr(it, "kind", "") for it in items}
    if kinds and kinds <= {"artist"}:
        return "circle_row"
    if kinds and kinds <= {"song", "video"}:
        return "tap_grid" if index == 0 else "shelf_row"
    rotation = ("mosaic", "shelf_row", "dense_grid")
    return rotation[(seed + index) % len(rotation)]


# How long the personalized shelves get before the loading note turns into
# a "took too long" message. The worker keeps running, so shelves that land
# after this still show and the message goes away.
_SHELVES_TIMEOUT_MS = 15_000

_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _RecoNote(QLabel):
    """One dim line in the shelves slot: a spinner and "loading
    recommendations" while the source's home feed is in flight, or the
    failure text. The spinner is a glyph cycled by a timer that only runs
    while the note is visible and loading."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "dim")
        self.setWordWrap(True)
        self._frame = 0
        self._loading = False
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._tick)
        self.set_loading()

    def is_loading(self) -> bool:
        return self._loading

    def set_loading(self) -> None:
        self._loading = True
        self._paint_frame()
        if self.isVisible():
            self._timer.start()

    def set_failed(self, reason: str) -> None:
        self._loading = False
        self._timer.stop()
        reason = (reason or "").strip().splitlines()[0:1]
        reason = reason[0][:200] if reason else "unknown error"
        self.setText(theming.styled_case(
            f"  failed to load recommended: {reason}"))

    def _paint_frame(self) -> None:
        self.setText(theming.styled_case("  loading recommendations")
                     .replace("  ", f"{_SPIN_FRAMES[self._frame]} ", 1))

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_SPIN_FRAMES)
        self._paint_frame()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._loading:
            self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)


class HomeView(QWidget):
    """Drop-in successor to ExploreView — same signals + home-engine ones."""

    play_now_requested = Signal(object, bool)
    queue_add_requested = Signal(object)
    radio_requested = Signal(object)
    album_requested = Signal(object)          # AlbumEntry
    artist_requested = Signal(object)         # ArtistEntry
    playlist_requested = Signal(object)       # PlaylistEntry
    resume_requested = Signal()               # hero [resume]
    likes_shuffle_requested = Signal()        # hero [shuffle likes]
    status_message = Signal(str)

    def __init__(self, api_obj: api.Api, settings_provider=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api_obj
        self._settings_provider = settings_provider or (lambda: None)
        self._theme = theming.manager().current()
        theming.manager().theme_changed.connect(self._on_theme)
        self._gen = 0
        self._loaded = False
        self._mode = "home"                   # "home" | "mood"
        self._pending_builds: list = []
        # weight -> the widgets that extra put in the extras slot
        self._extras_present: dict[int, list[QWidget]] = {}
        self._charts_band = None
        self._reco_note: _RecoNote | None = None
        self._build_ui()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        self.heading = Heading("home")
        self.heading.setProperty("class", "dim")
        self.back_btn = BracketButton("back to home")
        self.back_btn.setIconKey("back")
        self.back_btn.setRole("pill")
        self.back_btn.clicked.connect(self._leave_mood)
        self.back_btn.hide()
        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.setIconKey("refresh")
        self.refresh_btn.clicked.connect(self.reload)

        top = QHBoxLayout()
        top.addWidget(self.heading, stretch=1)
        top.addWidget(self.back_btn)
        top.addWidget(self.refresh_btn)

        self._content = QWidget()
        self._content_col = QVBoxLayout(self._content)
        self._content_col.setContentsMargins(0, 0, 0, 0)
        self._content_col.setSpacing(12)
        self._make_slots()

        scroll = QScrollArea()
        scroll.setWidget(self._content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        from .. import scale as _scale
        root = QVBoxLayout(self)
        root.setContentsMargins(*_scale.margins(16, 14, 16, 8))
        root.setSpacing(_scale.px(10))
        root.addLayout(top)
        root.addWidget(scroll, stretch=1)

    def _make_slots(self) -> None:
        """[hero] · shelves · extras · stretch. The hero goes in at the
        top when built; shelves and extras each own a column, so a block
        lands in its stratum whenever its worker answers."""
        def slot() -> tuple[QWidget, QVBoxLayout]:
            w = QWidget()
            col = QVBoxLayout(w)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(12)
            w.hide()            # an empty slot must not add page spacing
            return w, col
        self._shelves_slot, self._shelves_col = slot()
        self._extras_slot, self._extras_col = slot()
        self._content_col.addWidget(self._shelves_slot)
        self._content_col.addWidget(self._extras_slot)
        self._content_col.addStretch(1)

    def _clear_content(self) -> None:
        self._pending_builds.clear()
        self._extras_present.clear()
        self._charts_band = None
        self._reco_note = None      # deleted with the slots below
        while self._content_col.count():
            it = self._content_col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
            lay = it.layout()
            if lay is not None:
                lay.deleteLater()
        self._make_slots()

    def _append(self, w: QWidget) -> None:
        """Into the shelves stratum (shelf blocks, notes, the mood grid)."""
        self._shelves_col.addWidget(w)
        self._shelves_slot.show()

    # ---------- public ----------

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.reload()

    def reload(self) -> None:
        self._gen += 1
        self._loaded = False
        self._mode = "home"
        self.back_btn.hide()
        self._clear_content()
        plain = self._plain_mode()

        if not plain:
            self._build_hero()

        if hasattr(self.api, "supports") and not self.api.supports("home"):
            src_name = theming.styled_case(getattr(self.api, "name", "this source"))
            self.heading.set_label(f"home · no shelves from {src_name}")
            placeholder = QLabel(theming.styled_case(
                "  this source has no home feed. search still works, and "
                "you can switch sources in [source] (ctrl+7)."
            ))
            placeholder.setWordWrap(True)
            self._append(placeholder)
            self._loaded = True
            return

        self.heading.set_label("home · loading…")
        self.status_message.emit(theming.styled_case("loading home…"))
        gen = self._gen
        self._spawn(_ShelvesWorker(self.api, gen),
                    self._on_shelves, self._on_shelves_failed)
        if not plain:
            self._show_reco_note(gen)
            if self.api.supports("explore"):
                self._spawn(_ExploreWorker(self.api, gen),
                            self._on_explore, self._on_extra_failed)
            if self.api.supports("charts"):
                self._spawn(_ChartsWorker(self.api, _country_from_env(), gen),
                            self._on_charts, self._on_extra_failed)

    # ---------- hero ----------

    def _plain_mode(self) -> bool:
        s = self._settings_provider()
        return str(getattr(s, "home_layout", "patterns") or "patterns") == "shelves"

    def _daily_seed(self) -> int:
        return int(time.strftime("%Y%m%d"))

    def _build_hero(self) -> None:
        last_track = None
        week = None
        played: list = []
        try:
            recent = history_module.read_recent(_WEEK_HISTORY_LINES)
            if recent:
                last_track = recent[0].to_track()
                week = week_summary(recent)
                played = recently_played(recent)
        except Exception:
            pass
        can_resume = False
        try:
            can_resume = session_module.load() is not None
        except Exception:
            pass
        name = str(getattr(self.api, "account_name", "") or "").split(" ")[0]
        greeting = f"{_greeting()}, {name}" if name else _greeting()
        hero = patterns.Hero(
            greeting, _weekly_stats(), last_track,
            can_resume=can_resume,
            show_likes=(getattr(self.api, "slug", "") == "ytmusic"),
            week=week, recent=played,
        )
        hero.resume_clicked.connect(self.resume_requested.emit)
        hero.track_clicked.connect(lambda tr: self.play_now_requested.emit(tr, True))
        hero.radio_clicked.connect(self.radio_requested.emit)
        hero.likes_clicked.connect(self.likes_shuffle_requested.emit)
        self._content_col.insertWidget(0, hero)

    # ---------- shelves ----------

    def _spawn(self, worker: QObject, done, failed) -> None:
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(done)
        worker.failed.connect(failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker)
        thread.start()

    def _on_shelves(self, gen: int, shelves: list) -> None:
        if gen != self._gen or self._mode != "home":
            return
        self._loaded = True
        self._drop_reco_note()
        self.heading.set_label("home")
        if not shelves:
            self._dim_note("nothing on the home feed yet. play some music "
                           "and check back.")
            return
        plain = self._plain_mode()
        seed = self._daily_seed()
        # quick picks leads: youtube puts it a few shelves down, and it's
        # the one people reach for.
        shelves = sorted(
            shelves,
            key=lambda s: "quick picks" not in (s.title or "").lower())
        for i, shelf in enumerate(shelves):
            pattern = "shelf_row" if plain else pick_pattern(
                shelf.title, shelf.items, i, seed)
            self._queue_build(
                lambda s=shelf, p=pattern: self._build_shelf_block(s, p, seed))
        self.status_message.emit(
            theming.styled_case(f"home · {len(shelves)} shelves"))

    def _on_shelves_failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        self._loaded = True
        if self._reco_note is not None:
            self._reco_note.set_failed(msg)
        self.heading.set_label("home · shelves failed")
        self.status_message.emit(f"home: {msg}")

    def _show_reco_note(self, gen: int) -> None:
        """Spinner in the shelves slot until get_home answers (the extras
        read a disk cache and land first, so without this the page looks
        finished). Times out into a message; a late answer still wins."""
        self._reco_note = _RecoNote()
        self._append(self._reco_note)
        QTimer.singleShot(_SHELVES_TIMEOUT_MS,
                          lambda: self._on_shelves_timeout(gen))

    def _on_shelves_timeout(self, gen: int) -> None:
        note = self._reco_note
        if gen != self._gen or note is None or not note.is_loading():
            return
        note.set_failed("it took too long")

    def _drop_reco_note(self) -> None:
        note, self._reco_note = self._reco_note, None
        if note is None:
            return
        self._shelves_col.removeWidget(note)
        note.hide()
        note.deleteLater()
        if self._shelves_col.count() == 0:
            self._shelves_slot.hide()

    def _dim_note(self, text: str) -> None:
        note = QLabel(theming.styled_case("  " + text))
        note.setWordWrap(True)
        note.setProperty("class", "dim")
        self._append(note)

    def _queue_build(self, fn) -> None:
        """One block per event-loop turn: ten shelves of cards built in a
        single tick is a visible hitch; spread out, it's invisible."""
        self._pending_builds.append((self._gen, fn))
        if len(self._pending_builds) == 1:
            QTimer.singleShot(0, self._drain_one)

    def _drain_one(self) -> None:
        while self._pending_builds:
            gen, fn = self._pending_builds[0]
            if gen != self._gen:
                self._pending_builds.pop(0)
                continue
            try:
                fn()
            except Exception:
                pass    # block isolation: one bad shelf never blanks home
            self._pending_builds.pop(0)
            if self._pending_builds:
                QTimer.singleShot(0, self._drain_one)
            return

    def _build_shelf_block(self, shelf, pattern: str, seed: int) -> None:
        label = Heading(shelf.title or "shelf")
        label.setProperty("class", "dim")
        self._append(label)
        w: QWidget
        if pattern == "tap_grid":
            w = patterns.TapGrid(shelf.items)
            w.item_activated.connect(self._dispatch_item)
        elif pattern == "dense_grid":
            w = patterns.DenseGrid(shelf.items)
            w.item_activated.connect(self._dispatch_item)
        elif pattern == "mosaic":
            w = patterns.Mosaic(shelf.items, seed=seed)
            w.item_activated.connect(self._dispatch_item)
        elif pattern == "circle_row":
            w = patterns.circle_row(shelf.items, self._dispatch_item)
        else:
            w = patterns.shelf_row(shelf.items, self._dispatch_item)
        self._append(w)

    # ---------- extras ----------

    def _add_extra(self, weight: int, title: str | None, body: QWidget) -> None:
        """Put an extra in the extras slot, ordered by weight among the
        extras already there. ``title`` None = the body carries its own
        headings (the charts band)."""
        if self._mode != "home":
            return
        widgets: list[QWidget] = []
        if title is not None:
            label = Heading(title)
            label.setProperty("class", "dim")
            widgets.append(label)
        widgets.append(body)
        idx = sum(len(ws) for w, ws in self._extras_present.items()
                  if w < weight)
        for i, w in enumerate(widgets):
            self._extras_col.insertWidget(idx + i, w)
        self._extras_present.setdefault(weight, []).extend(widgets)
        self._extras_slot.show()

    def _on_explore(self, gen: int, data: dict) -> None:
        if gen != self._gen or self._mode != "home":
            return
        top = data.get("top_songs") or []
        if top:
            self._queue_build(lambda: self._build_ranked(
                "top songs", "top_songs", top))
        releases = data.get("new_releases") or []
        if releases:
            self._queue_build(lambda: self._build_release_mosaic(releases))
        videos = data.get("new_videos") or []
        if videos:
            self._queue_build(lambda: self._build_video_row(videos))
        moods = data.get("moods") or []
        if moods:
            self._queue_build(lambda: self._build_moods(moods))

    def _on_charts(self, gen: int, data: dict) -> None:
        if gen != self._gen or self._mode != "home":
            return
        artists = data.get("artists") or []
        label = data.get("selected") or ""
        if artists:
            title = f"charts · {label}" if label else "charts · artists"
            self._queue_build(lambda: self._build_ranked(
                title, "charts_artists", artists))
        playlists = data.get("playlists") or []
        if playlists:
            self._queue_build(lambda: self._build_chart_playlists(playlists))

    def _on_extra_failed(self, gen: int, _msg: str) -> None:
        # Extras are pure bonus — absence is the failure UI.
        return

    def _build_ranked(self, title: str, slot: str, entries: list) -> None:
        """A ranked list into the charts band: top songs on the left,
        the charts artists on the right, whichever lands first."""
        if self._mode != "home":
            return
        w = patterns.RankedList(entries)
        w.item_activated.connect(self._dispatch_item)
        if self._charts_band is None:
            self._charts_band = patterns.ChartsBand()
            self._add_extra(_EXTRA_WEIGHTS["charts"], None, self._charts_band)
        self._charts_band.set_list(slot, title, w)

    def _build_release_mosaic(self, releases: list) -> None:
        from ...sources.base import ShelfItem
        items = [ShelfItem(kind="album", title=r.title, subtitle=r.artists,
                           thumbnail=r.thumbnail, album=r)
                 for r in releases[:1 + patterns.Mosaic.MAX_REST]]
        w = patterns.Mosaic(items, seed=self._daily_seed() // 3)
        w.item_activated.connect(self._dispatch_item)
        self._add_extra(_EXTRA_WEIGHTS["new_releases"], "new releases", w)

    def _build_video_row(self, videos: list) -> None:
        from ...sources.base import ShelfItem
        items = [ShelfItem(kind="video", title=v.title, subtitle=v.artists,
                           thumbnail=v.thumbnail, track=v)
                 for v in videos[:14]]
        w = patterns.shelf_row(items, self._dispatch_item)
        self._add_extra(_EXTRA_WEIGHTS["new_videos"], "new music videos", w)

    def _build_chart_playlists(self, playlists: list) -> None:
        from ...sources.base import ShelfItem
        items = [ShelfItem(kind="playlist", title=p.title,
                           subtitle=p.description, thumbnail=p.thumbnail,
                           playlist=p)
                 for p in playlists[:12]]
        w = patterns.shelf_row(items, self._dispatch_item)
        self._add_extra(_EXTRA_WEIGHTS["charts_playlists"], "the charts", w)

    def _build_moods(self, sections: list) -> None:
        # One chip row per section, all under one heading.
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        for title, cats in sections:
            if len(sections) > 1:
                sub = QLabel(theming.styled_case(str(title)))
                sub.setProperty("class", "dim")
                col.addWidget(sub)
            chips = patterns.ChipRow(cats)
            chips.chip_activated.connect(self._open_mood)
            col.addWidget(chips)
        self._add_extra(_EXTRA_WEIGHTS["moods"], "moods & genres", wrap)

    # ---------- mood mode ----------

    def _open_mood(self, cat) -> None:
        self._gen += 1
        self._mode = "mood"
        self._clear_content()
        self.back_btn.show()
        self.heading.set_label(f"moods · {cat.title} · loading…")
        self._spawn(_MoodWorker(self.api, cat.title, cat.params, self._gen),
                    self._on_mood, self._on_mood_failed)

    def _on_mood(self, gen: int, title: str, playlists: list) -> None:
        if gen != self._gen or self._mode != "mood":
            return
        self.heading.set_label(f"moods · {title}")
        if not playlists:
            self._dim_note("nothing in this category right now.")
            return
        grid = CardGrid()
        for p in playlists:
            c = Card(p.title, p.description, p.thumbnail, p)
            c.clicked.connect(self.playlist_requested.emit)
            grid.add_card(c)
        self._append(grid)

    def _on_mood_failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        self.heading.set_label("moods · load failed")
        self.status_message.emit(f"moods: {msg}")

    def _leave_mood(self) -> None:
        self.reload()

    # ---------- dispatch ----------

    def _dispatch_item(self, item) -> None:
        kind = getattr(item, "kind", "")
        if kind in ("song", "video") and item.track is not None:
            self.play_now_requested.emit(item.track, True)
        elif kind == "album" and item.album is not None:
            self.album_requested.emit(item.album)
        elif kind == "artist" and item.artist is not None:
            self.artist_requested.emit(item.artist)
        elif kind == "playlist" and item.playlist is not None:
            self.playlist_requested.emit(item.playlist)

    def _on_theme(self, theme) -> None:
        self._theme = theme
