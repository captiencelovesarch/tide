"""Quick-access audio FX popover, anchored to a now-playing-strip button.

Reads + mutates the same shared ``AudioFxState`` the full panel owns.
The user clicks the button → small popover with the most-reached-for
knobs: master enable, preset dropdown, reverb dropdown + wet, bass +
treble shelves, lofi + crossfeed toggles. Everything else lives in the
full rack. Right-clicking the button toggles master enable inline.

Mirrors the SpeedButton / SpeedPopover pattern in ``speed.py``:
``Qt.Popup`` so external clicks auto-close, ``show_above(anchor)`` for
placement, theme-aware repaint. Like the speed popover, it has two faces
picked at popover build from the window's active personality: bracket
(this class, behaviorally verbatim — the brutalist personality) and
``SpringAudioFxPopover`` (modern — the wet/bass/treble sliders become
magnetic-detent SpringSliders whose release shortens the window's
debounced filter-chain push). A personality flip mid-session picks the
new face on the next open; the cached popover is rebuilt.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QMouseEvent
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
from .audio_fx_view import _commit_fx_debounce
from .spring_slider import SpringSlider
from .widgets import BracketButton


def _format_db(value: float) -> str:
    if abs(value) < 0.05:
        return "0"
    sign = "+" if value > 0 else "−"
    return f"{sign}{abs(value):.0f}"


def _default_fx_panel_key() -> str:
    """The shipped view_audio_fx binding, for popovers with no main
    window to ask (tests / standalone). Imported lazily — window.py is
    heavy and imports THIS module at runtime."""
    try:
        from .window import ACTIONS
        for action in ACTIONS:
            if action.id == "view_audio_fx":
                seq = QKeySequence(action.default)
                return seq.toString(QKeySequence.NativeText).lower()
    except Exception:
        pass
    return "ctrl+8"


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
        self._popover_face_built: str = ""
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

    def _popover_face(self) -> str:
        """``"spring"`` when the window's active personality is modern,
        else ``"bracket"``. Read from the window's settings at popover
        build time (the speed button's exact rule); anything unknown —
        no settings attached, tests, a third-party preset id — defaults
        to the bracket face."""
        settings = getattr(self.window(), "_settings", None)
        preset = str(getattr(settings, "preset", "") or "")
        return "spring" if preset == "modern" else "bracket"

    def _open_popover(self) -> None:
        face = self._popover_face()
        if self._popover is not None and self._popover_face_built != face:
            # The personality flipped since this popover was built —
            # rebuild so the new face shows on this open. Cheap: the
            # popover carries no state beyond what sync() pushes.
            self._popover.deleteLater()
            self._popover = None
        if self._popover is None:
            cls = SpringAudioFxPopover if face == "spring" else AudioFxPopover
            self._popover = cls(self.window())
            self._popover.state_changed.connect(self._on_pop_changed)
            self._popover_face_built = face
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
    WET_SCALE = 20    # 5% resolution on the reverb wet slider

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("AudioFxPopover")
        self._state: AudioFxState | None = None
        self._silent = False

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

        # reverb wet slider (5% steps) — face-specific construction; the
        # spring subclass overrides _make_wet_row / _make_shelf /
        # _wire_sliders / _sync_sliders and nothing else touches them.
        wet_row = self._make_wet_row()

        # quick fx toggles — just the two that fit the popover's job;
        # the rest live in the full rack
        self._lofi_btn = BracketButton("lofi")
        self._lofi_btn.setCheckable(True)
        self._lofi_btn.toggled.connect(self._on_lofi)
        self._crossfeed_btn = BracketButton("crossfeed")
        self._crossfeed_btn.setCheckable(True)
        self._crossfeed_btn.toggled.connect(self._on_crossfeed)
        fx_row = QHBoxLayout()
        fx_row.setSpacing(8)
        fx_row.addWidget(self._lofi_btn)
        fx_row.addWidget(self._crossfeed_btn)
        fx_row.addStretch(1)

        # bass + treble shelf sliders (compact)
        self._bass_slider, bass_row = self._make_shelf("bass", "_bass_read")
        self._treble_slider, treble_row = self._make_shelf("treble", "_treble_read")
        self._wire_sliders()

        # full-panel hint at bottom — the key name derives from the live
        # keymap (v2.0), not a hardcoded "ctrl+8"; refreshed on every
        # sync() so a rebind shows up on the next open. Colored by
        # _apply_theme (the dim token — palette(mid) ignored theming).
        self._hint = QLabel()
        self._hint.setAlignment(Qt.AlignCenter)
        self._refresh_hint()

        self._apply_theme(theming.manager().current())
        theming.manager().theme_changed.connect(self._apply_theme)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)
        root.addWidget(self._master_btn)
        root.addLayout(preset_row)
        root.addLayout(reverb_row)
        root.addLayout(wet_row)
        root.addLayout(bass_row)
        root.addLayout(treble_row)
        root.addLayout(fx_row)
        root.addWidget(self._hint)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumWidth(280)

    # ---------- bind + sync ----------

    def _refresh_hint(self) -> None:
        """Derive the full-panel pointer from the live keymap. The
        popover is parented to the MainWindow (AudioFxButton passes its
        window()), whose binding_display knows the current
        view_audio_fx binding — walked via parent() because the Popup
        window flag makes self.window() return the popover itself.
        Standalone (tests, no main window) falls back to the shipped
        default. An unbound action gets the nav-tab wording instead of
        advertising a dead key."""
        win = self.parent()
        while win is not None and not hasattr(win, "binding_display"):
            win = win.parent()
        if win is not None:
            try:
                key = str(win.binding_display("view_audio_fx") or "")
            except Exception:
                key = ""
        else:
            key = _default_fx_panel_key()
        if key:
            self._hint.setText(f"{key} → full panel")
        else:
            self._hint.setText("full panel → the [fx] nav tab")

    def sync(self, state: AudioFxState) -> None:
        self._refresh_hint()
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
            self._sync_sliders(state)
            self._lofi_btn.setChecked(state.lofi)
            self._crossfeed_btn.setChecked(state.crossfeed)
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

    def _on_wet(self, raw: int) -> None:
        if self._state is None or self._silent:
            return
        wet = max(0.0, min(1.0, raw / self.WET_SCALE))
        self._state.reverb_wet = wet
        self._wet_read.setText(f"{int(round(wet * 100))}%")
        self._emit()

    def _on_lofi(self, on: bool) -> None:
        if self._state is None or self._silent:
            return
        self._state.lofi = bool(on)
        self._emit()

    def _on_crossfeed(self, on: bool) -> None:
        if self._state is None or self._silent:
            return
        self._state.crossfeed = bool(on)
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

    # ---------- internals (the spring subclass overrides these) ----------

    def _make_wet_row(self) -> QHBoxLayout:
        self._wet_slider = QSlider(Qt.Horizontal)
        self._wet_slider.setMinimum(0)
        self._wet_slider.setMaximum(self.WET_SCALE)
        self._wet_slider.setSingleStep(1)
        self._wet_read = QLabel("50%")
        self._wet_read.setMinimumWidth(52)
        self._wet_read.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        wet_row = QHBoxLayout()
        wet_row.setSpacing(8)
        wet_lbl = QLabel("wet")
        wet_lbl.setMinimumWidth(50)
        wet_row.addWidget(wet_lbl)
        wet_row.addWidget(self._wet_slider, stretch=1)
        wet_row.addWidget(self._wet_read)
        return wet_row

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

    def _wire_sliders(self) -> None:
        self._wet_slider.valueChanged.connect(self._on_wet)
        self._bass_slider.valueChanged.connect(self._on_bass)
        self._treble_slider.valueChanged.connect(self._on_treble)

    def _sync_sliders(self, state: AudioFxState) -> None:
        self._wet_slider.setValue(int(round(state.reverb_wet * self.WET_SCALE)))
        self._wet_read.setText(f"{int(round(state.reverb_wet * 100))}%")
        self._bass_slider.setValue(int(round(state.bass_db * self.SHELF_SCALE)))
        self._treble_slider.setValue(int(round(state.treble_db * self.SHELF_SCALE)))
        self._bass_read.setText(f"{_format_db(state.bass_db)} dB")
        self._treble_read.setText(f"{_format_db(state.treble_db)} dB")

    def _emit(self) -> None:
        if self._state is not None:
            self.state_changed.emit(self._state)

    def _apply_theme(self, theme) -> None:
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        dim = theme.token("dim", "#666666") if theme else "#666666"
        self.setStyleSheet(
            f"QFrame#AudioFxPopover {{ background: {bg}; border: 1px solid {fg}; }}"
        )
        # The hint used to hardcode palette(mid), which ignores theming
        # entirely — route it through the dim token (hex fallback, same
        # pattern as SpringSlider's paint tokens).
        self._hint.setStyleSheet(f"color: {dim};")


class SpringAudioFxPopover(AudioFxPopover):
    """The modern face: wet / bass / treble become magnetic-detent
    SpringSliders (wet magnetized at 50%, the shelves at 0 dB). Same
    external surface as AudioFxPopover (``state_changed`` / ``sync`` /
    ``show_above``) so AudioFxButton can't tell the faces apart; the
    bracket base stays behaviorally verbatim because this class only
    overrides the four construction/wiring/sync seams plus its own
    float handlers.

    Debounce discipline: live drag values mutate the shared state and
    ride ``state_changed`` into the window's ~120 ms push / ~250 ms save
    debounce exactly like every other rack control — never a direct
    push. A committed interaction (release / wheel / keys settling)
    shortens that pending work so the final chain lands promptly
    instead of waiting out the timer — shortens, never bypasses. The
    settle itself is motion-gated by SpringSlider's construction
    (intensity OFF = synchronous snap)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Rounded corners on a top-level popup need this, or the pixels
        # outside the radius are unpainted window buffer. The base ctor
        # already ran _apply_theme (virtual, lands here).
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def _apply_theme(self, theme) -> None:
        # The modern face's frame: quiet border, active radius — the
        # bracket base keeps its hard fg outline.
        bg = theme.token("bg", "#0b0b0b") if theme else "#0b0b0b"
        border = theme.token("border_col", "#2a2a2a") if theme else "#2a2a2a"
        radius = theme.token("radius", "0px") if theme else "0px"
        dim = theme.token("dim", "#666666") if theme else "#666666"
        self.setStyleSheet(
            f"QFrame#AudioFxPopover {{ background: {bg}; "
            f"border: 1px solid {border}; border-radius: {radius}; }}"
        )
        self._hint.setStyleSheet(f"color: {dim};")

    def _make_wet_row(self) -> QHBoxLayout:
        self._wet_slider = SpringSlider()
        self._wet_slider.set_range(0.0, 1.0, 1.0 / self.WET_SCALE)
        self._wet_slider.set_detents((0.5,))
        self._wet_slider.set_formatter(lambda v: f"{int(round(v * 100))}%")
        self._wet_read = QLabel("50%")
        self._wet_read.setMinimumWidth(52)
        self._wet_read.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        wet_row = QHBoxLayout()
        wet_row.setSpacing(8)
        wet_lbl = QLabel("wet")
        wet_lbl.setMinimumWidth(50)
        wet_row.addWidget(wet_lbl)
        wet_row.addWidget(self._wet_slider, stretch=1)
        wet_row.addWidget(self._wet_read)
        return wet_row

    def _make_shelf(self, label: str, readout_attr: str) -> tuple[SpringSlider, QHBoxLayout]:
        slider = SpringSlider()
        slider.set_range(EQ_GAIN_MIN_DB, EQ_GAIN_MAX_DB, 1.0 / self.SHELF_SCALE)
        slider.set_detents((0.0,))
        slider.set_formatter(lambda v: f"{_format_db(v)} dB")
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

    def _wire_sliders(self) -> None:
        self._wet_slider.value_changed.connect(self._on_wet_f)
        self._wet_slider.value_committed.connect(self._on_commit)
        self._bass_slider.value_changed.connect(self._on_bass_f)
        self._bass_slider.value_committed.connect(self._on_commit)
        self._treble_slider.value_changed.connect(self._on_treble_f)
        self._treble_slider.value_committed.connect(self._on_commit)

    def _sync_sliders(self, state: AudioFxState) -> None:
        # Only move a handle when the value really differs — during a
        # drag the slider itself originated the change, and a set_value
        # here would snap the display out from under the finger (the
        # spring speed popover's exact rule). External changes settle
        # springily while the popover is up; the pre-show sync snaps.
        wet = max(0.0, min(1.0, float(state.reverb_wet)))
        self._wet_read.setText(f"{int(round(wet * 100))}%")
        if abs(self._wet_slider.value() - wet) > 1e-9:
            self._wet_slider.set_value(wet, animate=self.isVisible())
        bass = max(EQ_GAIN_MIN_DB, min(EQ_GAIN_MAX_DB, float(state.bass_db)))
        self._bass_read.setText(f"{_format_db(bass)} dB")
        if abs(self._bass_slider.value() - bass) > 1e-9:
            self._bass_slider.set_value(bass, animate=self.isVisible())
        treble = max(EQ_GAIN_MIN_DB, min(EQ_GAIN_MAX_DB, float(state.treble_db)))
        self._treble_read.setText(f"{_format_db(treble)} dB")
        if abs(self._treble_slider.value() - treble) > 1e-9:
            self._treble_slider.set_value(treble, animate=self.isVisible())

    # ---------- spring handlers (floats straight off the sliders) ----------

    def _on_wet_f(self, value: float) -> None:
        if self._state is None or self._silent:
            return
        wet = max(0.0, min(1.0, float(value)))
        self._state.reverb_wet = wet
        self._wet_read.setText(f"{int(round(wet * 100))}%")
        self._emit()

    def _on_bass_f(self, value: float) -> None:
        if self._state is None or self._silent:
            return
        self._state.bass_db = float(value)
        self._bass_read.setText(f"{_format_db(value)} dB")
        self._emit()

    def _on_treble_f(self, value: float) -> None:
        if self._state is None or self._silent:
            return
        self._state.treble_db = float(value)
        self._treble_read.setText(f"{_format_db(value)} dB")
        self._emit()

    def _on_commit(self, _value: float) -> None:
        # The settle landed — shorten the window's pending debounced
        # push/save (never an inline push: with motion off every wheel
        # notch commits). The popover is a Qt.Popup (its own window()),
        # so the helper walks parent()s to find the MainWindow.
        _commit_fx_debounce(self)
