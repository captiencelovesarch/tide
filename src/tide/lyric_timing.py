"""Word timing for the playing track's lyrics: one service, shared by
every lyrics surface (the panel, the mini, fullscreen, the visualizer's
lyrics tile).

A LyricsView hands over the line-timed LyricsResult it fetched;
:meth:`WordTimingService.sheet_for` answers at once with the best sheet
known (the cache, or line mode) and, when it can do better, upgrades it
in the background, delivering each stage through ``updated``:

  1. **source words** — the result's own word sheet (LRClib lyricsfile /
     enhanced LRC), else NetEase's YRC, matched onto the video's lines by
     text and shifted by the median offset (lyric_words.adopt_source_words);
  2. **precise timing** (optional, lyric_align) — the track's audio,
     aligned against the text: re-times a sheet that sits clearly off and
     places words on lines that still have none.

Lines nobody could time stay in line mode (words spread by length across
the line window), so karaoke always has something to sweep.

Settings are read through a provider so the service stays Qt-light and
testable: ``lyrics_word_timing`` ("sources" | "lines"),
``lyrics_netease`` (bool), ``lyrics_precise`` (bool).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, Signal

from . import cache, lyric_words
from .lyric_words import WordLine

NAMESPACE = "lyric-words-v1"
TTL_SECONDS = 180 * 24 * 3600
MAX_PAYLOAD_BYTES = 1024 * 1024
IDLE_EXIT_S = 20.0
STREAM_WAIT_S = 30.0


# The "lyrics offset" setting, in seconds to ADD to the playhead before
# looking lyrics up (a positive ms setting = lyrics later = a negative
# value here). Lives here, not in the ui, so the headless lyric tracker
# (mini ticker, plugins) follows it too. Set by ui.lyrics.set_offset_ms.
_offset_s = 0.0


def set_offset_s(value: float) -> None:
    global _offset_s
    _offset_s = float(value)


def offset_s() -> float:
    return _offset_s


def track_key(track) -> str | None:
    vid = getattr(track, "video_id", None)
    if not isinstance(vid, str) or not vid:
        return None
    return f"{getattr(track, 'source', '') or 'ytmusic'}:{vid}"


def _text_hash(result) -> str:
    h = hashlib.sha1()
    for t, line in getattr(result, "timed_lines", []) or []:
        h.update(f"{float(t):.2f}|{line}\n".encode("utf-8", "replace"))
    return h.hexdigest()[:16]


@dataclass
class _Job:
    key: str
    track: object
    result: object
    fingerprint: str
    sources: bool
    netease: bool
    precise: bool


class WordTimingService(QObject):
    updated = Signal(str, object)     # track key, list[WordLine]
    status = Signal(str, str)         # track key, short status text ("" = idle)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings_provider: Callable[[], object] | None = None
        self._stream_provider: Callable[[object], object] | None = None
        self._jobs: deque[_Job] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._current: str | None = None
        self._sheets: dict[str, tuple[str, list[WordLine]]] = {}
        # test seams
        self.fetch_netease = None
        self.align = None

    # ---------- wiring ----------

    def set_settings_provider(self, fn: Callable[[], object] | None) -> None:
        self._settings_provider = fn

    def set_stream_provider(self, fn: Callable[[object], object] | None) -> None:
        """``fn(track)`` → the StreamRef playing that track, or None."""
        self._stream_provider = fn

    def close(self) -> None:
        """Stop the worker (it checks between lines, and an inference
        chunk takes ~2 s). Called at app exit: a daemon thread inside
        onnxruntime at interpreter teardown can crash."""
        self._closed = True
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=4.0)

    # ---------- public ----------

    def _flags(self) -> tuple[bool, bool, bool]:
        s = self._settings_provider() if self._settings_provider is not None else None
        sources = str(getattr(s, "lyrics_word_timing", "sources") or "sources") != "lines"
        netease = bool(getattr(s, "lyrics_netease", True))
        precise = bool(getattr(s, "lyrics_precise", False))
        return sources, netease, precise

    def sheet_for(self, track, result) -> list[WordLine]:
        """The best sheet known now; schedules the upgrades."""
        timed = list(getattr(result, "timed_lines", []) or [])
        base = lyric_words.line_sheet(timed, list(getattr(result, "line_ends", []) or []))
        key = track_key(track)
        if key is None:
            return base
        sources, netease, precise = self._flags()
        if precise:
            from . import lyric_align
            precise = lyric_align.available()
        fp = f"{_text_hash(result)}|{int(sources)}{int(netease)}{int(precise)}"
        mem = self._sheets.get(key)
        if mem is not None and mem[0] == fp:
            return mem[1]
        cached = lyric_words.sheet_from_payload(cache.get_blob_json(NAMESPACE, f"{key}|{fp}"))
        if cached:
            self._sheets[key] = (fp, cached)
            return cached
        if not (sources or precise):
            return base
        self._enqueue(_Job(key, track, result, fp, sources, netease, precise))
        return base

    # ---------- worker ----------

    def _enqueue(self, job: _Job) -> None:
        if self._closed:
            return
        with self._lock:
            if self._current == job.key or any(j.key == job.key and j.fingerprint == job.fingerprint
                                               for j in self._jobs):
                return
            # the newest request is the track on screen; older ones are skips
            self._jobs = deque(j for j in self._jobs if j.key != job.key)
            self._jobs.appendleft(job)
            while len(self._jobs) > 4:
                self._jobs.pop()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="tide-lyric-words",
                                                daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        idle = 0.0
        while not self._closed:
            with self._lock:
                job = self._jobs.popleft() if self._jobs else None
                self._current = job.key if job else None
            if job is None:
                if self._wake.wait(timeout=1.0):
                    self._wake.clear()
                    idle = 0.0
                    continue
                idle += 1.0
                if idle >= IDLE_EXIT_S:
                    with self._lock:
                        if not self._jobs:
                            self._thread = None
                            return
                continue
            idle = 0.0
            try:
                self._serve(job)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._current = None
                if not self._closed:
                    self.status.emit(job.key, "")

    def _superseded(self, job: _Job) -> bool:
        if self._closed:
            return True
        with self._lock:
            return any(j.key != job.key for j in self._jobs)

    def _deliver(self, job: _Job, sheet: list[WordLine], final: bool) -> None:
        # only a finished sheet answers future sheet_for calls: a stage
        # that's still waiting on the aligner must be asked for again
        if final:
            self._sheets[job.key] = (job.fingerprint, sheet)
            cache.put_blob_json(NAMESPACE, f"{job.key}|{job.fingerprint}",
                                [l.payload() for l in sheet], TTL_SECONDS, MAX_PAYLOAD_BYTES)
        if not self._closed:
            self.updated.emit(job.key, sheet)

    def _serve(self, job: _Job) -> None:
        result = job.result
        timed = list(getattr(result, "timed_lines", []) or [])
        sheet = lyric_words.line_sheet(timed, list(getattr(result, "line_ends", []) or []))
        changed = False
        if job.sources:
            sheet, changed = self._source_words(job, sheet)
            if changed:
                self._deliver(job, sheet, final=not job.precise)
        if job.precise and sheet and not self._superseded(job):
            aligned = self._precise(job, sheet)
            if aligned is None:
                # couldn't run (no stream, superseded): this stage stays
                # unfinished, so the next sheet_for asks again
                if changed:
                    self._deliver(job, sheet, final=False)
                return
            sheet = aligned
        # final — also when nothing was found, so a re-listen skips the
        # lookups (the NetEase miss is cached on its own too)
        self._deliver(job, sheet, final=True)

    def _source_words(self, job: _Job, base: list[WordLine]) -> tuple[list[WordLine], bool]:
        """The result's own word sheet first; NetEase only when that's
        missing or doesn't match the lines."""
        def adopt(words: list[WordLine]) -> list[WordLine] | None:
            if not words:
                return None
            if not base:
                # untimed lyrics: a duration-matched word sheet is all we have
                return list(words)
            adopted = lyric_words.adopt_source_words(base, words)
            return adopted[0] if adopted is not None else None

        got = adopt(list(getattr(job.result, "word_lines", []) or []))
        if got is None and job.netease and not self._superseded(job):
            got = adopt(self._netease(job.track) or [])
        return (got, True) if got is not None else (base, False)

    def _netease(self, track) -> list[WordLine] | None:
        fetch = self.fetch_netease
        if fetch is None:
            from .lyrics_netease import fetch_yrc as fetch
        try:
            return fetch(title=getattr(track, "title", "") or "",
                         artist=getattr(track, "artists", "") or "",
                         duration_seconds=float(getattr(track, "duration_seconds", 0) or 0))
        except Exception:
            return None

    def _precise(self, job: _Job, sheet: list[WordLine]) -> list[WordLine] | None:
        if self.align is not None:
            return self.align(job, sheet)
        from . import lyric_align
        if not lyric_align.available() or self._stream_provider is None:
            return None
        # lyrics usually land before the stream resolves: wait for it
        ref = None
        for _ in range(int(STREAM_WAIT_S / 0.5)):
            try:
                ref = self._stream_provider(job.track)
            except Exception:
                ref = None
            if ref is not None or self._superseded(job):
                break
            time.sleep(0.5)
        payload = getattr(ref, "payload", None) if ref is not None else None
        if not payload or getattr(ref, "backend", "mpv") != "mpv":
            return None
        self.status.emit(job.key, "timing words to the audio…")
        stop = lambda: self._superseded(job)
        stereo = lyric_align.decode(payload, getattr(ref, "headers", None), should_stop=stop)
        if stereo is None or stop():
            return None
        em = lyric_align.Emissions(lyric_align.vocals(stereo))
        fit = lyric_align.align_sheet(sheet, em, should_stop=stop)
        if fit is None:
            return None
        return fit.sheet


_service: WordTimingService | None = None


def service() -> WordTimingService:
    global _service
    if _service is None:
        _service = WordTimingService()
    return _service
