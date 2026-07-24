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

from PySide6.QtCore import QModelIndex, QObject, QThread, QTimer, Qt, Signal

from ..sources import registry as source_registry

if TYPE_CHECKING:
    from ..api import Track
    from ..sources import StreamRef


# Conservatively below yt-dlp's typical 6h URL expiry. After this, the
# cached entry is dropped and lookup() falls back to a cache miss.
DEFAULT_TTL_SEC = 60 * 60  # 1 hour

# Sources whose resolve_stream is a real network round-trip (yt-dlp
# extraction). Warming anything else (local paths, subsonic's computed
# URL, spotify's URI) is pure waste — those resolve instantly.
_SLOW_RESOLVE_SOURCES = frozenset({"ytmusic", "soundcloud", "bandcamp", "mixcloud"})


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
    # Fired when an in-flight prefetch raises. Lets _play_track join an
    # in-flight resolve and still learn about failure (it falls back to its
    # own worker). Prefetch itself stays best-effort/silent otherwise.
    failed = Signal(str, str)   # video_id, msg

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: dict[str, tuple["StreamRef", float]] = {}
        # In-flight video_ids — guards request() against spawning duplicate
        # workers for the same track. Cleared in _on_resolved/_on_failed.
        self._inflight: set[str] = set()
        # Fire-time master switch for pointer-driven prefetch (hover +
        # press). A settings toggle lands here — checked when a hover/press
        # fires, NOT at wire time, because settings are injected after the
        # window (and its view wiring) is constructed.
        self.hover_enabled: bool = True
        # Pending warm() list — a single staggered timer chain, replaced
        # wholesale by each new warm() call.
        self._warm_queue: list = []
        self._warm_spacing_ms: int = 500
        self._warm_timer = QTimer(self)
        self._warm_timer.setSingleShot(True)
        self._warm_timer.timeout.connect(self._on_warm_fire)
        # Strong Python refs to live (thread, worker) pairs. REQUIRED:
        # PySide6 signal connections hold only weak references to
        # bound-method slots, so without these refs the worker's Python
        # wrapper (and with it the C++ object — the worker is unparented)
        # is garbage-collected the moment request() returns, and
        # ``thread.started -> worker.run`` never fires. That silently
        # no-op'd every prefetch in v1.2.4. Entries are reaped on the GUI
        # thread via the bound-method slot _reap_spawns — never from a
        # lambda, which PySide6 runs in the *emitting* (worker) thread and
        # which is how the original ref-holding attempt segfaulted.
        self._spawns: set = set()

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

    def is_inflight(self, video_id: str) -> bool:
        """True while a background resolve for ``video_id`` is running."""
        return video_id in self._inflight

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
        # QThread parented on us + unparented worker moved onto it. The
        # _spawns entry keeps both Python wrappers alive while the thread
        # runs (see __init__); deleteLater on finish lets Qt destruct the
        # C++ side at a safe moment, and _reap_spawns then drops the refs.
        thread = QThread(self)
        worker = _PrefetchWorker(track)
        worker.setParent(None)  # required before moveToThread
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.resolved.connect(self._on_resolved)
        worker.failed.connect(self._on_failed)
        worker.resolved.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._reap_spawns)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._spawns.add((thread, worker))
        thread.start()

    def warm(self, tracks: list["Track"], limit: int = 5) -> None:
        """Stagger-prefetch the first ``limit`` of ``tracks`` (e.g. the
        visible top of a result list). Replaces any pending warm list
        wholesale — a new context (new search, new album page) makes the
        old one moot. Tracks fire one per ``_warm_spacing_ms`` so we
        never burst yt-dlp round-trips.
        """
        self._warm_timer.stop()
        queue: list = []
        seen: set[str] = set()
        for t in tracks:
            if len(queue) >= limit:
                break
            if t is None or not t.video_id or t.video_id in seen:
                continue
            if (t.source or "ytmusic") not in _SLOW_RESOLVE_SOURCES:
                continue
            seen.add(t.video_id)
            queue.append(t)
        self._warm_queue = queue
        if self._warm_queue:
            self._on_warm_fire()

    def attach_hover(self, view, debounce_ms: int = 300) -> None:
        """Wire mouse-hover prefetch to a track-bearing QListView.

        Mouseover on a row warms its URL after a short debounce so a
        subsequent click on that row hits the cache instantly. Reused
        from every view that renders ``TrackRowDelegate`` (search
        results, queue, library, history, album, artist).

        Idempotent — calling twice on the same view is harmless because
        the second `entered` connection just races the first into the
        same dedupe-by-video_id pipeline.
        """
        if view is None:
            return
        try:
            view.viewport().setMouseTracking(True)
        except Exception:
            return
        # Per-view debounce timer parented on us so it survives the
        # view's lifetime if the view is reparented or hidden, and
        # tears down cleanly when the prefetcher shuts down.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(debounce_ms)
        # Single-cell list as a closure-mutable container — avoids
        # `nonlocal` and the noqa Qt closures sometimes need.
        pending: list = [None]

        def _on_entered(idx: QModelIndex) -> None:
            from ..api import Track
            if not idx.isValid():
                return
            track = idx.data(Qt.UserRole)
            if not isinstance(track, Track):
                return
            pending[0] = track
            timer.start()

        def _on_fire() -> None:
            tr = pending[0]
            if tr is not None and self.hover_enabled:
                self.request(tr)

        try:
            view.entered.connect(_on_entered)
        except Exception:
            return
        timer.timeout.connect(_on_fire)

    def attach_press(self, view) -> None:
        """Wire mouse-press prefetch to a track-bearing QListView.

        Debounce-free sibling of ``attach_hover``: the resolve starts the
        instant the button goes down, so by the time the release/activation
        reaches ``_play_track`` the resolve is already in flight and the
        window joins it instead of spawning its own worker. Costs zero
        extra network — a press is always followed by a play.
        """
        if view is None:
            return

        def _on_pressed(idx: QModelIndex) -> None:
            from ..api import Track
            if not self.hover_enabled:
                return
            if not idx.isValid():
                return
            track = idx.data(Qt.UserRole)
            if isinstance(track, Track):
                self.request(track)

        try:
            view.pressed.connect(_on_pressed)
        except Exception:
            return

    def invalidate(self, video_id: str) -> None:
        """Drop a single cached entry. Used when a previous lookup turned
        out to be stale (e.g. mpv failed to load the cached URL)."""
        self._cache.pop(video_id, None)

    def clear(self) -> None:
        """Drop the entire cache. Useful on source/account switches where
        URL signatures from one identity may not work with another."""
        self._warm_timer.stop()
        self._warm_queue.clear()
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
        self._warm_timer.stop()
        self._warm_queue.clear()
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
        self._spawns.clear()
        self._inflight.clear()
        self._cache.clear()

    # ---------- internals ----------

    def _on_resolved(self, video_id: str, ref) -> None:
        self._cache[video_id] = (ref, time.monotonic() + DEFAULT_TTL_SEC)
        self._inflight.discard(video_id)
        self.resolved.emit(video_id)

    def _on_failed(self, video_id: str, msg: str) -> None:
        # No retry — _play_track will spawn its own worker on cache miss.
        # Silent failure is intentional: prefetch is best-effort. The signal
        # exists for the window's in-flight join, which does retry.
        self._inflight.discard(video_id)
        self.failed.emit(video_id, msg)

    def _reap_spawns(self) -> None:
        # Runs on the GUI thread (bound-method slot → queued delivery from
        # the finishing thread). Drop refs to pairs whose thread is done;
        # an already-deleted C++ side (RuntimeError) counts as done.
        finished = set()
        for entry in self._spawns:
            thread, _worker = entry
            try:
                if thread.isFinished():
                    finished.add(entry)
            except RuntimeError:
                finished.add(entry)
        self._spawns -= finished

    def _on_warm_fire(self) -> None:
        # Pop until we find a track that still needs resolving, request it,
        # and chain the timer for the remainder of the queue.
        while self._warm_queue:
            track = self._warm_queue.pop(0)
            if self.is_inflight(track.video_id):
                continue
            if self.lookup(track.video_id) is not None:
                continue
            self.request(track)
            if self._warm_queue:
                self._warm_timer.start(self._warm_spacing_ms)
            return
