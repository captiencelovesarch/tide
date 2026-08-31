"""Keymap editor over the window module's ACTIONS table.

One row per action — label · current binding · capture box · [reset] —
with a live conflict highlight. [save] REFUSES a conflicted map: two
QShortcuts on one key make Qt emit activatedAmbiguously, which nothing
connects, so both actions would silently go dead.

settings.keymap stores ONLY deviations from the shipped defaults —
missing id = default, empty string = deliberately unbound. Writes land
on accept (then rebind_shortcuts(), no restart needed); nothing applies
while the dialog is open, so cancel has nothing to revert.

Global on purpose: muscle memory doesn't flip with the personality, so
the keymap is NOT a preset stash field.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import settings as settings_module, theming
from .headings import line_heading


# The blurb row doubles as the save-refusal line.
_BLURB_DEFAULT = ("click a box, press the new key. empty = unbound. "
                  "changes land on [save] — cancel forgets everything.")
_BLURB_CONFLICT = ("can't save while two actions share a key — an "
                   "ambiguous key fires NEITHER action, so both would "
                   "just go dead. rebind or clear one of the "
                   "highlighted rows.")


def _display(seq_text: str) -> str:
    """Key sequence → chrome text ("ctrl+s"), or ·unbound·."""
    seq = QKeySequence(seq_text)
    if seq.isEmpty():
        return "·unbound·"
    return seq.toString(QKeySequence.NativeText).lower()


def _portable(seq: QKeySequence) -> str:
    """Canonical storage form — portable text, so comparisons against the
    ACTIONS defaults (also portable) are exact."""
    return seq.toString(QKeySequence.PortableText)


class KeymapEditor(QDialog):

    def __init__(self, current_settings: settings_module.Settings,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — keymap")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.resize(600, 680)

        # Lazy: window.py is heavy — keep it out of import time so the
        # settings dialog can import us cheaply.
        from .window import ACTIONS, default_keymap, effective_keymap
        self._actions = ACTIONS
        self._defaults = {
            aid: _portable(QKeySequence(seq))
            for aid, seq in default_keymap().items()
        }
        self._settings = current_settings
        active = effective_keymap(current_settings)

        self._edits: dict[str, QKeySequenceEdit] = {}
        self._row_labels: dict[str, QLabel] = {}
        self._current_labels: dict[str, QLabel] = {}
        self._reset_btns: dict[str, QPushButton] = {}

        body = QVBoxLayout()
        body.setSpacing(6)
        grid: QGridLayout | None = None
        row_index = 0
        seen_groups: list[str] = []
        for action in self._actions:
            if action.group not in seen_groups:
                seen_groups.append(action.group)
                heading = QLabel(line_heading(action.group, 40))
                heading.setProperty("class", "dim")
                if grid is not None:
                    body.addLayout(grid)
                body.addWidget(heading)
                grid = QGridLayout()
                grid.setHorizontalSpacing(10)
                grid.setVerticalSpacing(4)
                grid.setColumnStretch(0, 1)
                row_index = 0
            name = QLabel(action.label)
            current = QLabel(_display(active[action.id]))
            current.setProperty("class", "dim")
            current.setToolTip("the binding in effect right now")
            edit = QKeySequenceEdit(QKeySequence(active[action.id]))
            if hasattr(edit, "setMaximumSequenceLength"):
                # One chord per action — multi-sequence captures would
                # not round-trip through the single-string keymap.
                edit.setMaximumSequenceLength(1)
            if hasattr(edit, "setClearButtonEnabled"):
                edit.setClearButtonEnabled(True)
            edit.keySequenceChanged.connect(
                lambda _seq=None, k=action.id: self._on_edited(k))
            reset = QPushButton("reset")
            reset.setFlat(True)
            reset.setToolTip(f"back to {_display(action.default)}")
            reset.clicked.connect(
                lambda _checked=False, k=action.id: self._reset_one(k))
            self._row_labels[action.id] = name
            self._current_labels[action.id] = current
            self._edits[action.id] = edit
            self._reset_btns[action.id] = reset
            grid.addWidget(name, row_index, 0)
            grid.addWidget(current, row_index, 1)
            grid.addWidget(edit, row_index, 2)
            grid.addWidget(reset, row_index, 3)
            row_index += 1
        if grid is not None:
            body.addLayout(grid)
        body.addStretch(1)

        page = QWidget()
        page.setLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(page)

        self.blurb = QLabel(_BLURB_DEFAULT)
        self.blurb.setProperty("class", "dim")
        self.blurb.setWordWrap(True)
        blurb = self.blurb

        self.reset_all_btn = QPushButton("reset all")
        self.reset_all_btn.setFlat(True)
        self.reset_all_btn.clicked.connect(self._reset_all)
        self.save_btn = QPushButton("save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn = QPushButton("cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.reset_all_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(blurb)
        root.addWidget(scroll, stretch=1)
        root.addLayout(btn_row)

        self._refresh_conflicts()

    # ---------- pending state ----------

    def _seq_text(self, action_id: str) -> str:
        return _portable(self._edits[action_id].keySequence())

    def pending_overrides(self) -> dict[str, str]:
        """Only deviations from the defaults. A capture back onto the
        default drops out — "missing = default" stays true on disk."""
        out: dict[str, str] = {}
        for action in self._actions:
            seq = self._seq_text(action.id)
            if seq != self._defaults[action.id]:
                out[action.id] = seq
        return out

    def conflicted_ids(self) -> set[str]:
        """Ids whose pending non-empty key is shared with another action."""
        by_seq: dict[str, list[str]] = {}
        for action in self._actions:
            seq = self._seq_text(action.id)
            if seq:
                by_seq.setdefault(seq, []).append(action.id)
        return {aid for ids in by_seq.values() if len(ids) > 1 for aid in ids}

    # ---------- handlers ----------

    def _on_edited(self, _action_id: str) -> None:
        self._refresh_conflicts()

    def _refresh_conflicts(self) -> None:
        """Per-widget styling only — never an app-wide QSS push."""
        bad = self.conflicted_ids()
        error = theming.status_color("error")
        for action in self._actions:
            label = self._row_labels[action.id]
            if action.id in bad:
                label.setStyleSheet(f"color: {error};")
                label.setToolTip("this key is bound to more than one action")
            else:
                label.setStyleSheet("")
                label.setToolTip("")
        if not bad and self.blurb.text() != _BLURB_DEFAULT:
            # the refused-save clash is gone — plain instructions back
            self.blurb.setStyleSheet("")
            self.blurb.setText(_BLURB_DEFAULT)

    def _reset_one(self, action_id: str) -> None:
        self._edits[action_id].setKeySequence(
            QKeySequence(self._defaults[action_id]))

    def _reset_all(self) -> None:
        for action in self._actions:
            self._reset_one(action.id)

    def _find_main_window(self):
        win = self.parent()
        while win is not None and not hasattr(win, "rebind_shortcuts"):
            win = win.parent()
        return win

    def _on_save(self) -> None:
        if self.conflicted_ids():
            # refuse — rows are already highlighted; the blurb explains
            error = theming.status_color("error")
            self.blurb.setStyleSheet(f"color: {error};")
            self.blurb.setText(_BLURB_CONFLICT)
            return
        self._settings.keymap = self.pending_overrides()
        settings_module.save_fields(self._settings, "keymap")
        win = self._find_main_window()
        if win is not None:
            win.rebind_shortcuts()
        self.accept()


def open_keymap_editor(parent, current_settings: settings_module.Settings) -> None:
    """Deferred out of the calling emission — exec'ing a dialog inside
    a click handler is the PySide6 + py3.14 segfault pattern."""
    def _do_open() -> None:
        dlg = KeymapEditor(current_settings, parent)
        dlg.exec()
        dlg.deleteLater()
    QTimer.singleShot(0, _do_open)
