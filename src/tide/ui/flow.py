"""Width-aware layout: tiles that fill their row, chips that wrap.

Qt's grid layouts place widgets in cells you name up front, so a shelf
built as "seven columns of 84px" stays seven columns of 84px on a 2000px
window and leaves the right half of it empty. These two primitives read
the width they are given instead.

``ResponsiveGrid`` lays out uniform tiles. It works out how many columns
the narrowest allowed tile permits, then grows the tiles (up to a cap) so
a whole number of them spans the row exactly: full bleed, no ragged right
edge until the cap is hit. Tiles are plain widgets that answer two calls,
``set_tile_width(px)`` and ``tile_height(px)``, so cards, quick-pick rows
and anything else can ride it. A tile added with ``span=2`` takes a 2×2
block, which is how the mosaic's featured item is placed.

Two modes. *wrap* (the default) adds rows as tiles arrive. *columns*
(``rows=N``) fills N fixed rows column by column, sizes the columns so a
whole number fits the width, and lets further columns run past the right
edge for a horizontal scroller to page through — the quick-picks shape.
In that mode the scroller tells the grid its viewport width through
``set_avail_width``; without it the grid uses its own width.

``FlowLayout`` is the classic Qt flow layout, for variable-width items
(the mood chips): items keep their size hints and wrap to the next line.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget


class FlowLayout(QLayout):
    """Left-to-right, wrapping. Items keep their sizeHint."""

    def __init__(self, parent: QWidget | None = None, *, h_gap: int = 4,
                 v_gap: int = 4) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_gap = int(h_gap)
        self._v_gap = int(v_gap)
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout protocol --
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, test=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- the arrangement --
    def _arrange(self, rect: QRect, *, test: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if line_h and x + hint.width() > area.right() + 1:
                x = area.x()
                y += line_h + self._v_gap
                line_h = 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h_gap
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class ResponsiveGrid(QWidget):
    """Uniform tiles that fill the width. See the module docstring."""

    def __init__(self, *, min_tile: int, max_tile: int, gap: int = 8,
                 row_gap: int | None = None, rows: int | None = None,
                 balance: bool = True, max_columns: int | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # wrap mode: even out the rows once tiles wrap (eleven tiles and
        # then three orphans reads as a mistake; seven and seven reads as
        # a grid), and cap the columns where a pattern has a shape to keep.
        self._balance = bool(balance)
        self._max_columns = int(max_columns) if max_columns else None
        self._min = max(1, int(min_tile))
        self._max = max(self._min, int(max_tile))
        self._gap = max(0, int(gap))
        self._row_gap = self._gap if row_gap is None else max(0, int(row_gap))
        self._rows = int(rows) if rows else None
        self._tiles: list[tuple[QWidget, int]] = []
        self._avail: int | None = None
        self._cols = 1
        self._tile_w = self._min
        self._last_key: tuple | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(self._min)
        self.setFixedHeight(0)

    # -- content --
    def add(self, tile: QWidget, *, span: int = 1) -> None:
        tile.setParent(self)
        tile.show()
        self._tiles.append((tile, 2 if span >= 2 else 1))
        self._relayout(force=True)

    def clear(self) -> None:
        for tile, _ in self._tiles:
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()
        self._relayout(force=True)

    def tiles(self) -> list[QWidget]:
        return [t for t, _ in self._tiles]

    def columns(self) -> int:
        """Columns the last layout used (visible columns in columns mode)."""
        return self._cols

    def tile_width(self) -> int:
        return self._tile_w

    def set_avail_width(self, width: int) -> None:
        """columns mode: the viewport width to fit whole columns into."""
        self._avail = max(0, int(width))
        self._relayout(force=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._min, self.minimumHeight())

    # -- layout --
    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._relayout()

    def showEvent(self, ev) -> None:  # noqa: N802
        # A hidden widget gets no resize events; catch up on first show.
        super().showEvent(ev)
        self._relayout()

    def _fit(self, avail: int, total_cols: int | None = None) -> tuple[int, int]:
        """(columns, tile width) for ``avail``. Columns come from the
        narrowest tile; the tiles then grow to fill, capped at max. With
        fewer columns to show than fit, the ones there are take the room."""
        if avail <= 0:
            return 1, self._min
        cols = max(1, (avail + self._gap) // (self._min + self._gap))
        if total_cols:
            cols = max(1, min(cols, total_cols))
        tile_w = (avail - (cols - 1) * self._gap) // cols
        return cols, max(self._min, min(self._max, tile_w))

    def _relayout(self, force: bool = False) -> None:
        avail = self._avail if self._avail is not None else self.width()
        key = (avail, len(self._tiles))
        if not force and key == self._last_key:
            return
        self._last_key = key
        if self._rows is None:
            self._layout_wrap(avail)
        else:
            self._layout_columns(avail)

    def _wrap_columns(self, avail: int) -> tuple[int, int]:
        """Columns and tile width for wrap mode: what fits, then fewer
        when the tiles run out, when a pattern caps them, or to balance
        the rows — and the tiles grow into whatever room that frees."""
        cells = sum(span for _, span in self._tiles)
        fit, _ = self._fit(avail)
        cols = fit
        if cells:
            cols = min(cols, cells)
        if self._max_columns:
            cols = min(cols, self._max_columns)
        if self._balance and cells > cols:
            rows = math.ceil(cells / cols)
            cols = max(1, math.ceil(cells / rows))
        return self._fit(avail, cols)

    def _layout_wrap(self, avail: int) -> None:
        cols, tw = self._wrap_columns(avail)
        self._cols, self._tile_w = cols, tw
        gap, rgap = self._gap, self._row_gap
        occupied: set[tuple[int, int]] = set()
        row_h: dict[int, int] = {}
        placed: list[tuple[QWidget, int, int, int, int, int]] = []
        r = c = 0
        for tile, span in self._tiles:
            span = min(span, cols)
            while True:
                if c + span > cols:
                    r, c = r + 1, 0
                    continue
                if any((r + dr, c + dc) in occupied
                       for dr in range(span) for dc in range(span)):
                    c += 1
                    continue
                break
            w = tw * span + gap * (span - 1)
            tile.set_tile_width(w)
            h = int(tile.tile_height(w))
            for dr in range(span):
                for dc in range(span):
                    occupied.add((r + dr, c + dc))
            placed.append((tile, r, c, span, w, h))
            if span == 1:
                row_h[r] = max(row_h.get(r, 0), h)
            c += span
        # A spanning tile must fit inside the rows it covers; when its own
        # rows are shorter than it (a featured card taller than two small
        # ones, or no small tile shares its second row), grow the last one.
        for tile, r, c, span, w, h in placed:
            if span > 1:
                have = sum(row_h.get(r + i, 0) for i in range(span)) + rgap * (span - 1)
                if have < h:
                    last = r + span - 1
                    row_h[last] = row_h.get(last, 0) + (h - have)
        ys: dict[int, int] = {}
        y = 0
        rows = (max(row_h) + 1) if row_h else 0
        for i in range(rows):
            ys[i] = y
            y += row_h.get(i, 0) + rgap
        total = max(0, y - rgap) if rows else 0
        for tile, r, c, span, w, h in placed:
            tile.setGeometry(c * (tw + gap), ys.get(r, 0), w, h)
        self.setFixedHeight(total)

    def _layout_columns(self, avail: int) -> None:
        rows = self._rows or 1
        n = len(self._tiles)
        total_cols = math.ceil(n / rows) if n else 0
        cols, tw = self._fit(avail, total_cols or None)
        self._cols, self._tile_w = cols, tw
        gap, rgap = self._gap, self._row_gap
        heights = [0] * rows
        for i, (tile, _) in enumerate(self._tiles):
            tile.set_tile_width(tw)
            heights[i % rows] = max(heights[i % rows], int(tile.tile_height(tw)))
        ys, y = [], 0
        for r in range(rows):
            ys.append(y)
            y += heights[r] + rgap
        for i, (tile, _) in enumerate(self._tiles):
            r, c = i % rows, i // rows
            tile.setGeometry(c * (tw + gap), ys[r], tw, heights[r])
        self.setFixedHeight(max(0, y - rgap) if n else 0)
        if self._avail is not None:
            # Content width for the scroller: whole columns, maybe wider
            # than the viewport — that overflow is the paging.
            self.setFixedWidth(max(0, total_cols * tw + max(0, total_cols - 1) * gap))
