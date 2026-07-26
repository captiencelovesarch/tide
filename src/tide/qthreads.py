"""Lifetime management for worker QThreads.

Why this module exists
---------------------
PySide6 gives *Python* ownership of a QObject built without a parent, so the
C++ object is ``delete``d the moment the last Python reference to its wrapper
goes away. A worker that has been moved onto a QThread therefore has to stay
referenced until that thread is completely gone. Drop the reference earlier
and Python frees the C++ worker from the GUI thread while the worker's own
thread is still dispatching events to it — a use-after-free that surfaces as
SIGSEGV inside ``QObject::~QObject``, usually while ``QThreadPrivate::finish``
drains the thread's DeferredDelete queue, with Qt printing::

    QObject: shared QObject was deleted directly. The program is malformed
    and may crash.

immediately beforehand.

Signal connections do **not** count as references: PySide6 keeps only a *weak*
reference to a bound-method slot, so ``thread.started.connect(worker.run)``
holds nothing alive.

Per-instance attributes (``self._thread`` / ``self._worker``) are not enough
either — the next call overwrites them and drops the previous worker while its
thread is still running. That is what crashed tide on a fast track change
(lyric fetch), on overlapping prefetches, and on a quick second click into a
library/album/artist page.

So every ``(thread, worker)`` pair lives in the registry below until the
QThread emits ``destroyed``, which happens on the GUI thread *after*
``worker.deleteLater()`` has already run on the worker's own thread — the only
thread allowed to destroy it.

Threads are also deliberately left **unparented**. Destroying a QThread whose
OS thread is still running is a Qt fatal abort, so a worker thread must never
be a child of a widget that can be torn down under it. Late signals from a
worker whose receiver has already been destroyed are auto-disconnected by Qt
and harmlessly dropped.

Usage::

    thread = QThread()                     # never QThread(self)
    worker = _Worker(...)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.done.connect(self._on_done)
    worker.done.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    qthreads.retain(thread, worker)        # before start()
    thread.start()
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread

# Strong refs to in-flight (thread, worker) pairs, tagged with a group name so
# a subsystem can wait out just its own threads at shutdown.
_LIVE: set[tuple[QThread, QObject, str]] = set()


def retain(thread: QThread, worker: QObject, group: str = "") -> None:
    """Hold strong refs to ``thread`` and ``worker`` until the thread is
    destroyed. Call once, before ``thread.start()``.

    ``group`` is an optional tag for :func:`join`.
    """
    entry = (thread, worker, group)
    _LIVE.add(entry)
    try:
        thread.destroyed.connect(lambda *_: _LIVE.discard(entry))
    except RuntimeError:
        # Wrapper already invalid — nothing worth retaining.
        _LIVE.discard(entry)


def live_count(group: str | None = None) -> int:
    """Number of retained pairs, optionally limited to one ``group``. Drains
    back toward zero as threads finish; used by tests and diagnostics."""
    if group is None:
        return len(_LIVE)
    return sum(1 for _t, _w, g in _LIVE if g == group)


def join(group: str | None = None, wait_ms: int = 2000) -> None:
    """Ask retained worker threads to quit, then wait briefly for each.

    Called at shutdown so an in-flight network resolve can't have its thread
    torn down mid-run. Each wait is capped so one stuck worker can't block
    exit; anything still running is left to the process teardown.

    ``group`` limits the sweep to threads retained under that tag; ``None``
    sweeps every retained thread.
    """
    entries = [e for e in list(_LIVE) if group is None or e[2] == group]
    for thread, _worker, _g in entries:
        try:
            thread.quit()
        except RuntimeError:
            pass
    for thread, _worker, _g in entries:
        try:
            thread.wait(wait_ms)
        except RuntimeError:
            pass
