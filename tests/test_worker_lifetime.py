"""Worker-thread lifetime guarantees (tide.qthreads).

Regression cover for the use-after-free that crashed tide on launch and
mid-playback: an unparented worker moved onto a QThread is owned by *Python*,
so dropping its last Python reference frees the C++ object from the GUI thread
while the worker's own thread is still dispatching to it. Qt reports it as
"QObject: shared QObject was deleted directly" followed by SIGSEGV in
QObject::~QObject.

Holding the pair in per-instance attributes is not enough — the next call
overwrites them. These tests pin the two properties that make the registry
safe: it keeps the pair alive while the thread runs, and it lets go afterwards
so nothing leaks.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import subprocess
import sys
import textwrap
import time
import unittest

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication

from tide import qthreads


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _spin(cond, timeout_ms: int = 10000) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return bool(cond())


class _Worker(QObject):
    done = Signal(str)

    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag

    def run(self) -> None:
        self.done.emit(self.tag)


class _Spawner(QObject):
    """Mimics tide's call sites: single-slot attributes for thread/worker,
    which a second call overwrites."""

    def __init__(self) -> None:
        super().__init__()
        self._thread = None
        self._worker = None
        self.seen: list[str] = []

    def spawn(self, tag: str, group: str = "") -> QThread:
        thread = QThread()          # unparented — never QThread(self)
        worker = _Worker(tag)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        qthreads.retain(thread, worker, group=group)
        self._thread = thread
        self._worker = worker
        thread.start()
        return thread

    def _on_done(self, tag: str) -> None:
        self.seen.append(tag)


class RetainTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_worker_runs_even_though_attrs_are_overwritten(self) -> None:
        """The whole point: overlapping spawns must all still deliver.

        Without retention the overwritten worker is collected before its
        thread gets to run it, so ``run`` never fires (tide v1.2.4's silent
        prefetch no-op) or the process crashes.
        """
        s = _Spawner()
        group = "test-overlap"
        for i in range(25):
            s.spawn(f"job-{i}", group=group)
        self.assertTrue(
            _spin(lambda: len(s.seen) == 25),
            f"only {len(s.seen)}/25 workers ran: {s.seen}",
        )

    def test_registry_drains_after_threads_finish(self) -> None:
        """Retention must be temporary — entries are released on
        thread.destroyed, otherwise every spawn leaks a thread + worker."""
        s = _Spawner()
        group = "test-drain"
        for i in range(10):
            s.spawn(f"drain-{i}", group=group)
        self.assertTrue(_spin(lambda: len(s.seen) == 10))
        self.assertTrue(
            _spin(lambda: qthreads.live_count(group) == 0),
            f"registry still holds {qthreads.live_count(group)} pair(s)",
        )

    def test_join_waits_out_its_own_group(self) -> None:
        s = _Spawner()
        for i in range(5):
            s.spawn(f"join-{i}", group="test-join")
        qthreads.join(group="test-join", wait_ms=5000)
        # join() returns only once the OS threads have exited.
        for thread, _worker, _g in list(qthreads._LIVE):
            if _g == "test-join":
                self.assertFalse(thread.isRunning())


class StressSubprocessTest(unittest.TestCase):
    """The in-process tests above cannot detect the failure mode directly: a
    use-after-free takes the whole interpreter down, pytest included. So run
    the overlapping-spawn stress in a child process under an aggressive cyclic
    collector and assert it exits cleanly rather than on a signal.

    With the bug present this child reliably dies with SIGSEGV (-11) inside a
    few hundred spawns; with the registry in place it completes.
    """

    SRC = textwrap.dedent(
        """
        import gc, sys
        from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal
        from tide import qthreads

        class W(QObject):
            done = Signal(str)
            def run(self):
                # Stay busy, so the GUI thread's next spawn -- and the
                # gc.collect() right after it -- lands while this worker is
                # still running on its own thread. That is the window in
                # which freeing the C++ worker is fatal.
                t = 0
                for i in range(60000):
                    t += i
                self.done.emit("x")

        class H(QObject):
            def __init__(self):
                super().__init__()
                self._t = None; self._w = None
                self.spawned = 0; self.done = 0; self.running = 0
            def fetch(self):
                t = QThread(); w = W()
                w.moveToThread(t)
                t.started.connect(w.run)
                w.done.connect(self._done)
                w.done.connect(t.quit)
                t.finished.connect(w.deleteLater)
                t.finished.connect(t.deleteLater)
                t.finished.connect(self._fin)
                qthreads.retain(t, w)
                # Single-slot attrs, exactly like tide's call sites: this
                # overwrite drops the previous pair mid-run.
                self._t = t; self._w = w
                self.spawned += 1; self.running += 1
                t.start()
                # Finalize the dropped wrapper now rather than "eventually",
                # so the test is deterministic instead of luck-of-the-GC.
                gc.collect()
            def _done(self, _): self.done += 1
            def _fin(self): self.running -= 1

        TARGET = 60
        app = QCoreApplication(sys.argv[:1])
        h = H()
        def tick():
            if h.spawned >= TARGET:
                if h.running:
                    return
                timer.stop()
                print("OK spawned=%d done=%d" % (h.spawned, h.done), flush=True)
                app.quit(); return
            h.fetch(); h.fetch()
        timer = QTimer(); timer.timeout.connect(tick); timer.start(0)
        app.exec()
        """
    )

    def test_overlapping_spawns_do_not_crash(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", self.SRC],
            capture_output=True,
            text=True,
            timeout=600,
            env={
                "PYTHONPATH": "src",
                "QT_QPA_PLATFORM": "offscreen",
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
            },
        )
        self.assertGreaterEqual(
            proc.returncode, 0,
            f"child died on signal {-proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr[-3000:]}",
        )
        self.assertEqual(
            proc.returncode, 0,
            f"child exited {proc.returncode}\nstderr: {proc.stderr[-3000:]}",
        )
        self.assertIn("OK spawned=60", proc.stdout)


if __name__ == "__main__":
    unittest.main()
