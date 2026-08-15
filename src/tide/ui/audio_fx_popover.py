"""Quick-access audio FX popover, anchored to a now-playing-strip button.

Reads + mutates the same shared ``AudioFxState`` the full panel owns.
The user clicks the button → small popover with the most-reached-for
knobs: master enable, preset dropdown, reverb dropdown, bass + treble
shelves. Right-clicking the button toggles master enable inline.

Mirrors the SpeedButton / SpeedPopover pattern in ``speed.py``:
``Qt.Popup`` so external clicks auto-close, ``show_above(anchor)`` for
placement, theme-aware repaint.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from ..audio_fx import (
    AudioFxState,
    EQ_GAIN_MAX_DB,
    EQ_GAIN_MIN_DB,
    EQ_PRESETS,
    REVERB_PRESETS,
)
from .widgets import BracketButton


def _format_db(value: float) -> str:
    if abs(value) < 0.05:
        return "0"
    sign = "+" if value > 0 else "−"
    return f"{sign}{abs(value):.0f}"


class AudioFxButton(BracketButton):
    """Compact bracket-styled button: shows ``[fx]`` when active and
    ``[fx·off]`` when bypassed. Click → opens the popover. Right-click
    → toggle master.
    """

    state_changed = Signal(object)   # AudioFxState

    def __init__(self, state: AudioFxState | None = None, parent: QWidget | None = None) -> None:
        super().__init__("fx", parent=parent)
        self._state = state if state is not None else AudioFxState()
        self._popover: AudioFxPopover | None = None
        self.clicked.connect(self._open_popover)
        self.setToolTip("audio fx — right-click to toggle the rack on/off")
        self._refresh_label()

    def state(self) -> AudioFxState:
        return self._state

    def set_state(self, state: AudioFxState, *, emit: bool = False) -> None:
        self._state = state
        self._refresh_label()
        if self._popover is not None and self._popover.isVisible():
            self._popover.sync(self._state)
        if emit:
            self.state_changed.emit(self._state)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.RightButton:
            self._state.master_enabled = not self._state.master_enabled
            self._refresh_label()
            self.state_changed.emit(self._state)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def _refresh_label(self) -> None:
        self.setLabel("fx" if self._state.master_enabled else "fx·off")

    def _open_popover(self) -> None:
        if self._popover is None:
            self._popover = AudioFxPopover(self.window())
            self._popover.state_changed.connect(self._on_pop_changed)
        self._popover.sync(self._state)
        self._popover.show_above(self)

    def _on_pop_changed(self, _state) -> None:
        # The popover mutates ``self._state`` in place (same instance).
        self._refresh_label()
        self.state_changed.emit(self._state)


class AudioFxPopover(QFrame):
    """Compact rack quick controls. Mutates the bound ``AudioFxState``
    in place and emits ``state_changed`` so the owner can fan out."""

    state_changed = Signal(object)   # AudioFxState

    SHELF_SCALE = 2   # ½-dB resolution on the int slider

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("AudioFxPopover")
        self._state: AudioFxState | None = None
        self._silent = False

        self._apply_theme(theming.manager().current())
        theming.manager().theme_changed.connect(self._apply_theme)

        # master toggle pill at top
        self._master_btn = BracketButton("rack on")
        self._master_btn.setCheckable(True)
        self._master_btn.toggled.connect(self._on_master)

        # preset row
        self._preset_combo = QComboBox()
        for name in EQ_PRESETS:
            self._preset_combo.addItem(name, name)
        self._preset_combo.currentIndexChanged.connect(self._on_preset)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        preset_lbl = QLabel("preset")
        preset_row.addWidget(preset_lbl)
        preset_row.addWidget(self._preset_combo, stretch=1)

        # reverb row
        self._reverb_combo = QComboBox()
        for name in REVERB_PRESETS:
            self._reverb_combo.addItem(name, name)
        self._reverb_combo.currentIndexChanged.connect(self._on_reverb)
        reverb_row = QHBoxLayout()
        reverb_row.setSpacing(8)
        reverb_row.addWidget(QLabel("reverb"))
        reverb_row.addWidget(self._reverb_combo, stretch=1)

        # bass + treble shelf sliders (compact)
        self._bass_slider, bass_row = self._make_shelf("bass", "_bass_read")
        self._treble_slider, treble_row = self._make_shelf("treble", "_treble_read")
        self._bass_slider.valueChanged.connect(self._on_bass)
        self._treble_slider.valueChanged.connect(self._on_treble)

        # full-panel hint at bottom
        self._hint = QLabel("ctrl+8 → full panel")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet("color: palette(mid);")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)
        root.addWidget(self._master_btn)
        root.addLayout(preset_row)
        root.addLayout(reverb_row)
        root.addLayout(bass_row)
        root.addLayout(treble_row)
        root.addWidget(self._hint)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumWidth(280)

    # ---------- bind + sync ----------

    def sync(self, state: AudioFxState) -> None:
        self._state = state
        self._silent = True
        try:
            self._master_btn.setChecked(state.master_enabled)
            self._master_btn.setLabel("rack on" if state.master_enabled else "rack off")
            # Preset combo — find the nearest match, fall back to "custom".
            from ..audio_fx import detect_eq_preset
            active_preset = detect_eq_preset(state.eq_bands)
            if active_preset == "custom":
                # Add (or update) a transient "custom" entry so the
                # combo can show it without us mutating EQ_PRESETS.
                if self._preset_combo.findData("custom") < 0:
                    self._preset_combo.addItem("custom", "custom")
                self._preset_combo.setCurrentIndex(self._preset_combo.findData("custom"))
            else:
                self._preset_combo.setCurrentIndex(
                    max(0, self._preset_combo.findData(active_preset))
                )
            self._reverb_combo.setCurrentIndex(
                max(0, self._reverb_combo.findData(state.reverb_preset))
            )
            self._bass_slider.setValue(int(round(state.bass_db * self.SHELF_SCALE)))
            self._treble_slider.setValue(int(round(state.treble_db * self.SHELF_SCALE)))
            self._bass_read.setText(f"{_format_db(state.bass_db)} dB")
            self._treble_read.setText(f"{_format_db(state.treble_db)} dB")
        finally:
            self._silent = False

    def show_above(self, anchor: QWidget) -> None:
        self.adjustSize()
        anchor_top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        x = anchor_top_left.x() + (anchor.width() - self.width()) // 2
        y = anchor_top_left.y() - self.height() - 4
        screen = anchor.screen()
        if screen is not None:
            geom = screen.availableGeometry()
            if y < geom.top():
                y = anchor_top_left.y() + anchor.height() + 4
            x = max(geom.left() + 4, min(x, geom.right() - self.width() - 4))
        self.move(x, y)
        self.show()
        self.raise_()

    # ---------- handlers ----------

    def _on_master(self, on: bool) -> None:
        if self._state is None or self._silent:
            return
        self._state.master_enabled = bool(on)
        self._master_btn.setLabel("rack on" if on else "rack off")
        self._emit()

    def _on_preset(self, _idx: int) -> None:
        if self._state is None or self._silent:
            return
        name = self._preset_combo.currentData()
        if not name or name == "custom":
            return
        self._state.apply_eq_preset(name)
        self._emit()

    def _on_reverb(self, _idx: int) -> None:
        if self._state is None or self._silent:
            return
        self._state.reverb_preset = self._reverb_combo.currentData() or "off"
        self._emit()

    def _on_bass(self, raw: int) -> None:
        if self._state is None or self._silent:
            return
        db = raw / self.SHELF_SCALE
        self._state.bass_db = float(db)
        self._bass_read.setText(f"{_format_db(db)} dB")
        self._emit()

    def _on_treble(self, raw: int) -> None:
        if self._state is None or self._silent:
            return
        db = raw / self.SHELF_SCALE
        self._state.treble_db = float(db)
        self._treble_read.setText(f"{_format_db(db)} dB")
        self._emit()

    # ---------- internals ----------

    def _make_shelf(self, label: str, readout_attr: str) -> tuple[QSlider, QHBoxLayout]:
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(int(EQ_GAIN_MIN_DB * self.SHELF_SCALE))
        slider.setMaximum(int(EQ_GAIN_MAX_DB * self.SHELF_SCALE))
        slider.setSingleStep(1)
        readout = QLabel("0 dB")
        readout.setMinimumWidth(52)
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        setattr(self, readout_attr, readout)
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label)
        lbl.setMinimumWidth(50)
        row.addWidget(lbl)
        row.addWidget(slider, stretch=1)
        row.addWidget(readout)
        return slider, row

    def _emit(self) -> None:
        if self._state is not None:
            self.state_changed.emit(self._state)

    def _apply_theme(self, theme) -> None:
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        self.setStyleSheet(
            f"QFrame#AudioFxPopover {{ background: {bg}; border: 1px solid {fg}; }}"
        )
