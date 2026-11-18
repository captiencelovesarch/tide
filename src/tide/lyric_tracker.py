"""Headless current-lyric tracker.

Feeds "the line under the playhead right now" to non-UI consumers —
today that's Discord rich presence (live synced lyric in the status).
The lyrics *panel* (ui/lyrics.py) can't serve this role: it only tracks
the active line while it's the visible view, so a presence wired to it
would freeze the moment the user switches tabs.

Fetching is the panel's own (lyrics_provider.fetch_from: the track's
source first, LRClib as the community fallback, or the source picked
for the song), shared with any open lyrics view, so the two cost one
network fetch, not two.

Emissions: ``timeline_changed`` with the whole timed sheet (presence
schedules ahead from it), and ``lyric_changed(str | None)``. None means "no current line" —
pre-first-line intro, instrumental gap (empty LRC line), untimed/missing
lyrics, disabled, or no track — so consumers can fall back to whatever
they normally display.

``break_changed`` says when a break is up: the stretch the lyrics views
draw their three dots over (tide.lyric_breaks), as ``(begin, end)`` in
song time, or None. ``break_at`` answers the same for any moment, for
consumers that look ahead.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from . import lyric_breaks, lyric_timing, lyric_words, lyrics_provider, qthreads

if TYPE_CHECKING:
    from .player import Player
    from .queue import Queue


class _FetchWorker(QObject):
    done = Signal(str, object)   # video_id, LyricsResult | None

    def __init__(self, api_obj, track, source: str | None = None) -> None:
        super().__init__()
        self._api = api_obj
        self._track = track
        self._source = source

    def run(self) -> None:
        # The same fetch the lyrics views make (lyrics_provider.fetch_from
        # is shared, so an open view and this tracker cost one), and the
        # source picked for the song in the lyrics menu, if any.
        result = None
        try:
            result = lyrics_provider.fetch_from(self._api, self._track, self._source)
        except Exception:
            result = None
        # (a picked source is final, even without timing)
        if self._source is None and (result is None or not getattr(result, "has_timed", False)):
            # Source has no timed lyrics of its own (base sources return
            # None outright) — ask LRClib directly. fetch_lrclib caches
            # to disk, so re-listens and the lyrics panel don't re-query.
            try:
                lrc = lyrics_provider.fetch_lrclib(
                    title=self._track.title or "",
                    artist=self._track.artists or "",
                    album=self._track.album or "",
                    duration_seconds=int(self._track.duration_seconds or 0),
                )
            except Exception:
                lrc = None
            if lrc is not None and lrc.has_timed:
                result = lrc
        self.done.emit(self._track.video_id, result)


class LyricTracker(QObject):
    """Watches queue + playback position; emits the active timed line."""

    lyric_changed = Signal(object)   # str | None
    # The whole timed sheet as (times, lines), or None when there is none
    # (off, no track, untimed). Discord presence schedules from this, so it
    # can push a line before it's sung and fold fast lines together.
    timeline_changed = Signal(object)
    # (begin, end) of the break under the playhead, in song time, or None
    break_changed = Signal(object)

    def __init__(self, api_obj, player: "Player", queue: "Queue",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api = api_obj
        self._player = player
        self._queue = queue

        self._enabled = False
        self._video_id: str | None = None
        # Parallel arrays from LyricsResult.timed_lines, kept split so the
        # per-tick lookup is a plain bisect over floats.
        self._times: list[float] = []
        self._lines: list[str] = []
        self._emitted: str | None = None
        # Where the breaks are (lyric_breaks): when each line's singing
        # ends, which lines have no words, the next line with some.
        self._track = None
        self._result = None
        self._ends: list = []
        self._blank: list[bool] = []
        self._next: list = []
        self._break: tuple | None = None
        self._thread: QThread | None = None
        self._worker: _FetchWorker | None = None

    def start_wire(self) -> None:
        self._queue.current_changed.connect(self._on_current_changed)
        self._player.position_changed.connect(self._on_position_changed)
        # word timing found where lines really end (a lyrics view asked)
        lyric_timing.service().updated.connect(self._on_sheet)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._clear_sheet()
            self._emit(None)
            self.timeline_changed.emit(None)
            return
        # Turned on mid-listen: fetch for whatever is already playing.
        # The line itself lands on the next position tick after the
        # fetch resolves.
        current = self._queue.current
        if current is not None:
            self._video_id = current.video_id
            self._fetch(current)

    # ---------- signal handlers ----------

    def _on_current_changed(self, track) -> None:
        self._clear_sheet()
        self._emit(None)
        self.timeline_changed.emit(None)
        if track is None:
            self._video_id = None
            return
        self._video_id = track.video_id
        if self._enabled:
            self._fetch(track)

    def _on_position_changed(self, secs: float) -> None:
        if not self._times:
            return
        # Latest line whose timestamp <= position. Bisect (not a walk from
        # the previous index) so seeks in either direction just work.
        pos = float(secs) + lyric_timing.offset_s()
        idx = bisect_right(self._times, pos) - 1
        text = self._lines[idx].strip() if idx >= 0 else ""
        self._emit(text or None)
        self._emit_break(self._break_for(idx, pos))

    # ---------- breaks ----------

    def break_at(self, secs: float) -> tuple | None:
        """(begin, end) of the break at song time ``secs``, or None."""
        if not self._times:
            return None
        pos = float(secs) + lyric_timing.offset_s()
        span = self._break_for(bisect_right(self._times, pos) - 1, pos)
        if span is not None and self._break is not None and span[1] == self._break[1]:
            return self._break      # the one that's up: same start as announced
        return span

    def _break_for(self, idx: int, pos: float) -> tuple | None:
        _lit, gap = lyric_breaks.visual(idx, pos, self._times, self._ends,
                                        self._blank, self._next)
        if gap is None:
            return None
        off = lyric_timing.offset_s()
        return (gap[1] - off, gap[2] - off)

    def _emit_break(self, span: tuple | None) -> None:
        # one break is known by where it ends (the line it leads to)
        if (span is None) == (self._break is None) and (
                span is None or span[1] == self._break[1]):
            return
        self._break = span
        self.break_changed.emit(span)

    def _clear_sheet(self) -> None:
        self._times, self._lines = [], []
        self._track = self._result = None
        self._ends, self._blank, self._next = [], [], []
        self._emit_break(None)

    def _set_ends(self, sheet) -> None:
        """Line ends from a word sheet with the same lines; else only what
        the source said (lyric_breaks.real_line_ends)."""
        if not sheet or len(sheet) != len(self._times):
            sheet = lyric_words.line_sheet(
                list(zip(self._times, self._lines)),
                list(getattr(self._result, "line_ends", []) or []))
        self._ends = lyric_breaks.real_line_ends(sheet, self._result)

    def _on_sheet(self, key: str, sheet) -> None:
        if (self._track is None or not self._times
                or key != lyric_timing.track_key(self._track)):
            return
        self._set_ends(sheet)

    # ---------- internals ----------

    def _emit(self, value: str | None) -> None:
        # Dedupe on the emitted *value*: repeated identical lines (LRC
        # "yeah / yeah") change the index but not what a consumer shows.
        if value == self._emitted:
            return
        self._emitted = value
        self.lyric_changed.emit(value)

    def _fetch(self, track) -> None:
        self._track = track
        thread = QThread()
        from . import lyrics_picks
        worker = _FetchWorker(self._track_api(track), track,
                              lyrics_picks.source(lyric_timing.track_key(track)))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_fetched)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        qthreads.retain(thread, worker)
        thread.start()

    def _track_api(self, track):
        """Route lyrics through the track's own source when the registry
        knows it (a spotify track shouldn't ask the ytmusic client); fall
        back to the app-level api object like the lyrics panel does."""
        try:
            from .sources import registry
            src = registry().get(getattr(track, "source", "") or "")
            if src is not None:
                return src
        except Exception:
            pass
        return self._api

    def _on_fetched(self, video_id: str, result) -> None:
        if not self._enabled or video_id != self._video_id:
            return   # stale — user skipped on / toggled off mid-fetch
        if result is None or not getattr(result, "has_timed", False):
            return
        self._times = [float(t) for t, _line in result.timed_lines]
        self._lines = [line for _t, line in result.timed_lines]
        self._result = result
        self._blank = [lyric_breaks.is_blank(line) for line in self._lines]
        self._next = lyric_breaks.next_sung(self._blank)
        # the best sheet known, without starting word timing for it
        self._set_ends(lyric_timing.service().peek(self._track, result)
                       if self._track is not None else None)
        self.timeline_changed.emit((list(self._times), list(self._lines)))
        # No immediate emit: mpv's position ticks are sub-second, so the
        # current line lands on the next tick (and while paused there's
        # nothing to show anyway — presence is hidden).
