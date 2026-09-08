"""Theme editor: the active theme's colors, typography and radius, live.

Previews ride the theming manager's sticky user-override layer — its
deferred queue is the only safe path to a full-app restyle, never a
synchronous QApplication.setStyleSheet. Cancel reverts every preview to
the open-time snapshot.

[save as my theme] writes a real theme dir under
``~/.config/tide/themes/``: theme.toml with the edited values over
verbatim copies of the base's other tables (layout, slots, [meta]
aesthetic — a brutalist base stays brutalist) plus its theme.qss, then
refreshes the manager and lands the app on the new theme wearing the
pre-open sticky overrides. Persisting ``settings.theme = <slug>`` is
the caller's job — ``theme_saved(slug)`` hands it over.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import config, theming
from .headings import Heading, line_heading
from . import scale


# row order; declared-but-unlisted tokens append after, still editable
TOKEN_ORDER: tuple[str, ...] = (
    "bg", "bg_alt", "fg", "dim",
    "accent", "accent_alt",
    "sel_bg", "sel_fg",
    "hover_bg", "hover_fg",
    "border_col", "border_dim",
    "ok", "warn", "error",
)

TOKEN_BLURBS: dict[str, str] = {
    "bg": "the canvas behind everything",
    "bg_alt": "raised panels · inputs · popovers",
    "fg": "text",
    "dim": "secondary text",
    "accent": "the highlight color",
    "accent_alt": "the second accent (gradients, meters)",
    "sel_bg": "selected row",
    "sel_fg": "text on a selected row",
    "hover_bg": "hover fill",
    "hover_fg": "text under the cursor",
    "border_col": "borders",
    "border_dim": "quiet borders",
    "ok": "status · good",
    "warn": "status · caution",
    "error": "status · broken",
}

# an off-list theme weight gets a row prepended so the seed is selectable
_WEIGHT_ROWS: tuple[tuple[int, str], ...] = (
    (300, "300 · light"),
    (400, "400 · regular"),
    (500, "500 · medium"),
    (600, "600 · semibold"),
    (700, "700 · bold"),
)

_SAMPLE_TEXT = "waves roll in · waves roll out · 0123456789"


def slugify(name: str) -> str:
    """Display name → theme-dir slug; empty → ``my-theme``."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or "my-theme"


def _is_color(value: object) -> bool:
    """Whether a token value is a color (vs a px length etc.)."""
    text = str(value).strip()
    if not text or text.endswith("px"):
        return False
    return QColor(text).isValid()


def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _toml_dump(sections: dict[str, dict], header: str = "") -> str:
    """Tiny TOML writer (stdlib has none) for theme.toml's flat
    table-of-scalars shape. Round-trips through tomllib — tests pin it."""
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}" for line in header.splitlines())
        lines.append("")
    for table, values in sections.items():
        lines.append(f"[{table}]")
        for key, value in values.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _dim(label: QLabel) -> QLabel:
    label.setProperty("class", "dim")
    label.setWordWrap(True)
    return label


class ThemeEditorDialog(QDialog):
    """Open DEFERRED (``QTimer.singleShot(0, ...)``) from any click or
    signal handler — same rule as every modal in tide."""

    theme_saved = Signal(str)   # slug of the newly written user theme

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tide — theme editor")
        self.setModal(True)
        scale.fit_dialog(self, 500, 680, min_width=440)

        self._mgr = theming.manager()
        self._base = self._mgr.current()

        # ---- snapshot: what cancel puts back ----
        self._snap_overrides: dict[str, str] = dict(
            getattr(self._mgr, "_user_overrides", {}) or {})
        self._snap_font: str = self._mgr.user_font()
        self._snap_font_size: int = self._mgr.user_font_size()
        self._snap_case: str = theming.case_override()

        # ---- editor state ----
        self._tokens: dict[str, str] = {}
        self._touched_tokens: set[str] = set()
        self._touched: set[str] = set()   # "radius" "family" "size" "case" "weight"
        self._saved_slug: str = ""
        self._accepted = False
        self._reverted = False
        self._populating = True

        if self._base is None:
            # no active theme (impossible in a running app) — offer a way out
            col = QVBoxLayout(self)
            col.addWidget(_dim(QLabel(
                "no theme is active — open the editor from settings once "
                "the app is themed.")))
            self.cancel_btn = QPushButton("close")
            self.cancel_btn.clicked.connect(self.reject)
            col.addWidget(self.cancel_btn, alignment=Qt.AlignRight)
            self._populating = False
            return

        self._seed_state()
        self._build_ui()
        self._populating = False

    # ---------- seeding ----------

    def _seed_state(self) -> None:
        """Base values with the STICKY user layer on top. Adaptive per-track
        overrides stay out — saving mid-song must not bake a transient accent."""
        base = self._base
        sticky = dict(self._snap_overrides)
        sticky.pop("radius", None)          # the radius spin owns this axis
        merged = dict(base.tokens)
        merged.update({k: v for k, v in sticky.items() if _is_color(v)})
        ordered = [k for k in TOKEN_ORDER if k in merged and _is_color(merged[k])]
        extras = sorted(
            k for k in merged
            if k not in TOKEN_ORDER and _is_color(merged[k])
        )
        self._token_rows: tuple[str, ...] = tuple(ordered + extras)
        self._tokens = {k: str(merged[k]).strip() for k in self._token_rows}

        self._family: str = str(
            self._snap_font or base.t("typography", "family", "monospace"))
        self._size_pt: int = int(
            self._snap_font_size or base.t("typography", "size_pt", 10))
        self._weight: int = int(base.t("typography", "weight", 400))
        self._case: str = str(
            self._snap_case or base.t("typography", "case", "lower"))
        self._radius_px: int = theming.effective_radius_px(
            self._mgr.current_effective())

    # ---------- build ----------

    def _build_ui(self) -> None:
        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(6, 10, 6, 10)
        col.setSpacing(12)

        col.addWidget(_dim(QLabel(
            "every edit previews live on the app behind this window. "
            "nothing is written until you save; cancel puts everything "
            "back."
        )))

        # ---- colors ----
        col.addWidget(Heading("colors", 34))
        self.swatches: dict[str, QPushButton] = {}
        form = QFormLayout()
        for key in self._token_rows:
            btn = QPushButton()
            blurb = TOKEN_BLURBS.get(key, "")
            if blurb:
                btn.setToolTip(blurb)
            btn.clicked.connect(
                lambda _c=False, k=key: self._on_swatch_clicked(k))
            self.swatches[key] = btn
            form.addRow(f"{key}:", btn)
            self._refresh_swatch(key)
        col.addLayout(form)

        # ---- typography ----
        col.addWidget(Heading("typography", 34))
        tform = QFormLayout()

        self.family_combo = QComboBox()
        self.family_combo.setEditable(True)
        self.family_combo.setInsertPolicy(QComboBox.NoInsert)
        seen: set[str] = set()
        for fam in (self._family, "IBM Plex Mono", "JetBrains Mono", "Inter"):
            if fam and fam not in seen:
                self.family_combo.addItem(fam)
                seen.add(fam)
        try:
            from PySide6.QtGui import QFontDatabase
            for fam in sorted(QFontDatabase.families()):
                if fam.startswith(".") or fam in seen:
                    continue
                self.family_combo.addItem(fam)
                seen.add(fam)
        except Exception:
            pass
        self.family_combo.setCurrentText(self._family)
        # Preview on a pick or a finished typed edit — not per keystroke,
        # which would re-apply the theme once per letter.
        self.family_combo.activated.connect(
            lambda _i=0: self._on_family_changed())
        edit = self.family_combo.lineEdit()
        if edit is not None:
            edit.editingFinished.connect(self._on_family_changed)
        tform.addRow("family:", self.family_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(6, 32)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(self._size_pt)
        self.size_spin.valueChanged.connect(self._on_size_changed)
        tform.addRow("size:", self.size_spin)

        self.weight_combo = QComboBox()
        rows = list(_WEIGHT_ROWS)
        if self._weight not in {w for w, _ in rows}:
            rows.insert(0, (self._weight, f"{self._weight} · theme"))
        for weight, label in rows:
            self.weight_combo.addItem(label, weight)
        self.weight_combo.setCurrentIndex(
            self.weight_combo.findData(self._weight))
        self.weight_combo.currentIndexChanged.connect(self._on_weight_changed)
        tform.addRow("weight:", self.weight_combo)

        self.case_combo = QComboBox()
        for mode in theming.CASE_MODES:
            self.case_combo.addItem(mode, mode)
        idx = self.case_combo.findData(self._case)
        if idx < 0:
            self.case_combo.addItem(self._case, self._case)
            idx = self.case_combo.count() - 1
        self.case_combo.setCurrentIndex(idx)
        self.case_combo.currentIndexChanged.connect(self._on_case_changed)
        tform.addRow("case:", self.case_combo)
        col.addLayout(tform)

        self.sample_label = QLabel(_SAMPLE_TEXT)
        self.sample_label.setWordWrap(True)
        col.addWidget(self.sample_label)
        col.addWidget(_dim(QLabel(
            "weight has no live app preview (it lands in the saved "
            "theme) — the sample line above shows it."
        )))

        # ---- shape ----
        col.addWidget(Heading("shape", 34))
        sform = QFormLayout()
        self.radius_spin = QSpinBox()
        self.radius_spin.setRange(0, 24)
        self.radius_spin.setSuffix(" px")
        self.radius_spin.setValue(self._radius_px)
        self.radius_spin.setToolTip(
            "corner radius. previews via the same sticky @radius "
            "override the corner-style setting uses; your corner-style "
            "pick still wins after the theme is saved.")
        self.radius_spin.valueChanged.connect(self._on_radius_changed)
        sform.addRow("radius:", self.radius_spin)
        col.addLayout(sform)

        # ---- save ----
        col.addWidget(Heading("save as my theme", 34))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(f"my {self._base.name}")
        nform = QFormLayout()
        nform.addRow("name:", self.name_edit)
        col.addLayout(nform)
        col.addWidget(_dim(QLabel(
            "writes a real theme into your themes folder — it shows up "
            "in every theme picker immediately, and the app puts it on."
        )))
        self.status_label = _dim(QLabel(""))
        col.addWidget(self.status_label)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.save_btn = QPushButton("save as my theme")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.cancel_btn = QPushButton("cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.save_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)
        root.addWidget(scroll, stretch=1)
        root.addLayout(btns)

        self._refresh_sample()

    # ---------- introspection (tests + callers) ----------

    def token_keys(self) -> tuple[str, ...]:
        return self._token_rows

    def current_tokens(self) -> dict[str, str]:
        return dict(self._tokens)

    def saved_slug(self) -> str:
        """The slug [save as my theme] wrote. Empty until an accept."""
        return self._saved_slug

    # ---------- color editing ----------

    def _refresh_swatch(self, key: str) -> None:
        btn = self.swatches[key]
        value = self._tokens.get(key, "")
        color = QColor(value)
        pix = QPixmap(18, 18)
        pix.fill(color if color.isValid() else QColor(0, 0, 0, 0))
        btn.setIcon(QIcon(pix))
        btn.setText(value)

    def _on_swatch_clicked(self, key: str) -> None:
        # Deferred — a modal opened inside clicked() is the PySide6 +
        # py3.14 segfault ([[feedback-pyside-modal]]).
        QTimer.singleShot(0, lambda: self._do_pick_color(key))

    def _do_pick_color(self, key: str) -> None:
        initial = QColor(self._tokens.get(key, "#000000"))
        color = QColorDialog.getColor(initial, self, f"tide — {key}")
        if color is not None and color.isValid():
            self.set_token(key, color.name())

    def set_token(self, key: str, value: str) -> None:
        """set_user_override queues the restyle deferred and no-ops same-value pushes."""
        value = str(value).strip()
        if key not in self._tokens or not value:
            return
        if value == self._tokens[key]:
            return
        self._tokens[key] = value
        self._touched_tokens.add(key)
        self._refresh_swatch(key)
        self._mgr.set_user_override(key, value)

    # ---------- typography / shape editing ----------

    def _refresh_sample(self) -> None:
        font = QFont(self._family)
        font.setPointSize(self._size_pt)
        font.setWeight(QFont.Weight(self._weight))
        self.sample_label.setFont(font)

    def _on_family_changed(self) -> None:
        if self._populating:
            return
        family = self.family_combo.currentText().strip()
        if not family or family == self._family:
            return
        self._family = family
        self._touched.add("family")
        self._mgr.set_user_font(family)
        self._refresh_sample()

    def _on_size_changed(self, value: int) -> None:
        if self._populating or int(value) == self._size_pt:
            return
        self._size_pt = int(value)
        self._touched.add("size")
        self._mgr.set_user_font_size(self._size_pt)
        self._refresh_sample()

    def _on_weight_changed(self, _index: int = 0) -> None:
        if self._populating:
            return
        weight = self.weight_combo.currentData()
        if weight is None or int(weight) == self._weight:
            return
        self._weight = int(weight)
        self._touched.add("weight")
        self._refresh_sample()   # in-dialog only — no app-wide weight axis

    def _on_case_changed(self, _index: int = 0) -> None:
        if self._populating:
            return
        case = self.case_combo.currentData()
        if case is None or str(case) == self._case:
            return
        self._case = str(case)
        self._touched.add("case")
        theming.set_case_override(self._case)

    def _on_radius_changed(self, value: int) -> None:
        if self._populating or int(value) == self._radius_px:
            return
        self._radius_px = int(value)
        self._touched.add("radius")
        self._mgr.set_user_override("radius", f"{self._radius_px}px")

    # ---------- cancel / revert ----------

    def _restore_map(self) -> dict[str, str | None]:
        """Each touched token (and radius) back to its pre-open value, or
        removed. Untouched pre-existing overrides stay — the user's sticky state."""
        restore: dict[str, str | None] = {}
        for key in self._touched_tokens:
            restore[key] = self._snap_overrides.get(key)
        if "radius" in self._touched:
            restore["radius"] = self._snap_overrides.get("radius")
        return restore

    def _revert_previews(self) -> None:
        if not self._touched_tokens and not (
                self._touched & {"radius", "family", "size", "case"}):
            return   # nothing app-wide was previewed — zero-cost close
        self._mgr.apply_bundle(
            font_family=self._snap_font if "family" in self._touched else None,
            font_size=(self._snap_font_size
                       if "size" in self._touched else None),
            case=self._snap_case if "case" in self._touched else None,
            user_overrides=self._restore_map() or None,
        )

    def reject(self) -> None:   # cancel button, Esc, and window close
        if not self._accepted and not self._reverted:
            self._reverted = True
            self._revert_previews()
        super().reject()

    # ---------- save as my theme ----------

    def _on_save_clicked(self) -> None:
        name = self.name_edit.text().strip() or f"my {self._base.name}"
        try:
            slug = self._write_theme(name)
        except OSError as exc:
            self.status_label.setText(f"couldn't write the theme · {exc}")
            return
        # discovery reads the user dir (later wins) — fresh slug in every picker
        self._mgr.refresh()
        # One apply: preview overrides off (the theme carries the edits
        # now), pre-open sticky state back — what picking it would show.
        self._mgr.apply_bundle(
            slug=slug,
            font_family=self._snap_font,
            font_size=self._snap_font_size,
            case=self._snap_case,
            user_overrides=self._restore_map() or None,
        )
        self._saved_slug = slug
        self._accepted = True
        self.theme_saved.emit(slug)
        self.accept()

    def _unique_slug(self, name: str) -> str:
        """Suffixed past ANY existing theme slug, bundled included — a
        user "nord" becomes nord-2 instead of shadowing the shipped one."""
        taken = set(theming.discover_themes())
        base = slugify(name)
        slug, n = base, 2
        while slug in taken or (config.USER_THEMES_DIR / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        return slug

    def _write_theme(self, name: str) -> str:
        """Write the theme dir, return the slug. Config dir read from
        tide.config at call time — never frozen at import."""
        base = self._base
        slug = self._unique_slug(name)
        dest = config.USER_THEMES_DIR / slug
        dest.mkdir(parents=True, exist_ok=True)

        tokens = dict(base.tokens)
        tokens.update(self._tokens)
        typography = dict(base.typography)
        typography.update({
            "family": self._family,
            "size_pt": self._size_pt,
            "weight": self._weight,
            "case": self._case,
        })
        layout = dict(base.layout)
        layout["radius_px"] = self._radius_px
        sections: dict[str, dict] = {
            "meta": {
                "name": name,
                "slug": slug,
                "author": "you",
                "version": 1,
                "dark": bool(base.dark),
                "aesthetic": base.aesthetic,
            },
            "tokens": tokens,
            "typography": typography,
            "layout": layout,
        }
        if base.slots:
            sections["slots"] = dict(base.slots)
        toml_text = _toml_dump(
            sections,
            header=f"saved by the tide theme editor · based on {base.slug}",
        )
        (dest / "theme.toml").write_text(toml_text, encoding="utf-8")

        qss_src = base.path / "theme.qss"
        try:
            qss_text = qss_src.read_text(encoding="utf-8")
        except OSError:
            qss_text = base.qss   # discovery-time copy; "" for qss-less themes
        (dest / "theme.qss").write_text(qss_text, encoding="utf-8")
        return slug
