"""Headless current-lyric tracker.

Feeds "the line under the playhead right now" to non-UI consumers —
today that's Discord rich presence (live synced lyric in the status).
The lyrics *panel* (ui/lyrics.py) can't serve this role: it only tracks
the active line while it's the visible view, so a presence wired to it
would freeze the moment the user switches tabs.

Fetching mirrors the panel's provider chain (track's own source first,
LRClib as the community fallback), and lyrics_provider's on-disk cache
means an open lyrics panel + this tracker cost one network fetch, not
two.

Emissions: ``lyric_changed(str | None)``. None means "no current line" —
pre-first-line intro, instrumental gap (empty LRC line), untimed/missing
lyrics, disabled, or no track — so consumers can fall back to whatever
they normally display.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from . import lyrics_provider, qthreads

if TYPE_CHECKING:
    from .player import Player
    from .queue import Queue


class _FetchWorker(QObject):
    done = Signal(str, object)   # video_id, LyricsResult | None

    def __init__(self, api_obj, track) -> None:
        super().__init__()
        self._api = api_obj
        self._track = track

    def run(self) -> None:
        result = None
        try:
            result = self._api.get_lyrics_for_track(self._track)
        except Exception:
            result = None
        if result is None or not getattr(result, "has_timed", False):
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
        self._thread: QThread | None = None
        self._worker: _FetchWorker | None = None

    def start_wire(self) -> None:
        self._queue.current_changed.connect(self._on_current_changed)
        self._player.position_changed.connect(self._on_position_changed)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._times = []
            self._lines = []
            self._emit(None)
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
        self._times = []
        self._lines = []
        self._emit(None)
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
        idx = bisect_right(self._times, float(secs)) - 1
        text = self._lines[idx].strip() if idx >= 0 else ""
        self._emit(text or None)

    # ---------- internals ----------

    def _emit(self, value: str | None) -> None:
        # Dedupe on the emitted *value*: repeated identical lines (LRC
        # "yeah / yeah") change the index but not what a consumer shows.
        if value == self._emitted:
            return
        self._emitted = value
        self.lyric_changed.emit(value)

    def _fetch(self, track) -> None:
        thread = QThread()
        worker = _FetchWorker(self._track_api(track), track)
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
        # No immediate emit: mpv's position ticks are sub-second, so the
        # current line lands on the next tick (and while paused there's
        # nothing to show anyway — presence is hidden).
