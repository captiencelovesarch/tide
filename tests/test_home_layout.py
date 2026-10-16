"""Home 2.3 layout: fixed strata, the charts band, the hero's recently
played strip, and a mosaic that fills the width.

The extras' workers read a disk cache and answer before ``get_home``'s
network round trip, so blocks appended in arrival order put top songs
above quick picks. The strata are fixed slots now: these tests feed the
handlers in the "wrong" order and check where the blocks land.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_home_layout.py
"""
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from tide import theming
from tide.history import HistoryEntry
from tide.sources.base import AlbumEntry, ChartEntry, Shelf, ShelfItem
from tide.ui import scale
from tide.ui.flow import ResponsiveGrid
from tide.ui.home import patterns
from tide.ui.home.view import HomeView, recently_played


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _items(n: int, kind: str = "song") -> list:
    return [ShelfItem(kind=kind, title=f"t{i}", subtitle=f"s{i}") for i in range(n)]


def _ranked(n: int = 10) -> list:
    return [ChartEntry(rank=i + 1, trend="up",
                       item=ShelfItem(kind="song", title=f"c{i}"))
            for i in range(n)]


def _tracks(n: int) -> list:
    return [HistoryEntry(video_id=f"v{i}", title=f"song {i}", artists="a",
                         thumbnail=f"https://art/{i}").to_track()
            for i in range(n)]


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
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()
        self._widgets: list[QWidget] = []

    def tearDown(self) -> None:
        self._sheet.stop()
        for w in self._widgets:
            w.deleteLater()
        QTest.qWait(5)

    def _keep(self, w):
        self._widgets.append(w)
        return w

    @staticmethod
    def _size(w: QWidget, width: int, height: int = 10) -> None:
        w.resize(width, height)
        w.show()
        QTest.qWait(10)


class _Api:
    name = "fake"
    slug = "ytmusic"
    account_name = ""

    def supports(self, _what):
        return True


class StrataTest(_Base):
    """Personalized shelves above the generic extras, whatever answers first."""

    def _view(self) -> HomeView:
        return self._keep(HomeView(_Api()))

    @staticmethod
    def _drain(view: HomeView) -> None:
        for _ in range(40):
            if not view._pending_builds:
                break
            QTest.qWait(2)

    def _explore(self) -> dict:
        albums = [AlbumEntry(browse_id=f"b{i}", title=f"al{i}") for i in range(5)]
        return {"top_songs": _ranked(), "new_releases": albums}

    def test_shelves_arriving_last_still_sit_above_the_extras(self) -> None:
        v = self._view()
        v._on_explore(v._gen, self._explore())
        v._on_charts(v._gen, {"artists": _ranked(), "selected": "united states"})
        self._drain(v)
        v._on_shelves(v._gen, [Shelf("Quick picks", _items(8)),
                               Shelf("Listen again", _items(6, "album"))])
        self._drain(v)
        col = v._content_col
        order = [col.itemAt(i).widget() for i in range(col.count())]
        self.assertLess(order.index(v._shelves_slot), order.index(v._extras_slot))
        shelf_heads = [w.text() for w in v._shelves_slot.findChildren(
            QLabel) if "quick" in w.text().lower()
            or "listen" in w.text().lower()]
        self.assertEqual(len(shelf_heads), 2)
        # The extras slot holds the charts band first, then new releases.
        first = v._extras_col.itemAt(0).widget()
        self.assertIsInstance(first, patterns.ChartsBand)

    def test_extras_keep_their_order_when_releases_answer_first(self) -> None:
        v = self._view()
        v._on_explore(v._gen, {"new_releases": self._explore()["new_releases"]})
        self._drain(v)
        v._on_charts(v._gen, {"artists": _ranked()})
        self._drain(v)
        widgets = [v._extras_col.itemAt(i).widget()
                   for i in range(v._extras_col.count())]
        self.assertIsInstance(widgets[0], patterns.ChartsBand)
        self.assertIsInstance(widgets[-1], patterns.Mosaic)

    def test_empty_slots_are_hidden(self) -> None:
        v = self._view()
        self.assertTrue(v._shelves_slot.isHidden())
        self.assertTrue(v._extras_slot.isHidden())
        v._on_shelves(v._gen, [Shelf("Quick picks", _items(4))])
        self._drain(v)
        self.assertFalse(v._shelves_slot.isHidden())
        self.assertTrue(v._extras_slot.isHidden())

    def test_mood_mode_clears_the_strata(self) -> None:
        v = self._view()
        v._on_charts(v._gen, {"artists": _ranked()})
        self._drain(v)
        self.assertIsNotNone(v._charts_band)
        v._clear_content()
        self.assertIsNone(v._charts_band)
        self.assertEqual(v._extras_col.count(), 0)


class ChartsBandTest(_Base):
    def _band(self, first: str = "top_songs") -> patterns.ChartsBand:
        band = self._keep(patterns.ChartsBand())
        lists = {"top_songs": "top songs", "charts_artists": "charts · us"}
        order = [first] + [s for s in lists if s != first]
        for slot in order:
            band.set_list(slot, lists[slot], patterns.RankedList(_ranked()))
        return band

    def test_side_by_side_when_wide(self) -> None:
        band = self._band()
        self._size(band, 2000)
        self.assertTrue(band.side_by_side())
        left, right = band.halves()
        self.assertLess(left.x(), right.x())
        self.assertEqual(left.y(), right.y())
        self.assertGreater(right.x() + right.width(), 2000 * 0.9)

    def test_stacked_when_narrow(self) -> None:
        band = self._band()
        self._size(band, 900)
        self.assertFalse(band.side_by_side())
        top, bottom = band.halves()
        self.assertLess(top.y(), bottom.y())

    def test_top_songs_lead_whichever_lands_first(self) -> None:
        band = self._band(first="charts_artists")
        self._size(band, 2000)
        self.assertEqual(band.halves()[0].heading.text().lower().count("top songs"), 1)

    def test_one_list_alone_never_splits(self) -> None:
        band = self._keep(patterns.ChartsBand())
        band.set_list("charts_artists", "charts", patterns.RankedList(_ranked()))
        self._size(band, 2400)
        self.assertFalse(band.side_by_side())


class RecentlyPlayedTest(unittest.TestCase):
    def _e(self, vid, thumb):
        return HistoryEntry(video_id=vid, title=vid, artists="a", thumbnail=thumb)

    def test_one_per_cover_and_not_the_keep_listening_one(self) -> None:
        entries = [self._e("now", "c0"), self._e("same-album", "c0"),
                   self._e("a", "c1"), self._e("b", "c1"), self._e("c", "c2"),
                   self._e("noart", ""), self._e("", "c3")]
        self.assertEqual([t.video_id for t in recently_played(entries)],
                         ["a", "c"])

    def test_limit(self) -> None:
        entries = [self._e(f"v{i}", f"c{i}") for i in range(50)]
        self.assertEqual(len(recently_played(entries, limit=10)), 10)


class RecentStripTest(_Base):
    def test_two_rows_as_many_columns_as_fit(self) -> None:
        strip = self._keep(patterns.RecentStrip(_tracks(24)))
        self._size(strip, 600, strip.height())
        cells = strip.cells()
        tile, gap = strip._tile(), scale.px(strip.GAP)
        cols = (600 + gap) // (tile + gap)
        self.assertEqual(len(cells), cols * 2)
        self.assertEqual(len({c.y() for c in cells}), 2)
        # spread to the full width (within the gap cap)
        self.assertGreater(max(c.right() for c in cells), 600 - tile)

    def test_few_tracks_do_not_spread(self) -> None:
        strip = self._keep(patterns.RecentStrip(_tracks(4)))
        self._size(strip, 900, strip.height())
        cells = strip.cells()
        self.assertEqual(len(cells), 4)
        self.assertEqual(cells[1].x() - cells[0].x(),
                         strip._tile() + scale.px(strip.GAP))

    def test_click_plays_that_cover(self) -> None:
        strip = self._keep(patterns.RecentStrip(_tracks(8)))
        self._size(strip, 600, strip.height())
        seen = []
        strip.track_clicked.connect(seen.append)
        QTest.mouseClick(strip, Qt.LeftButton, pos=strip.cells()[3].center())
        self.assertEqual([t.video_id for t in seen], ["v3"])
        QTest.mouseClick(strip, Qt.LeftButton, pos=QPoint(599, 1))
        self.assertEqual(len(seen), 1)


class HeroLayoutTest(_Base):
    def _hero(self, recent=8, week=True) -> patterns.Hero:
        w = {"minutes": [10.0] * 7, "days": list("smtwtfs"), "streak": 3,
             "repeat": [], "new": []} if week else None
        last = HistoryEntry(video_id="now", title="now", artists="a",
                            thumbnail="https://art/now").to_track()
        return self._keep(patterns.Hero(
            "hello", "", last, can_resume=True, show_likes=False,
            week=w, recent=_tracks(recent)))

    def test_wide_puts_the_three_zones_on_one_line(self) -> None:
        h = self._hero()
        self._size(h, 1800, 300)
        self.assertTrue(h.is_wide())
        self.assertEqual(h._recent_box.y(), h._text_box.y())
        self.assertLess(h._recent_box.x(), h.week_panel.x())
        self.assertGreater(h.recent_strip.width(), 400)     # the middle is used

    def test_narrow_drops_the_week_under(self) -> None:
        h = self._hero()
        self._size(h, 700, 400)
        self.assertFalse(h.is_wide())
        self.assertGreater(h.week_panel.y(), h._text_box.y())
        self._size(h, 1800, 300)
        self.assertTrue(h.is_wide())

    def test_minimum_width_does_not_hold_the_page_open(self) -> None:
        h = self._hero()
        self.assertLessEqual(h.minimumSizeHint().width(), scale.px(320))

    def test_strip_click_plays(self) -> None:
        h = self._hero()
        self._size(h, 1800, 300)
        seen = []
        h.track_clicked.connect(seen.append)
        h.recent_strip.track_clicked.emit(h.recent_strip.tracks()[0])
        self.assertEqual([t.video_id for t in seen], ["v0"])

    def test_no_history_no_strip(self) -> None:
        h = self._hero(recent=0)
        self.assertIsNone(h.recent_strip)

    def test_both_personalities_reflow(self) -> None:
        for slug in ("brutalist-mono", "blackwater"):
            theming.manager().apply(slug)
            QTest.qWait(5)
            h = self._hero()
            self._size(h, 1800, 300)
            self.assertTrue(h.is_wide(), slug)
            self._size(h, 600, 400)
            self.assertFalse(h.is_wide(), slug)


class MosaicFillTest(_Base):
    def test_wide_window_shows_more_small_tiles_in_two_rows(self) -> None:
        g = self._keep(patterns.Mosaic(_items(17, "album"), seed=0))
        self._size(g, 2000)
        shown = g.shown_tiles()
        self.assertGreater(len(shown), 9)
        self.assertEqual(len({t.y() for t in shown[1:]}), 2)
        right = max(t.x() + t.width() for t in shown)
        self.assertGreater(right, 2000 * 0.85)      # the tile cap, not a ragged half

    def test_narrow_window_hides_the_overflow_instead_of_wrapping(self) -> None:
        g = self._keep(patterns.Mosaic(_items(17, "album"), seed=0))
        self._size(g, 700)
        shown = g.shown_tiles()
        self.assertLess(len(shown), 17)
        self.assertEqual(len({t.y() for t in shown[1:]}), 2)
        self._size(g, 2000)
        self.assertGreater(len(g.shown_tiles()), len(shown))


class MaxRowsTest(_Base):
    def test_wrap_mode_stops_at_max_rows(self) -> None:
        class _T(QWidget):
            def set_tile_width(self, w):
                self.setFixedSize(w, w)

            def tile_height(self, w):
                return w

        g = self._keep(ResponsiveGrid(min_tile=100, max_tile=100, gap=0,
                                      balance=False, max_rows=2))
        for _ in range(10):
            g.add(_T())
        self._size(g, 300)
        self.assertEqual(len(g.shown_tiles()), 6)
        self.assertEqual(g.height(), 200)
        self._size(g, 500)
        self.assertEqual(len(g.shown_tiles()), 10)


if __name__ == "__main__":
    unittest.main()
