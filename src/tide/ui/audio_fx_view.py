"""The [audio fx] view — full rack with EQ + every knob.

Sections, top to bottom:
  1. Master enable + active-preset readout.
  2. 10-band graphic EQ — vertical sliders with frequency labels.
  3. EQ presets — clickable cards (flat / bass / treble / vocal / v-shape /
     soft warmth).
  4. Reverb — preset dropdown + wet slider (convolution, see fx_ir).
  5. Effects — chorus / flanger / phaser / tremolo / exciter /
     crossfeed / lofi toggles + their few knobs.
  6. Shelves + glue — bass, treble, loudness norm, stereo width,
     compressor, mono.
  7. Custom slots — three [save] / [load] / [clear] rows.

The view owns a single ``AudioFxState`` and emits ``state_changed``
whenever any control flips. ``app.py`` wires that signal to the playback
router + a debounced settings save.

Two faces (v2.0): the continuous controls sit in ``_FxControl`` wrappers
hosting the original ``_ShelfSlider`` (bracket — brutalist, behavior
verbatim) or a ``_SpringShelfSlider`` (modern). The face lands on show
(``showEvent`` → ``_ensure_face``) because the view is built before
app.py attaches ``window._settings`` — construction always starts
bracket; a mid-session flip lands the next time the page is shown.

Debounce discipline: one mpv filter-chain push costs 34-62 ms ON THE GUI
THREAD and restarts the reverb tail, so drags must NEVER push per-tick.
Live changes ride ``state_changed`` into the window's trailing-edge
debounce (~120 ms push / ~250 ms save); a commit SHORTENS that pending
work via ``_commit_fx_debounce`` — never bypasses it, because commits
arrive per wheel notch and key auto-repeat once motion is off.

The 10 EQ bands stay vertical QSliders (SpringSlider is horizontal by
contract) but join the commit discipline; swap at ``_EqBand`` if a
vertical SpringSlider ever lands.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..audio_fx import (
    AudioFxState,
    EQ_BAND_COUNT,
    EQ_FREQUENCIES_HZ,
    EQ_GAIN_MAX_DB,
    EQ_GAIN_MIN_DB,
    EQ_PRESETS,
    REVERB_PRESETS,
    detect_eq_preset,
)
from ..theming import styled_case
from .spring_slider import SpringSlider


def _format_hz(hz: int) -> str:
    if hz >= 1000:
        return f"{hz // 1000}k" if hz % 1000 == 0 else f"{hz / 1000:.1f}k"
    return str(hz)


def _format_db(value: float) -> str:
    if abs(value) < 0.05:
        return "0"
    sign = "+" if value > 0 else "−"
    return f"{sign}{abs(value):.0f}"


def _format_suffixed(v: float, suffix: str) -> str:
    """Shared readout format — both faces must print values identically."""
    if suffix == " dB":
        return f"{_format_db(v)}{suffix}"
    if suffix == "%":
        return f"{int(round(v * 100))}%"
    if suffix == "x":
        return f"{v:.2f}×"
    return f"{v:.1f}{suffix}"


def _active_face(widget: QWidget) -> str:
    """``"spring"`` under the modern personality, else ``"bracket"`` —
    the speed popover's exact rule; unknown/absent settings → bracket."""
    settings = getattr(widget.window(), "_settings", None)
    preset = str(getattr(settings, "preset", "") or "")
    return "spring" if preset == "modern" else "bracket"


# Commit → drain delay: half the push debounce, long enough to swallow a
# burst (X11 auto-repeat ~33 ms; each drain costs a 34-62 ms mpv rebuild).
COMMIT_DRAIN_MS = 60

_FX_DEBOUNCE_TIMERS = (
    ("_audio_fx_push_timer", "_flush_audio_fx_push"),
    ("_audio_fx_save_timer", "_flush_audio_fx_state"),
)


def _fx_debounce_host(widget: QWidget):
    """The MainWindow-ish object holding the fx debounce timers, or None —
    walks parent() past Qt.Popup windows; duck-typed (tests find nothing)."""
    w = widget.window()
    while w is not None and not hasattr(w, "_flush_audio_fx_push"):
        w = w.parent()
    return w


def _drain_fx_debounce(host) -> None:
    """Cut short what the debounce still owes. Idle timers are skipped —
    nothing pending means a flush would just redo the same work."""
    for timer_attr, flush_attr in _FX_DEBOUNCE_TIMERS:
        try:
            timer = getattr(host, timer_attr, None)
            if timer is not None and timer.isActive():
                timer.stop()
                getattr(host, flush_attr)()
        except Exception:
            pass


def _commit_fx_debounce(widget: QWidget) -> None:
    """Ask the pending debounced fx work (~120 ms push, ~250 ms save) to
    land now-ish — via a ``COMMIT_DRAIN_MS`` single-shot, never an inline
    flush: with motion off every wheel notch and key auto-repeat commits,
    and an inline flush meant a synchronous 34-62 ms ``af set`` plus a
    TOML write per notch. During a burst nothing pushes; one drain lands
    the final value. Nothing pending → nothing scheduled."""
    host = _fx_debounce_host(widget)
    if host is None:
        return
    try:
        pending = any(
            getattr(host, attr, None) is not None
            and getattr(host, attr).isActive()
            for attr, _flush in _FX_DEBOUNCE_TIMERS
        )
    except RuntimeError:
        return
    if not pending:
        return
    timer = getattr(host, "_audio_fx_commit_timer", None)
    if timer is None:
        timer = QTimer(host)    # child of the host: dies with the window
        timer.setSingleShot(True)
        timer.setInterval(COMMIT_DRAIN_MS)
        timer.timeout.connect(lambda t=timer: _drain_fx_debounce(t.parent()))
        host._audio_fx_commit_timer = timer
    timer.start()


class _EqBand(QWidget):
    """One slider + frequency label + live gain readout. Emits the new
    gain in dB on each user-driven change."""

    gain_changed = Signal(int, float)   # band index, gain dB
    # Fired on mouse-drag release. Unconnected under the bracket face
    # (zero behavior change); the modern face flushes the debounced push.
    gain_committed = Signal(int)        # band index

    SLIDER_SCALE = 10   # 0.1 dB resolution under the int slider

    def __init__(self, index: int, frequency_hz: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = index
        self._frequency = frequency_hz
        self._silent = False  # mute setValue → valueChanged loop during sync

        self._readout = QLabel("0")
        self._readout.setAlignment(Qt.AlignCenter)
        self._readout.setMinimumWidth(28)

        self._slider = QSlider(Qt.Vertical)
        self._slider.setMinimum(int(EQ_GAIN_MIN_DB * self.SLIDER_SCALE))
        self._slider.setMaximum(int(EQ_GAIN_MAX_DB * self.SLIDER_SCALE))
        self._slider.setSingleStep(1)
        self._slider.setPageStep(self.SLIDER_SCALE)
        self._slider.setTickPosition(QSlider.TicksRight)
        self._slider.setTickInterval(self.SLIDER_SCALE * 6)  # ±6 / ±12 marks
        self._slider.setFixedHeight(170)
        self._slider.setMinimumWidth(28)
        self._slider.valueChanged.connect(self._on_slider)
        self._slider.sliderReleased.connect(
            lambda: self.gain_committed.emit(self._index))

        # Double-click the readout to reset that band to 0 dB.
        self._readout.setCursor(Qt.PointingHandCursor)
        self._readout.mouseDoubleClickEvent = lambda _ev: self.set_gain(0.0)

        self._label = QLabel(_format_hz(frequency_hz))
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setObjectName("eqBandLabel")

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        col.addWidget(self._readout)
        col.addWidget(self._slider, alignment=Qt.AlignHCenter)
        col.addWidget(self._label)

    def gain(self) -> float:
        return self._slider.value() / self.SLIDER_SCALE

    def set_gain(self, db: float) -> None:
        clamped = max(EQ_GAIN_MIN_DB, min(EQ_GAIN_MAX_DB, float(db)))
        target = int(round(clamped * self.SLIDER_SCALE))
        if target == self._slider.value():
            self._readout.setText(_format_db(clamped))
            return
        self._silent = True
        try:
            self._slider.setValue(target)
        finally:
            self._silent = False
        self._readout.setText(_format_db(clamped))

    def _on_slider(self, value: int) -> None:
        db = value / self.SLIDER_SCALE
        self._readout.setText(_format_db(db))
        if not self._silent:
            self.gain_changed.emit(self._index, db)


class _PresetCard(QPushButton):
    """A bracket-styled pill that highlights when active."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("audioFxPresetCard")
        self.setFlat(False)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setText(styled_case(label))
        self.setMinimumHeight(28)


class _ShelfSlider(QWidget):
    """Horizontal labeled slider with a live readout. Used for bass +
    treble + reverb wet + stereo width."""

    value_changed = Signal(float)

    def __init__(self, label: str, vmin: float, vmax: float, step: float = 0.1,
                 suffix: str = " dB", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vmin = vmin
        self._vmax = vmax
        self._step = step
        self._suffix = suffix
        self._silent = False
        self._scale = int(round(1.0 / step)) if step > 0 else 1

        self._label = QLabel(styled_case(label))
        self._label.setMinimumWidth(110)
        self._readout = QLabel("0")
        self._readout.setMinimumWidth(56)
        self._readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(int(round(vmin * self._scale)))
        self._slider.setMaximum(int(round(vmax * self._scale)))
        self._slider.setSingleStep(1)
        self._slider.setPageStep(self._scale)
        self._slider.valueChanged.connect(self._on_slider)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._label)
        row.addWidget(self._slider, stretch=1)
        row.addWidget(self._readout)

    def value(self) -> float:
        return self._slider.value() / self._scale

    def set_value(self, v: float) -> None:
        clamped = max(self._vmin, min(self._vmax, float(v)))
        target = int(round(clamped * self._scale))
        if target == self._slider.value():
            self._readout.setText(self._format(clamped))
            return
        self._silent = True
        try:
            self._slider.setValue(target)
        finally:
            self._silent = False
        self._readout.setText(self._format(clamped))

    def _format(self, v: float) -> str:
        return _format_suffixed(v, self._suffix)

    def _on_slider(self, value: int) -> None:
        v = value / self._scale
        self._readout.setText(self._format(v))
        if not self._silent:
            self.value_changed.emit(v)


class _SpringShelfSlider(QWidget):
    """Modern face for a continuous rack control: same row shape and
    formats as ``_ShelfSlider``, a SpringSlider in the groove seat, one
    detent on the control's stock value. ``value_changed`` fires live,
    ``value_committed`` on landing, ``set_value`` never emits."""

    value_changed = Signal(float)
    value_committed = Signal(float)

    def __init__(self, label: str, vmin: float, vmax: float, step: float = 0.1,
                 suffix: str = " dB", detents: tuple = (),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._suffix = suffix

        self._label = QLabel(styled_case(label))
        self._label.setMinimumWidth(110)
        self._readout = QLabel("0")
        self._readout.setMinimumWidth(56)
        self._readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._slider = SpringSlider()
        self._slider.set_range(vmin, vmax, step)
        self._slider.set_detents(detents)
        self._slider.set_formatter(self._format)
        self._slider.value_changed.connect(self._on_changed)
        self._slider.value_committed.connect(self.value_committed)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._label)
        row.addWidget(self._slider, stretch=1)
        row.addWidget(self._readout)

        self._readout.setText(self._format(self._slider.value()))

    def value(self) -> float:
        return self._slider.value()

    def set_value(self, v: float) -> None:
        clamped = max(self._vmin, min(self._vmax, float(v)))
        self._slider.set_value(clamped)     # programmatic — never emits
        self._readout.setText(self._format(self._slider.value()))

    def _format(self, v: float) -> str:
        return _format_suffixed(v, self._suffix)

    def _on_changed(self, v: float) -> None:
        self._readout.setText(self._format(v))
        self.value_changed.emit(v)


class _FxControl(QWidget):
    """Hosts one continuous control and swaps its face in place — the
    wrapper is what the view wires and the layouts hold, so a swap never
    touches layout positions or external connections. ``value_committed``
    only ever fires from the spring face (bracket has no commit notion)."""

    value_changed = Signal(float)
    value_committed = Signal(float)

    def __init__(self, label: str, vmin: float, vmax: float, step: float = 0.1,
                 suffix: str = " dB", detents: tuple = (),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = (label, vmin, vmax, step, suffix)
        self._detents = tuple(detents)
        self._face = ""
        self._inner: QWidget | None = None
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)
        self.set_face("bracket")

    def face(self) -> str:
        return self._face

    def set_face(self, face: str) -> None:
        face = "spring" if face == "spring" else "bracket"
        if face == self._face:
            return
        old = self._inner
        carried = old.value() if old is not None else None
        label, vmin, vmax, step, suffix = self._spec
        if face == "spring":
            inner = _SpringShelfSlider(label, vmin, vmax, step, suffix=suffix,
                                       detents=self._detents)
            inner.value_committed.connect(self.value_committed)
        else:
            inner = _ShelfSlider(label, vmin, vmax, step, suffix=suffix)
        inner.value_changed.connect(self.value_changed)
        self._face = face
        self._inner = inner
        self._box.addWidget(inner)
        if old is not None:
            old.hide()
            self._box.removeWidget(old)
            old.deleteLater()
            if carried is not None:
                inner.set_value(carried)    # silent on both faces

    def value(self) -> float:
        return self._inner.value()

    def set_value(self, v: float) -> None:
        self._inner.set_value(v)


class _SlotRow(QFrame):
    """Custom-slot row: [save] [load] [clear] + a small status line."""

    save_clicked = Signal(int)
    load_clicked = Signal(int)
    clear_clicked = Signal(int)

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = index
        self.setObjectName("audioFxSlotRow")

        self._label = QLabel(styled_case(f"slot {index + 1}"))
        self._label.setMinimumWidth(80)
        self._status = QLabel(styled_case("empty"))
        self._status.setObjectName("audioFxSlotStatus")

        self._save_btn = QPushButton(styled_case("[save]"))
        self._save_btn.clicked.connect(lambda: self.save_clicked.emit(self._index))
        self._load_btn = QPushButton(styled_case("[load]"))
        self._load_btn.clicked.connect(lambda: self.load_clicked.emit(self._index))
        self._clear_btn = QPushButton(styled_case("[clear]"))
        self._clear_btn.clicked.connect(lambda: self.clear_clicked.emit(self._index))

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        row.addWidget(self._label)
        row.addWidget(self._status, stretch=1)
        row.addWidget(self._save_btn)
        row.addWidget(self._load_btn)
        row.addWidget(self._clear_btn)

    def set_state(self, occupied: bool, summary: str = "") -> None:
        self._status.setText(styled_case(summary or ("empty" if not occupied else "saved")))
        self._load_btn.setEnabled(occupied)
        self._clear_btn.setEnabled(occupied)


class AudioFxView(QWidget):
    """The full [audio fx] panel. The single source of truth for the
    rack lives in ``self._state``; UI controls reflect it and call back
    to mutate it."""

    state_changed = Signal(object)   # AudioFxState

    def __init__(self, state: AudioFxState | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state if state is not None else AudioFxState()
        # Construction always builds bracket — window._settings doesn't
        # exist yet at window-build time; showEvent lands the real face.
        self._face = "bracket"

        heading = QLabel(styled_case("audio fx"))
        heading.setObjectName("sectionHeading")

        sub = QLabel(styled_case(
            "10-band eq · reverb · chorus, tremolo, lofi and "
            "the rest of the rack. filter chain rebuilds live, no restart "
            "needed. double-click any eq slider readout to zero it."
        ))
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)

        # 1. Master enable row.
        self._master_box = QCheckBox(styled_case("rack on"))
        self._master_box.setChecked(self._state.master_enabled)
        self._master_box.toggled.connect(self._on_master_toggle)

        self._preset_readout = QLabel("")
        self._preset_readout.setObjectName("audioFxActivePreset")

        master_row = QHBoxLayout()
        master_row.setContentsMargins(0, 0, 0, 0)
        master_row.setSpacing(10)
        master_row.addWidget(self._master_box)
        master_row.addWidget(self._preset_readout)
        master_row.addStretch(1)

        # 2. EQ band sliders.
        self._eq_bands: list[_EqBand] = []
        eq_row = QHBoxLayout()
        eq_row.setContentsMargins(0, 0, 0, 0)
        eq_row.setSpacing(10)
        for idx, freq in enumerate(EQ_FREQUENCIES_HZ):
            band = _EqBand(idx, freq)
            band.gain_changed.connect(self._on_band_changed)
            band.gain_committed.connect(self._on_band_committed)
            eq_row.addWidget(band)
            self._eq_bands.append(band)
        eq_row.addStretch(1)
        eq_frame = QFrame()
        eq_frame.setObjectName("audioFxEqFrame")
        eq_inner = QVBoxLayout(eq_frame)
        eq_inner.setContentsMargins(8, 8, 8, 8)
        eq_inner.setSpacing(0)
        eq_inner.addLayout(eq_row)

        # 3. EQ preset cards.
        self._preset_cards: dict[str, _PresetCard] = {}
        presets_grid = QGridLayout()
        presets_grid.setHorizontalSpacing(8)
        presets_grid.setVerticalSpacing(6)
        for i, name in enumerate(EQ_PRESETS.keys()):
            card = _PresetCard(name)
            card.clicked.connect(lambda _=False, n=name: self._on_preset_picked(n))
            self._preset_cards[name] = card
            presets_grid.addWidget(card, i // 3, i % 3)

        # 4. Reverb section.
        self._reverb_combo = QComboBox()
        for name in REVERB_PRESETS:
            self._reverb_combo.addItem(styled_case(name), name)
        self._reverb_combo.currentIndexChanged.connect(self._on_reverb_changed)
        self._reverb_wet = _FxControl("wet", 0.0, 1.0, 0.05, suffix="%",
                                      detents=(0.5,))
        self._reverb_wet.value_changed.connect(self._on_reverb_wet)
        reverb_row = QHBoxLayout()
        reverb_row.setContentsMargins(0, 0, 0, 0)
        reverb_row.setSpacing(10)
        rb_lbl = QLabel(styled_case("reverb"))
        rb_lbl.setMinimumWidth(110)
        reverb_row.addWidget(rb_lbl)
        reverb_row.addWidget(self._reverb_combo)
        reverb_row.addWidget(self._reverb_wet, stretch=1)

        # 5. Effects — the fx drawer. Toggles first, then the few knobs
        # that earn a slider.
        self._chorus_box = QCheckBox(styled_case("chorus (doubling)"))
        self._chorus_box.toggled.connect(self._on_chorus)
        self._flanger_box = QCheckBox(styled_case("flanger (jet sweep)"))
        self._flanger_box.toggled.connect(self._on_flanger)
        self._phaser_box = QCheckBox(styled_case("phaser (swirl)"))
        self._phaser_box.toggled.connect(self._on_phaser)
        self._tremolo_box = QCheckBox(styled_case("tremolo (wobble)"))
        self._tremolo_box.toggled.connect(self._on_tremolo)
        self._exciter_box = QCheckBox(styled_case("exciter (sparkle)"))
        self._exciter_box.toggled.connect(self._on_exciter)
        self._crossfeed_box = QCheckBox(styled_case("crossfeed (headphones)"))
        self._crossfeed_box.toggled.connect(self._on_crossfeed)
        self._lofi_box = QCheckBox(styled_case("lofi (tape + crush)"))
        self._lofi_box.toggled.connect(self._on_lofi)

        self._tremolo_speed_slider = _FxControl(
            "tremolo speed", 0.5, 10.0, 0.5, suffix=" Hz", detents=(5.0,)
        )
        self._tremolo_speed_slider.value_changed.connect(self._on_tremolo_speed)
        self._exciter_slider = _FxControl("exciter amount", 0.0, 5.0, 0.5,
                                          suffix="", detents=(2.0,))
        self._exciter_slider.value_changed.connect(self._on_exciter_amount)
        self._crossfeed_slider = _FxControl(
            "crossfeed amount", 0.0, 1.0, 0.05, suffix="%", detents=(0.4,)
        )
        self._crossfeed_slider.value_changed.connect(self._on_crossfeed_strength)

        # 6. Shelves + glue.
        self._bass_slider = _FxControl("bass shelf", EQ_GAIN_MIN_DB,
                                       EQ_GAIN_MAX_DB, 0.5, detents=(0.0,))
        self._bass_slider.value_changed.connect(self._on_bass)
        self._treble_slider = _FxControl("treble shelf", EQ_GAIN_MIN_DB,
                                         EQ_GAIN_MAX_DB, 0.5, detents=(0.0,))
        self._treble_slider.value_changed.connect(self._on_treble)
        self._stereo_slider = _FxControl("stereo width", 0.0, 2.5, 0.1,
                                         suffix="x", detents=(1.0,))
        self._stereo_slider.value_changed.connect(self._on_stereo)

        # Only the spring face emits value_committed (see
        # _commit_fx_debounce) — the brutalist rack never takes this path.
        for _ctrl in (self._reverb_wet, self._tremolo_speed_slider,
                      self._exciter_slider, self._crossfeed_slider,
                      self._bass_slider, self._treble_slider,
                      self._stereo_slider):
            _ctrl.value_committed.connect(self._on_fx_commit)

        self._loudness_box = QCheckBox(styled_case("loudness normalize (-14 lufs)"))
        self._loudness_box.toggled.connect(self._on_loudness)
        self._compressor_box = QCheckBox(styled_case("compressor (level boost)"))
        self._compressor_box.toggled.connect(self._on_compressor)
        self._mono_box = QCheckBox(styled_case("fold to mono"))
        self._mono_box.toggled.connect(self._on_mono)

        # 7. Custom slots.
        self._slot_rows: list[_SlotRow] = []
        slots_col = QVBoxLayout()
        slots_col.setContentsMargins(0, 0, 0, 0)
        slots_col.setSpacing(4)
        for i in range(3):
            row = _SlotRow(i)
            row.save_clicked.connect(self._on_save_slot)
            row.load_clicked.connect(self._on_load_slot)
            row.clear_clicked.connect(self._on_clear_slot)
            self._slot_rows.append(row)
            slots_col.addWidget(row)

        # ---------- assemble ----------
        section_label_font = QFont()
        section_label_font.setBold(True)

        def _section(label_text: str) -> QLabel:
            lab = QLabel(styled_case(label_text))
            lab.setObjectName("audioFxSectionLabel")
            lab.setFont(section_label_font)
            return lab

        col = QVBoxLayout()
        col.setContentsMargins(28, 24, 28, 24)
        col.setSpacing(14)
        col.addWidget(heading)
        col.addWidget(sub)
        col.addLayout(master_row)
        col.addWidget(_section("graphic eq"))
        col.addWidget(eq_frame)
        col.addWidget(_section("presets"))
        col.addLayout(presets_grid)
        col.addWidget(_section("reverb"))
        col.addLayout(reverb_row)
        col.addWidget(_section("effects"))
        fx_row_a = QHBoxLayout()
        fx_row_a.setSpacing(20)
        fx_row_a.addWidget(self._chorus_box)
        fx_row_a.addWidget(self._flanger_box)
        fx_row_a.addWidget(self._phaser_box)
        fx_row_a.addWidget(self._tremolo_box)
        fx_row_a.addStretch(1)
        col.addLayout(fx_row_a)
        fx_row_b = QHBoxLayout()
        fx_row_b.setSpacing(20)
        fx_row_b.addWidget(self._exciter_box)
        fx_row_b.addWidget(self._crossfeed_box)
        fx_row_b.addWidget(self._lofi_box)
        fx_row_b.addStretch(1)
        col.addLayout(fx_row_b)
        col.addWidget(self._tremolo_speed_slider)
        col.addWidget(self._exciter_slider)
        col.addWidget(self._crossfeed_slider)
        col.addWidget(_section("shelves + glue"))
        col.addWidget(self._bass_slider)
        col.addWidget(self._treble_slider)
        col.addWidget(self._stereo_slider)
        toggles_row = QHBoxLayout()
        toggles_row.setSpacing(20)
        toggles_row.addWidget(self._loudness_box)
        toggles_row.addWidget(self._compressor_box)
        toggles_row.addWidget(self._mono_box)
        toggles_row.addStretch(1)
        col.addLayout(toggles_row)
        col.addWidget(_section("saved slots"))
        col.addLayout(slots_col)
        col.addStretch(1)

        scroll_inner = QWidget()
        scroll_inner.setLayout(col)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_inner)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Initial sync from state — sets all the controls without emitting.
        self.sync_from_state()

    # ---------- public ----------

    def face(self) -> str:
        return self._face

    def state(self) -> AudioFxState:
        return self._state

    def set_state(self, state: AudioFxState) -> None:
        self._state = state
        self.sync_from_state()

    def sync_from_state(self) -> None:
        """Push self._state to every control. Signals are gated so this
        does NOT emit ``state_changed`` — used on first show and after
        ``set_state``."""
        self._master_box.blockSignals(True)
        self._master_box.setChecked(self._state.master_enabled)
        self._master_box.blockSignals(False)
        for idx, band in enumerate(self._eq_bands):
            band.set_gain(self._state.eq_bands[idx] if idx < len(self._state.eq_bands) else 0.0)
        self._reverb_combo.blockSignals(True)
        rv_idx = max(0, self._reverb_combo.findData(self._state.reverb_preset))
        self._reverb_combo.setCurrentIndex(rv_idx)
        self._reverb_combo.blockSignals(False)
        self._reverb_wet.set_value(self._state.reverb_wet)
        self._bass_slider.set_value(self._state.bass_db)
        self._treble_slider.set_value(self._state.treble_db)
        self._stereo_slider.set_value(self._state.stereo_width)
        self._tremolo_speed_slider.set_value(self._state.tremolo_speed)
        self._exciter_slider.set_value(self._state.exciter_amount)
        self._crossfeed_slider.set_value(self._state.crossfeed_strength)
        for box, attr in (
            (self._loudness_box, "loudness_norm"),
            (self._compressor_box, "compressor"),
            (self._mono_box, "mono"),
            (self._chorus_box, "chorus"),
            (self._flanger_box, "flanger"),
            (self._phaser_box, "phaser"),
            (self._tremolo_box, "tremolo"),
            (self._exciter_box, "exciter"),
            (self._crossfeed_box, "crossfeed"),
            (self._lofi_box, "lofi"),
        ):
            box.blockSignals(True)
            box.setChecked(getattr(self._state, attr))
            box.blockSignals(False)
        for i, row in enumerate(self._slot_rows):
            slot = self._state.custom_slots[i] if i < len(self._state.custom_slots) else None
            occupied = bool(slot and any(abs(v) > 0.05 for v in slot.bands))
            summary = ""
            if occupied and slot is not None:
                # Quick fingerprint so the user can tell their slots apart
                # without loading each one — peak band + its gain.
                peak_idx = max(range(EQ_BAND_COUNT), key=lambda j: abs(slot.bands[j]))
                summary = f"{_format_hz(EQ_FREQUENCIES_HZ[peak_idx])} {_format_db(slot.bands[peak_idx])} dB"
            row.set_state(occupied, summary)
        self._refresh_preset_highlight()

    # ---------- face ----------

    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._ensure_face()

    def _fx_controls(self) -> tuple:
        return (self._reverb_wet, self._tremolo_speed_slider,
                self._exciter_slider, self._crossfeed_slider,
                self._bass_slider, self._treble_slider, self._stereo_slider)

    def _ensure_face(self) -> None:
        """Land the personality's face. Runs every show, so a mid-session
        preset flip lands the next time the page is opened — never mid-view."""
        face = _active_face(self)
        if face == self._face:
            return
        self._face = face
        for ctrl in self._fx_controls():
            ctrl.set_face(face)
        # set_face carried each control's value; this also refreshes
        # readouts, preset highlight and the rest, without emitting.
        self.sync_from_state()

    # ---------- handlers ----------

    def _ui_sound(self, key: str) -> None:
        w = self.window()
        player = getattr(w, "ui_sounds", None) if w is not None else None
        if player is not None:
            try:
                player.play(key)
            except Exception:
                pass

    def _on_master_toggle(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.master_enabled = bool(on)
        self._emit()

    def _on_band_changed(self, idx: int, db: float) -> None:
        if 0 <= idx < len(self._state.eq_bands):
            self._state.eq_bands[idx] = float(db)
            self._refresh_preset_highlight()
            self._emit()

    def _on_band_committed(self, _idx: int) -> None:
        # EQ drag released — modern face only: brutalist keeps today's
        # push timing verbatim (product contract).
        if self._face == "spring":
            _commit_fx_debounce(self)

    def _on_fx_commit(self, _value: float) -> None:
        # Spring-face interaction landed — see _commit_fx_debounce.
        _commit_fx_debounce(self)

    def _on_preset_picked(self, name: str) -> None:
        self._state.apply_eq_preset(name)
        for i, band in enumerate(self._eq_bands):
            band.set_gain(self._state.eq_bands[i])
        self._refresh_preset_highlight()
        self._emit()

    def _on_reverb_changed(self, _idx: int) -> None:
        self._state.reverb_preset = self._reverb_combo.currentData() or "off"
        self._emit()

    def _on_reverb_wet(self, value: float) -> None:
        self._state.reverb_wet = max(0.0, min(1.0, value))
        self._emit()

    def _on_bass(self, value: float) -> None:
        self._state.bass_db = float(value)
        self._emit()

    def _on_treble(self, value: float) -> None:
        self._state.treble_db = float(value)
        self._emit()

    def _on_stereo(self, value: float) -> None:
        self._state.stereo_width = float(value)
        self._emit()

    def _on_loudness(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.loudness_norm = bool(on)
        self._emit()

    def _on_compressor(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.compressor = bool(on)
        self._emit()

    def _on_mono(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.mono = bool(on)
        self._emit()

    def _on_chorus(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.chorus = bool(on)
        self._emit()

    def _on_flanger(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.flanger = bool(on)
        self._emit()

    def _on_phaser(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.phaser = bool(on)
        self._emit()

    def _on_tremolo(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.tremolo = bool(on)
        self._emit()

    def _on_tremolo_speed(self, value: float) -> None:
        self._state.tremolo_speed = float(value)
        self._emit()

    def _on_exciter(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.exciter = bool(on)
        self._emit()

    def _on_exciter_amount(self, value: float) -> None:
        self._state.exciter_amount = float(value)
        self._emit()

    def _on_crossfeed(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.crossfeed = bool(on)
        self._emit()

    def _on_crossfeed_strength(self, value: float) -> None:
        self._state.crossfeed_strength = float(value)
        self._emit()

    def _on_lofi(self, on: bool) -> None:
        self._ui_sound("toggle_on" if on else "toggle_off")
        self._state.lofi = bool(on)
        self._emit()

    def _on_save_slot(self, idx: int) -> None:
        self._state.save_custom_slot(idx)
        self.sync_from_state()
        self._emit()

    def _on_load_slot(self, idx: int) -> None:
        if self._state.load_custom_slot(idx):
            for i, band in enumerate(self._eq_bands):
                band.set_gain(self._state.eq_bands[i])
            self._refresh_preset_highlight()
            self._emit()

    def _on_clear_slot(self, idx: int) -> None:
        self._state.clear_custom_slot(idx)
        self.sync_from_state()
        self._emit()

    # ---------- helpers ----------

    def _refresh_preset_highlight(self) -> None:
        active = detect_eq_preset(self._state.eq_bands)
        for name, card in self._preset_cards.items():
            card.setChecked(name == active)
        self._preset_readout.setText(
            styled_case(f"current — {active}") if active != "custom" else styled_case("current — custom")
        )

    def _emit(self) -> None:
        self.state_changed.emit(self._state)
