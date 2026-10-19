"""The steamvr plugin's offline parts: placement, the wrist watch, the
attention fade (plugins/steamvr/geometry.py), and the plugin starting and
stopping under the host with steamvr kept out of it (TIDE_VR_OFFLINE).

Run offscreen:  QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/
"""
import importlib.util
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tide.plugin_host import BUNDLED_DIR

_GEO = BUNDLED_DIR / "steamvr" / "geometry.py"
_spec = importlib.util.spec_from_file_location("steamvr_geometry", _GEO)
geo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(geo)


def at(x, y, z):
    return geo.translate(x, y, z)


HEAD = at(0.0, 1.7, 0.0)        # looking down -z


class PlacementTests(unittest.TestCase):
    def test_panel_spawns_ahead_low_and_facing_you(self):
        p = geo.spawn_in_front(HEAD)
        pos = p[:3, 3]
        self.assertAlmostEqual(pos[2], -geo.PANEL_DISTANCE_M, places=6)
        self.assertLess(pos[1], 1.7)
        # its +z points back at the head
        to_head = HEAD[:3, 3] - pos
        self.assertGreater(np.dot(p[:3, 2], to_head / np.linalg.norm(to_head)), 0.999)
        # and it stays upright
        self.assertAlmostEqual(p[:3, 0][1], 0.0, places=6)

    def test_spawn_ignores_head_pitch(self):
        looking_down = HEAD.copy()
        a = math.radians(-50)
        looking_down[1:3, 1:3] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
        p = geo.spawn_in_front(looking_down)
        self.assertAlmostEqual(p[2, 3], -geo.PANEL_DISTANCE_M, places=6)

    def test_wrist_mirrors_between_hands(self):
        left = geo.wrist_point(np.eye(4), "left")
        right = geo.wrist_point(np.eye(4), "right")
        self.assertAlmostEqual(left[0], -right[0])
        self.assertAlmostEqual(left[1], geo.WRIST_LEFT[1] + geo.WATCH_LIFT_M)

    def test_looking_at_the_wrist(self):
        self.assertTrue(geo.looking_at_wrist(HEAD, np.array([0.0, 1.55, -0.35])))
        # behind you, too far away, or off to the side
        self.assertFalse(geo.looking_at_wrist(HEAD, np.array([0.0, 1.55, 0.35])))
        self.assertFalse(geo.looking_at_wrist(HEAD, np.array([0.0, 1.55, -0.9])))
        self.assertFalse(geo.looking_at_wrist(HEAD, np.array([0.4, 1.7, -0.3])))


class ShownTests(unittest.TestCase):
    def test_delays_both_ways(self):
        s = geo.Shown(0.15, 0.5)
        self.assertFalse(s.update(0.0, True))
        self.assertFalse(s.update(0.1, True))
        self.assertTrue(s.update(0.16, True))
        self.assertTrue(s.update(0.3, False))
        self.assertTrue(s.update(0.7, False))
        self.assertFalse(s.update(0.81, False))

    def test_a_glance_doesnt_flicker(self):
        s = geo.Shown(0.15, 0.5)
        s.update(0.0, True)
        s.update(0.1, False)        # looked away before it showed
        self.assertFalse(s.update(0.2, True))


class AttentionTests(unittest.TestCase):
    def run_for(self, att, start, seconds, gaze, hand=9.0, touch_at=-100.0, dt=1 / 90):
        t = start
        alpha, closed = att.alpha, False
        while t < start + seconds and not closed:
            t += dt
            alpha, closed = att.update(t, gaze, hand, t - touch_at)
        return t, alpha, closed

    def test_stays_while_looked_at(self):
        att = geo.Attention(2.5)
        att.reset(0.0)
        _t, alpha, closed = self.run_for(att, 0.0, 10.0, gaze=5.0)
        self.assertEqual((alpha, closed), (1.0, False))

    def test_looking_away_dims_then_closes(self):
        att = geo.Attention(2.5, fade_s=0.6)
        att.reset(0.0)
        t, alpha, closed = self.run_for(att, 0.0, 2.0, gaze=80.0)
        self.assertFalse(closed)
        self.assertAlmostEqual(alpha, att.DIM_ALPHA, places=3)
        t, alpha, closed = self.run_for(att, t, 2.0, gaze=80.0)
        self.assertTrue(closed)
        # linger plus the fade from the dim level, give or take a tick
        self.assertAlmostEqual(t, 2.5 + att.DIM_ALPHA * 0.6, delta=0.05)

    def test_looking_back_mid_fade_brings_it_back(self):
        att = geo.Attention(2.5, fade_s=0.6)
        att.reset(0.0)
        t, alpha, closed = self.run_for(att, 0.0, 2.8, gaze=80.0)
        self.assertLess(alpha, att.DIM_ALPHA)
        self.assertFalse(closed)
        t, alpha, closed = self.run_for(att, t, 1.0, gaze=5.0)
        self.assertEqual((alpha, closed), (1.0, False))

    def test_hand_near_or_a_recent_touch_keeps_it(self):
        for kw in ({"hand": 0.2}, {"touch_at": 9.0}):
            att = geo.Attention(2.5)
            att.reset(0.0)
            _t, alpha, closed = self.run_for(att, 0.0, 10.0, gaze=80.0, **kw)
            with self.subTest(kw):
                self.assertFalse(closed)

    def test_motion_off_skips_the_fade(self):
        att = geo.Attention(2.5, animate=False)
        att.reset(0.0)
        t, alpha, closed = self.run_for(att, 0.0, 5.0, gaze=80.0)
        self.assertTrue(closed)
        # the look-away clock starts on the first tick; two ticks of slack
        self.assertAlmostEqual(t, 2.5, delta=2 / 90 + 1e-6)


class PluginLifecycleTests(unittest.TestCase):
    """the real plugin under the real host, steamvr kept out."""

    @classmethod
    def setUpClass(cls):
        try:
            import openvr  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pyopenvr isn't installed")
        os.environ["TIDE_VR_OFFLINE"] = "1"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("TIDE_VR_OFFLINE", None)

    def test_starts_renders_and_stops(self):
        from PySide6.QtWidgets import QApplication
        from tide.plugin_host import PluginHost
        from tests.test_plugins import FakePlayer, FakeQueue
        app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.mkdtemp(prefix="tide-vr-"))
        host = PluginHost(None, FakePlayer(), FakeQueue(), roots=[(BUNDLED_DIR, "bundled")],
                          state_file=tmp / "p.toml", settings_dir=tmp / "s",
                          stamp=tmp / "stamp")
        self.assertTrue(host.set_enabled("steamvr", True), host.record("steamvr").status)
        plugin = host.record("steamvr").instance
        for _ in range(20):
            app.processEvents()
        self.assertEqual(host.record("steamvr").detail, "waiting for steamvr")
        img = plugin.panel.frame()
        self.assertEqual((img.width(), img.height()), (900, 420))
        host.set_enabled("steamvr", False)
        # stopped, and every qt object it adopted is gone right away (not
        # left for python's exit, where deleting them crashed)
        import shiboken6
        for obj in (plugin.thread, plugin.panel, plugin.surface, plugin.bridge):
            self.assertFalse(shiboken6.isValid(obj))
        self.assertNotIn("tide_plugins.steamvr.vr", sys.modules)


if __name__ == "__main__":
    unittest.main()
