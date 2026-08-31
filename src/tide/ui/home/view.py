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
from ..headings import line_heading as _line_heading
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


def _greeting() -> str:
    hour = time.localtime().tm_hour
    if 5 <= hour < 12:
        return "good morning"
    if 12 <= hour < 18:
        return "good afternoon"
    if 18 <= hour < 23:
        return "good evening"
    return "up late"


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
_EXTRA_WEIGHTS = {
    "top_songs": 0,
    "charts_artists": 1,
    "new_releases": 2,
    "new_videos": 3,
    "charts_playlists": 4,
    "moods": 5,
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
        self._extras_present: list[int] = []  # weights already inserted
        self._build_ui()

    # ---------- layout ----------

    def _build_ui(self) -> None:
        self.heading = QLabel(_line_heading("home"))
        self.heading.setProperty("class", "dim")
        self.back_btn = BracketButton("back to home")
        self.back_btn.clicked.connect(self._leave_mood)
        self.back_btn.hide()
        self.refresh_btn = BracketButton("refresh")
        self.refresh_btn.clicked.connect(self.reload)

        top = QHBoxLayout()
        top.addWidget(self.heading, stretch=1)
        top.addWidget(self.back_btn)
        top.addWidget(self.refresh_btn)

        self._content = QWidget()
        self._content_col = QVBoxLayout(self._content)
        self._content_col.setContentsMargins(0, 0, 0, 0)
        self._content_col.setSpacing(12)
        self._content_col.addStretch(1)

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

    def _clear_content(self) -> None:
        self._pending_builds.clear()
        self._extras_present.clear()
        while self._content_col.count():
            it = self._content_col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
            lay = it.layout()
            if lay is not None:
                lay.deleteLater()
        self._content_col.addStretch(1)

    def _insert_at(self, index: int, w: QWidget) -> None:
        self._content_col.insertWidget(
            min(index, self._content_col.count() - 1), w)

    def _append(self, w: QWidget) -> None:
        self._content_col.insertWidget(self._content_col.count() - 1, w)

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
            self.heading.setText(
                _line_heading(f"home · no shelves from {src_name}"))
            placeholder = QLabel(theming.styled_case(
                "  this source has no home feed. search still works, and "
                "you can switch sources in [source] (ctrl+7)."
            ))
            placeholder.setWordWrap(True)
            self._append(placeholder)
            self._loaded = True
            return

        self.heading.setText(_line_heading("home · loading…"))
        self.status_message.emit(theming.styled_case("loading home…"))
        gen = self._gen
        self._spawn(_ShelvesWorker(self.api, gen),
                    self._on_shelves, self._on_shelves_failed)
        if not plain:
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
        try:
            recent = history_module.read_recent(1)
            if recent:
                last_track = recent[0].to_track()
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
        )
        hero.resume_clicked.connect(self.resume_requested.emit)
        hero.track_clicked.connect(lambda tr: self.play_now_requested.emit(tr, True))
        hero.radio_clicked.connect(self.radio_requested.emit)
        hero.likes_clicked.connect(self.likes_shuffle_requested.emit)
        self._append(hero)

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
        self.heading.setText(_line_heading("home"))
        if not shelves:
            self._dim_note("nothing on the home feed yet. play some music "
                           "and check back.")
            return
        plain = self._plain_mode()
        seed = self._daily_seed()
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
        self.heading.setText(_line_heading("home · shelves failed"))
        self.status_message.emit(f"home: {msg}")

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
        label = QLabel(_line_heading(shelf.title or "shelf"))
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

    def _extra_anchor(self, weight: int) -> int:
        """Index where an extra block belongs: after the stretch-less tail
        of shelves, ordered by weight among extras already present."""
        before = sum(1 for w in self._extras_present if w < weight)
        # Each extra is [heading + body] = 2 widgets; the stretch is last.
        base = self._content_col.count() - 1
        # Walk back over extras that should come after this one.
        after = len(self._extras_present) - before
        return base - after * 2

    def _add_extra(self, weight: int, title: str, body: QWidget) -> None:
        if self._mode != "home":
            return
        idx = self._extra_anchor(weight)
        label = QLabel(_line_heading(title))
        label.setProperty("class", "dim")
        self._insert_at(idx, label)
        self._insert_at(idx + 1, body)
        self._extras_present.append(weight)

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
        w = patterns.RankedList(entries)
        w.item_activated.connect(self._dispatch_item)
        self._add_extra(_EXTRA_WEIGHTS[slot], title, w)

    def _build_release_mosaic(self, releases: list) -> None:
        from ...sources.base import ShelfItem
        items = [ShelfItem(kind="album", title=r.title, subtitle=r.artists,
                           thumbnail=r.thumbnail, album=r)
                 for r in releases[:9]]
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
        self.heading.setText(_line_heading(f"moods · {cat.title} · loading…"))
        self._spawn(_MoodWorker(self.api, cat.title, cat.params, self._gen),
                    self._on_mood, self._on_mood_failed)

    def _on_mood(self, gen: int, title: str, playlists: list) -> None:
        if gen != self._gen or self._mode != "mood":
            return
        self.heading.setText(_line_heading(f"moods · {title}"))
        if not playlists:
            self._dim_note("nothing in this category right now.")
            return
        grid = CardGrid()
        cols = 5
        grid.set_columns(cols)
        for p in playlists:
            c = Card(p.title, p.description, p.thumbnail, p)
            c.clicked.connect(self.playlist_requested.emit)
            grid.add_card(c)
        self._append(grid)

    def _on_mood_failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        self.heading.setText(_line_heading("moods · load failed"))
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
