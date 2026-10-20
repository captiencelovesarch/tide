"""the hidden qt panels the spike puts in vr, and the bit that turns
overlay mouse events into qt mouse events.

no openvr in here, so ``spike.py panel --preview`` can show the same
widgets on the desktop.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPointF, Qt, Signal
from PySide6.QtGui import QEnterEvent, QFont, QMouseEvent
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

W, H = 800, 480

QSS = """
QWidget#panel { background: #101010; }
QLabel { color: #e8e8e8; }
QLabel#dim { color: #8a8a8a; }
QLabel#big { color: #f5f5f5; }
QPushButton {
    color: #f5f5f5; background: #262626; border: 3px solid #e8e8e8;
    padding: 10px 18px; min-height: 64px;
}
QPushButton:hover { background: #3a3a3a; }
QPushButton:pressed { background: #ffd400; color: #101010; }
QPushButton#mic { min-height: 120px; border-width: 4px; }
QPushButton#close { min-width: 64px; max-width: 64px; min-height: 64px; max-height: 64px; padding: 0; }
QPushButton#mic:pressed { background: #e0312b; color: #ffffff; }
QSlider::groove:horizontal { height: 10px; background: #3a3a3a; }
QSlider::handle:horizontal { width: 36px; margin: -16px 0; background: #ffd400; }
"""


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class Forwarder:
    """overlay mouse events in, the qt events a real mouse would cause out.

    the pressed widget keeps getting moves until release (a drag on the
    slider survives the laser drifting off it), and enter/leave go to
    whatever is under the pointer so hover styles work."""

    def __init__(self, root: QWidget) -> None:
        self.root = root
        self.pos = QPointF(-1, -1)
        self.hover: QWidget | None = None
        self.grab: QWidget | None = None

    def _at(self, p: QPointF) -> QWidget:
        return self.root.childAt(p.toPoint()) or self.root

    def _send(self, kind, target: QWidget, button, buttons) -> None:
        local = QPointF(target.mapFrom(self.root, self.pos.toPoint()))
        ev = QMouseEvent(kind, local, local, self.pos, button, buttons, Qt.NoModifier)
        QApplication.sendEvent(target, ev)

    def _rehover(self) -> None:
        target = self._at(self.pos)
        if target is self.hover:
            return
        if self.hover is not None:
            QApplication.sendEvent(self.hover, QEvent(QEvent.Leave))
        self.hover = target
        local = QPointF(target.mapFrom(self.root, self.pos.toPoint()))
        QApplication.sendEvent(target, QEnterEvent(local, local, self.pos))

    def move(self, x: float, y: float) -> None:
        self.pos = QPointF(x, y)
        if self.grab is not None:
            self._send(QEvent.MouseMove, self.grab, Qt.NoButton, Qt.LeftButton)
            return
        self._rehover()
        self._send(QEvent.MouseMove, self.hover, Qt.NoButton, Qt.NoButton)

    def press(self) -> None:
        self.grab = self._at(self.pos)
        self._send(QEvent.MouseButtonPress, self.grab, Qt.LeftButton, Qt.LeftButton)

    def release(self) -> None:
        target = self.grab or self._at(self.pos)
        self.grab = None
        self._send(QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
        self._rehover()

    def leave(self) -> None:
        if self.hover is not None:
            QApplication.sendEvent(self.hover, QEvent(QEvent.Leave))
        self.hover = None


class _Base(QWidget):
    """fixed-size panel. ``dirty`` fires when its content changed; input
    forwarded from vr marks it dirty on the loop side (a hidden widget
    can't be trusted to report its own repaints)."""

    dirty = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("panel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(W, H)
        self.setStyleSheet(QSS)
        self._log: list[str] = []

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log = ([f"{stamp} {text}"] + self._log)[:6]
        self.log_label.setText("\n".join(self._log))
        print(f"{stamp} {text}", flush=True)
        self.dirty.emit()


class SpikePanel(_Base):
    """buttons to click, one to hold, a slider to drag, a live log, and an
    x that closes it."""

    close_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.clicks = 0
        self._held_at: float | None = None

        title = QLabel("tide · vr spike")
        title.setObjectName("big")
        title.setFont(_font(34, bold=True))
        self.close_btn = QPushButton("x")
        self.close_btn.setObjectName("close")
        self.close_btn.setFont(_font(28, bold=True))
        # a big target (sized in QSS): a laser at arm's length wobbles
        self.close_btn.clicked.connect(self.close_requested)
        top = QHBoxLayout()
        top.addWidget(title, 1)
        top.addWidget(self.close_btn, 0, Qt.AlignTop)
        self.focus_label = QLabel("focus: ?")
        self.focus_label.setObjectName("dim")
        self.focus_label.setFont(_font(18))

        self.click_btn = QPushButton("click  (0)")
        self.click_btn.setFont(_font(26, bold=True))
        self.click_btn.clicked.connect(self._on_click)
        self.hold_btn = QPushButton("hold")
        self.hold_btn.setFont(_font(26, bold=True))
        self.hold_btn.pressed.connect(self._on_hold)
        self.hold_btn.released.connect(self._on_unhold)
        row = QHBoxLayout()
        row.addWidget(self.click_btn, 1)
        row.addWidget(self.hold_btn, 1)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setFixedHeight(56)
        self.slider_label = QLabel("drag  50")
        self.slider_label.setFont(_font(20))
        self.slider.setValue(50)
        self.slider.valueChanged.connect(lambda v: self.slider_label.setText(f"drag  {v}"))
        srow = QHBoxLayout()
        srow.addWidget(self.slider, 1)
        srow.addWidget(self.slider_label)

        self.log_label = QLabel("")
        self.log_label.setFont(_font(16))
        self.log_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim")
        self.stats_label.setFont(_font(16))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 14)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.focus_label)
        lay.addLayout(row)
        lay.addLayout(srow)
        lay.addWidget(self.log_label, 1)
        lay.addWidget(self.stats_label)

    def _on_click(self) -> None:
        self.clicks += 1
        self.click_btn.setText(f"click  ({self.clicks})")
        self.log(f"click {self.clicks}")

    def _on_hold(self) -> None:
        self._held_at = time.monotonic()
        self.log("hold: down")

    def _on_unhold(self) -> None:
        if self._held_at is not None:
            self.log(f"hold: up after {time.monotonic() - self._held_at:.2f} s")
        self._held_at = None


class VoicePanel(_Base):
    """one request at a time: what to say, and a big mic button to hold."""

    talk_started = Signal()
    talk_stopped = Signal()
    skip = Signal()
    redo = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.count_label = QLabel("1 / 20")
        self.count_label.setObjectName("dim")
        self.count_label.setFont(_font(18))
        self.say_label = QLabel("say:")
        self.say_label.setObjectName("big")
        self.say_label.setFont(_font(30, bold=True))
        self.say_label.setWordWrap(True)

        self.mic = QPushButton("hold to talk")
        self.mic.setObjectName("mic")
        self.mic.setFont(_font(32, bold=True))
        self.mic.pressed.connect(self.talk_started)
        self.mic.released.connect(self.talk_stopped)

        redo = QPushButton("redo")
        redo.setFont(_font(22))
        redo.clicked.connect(self.redo)
        skip = QPushButton("skip")
        skip.setFont(_font(22))
        skip.clicked.connect(self.skip)
        row = QHBoxLayout()
        row.addWidget(redo)
        row.addWidget(skip)

        self.log_label = QLabel("")
        self.log_label.setFont(_font(16))
        self.log_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 14)
        lay.setSpacing(10)
        lay.addWidget(self.count_label)
        lay.addWidget(self.say_label)
        lay.addWidget(self.mic)
        lay.addLayout(row)
        lay.addWidget(self.log_label, 1)

    def show_prompt(self, index: int, total: int, text: str) -> None:
        self.count_label.setText(f"{index + 1} / {total}")
        self.say_label.setText(f"say: {text}")
