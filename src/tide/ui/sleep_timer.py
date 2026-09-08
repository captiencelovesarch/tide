"""Sleep timer — pause after N minutes / current song / current queue.

The dialog launches the timer and returns. The actual countdown lives on
the MainWindow so the dialog can close without cancelling the timer.
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from . import scale


class SleepMode(str, Enum):
    MINUTES = "minutes"
    AFTER_SONG = "after_song"
    AFTER_QUEUE = "after_queue"


class SleepTimerDialog(QDialog):
    started = Signal(object, int)   # SleepMode, minutes (0 if N/A)
    cancelled = Signal()

    PRESETS = (5, 15, 30, 45, 60, 90)

    def __init__(self, default_minutes: int = 30, active_mode: "SleepMode | None" = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — sleep timer")
        self.setModal(True)
        self.setMinimumWidth(scale.px(380))

        heading = QLabel("pause playback after…")
        heading.setProperty("class", "dim")

        self.mode_group = QButtonGroup(self)
        self.rb_minutes = QRadioButton("in")
        self.rb_song    = QRadioButton("after current song")
        self.rb_queue   = QRadioButton("after current queue")
        for i, rb in enumerate((self.rb_minutes, self.rb_song, self.rb_queue)):
            self.mode_group.addButton(rb, i)

        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(1, 240)
        self.minutes_spin.setSingleStep(5)
        self.minutes_spin.setValue(default_minutes)
        self.minutes_spin.setSuffix(" min")

        minutes_row = QHBoxLayout()
        minutes_row.addWidget(self.rb_minutes)
        minutes_row.addWidget(self.minutes_spin)
        minutes_row.addStretch(1)

        # Preset row — clicking a preset fills the spinner and selects minutes mode.
        presets_row = QHBoxLayout()
        for n in self.PRESETS:
            btn = QPushButton(f"{n}m")
            btn.setFlat(True)
            btn.clicked.connect(lambda _=False, m=n: self._set_preset(m))
            presets_row.addWidget(btn)
        presets_row.addStretch(1)

        if active_mode == SleepMode.AFTER_SONG:
            self.rb_song.setChecked(True)
        elif active_mode == SleepMode.AFTER_QUEUE:
            self.rb_queue.setChecked(True)
        else:
            self.rb_minutes.setChecked(True)

        self.start_btn = QPushButton("start")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        self.cancel_btn = QPushButton("cancel timer")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.close_btn = QPushButton("close")
        self.close_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.close_btn)
        btn_row.addWidget(self.start_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(heading)
        root.addLayout(minutes_row)
        root.addLayout(presets_row)
        root.addWidget(self.rb_song)
        root.addWidget(self.rb_queue)
        root.addStretch(1)
        root.addLayout(btn_row)

    def _set_preset(self, minutes: int) -> None:
        self.rb_minutes.setChecked(True)
        self.minutes_spin.setValue(minutes)

    def _on_start(self) -> None:
        if self.rb_song.isChecked():
            self.started.emit(SleepMode.AFTER_SONG, 0)
        elif self.rb_queue.isChecked():
            self.started.emit(SleepMode.AFTER_QUEUE, 0)
        else:
            self.started.emit(SleepMode.MINUTES, int(self.minutes_spin.value()))
        self.accept()

    def _on_cancel(self) -> None:
        self.cancelled.emit()
        self.accept()
