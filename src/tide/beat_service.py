"""beat map service: one worker, one cache, maps delivered as they land."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading

from PySide6.QtCore import QObject, Signal

from . import beat_map, cache
from .beat_map import BeatMap


NAMESPACE = "beat-maps-v1"
# analysis is a pure function of the audio, so a map stays right for as
# long as the track does
TTL_SECONDS = 90 * 24 * 3600
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_QUEUED = 8
IDLE_EXIT_S = 20.0   # an idle worker leaves; the next request starts a fresh one


@dataclass
class _Job:
    key: str
    payload: str | None
    headers: dict | None
    urgent: bool


class BeatMapService(QObject):
    """hands out beat maps by track key: from the cache when it has one,
    otherwise by decoding and analyzing the stream. partial maps arrive
    while the decode runs and the final one is cached. one analysis at a
    time; an urgent request (the track playing now) jumps the queue and
    cancels an in-flight analysis of anything else."""

    updated = Signal(str, object)   # key, BeatMap (partial or final)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: deque[_Job] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._current: str | None = None
        self._urgent: str | None = None

    def request(self, key: str | None, payload: str | None = None,
                headers: dict | None = None, *, urgent: bool = False) -> None:
        """ask for ``key``'s map. ``payload`` is what to decode if the cache
        has nothing: an http(s) url or a local path. None means cache only."""
        if self._closed or not key:
            return
        with self._lock:
            if urgent:
                self._urgent = key
                # a skipped track's pending analysis is not worth the download
                self._jobs = deque(j for j in self._jobs if j.key == key or not j.urgent)
            for job in self._jobs:
                if job.key == key:
                    if job.payload is None and payload is not None:
                        job.payload, job.headers = payload, headers
                    if urgent and not job.urgent:
                        job.urgent = True
                        self._jobs.remove(job)
                        self._jobs.appendleft(job)
                    break
            else:
                if self._current == key and not urgent:
                    return
                job = _Job(key, payload, headers, urgent)
                if urgent:
                    self._jobs.appendleft(job)
                elif len(self._jobs) < MAX_QUEUED:
                    self._jobs.append(job)
                else:
                    return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="tide-beat-map", daemon=True)
                self._thread.start()
        self._wake.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    # ---------- worker ----------

    def _pop(self) -> _Job | None:
        with self._lock:
            if self._jobs:
                job = self._jobs.popleft()
                self._current = job.key
                return job
            self._current = None
            return None

    def _should_stop(self, job: _Job) -> bool:
        if self._closed:
            return True
        with self._lock:
            return self._urgent is not None and self._urgent != job.key

    def _emit(self, key: str, result: BeatMap) -> None:
        if not self._closed:
            self.updated.emit(key, result)

    def _run(self) -> None:
        idle = 0.0
        while not self._closed:
            job = self._pop()
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
                    if self._urgent == job.key:
                        self._urgent = None
                    if self._current == job.key:
                        self._current = None

    def _serve(self, job: _Job) -> None:
        cached = BeatMap.from_payload(cache.get_blob_json(NAMESPACE, job.key))
        if cached is not None:
            self._emit(job.key, cached)
            if cached.complete:
                return
        if job.payload is None or not beat_map.is_analyzable(job.payload):
            return
        # a request that arrives while the job sits queued may have made it
        # urgent, or given it a payload; from here the job object is ours
        if self._should_stop(job) and not job.urgent:
            return
        final = beat_map.analyze_target(
            job.payload, job.headers,
            on_partial=lambda partial: self._emit(job.key, partial),
            should_stop=lambda: self._should_stop(job))
        if final is None or self._closed:
            return
        cache.put_blob_json(NAMESPACE, job.key, final.payload(),
                            TTL_SECONDS, MAX_PAYLOAD_BYTES)
        self._emit(job.key, final)
