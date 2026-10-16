"""2.3 appearance widgets for the settings dialog's custom rows.

  * :class:`IconSetPicker` — every icon set as a row of its own icons,
    click one to pick it (``icon_set``).
  * :class:`FontBrowser` — a live font browser popup: search, every
    family drawn in its own face, bundled faces pinned first, a sample
    line (title, numbers) in the highlighted face.
  * :class:`FamilyButton` — a button wearing its family; opens the browser.
  * :class:`TypeRolesEditor` — pairings plus a row per typography role
    (``font_roles``): family, weight, size, tracking, with a live sample.

Each custom widget speaks the dialog's custom-kind contract: ``value()``
returns the storage form, ``set_value(raw)`` loads one, ``normalize(raw)``
maps any raw value to the storage form (so an untouched row never reads
as a diff), and ``changed`` fires on every user edit.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from . import icon_sets, typefaces

SAMPLE_TEXT = "the quick brown fox · 0:42 / 3:10 · 1.25×"


# --------------------------------------------------------------------------
# icon sets
# --------------------------------------------------------------------------

def _icon_pixmap(svg: str, ink: str, px: int) -> QPixmap:
    pix = QPixmap(px, px)
    pix.fill(Qt.transparent)
    r = QSvgRenderer(QByteArray(svg.replace("currentColor", ink).encode("utf-8")))
    p = QPainter(pix)
    r.render(p, QRectF(0, 0, px, px))
    p.end()
    return pix


class _SetRow(QPushButton):
    """One set: its name, then its icons drawn in the theme's ink."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.set_name = name
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setObjectName("IconSetRow")
        self.setMinimumHeight(34)

    def paintEvent(self, ev) -> None:
        theme = theming.manager().current_effective()
        fg = theme.token("fg", "#e6e6e6") if theme else "#e6e6e6"
        accent = theme.token("accent", "#d4b95e") if theme else "#d4b95e"
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self.isChecked() or self.underMouse():
            c = QColor(accent if self.isChecked() else fg)
            c.setAlpha(46 if self.isChecked() else 22)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            radius = float(theming.effective_radius_px(theme)) if theme else 0.0
            p.drawRoundedRect(QRectF(self.rect()), radius, radius)
        p.setPen(QColor(accent if self.isChecked() else fg))
        label = theming.styled_case(self.set_name)
        p.drawText(self.rect().adjusted(10, 0, 0, 0),
                   Qt.AlignVCenter | Qt.AlignLeft, label)
        px = 18
        x = 110
        y = (self.height() - px) // 2
        for key in icon_sets.SAMPLE_KEYS:
            svg = icon_sets.svg_for(key, self.set_name)
            if svg:
                p.drawPixmap(x, y, _icon_pixmap(svg, fg, px))
            x += px + 10
        p.end()

    def sizeHint(self) -> QSize:
        return QSize(110 + len(icon_sets.SAMPLE_KEYS) * 28 + 10, 34)


class IconSetPicker(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self.rows: dict[str, _SetRow] = {}
        for name in icon_sets.SETS:
            row = _SetRow(name)
            row.setToolTip(icon_sets.LABELS[name])
            row.clicked.connect(lambda _=False, n=name: self._pick(n))
            self.rows[name] = row
            col.addWidget(row)
        self._value = "regular"
        self.set_value("regular")
        theming.manager().theme_changed.connect(lambda _t: self.update())

    def normalize(self, raw) -> str:
        return raw if raw in icon_sets.SETS else "regular"

    def value(self) -> str:
        return self._value

    def set_value(self, raw) -> None:
        self._value = self.normalize(raw)
        for n, r in self.rows.items():
            r.setChecked(n == self._value)

    def _pick(self, name: str) -> None:
        changed = name != self._value
        self.set_value(name)
        if changed:
            self.changed.emit()


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------

def font_families() -> list[str]:
    """Bundled faces first, then every Latin-capable system family."""
    out = list(typefaces.BUNDLED)
    seen = set(out)
    latin = QFontDatabase.WritingSystem.Latin
    for f in sorted(QFontDatabase.families()):
        if f in seen or f.startswith("."):
            continue
        systems = QFontDatabase.writingSystems(f)
        if systems and latin not in systems:
            continue        # symbol / emoji / CJK-only: useless for UI text
        out.append(f)
        seen.add(f)
    return out


class FontBrowser(QFrame):
    """A popup font browser. ``picked(family)`` ("" = from theme)."""

    picked = Signal(str)

    def __init__(self, current: str = "", sample: str = SAMPLE_TEXT,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("FontBrowser")
        self.setFrameShape(QFrame.Box)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._sample_text = sample
        self.search = QLineEdit()
        self.search.setPlaceholderText(theming.styled_case("search fonts"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setViewMode(QListView.ListMode)
        self.list.currentItemChanged.connect(self._preview)
        self.list.itemActivated.connect(self._accept_item)
        self.list.itemClicked.connect(self._accept_item)
        self.sample = QLabel(sample)
        self.sample.setWordWrap(True)
        self.sample.setMinimumHeight(48)
        theme_item = QListWidgetItem(theming.styled_case("from theme"))
        theme_item.setData(Qt.UserRole, "")
        self.list.addItem(theme_item)
        for fam in font_families():
            label = f"{fam}  ·  bundled" if fam in typefaces.BUNDLED else fam
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, fam)
            f = QFont(fam)
            f.setPointSize(11)
            it.setFont(f)
            self.list.addItem(it)
            if fam == current:
                self.list.setCurrentItem(it)
        if self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 10, 10, 10)
        col.setSpacing(6)
        col.addWidget(self.search)
        col.addWidget(self.list, stretch=1)
        col.addWidget(self.sample)
        self.resize(360, 420)

    def families(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole)
                for i in range(self.list.count())]

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        first = None
        for i in range(self.list.count()):
            it = self.list.item(i)
            fam = it.data(Qt.UserRole) or ""
            hide = bool(q) and q not in fam.lower() and q not in it.text().lower()
            it.setHidden(hide)
            if not hide and first is None:
                first = it
        if first is not None and (self.list.currentItem() is None
                                  or self.list.currentItem().isHidden()):
            self.list.setCurrentItem(first)

    def _preview(self, item, _prev=None) -> None:
        if item is None:
            return
        fam = item.data(Qt.UserRole) or ""
        f = QFont(fam) if fam else QFont(self.font())
        f.setPointSize(14)
        self.sample.setFont(f)
        # The sheet's ``*`` rule would paint the sample in the app face.
        self.sample.setStyleSheet(
            f'font-family: "{fam}"; font-size: 14pt;' if fam else "font-size: 14pt;")

    def _accept_item(self, item) -> None:
        if item is None:
            return
        self.picked.emit(item.data(Qt.UserRole) or "")
        self.close()

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._accept_item(self.list.currentItem())
            return
        if ev.key() in (Qt.Key_Down, Qt.Key_Up) and self.search.hasFocus():
            self.list.setFocus()
            self.list.keyPressEvent(ev)
            return
        super().keyPressEvent(ev)

    def show_under(self, anchor: QWidget) -> None:
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 2))
        screen = anchor.screen()
        if screen is not None:
            g = screen.availableGeometry()
            if pos.y() + self.height() > g.bottom():
                pos.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 2)
            pos.setX(max(g.left(), min(pos.x(), g.right() - self.width())))
        self.move(pos)
        self.show()
        self.search.setFocus()


class FamilyButton(QPushButton):
    """Shows a family in its own face; click → the font browser."""

    family_changed = Signal(str)

    def __init__(self, family: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._family = ""
        self.clicked.connect(self._open)
        self.set_family(family)

    def family(self) -> str:
        return self._family

    def set_family(self, family: str) -> None:
        self._family = (family or "").strip()
        self.setText(self._family or theming.styled_case("from theme"))
        if self._family:
            self.setStyleSheet(f'font-family: "{self._family}";')
        else:
            self.setStyleSheet("")

    def _open(self) -> None:
        # Deferred out of the click emission (the py3.14 popup rule).
        QTimer.singleShot(0, self._open_now)

    def _open_now(self) -> None:
        browser = FontBrowser(self._family, parent=self.window())
        browser.picked.connect(self._picked)
        self._browser = browser
        browser.show_under(self)

    def _picked(self, family: str) -> None:
        if family != self._family:
            self.set_family(family)
            self.family_changed.emit(family)


class TypeRolesEditor(QWidget):
    """Pairings + one row per role. ``value()`` is the compact roles dict
    (``font_roles``); the body's family is the separate font row — a
    pairing writes it through ``ui_family_requested``."""

    changed = Signal()
    ui_family_requested = Signal(str)

    def __init__(self, personality: str = "modern", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._personality = personality if personality in typefaces.PAIRINGS else "modern"
        self._roles = typefaces.defaults()
        self._loading = False
        self._ui_family = ""

        self.pairing = QComboBox()
        self.pairing.addItem(theming.styled_case("custom"), "")
        for slug, label, _spec in typefaces.pairings_for(self._personality):
            self.pairing.addItem(theming.styled_case(label), slug)
        self.pairing.activated.connect(self._on_pairing)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        for c, head in enumerate(("", "face", "weight", "size", "tracking")):
            if head:
                h = QLabel(theming.styled_case(head))
                h.setProperty("class", "dim")
                grid.addWidget(h, 0, c)
        self.rows: dict[str, dict[str, QWidget]] = {}
        for r, role in enumerate(typefaces.ROLES, start=1):
            grid.addWidget(QLabel(theming.styled_case(typefaces.ROLE_LABELS[role])), r, 0)
            widgets: dict[str, QWidget] = {}
            if role == "ui":
                same = QLabel(theming.styled_case("the font row above"))
                same.setProperty("class", "dim")
                grid.addWidget(same, r, 1)
            else:
                fam = FamilyButton()
                fam.family_changed.connect(lambda _f, ro=role: self._edit(ro))
                widgets["family"] = fam
                grid.addWidget(fam, r, 1)
            weight = QComboBox()
            for w, label in typefaces.WEIGHTS:
                weight.addItem(theming.styled_case(label), w)
            weight.currentIndexChanged.connect(lambda _i, ro=role: self._edit(ro))
            widgets["weight"] = weight
            grid.addWidget(weight, r, 2)
            if role != "ui":
                size = QSpinBox()
                size.setRange(*typefaces.SIZE_RANGE)
                size.setSingleStep(5)
                size.setSuffix("%")
                size.valueChanged.connect(lambda _v, ro=role: self._edit(ro))
                widgets["size"] = size
                grid.addWidget(size, r, 3)
            spacing = QSpinBox()
            spacing.setRange(*typefaces.SPACING_RANGE)
            spacing.setSuffix("%")
            spacing.setToolTip(theming.styled_case("letter spacing, added to normal"))
            spacing.valueChanged.connect(lambda _v, ro=role: self._edit(ro))
            widgets["spacing"] = spacing
            grid.addWidget(spacing, r, 4)
            self.rows[role] = widgets

        self.sample_title = QLabel("song title")
        self.sample_numbers = QLabel("0:42 / 3:10   1.25×")
        sample_row = QHBoxLayout()
        sample_row.addWidget(self.sample_title, stretch=1)
        sample_row.addWidget(self.sample_numbers)

        pair_row = QHBoxLayout()
        pair_row.addWidget(QLabel(theming.styled_case("pairing")))
        pair_row.addWidget(self.pairing, stretch=1)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        col.addLayout(pair_row)
        col.addLayout(grid)
        col.addLayout(sample_row)
        self._load()

    # -- contract ----------------------------------------------------------

    def normalize(self, raw) -> dict:
        return typefaces.compact(raw)

    def value(self) -> dict:
        return typefaces.compact(self._roles)

    def set_value(self, raw) -> None:
        self._roles = typefaces.normalize(raw)
        self._load()

    def set_ui_family(self, family: str) -> None:
        """The body family lives in the font row; mirrored here only to
        recognise which pairing is on."""
        self._ui_family = (family or "").strip()
        self._sync_pairing()

    # -- internals ---------------------------------------------------------

    def _load(self) -> None:
        self._loading = True
        try:
            for role, w in self.rows.items():
                spec = self._roles[role]
                if "family" in w:
                    w["family"].set_family(spec["family"])
                idx = w["weight"].findData(spec["weight"])
                w["weight"].setCurrentIndex(max(0, idx))
                if "size" in w:
                    w["size"].setValue(spec["size"])
                w["spacing"].setValue(spec["spacing"])
        finally:
            self._loading = False
        self._sync_pairing()
        self._refresh_sample()

    def _edit(self, role: str) -> None:
        if self._loading:
            return
        w = self.rows[role]
        spec = self._roles[role]
        if "family" in w:
            spec["family"] = w["family"].family()
        spec["weight"] = int(w["weight"].currentData() or 0)
        if "size" in w:
            spec["size"] = int(w["size"].value())
        spec["spacing"] = int(w["spacing"].value())
        self._roles = typefaces.normalize(self._roles)
        self._sync_pairing()
        self._refresh_sample()
        self.changed.emit()

    def _on_pairing(self, _index: int) -> None:
        slug = self.pairing.currentData()
        if not slug:
            return
        spec = typefaces.pairing(self._personality, slug)
        if spec is None:
            return
        ui_family = spec["ui"]["family"]
        self._roles = spec
        self._load()
        self.ui_family_requested.emit(ui_family)
        self._ui_family = ui_family
        self._sync_pairing()
        self.changed.emit()

    def _sync_pairing(self) -> None:
        slug = typefaces.matching_pairing(self._personality, self._roles,
                                          self._ui_family)
        idx = self.pairing.findData(slug)
        self.pairing.blockSignals(True)
        self.pairing.setCurrentIndex(max(0, idx))
        self.pairing.blockSignals(False)

    def _refresh_sample(self) -> None:
        base = QFont(self.font())
        base_pt = base.pointSizeF() if base.pointSizeF() > 0 else 10.0
        for label, role, scale_ in ((self.sample_title, "title", 1.4),
                                    (self.sample_numbers, "numbers", 1.0)):
            spec = self._roles[role]
            f = QFont(base)
            f.setPointSizeF(base_pt * scale_ * spec["size"] / 100.0)
            if spec["family"]:
                f.setFamily(spec["family"])
            if spec["weight"]:
                f.setWeight(QFont.Weight(spec["weight"]))
            if spec["spacing"]:
                f.setLetterSpacing(QFont.PercentageSpacing, 100 + spec["spacing"])
            label.setFont(f)
            css = f"font-size: {f.pointSizeF():.1f}pt;"
            if spec["family"]:
                css += f' font-family: "{spec["family"]}";'
            if spec["weight"]:
                css += f" font-weight: {spec['weight']};"
            label.setStyleSheet(css)


# --------------------------------------------------------------------------
# settings-dialog builders
# --------------------------------------------------------------------------

def build_icon_set_picker(dialog) -> IconSetPicker:
    return IconSetPicker()


def build_type_roles_editor(dialog) -> TypeRolesEditor:
    s = getattr(dialog, "_settings", None)
    personality = getattr(s, "preset", "") or "modern"
    ed = TypeRolesEditor(personality)
    ed.set_ui_family(getattr(s, "font_family_override", "") or "")

    def _ui_family(family: str) -> None:
        desc = dialog._by_key.get("font_family_override")
        if desc is not None:
            dialog._set_widget_value(desc, family)

    ed.ui_family_requested.connect(_ui_family)
    picker = dialog.widget_for("font_family_override") \
        if hasattr(dialog, "widget_for") else None
    if picker is not None and hasattr(picker, "currentIndexChanged"):
        picker.currentIndexChanged.connect(
            lambda _i: ed.set_ui_family(picker.currentData() or ""))
    return ed
