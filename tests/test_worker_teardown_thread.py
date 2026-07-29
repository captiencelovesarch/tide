"""Worker teardown must happen on the GUI thread.

`thread.finished → worker.deleteLater` destroys the PySide wrapper on the
worker's own thread: Shiboken takes Qt's pooled connection mutex inside
~QObject and then waits for the GIL, while the GUI thread (GIL held) can be
inside any QObject::connect waiting on the same pooled mutex — lock inversion,
permanent freeze. The qthreads retain registry is the destruction owner now:
its ref-drop on the GUI thread frees the worker after the thread is gone.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import pathlib
import re
import sys
import unittest

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import qthreads

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "tide"


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Worker(QObject):
    done = Signal()

    def run(self) -> None:
        self.done.emit()


class WorkerTeardownThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()

    def test_no_source_file_connects_worker_deleteLater_to_finished(self) -> None:
        """The banned pattern must not come back at any spawn site."""
        banned = re.compile(r"finished\.connect\(\s*worker\.deleteLater\s*\)")
        offenders = []
        for path in SRC.rglob("*.py"):
            if path.name == "qthreads.py":
                continue    # the module documents the ban itself
            if banned.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        self.assertEqual(
            offenders, [],
            "worker-thread wrapper destruction is the GIL/mutex deadlock — "
            "let the qthreads registry free the worker on the GUI thread",
        )

    def test_worker_wrapper_is_destroyed_on_the_gui_thread(self) -> None:
        destroyed_on: list[QThread] = []

        thread = QThread()
        worker = _Worker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        # Direct connection so the recorder runs on whichever thread actually
        # executes the destructor — that thread is what's under test.
        worker.destroyed.connect(
            lambda *_: destroyed_on.append(QThread.currentThread()),
            Qt.DirectConnection,
        )
        qthreads.retain(thread, worker, group="test-teardown")
        thread.start()

        # Let the thread finish, its deleteLater drain, and the registry drop.
        deadline_waits = 0
        del worker
        while qthreads.live_count("test-teardown") and deadline_waits < 100:
            QTest.qWait(10)
            deadline_waits += 1
        QTest.qWait(20)

        self.assertEqual(qthreads.live_count("test-teardown"), 0,
                         "registry never released the finished thread")
        self.assertEqual(len(destroyed_on), 1, "worker was never destroyed")
        self.assertIs(
            destroyed_on[0], self.app.thread(),
            "worker wrapper was destroyed off the GUI thread — that's the "
            "deadlock pattern",
        )


if __name__ == "__main__":
    unittest.main()
