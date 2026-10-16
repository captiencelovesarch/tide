"""2.3 waveform seek bar — loading frontier, the post-load reveal, seeking.

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/test_waveform_seek.py
"""
import sys
import unittest
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from tide.ui import bar_config, motion
from tide.ui.variants import make_progress
from tide.ui.waveform_seek import WaveformSeek, bars_for, envelope


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _map(duration, complete, swell=None, **extra):
    return SimpleNamespace(duration=duration, complete=complete,
                           swell=tuple(swell or [0.5] * int(duration * 4)),
                           swell_rate=4.0, **extra)


class PureTests(unittest.TestCase):
    def test_envelope_prefers_full_band(self) -> None:
        m = _map(2, True, loudness=(0.1, 0.9), loudness_rate=1.0)
        self.assertEqual(envelope(m), ((0.1, 0.9), 1.0))
        self.assertEqual(envelope(_map(1, True))[1], 4.0)
        self.assertEqual(envelope(None), ((), 0.0))

    def test_bars_stop_at_the_frontier(self) -> None:
        vals = [0.2] * 40                       # 10 s at 4 Hz
        bars = bars_for(vals, 4.0, total=20.0, frontier=10.0, count=10)
        self.assertEqual(bars[:5], [0.2] * 5)
        self.assertEqual(bars[5:], [None] * 5)

    def test_bars_blend_mean_and_peak(self) -> None:
        vals = [0.1] * 8
        vals[3] = 0.9
        first, second = bars_for(vals, 4.0, 2.0, 2.0, 2)
        self.assertAlmostEqual(first, 0.75 * 0.3 + 0.25 * 0.9)   # the hit shows...
        self.assertLess(first, 0.9)                              # ...without pinning
        self.assertAlmostEqual(second, 0.1)

    def test_real_beat_map_loudness_drives_the_bars(self) -> None:
        import numpy as np
        from tide import beat_map
        rng = np.random.default_rng(1)
        sr = beat_map.SR
        quiet = 0.05 * rng.standard_normal(6 * sr)
        loud = 0.6 * rng.standard_normal(6 * sr)
        an = beat_map.Analysis()
        an.push(np.concatenate([quiet, loud]).astype(np.float32))
        m = an.build(True)
        values, rate = envelope(m)
        self.assertEqual(rate, beat_map.LOUDNESS_RATE)
        self.assertEqual(values, m.loudness)
        bars = bars_for(values, rate, m.duration, m.duration, 12)
        self.assertLess(max(bars[1:5]), 0.5 * min(bars[7:11]))
        # a partial map draws only up to its frontier
        part = beat_map.Analysis()
        part.push(quiet.astype(np.float32))
        pm = part.build(False)
        pv, pr = envelope(pm)
        pbars = bars_for(pv, pr, 12.0, pm.duration, 12)
        self.assertIsNone(pbars[-1])
        self.assertIsNotNone(pbars[0])

    def test_degenerate(self) -> None:
        self.assertEqual(bars_for([], 4.0, 10, 10, 3), [None] * 3)
        self.assertEqual(bars_for([0.5], 4.0, 10, 10, 0), [])

    def test_config_mode(self) -> None:
        self.assertEqual(bar_config.normalize({})["waveform"], "on")
        self.assertEqual(bar_config.normalize({"waveform": "x"})["waveform"], "on")
        self.assertEqual(bar_config.normalize({"waveform": "hover"})["waveform"], "hover")


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self._prev = motion._user_intensity
        motion.set_intensity(motion.Intensity.OFF)
        self.addCleanup(motion.set_intensity, self._prev)
        self.host = QWidget()
        lay = QVBoxLayout(self.host)
        self.bar = make_progress("bar")
        lay.addWidget(self.bar)
        self.host.resize(400, 60)
        self.host.show()
        self.current = None
        self.wf = WaveformSeek(lambda: self.current)
        self.wf.attach(self.bar)
        self.wf.set_duration(20.0)
        self.wf.track_changed()
        QTest.qWait(10)

    def tearDown(self) -> None:
        self.wf._poll.stop()
        self.wf._hold.stop()
        self.host.deleteLater()
        QTest.qWait(10)

    def test_loading_shows_the_frontier(self) -> None:
        self.current = _map(5.0, False)
        self.wf._pull_map()
        self.assertEqual(self.wf.state(), "loading")
        self.assertEqual(self.wf.alpha(), 1.0)
        self.assertTrue(self.wf.overlay.loading)
        self.assertEqual(self.wf.overlay.frontier, 5.0)
        self.current = _map(12.0, False)
        self.wf._pull_map()
        self.assertEqual(self.wf.overlay.frontier, 12.0)

    def test_loaded_reveal_then_fades(self) -> None:
        self.current = _map(20.0, True)
        self.wf._pull_map()
        self.assertEqual(self.wf.state(), "idle")      # not playing yet
        self.wf.set_playing(True)
        self.assertEqual(self.wf.state(), "loaded")
        self.assertEqual(self.wf.alpha(), 1.0)
        self.wf._hold.stop()
        self.wf._settle()                                # hold elapsed
        self.assertEqual(self.wf.state(), "idle")
        self.assertEqual(self.wf.alpha(), 0.0)
        self.wf.set_playing(False)
        self.wf.set_playing(True)                        # once per track
        self.assertEqual(self.wf.state(), "idle")

    def test_hover_shows_with_a_cursor_line(self) -> None:
        self.current = _map(20.0, True)
        self.wf._pull_map()
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QEnterEvent
        self.wf.eventFilter(self.bar, QEnterEvent(QPointF(100, 4), QPointF(100, 4), QPointF(100, 4)))
        self.assertEqual(self.wf.state(), "seeking")
        self.assertEqual(self.wf.alpha(), 1.0)
        self.wf.eventFilter(self.bar, QEvent(QEvent.Leave))
        self.assertEqual(self.wf.state(), "idle")
        self.assertIsNone(self.wf.overlay.hover_x)

    def test_the_bar_stays_and_the_waveform_stands_on_it(self) -> None:
        self.current = _map(5.0, False)
        self.wf._pull_map()
        self.assertIsNone(self.bar.graphicsEffect())        # never faded out
        ov, g = self.wf.overlay.geometry(), self.bar.geometry()
        self.assertLessEqual(ov.bottom(), g.top())           # above the bar
        self.assertGreaterEqual(ov.bottom(), g.top() - 3)    # standing on it
        self.wf.set_mode("off")
        self.assertEqual(self.wf.state(), "idle")

    def test_played_part_feathers_into_unplayed(self) -> None:
        from PySide6.QtGui import QColor
        from tide.ui.waveform_seek import blend, feather
        self.assertEqual(feather(0, 100, 40), 0.0)
        self.assertEqual(feather(200, 100, 40), 1.0)
        self.assertAlmostEqual(feather(100, 100, 40), 0.5)
        mids = [feather(x, 100, 40) for x in range(80, 121, 5)]
        self.assertEqual(mids, sorted(mids))                 # monotone, no jump
        self.assertTrue(all(b - a < 0.35 for a, b in zip(mids, mids[1:])))
        c = blend(QColor("#000000"), QColor("#ffffff"), 0.5)
        self.assertAlmostEqual(c.redF(), 0.5, places=2)

    def test_hover_mode_skips_loading(self) -> None:
        self.wf.set_mode("hover")
        self.current = _map(5.0, False)
        self.wf._pull_map()
        self.assertEqual(self.wf.state(), "idle")

    def test_overlay_never_takes_the_mouse(self) -> None:
        self.assertTrue(self.wf.overlay.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_paints_without_error(self) -> None:
        self.current = _map(5.0, False)
        self.wf._pull_map()
        self.wf.overlay.hover_x = 50
        self.wf.overlay.grab()


if __name__ == "__main__":
    unittest.main()
