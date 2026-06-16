"""Stream-URL prefetch.

Most of the perceived "loading" gap between clicking next and hearing audio
in tide is the source's ``resolve_stream`` call — typically a yt-dlp /
ytmusic network round-trip costing 0.5–2 seconds. The mpv buffer is usually
under a second. If we can resolve the next track's URL while the current
one is still playing, ``_play_track`` finds a cache hit and goes straight
to ``player.load_ref``, skipping the worker entirely.

Design:
  * In-memory cache keyed by ``track.video_id`` → ``(StreamRef, expires_at)``.
    yt-dlp URLs typically last several hours, so a ~1h TTL is conservative.
  * In-flight dedupe via a small set so requesting the same track twice
    (e.g. the position tick fires every second past the threshold) doesn't
    spawn a second worker.
  * Silent failure mode. If a resolve raises, the entry is simply not cached
    and ``_play_track``'s normal path takes over with a fresh worker. There
    is no failure mode that's worse than today's behavior.
  * Lives on the GUI thread. The worker QThreads it spawns do the actual
    network work; their completion signals marshal results back via Qt's
    signal/slot queuing.
"""
from __future__ import annotations

import time
from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from ..sources import registry as source_registry

if TYPE_CHECKING:
    from ..api import Track
    from ..sources import StreamRef


# Conservatively below yt-dlp's typical 6h URL expiry. After this, the
# cached entry is dropped and lookup() falls back to a cache miss.
DEFAULT_TTL_SEC = 60 * 60  # 1 hour


class _PrefetchWorker(QObject):
    """Mirror of window._ResolveWorker but local to the prefetch system so
    we don't depend on the UI module. Emits the same shape so the resolve
    output is uniform."""

    resolved = Signal(str, object)   # video_id, StreamRef
    failed = Signal(str, str)        # video_id, msg

    def __init__(self, track: "Track") -> None:
        super().__init__()
        self.track = track
        self.video_id = track.video_id

    def run(self) -> None:
        try:
            source = source_registry().get(self.track.source or "ytmusic")
            if source is None:
                raise RuntimeError(f"no source registered for {self.track.source!r}")
            ref = source.resolve_stream(self.track)
            self.resolved.emit(self.video_id, ref)
        except Exception as exc:
            self.failed.emit(self.video_id, str(exc))


class StreamPrefetch(QObject):
    """Pre-resolves stream URLs for upcoming tracks. Holds an in-memory cache
    keyed by ``video_id`` so ``lookup`` is constant-time."""

    # Fired whenever a prefetch successfully resolves — purely informational,
    # for tests / status indicators that want to react to a warm cache.
    resolved = Signal(str)   # video_id

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: dict[str, tuple["StreamRef", float]] = {}
        # In-flight video_ids — guards request() against spawning duplicate
        # workers for the same track. Cleared in _on_resolved/_on_failed.
        self._inflight: set[str] = set()
        # NOTE: do NOT keep Python-side refs to QThread/_PrefetchWorker. They
        # live on Qt's parent-child tree via ``QThread(self)`` and the
        # worker.moveToThread() association, and self-delete on finish via
        # deleteLater. Holding our own dict refs and popping them on
        # thread.finished caused a use-after-free segfault in PySide6 — the
        # destructor processed deleteLater while our pop was concurrently
        # invalidating the Python proxy.

    # ---------- public ----------

    def lookup(self, video_id: str) -> Optional["StreamRef"]:
        """Return a cached StreamRef for ``video_id`` if present and not
        expired, else None."""
        entry = self._cache.get(video_id)
        if entry is None:
            return None
        ref, expires_at = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(video_id, None)
            return None
        return ref

    def request(self, track: "Track") -> None:
        """Kick off a background resolve for ``track`` unless it's already
        cached or in-flight. Idempotent — safe to call from a position-tick
        every frame."""
        if track is None:
            return
        vid = track.video_id
        if not vid:
            return
        if vid in self._inflight:
            return
        if self.lookup(vid) is not None:
            return

        self._inflight.add(vid)
        # Qt-owned QThread + worker. Parent on the thread keeps it alive
        # while running; moveToThread associates the worker with the thread
        # for slot dispatch; both deleteLater on finish so Qt's event loop
        # destructs them at a safe moment.
        thread = QThread(self)
        worker = _PrefetchWorker(track)
        worker.setParent(None)  # required before moveToThread
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resolved.connect(self._on_resolved)
        worker.failed.connect(self._on_failed)
        worker.resolved.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def invalidate(self, video_id: str) -> None:
        """Drop a single cached entry. Used when a previous lookup turned
        out to be stale (e.g. mpv failed to load the cached URL)."""
        self._cache.pop(video_id, None)

    def clear(self) -> None:
        """Drop the entire cache. Useful on source/account switches where
        URL signatures from one identity may not work with another."""
        self._cache.clear()

    def shutdown(self, wait_ms: int = 2000) -> None:
        """Quit all in-flight resolver threads and wait briefly for them to
        exit. Called from app.py on app shutdown — if a network resolve is
        mid-flight when the window destructs, the parent's destructor would
        otherwise tear down a still-running QThread and segfault.

        Threads live as Qt children, so we discover them via
        ``findChildren`` rather than a Python-side dict — that's the same
        list of objects Qt knows about, so we can't miss one or stale-ref
        one that's already destructed.
        """
        threads: list[QThread] = list(self.findChildren(QThread))
        for t in threads:
            try:
                t.quit()
            except Exception:
                pass
        for t in threads:
            try:
                # Cap each wait so a stuck yt-dlp call doesn't block exit.
                # Qt will terminate any survivor when its parent destructs.
                t.wait(wait_ms)
            except Exception:
                pass
        self._inflight.clear()
        self._cache.clear()

    # ---------- internals ----------

    def _on_resolved(self, video_id: str, ref) -> None:
        self._cache[video_id] = (ref, time.monotonic() + DEFAULT_TTL_SEC)
        self._inflight.discard(video_id)
        self.resolved.emit(video_id)

    def _on_failed(self, video_id: str, _msg: str) -> None:
        # No retry — _play_track will spawn its own worker on cache miss.
        # Silent failure is intentional: prefetch is best-effort.
        self._inflight.discard(video_id)
