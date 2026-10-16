"""The now-playing bar's customizer (2.3).

Three pieces:
  * :class:`LongPress` — watches the strip; a held press (or a
    right-click) on EMPTY bar space — not a control, not the art, not
    the clickable title — emits ``triggered(QPoint)`` at the press.
  * :class:`BarEditor` — the editor itself, shared by the popover and
    the settings dialog's "player bar" row: a controls page (show /
    order by drag / S·M·L each), an info page (the fields on each info
    line as chips you click and drag, plus where "next:" sits) and a
    layout page (art size, time readout, density, the slot styles).
    Speaks ``bar_config`` dicts; emits ``changed(dict)`` on every edit.
  * :class:`BarCustomizerPopover` — the editor in a panel that floats
    over the bar where the press landed. Live: every edit reaches the
    real bar at once (the window saves it, field-scoped).

Everything here is built with the bar's own BracketButtons, so it wears
the active personality without a face of its own: brackets and an
inverting hover in brutalist, soft pills in modern.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theming
from . import bar_config
from .flow import FlowLayout
from .widgets import BracketButton, paint_popover_panel

_CLEAR = "BarEditorClear"   # containers that must not paint @bg
HOLD_MS = 480          # press-and-hold before the customizer opens
SLOP_PX = 10           # travel that turns a hold into a drag (cancels)


def _modern(theme=None) -> bool:
    theme = theme or theming.manager().current_effective()
    return getattr(theme, "aesthetic", "") == "modern"


def _tok(name: str, default: str) -> QColor:
    theme = theming.manager().current_effective()
    return QColor(theme.token(name, default) if theme is not None else default)


# --------------------------------------------------------------------------
# long-press
# --------------------------------------------------------------------------

class _HoldRing(QWidget):
    """The hold's progress, a ring filling around the finger. Purely a
    hint — mouse-transparent, gone on release."""

    SIZE = 34

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._t = 0.0
        self.hide()

    def set_progress(self, t: float) -> None:
        self._t = max(0.0, min(1.0, float(t)))
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        dim = _tok("dim", "#666")
        dim.setAlpha(90)
        p.setPen(QPen(dim, 2.5))
        p.drawEllipse(r)
        accent = _tok("accent", "#d4b95e")
        pen = QPen(accent, 3.0)
        pen.setCapStyle(Qt.RoundCap if _modern() else Qt.FlatCap)
        p.setPen(pen)
        p.drawArc(r, 90 * 16, int(-360 * 16 * self._t))


class LongPress(QObject):
    """Press-and-hold / right-click on empty space of ``host``.

    "Empty" is the host itself, a layout gap, or a plain QLabel (the
    time and "next:" readouts) — presses on controls never reach here
    because buttons accept them; the title label and the art do too, so
    they're excluded by type. Emits the GLOBAL press position."""

    triggered = Signal(QPoint)

    def __init__(self, host: QWidget, extra=(), parent: QObject | None = None) -> None:
        super().__init__(parent or host)
        self._host = host
        self._extra = tuple(extra)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(HOLD_MS)
        self._timer.timeout.connect(self._fire)
        self._tick = QTimer(self)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._advance)
        self._elapsed = 0
        self._press_global: QPoint | None = None
        self._ring = _HoldRing(host)
        host.installEventFilter(self)

    def is_empty_at(self, pos: QPoint) -> bool:
        child = self._host.childAt(pos)
        if child is None:
            return True
        if child in self._extra:
            return True
        # Exactly QLabel: subclasses (the art tile, painted readouts)
        # have click behaviour of their own.
        return type(child) is QLabel

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 (Qt)
        if obj is not self._host:
            return False
        t = ev.type()
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            pos = ev.position().toPoint()
            if self.is_empty_at(pos):
                self._begin(pos, ev.globalPosition().toPoint())
        elif t == QEvent.MouseMove and self._press_global is not None:
            d = ev.globalPosition().toPoint() - self._press_global
            if d.manhattanLength() > SLOP_PX:
                self.cancel()
        elif t in (QEvent.MouseButtonRelease, QEvent.Leave, QEvent.Hide):
            self.cancel()
        elif t == QEvent.ContextMenu:
            pos = ev.pos()
            if self.is_empty_at(pos):
                self._context_menu(ev.globalPos())
                return True
        return False

    def _begin(self, pos: QPoint, global_pos: QPoint) -> None:
        self._press_global = global_pos
        self._elapsed = 0
        from . import motion as motion_module
        if motion_module.intensity() != motion_module.Intensity.OFF:
            half = _HoldRing.SIZE // 2
            self._ring.move(pos.x() - half, pos.y() - half)
            self._ring.set_progress(0.0)
            self._ring.show()
            self._ring.raise_()
            self._tick.start()
        self._timer.start()

    def _advance(self) -> None:
        self._elapsed += self._tick.interval()
        self._ring.set_progress(self._elapsed / HOLD_MS)

    def cancel(self) -> None:
        self._timer.stop()
        self._tick.stop()
        self._ring.hide()
        self._press_global = None

    def _fire(self) -> None:
        pos = self._press_global
        self.cancel()
        if pos is not None:
            self.triggered.emit(pos)

    def _context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self._host)
        menu.setAttribute(Qt.WA_DeleteOnClose, True)
        act = menu.addAction(theming.styled_case("customize bar…"))
        act.triggered.connect(lambda: self.triggered.emit(global_pos))
        # popup(), never exec(): no nested loop inside the event filter.
        menu.popup(global_pos)
        self._menu = menu


# --------------------------------------------------------------------------
# small controls
# --------------------------------------------------------------------------

class Segmented(QWidget):
    """A row of BracketButtons, one latched (accent). ``picked(value)``."""

    picked = Signal(object)

    def __init__(self, options, current=None, parent: QWidget | None = None,
                 *, compact: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName(_CLEAR)
        self._buttons: dict[object, BracketButton] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2 if compact else 4)
        for value, label in options:
            b = BracketButton(label)
            b.setCursor(Qt.PointingHandCursor)
            if compact:
                b.setSizeFactor(0.85)
            b.clicked.connect(lambda _=False, v=value: self._on_click(v))
            self._buttons[value] = b
            row.addWidget(b)
        row.addStretch(1)
        self._value = None
        self.set_value(current)

    def _on_click(self, value) -> None:
        if value == self._value:
            return
        self.set_value(value)
        self.picked.emit(value)

    def set_value(self, value) -> None:
        self._value = value
        for v, b in self._buttons.items():
            b.setActiveState(v == value)

    def value(self):
        return self._value

    def button(self, value) -> BracketButton | None:
        return self._buttons.get(value)


class ReorderList(QWidget):
    """Rows (or wrapping chips) you drag to reorder.

    ``add(key, widget, handle)`` — the handle is where a drag starts
    (the whole chip, or a ⠿ grip). A press that doesn't travel stays a
    click for the widget under it; one that does becomes a drag, and the
    release emits ``moved(key, new_index)``. An accent line marks where
    it will land."""

    moved = Signal(str, int)

    def __init__(self, *, flow: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(_CLEAR)
        self._flow = flow
        if flow:
            self._lay = FlowLayout(self, h_gap=4, v_gap=4)
        else:
            self._lay = QVBoxLayout(self)
            self._lay.setSpacing(3)
        self._lay.setContentsMargins(0, 2, 0, 2)
        self._rows: list[tuple[str, QWidget]] = []
        self._handles: dict[QObject, str] = {}
        self._drag_key: str | None = None
        self._press: QPoint | None = None
        self._dragging = False
        self._drop: int | None = None

    def clear(self) -> None:
        for _key, w in self._rows:
            self._lay.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._rows.clear()
        self._handles.clear()

    def add(self, key: str, widget: QWidget, handle: QWidget | None = None) -> None:
        self._rows.append((key, widget))
        self._lay.addWidget(widget)
        handle = handle or widget
        self._handles[handle] = key
        handle.installEventFilter(self)

    def keys(self) -> list[str]:
        return [k for k, _ in self._rows]

    def row(self, key: str) -> QWidget | None:
        for k, w in self._rows:
            if k == key:
                return w
        return None

    # -- drag ------------------------------------------------------------

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 (Qt)
        key = self._handles.get(obj)
        if key is None:
            return False
        t = ev.type()
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            self._drag_key = key
            self._press = ev.globalPosition().toPoint()
            self._dragging = False
            return False
        if t == QEvent.MouseMove and self._drag_key is not None:
            gp = ev.globalPosition().toPoint()
            if not self._dragging and \
                    (gp - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._dragging = True
                if isinstance(obj, BracketButton):
                    obj.setDown(False)
                QApplication.setOverrideCursor(Qt.ClosedHandCursor)
            if self._dragging:
                self._drop = self.drop_index_at(self.mapFromGlobal(gp))
                self.update()
                return True
            return False
        if t == QEvent.MouseButtonRelease and self._drag_key is not None:
            dragged, key = self._dragging, self._drag_key
            self._drag_key, self._press = None, None
            if dragged:
                QApplication.restoreOverrideCursor()
                self._dragging = False
                drop, self._drop = self._drop, None
                self.update()
                if isinstance(obj, BracketButton):
                    obj.setDown(False)
                if drop is not None:
                    self.move_key(key, drop)
                return True     # a drag is never also a click
        return False

    def drop_index_at(self, pos: QPoint) -> int:
        """Insertion index (before which row) for a point in our coords."""
        if not self._rows:
            return 0
        if not self._flow:
            for i, (_k, w) in enumerate(self._rows):
                if pos.y() < w.geometry().center().y():
                    return i
            return len(self._rows)
        # Wrapped chips: before the first chip on the pointer's line
        # whose centre is right of it; past every chip above it.
        best = 0
        for i, (_k, w) in enumerate(self._rows):
            g = w.geometry()
            if g.top() - 4 <= pos.y() <= g.bottom() + 4:
                if pos.x() < g.center().x():
                    return i
                best = i + 1
            elif pos.y() > g.bottom():
                best = i + 1
        return best

    def move_key(self, key: str, drop: int) -> None:
        """Move ``key`` so it lands before what is now at ``drop``."""
        keys = self.keys()
        if key not in keys:
            return
        cur = keys.index(key)
        new = drop if drop <= cur else drop - 1
        new = max(0, min(new, len(keys) - 1))
        if new != cur:
            self.moved.emit(key, new)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if not self._dragging or self._drop is None or not self._rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(_tok("accent", "#d4b95e"), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        i = self._drop
        if self._flow:
            if i < len(self._rows):
                g = self._rows[i][1].geometry()
                x = g.left() - 2
            else:
                g = self._rows[-1][1].geometry()
                x = g.right() + 2
            p.drawLine(QPointF(x, g.top()), QPointF(x, g.bottom()))
        else:
            if i < len(self._rows):
                y = self._rows[i][1].geometry().top() - 2
            else:
                y = self._rows[-1][1].geometry().bottom() + 2
            p.drawLine(QPointF(4, y), QPointF(self.width() - 4, y))


# --------------------------------------------------------------------------
# the editor
# --------------------------------------------------------------------------

class _ControlRow(QWidget):
    """⠿  [x] label ............ [s][m][l]"""

    def __init__(self, item: str, shown: bool, size: str) -> None:
        super().__init__()
        self.setObjectName(_CLEAR)
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        self.handle = QLabel("⠿")
        self.handle.setProperty("class", "dim")
        self.handle.setCursor(Qt.OpenHandCursor)
        self.handle.setToolTip(theming.styled_case("drag to reorder"))
        row.addWidget(self.handle)
        if item == bar_config.GAP:
            gap = QLabel(theming.styled_case(bar_config.LABELS[item]))
            gap.setProperty("class", "dim")
            gap.setToolTip(theming.styled_case(
                "everything after this line sits at the right end of the bar"))
            row.addWidget(gap, stretch=1)
            self.check = None
            self.sizes = None
            return
        self.check = QCheckBox(theming.styled_case(bar_config.LABELS[item]))
        self.check.setChecked(shown)
        if item in bar_config.ALWAYS_SHOWN:
            self.check.setEnabled(False)
            self.check.setToolTip(theming.styled_case(
                "play stays — a bar you can't play from isn't a bar"))
        row.addWidget(self.check, stretch=1)
        # Capitals: a lowercase l in a mono face reads as a 1.
        self.sizes = Segmented([(s, s.upper()) for s in bar_config.SIZES], size,
                               compact=True)
        self.sizes.setToolTip(theming.styled_case("size"))
        row.addWidget(self.sizes)


class BarEditor(QWidget):
    """The bar's editor. ``value()`` / ``set_value(cfg)``; ``changed``
    fires with the full normalized config after every edit."""

    changed = Signal(dict)
    styles_requested = Signal()

    PAGES = ("controls", "info", "layout")

    def __init__(self, cfg=None, variant: str = "stacked",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(_CLEAR)
        # The app sheet paints every QWidget @bg; the editor's own
        # containers must let the panel behind them show.
        self.setStyleSheet(f"QWidget#{_CLEAR} {{ background: transparent; }}")
        self._cfg = bar_config.normalize(cfg)
        self._variant = variant or "stacked"
        self._building = False

        tabs = QHBoxLayout()
        tabs.setContentsMargins(0, 0, 0, 0)
        self.tabs = Segmented([(p, p) for p in self.PAGES], "controls")
        self.tabs.picked.connect(self._show_page)
        tabs.addWidget(self.tabs)

        self.stack = QStackedWidget()
        self.stack.setObjectName(_CLEAR)
        self.stack.addWidget(self._controls_page())
        self.stack.addWidget(self._info_page())
        self.stack.addWidget(self._layout_page())

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addLayout(tabs)
        col.addWidget(self.stack, stretch=1)
        self._refresh()

    # -- API ---------------------------------------------------------------

    def normalize(self, raw) -> dict:
        """The storage form (Settings.bar_layout): ``{}`` for the shipped
        bar, else the full normalized config."""
        cfg = bar_config.normalize(raw)
        return {} if cfg == bar_config.defaults() else cfg

    def value(self) -> dict:
        return self.normalize(self._cfg)

    def set_value(self, cfg) -> None:
        self._cfg = bar_config.normalize(cfg)
        self._refresh()

    def set_variant(self, variant: str) -> None:
        self._variant = variant or "stacked"
        self._refresh()

    def page(self) -> str:
        return self.PAGES[self.stack.currentIndex()]

    def _show_page(self, name) -> None:
        self.tabs.set_value(name)
        self.stack.setCurrentIndex(self.PAGES.index(name))

    def _commit(self, cfg) -> None:
        cfg = bar_config.normalize(cfg)
        if cfg == self._cfg:
            return
        self._cfg = cfg
        self._refresh()
        self.changed.emit(self.value())

    # -- pages -------------------------------------------------------------

    def _controls_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName(_CLEAR)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        hint = QLabel(theming.styled_case(
            "tick what shows · drag ⠿ to reorder · S / M / L to size"))
        hint.setProperty("class", "dim")
        hint.setWordWrap(True)
        col.addWidget(hint)
        self.controls = ReorderList()
        self.controls.moved.connect(
            lambda key, i: self._commit(bar_config.move(self._cfg, key, i)))
        col.addWidget(self.controls)
        col.addStretch(1)
        return page

    def _chip_line(self, which: str) -> tuple[QWidget, ReorderList, QWidget]:
        box = QWidget()
        box.setObjectName(_CLEAR)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        head = QHBoxLayout()
        title = QLabel(theming.styled_case(
            "line 1 · the big one" if which == "line1" else "line 2 · the small one"))
        head.addWidget(title)
        head.addStretch(1)
        own = BracketButton(theming.styled_case("style's own"))
        own.setToolTip(theming.styled_case(
            "follow the label style's own fields (strip builder → label)"))
        own.clicked.connect(lambda: self._commit(
            bar_config.with_change(self._cfg, **{which: None})))
        head.addWidget(own)
        col.addLayout(head)
        chips = ReorderList(flow=True)
        chips.moved.connect(lambda key, i, w=which: self._move_chip(w, key, i))
        col.addWidget(chips)
        return box, chips, own

    def _info_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName(_CLEAR)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        hint = QLabel(theming.styled_case(
            "click a chip to show it · drag to reorder"))
        hint.setProperty("class", "dim")
        hint.setWordWrap(True)
        col.addWidget(hint)
        box1, self.line1_chips, self.line1_own = self._chip_line("line1")
        box2, self.line2_chips, self.line2_own = self._chip_line("line2")
        col.addWidget(box1)
        col.addWidget(box2)
        nxt = QHBoxLayout()
        nxt.addWidget(QLabel(theming.styled_case("next:")))
        self.next_picker = Segmented(
            [(m, bar_config.CHOICE_LABELS["next"][m].split(" (")[0])
             for m in bar_config.NEXT_MODES], self._cfg["next"], compact=True)
        self.next_picker.picked.connect(
            lambda v: self._commit(bar_config.with_change(self._cfg, next=v)))
        nxt.addWidget(self.next_picker, stretch=1)
        col.addLayout(nxt)
        col.addStretch(1)
        return page

    def _layout_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName(_CLEAR)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        self.layout_pickers: dict[str, Segmented] = {}
        for key, title in (("art", "art"), ("time", "time"),
                           ("density", "density"), ("waveform", "waveform")):
            row = QHBoxLayout()
            lbl = QLabel(theming.styled_case(title))
            lbl.setMinimumWidth(64)
            row.addWidget(lbl)
            labels = bar_config.CHOICE_LABELS[key]
            seg = Segmented(list(labels.items()), self._cfg[key], compact=True)
            seg.picked.connect(lambda v, k=key: self._commit(
                bar_config.with_change(self._cfg, **{k: v})))
            self.layout_pickers[key] = seg
            row.addWidget(seg, stretch=1)
            col.addLayout(row)
        styles = QHBoxLayout()
        blurb = QLabel(theming.styled_case(
            "progress, volume, art, buttons and label styles:"))
        blurb.setProperty("class", "dim")
        blurb.setWordWrap(True)
        styles.addWidget(blurb, stretch=1)
        self.styles_btn = BracketButton(theming.styled_case("styles  →"))
        self.styles_btn.clicked.connect(self.styles_requested.emit)
        styles.addWidget(self.styles_btn)
        col.addLayout(styles)
        self.reset_btn = BracketButton(theming.styled_case("reset the whole bar"))
        self.reset_btn.clicked.connect(
            lambda: self._commit(bar_config.defaults()))
        col.addWidget(self.reset_btn, alignment=Qt.AlignLeft)
        col.addStretch(1)
        return page

    # -- refresh -----------------------------------------------------------

    def _refresh(self) -> None:
        self._building = True
        try:
            self._refresh_controls()
            self._refresh_lines()
            self.next_picker.set_value(self._cfg["next"])
            for key, seg in self.layout_pickers.items():
                seg.set_value(self._cfg[key])
        finally:
            self._building = False

    def _refresh_controls(self) -> None:
        self.controls.clear()
        hidden = set(self._cfg["hidden"])
        for item in self._cfg["order"]:
            row = _ControlRow(item, item not in hidden,
                              self._cfg["sizes"].get(item, "m"))
            if row.check is not None:
                row.check.toggled.connect(
                    lambda on, i=item: self._commit(
                        bar_config.set_shown(self._cfg, i, on)))
                row.sizes.picked.connect(
                    lambda v, i=item: self._commit(
                        bar_config.set_size(self._cfg, i, v)))
            # Alt+↑ / Alt+↓ move the focused row — drag isn't the only way.
            for seq, delta in (("Alt+Up", -1), ("Alt+Down", 1)):
                sc = QShortcut(QKeySequence(seq), row)
                sc.setContext(Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(lambda i=item, d=delta: self._nudge(i, d))
            self.controls.add(item, row, row.handle)

    def _nudge(self, item: str, delta: int) -> None:
        order = self._cfg["order"]
        i = order.index(item)
        self._commit(bar_config.move(self._cfg, item, max(0, i + delta)))

    def _line_order(self, which: str) -> tuple[list[str], set[str]]:
        l1, l2 = bar_config.lines_for(self._cfg, self._variant)
        active = list(l1 if which == "line1" else l2)
        rest = [f for f in bar_config.FIELDS if f not in active]
        return active + rest, set(active)

    def _refresh_lines(self) -> None:
        for which, chips, own in (("line1", self.line1_chips, self.line1_own),
                                  ("line2", self.line2_chips, self.line2_own)):
            chips.clear()
            order, active = self._line_order(which)
            for f in order:
                chip = BracketButton(theming.styled_case(bar_config.FIELD_LABELS[f]))
                chip.setActiveState(f in active)
                chip.setCursor(Qt.PointingHandCursor)
                chip.clicked.connect(
                    lambda _=False, w=which, fld=f: self._toggle_field(w, fld))
                chips.add(f, chip)
            own.setActiveState(self._cfg[which] is None)

    def _toggle_field(self, which: str, field: str) -> None:
        order, active = self._line_order(which)
        if field in active:
            active.discard(field)
            order.remove(field)
            fields = [f for f in order if f in active]
        else:
            fields = [f for f in order if f in active] + [field]
        self._commit(bar_config.with_change(self._cfg, **{which: fields}))

    def _move_chip(self, which: str, field: str, index: int) -> None:
        order, active = self._line_order(which)
        order.remove(field)
        order.insert(index, field)
        self._commit(bar_config.with_change(
            self._cfg, **{which: [f for f in order if f in active]}))


# --------------------------------------------------------------------------
# the popover
# --------------------------------------------------------------------------

class BarCustomizerPopover(QFrame):
    """The editor floating over the bar. ``changed`` relays every edit
    (the window applies + saves it live); "all settings" hands off to
    the full dialog."""

    changed = Signal(dict)
    open_settings_requested = Signal()

    def __init__(self, cfg, variant: str = "stacked",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("BarCustomizer")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._theme = theming.manager().current_effective()
        theming.manager().theme_changed.connect(self._on_theme)

        title = QLabel(theming.styled_case("customize bar"))
        title.setStyleSheet("font-weight: 600;")
        self.settings_btn = BracketButton(theming.styled_case("all settings"))
        self.settings_btn.setSizeFactor(0.85)
        self.settings_btn.clicked.connect(self._to_settings)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.settings_btn)

        self.editor = BarEditor(cfg, variant)
        self.editor.changed.connect(self.changed.emit)
        self.editor.styles_requested.connect(self._open_styles)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        root.addLayout(head)
        root.addWidget(self.editor)
        self.setMinimumWidth(440)

    def _on_theme(self, theme) -> None:
        self._theme = theme
        self.update()

    def _to_settings(self) -> None:
        self.close()
        self.open_settings_requested.emit()

    def _open_styles(self) -> None:
        win = self.parentWidget()
        self.close()
        settings = getattr(win, "_settings", None)
        from .strip_builder import open_strip_builder
        open_strip_builder(
            win, dict(getattr(settings, "layout_overrides", {}) or {}),
            on_chosen=getattr(win, "apply_strip_overrides", None))

    def show_over(self, anchor: QWidget, global_pos: QPoint | None = None) -> None:
        """Above ``anchor`` (the bar), centred on the press when given,
        kept on screen; flips below when there's no room above."""
        self.adjustSize()
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        cx = global_pos.x() if global_pos is not None else \
            top_left.x() + anchor.width() // 2
        x = cx - self.width() // 2
        y = top_left.y() - self.height() - 6
        screen = anchor.screen()
        if screen is not None:
            g = screen.availableGeometry()
            if y < g.top():
                y = top_left.y() + anchor.height() + 6
            x = max(g.left() + 6, min(x, g.right() - self.width() - 6))
            y = max(g.top() + 6, min(y, g.bottom() - self.height() - 6))
        self.move(x, y)
        self.show()
        self.raise_()

    def paintEvent(self, ev) -> None:
        if _modern(self._theme):
            paint_popover_panel(self, self._theme)
        else:
            p = QPainter(self)
            bg = QColor(self._theme.token("bg", "#0b0b0b")) if self._theme else QColor("#0b0b0b")
            fg = QColor(self._theme.token("fg", "#e6e6e6")) if self._theme else QColor("#e6e6e6")
            p.fillRect(self.rect(), bg)
            p.setPen(QPen(fg, 1))
            p.drawRect(QRect(0, 0, self.width() - 1, self.height() - 1))
            p.end()
        super().paintEvent(ev)


def make_settings_editor(dialog) -> BarEditor:
    """The settings dialog's custom builder for ``bar_layout``: the same
    editor, inline. Its value rides the dialog's accept diff."""
    s = getattr(dialog, "_settings", None)
    win = None
    try:
        win = dialog._find_main_window()
    except Exception:
        win = None
    variant = getattr(getattr(win, "now_label", None), "VARIANT", "stacked")
    editor = BarEditor(getattr(s, "bar_layout", None), variant)
    editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    if win is not None:
        editor.styles_requested.connect(
            getattr(dialog, "_on_open_strip_builder", lambda: None))
    return editor
