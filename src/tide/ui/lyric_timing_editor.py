"""The settings dialog's "precise timing" row: the toggle, plus getting
what it needs — the model download (and removal) right here, with
progress, so turning it on never means a terminal or a config file.
The one thing it can't do is install onnxruntime; it says which package.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .. import lyric_align, qthreads, theming

PACKAGE = "python-onnxruntime-cpu"


class _Download(QObject):
    progress = Signal(int, int)
    done = Signal(str)            # "" = ok, else the error

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    def run(self) -> None:
        try:
            lyric_align.download_model(
                progress=lambda a, b: self.progress.emit(int(a // 1024), int(b // 1024)),
                should_stop=lambda: self.cancelled)
        except Exception as exc:
            self.done.emit(str(exc) or exc.__class__.__name__)
            return
        self.done.emit("")


class PreciseTimingEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.check = QCheckBox(theming.styled_case("time words to the audio"))
        self.check.toggled.connect(lambda _on: self.changed.emit())
        self.status = QLabel("")
        self.status.setProperty("class", "dim")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.get_btn = QPushButton(theming.styled_case(
            f"download model ({lyric_align.MODEL_BYTES / 1e6:.0f} mb)"))
        self.get_btn.clicked.connect(self._on_get)
        self.remove_btn = QPushButton(theming.styled_case("remove model"))
        self.remove_btn.clicked.connect(self._on_remove)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.hide()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.check)
        row.addStretch(1)
        row.addWidget(self.get_btn)
        row.addWidget(self.remove_btn)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        col.addLayout(row)
        col.addWidget(self.bar)
        col.addWidget(self.status)
        self._thread: QThread | None = None
        self._worker: _Download | None = None
        self._refresh()

    # ---------- the custom-kind contract ----------

    def value(self) -> bool:
        return bool(self.check.isChecked())

    def set_value(self, raw) -> None:
        self.check.blockSignals(True)
        self.check.setChecked(bool(raw))
        self.check.blockSignals(False)
        self._refresh()

    def normalize(self, raw) -> bool:
        return bool(raw)

    # ---------- state ----------

    def _refresh(self) -> None:
        runtime = lyric_align.runtime_available()
        model = lyric_align.model_installed()
        busy = self._worker is not None
        self.get_btn.setVisible(runtime and not model)
        self.get_btn.setEnabled(not busy)
        self.remove_btn.setVisible(runtime and model and not busy)
        self.check.setEnabled(runtime and model)
        if not runtime:
            text = (f"needs {PACKAGE} (from the official repos). "
                    "restart tide after installing it.")
        elif busy:
            text = "downloading the model…"
        elif not model:
            text = ("downloads a speech model once (english). songs are then "
                    "timed in the background, once each.")
        else:
            text = "english lyrics. clear vocals work best; anything unsure stays as it was."
        self.status.setText(theming.styled_case(text))

    def _on_get(self) -> None:
        if self._worker is not None:
            return
        thread = QThread()
        worker = _Download()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        qthreads.retain(thread, worker)
        self.bar.setRange(0, 0)
        self.bar.show()
        self._refresh()
        thread.start()

    def _on_progress(self, done_kb: int, total_kb: int) -> None:
        self.bar.setRange(0, max(1, total_kb))
        self.bar.setValue(done_kb)

    def _on_done(self, error: str) -> None:
        self._worker = None
        self._thread = None
        self.bar.hide()
        self._refresh()
        if error:
            self.status.setText(theming.styled_case(f"download failed · {error}"))
        elif not self.check.isChecked():
            self.check.setChecked(True)      # what the click was for

    def _on_remove(self) -> None:
        lyric_align.remove_model()
        if self.check.isChecked():
            self.check.setChecked(False)
        self._refresh()
