"""Glyph editor: one row per key in tide.glyphs, editing its override layer.

Edits push the layer live through ``glyphs.set_overrides()`` — preview
column and running window both show the candidates — but NOTHING
persists until save (field-scoped, only when the set changed; the live
layer stays applied). Cancel / Esc / window-close puts the layer back
exactly as the dialog found it.

Boot contract: app.py applies ``settings.glyph_overrides`` before the
window exists, and preset flips re-apply the incoming personality's set
(it's in presets.STASH_FIELDS) — so the field is a truthful snapshot of
the live layer, and cancel restores exactly that.
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
from .headings import Heading, line_heading
from . import scale


# row labels where an underscore-to-space read isn't already the words
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
    # override length cap; empty = no override (the pack wins)
    MAX_CHARS = 3

    def __init__(self, current_settings: settings_module.Settings,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — glyphs")
        self.setModal(True)
        scale.fit_dialog(self, 460, 660, min_width=400)

        # The LIVE settings object — written ONLY in _on_save.
        self._settings = current_settings

        # Revert snapshot, filtered the way set_overrides filters — a stale
        # key from another version can't crash a row or survive an accept.
        raw = dict(current_settings.glyph_overrides or {})
        self._initial: dict[str, str] = {
            k: str(v) for k, v in raw.items() if k in glyphs.DEFAULT_PACK
        }

        # Pack-resolved base glyphs (reset target, diff base), read with the
        # layer lifted. The lift/restore is plain synchronous data — no
        # signals — and an identity in a running app (live layer == settings).
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
        heading = Heading("transport glyphs", 34)
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
            edit.setFixedWidth(scale.px(64))
            edit.setAlignment(Qt.AlignHCenter)
            edit.setPlaceholderText(self._base[key])
            if key in self._initial:
                edit.setText(self._initial[key])
            # connect AFTER the populate setText — building a row must
            # not ride the change path
            edit.textChanged.connect(
                lambda _t="", k=key: self._on_edited(k))
            preview = QLabel(self._initial.get(key) or self._base[key])
            preview.setMinimumWidth(scale.px(40))
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
        """Non-empty text differing from the pack glyph. Typing the pack glyph
        back is a reset — storing it would pin a future pack swap."""
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
        # clear() fires textChanged when non-empty, which previews and
        # pushes; an already-empty row stays quiet
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
        """Window labels draw through glyphs.glyph(), so swapping the
        layer + a refresh shows the candidate everywhere."""
        glyphs.set_overrides(self.current_overrides())
        self._refresh_window()

    def _refresh_window(self) -> None:
        """Walk up to whatever owns refresh_glyphs() (the MainWindow;
        hasattr-guarded for standalone construction)."""
        p = self.parent()
        while p is not None:
            if hasattr(p, "refresh_glyphs"):
                p.refresh_glyphs()
                return
            p = p.parent()

    # ---------- accept / cancel ----------

    def _on_save(self) -> None:
        """The only write path; an untouched accept writes nothing."""
        overrides = self.current_overrides()
        s = self._settings
        if dict(s.glyph_overrides or {}) != overrides:
            s.glyph_overrides = dict(overrides)
            settings_module.save_fields(s, "glyph_overrides")
        glyphs.set_overrides(overrides)
        self._refresh_window()
        self.accept()

    def reject(self) -> None:
        """Restore the opening layer and repaint — settings were never touched."""
        glyphs.set_overrides(dict(self._initial))
        self._refresh_window()
        super().reject()


# open() doesn't block like exec(), so the module holds the only strong
# reference — a parentless editor must not be GC'd out from under the user.
_open_dialogs: set[GlyphEditorDialog] = set()


def _release(dlg: GlyphEditorDialog) -> None:
    _open_dialogs.discard(dlg)
    dlg.deleteLater()


def open_glyph_editor(current_settings: settings_module.Settings,
                      parent=None) -> None:
    """Deferred out of the calling emission ([[feedback-pyside-modal]]).
    Uses open(), window-modal, non-blocking; the finished handler releases
    the module ref and deleteLater-destroys the dialog on the GUI thread."""
    def _open() -> None:
        dlg = GlyphEditorDialog(current_settings, parent)
        _open_dialogs.add(dlg)
        dlg.finished.connect(lambda _result, d=dlg: _release(d))
        dlg.open()
    QTimer.singleShot(0, _open)
