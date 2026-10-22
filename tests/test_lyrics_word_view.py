"""LyricsView with word timing (offscreen): the karaoke takes the
service's sheet, upgrades in place, names its timing on the checkbox,
and the lyrics offset moves every surface."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tide import lyric_timing, lyric_words as lw  # noqa: E402
from tide.lyric_words import Word, WordLine  # noqa: E402
from tide.lyrics_provider import LyricsResult  # noqa: E402

_app = QApplication.instance() or QApplication([])

LINES = [(1.0, "one two three"), (4.0, "four five")]


class _Svc(lyric_timing.WordTimingService):
    """No worker: answers line mode, the test pushes upgrades."""

    def sheet_for(self, track, result):
        return lw.line_sheet(list(result.timed_lines), list(result.line_ends))


class ViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = lyric_timing._service
        lyric_timing._service = _Svc()
        self.addCleanup(setattr, lyric_timing, "_service", self._orig)
        from tide.ui import lyrics
        self.lyrics = lyrics
        self.addCleanup(lyrics.set_offset_ms, 0)
        self.view = lyrics.LyricsView(SimpleNamespace())
        self.addCleanup(_app.processEvents)
        self.addCleanup(self.view.deleteLater)
        self.track = SimpleNamespace(video_id="v1", source="ytmusic", title="t",
                                     artists="a", album="", duration_seconds=0)
        # show_for without the fetch thread
        self.view._current_video_id = "v1"
        self.view._current_track = self.track
        self.view._current_key = lyric_timing.track_key(self.track)
        self.view._on_done("v1", LyricsResult(timed_lines=list(LINES)))

    def test_line_mode_first(self) -> None:
        self.assertIn("estimated words", self.view.karaoke_check.text())
        sheet = self.view._karaoke_widget.sheet()
        self.assertEqual([l.timing for l in sheet], ["line", "line"])

    def test_upgrade_keeps_the_line_widgets(self) -> None:
        before = list(self.view._line_widgets)
        words = (Word(1.2, 1.6, "one"), Word(1.6, 2.0, "two"), Word(2.0, 2.8, "three"))
        upgraded = [WordLine(1.2, 2.8, "one two three", words, "source"),
                    lw.line_sheet([(4.0, "four five")])[0]]
        lyric_timing.service().updated.emit("ytmusic:v1", upgraded)
        self.assertEqual(self.view._line_widgets, before)
        self.assertIn("synced words", self.view.karaoke_check.text())
        self.assertEqual(self.view._timed_lines[0][0], 1.2)

    def test_other_tracks_sheets_are_ignored(self) -> None:
        lyric_timing.service().updated.emit("ytmusic:someone-else", [
            WordLine(0.0, 1.0, "nope", (Word(0.0, 1.0, "nope"),), "source")])
        self.assertEqual(self.view._timed_lines[0][1], "one two three")

    def test_karaoke_follows_the_sheet(self) -> None:
        self.view.karaoke_check.setChecked(True)
        self.view.update_position(1.5)
        cur = self.view._karaoke_widget.current_line.line()
        self.assertEqual(cur.text, "one two three")
        self.view.update_position(4.5)
        self.assertEqual(self.view._karaoke_widget.current_line.line().text, "four five")
        self.assertEqual(self.view._karaoke_widget.prev_label.text(), "one two three")

    def test_list_sweeps_the_active_line(self) -> None:
        self.view.update_position(1.5)
        sweep = self.view._list_sweep
        self.assertEqual(self.view._sweep_at, 0)
        self.assertEqual(sweep.line().text, "one two three")
        self.assertTrue(self.view._line_widgets[0].isHidden())
        self.assertIs(self.view._timed_layout.itemAt(0).widget(), sweep)
        # next line: the sweep moves down, the first label comes back
        self.view.update_position(4.2)
        self.assertEqual(self.view._sweep_at, 1)
        self.assertFalse(self.view._line_widgets[0].isHidden())
        self.assertTrue(self.view._line_widgets[1].isHidden())
        self.assertIs(self.view._timed_layout.itemAt(1).widget(), sweep)
        self.assertEqual(self.view._timed_layout.count(), 2 + 1 + 1)   # labels, sweep, stretch

    def test_list_sweep_survives_an_upgrade_and_plain(self) -> None:
        self.view.update_position(1.5)
        words = (Word(1.2, 1.6, "one"), Word(1.6, 2.0, "two"), Word(2.0, 2.8, "three"))
        lyric_timing.service().updated.emit("ytmusic:v1", [
            WordLine(1.2, 2.8, "one two three", words, "source"),
            lw.line_sheet([(4.0, "four five")])[0]])
        self.assertEqual(self.view._list_sweep.line().words, words)
        self.assertEqual(self.view._sweep_at, 0)
        self.view._show_plain("plain now")
        self.assertEqual(self.view._sweep_at, -1)
        self.assertTrue(self.view._list_sweep.isHidden())

    def test_list_sweep_toggle(self) -> None:
        self.addCleanup(self.lyrics.set_list_sweep, True)
        self.view.update_position(1.5)
        self.assertEqual(self.view._sweep_at, 0)
        self.lyrics.set_list_sweep(False)            # back to the whole line
        self.assertEqual(self.view._sweep_at, -1)
        self.assertFalse(self.view._line_widgets[0].isHidden())
        self.view.update_position(4.2)
        self.assertEqual(self.view._sweep_at, -1)
        self.lyrics.set_list_sweep(True)
        self.assertEqual(self.view._sweep_at, 1)

    def test_tab_checkbox(self) -> None:
        self.addCleanup(self.lyrics.set_list_sweep, True)
        asked = []
        self.view.list_sweep_toggled.connect(asked.append)
        self.view.sweep_check.setChecked(False)
        self.assertEqual(asked, [False])
        # the setting comes back through set_list_sweep and syncs every view
        self.lyrics.set_list_sweep(False)
        other = self.lyrics.LyricsView(SimpleNamespace())
        self.addCleanup(other.deleteLater)
        self.assertFalse(other.sweep_check.isChecked())
        # karaoke always sweeps: the checkbox greys out
        self.view.karaoke_check.setChecked(True)
        self.assertFalse(self.view.sweep_check.isEnabled())

    def test_embeds_hide_the_checkbox(self) -> None:
        # mini / fullscreen / visualizer tile hide the tab chrome by name
        view = self.lyrics.LyricsView(SimpleNamespace())
        self.addCleanup(view.deleteLater)
        for chrome in (view.heading, view.karaoke_check, view.mute_btn, view.swap_status):
            chrome.hide()
        view.show()
        _app.processEvents()
        self.assertTrue(view.sweep_check.isHidden())
        view.hide()
        self.view.show()
        _app.processEvents()
        self.assertFalse(self.view.sweep_check.isHidden())
        self.view.hide()

    def test_before_the_first_line_no_sweep(self) -> None:
        self.view.update_position(0.2)
        self.assertEqual(self.view._sweep_at, -1)

    def test_offset_moves_lyrics_later(self) -> None:
        self.view.update_position(4.2)
        self.assertEqual(self.view._active_line_index, 1)
        self.lyrics.set_offset_ms(500)          # lyrics 0.5 s later
        self.assertEqual(self.view._active_line_index, 0)
        self.assertEqual(self.lyrics.offset_ms(), 500)
        from tide import lyric_timing as lt
        self.assertAlmostEqual(lt.offset_s(), -0.5)      # the tracker reads this
        self.lyrics.set_offset_ms(99999)
        self.assertEqual(self.lyrics.offset_ms(), self.lyrics.OFFSET_RANGE_MS)

    def test_plain_lyrics_reset_the_tag(self) -> None:
        self.view._show_plain("just words")
        self.assertEqual(self.view.karaoke_check.text().lower().strip(), "karaoke mode")

    def test_timing_of(self) -> None:
        line = lw.line_sheet([(0.0, "a")])
        self.assertEqual(self.lyrics.timing_of(line), "line")
        aligned = [WordLine(0, 1, "a", (Word(0, 1, "a"),), "aligned")]
        self.assertEqual(self.lyrics.timing_of(line + aligned), "aligned")


if __name__ == "__main__":
    unittest.main()
