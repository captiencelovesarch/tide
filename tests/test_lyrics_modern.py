"""The lyrics list's modern look (3.2): line tones, the one-motion line
advance, edge fades, the quiet scrollbar, the empty states and the tab
heading. Brutalist keeps the 1.x look; the older lyrics tests pin it."""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tide import contrast, theming
from tide.ui import motion as motion_module


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _lc(color, bg) -> float:
    return abs(contrast.apca_lc(contrast.apca_y(QColor(color)),
                                contrast.apca_y(QColor(bg))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()
        cls._mgr = theming.manager()
        cls._mgr.refresh()
        cls._theme0 = cls._mgr._current

    @classmethod
    def tearDownClass(cls) -> None:
        # other lyrics tests expect no theme (the brutalist path)
        cls._mgr._current = cls._theme0

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from tide import lyrics_picks
        self._sheet = mock.patch.object(QApplication.instance(), "setStyleSheet")
        self._sheet.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._picks = mock.patch.object(lyrics_picks, "PATH", Path(self._tmp.name) / "picks.json")
        self._picks.start()
        self._motion0 = motion_module.intensity()
        motion_module.set_intensity(motion_module.Intensity.LITE)
        self._views = []

    def tearDown(self) -> None:
        from tide.ui import lyrics
        motion_module.set_intensity(self._motion0)
        for v in self._views:
            v._stop_line_motion()
            v.hide()
            v.deleteLater()
        QTest.qWait(5)
        lyrics._song_key, lyrics._song_offset_s = None, 0.0
        self._picks.stop()
        self._tmp.cleanup()
        self._sheet.stop()

    def _apply(self, slug: str) -> None:
        self._mgr.apply(slug)
        QTest.qWait(5)

    def _view(self, *, tab: bool = True, shown: bool = True):
        from tide.ui.lyrics import LyricsView
        view = LyricsView(SimpleNamespace(), tab=tab)
        self._views.append(view)
        if shown:
            view.resize(640, 420)
            view.show()
            QTest.qWait(10)
        return view


class ToneTest(_Base):
    def test_sung_leads_every_modern_theme(self) -> None:
        from tide.ui.lyrics import line_tones
        for theme in self._mgr.list_themes():
            self._apply(theme.slug)
            th = self._mgr.current_effective()
            if th.aesthetic != "modern":
                continue
            sung, coming, past = line_tones(th)
            bg = th.token("bg")
            with self.subTest(theme=theme.slug):
                self.assertGreater(_lc(sung, bg), _lc(coming, bg))
                self.assertGreater(_lc(coming, bg), _lc(past, bg))


class AdvanceTest(_Base):
    def test_one_motion_then_steady_looks(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(40)])
        QTest.qWait(10)
        view.update_position(30.5)            # line 10
        self.assertIsNotNone(view._line_motion, "the advance animates")
        QTest.qWait(700)
        self.assertIsNone(view._line_motion)
        sung, coming, past = view._tones
        self.assertIn(f"color: {sung.name()}", view._line_widgets[10].styleSheet())
        self.assertIn("font-weight: 700", view._line_widgets[10].styleSheet())
        self.assertIn(f"color: {past.name()}", view._line_widgets[9].styleSheet())
        self.assertIn(f"color: {coming.name()}", view._line_widgets[11].styleSheet())
        # parked about a third down, like the brutalist glide
        y = view._line_widgets[10].y() + view._timed_host.y()
        vh = view._scroll.viewport().height()
        self.assertAlmostEqual(y / vh, 0.38, delta=0.05)

    def test_motion_off_snaps(self) -> None:
        self._apply("blackwater")
        motion_module.set_intensity(motion_module.Intensity.OFF)
        view = self._view()
        view._show_timed([(0.0, "one"), (5.0, "two"), (10.0, "three")])
        view.update_position(6.0)
        self.assertIsNone(view._line_motion)
        self.assertIn("font-weight: 700", view._line_widgets[1].styleSheet())

    def test_brutalist_keeps_its_sheets(self) -> None:
        self._apply("brutalist-mono")
        view = self._view()
        view._show_timed([(0.0, "one"), (5.0, "two"), (10.0, "three")])
        view.update_position(6.0)
        self.assertIsNone(view._line_motion)
        accent = self._mgr.current_effective().token("accent")
        self.assertIn(f"color: {accent};", view._line_widgets[1].styleSheet())


class FadeAndBarTest(_Base):
    def test_edge_lines_fade_in_modern_only(self) -> None:
        from tide.ui.lyrics import _EdgeFade
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(40)])
        QTest.qWait(10)
        view.update_position(60.5)
        QTest.qWait(700)
        faded = [w for w in view._line_widgets if isinstance(w.graphicsEffect(), _EdgeFade)]
        self.assertTrue(faded, "lines at the edges fade")
        self.assertNotIn(view._line_widgets[20], faded, "the sung line never does")
        self.assertIsNotNone(view._bar_fx)
        self.assertEqual(view._bar_fx.opacity(), 0.0, "the scrollbar waits to be wanted")
        self._apply("brutalist-mono")
        QTest.qWait(10)
        self.assertFalse([w for w in view._line_widgets if w.graphicsEffect() is not None])
        self.assertIsNone(view._bar_fx)

    def test_top_of_the_song_is_not_faded(self) -> None:
        from tide.ui.lyrics import _EdgeFade
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(40)])
        QTest.qWait(20)
        self.assertNotIsInstance(view._line_widgets[0].graphicsEffect(), _EdgeFade)


class StatusTest(_Base):
    def test_modern_empty_states(self) -> None:
        self._apply("blackwater")
        view = self._view()
        self.assertIs(view._scroll.widget(), view._empty_host)
        self.assertEqual(view._empty_msg.text(), theming.styled_case("nothing playing"))
        self.assertTrue(view._empty_btn.isHidden())
        view._current_track = SimpleNamespace(video_id="v")
        view._show_status("missing")
        self.assertFalse(view._empty_btn.isHidden(), "a retry where it could help")
        view._current_video_id = "v"
        view._show_status("loading")
        self.assertEqual(view._empty_msg.text(), "", "no flash for a cached sheet")
        QTest.qWait(400)
        self.assertEqual(view._empty_msg.text(), theming.styled_case("loading lyrics"))

    def test_brutalist_keeps_the_old_lines(self) -> None:
        self._apply("brutalist-mono")
        view = self._view()
        self.assertIs(view._scroll.widget(), view._plain_label)
        self.assertEqual(view._plain_label.text(), "── no track ──")
        view._show_status("missing")
        self.assertEqual(view._plain_label.text(), "── no lyrics for this track ──")

    def test_retry_fetches_again(self) -> None:
        self._apply("blackwater")
        view = self._view(shown=False)
        track = SimpleNamespace(video_id="v")
        view._current_track = track
        view._current_video_id = "v"
        with mock.patch.object(view, "show_for") as show_for:
            view._retry()
        show_for.assert_called_once_with(track)
        self.assertIsNone(view._current_video_id)


class SeekTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        from tide.ui import lyrics
        self.lyrics = lyrics
        self.seeks = []
        lyrics.set_seeker(self.seeks.append)

    def tearDown(self) -> None:
        self.lyrics.set_seeker(None)
        self.lyrics.set_offset_ms(0)
        super().tearDown()

    def test_click_plays_from_the_line(self) -> None:
        from PySide6.QtCore import QPoint, Qt
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(10)])
        QTest.qWait(10)
        QTest.mouseClick(view._line_widgets[4], Qt.LeftButton, pos=QPoint(20, 8))
        self.assertEqual(len(self.seeks), 1)
        self.assertAlmostEqual(self.seeks[0], 12.02, places=3)

    def test_offset_maps_back_to_song_time(self) -> None:
        from PySide6.QtCore import QPoint, Qt
        self._apply("blackwater")
        self.lyrics.set_offset_ms(500)       # lyrics shown half a second later
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(10)])
        QTest.qWait(10)
        QTest.mouseClick(view._line_widgets[2], Qt.LeftButton, pos=QPoint(20, 8))
        self.assertAlmostEqual(self.seeks[0], 6.52, places=3)

    def test_drag_selects_instead(self) -> None:
        from PySide6.QtCore import QPoint, Qt
        self._apply("brutalist-mono")
        view = self._view()
        view._show_timed([(0.0, "a line long enough to drag across"), (5.0, "next")])
        QTest.qWait(10)
        lbl = view._line_widgets[0]
        QTest.mousePress(lbl, Qt.LeftButton, pos=QPoint(4, 8))
        QTest.mouseMove(lbl, QPoint(120, 8))
        QTest.mouseRelease(lbl, Qt.LeftButton, pos=QPoint(120, 8))
        self.assertEqual(self.seeks, [])


class FollowTest(_Base):
    def _wheel(self, view) -> None:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        ev = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, 120),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(view._scroll.viewport(), ev)

    def test_your_scroll_holds_the_list(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(40)])
        QTest.qWait(10)
        view.update_position(60.5)
        QTest.qWait(700)
        self._wheel(view)
        self.assertFalse(view._following)
        self.assertFalse(view._back_btn.isHidden())
        held = view._scroll.verticalScrollBar().value()
        view.update_position(63.5)
        QTest.qWait(700)
        self.assertEqual(view._scroll.verticalScrollBar().value(), held)
        view._back_btn.click()
        self.assertTrue(view._following)
        self.assertTrue(view._back_btn.isHidden())
        QTest.qWait(700)
        self.assertNotEqual(view._scroll.verticalScrollBar().value(), held)

    def test_a_new_song_follows_again(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(i * 3.0, f"line {i}") for i in range(40)])
        QTest.qWait(10)
        self._wheel(view)
        self.assertFalse(view._following)
        with mock.patch("tide.ui.lyrics.QThread"), mock.patch("tide.ui.lyrics._LyricsWorker"), \
                mock.patch("tide.ui.lyrics.qthreads"):
            view.show_for(SimpleNamespace(video_id="new", title="t"))
        self.assertTrue(view._following)


class GapTest(_Base):
    def test_intro_gets_dots_that_go_with_the_first_line(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(10.0, "first"), (13.0, "second")])
        QTest.qWait(10)
        view.update_position(1.0)
        self.assertEqual(view._lit_index, -1)
        self.assertEqual(view._marker_at, 0)
        self.assertAlmostEqual(view._marker.progress(), 0.1, delta=0.05)
        view.update_position(10.5)
        QTest.qWait(700)
        self.assertEqual(view._lit_index, 0)
        self.assertEqual(view._marker_at, -1)

    def test_short_intro_has_none(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(2.0, "first"), (5.0, "second")])
        view.update_position(0.5)
        self.assertEqual(view._marker_at, -1)

    def test_blank_line_steps_aside(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(0.0, "a"), (5.0, ""), (15.0, "b")])
        QTest.qWait(10)
        view.update_position(6.0)
        self.assertEqual(view._lit_index, -1)
        self.assertEqual(view._marker_at, 2)
        self.assertTrue(view._line_widgets[1].isHidden())
        view.update_position(15.5)
        QTest.qWait(700)
        self.assertFalse(view._line_widgets[1].isHidden())

    def test_line_fades_back_once_sung(self) -> None:
        from tide import lyric_words
        self._apply("blackwater")
        view = self._view()
        timed = [(0.0, "one"), (3.0, "two"), (12.0, "three")]
        ends = [2.5, 5.0, 14.0]
        view._result = SimpleNamespace(line_ends=ends)
        view._show_sheet(lyric_words.line_sheet(timed, ends))
        QTest.qWait(10)
        view.update_position(4.0)
        self.assertEqual(view._lit_index, 1)
        view.update_position(5.6)
        QTest.qWait(700)
        self.assertEqual(view._lit_index, -1)
        self.assertEqual(view._marker_at, 2)
        self.assertIn(f"color: {view._tones[2].name()}", view._line_widgets[1].styleSheet())

    def test_no_known_end_stays_lit(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(0.0, "one"), (3.0, "two"), (12.0, "three")])
        view.update_position(8.0)
        self.assertEqual(view._lit_index, 1)

    def test_brutalist_gets_the_dots_too(self) -> None:
        self._apply("brutalist-mono")
        view = self._view()
        view._show_timed([(10.0, "first"), (13.0, "second")])
        view.update_position(1.0)
        self.assertEqual(view._marker_at, 0)
        self.assertFalse(view._marker._modern)


class NoteLineTest(_Base):
    """A source's ♪ line is drawn as the break's dots, and when its break
    plays the live dots take its place without moving anything."""

    def test_note_lines_draw_dots(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(0.0, "a"), (3.0, "♪"), (5.0, "b")])
        lbl = view._line_widgets[1]
        self.assertTrue(lbl._dots)
        self.assertEqual(lbl.text(), " ")
        self.assertIsNone(view._line_widgets[0]._dots)
        self._apply("brutalist-mono")
        self.assertFalse(lbl._dots, "brutalist squares")

    def test_live_dots_stand_in_for_the_note(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(0.0, "a"), (3.0, "♪"), (15.0, "b")])
        QTest.qWait(20)
        room = view._line_widgets[1].height()
        view.update_position(4.0)
        self.assertEqual(view._marker_at, 2)
        self.assertEqual(view._marker_kind, "note")
        self.assertTrue(view._line_widgets[1].isHidden())
        self.assertEqual(view._marker.height(), room, "exactly the note's room")
        self.assertEqual(view._marker.presence(), 1.0, "no grow: it was already dots")
        view.update_position(15.5)
        QTest.qWait(700)
        self.assertEqual(view._marker_at, -1)
        self.assertFalse(view._line_widgets[1].isHidden())


class PicksTest(_Base):
    def test_round_trip_and_cleaning(self) -> None:
        from tide import lyrics_picks
        lyrics_picks.set_source("ytmusic:a", "lrclib")
        lyrics_picks.set_offset_ms("ytmusic:a", 750)
        lyrics_picks.set_source("ytmusic:b", "nonsense")
        lyrics_picks._data = None              # read it back from disk
        self.assertEqual(lyrics_picks.source("ytmusic:a"), "lrclib")
        self.assertEqual(lyrics_picks.offset_ms("ytmusic:a"), 750)
        self.assertIsNone(lyrics_picks.source("ytmusic:b"))
        lyrics_picks.set_offset_ms("ytmusic:a", 99_999)
        self.assertEqual(lyrics_picks.offset_ms("ytmusic:a"), lyrics_picks.OFFSET_RANGE_MS)
        lyrics_picks.set_source("ytmusic:a", None)
        lyrics_picks.set_offset_ms("ytmusic:a", 0)
        lyrics_picks._data = None
        self.assertNotIn("ytmusic:a", lyrics_picks._load(), "nothing picked, no entry")

    def test_keeps_the_newest(self) -> None:
        from tide import lyrics_picks
        with mock.patch.object(lyrics_picks, "MAX_SONGS", 3):
            for k in "abcd":
                lyrics_picks.set_offset_ms(k, 250)
        self.assertEqual(list(lyrics_picks._load()), ["b", "c", "d"])


class FetchFromTest(unittest.TestCase):
    def test_each_source(self) -> None:
        from tide import lyrics_provider
        from tide.lyrics_provider import LyricsResult, fetch_from
        track = SimpleNamespace(video_id="v", title="t", artists="a", album="",
                                duration_seconds=200)
        yt = LyricsResult(timed_lines=[(0.0, "x")], source="youtube music")
        api = mock.Mock()
        api._yt_timed_lyrics.return_value = yt
        api.get_lyrics_for_track.return_value = "usual"
        self.assertEqual(fetch_from(api, track, None), "usual")
        self.assertIs(fetch_from(api, track, "youtube music"), yt)
        with mock.patch.object(lyrics_provider, "fetch_lrclib", return_value="lrc") as lrc:
            self.assertEqual(fetch_from(api, track, "lrclib"), "lrc")
        lrc.assert_called_once()
        from tide.lyric_words import line_sheet
        with mock.patch("tide.lyrics_netease.fetch_yrc",
                        return_value=line_sheet([(1.0, "one"), (4.0, "two")])):
            got = fetch_from(api, track, "netease")
        self.assertEqual(got.source, "netease")
        self.assertEqual([t for t, _l in got.timed_lines], [1.0, 4.0])


class MenuTest(_Base):
    def _ready(self):
        self._apply("blackwater")
        view = self._view()
        track = SimpleNamespace(video_id="v", source="ytmusic", title="t")
        view._current_track = track
        view._current_key = "ytmusic:v"
        view._current_video_id = "v"
        self.lyrics.song_changed(track)
        view._show_timed([(i * 3.0, f"line {i}") for i in range(10)])
        QTest.qWait(20)
        return view

    def setUp(self) -> None:
        super().setUp()
        from tide.ui import lyrics
        self.lyrics = lyrics

    def _labels(self, menu) -> list[str]:
        return [a.text() for a in menu.actions() if a.text()]

    def test_line_menu(self) -> None:
        from PySide6.QtWidgets import QWidgetAction
        view = self._ready()
        lbl = view._line_widgets[3]
        self.lyrics.set_seeker(lambda s: None)
        try:
            menu = view._build_menu(lbl.mapToGlobal(lbl.rect().center()))
        finally:
            self.lyrics.set_seeker(None)
        labels = self._labels(menu)
        case = theming.styled_case
        for want in ("play from here", "copy line", "copy all lyrics", "lyrics from"):
            self.assertIn(case(want), labels)
        self.assertTrue(any(isinstance(a, QWidgetAction) for a in menu.actions()),
                        "the timing row")
        menu.deleteLater()

    def test_surface_items_come_after(self) -> None:
        view = self._ready()
        seen = []

        def extra(menu):
            seen.append(menu)
            menu.addAction("exit")
        view.set_menu_extra(extra)
        menu = view._build_menu(view.mapToGlobal(view.rect().center()))
        self.assertEqual(seen, [menu])
        self.assertEqual(menu.actions()[-1].text(), "exit")
        menu.deleteLater()

    def test_nudge_moves_this_song_only(self) -> None:
        from tide import lyric_timing, lyrics_picks
        view = self._ready()
        view.nudge_timing(2)
        self.assertEqual(lyrics_picks.offset_ms("ytmusic:v"), 500)
        self.assertAlmostEqual(view._shift(), -0.5)
        self.assertAlmostEqual(lyric_timing.offset_s(), -0.5, msg="discord + plugins too")
        view.update_position(3.2)        # lyric time 2.7: still line 0
        self.assertEqual(view._active_line_index, 0)
        self.lyrics.song_changed(SimpleNamespace(video_id="other", source="ytmusic"))
        self.assertAlmostEqual(view._shift(), 0.0, msg="another song: none of it")
        view.reset_timing()
        self.assertEqual(lyrics_picks.offset_ms("ytmusic:v"), 0)

    def test_pick_source_fetches_again(self) -> None:
        from tide import lyrics_picks
        view = self._ready()
        with mock.patch.object(view, "_retry") as retry:
            view.pick_source("lrclib")
            view.pick_source("lrclib")       # already the pick: nothing
        self.assertEqual(lyrics_picks.source("ytmusic:v"), "lrclib")
        retry.assert_called_once()

    def test_copy_line_takes_the_sung_one(self) -> None:
        view = self._ready()
        view.update_position(6.5)
        view.copy_line()
        self.assertEqual(QApplication.clipboard().text(), "line 2")


class KeysTest(unittest.TestCase):
    def test_lyrics_keys_dont_collide(self) -> None:
        from tide.ui.window import ACTIONS, LYRICS_ACTION_IDS
        self.assertEqual(LYRICS_ACTION_IDS, {"lyrics_karaoke", "lyrics_earlier", "lyrics_later",
                                             "lyrics_timing_reset", "lyrics_copy"})
        keys = [a.default for a in ACTIONS if a.default]
        self.assertEqual(len(keys), len(set(keys)), "no default bound twice")


class LayoutTest(_Base):
    """The tab's layout: the setting, else the theme's [slots] lyrics,
    else the list. Embeds always keep the list."""

    def tearDown(self) -> None:
        from tide.ui import lyrics
        lyrics.set_layout_pref("theme")
        super().tearDown()

    def test_setting_then_theme_then_list(self) -> None:
        from tide.ui import lyrics

        class Stub:
            def __init__(self, slots):
                self.slots = slots

            def t(self, kind, key, default=None):
                return self.slots.get(key, default)
        self.assertEqual(lyrics.layout_for(Stub({})), "list")
        self.assertEqual(lyrics.layout_for(Stub({"lyrics": "beside"})), "beside")
        self.assertEqual(lyrics.layout_for(Stub({"lyrics": "sideways"})), "list")
        lyrics.set_layout_pref("center")
        self.assertEqual(lyrics.layout_for(Stub({"lyrics": "beside"})), "center")
        lyrics.set_layout_pref("whatever")
        self.assertEqual(lyrics.layout_pref(), "theme")

    def test_bundled_themes_say_list(self) -> None:
        from tide.ui import lyrics
        for theme in self._mgr.list_themes():
            with self.subTest(theme=theme.slug):
                self.assertEqual(lyrics.layout_for(theme), "list")

    def test_center_centres_the_list(self) -> None:
        from tide.ui import lyrics
        self._apply("blackwater")
        view = self._view()
        view._show_timed([(0.0, "one"), (3.0, "♪"), (5.0, "three")])
        lyrics.set_layout_pref("center")
        self.assertTrue(view._centered)
        self.assertTrue(view._line_widgets[0].alignment() & Qt.AlignHCenter)
        self.assertTrue(view._marker._centered)
        self.assertTrue(view._list_sweep._snug)
        view._show_timed([(0.0, "new"), (3.0, "lines")])
        self.assertTrue(view._line_widgets[1].alignment() & Qt.AlignHCenter,
                        "a new sheet comes in centred")
        lyrics.set_layout_pref("list")
        self.assertFalse(view._line_widgets[0].alignment() & Qt.AlignHCenter)

    def test_beside_needs_room(self) -> None:
        from tide.ui import lyrics
        self._apply("blackwater")
        view = self._view()
        view.resize(1000, 500)
        lyrics.set_layout_pref("beside")
        QTest.qWait(10)
        self.assertTrue(view._side.isVisible())
        view.show_for(None)
        self.assertEqual(view._side_title.text(), theming.styled_case("nothing playing"))
        view.resize(500, 500)
        QTest.qWait(10)
        self.assertFalse(view._side.isVisible(), "narrow: the list")

    def test_embeds_keep_the_list(self) -> None:
        from tide.ui import lyrics
        self._apply("blackwater")
        view = self._view(tab=False)
        lyrics.set_layout_pref("center")
        self.assertIsNone(view._side)
        self.assertFalse(view._centered)


class _CacheDir(unittest.TestCase):
    """A throwaway lyrics cache, and no shared answers left from before."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from tide import config, lyrics_provider
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = mock.patch.object(config, "LYRICS_CACHE_DIR", Path(self._tmp.name))
        self._dir.start()
        lyrics_provider._memo.clear()

    def tearDown(self) -> None:
        from tide import lyrics_provider
        lyrics_provider._memo.clear()
        self._dir.stop()
        self._tmp.cleanup()


TRACK = SimpleNamespace(video_id="v", source="ytmusic", title="t", artists="a",
                        album="", duration_seconds=200, thumbnail="")


class SharedFetchTest(_CacheDir):
    def test_callers_share_one_fetch(self) -> None:
        import threading
        import time
        from tide import lyrics_provider
        from tide.lyrics_provider import LyricsResult
        calls = []

        def slow(api, track, source):
            calls.append(source)
            time.sleep(0.1)
            return LyricsResult(timed_lines=[(0.0, "x")])
        out = []
        with mock.patch.object(lyrics_provider, "_fetch_from", side_effect=slow):
            threads = [threading.Thread(target=lambda: out.append(
                lyrics_provider.fetch_from(None, TRACK, None))) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(r is out[0] for r in out))
            lyrics_provider.fetch_from(None, TRACK, None, force=True)
            self.assertEqual(len(calls), 2, "try again asks again")
            lyrics_provider.fetch_from(None, TRACK, "lrclib")
            self.assertEqual(calls[-1], "lrclib", "a pick is its own answer")

    def test_a_miss_isnt_kept(self) -> None:
        from tide import lyrics_provider
        with mock.patch.object(lyrics_provider, "_fetch_from", return_value=None) as f:
            lyrics_provider.fetch_from(None, TRACK, None)
            lyrics_provider.fetch_from(None, TRACK, None)
        self.assertEqual(f.call_count, 2)


class YouTubeCacheTest(_CacheDir):
    def _source(self):
        from tide.sources.ytmusic import YTMusicSource
        return YTMusicSource.__new__(YTMusicSource)

    def test_timed_lyrics_are_kept_on_disk(self) -> None:
        from tide.lyrics_provider import LyricsResult
        src = self._source()
        got = LyricsResult(timed_lines=[(1.0, "one")], line_ends=[2.0], source="youtube music")
        with mock.patch.object(src, "_fetch_yt_timed", return_value=got) as f:
            first = src._yt_timed_lyrics("vid")
            again = src._yt_timed_lyrics("vid")
        self.assertEqual(f.call_count, 1)
        self.assertEqual(again.timed_lines, [(1.0, "one")])
        self.assertEqual(again.line_ends, [2.0])
        self.assertEqual(again.source, "youtube music")
        self.assertIs(first, got)

    def test_none_is_kept_trouble_isnt(self) -> None:
        src = self._source()
        with mock.patch.object(src, "_fetch_yt_timed", return_value=None) as f:
            src._yt_timed_lyrics("a")
            src._yt_timed_lyrics("a")
        self.assertEqual(f.call_count, 1, "no lyrics: asked once")
        with mock.patch.object(src, "_fetch_yt_timed", side_effect=OSError("down")) as f:
            src._yt_timed_lyrics("b")
            src._yt_timed_lyrics("b")
        self.assertEqual(f.call_count, 2, "trouble: asked again")

    def test_plain_lyrics_too(self) -> None:
        src = self._source()
        with mock.patch.object(src, "_fetch_plain_lyrics", return_value="words") as f:
            self.assertEqual(src.get_lyrics_for("vid"), "words")
            self.assertEqual(src.get_lyrics_for("vid"), "words")
        self.assertEqual(f.call_count, 1)


class TrackerPickTest(_CacheDir):
    def test_tracker_takes_the_pick(self) -> None:
        import tempfile
        from pathlib import Path
        from tide import lyric_tracker, lyrics_picks, lyrics_provider
        from tide.lyrics_provider import LyricsResult
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(lyrics_picks, "PATH", Path(tmp) / "p.json"):
            lyrics_picks.set_source("ytmusic:v", "netease")
            src = lyrics_picks.source("ytmusic:v")
        plain = LyricsResult(plain_text="no timing", source="netease")
        with mock.patch.object(lyrics_provider, "fetch_from", return_value=plain) as f, \
                mock.patch.object(lyrics_provider, "fetch_lrclib") as lrc:
            worker = lyric_tracker._FetchWorker(None, TRACK, src)
            got = []
            worker.done.connect(lambda vid, r: got.append(r))
            worker.run()
        f.assert_called_once_with(None, TRACK, "netease")
        lrc.assert_not_called()
        self.assertIs(got[0], plain)


class NetEaseRomanTest(_CacheDir):
    def _replies(self, url: str):
        if "search" in url:
            return {"result": {"songs": [{"id": 7, "name": "t", "artists": [{"name": "a"}],
                                          "duration": 200000}]}}
        return {"lrc": {"lyric": "[00:01.00]夜に駆ける\n[00:05.50]沈むように"},
                "romalrc": {"lyric": "[00:01.00]yo ru ni ka ke ru\n[00:05.50]shi zu mu yo u ni"}}

    def test_pairs_by_timestamp_from_one_lookup(self) -> None:
        from tide import lyrics_netease as ne
        with mock.patch.object(ne, "_get_json", side_effect=self._replies) as get:
            pairs = ne.fetch_roman(title="t", artist="a", duration_seconds=200)
            self.assertIsNone(ne.fetch_yrc(title="t", artist="a", duration_seconds=200))
        self.assertEqual(pairs, [(1.0, "夜に駆ける", "yo ru ni ka ke ru"),
                                 (5.5, "沈むように", "shi zu mu yo u ni")])
        self.assertEqual(get.call_count, 2, "search + lyric, once for both")


class RomanViewTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        from tide.ui import lyrics
        self.lyrics = lyrics
        self.saved = []
        lyrics.set_romanized_saver(self.saved.append)

    def tearDown(self) -> None:
        self.lyrics.set_romanized(False, save=False)
        self.lyrics.set_romanized_saver(None)
        super().tearDown()

    def test_wants_roman(self) -> None:
        self.assertTrue(self.lyrics.wants_roman(["夜に駆ける", "沈むように溶けてゆくように"]))
        self.assertFalse(self.lyrics.wants_roman(["I'm losing the person I was", "when I found you"]))

    def test_lines_pick_up_their_romaji(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._current_key = "ytmusic:v"
        view._show_timed([(1.2, "夜に駆ける"), (3.0, ""), (5.7, "沈むように")])
        QTest.qWait(10)
        short = view._line_widgets[0].height()
        view._on_roman("ytmusic:v", [(1.0, "夜に駆ける", "yo ru ni ka ke ru"),
                                     (5.5, "沈むように", "shi zu mu yo u ni")])
        self.assertEqual(view._roman, ["yo ru ni ka ke ru", None, "shi zu mu yo u ni"])
        self.assertTrue(view.has_roman())
        self.assertIsNone(view._line_widgets[0]._roman, "off until switched on")
        self.lyrics.set_romanized(True)
        self.assertEqual(self.saved, [True])
        self.assertEqual(view._line_widgets[2]._roman, "shi zu mu yo u ni")
        QTest.qWait(20)
        self.assertGreater(view._line_widgets[0].height(), short, "room for the romaji")

    def test_menu_offers_it_only_when_there(self) -> None:
        self._apply("blackwater")
        view = self._view()
        view._current_key = "ytmusic:v"
        view._show_timed([(1.0, "夜に駆ける"), (5.5, "沈むように")])
        QTest.qWait(10)
        lbl = view._line_widgets[0]
        where = lbl.mapToGlobal(lbl.rect().center())
        label = theming.styled_case("show romanized")
        menu = view._build_menu(where)
        self.assertNotIn(label, [a.text() for a in menu.actions()])
        menu.deleteLater()
        view._on_roman("ytmusic:v", [(1.0, "夜に駆ける", "a"), (5.5, "沈むように", "b")])
        menu = view._build_menu(where)
        self.assertIn(label, [a.text() for a in menu.actions()])
        menu.deleteLater()


class CardTest(_Base):
    def test_render_sizes(self) -> None:
        from tide.ui import lyric_card
        self._apply("blackwater")
        cover = QImage(64, 64, QImage.Format_RGB32)
        cover.fill(QColor("#c0603a"))
        long = "a line long enough to wrap a couple of times on the card " * 2
        for shape, size in (("square", (1080, 1080)), ("tall", (1080, 1920))):
            img = lyric_card.render([long] * 4, title="porch light", artist="the band",
                                    cover=cover, shape=shape,
                                    theme=self._mgr.current_effective())
            self.assertEqual((img.width(), img.height()), size)
        top, bottom = lyric_card.card_colors(cover, None)
        self.assertLess(top.valueF(), 0.4, "deep enough for light text")

    def test_dialog_picks_lines(self) -> None:
        from tide.ui.lyric_card import CardDialog, MAX_LINES
        self._apply("blackwater")
        lines = [f"line {i}" for i in range(6)]
        d = CardDialog(lines, 4, title="t", artist="a", cover=None)
        self.assertEqual(d.chosen(), ["line 4", "line 5"])
        d._resize(1)
        self.assertEqual(d.chosen(), ["line 4", "line 5"], "no lines past the end")
        d._move(-2)
        for _ in range(5):
            d._resize(1)
        self.assertEqual(len(d.chosen()), MAX_LINES)
        d._copy()
        self.assertFalse(QApplication.clipboard().image().isNull())
        d.deleteLater()


class PrefetchTest(unittest.TestCase):
    def test_runs_the_shared_fetch_off_the_gui_thread(self) -> None:
        import threading
        from tide import lyrics_provider
        from tide.ui import lyrics
        seen = threading.Event()
        with mock.patch.object(lyrics_provider, "fetch_from",
                               side_effect=lambda *a, **k: seen.set()), \
                mock.patch.object(lyrics.lyrics_picks, "source", return_value=None):
            lyrics.prefetch(object(), TRACK)
            self.assertTrue(seen.wait(2.0))


class HeadingTest(_Base):
    def test_tab_heading_follows_the_aesthetic(self) -> None:
        self._apply("blackwater")
        view = self._view()
        self.assertFalse(view.heading.isVisible())
        self._apply("brutalist-mono")
        self.assertTrue(view.heading.isVisible())

    def test_embeds_keep_their_own_call(self) -> None:
        self._apply("brutalist-mono")
        view = self._view(tab=False)
        view.heading.hide()             # what the mini and fullscreen do
        self._apply("blackwater")
        self._apply("brutalist-mono")
        self.assertTrue(view.heading.isHidden())


if __name__ == "__main__":
    unittest.main()
