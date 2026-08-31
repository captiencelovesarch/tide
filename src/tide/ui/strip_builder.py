"""Strip builder — build your player bar.

The now-playing strip is five slots (art · label · controls · progress ·
volume) and every slot has variants (ui/variants.py). v1 buried the
choice in five bare combos; this is the first-class page: pick a variant
per slot and watch a LIVE miniature strip built from the real widget
factories. Offscreen-safe, no audio — the preview widgets are fed sample
data and wired to nothing.

Contract (phase 2):
- rows and choices come from the real variant registries
  (``all_variant_slugs``) — a new variant registered in variants.py
  appears here with zero edits, and a hardcoded copy can't drift;
- the dialog NEVER applies or persists anything itself. Accept emits
  ``overrides_chosen(dict)`` — the minimal per-slot diff against the
  active base layout — and integration routes it through the existing
  update_overrides → apply_layout path (the keep-list rules live there)
  plus save_fields. Cancel is zero-trace by construction: the preview
  lives inside the dialog, never on the running window;
- an untouched accept emits nothing at all, so the caller writes
  nothing (the phase-2 zero-change contract);
- ``open_strip_builder`` defers construction out of the calling signal
  emission — a modal built synchronously inside a click handler is the
  PySide6 + py3.14 segfault pattern ([[feedback-pyside-modal]]).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import layout as layout_module
from .. import theming
from .headings import line_heading
from .variants import (
    all_variant_slugs,
    make_album_art,
    make_controls,
    make_now_label,
    make_progress,
    make_volume,
)


# Row order + what each row says. Keys are the layout system's slot
# names; the choice lists themselves come from all_variant_slugs() at
# build time — never a copy.
SLOT_ORDER: tuple[str, ...] = (
    "album_art",
    "now_label",
    "controls",
    "progress",
    "volume",
)

SLOT_LABELS: dict[str, str] = {
    "album_art": "art",
    "now_label": "label",
    "controls": "controls",
    "progress": "progress",
    "volume": "volume",
}

# Preview art base size (pre ui-scale) — miniature, not the strip's 96.
_PREVIEW_ART_SIZE = 56


def _sample_art() -> QImage:
    """A generated cover for the preview tile — bg_alt field, accent
    disc — so the square/circle/polaroid masks actually show instead of
    three identical ``[no art]`` boxes. No file, no network."""
    theme = theming.manager().current()
    bg = QColor(theme.token("bg_alt", "#141414") if theme else "#141414")
    accent = QColor(theme.token("accent", "#d4b95e") if theme else "#d4b95e")
    dim = QColor(theme.token("dim", "#666") if theme else "#666")
    img = QImage(96, 96, QImage.Format_RGB32)
    img.fill(bg)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(accent)
    p.drawEllipse(18, 18, 60, 60)
    p.setBrush(dim)
    p.drawEllipse(42, 42, 12, 12)
    p.end()
    return img


class StripBuilder(QDialog):
    """Pick a variant per slot, preview live, hand the diff back.

    ``overrides_chosen`` fires ONCE on an accept that changed something,
    carrying the minimal overrides dict (see :meth:`chosen_overrides`).
    The dialog itself touches no settings object, no manager, no disk.
    """

    overrides_chosen = Signal(dict)

    def __init__(self, overrides: dict[str, str] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — strip builder")
        self.setModal(True)
        self.setMinimumWidth(520)

        # The base layout the diff is computed against: the manager's
        # active preset WITHOUT overrides. chosen picks that match it
        # need no override row — and returning to it clears one.
        mgr = layout_module.manager()
        base = mgr.get(mgr.base_slug()) or layout_module.fallback_layout()
        self._base_slots: dict[str, str] = dict(base.slots)
        if overrides is None:
            # No explicit overrides — open on what the window shows now.
            effective = dict(mgr.current().slots)
        else:
            effective = dict(layout_module.Layout.with_overrides(
                base, dict(overrides)).slots)

        self._registry: dict[str, list[str]] = {
            slot: list(slugs) for slot, slugs in all_variant_slugs().items()
        }
        self.pickers: dict[str, QComboBox] = {}
        self._preview_widgets: dict[str, QWidget] = {}
        self._preview_host: QWidget | None = None

        # Guard: populating combos programmatically must not trigger a
        # rebuild per row — one rebuild after, not five during.
        self._building = True
        self._build_ui()
        self._populate(effective)
        self._initial_slots = self._chosen_slots()
        self._building = False
        self._rebuild_preview()

    # ---------- build ----------

    def _build_ui(self) -> None:
        heading = QLabel(line_heading("build your player bar", 34))
        heading.setProperty("class", "dim")
        blurb = QLabel(
            "one variant per slot. the miniature below is built from the "
            "real widgets — what you see is what the bar becomes. nothing "
            "changes until you accept; cancel walks away clean."
        )
        blurb.setProperty("class", "dim")
        blurb.setWordWrap(True)

        form = QFormLayout()
        for slot in SLOT_ORDER:
            combo = QComboBox()
            for slug in self._registry[slot]:
                combo.addItem(slug, slug)
            combo.currentIndexChanged.connect(
                lambda _i=0, s=slot: self._on_slot_changed(s))
            self.pickers[slot] = combo
            form.addRow(f"{SLOT_LABELS[slot]}:", combo)

        preview_heading = QLabel(line_heading("preview", 34))
        preview_heading.setProperty("class", "dim")
        self._preview_area = QVBoxLayout()

        self.reset_btn = QPushButton("reset to theme defaults")
        self.reset_btn.clicked.connect(self._on_reset)
        self.cancel_btn = QPushButton("cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.accept_btn = QPushButton("use this bar")
        self.accept_btn.setDefault(True)
        self.accept_btn.clicked.connect(self._on_accept)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.accept_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(heading)
        root.addWidget(blurb)
        root.addLayout(form)
        root.addWidget(preview_heading)
        root.addLayout(self._preview_area)
        root.addStretch(1)
        root.addLayout(btn_row)

    def _populate(self, effective: dict[str, str]) -> None:
        for slot in SLOT_ORDER:
            combo = self.pickers[slot]
            idx = combo.findData(effective.get(slot))
            if idx < 0:
                # Stale/unknown slug (an old override naming a variant
                # this version doesn't ship) — show the base layout's
                # choice instead of an arbitrary first row.
                idx = combo.findData(self._base_slots.get(slot))
            combo.setCurrentIndex(max(0, idx))

    # ---------- the live miniature ----------

    def _on_slot_changed(self, _slot: str) -> None:
        if self._building:
            return
        self._rebuild_preview()

    def _rebuild_preview(self) -> None:
        old = self._preview_host
        host = self._build_preview_host()
        self._preview_area.addWidget(host)
        self._preview_host = host
        if old is not None:
            # The old preview widgets die with their host on the next
            # loop turn — no leaked theme_changed listeners piling up
            # restyle cost while the user browses variants.
            old.setParent(None)
            old.deleteLater()

    def _build_preview_host(self) -> QWidget:
        """One miniature strip from the REAL slot factories. Sample data
        only; every signal (seek, volume, clicks) is wired to nothing,
        so poking the preview moves pixels and nothing else."""
        slots = self._chosen_slots()
        host = QWidget()
        host.setObjectName("stripBuilderPreview")

        art = make_album_art(slots["album_art"], _PREVIEW_ART_SIZE)
        art.setImage(_sample_art())
        label = make_now_label(slots["now_label"])
        label.setTrack("the artist", "sample song", "the album")
        progress = make_progress(slots["progress"])
        progress.setDuration(240.0)
        progress.setPosition(88.0)
        controls = make_controls(slots["controls"])
        volume = make_volume(slots["volume"])
        volume.setVolume(65, emit=False)
        self._preview_widgets = {
            "album_art": art,
            "now_label": label,
            "controls": controls,
            "progress": progress,
            "volume": volume,
        }

        right = QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(label)
        right.addWidget(progress)
        lower = QHBoxLayout()
        lower.setSpacing(8)
        lower.addWidget(controls)
        lower.addStretch(1)
        lower.addWidget(volume)
        right.addLayout(lower)

        row = QHBoxLayout(host)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(14)
        row.addWidget(art, alignment=Qt.AlignTop)
        row.addLayout(right, stretch=1)
        return host

    # ---------- read ----------

    def picker_for(self, slot: str) -> QComboBox | None:
        return self.pickers.get(slot)

    def preview_widget(self, slot: str) -> QWidget | None:
        """The live preview widget currently standing in for ``slot``."""
        return self._preview_widgets.get(slot)

    def _chosen_slots(self) -> dict[str, str]:
        return {slot: self.pickers[slot].currentData()
                for slot in SLOT_ORDER}

    def chosen_overrides(self) -> dict[str, str]:
        """The minimal diff: only slots whose pick differs from the base
        layout's declared variant. update_overrides replaces the dict
        wholesale, so a pick that returns to the layout default CLEARS
        its override instead of pinning it forever."""
        return {
            slot: slug for slot, slug in self._chosen_slots().items()
            if slug != self._base_slots.get(slot)
        }

    # ---------- reset / accept ----------

    def _on_reset(self) -> None:
        """Back to what the active THEME wants: its [slots] prefs where
        declared, the base layout's choice where not. Nothing leaves the
        dialog — this only moves the combos (and the preview)."""
        theme = theming.manager().current()
        prefs = dict(getattr(theme, "slots", None) or {})
        self._building = True
        try:
            self._populate({
                slot: prefs.get(slot) or self._base_slots.get(slot)
                for slot in SLOT_ORDER
            })
        finally:
            self._building = False
        self._rebuild_preview()

    def _on_accept(self) -> None:
        """Emit the pick and close. NO apply, NO persist — integration
        routes the signal through update_overrides → apply_layout (where
        the _rebuild_strip keep-list rules live) + save_fields. An
        untouched accept emits nothing, so the caller writes nothing."""
        if self._chosen_slots() != self._initial_slots:
            self.overrides_chosen.emit(self.chosen_overrides())
        self.accept()


def open_strip_builder(parent: QWidget | None = None,
                       overrides: dict[str, str] | None = None,
                       on_chosen=None) -> None:
    """Open the builder DEFERRED out of the calling emission.

    The one sanctioned entry point from a click/signal handler: a modal
    constructed synchronously inside an emission is the PySide6 + py3.14
    segfault ([[feedback-pyside-modal]]). ``on_chosen`` (if given) is
    connected to ``overrides_chosen`` before exec."""
    def _open() -> None:
        dlg = StripBuilder(overrides=overrides, parent=parent)
        if on_chosen is not None:
            dlg.overrides_chosen.connect(on_chosen)
        dlg.exec()
        dlg.deleteLater()

    QTimer.singleShot(0, _open)
