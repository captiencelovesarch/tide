"""Glyph editor — every transport glyph, user-editable.

Phase 1 made the glyph vocabulary data (tide.glyphs: KEYS, packs, an
override layer). This is the Phase-2 power tool that edits the override
layer: one row per glyph key — the pack's glyph, a 1-3 character
replacement field, a live preview, [reset] — plus [reset all].

Preview discipline (the phase-1 browse-flip bug is the law here): edits
push the override layer live through ``glyphs.set_overrides()`` so the
dialog's preview column AND the running window show the candidate
glyphs, but NOTHING persists until save. Cancel / Esc / window-close
puts the layer back exactly as the dialog found it. Save is the only
path that writes: ``settings.glyph_overrides`` updated and field-saved
(``settings.save_fields`` — never a whole-object save, and only when
the set actually changed), the live layer left applied, and the main
window's ``refresh_glyphs()`` — hasattr-guarded until integration
builds it — re-pushes state to every transport label.

Boot contract this rides on: app.py applies ``settings.glyph_overrides``
via ``glyphs.set_overrides()`` before the window exists, and preset
flips re-apply the incoming personality's set (``glyph_overrides`` is
in presets.STASH_FIELDS — glyphs are chrome, chrome is personality).
The dialog therefore treats ``settings.glyph_overrides`` as the
truthful snapshot of the live layer, and its cancel-revert restores
exactly that.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import glyphs, settings as settings_module
from .headings import line_heading


# Row labels for the vocabulary — the key itself where a plain
# underscore-to-space read is already the right words.
_LABELS: dict[str, str] = {
    "prev": "previous",
    "repeat_one": "repeat one",
    "like_on": "liked",
    "like_off": "not liked",
    "sleep": "sleep timer",
    "heading_dash": "heading dash",
}


def _dim(label: QLabel) -> QLabel:
    label.setProperty("class", "dim")
    return label


class GlyphEditorDialog(QDialog):
    """One row per key in ``glyphs.KEYS``; the override layer previews
    live while the dialog is open; field-scoped save on accept, exact
    revert on anything else."""

    # The contract cap: an override is 1-3 characters. Empty = no
    # override (fall back to the pack).
    MAX_CHARS = 3

    def __init__(self, current_settings: settings_module.Settings,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — glyphs")
        self.setModal(True)
        self.setMinimumWidth(400)
        self.resize(460, 660)

        # The LIVE settings object — written ONLY in _on_save.
        self._settings = current_settings

        # The revert snapshot. Filtered to the known vocabulary the same
        # way glyphs.set_overrides filters, so a stale key from another
        # version can neither crash a row nor survive an accept.
        raw = dict(current_settings.glyph_overrides or {})
        self._initial: dict[str, str] = {
            k: str(v) for k, v in raw.items() if k in glyphs.DEFAULT_PACK
        }

        # Pack-resolved base glyphs (what reset falls back to, and what
        # an override is diffed against), read with the override layer
        # lifted. The lift/restore is an identity in a running app (boot
        # keeps the live layer == settings.glyph_overrides) and is
        # synchronous plain data — no signals, no restyle.
        glyphs.set_overrides({})
        self._base: dict[str, str] = {k: glyphs.glyph(k) for k in glyphs.KEYS}
        glyphs.set_overrides(self._initial)

        self._edits: dict[str, QLineEdit] = {}
        self._previews: dict[str, QLabel] = {}
        self._resets: dict[str, QPushButton] = {}
        self._building = True
        try:
            self._build_ui()
        finally:
            self._building = False

    # ---------- build ----------

    def _build_ui(self) -> None:
        heading = _dim(QLabel(line_heading("transport glyphs", 34)))
        blurb = _dim(QLabel(
            "1-3 characters per glyph; empty falls back to the pack. "
            "edits preview live — in this list and on the player — and "
            "only stick when you save."
        ))
        blurb.setWordWrap(True)
        per = _dim(QLabel(
            "·per personality· — each personality keeps its own glyph set"
        ))
        per.setToolTip(
            "flips with the personality preset — each side remembers "
            "its own value")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        for col, text in ((1, "pack"), (2, "yours"), (3, "preview")):
            grid.addWidget(_dim(QLabel(text)), 0, col,
                           alignment=Qt.AlignHCenter)
        for row, key in enumerate(glyphs.KEYS, start=1):
            name = QLabel(_LABELS.get(key, key.replace("_", " ")))
            base = _dim(QLabel(self._base[key]))
            base.setToolTip("the active pack's glyph — what reset returns to")
            edit = QLineEdit()
            edit.setMaxLength(self.MAX_CHARS)
            edit.setFixedWidth(64)
            edit.setAlignment(Qt.AlignHCenter)
            edit.setPlaceholderText(self._base[key])
            if key in self._initial:
                edit.setText(self._initial[key])
            # Connect AFTER the populate setText so building a row never
            # rides the change path at all.
            edit.textChanged.connect(
                lambda _t="", k=key: self._on_edited(k))
            preview = QLabel(self._initial.get(key) or self._base[key])
            preview.setMinimumWidth(40)
            preview.setAlignment(Qt.AlignHCenter)
            reset = QPushButton("reset")
            reset.setFlat(True)
            reset.clicked.connect(lambda _c=False, k=key: self._on_reset(k))
            self._edits[key] = edit
            self._previews[key] = preview
            self._resets[key] = reset
            grid.addWidget(name, row, 0)
            grid.addWidget(base, row, 1, alignment=Qt.AlignHCenter)
            grid.addWidget(edit, row, 2)
            grid.addWidget(preview, row, 3, alignment=Qt.AlignHCenter)
            grid.addWidget(reset, row, 4)
        grid.setColumnStretch(0, 1)

        self.reset_all_btn = QPushButton("reset all")
        self.reset_all_btn.clicked.connect(self._on_reset_all)
        self.save_btn = QPushButton("save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn = QPushButton("cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addWidget(self.reset_all_btn)
        btns.addStretch(1)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.save_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(heading)
        root.addWidget(blurb)
        root.addWidget(per)
        root.addLayout(grid)
        root.addStretch(1)
        root.addLayout(btns)

    # ---------- state ----------

    def current_overrides(self) -> dict[str, str]:
        """The override set the rows currently describe: non-empty text
        that differs from the pack's glyph. Typing the pack glyph back
        in is a reset, not an override — storing it would pin a future
        pack swap to today's default for no visible reason."""
        out: dict[str, str] = {}
        for key in glyphs.KEYS:
            text = self._edits[key].text().strip()
            if text and text != self._base[key]:
                out[key] = text
        return out

    def edit_for(self, key: str) -> QLineEdit:
        return self._edits[key]

    def preview_for(self, key: str) -> QLabel:
        return self._previews[key]

    def reset_for(self, key: str) -> QPushButton:
        return self._resets[key]

    def base_glyph(self, key: str) -> str:
        return self._base[key]

    # ---------- change handling / live preview ----------

    def _on_edited(self, key: str) -> None:
        text = self._edits[key].text().strip()
        self._previews[key].setText(text or self._base[key])
        if self._building:
            return
        self._push_live()

    def _on_reset(self, key: str) -> None:
        # clear() fires textChanged (when non-empty), which previews and
        # pushes; an already-empty row stays quiet — nothing changed.
        self._edits[key].clear()

    def _on_reset_all(self) -> None:
        # One live push for the whole sweep, not one per row.
        self._building = True
        try:
            for edit in self._edits.values():
                edit.clear()
        finally:
            self._building = False
        self._push_live()

    def _push_live(self) -> None:
        """Live preview through the registry: the running window's
        labels draw through glyphs.glyph(), so replacing the override
        layer + a refresh shows the candidate everywhere. Never
        persists — reject() restores the opening snapshot."""
        glyphs.set_overrides(self.current_overrides())
        self._refresh_window()

    def _refresh_window(self) -> None:
        """Walk up to whatever owns refresh_glyphs() (the MainWindow —
        integration builds the method, hence the hasattr guard) and ask
        it to re-push current state to every transport label."""
        p = self.parent()
        while p is not None:
            if hasattr(p, "refresh_glyphs"):
                p.refresh_glyphs()
                return
            p = p.parent()

    # ---------- accept / cancel ----------

    def _on_save(self) -> None:
        """The ONLY write path: update settings.glyph_overrides, save
        exactly that field (and only when the set actually changed — an
        untouched accept writes nothing, matching the settings engine),
        leave the live layer applied, refresh the window."""
        overrides = self.current_overrides()
        s = self._settings
        if dict(s.glyph_overrides or {}) != overrides:
            s.glyph_overrides = dict(overrides)
            settings_module.save_fields(s, "glyph_overrides")
        glyphs.set_overrides(overrides)
        self._refresh_window()
        self.accept()

    def reject(self) -> None:
        """Cancel / Esc / window close — previews never commit: put the
        live override layer back exactly as the dialog found it and
        repaint the window with it. The settings object was never
        touched, so there is nothing else to undo."""
        glyphs.set_overrides(dict(self._initial))
        self._refresh_window()
        super().reject()


# Open dialogs hold no other strong Python reference (open() doesn't
# block like exec()), so the module retains them until they finish —
# a parentless editor must not be GC-collected out from under the user.
_open_dialogs: set[GlyphEditorDialog] = set()


def _release(dlg: GlyphEditorDialog) -> None:
    _open_dialogs.discard(dlg)
    dlg.deleteLater()


def open_glyph_editor(current_settings: settings_module.Settings,
                      parent=None) -> None:
    """Open the editor DEFERRED out of the calling signal emission —
    constructing/showing a dialog synchronously inside a click handler
    is the PySide6 + py3.14 segfault pattern ([[feedback-pyside-modal]]).
    Safe to wire straight to a clicked signal. Uses open() (window-modal,
    non-blocking); the finished handler releases the module's reference
    and deleteLater-destroys the dialog on the GUI thread."""
    def _open() -> None:
        dlg = GlyphEditorDialog(current_settings, parent)
        _open_dialogs.add(dlg)
        dlg.finished.connect(lambda _result, d=dlg: _release(d))
        dlg.open()
    QTimer.singleShot(0, _open)
