"""The hero's week panel: view.week_summary over the local history, and
the panel inside the Hero (bars, streak, new artists, on repeat).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import datetime as dt
import sys
import time
import unittest

from PySide6.QtWidgets import QApplication

from tide.history import HistoryEntry
from tide.ui.home.view import week_summary


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


NOW = time.mktime(dt.datetime(2026, 9, 28, 15, 0).timetuple())
DAY = 86400.0


def _e(vid: str, days_ago: float, artist: str = "otuka",
       secs: int = 180) -> HistoryEntry:
    return HistoryEntry(video_id=vid, title=f"song {vid}", artists=artist,
                        duration_seconds=secs,
                        played_at=NOW - days_ago * DAY,
                        thumbnail="")


def _newest_first(entries):
    return sorted(entries, key=lambda e: e.played_at, reverse=True)


class WeekSummaryTest(unittest.TestCase):
    def test_empty_week_is_none(self) -> None:
        self.assertIsNone(week_summary([_e("a", 20)], now=NOW))
        self.assertIsNone(week_summary([], now=NOW))

    def test_minutes_land_on_their_day_today_last(self) -> None:
        w = week_summary(_newest_first([
            _e("a", 0.01, secs=600), _e("b", 1, secs=120),
            _e("c", 6, secs=60), _e("d", 8, secs=999)]), now=NOW)
        self.assertEqual(w["minutes"][6], 10.0)
        self.assertEqual(w["minutes"][5], 2.0)
        self.assertEqual(w["minutes"][0], 1.0)
        self.assertEqual(len(w["days"]), 7)
        self.assertEqual(w["days"][6], "m")     # 2026-09-28 is a monday

    def test_streak_counts_back_from_today(self) -> None:
        w = week_summary(_newest_first(
            [_e(str(i), i + 0.01) for i in range(4)] + [_e("x", 6)]), now=NOW)
        self.assertEqual(w["streak"], 4)

    def test_streak_survives_a_day_not_started_yet(self) -> None:
        w = week_summary(_newest_first(
            [_e(str(i), i + 1) for i in range(3)]), now=NOW)
        self.assertEqual(w["streak"], 3)

    def test_on_repeat_needs_three_plays(self) -> None:
        w = week_summary(_newest_first(
            [_e("loop", d) for d in (0.1, 0.2, 1, 2, 3)]
            + [_e("twice", 0.3), _e("twice", 0.4)]), now=NOW)
        self.assertEqual([(t.video_id, n) for t, n in w["repeat"]],
                         [("loop", 5)])

    def test_new_artists_need_history_to_compare_against(self) -> None:
        old = [_e("o", 30, artist="radiohead")]
        week = [_e("n", 1, artist="akiradoves"), _e("r", 2, artist="radiohead")]
        w = week_summary(_newest_first(old + week), now=NOW)
        self.assertEqual(w["new"], ["akiradoves"])
        # Only a week of history: everything would be "new", so nothing is.
        self.assertEqual(week_summary(_newest_first(week), now=NOW)["new"], [])


class HeroPanelTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()

    def test_the_hero_carries_the_week(self) -> None:
        from tide.ui.home.patterns import Hero
        week = week_summary(_newest_first(
            [_e("loop", d) for d in (0.1, 0.2, 1)] + [_e("x", 30)]), now=NOW)
        seen = []
        hero = Hero("good afternoon", "", _e("loop", 0.1).to_track(),
                    can_resume=False, show_likes=False, week=week)
        hero.track_clicked.connect(seen.append)
        self.assertIsNotNone(hero.week_panel)
        self.assertEqual(len(hero.week_panel.repeat_rows), 1)
        hero.week_panel.repeat_rows[0].clicked.emit(week["repeat"][0][0])
        self.assertEqual([t.video_id for t in seen], ["loop"])

    def test_no_week_no_panel(self) -> None:
        from tide.ui.home.patterns import Hero
        hero = Hero("good afternoon", "", _e("a", 0.1).to_track(),
                    can_resume=False, show_likes=False, week=None)
        self.assertIsNone(hero.week_panel)


if __name__ == "__main__":
    unittest.main()
