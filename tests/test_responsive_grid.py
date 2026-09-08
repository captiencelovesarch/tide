"""Grids read the width they are given.

``ResponsiveGrid`` fits as many columns as the narrowest tile allows and
grows the tiles to fill the row; ``FlowLayout`` wraps chips. The home
patterns, the library grid and the cards ride them.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_responsive_grid.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from tide import theming
from tide.sources.base import ShelfItem
from tide.ui import scale
from tide.ui.card import Card, CardGrid
from tide.ui.flow import FlowLayout, ResponsiveGrid
from tide.ui.home import patterns


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Tile(QWidget):
    """Square-ish tile: height = width + 20 (a card's text block)."""

    def __init__(self) -> None:
        super().__init__()
        self.widths: list[int] = []

    def set_tile_width(self, w: int) -> None:
        self.widths.append(w)
        self.setFixedSize(w, w + 20)

    def tile_height(self, w: int) -> int:
        return w + 20


def _items(n: int, kind: str = "song") -> list:
    return [ShelfItem(kind=kind, title=f"t{i}", subtitle=f"s{i}") for i in range(n)]


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        cls._saved = scale.current()
        scale.set_factor(scale.Scale.NORMAL)
        theming.manager().refresh()

    @classmethod
    def tearDownClass(cls) -> None:
        scale.set_factor(cls._saved)

    def setUp(self) -> None:
        self._widgets: list[QWidget] = []

    def tearDown(self) -> None:
        for w in self._widgets:
            w.deleteLater()
        QTest.qWait(5)

    def _keep(self, w):
        self._widgets.append(w)
        return w

    @staticmethod
    def _size(w: QWidget, width: int) -> None:
        """Hidden widgets get no resize events: show, size, settle."""
        w.resize(width, 10)
        w.show()
        QTest.qWait(5)


class WrapModeTest(_Base):
    def _grid(self, n: int, **kw) -> ResponsiveGrid:
        kw.setdefault("balance", False)     # raw fit unless a test asks
        g = self._keep(ResponsiveGrid(min_tile=100, max_tile=140, gap=10, **kw))
        for _ in range(n):
            g.add(_Tile())
        return g

    def test_columns_come_from_the_narrowest_tile_then_tiles_grow(self) -> None:
        g = self._grid(20)
        self._size(g, 1000)
        # (1000 + 10) // 110 = 9 columns; (1000 - 80) // 9 = 102 wide
        self.assertEqual(g.columns(), 9)
        self.assertEqual(g.tile_width(), 102)
        first, last_in_row = g.tiles()[0], g.tiles()[8]
        self.assertEqual(first.x(), 0)
        self.assertEqual(last_in_row.x() + last_in_row.width(), 8 * 112 + 102)
        # 20 tiles in 9 columns = 3 rows of (102 + 20) with two 10px gaps
        self.assertEqual(g.height(), 3 * 122 + 2 * 10)
        self.assertEqual(g.tiles()[9].y(), 122 + 10)

    def test_growth_is_capped_and_the_remainder_stays_empty(self) -> None:
        g = self._grid(2)
        self._size(g, 1000)
        self.assertEqual(g.tile_width(), 140)
        self.assertEqual(g.height(), 160)

    def test_narrow_width_never_shrinks_below_the_minimum(self) -> None:
        g = self._grid(3)
        self._size(g, 80)
        self.assertEqual(g.columns(), 1)
        self.assertEqual(g.tile_width(), 100)
        self.assertEqual(g.height(), 3 * 120 + 2 * 10)

    def test_resizing_relays_out_and_clear_empties(self) -> None:
        g = self._grid(6)
        self._size(g, 1000)
        self.assertEqual(g.columns(), 6)
        self._size(g, 330)
        self.assertEqual(g.columns(), 3)        # (330+10)//110
        self.assertEqual(g.tile_width(), 103)  # (330-20)//3
        g.clear()
        self.assertEqual(g.tiles(), [])
        self.assertEqual(g.height(), 0)

    def test_balancing_evens_out_wrapped_rows(self) -> None:
        g = self._grid(14, balance=True)
        self._size(g, 1300)                     # 11 fit; 14 tiles → 2 rows of 7
        self.assertEqual(g.columns(), 7)
        rows = sorted({t.y() for t in g.tiles()})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for t in g.tiles() if t.y() == rows[0]), 7)
        self.assertEqual(g.tile_width(), 140)    # grown into the freed room, capped
        g2 = self._grid(20, balance=True)
        self._size(g2, 1000)                    # 9 fit; 20 → 3 rows → 7 columns
        self.assertEqual(g2.columns(), 7)
        self.assertEqual(g2.tile_width(), 134)   # (1000 - 60) // 7

    def test_max_columns_caps_the_fit(self) -> None:
        g = self._grid(12, max_columns=4)
        self._size(g, 1000)
        self.assertEqual(g.columns(), 4)
        self.assertEqual(g.tile_width(), 140)

    def test_a_span_two_tile_takes_a_two_by_two_block(self) -> None:
        g = self._keep(ResponsiveGrid(min_tile=100, max_tile=100, gap=10, balance=False))
        big = _Tile()
        g.add(big, span=2)
        small = [_Tile() for _ in range(6)]
        for t in small:
            g.add(t)
        g.resize(540, 10)                       # 5 columns of 100
        QTest.qWait(5)
        self.assertEqual(g.columns(), 5)
        self.assertEqual((big.x(), big.y(), big.width()), (0, 0, 210))
        # smalls fill (0,2) (0,3) (0,4) then (1,2) ...
        self.assertEqual([(t.x(), t.y()) for t in small[:3]],
                         [(220, 0), (330, 0), (440, 0)])
        self.assertEqual(small[3].x(), 220)
        self.assertGreater(small[3].y(), 0)
        # the featured tile fits inside its two rows
        self.assertGreaterEqual(small[3].y() + small[3].height(),
                                big.y() + big.height())

    def test_span_collapses_on_a_single_column(self) -> None:
        g = self._keep(ResponsiveGrid(min_tile=100, max_tile=100, gap=10, balance=False))
        g.add(_Tile(), span=2)
        g.add(_Tile())
        self._size(g, 100)
        self.assertEqual(g.columns(), 1)
        self.assertEqual(g.tiles()[0].width(), 100)
        self.assertEqual(g.tiles()[1].y(), 120 + 10)


class ColumnsModeTest(_Base):
    def test_fixed_rows_fill_column_major_and_overflow_to_the_right(self) -> None:
        g = self._keep(ResponsiveGrid(min_tile=250, max_tile=400, gap=14,
                                      row_gap=4, rows=4))
        tiles = [_Tile() for _ in range(10)]
        for t in tiles:
            g.add(t)
        g.set_avail_width(1180)                 # viewport: (1180+14)//264 = 4 columns
        self.assertEqual(g.columns(), 3)        # but only 3 exist: they take the room
        self.assertEqual(g.tile_width(), 384)   # (1180 - 28) // 3
        self.assertEqual((tiles[0].x(), tiles[0].y()), (0, 0))
        self.assertEqual(tiles[4].x(), 384 + 14)
        self.assertEqual(tiles[1].y(), tiles[0].height() + 4)
        self.assertEqual(g.width(), 3 * 384 + 2 * 14)

    def test_more_columns_than_fit_become_scroll_width(self) -> None:
        g = self._keep(ResponsiveGrid(min_tile=250, max_tile=400, gap=14,
                                      row_gap=4, rows=4))
        for _ in range(20):
            g.add(_Tile())
        g.set_avail_width(600)                  # (600+14)//264 = 2 visible columns
        self.assertEqual(g.columns(), 2)
        self.assertEqual(g.tile_width(), 293)
        self.assertEqual(g.width(), 5 * 293 + 4 * 14)   # 5 columns exist
        self.assertGreater(g.width(), 600)


class FlowLayoutTest(_Base):
    def test_chips_wrap_to_the_width(self) -> None:
        host = self._keep(QWidget())
        flow = FlowLayout(host, h_gap=4, v_gap=4)
        for i in range(6):
            b = QPushButton(f"chip {i}")
            b.setFixedSize(100, 24)
            flow.addWidget(b)
        host.resize(350, 100)
        host.show()
        QTest.qWait(10)
        rows = sorted({flow.itemAt(i).widget().y() for i in range(flow.count())})
        self.assertEqual(len(rows), 2)          # 3 per row at 350px
        self.assertEqual(flow.itemAt(3).widget().x(), 0)
        self.assertEqual(flow.heightForWidth(350), 24 + 4 + 24)
        self.assertEqual(flow.heightForWidth(1000), 24)


class CardTileTest(_Base):
    def test_card_grows_to_its_column_and_reports_its_height(self) -> None:
        c = self._keep(Card("t", "s", "", None, thumb_px=84))
        self.assertEqual(c.THUMB, 84)
        c.set_tile_width(120)
        self.assertEqual(c.THUMB, 120 - 2 * c.MARGIN)
        self.assertEqual(c.width(), 120)
        self.assertEqual(c.height(), c.tile_height(120))
        # a theme refresh keeps the column width
        c._on_theme(theming.manager().current())
        self.assertEqual(c.width(), 120)

    def test_cards_paint_in_both_aesthetics_with_and_without_hover(self) -> None:
        for slug in ("nord", "brutalist-mono"):
            with mock.patch.object(QApplication.instance(), "setStyleSheet"):
                theming.manager().apply(slug)
            for circular in (False, True):
                c = self._keep(Card("t", "s", "", None, circular=circular))
                c.show()
                QTest.qWait(5)
                self.assertFalse(c.grab().toImage().isNull())
                with mock.patch.object(Card, "underMouse", return_value=True):
                    self.assertFalse(c.grab().toImage().isNull())

    def test_compact_row_stretches_to_its_column(self) -> None:
        r = self._keep(patterns.CompactTrackRow("t", "s", "", None))
        base_h = r.height()
        r.set_tile_width(333)
        self.assertEqual((r.width(), r.height()), (333, base_h))
        r._on_theme(theming.manager().current())
        self.assertEqual(r.width(), 333)
        with mock.patch.object(patterns.CompactTrackRow, "underMouse", return_value=True):
            self.assertFalse(r.grab().toImage().isNull())


class PatternsTest(_Base):
    def test_dense_grid_fills_the_width_and_caps_at_fourteen(self) -> None:
        g = self._keep(patterns.DenseGrid(_items(20)))
        self.assertEqual(len(g.tiles()), 14)
        self._size(g, 2200)
        self.assertGreaterEqual(g.columns(), 14)     # one long row on a wide window
        self.assertEqual(len({t.y() for t in g.tiles()}), 1)
        self._size(g, 500)
        self.assertGreater(len({t.y() for t in g.tiles()}), 1)
        right_edges = [t.x() + t.width() for t in g.tiles()[: g.columns()]]
        self.assertLessEqual(max(right_edges), 500)

    def test_mosaic_keeps_its_two_row_shape_on_a_wide_window(self) -> None:
        g = self._keep(patterns.Mosaic(_items(9), seed=3))
        self._size(g, 1800)
        big = g.tiles()[0]
        self.assertEqual(big._title, "t3")
        self.assertGreater(big.width(), g.tiles()[1].width() * 1.9)
        self.assertEqual(len(g.tiles()), 9)
        self.assertEqual(g.columns(), 6)                    # feature + 4 per row
        self.assertEqual(len({t.y() for t in g.tiles()[1:]}), 2)

    def test_dense_grid_balances_its_two_rows(self) -> None:
        g = self._keep(patterns.DenseGrid(_items(14)))
        self._size(g, 1300)
        rows = sorted({t.y() for t in g.tiles()})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for t in g.tiles() if t.y() == rows[0]), 7)

    def test_tap_grid_sizes_columns_to_its_viewport(self) -> None:
        t = self._keep(patterns.TapGrid(_items(20)))
        t.resize(1200, 300)
        t.show()
        QTest.qWait(20)
        vw = t.viewport().width()
        self.assertEqual(t.grid.columns(), (vw + scale.px(14)) // (scale.px(252) + scale.px(14)))
        self.assertEqual(len(t.grid.tiles()), 20)
        self.assertGreaterEqual(t.grid.width(), vw)     # 5 columns exist

    def test_ranked_list_has_two_columns_sharing_the_width(self) -> None:
        class _E:
            def __init__(self, i):
                self.item = ShelfItem(kind="song", title=f"t{i}")
                self.rank, self.trend = i + 1, "up"
        g = self._keep(patterns.RankedList([_E(i) for i in range(10)]))
        self._size(g, 1000)
        self.assertEqual(g.columns(), 2)
        self.assertEqual(g.tile_width(), min(scale.px(480), (1000 - scale.px(24)) // 2))
        self.assertEqual(g.tiles()[5].x(), g.tile_width() + scale.px(24))

    def test_chip_row_wraps(self) -> None:
        class _Cat:
            def __init__(self, t):
                self.title, self.params = t, ""
        row = self._keep(patterns.ChipRow([_Cat(f"genre {i}") for i in range(12)]))
        row.resize(420, 100)
        row.show()
        QTest.qWait(10)
        self.assertEqual(len(row.chips), 12)
        self.assertGreater(len({c.y() for c in row.chips}), 1)

    def test_card_grid_wraps_cards(self) -> None:
        g = self._keep(CardGrid())
        for i in range(8):
            g.add_card(Card(f"t{i}", "", "", None))
        self._size(g, 1000)
        # six fit; eight cards balance into two rows of four
        self.assertEqual(g.columns(), 4)
        self.assertEqual(len({t.y() for t in g.tiles()}), 2)
        self.assertGreater(g.height(), 0)
        g.clear()
        self.assertEqual(g.tiles(), [])


if __name__ == "__main__":
    unittest.main()
