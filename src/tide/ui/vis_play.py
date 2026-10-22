"""the visualizer's tile grid, and the lyrics tile.

up to four tiles share the visualizer tab (``LAYOUTS``), each picked per
slot from ``tile_kinds()``: the live scenes and the beat deck
(vis_deck.py), the lyrics, and whatever plugins add (``register_tile``,
through ``ctx.ui.add_visualizer_tile``). the loudness river, spectrogram,
halo and beat tapper left for the store in 3.0 (the vis-tiles plugin).

a slot naming a tile whose plugin is off keeps its pick and shows a
placeholder saying where the tile comes from, so turning the plugin back
on puts your grid back.

a tile's knobs show in its own menu: the [edit] button that shows while
you hover a tile, or a right-click. core tiles read tide's settings,
plugin tiles their plugin's. all of them run off the engine's one clock
and stop asking for frames once they've settled.
"""
from __future__ import annotations

from collections import deque
import math
import time

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPen, QPolygonF, QRegion
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import theming
from .vis_deck import (
    CanvasTile, DeckTile, Frame, SceneTile, TileMixin, VisEngine, _alpha, _mix,
    bloom, paint_outline,
)


TILE_KINDS: tuple[tuple[str, str], ...] = (
    ("spectrum", "spectrum"),
    ("mirror", "mirror"),
    ("scope", "scope"),
    ("deck", "beat deck"),
    ("lyrics", "lyrics"),
    ("classic", "classic renderer"),
)
KIND_LABELS = dict(TILE_KINDS)

# tiles that left core for a store plugin: kind -> (label, plugin name)
MOVED_TILES: dict[str, tuple[str, str]] = {
    "river": ("loudness river", "more visualizer tiles"),
    "spectrogram": ("spectrogram", "more visualizer tiles"),
    "halo": ("art halo", "more visualizer tiles"),
    "tapper": ("beat tapper", "more visualizer tiles"),
}


class _Registry(QObject):
    changed = Signal()


class PluginTile:
    """what a plugin registered for one tile kind."""

    def __init__(self, kind: str, label: str, factory, knobs, settings, set_knob) -> None:
        self.kind, self.label, self.factory = kind, label, factory
        self.knobs = tuple(knobs)          # setting keys (choice kind) for its menu
        self.settings = settings           # the plugin's PluginSettings
        self.set_knob = set_knob           # (key, value): store it and tell the plugin


_PLUGIN_TILES: dict[str, PluginTile] = {}
registry_signal = _Registry()


def register_tile(entry: PluginTile) -> None:
    _PLUGIN_TILES[entry.kind] = entry
    registry_signal.changed.emit()


def unregister_tile(kind: str) -> None:
    if _PLUGIN_TILES.pop(kind, None) is not None:
        registry_signal.changed.emit()


def plugin_tile(kind: str) -> PluginTile | None:
    return _PLUGIN_TILES.get(kind)


def tile_kinds() -> tuple[tuple[str, str], ...]:
    """what a slot can show right now: core's tiles, then plugins'."""
    extra = tuple((k, e.label) for k, e in _PLUGIN_TILES.items() if k not in KIND_LABELS)
    return TILE_KINDS + extra


def tile_choices() -> tuple[tuple[str, str], ...]:
    """tile_kinds plus the moved tiles whose plugin isn't on, so a saved
    pick of one survives the settings dialog."""
    have = dict(tile_kinds())
    rest = tuple((k, f"{label} ({plugin})") for k, (label, plugin) in MOVED_TILES.items()
                 if k not in have)
    return tuple(have.items()) + rest


def known_kind(kind: str) -> bool:
    return kind in KIND_LABELS or kind in _PLUGIN_TILES or kind in MOVED_TILES

# (row, col, rowspan, colspan) per slot, and the grid's (row, col) stretch
LAYOUTS: dict[str, tuple[tuple[int, int, int, int], ...]] = {
    "single": ((0, 0, 1, 1),),
    "split": ((0, 0, 1, 1), (0, 1, 1, 1)),
    "stack": ((0, 0, 1, 1), (1, 0, 1, 1)),
    "trio": ((0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)),
    "quad": ((0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)),
}
STRETCH: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "single": ((1,), (1,)),
    "split": ((1,), (1, 1)),
    "stack": ((2, 1), (1,)),
    "trio": ((1, 1), (3, 2)),
    "quad": ((1, 1), (1, 1)),
}
LAYOUT_LABELS: tuple[tuple[str, str], ...] = (
    ("single", "one tile"),
    ("split", "two, side by side"),
    ("stack", "two, stacked"),
    ("trio", "three: one big, two small"),
    ("quad", "four: a 2×2 grid"),
)
SLOT_NAMES: dict[str, tuple[str, ...]] = {
    "single": ("the tile",),
    "split": ("left", "right"),
    "stack": ("top", "bottom"),
    "trio": ("big", "top right", "bottom right"),
    "quad": ("top left", "top right", "bottom left", "bottom right"),
}
SLOT_KEYS = ("visualizer_tile_1", "visualizer_tile_2", "visualizer_tile_3",
             "visualizer_tile_4")

_SCENE_KINDS = ("spectrum", "mirror", "scope", "classic")


class PlayTile(CanvasTile):
    """a tile you click into: double-click must not leave for fullscreen."""

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def knob(self, name: str, default):
        # a plugin's tile reads its plugin's settings (set by make_tile)
        ps = getattr(self, "plugin_settings", None)
        if ps is not None:
            try:
                return ps[name]
            except KeyError:
                return default
        return self.engine.setting(name, default)

    HINT_H = 18.0

    def inner(self, w: float, h: float) -> QRectF:
        """the play area: the tile less its padding and the hint line."""
        return QRectF(self.PAD, self.PAD, max(1.0, w - 2 * self.PAD),
                      max(1.0, h - 2 * self.PAD - self.HINT_H))

    def hint(self, p: QPainter, w: float, h: float, f: Frame, text: str) -> None:
        font = self.font_at(0.85)
        p.setFont(font)
        p.setPen(_alpha(f.pal.dim, 200))
        fm = QFontMetricsF(font)
        p.drawText(QPointF(w - self.PAD - fm.horizontalAdvance(text), h - self.PAD + 2 - fm.descent()), text)


# ---------- lyrics ----------

class LyricsTile(TileMixin, QWidget):
    """the synced lyrics, in a tile. same embedding as the fullscreen
    player: a LyricsView with its in-app chrome hidden."""

    kind = "lyrics"
    _QSS = """
    QWidget#visLyricsTile, QWidget#visLyricsTile .QWidget,
    QWidget#visLyricsTile LyricsView, QWidget#visLyricsTile QScrollArea,
    QWidget#visLyricsTile QScrollArea > QWidget,
    QWidget#visLyricsTile QScrollArea > QWidget > QWidget,
    QWidget#visLyricsTile #lyricsKaraoke { background: transparent; }
    """

    def __init__(self, engine: VisEngine, api_provider=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._api_provider = api_provider
        api = None
        try:
            api = api_provider() if api_provider is not None else None
        except Exception:
            api = None
        self.setObjectName("visLyricsTile")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setStyleSheet(self._QSS)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(120, 90)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self.view = None
        if api is not None:
            from .lyrics import LyricsView
            self.view = LyricsView(api, font_scale=1.15)
            for chrome in ("heading", "karaoke_check", "mute_btn", "swap_status"):
                w = getattr(self.view, chrome, None)
                if w is not None:
                    w.hide()
            lay.addWidget(self.view)
        else:
            label = QLabel(theming.styled_case("lyrics show up here"))
            label.setAlignment(Qt.AlignCenter)
            label.setProperty("class", "dim")
            lay.addWidget(label)
        self._track = None
        self._shown_for = object()
        self._last_pos = -math.inf
        self._rippling = False
        engine.register(self)

    def _sync(self) -> None:
        if self.view is None or not self.isVisible():
            return
        try:
            api = self._api_provider() if self._api_provider is not None else None
        except Exception:
            api = None
        if api is not None and getattr(self.view, "api", None) is not api:
            self.view.api = api              # the active source changed
            self._shown_for = object()
        if self._shown_for is not self.engine.track:
            self._shown_for = self.engine.track
            self.view.show_for(self.engine.track)
            self._last_pos = -math.inf
        pos = self.engine.player_pos
        if pos is not None:
            self.view.update_position(pos)
            self._last_pos = pos

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync()

    def track_changed(self, track) -> None:
        self._sync()

    def position_changed(self, seconds: float) -> None:
        if self.view is None or not self.isVisible():
            return
        if self._shown_for is not self.engine.track:
            self._sync()
            return
        # karaoke interpolates the active word per tick; lines need ~4 Hz
        if getattr(self.view, "_karaoke_mode", False) or abs(seconds - self._last_pos) >= 0.25:
            self._last_pos = seconds
            self.view.update_position(seconds)

    def on_frame(self, now, dt) -> None:
        ripples = bool(self.engine.frame.ripples)
        if ripples or self._rippling:
            # repaint only the frame's edges: the lyrics underneath stay put
            r = self.rect()
            band = 10
            region = QRegion(0, 0, r.width(), band)
            region += QRegion(0, r.height() - band, r.width(), band)
            region += QRegion(0, 0, band, r.height())
            region += QRegion(r.width() - band, 0, band, r.height())
            self.update(region)
        self._rippling = ripples

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        paint_outline(p, self.outline_rect(), self.ripple_origin(), self.engine.frame)
        p.end()


# ---------- the grid ----------

class _EditPlate(QWidget):
    """the [edit] button's backing, so a busy tile can't show through it."""

    def __init__(self, engine: VisEngine, parent: QWidget) -> None:
        super().__init__(parent)
        self._engine = engine
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    def paintEvent(self, _event) -> None:
        pal = self._engine.pal
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(pal.bg, 235))
        if self._engine.look == "glow":
            p.setRenderHint(QPainter.Antialiasing, True)
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        else:
            p.drawRect(self.rect())
        p.end()


class MissingTile(CanvasTile):
    """a slot whose tile comes with a plugin that isn't on. the pick
    stays; installing (or turning on) the plugin brings the tile back.
    a canvas tile like the rest, so it lives and goes the same way."""

    def __init__(self, engine: VisEngine, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(engine, parent)
        self.kind = kind
        label, plugin = MOVED_TILES.get(kind, (kind, "a plugin"))
        self.message = theming.styled_case(
            f"the {label} comes with the {plugin} plugin. it's in the store.")

    def paint_overlay(self, p: QPainter, w: float, h: float, f: Frame) -> None:
        p.setFont(self.font_at(1.0))
        p.setPen(_alpha(f.pal.dim, 220))
        pad = self.PAD * 2
        p.drawText(QRectF(pad, pad, max(1.0, w - 2 * pad), max(1.0, h - 2 * pad)),
                   int(Qt.AlignCenter | Qt.TextWordWrap), self.message)


def _stale(tile: QWidget) -> bool:
    """a placeholder whose plugin has come, or a plugin's tile whose
    plugin has gone: either way the slot needs a new tile."""
    if isinstance(tile, MissingTile):
        return tile.kind in _PLUGIN_TILES
    return hasattr(tile, "plugin_settings") and tile.kind not in _PLUGIN_TILES


def make_tile(kind: str, engine: VisEngine, api_provider=None) -> QWidget:
    entry = _PLUGIN_TILES.get(kind)
    if entry is not None:
        try:
            tile = entry.factory(engine)
        except Exception:  # noqa: BLE001 - a broken plugin tile is a placeholder
            return MissingTile(engine, kind)
        tile.kind = kind
        tile.plugin_settings = entry.settings
        return tile
    if kind in MOVED_TILES:
        return MissingTile(engine, kind)
    if kind in _SCENE_KINDS:
        return SceneTile(engine, kind)
    if kind == "deck":
        return DeckTile(engine)
    if kind == "lyrics":
        return LyricsTile(engine, api_provider)
    return SceneTile(engine, "spectrum")


class TileGrid(QWidget):
    """lays the tiles out. a tile keeps its state across a re-layout as
    long as its slot keeps its kind (the tapper's streak, the spectrogram's history)."""

    menu_requested = Signal(int, object)    # slot, global QPoint

    def __init__(self, engine: VisEngine, api_provider=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._api_provider = api_provider
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self.tiles: list[QWidget] = []
        self.layout_name = ""

    def configure(self, layout: str, kinds: list[str]) -> None:
        if layout not in LAYOUTS:
            layout = "stack"
        slots = LAYOUTS[layout]
        kinds = [k if known_kind(k) else "spectrum" for k in kinds[:len(slots)]]
        while len(kinds) < len(slots):
            kinds.append("spectrum")
        if layout == self.layout_name and [t.kind for t in self.tiles] == kinds \
                and not any(_stale(t) for t in self.tiles):
            return
        for tile in self.tiles:
            self._grid.removeWidget(tile)
        keep: list[QWidget] = []
        for slot, kind in enumerate(kinds):
            old = self.tiles[slot] if slot < len(self.tiles) else None
            # a placeholder goes once its tile's plugin is here (and a
            # plugin tile once its plugin has gone)
            if old is not None and old.kind == kind and not _stale(old):
                keep.append(old)
                continue
            if old is not None:
                self._drop(old)
            tile = make_tile(kind, self.engine, self._api_provider)
            tile.setParent(self)
            tile.customContextMenuRequested.connect(
                lambda pos, t=tile: self._on_menu(t, pos))
            self._attach_edit(tile)
            keep.append(tile)
        for old in self.tiles[len(kinds):]:
            self._drop(old)
        self.tiles = keep
        for i in range(4):
            self._grid.setRowStretch(i, 0)
            self._grid.setColumnStretch(i, 0)
        rows, cols = STRETCH[layout]
        for i, s in enumerate(rows):
            self._grid.setRowStretch(i, s)
        for i, s in enumerate(cols):
            self._grid.setColumnStretch(i, s)
        caption_given = False
        for tile, (r, c, rs, cs) in zip(self.tiles, slots):
            self._grid.addWidget(tile, r, c, rs, cs)
            tile.solo = len(slots) == 1
            tile.caption_here = (not caption_given and tile.kind in ("spectrum", "mirror", "scope"))
            caption_given = caption_given or tile.caption_here
            tile.show()
        self.layout_name = layout
        self.engine.wake()

    # ---------- the edit button ----------
    # right-click isn't discoverable (and a tile may want the button), so each
    # tile also gets an [edit] button that shows while the mouse is over
    # it and opens the same menu.

    def _attach_edit(self, tile: QWidget) -> None:
        from .widgets import BracketButton
        plate = _EditPlate(self.engine, tile)
        btn = BracketButton("edit", parent=plate)
        btn.setToolTip(theming.styled_case("change this tile and its knobs"))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False, t=tile, b=btn: self._edit_clicked(t, b))
        lay = QVBoxLayout(plate)
        lay.setContentsMargins(3, 2, 3, 2)
        lay.addWidget(btn)
        plate.hide()
        tile._edit_btn = btn
        tile._edit_plate = plate
        tile.installEventFilter(self)

    def _place_edit(self, tile: QWidget) -> None:
        plate = getattr(tile, "_edit_plate", None)
        if plate is None:
            return
        plate.adjustSize()
        plate.move(max(0, tile.width() - plate.width() - 8), 8)

    def _edit_clicked(self, tile: QWidget, btn: QWidget) -> None:
        if tile in self.tiles:
            self.menu_requested.emit(self.tiles.index(tile),
                                     btn.mapToGlobal(btn.rect().bottomLeft()))

    def eventFilter(self, obj, event) -> bool:
        plate = getattr(obj, "_edit_plate", None)
        if plate is not None:
            kind = event.type()
            if kind == QEvent.Enter:
                self._place_edit(obj)
                plate.show()
                plate.raise_()
            elif kind == QEvent.Leave:
                plate.hide()
            elif kind == QEvent.Resize and plate.isVisible():
                self._place_edit(obj)
        return super().eventFilter(obj, event)

    def _drop(self, tile: QWidget) -> None:
        tile.removeEventFilter(self)
        try:
            tile.release()
        except Exception:
            pass
        tile.hide()
        tile.setParent(None)
        tile.deleteLater()

    def _on_menu(self, tile: QWidget, pos) -> None:
        if tile in self.tiles:
            self.menu_requested.emit(self.tiles.index(tile), tile.mapToGlobal(pos))

    def teardown(self) -> None:
        for tile in self.tiles:
            self._drop(tile)
        self.tiles = []
        self.layout_name = ""
